# MRO AI 서버: LLM1·2·3

FastAPI가 서버 시작·종료와 상태 확인을 맡고, Spring과는 RabbitMQ로 통신합니다.
LangChain으로 모델과 MCP를 호출하고 LangGraph로 LLM1→LLM2→LLM3 및 재조사 흐름을 실행합니다.
모델은 OpenAI `gpt-4o-mini`이며 LLM1은 확정한 [분류 기준](docs/classification-criteria.md)을 따릅니다.

## 실행

```sh
uv sync --locked --extra openai
cp .env.example .env  # 새 환경에서만 실행. 기존 .env 보존
uv run --extra openai mro-worker run
```

`mro-worker run`은 FastAPI를 포트 8100에서 시작합니다.
직접 `uv run --extra openai uvicorn app.main:app --host 127.0.0.1 --port 8100 --workers 1`로 실행해도 같은 서버입니다.
두 명령을 동시에 실행하지 않습니다. 기본 `AI_MODE=disabled`는 HTTP 상태 확인만 제공합니다.
`/health/live`는 프로세스, `/health/ready`는 소비자 연결 상태이며 실제 모델·MCP 품질 보장은 아닙니다.
실행 요청은 RabbitMQ로 받습니다.

## 통합한 코드와 흐름

팀원 [from-scratch-llm23-service, ecd915a](https://github.com/mro-agent-proj/LLM/tree/ecd915af2b5360bd37c015a7ef10dd9317b3c200)의
LLM2·3 노드·프롬프트, 그래프 체크포인트·이벤트 복구, Docker 배포를 통합했습니다.
독립 LLM2·3 소비자는 FastAPI의 단일 소비자로 정리했습니다.

```text
Spring → mro.agent.run → FastAPI의 단일 RabbitMQ 소비자
  → LLM1: B01/B02/B03
      충분 → 동일 이력·접수·부서 재검증
      부족 → LLM2 조사 → LLM3 검토 → 필요 시 최대 2회 재조사 → 최종 근거 재검증
      보류·검증 실패 → 사람 검토
  → 체크포인트에 단계/최종 이벤트 저장 → 브로커 확인 → ACK
  → mro.agent.events → Spring이 업무 상태·실제 배정 처리
```

`app/agent/full_workflow.py`가 전체 그래프입니다. `workflow.build_graph`는 LLM2·3 부분 그래프로 유지합니다.
`app/service.py`가 전체 그래프의 복원·이벤트 전달을 담당하고 `app/server/rabbit.py`만 큐를 소비합니다.
`ContractModel`과 `MCPContractError`는 새 이름에 대한 호환 별칭을 제공해 LLM1·서버 호출부를 보존합니다.
합성 데모·평가는 팀 브랜치대로 `tests/support.py`, `tests/scenarios.py`로 이동했습니다.
`docs/upstream/baseline.json`은 최초 도입 버전의 역사적 해시 기록이며 현재 코드 불변을 뜻하지 않습니다.

LLM1의 분류 기준과 외부 Classification/Investigation/Review/Evidence 필드는 유지합니다.
연결 근거가 충분하면 직접 경로를 허용하며, 과거 조치 성공·최근 30일·최소 2건·세부 조건 완전 일치를
일률적인 필수 조건으로 추가하지 않습니다. 도구 오류나 불완전한 페이지 조회를 이력 없음으로 해석하지 않습니다.
AMM은 SDR의 인용 맥락이며 실제 정비 절차가 아닙니다.

## MCP 인증·실행 컨텍스트

Backend의 위임 토큰·실행 컨텍스트, business 요청 인증·조회 권한, B01/RAG 범위 일치가 아직 연결 조건입니다.
request_id만으로 권한을 대신하지 않으며 서비스 항공사를 SDR 운영사로 임의 매핑하지 않습니다.

- `MCP_DELEGATION_TRANSPORT=amqp_headers`: 요청별 `x-mro-business-token`, `x-mro-rag-token`을 읽습니다.
  팀 브랜치의 `business_token`, `rag_token`도 호환합니다. 두 이름에 서로 다른 값이 있으면 거부합니다.
  두 토큰 모두 필수이며 토큰을 모델·체크포인트·이벤트·journal에 넣지 않습니다.
- `MCP_DELEGATION_TRANSPORT=broker`: 팀 브랜치의 선택적 위임 엔드포인트 제안입니다.
  `MCP_DELEGATION_URL`과 `MCP_DELEGATION_KEY`를 명시해야 하며 요청별 두 토큰을 반환해야 합니다.
  아직 Backend에 구현·합의된 서비스로 간주하지 않습니다.
- 기본 `unconfigured`에서는 소비를 시작하지 않습니다. 합의 후 `AI_MODE=rabbit`, `RABBITMQ_URL`,
  `LLM_API_KEY`와 도구 URL을 설정합니다. 모델은 `LLM_PROVIDER=openai`, `LLM_MODEL=gpt-4o-mini`입니다.
- 새 전달에서도 컨텍스트·검색 스냅샷이 달라지면 기존 판단을 계속 쓰지 않습니다.
  현재 전체 그래프는 사람 검토로 종료하며 새 위임을 자동 생성하지 않습니다.

## 체크포인트·이벤트 복구

기본은 `STATE_DIR=.state`의 SQLite LangGraph 체크포인트이며 이벤트 journal도 같은 지속 디렉터리에 둡니다.
중단한 단계부터 재개하고, 완료된 요청은 모델을 다시 호출하지 않고 저장 이벤트를 재전송합니다.
이벤트 ID와 본문을 유지하므로 Spring은 event_id로 중복 제거해야 합니다.
전체 그래프의 체크포인트 thread ID에는 `llm123-v1:` 접두사를 붙여 이전 LLM2·3 전용 그래프의
중간 상태를 잘못 이어받지 않습니다. 배포는 단일 replica·단일 Uvicorn worker이며 로컬 상태 경로에 프로세스 잠금을 둡니다.
다른 볼륨의 여러 인스턴스나 동일 request_id의 새 실행은 별도 실행 ID·저장소 계약이 필요합니다.
기존 journal이 있다면 `RUN_JOURNAL_PATH`로 원래 파일을 유지합니다.

Postgres 체크포인트는 `CHECKPOINT_DATABASE_URL`을 명시할 때만 사용합니다.
Backend 담당자가 별도 AI 계정과 `langgraph` 스키마·권한을 준비해야 합니다.
가능하면 별도 AI DB를 사용하며 공유 DB에 임의 DDL·계정 보완을 실행하지 않습니다.
준비된 전용 저장소 초기화 명령은 `uv run --extra openai mro-worker init-checkpoints`입니다.
실제 업무·RAG 테이블은 MCP 조회 계정으로 도구를 통해서만 조회합니다.

Spring 소유 exchange `mro.agent`·queue `mro.agent.run`을 수동 확인하며 새로 선언하지 않습니다.
prefetch=1, 확인된 최종 이벤트 후 ACK, 영구 입력/인증 오류는 reject(requeue=false)입니다.
발행 연결 실패는 미확인 메시지를 유지하고 재연결합니다. 불확실한 COMPLETED 뒤 FAILED를 발행하지 않습니다.
최초 수신 시각·메시지 timestamp를 반영한 총 예산은 최대 295초이며 그래프는 최대 280초입니다.
이미 진행된 시간은 재전달로 초기화하지 않습니다. MCP 호출 10초·재시도 최대 1회를 유지합니다.

## Docker

Backend와 agent의 두 MCP를 먼저 준비한 후 `.env`를 채웁니다.
`BACKEND_NETWORK`는 agent와 같은 네트워크여야 하고, `AI_CONTAINER_RABBITMQ_URL`은 그 네트워크에서
접근 가능한 실제 RabbitMQ 서비스 주소로 지정합니다. 호스트용 `localhost` URL을 그대로 쓰지 않습니다.
Compose는 MCP 주소를 `http://business-mcp:8101/mcp`, `http://rag-mcp:8102/mcp`로 지정합니다.

```sh
docker compose up -d --build ai
```

호스트 포트는 `127.0.0.1:8100`이며 모델 키는 AI에만 설정합니다.
`ai-state` 볼륨을 유지하고 `docker compose down -v`로 초기화하지 않습니다.
Docker도 같은 `mro-worker run`을 실행하므로 별도 LLM2·3 소비자를 추가하지 않습니다.

## 검증 상태

이번 서비스 통합은 사용자 요청에 따라 테스트를 실행하지 않았습니다.
Python 문법·import 참조·Ruff·의존성 lock·Compose 설정을 정적으로 확인합니다.
이전 버전의 합성 테스트 결과를 이번 통합의 통과 결과로 간주하지 않습니다.
실제 OpenAI·팀 MCP·RabbitMQ·DB 종단 간 실행과 분류·조사 품질은 미검증입니다.
공유 DB 변경·덤프 복원도 수행하지 않았습니다.
