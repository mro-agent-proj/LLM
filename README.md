# LLM2 조사·판정 / LLM3 검토·인계

## 구현 범위

- PDF의 `app/agent/nodes/investigate.py`, `review.py` 경로와 `async (state) -> dict` 계약.
- 모델이 현재 질문에 따라 다음 조회 도구를 선택하는 제한된 조사 루프.
- 도구 호출마다 확인할 질문·실제 인자·결과·시도 횟수·반환 ID 기록.
- 읽기 전용 도구 허용 목록, 10초 타임아웃, 일시적 연결 실패 시 1회 재시도.
- 원문 ID/발췌/부서 검증, 재조사 최대 2회, 내부 보류 → HUMAN.
- 최종 COMPLETED payload 준비 전 문서·이력·부서 재조회.
- 실제 localhost MCP Streamable HTTP 전송 시험과 합성 모델로 하는 그래프 시험.

노드 안에서는 DB에 직접 쓰거나 배정하지 않는다. `workflow.py`도 callback payload를 준비할 뿐
Spring에 전송하지 않는다. 팀 AI 서버 실행기가 /runs·콜백·FAILED 처리·5분 제한·Postgres 체크포인트를 소유한다.
LLM1 초기 분류와 직접 배정 경로는 구현하지 않았다.

## 실행

```sh
uv sync --locked
uv run pytest -q
uv run ruff check app tests
uv run python -m app.agent.demo
```

Python 3.12를 사용한다. 데모의 기체·사례·조사 결과·부서 배정은 모두 합성이며,
실제 FAA 사례를 검색하거나 실제 LLM 품질을 평가한 결과가 아니다.

## 실제 모델 연결

`.env.example`을 `.env`로 복사하고 팀에서 선택한 모델·제공사·키를 로컬에 설정한다.
API 키는 코드·채팅·버전 관리에 넣지 않는다. 모델은 tool planning과 구조화 출력을 지원해야 한다.
`LLM_MODEL`과 `LLM_API_KEY`가 비어 있으면 실제 실행은 명확한 설정 오류로 중단된다.

코어는 `StructuredModel.complete(schema, system, context)`에 의존하므로 모델을 주입할 수 있다.
기본 LangChain 어댑터는 `with_structured_output`을 사용한다. 제공사별 SDK와 초기화 인자는
팀 모델 선택 후 확인한다. OpenAI 제공사를 선택하는 경우에만 다음 extra를 설치할 수 있다.

```sh
uv sync --locked --extra openai
```

모델 제공사 선택은 아직 확정하지 않았다. 현재 실 LLM 호출을 성공했다고 주장하지 않는다.

## 실행기와 연결

```python
from app.agent.runtime import get_runtime
from app.agent.workflow import build_graph, completion_payload

runtime = get_runtime()
graph = build_graph(runtime, checkpointer=team_checkpointer)
# initial_state는 Spring 확인 및 LLM1의 추가 조사 분기 이후의 상태다.
state = await graph.ainvoke(
    initial_state,
    {"configurable": {"thread_id": initial_state["request_id"]}},
)
payload = await completion_payload(state, runtime)
# 실행기가 이 payload를 COMPLETED 콜백으로 Spring에 전송한다.
```

`completion_payload`를 건너뛰고 PASS만 보고 배정 콜백을 보내면 안 된다.
예외는 노드에서 삼키지 않는다. 호출자가 그래프 실행과 최종 검증 양쪽의 예외를 잡아
PDF의 FAILED 콜백으로 전송해야 한다. callback의 event_id, 재시도 1·5·30초, 중복 제거는 실행기/Spring 책임이다.
이 저장소에는 Spring으로 요청을 전송하는 HTTP 클라이언트와 /runs API가 없다.

별도 노드 호출이 필요하면 다음 두 함수가 PDF 경로에 있다.

```python
from app.agent.nodes.investigate import investigate
from app.agent.nodes.review import review
```

테스트·통합 시 의존성을 명시적으로 주입하려면 `run_investigate(state, runtime)`과
`run_review(state, runtime)`을 사용한다.

## MCP 연결

기본 URL은 business 8101/mcp, rag 8102/mcp이다. `get_tools("business", "rag")` 계약을 따른다.
앱 시작 시 미리 로드하거나 첫 사용 시 로드하고 도구 정의를 캐시한다. 각 접수의 검색 결과는 공유 캐시에 두지 않는다.

- business: get_aircraft, get_maintenance_history, search_similar_history, list_departments
- rag: search_sdr, search_amm, get_document

쓰기 도구가 서버에 존재해도 LLM2·3에는 노출하지 않는다.
정확한 기체·모델 인자를 고정하고 반환 메타데이터에 명시된 모델·항공사 불일치를 제외한다.
하지만 메타데이터 누락은 일치를 입증하지 않는다. 서버 측 tenant/항공사 권한 검사와
정확한 범위 필터를 이 클라이언트가 대신하지 않는다. 실제 MCP의 인증·범위 정보는 담당자와 연결해야 한다.

팀 PDF가 지정한 `langchain-mcp-adapters`를 유지했다. 현재 adapters는 0.2.2, MCP SDK는 1.30.0으로
lock에 고정됐다. SDK 2.x와의 import 충돌을 확인하여 `mcp<2`를 명시했다.
전송 smoke test에는 upstream의 streamable_http_client 명칭 변경 DeprecationWarning이 남는다.
새 `langchain.mcp`로의 전환은 팀 인터페이스 변경 시 검토한다.

## 확인하지 않은 범위

- 실제 팀 MCP 서버·원본 SDR·AMM 참조 색인과의 연동 및 검색 품질.
- 실제 LLM의 근거 선택·판단·배정 정확도, 지연·토큰 비용.
- Spring 콜백·권한·실제 배정·정비팀 수락/거절의 전체 통합.
- 실제 Postgres에서의 프로세스 재시작 복구. 현재 체크포인트 시험은 InMemorySaver 기반이다.
- 문서·데이터 스냅샷 동결은 서버가 버전을 제공해야 한다. 제공된 버전의 변경은 감지하지만 없는 버전을 만들어내지 않는다.

공식 확인 자료:
- https://docs.langchain.com/oss/python/langchain/models
- https://docs.langchain.com/oss/python/langgraph/graph-api
- https://github.com/langchain-ai/langchain-mcp-adapters
