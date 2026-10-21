# MRO LLM2·3 — 조사·검토와 근거 검증

팀 인터페이스 PDF 본문 v0.3을 우선하고, 읽기 도구의 상세 입력·반환은 Google MCP v1.0 및 팀 RAG 구현을 따른다.
외부 Investigation / Review / Evidence 모델은 유지한다. 실제 배정과 요청 상태 변경은 Spring 책임이다.

## 현재 범위

- LLM2: 현재 질문에 필요한 B01~B03, R01~R05를 선택하고 근거 있는 조사안을 작성.
- LLM3: 조사안 검토, 추가 근거 확인, PASS / SUPPLEMENT / HUMAN.
- 최종 검증 노드: 접수 상태·범위 버전·부서·LLM2와 LLM3의 최종 인용 근거를 재조회하고 체크포인트에 기록.
- SUPPLEMENT 재조사 최대 2회. 컨텍스트 변경 시 폐기한 판단을 보존하고 별도 1회만 범위를 다시 읽어 조사.
- 원문·출처·위치·데이터/색인 버전 변경, 반복 컨텍스트 변경은 자동 인계 중단.
- R04는 내부 참고용. 핵심 판단이 기술자료에 의존하면 기존 외부 형식을 유지하며 사람 검토로 인계.
- 평가 모델은 OpenAI gpt-4o-mini로 선택. 모델 교체는 가능하며 실제 LLM 품질 결과는 아직 없다.

이 저장소는 LLM1·RabbitMQ 소비자·Spring·업무 DB·실제 MCP 서버를 구현하지 않는다.
팀 MCP 원본 데이터와의 종단 간 연동은 별도 확인해야 한다.

## 설치와 오프라인 확인

Python 3.12와 uv를 사용한다.

~~~sh
uv sync --locked
uv run pytest -q
uv run ruff check app tests
uv run python -m app.agent.demo
uv run python -m app.agent.evaluation
~~~

데모와 기본 평가는 합성 자료·응답 대역만 사용하며 API 키가 필요 없다.
평가의 8개 기대 경로는 평가기에만 있고 모델 입력과 검색 자료에 넣지 않는다.
기대 경로 일치는 실제 고장 진단·검색 품질·정비 성공률이 아니다.

## 실제 모델 품질 평가 준비

.env.example은 LLM_MODEL=gpt-4o-mini, LLM_PROVIDER=openai다.
로컬 .env에서 LLM_API_KEY를 설정하거나 기존 환경의 OPENAI_API_KEY를 사용할 수 있다.
명시한 LLM_API_KEY가 우선하며, OpenAI 제공사에서만 OPENAI_API_KEY를 대체로 사용한다.
기존 .env를 덮어쓰지 않는다. 키는 채팅이나 Git에 넣지 않는다.
제공사 모델은 LangChain with_structured_output을 지원해야 한다.
OpenAI 제공사를 선택할 때만 해당 SDK extra를 설치한다.

이번 버전에서는 사용자 결정에 따라 실제 LLM 평가를 수행하지 않았다.

~~~sh
uv sync --locked --extra openai
uv run python -m app.agent.evaluation --live-model --case supported_handoff
~~~

--live-model은 실제 모델 사용량을 발생시킨다. 도구와 자료는 여전히 합성이다.
먼저 한 사례에서 도구 선택·조사·검토를 사람이 확인한 후 전체 8개로 확대한다.
현재 어댑터가 토큰 사용량을 수집하지 않으므로 token_usage=null이며 비용 0을 뜻하지 않는다.
평가 범위와 수동 검토 기준은 EVALUATION.md에 있다.

## 팀 실행기 연결

~~~python
from app.agent.runtime import runtime_for_request
from app.agent.workflow import build_graph, completion_payload

# Backend가 이 실행/접수/사용자 범위에 발급한 토큰을 실행기가 전달한다.
# 토큰 발급·전달 경로는 Backend 담당 모듈에서 연결해야 한다.
runtime = runtime_for_request(rag_token=backend_delegation)
graph = build_graph(runtime, checkpointer=team_checkpointer)
config = {"configurable": {"thread_id": initial_state["request_id"]}}
state = await graph.ainvoke(initial_state, config)
payload = await completion_payload(state, runtime)
# 실행기가 mro.agent.events에 COMPLETED를 발행. 실제 배정은 Spring.
~~~

build_graph는 LLM1 sufficient=False 이후 진입하는 부분 그래프다.
최종 finalize 노드가 원문을 재조회하므로 완료된 state에는 최종 검증 호출 기록과 prepared_payload가 포함된다.
completion_payload는 이 검증 결과를 추출한다. 오래된 완료 체크포인트의 payload를 새 실행에 재사용하지 않는다.
이미 완료된 그래프를 재발행해야 한다면 Spring의 현재 상태·이벤트 중복 방지 정책을 적용하고,
새 결정이 필요하면 finalize_state 결과를 체크포인트에 반영한 뒤 발행해야 한다.
단독 노드 조합은 finalize_state의 반환값을 저장해야 최종 검증 기록이 남는다.

PDF 경로의 investigate(state), review(state)를 직접 사용하는 실행기는 요청 범위 런타임을 바인딩할 수 있다.

~~~python
from app.agent.runtime import using_runtime
with using_runtime(runtime):
    output = await investigate(state)
~~~

공유 프로세스에 사용자 토큰을 전역 캐시하거나 GraphState에 넣지 않는다.
토큰 없이 기본 get_runtime을 사용하면 인증이 필요한 팀 RAG에는 연결할 수 없다.

## 실행기와 Spring이 연결할 사항

- RabbitMQ: Spring이 선언한 큐/익스체인지를 소비·발행. prefetch=1.
- 각 LLM 단계에서 STAGE_STARTED / STAGE_RESULT 발행. 내부 보류 시 가짜 Investigation 이벤트를 만들지 않음.
- 그래프 예외는 FAILED. COMPLETED/FAILED 발행 성공 뒤 ACK. 영구 실패는 reject(requeue=false).
- event_id 재사용과 중복 처리, 늦은 이벤트 차단, 5분 기한, 실제 Postgres 체크포인트 연결.
- 최종 검증과 실제 배정 사이의 동시 변경은 Spring이 다시 검사.
- LLM1 sufficient=True 직접 경로 및 정비팀 수락/거절·사람 검토는 별도 모듈.

## MCP 계약

business: get_request_context / get_aircraft_history / search_departments
rag: search_jasc_codes / search_sdr_cases / search_amm_references / search_technical_docs / get_evidence

서버가 제공한 실제 inputSchema로 입력을 검사한다. tool_inputs.json은 합성 데모용 입력 계약 보존본이다.
필수 도구가 빠지면 MCPContractError로 중단한다. R04만 팀 서버 미구현 시 선택 기능으로 허용한다.
쓰기 도구는 노출하지 않는다. 도구 호출 10초, 일시적 실패 재시도 1회, 노드별 호출 한도 8회가 기본값이다.

현재 SDK 조합은 mcp 1.30.0 / langchain-mcp-adapters 0.2.2이며 lock에 고정됐다.
어댑터의 streamable_http_client 명칭 변경 경고가 남는다.

상세 계약: INTERFACE.md / 검증 기록: VALIDATION.md.
