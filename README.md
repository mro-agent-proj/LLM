# MRO LLM2·3 Service

항공기 정비 요청을 조사하고 검토하는 독립 실행형 LLM2·3 서비스입니다.

- LLM2는 Business MCP와 RAG MCP의 읽기 도구를 선택해 정비 근거를 수집하고 조사 결과를 만듭니다.
- LLM3는 조사 결과와 원문 근거를 다시 확인해 `PASS`, `SUPPLEMENT`, `HUMAN` 중 하나를 결정합니다.
- `SUPPLEMENT`이면 LLM2가 보완 조사를 수행하며, 한도를 넘거나 근거가 부족하면 사람 검토로 전환합니다.
- 완료 직전 근거와 담당 부서를 재조회해 오래되거나 존재하지 않는 인용을 차단합니다.
- 실행 상태와 발행할 이벤트를 체크포인트에 저장해 프로세스 또는 브로커 재시작 후 이어서 처리합니다.

인터페이스 문서는 서비스 경계의 입력·출력 형식을 결정하는 연결 기준으로만 사용했습니다. 이 저장소의 구현 대상은 LLM2 조사, LLM3 검토, MCP 조회, 실행 복원, RabbitMQ 입출력입니다. 실제 정비 배정과 업무 데이터 변경은 이 서비스가 수행하지 않습니다.

## 처리 흐름

```text
RabbitMQ request
  -> 요청 검증 및 요청별 MCP 자격 증명 구성
  -> LLM2 조사 + 근거 검증
  -> LLM3 검토
       -> SUPPLEMENT: LLM2 보완 조사
       -> HUMAN: 사람 검토 결과 생성
       -> PASS: 최종 근거·부서 재검증
  -> 단계 이벤트 및 최종 결과 발행
  -> RabbitMQ ACK
```

최종 이벤트가 브로커에서 확인된 뒤에만 입력 메시지를 ACK합니다. 발행이 중단되면 같은 `event_id`를 체크포인트에서 복원해 재전송하므로 수신 측은 `event_id`로 중복을 제거할 수 있습니다.

## 주요 디렉터리

```text
app/
  agent/             LLM2·3 그래프, 프롬프트, MCP 도구, 결과 검증
  checkpoints.py     SQLite/Postgres 체크포인트
  credentials.py     요청별 MCP 자격 증명
  messages.py        입력 메시지와 단계 이벤트 모델
  service.py         요청 실행, 복원, 제한 시간, 이벤트 발행
  worker.py          RabbitMQ 소비자와 CLI
tests/               합성 모델·MCP를 사용하는 단위 및 서비스 테스트
examples/request.json
compose.yaml
Dockerfile
```

## 로컬 설치와 검증

Python 3.12와 `uv`가 필요합니다.

```bash
uv sync --locked
uv run pytest -q
uv run ruff check app tests
uv run ruff format --check app tests
```

테스트는 합성 모델과 합성 MCP 응답을 사용하므로 API 키가 필요하지 않습니다. 실제 LLM의 판단 품질 평가는 포함하지 않습니다.

## 환경 설정

```bash
cp .env.example .env
```

필수 연결 항목은 다음과 같습니다.

```dotenv
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=

BUSINESS_MCP_URL=http://localhost:8101/mcp
RAG_MCP_URL=http://localhost:8102/mcp
RABBITMQ_URL=amqp://mro:mro@localhost:5672/
RUN_QUEUE=mro.agent.run
EVENT_EXCHANGE=mro.agent
EVENT_ROUTING_KEY=agent.event
```

API 키와 위임 토큰은 Git에 커밋하지 않습니다. RAG/Business MCP 토큰은 입력 JSON이 아니라 AMQP 헤더 또는 백엔드가 운영하는 위임 엔드포인트를 통해 요청별로 전달합니다.

## 실행

체크포인트 저장소를 초기화합니다. `CHECKPOINT_DATABASE_URL`이 비어 있으면 `.state/checkpoints.sqlite`를 사용합니다.

```bash
uv run mro-worker init-checkpoints
```

RabbitMQ worker를 시작합니다.

```bash
uv run mro-worker run
```

RabbitMQ 없이 한 요청을 직접 실행할 수도 있습니다. 이 명령은 실제 LLM과 MCP 연결을 사용하고 단계 이벤트를 표준 출력으로 내보냅니다.

```bash
uv run mro-worker run-file examples/request.json
```

Docker로 실행할 때는 `.env`를 작성한 뒤 사용합니다.

```bash
docker compose up --build llm23
```

`compose.yaml`은 RabbitMQ와 MCP 서버를 새로 만들지 않습니다. 팀이 운영하는 기존 서비스에 연결합니다.

## RabbitMQ 메시지

입력은 `application/json`이며 `message_id`가 있으면 `request_id`와 같아야 합니다.

```json
{
  "request_id": "97d9e320-7554-41a8-866e-26a169fd057c",
  "tail_number": "HL1234",
  "aircraft_model": "A320-214",
  "airline_code": "KAL",
  "symptom_text": "착륙 후 우측 메인 휠에서 비정상 진동이 보고됨",
  "classification": null
}
```

서비스는 `STAGE_STARTED`, `STAGE_RESULT`, `COMPLETED`, `FAILED` 이벤트를 발행합니다. 비정상 입력과 같은 영구 오류는 재큐잉하지 않으며, 이벤트 발행 연결이 끊기면 입력을 ACK하지 않고 재연결 후 체크포인트에서 복원합니다.

## MCP 도구

서비스가 사용하는 읽기 도구는 다음과 같습니다.

Business MCP:

- `get_request_context`
- `get_aircraft_history`
- `search_departments`

RAG MCP:

- `search_jasc_codes`
- `search_sdr_cases`
- `search_amm_references`
- `search_technical_docs` (선택)
- `get_evidence`

쓰기 도구는 모델에 노출하지 않습니다. MCP 빈 검색 결과와 서버 오류를 구분하며, 인용 ID·원문 발췌·자료 위치·데이터 버전·부서 유효성을 최종 결과 전에 확인합니다.

## 체크포인트 운영

독립 실행 시 SQLite를 사용할 수 있습니다. 팀 배포에서는 AI 서비스 전용 Postgres 역할과 `langgraph` 스키마를 준비하고 다음 값을 설정합니다.

```dotenv
CHECKPOINT_DATABASE_URL=postgresql://mro_ai:password@postgres:5432/mro
```

초기 스키마 생성은 배포 단계에서 한 번만 수행합니다.

```bash
uv run mro-worker init-checkpoints
```

서비스는 업무 테이블을 읽거나 변경하지 않고 LangGraph 체크포인트만 저장합니다.

## 현재 검증 범위

자동 테스트는 다음을 확인합니다.

- LLM2 조사, LLM3 검토, 보완 조사 및 사람 검토 분기
- MCP 입력 스키마, 읽기 도구 제한, 재시도와 호출 한도
- 인용 원문과 부서의 최종 재검증
- 단계 이벤트 순서와 최종 결과
- 완료 요청 재전달 시 모델을 다시 호출하지 않는 복원
- 브로커 발행 실패 시 미확인 메시지 유지
- 변경된 입력으로 같은 요청 ID를 재사용하지 못하도록 차단
- 오류 메시지에서 모델 응답·자격 증명·URL 비노출

실제 팀 MCP 데이터에 대한 검색 품질과 실제 LLM의 정비 판단 정확도는 팀 통합 환경에서 별도로 평가해야 합니다.
