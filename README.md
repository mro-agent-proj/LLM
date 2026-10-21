# MRO AI 서버: LLM1·2·3

FastAPI가 서버 시작·종료와 상태 확인을 맡고, Spring과는 RabbitMQ로 통신합니다. LangChain으로 모델과 MCP를 호출하고 LangGraph로 LLM1→LLM2→LLM3 및 재조사 흐름을 실행합니다. LLM1은 사용자 지정 OpenAI `gpt-4o-mini`와 확정한 [분류 기준](docs/classification-criteria.md)을 사용합니다.

## 실행

```sh
uv sync --locked --extra openai
cp .env.example .env  # 새 환경에서만 실행. 기존 .env는 보존하세요.
uv run --extra openai uvicorn app.main:app --host 127.0.0.1 --port 8100 --workers 1
```

`.env`의 `LLM_API_KEY`에 키를 설정합니다. 기본 `AI_MODE=disabled`에서는 HTTP 상태 확인만 가능합니다. `/health/live`는 프로세스 상태, `/health/ready`는 큐 소비 준비 상태를 반환합니다. 실행 HTTP API는 제공하지 않습니다.

## 실행 흐름

```text
Spring → RabbitMQ mro.agent.run → FastAPI lifespan의 소비자
  → LLM1: business-mcp B01/B02/B03
      충분 → B01/B02/B03 재검증 → COMPLETED(ASSIGN)
      부족 → 기존 LLM2 조사 → LLM3 검토 → 최종 근거 재검증
      오류·검증 실패 → 사람 검토 또는 FAILED
  → RabbitMQ mro.agent.events → Spring이 실제 상태 변경·배정
```

LLM1은 동일 기체 이력의 모든 페이지를 제한된 도구 호출 예산 안에서 읽습니다. 페이지를 다 읽지 못하면 이력 없음으로 판단하지 않습니다. 위치·발생 조건의 완전 일치, 최근 30일, 최소 2건을 필수 조건으로 추가하지 않았습니다. `ClassificationDecision`은 ATA 등을 생성할 수 없는 경우를 표현하는 내부 래퍼이며 외부 `Classification` 5개 필드는 그대로입니다.

기존 LLM2·3와 공통 모듈은 [fd80da3](https://github.com/mro-agent-proj/LLM/tree/fd80da3f29869a833b38c21803bf2de0e743dda3)에서 가져와 수정하지 않았습니다. 해시는 `docs/upstream/baseline.json`, 이전 설명은 `docs/upstream/`에 보존했습니다. 기존 `workflow.build_graph`는 LLM2·3 부분 그래프이며 전체 실행은 `full_workflow.build_full_graph`입니다.

## 실제 연결 전에 필요한 합의

- Backend의 요청별 위임 토큰 발급·실행 컨텍스트 생성·전달 방식과 B01 읽기 권한이 남아 있습니다. request_id만으로 권한을 대신하지 않습니다.
- `MCP_DELEGATION_TRANSPORT=amqp_headers` 어댑터는 구현 제안입니다. 팀 합의가 아니며 기본값은 `unconfigured`입니다. Backend가 AMQP 헤더 `x-mro-business-token`, `x-mro-rag-token`을 요청별로 제공하고 MCP가 검증할 수 있을 때만 선택합니다. 토큰은 모델·이벤트·journal에 넣지 않습니다.
- 합의 후 `AI_MODE=rabbit`, `RABBITMQ_URL`, 모델 키·MCP URL을 설정합니다. 토큰 전달 설정이 없으면 큐 소비를 시작하지 않고 readiness 503을 반환합니다. readiness 200은 큐 연결 상태이며 실제 MCP·모델 품질 검증 결과가 아닙니다.
- 컨텍스트가 변경되면 새 위임이 필요합니다. 전체 그래프는 기존 토큰으로 자동 재조사하지 않고 사람 검토로 종료합니다. 토큰 갱신 계약은 후속 연결 사항입니다.
- B03은 `code` 또는 부서 코드인 `department_id`를 제공해야 합니다. B01의 resolved_scope와 RAG의 applied_scope에는 동일한 locked_filters가 필요합니다. UUID 부서 ID만 반환하면 연결할 수 없습니다.
- SDR 운항사 매핑과 Backend의 `VALIDATION_FAILED` 사유 보존도 기존 미해결 항목입니다.

## RabbitMQ와 복구

Spring이 선언한 exchange `mro.agent`, queue `mro.agent.run`, routing key `agent.event`를 사용합니다. AI는 수동적 존재 확인만 하며 큐·익스체인지를 새로 생성하지 않습니다. prefetch=1, 발행 확인 후 ACK, 복구 불가 시 reject(requeue=false)를 적용합니다.

전체 처리 예산은 최대 295초, 그래프는 최대 280초로 실패 이벤트 발행 시간을 남깁니다. 발행 확인 재시도는 같은 이벤트 ID·본문을 사용합니다. 로컬 `RUN_JOURNAL_PATH`의 SQLite에는 토큰 없이 이벤트를 발행 전에 저장하고 완료된 요청 재전달 시 같은 이벤트를 재발행합니다. 중간에 중단된 그래프는 접수·권한을 다시 조회해 시작합니다.

이 journal은 LangGraph의 영구 체크포인트가 아닙니다. 현재는 단일 worker와 지속되는 로컬 볼륨을 전제로 하며 다중 인스턴스, 동일 request_id의 별도 새 실행, 운영용 체크포인트는 실행 ID·저장소 계약을 추가해야 합니다. 공유 업무 DB에는 접근하거나 쓰지 않습니다. Backend의 접수 후 5분 제한과 늦은 이벤트 차단은 계속 적용됩니다.

## 검증 상태

추가 구현 테스트 36건과 Ruff 검사를 통과한 뒤 사용자의 빠른 공유 요청에 따라 추가 테스트를 중단했습니다. 이후 이벤트 발행 실패 처리, SQLite 연결 종료, RabbitMQ 종료 처리의 작은 보완은 재실행하지 않았습니다. 기존 LLM2·3 기준 버전은 이전 검토에서 65건을 통과했습니다.

실제 OpenAI 호출, 실제 팀 MCP·RabbitMQ·DB 종단 간 검증, S01~S23 모델 분류 정확도 평가는 수행하지 않았습니다. 합성 응답 테스트를 모델 정확도로 해석하지 않습니다.

## 참고 문서

- [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/)
- [LangGraph의 그래프 연결](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)
- [RabbitMQ 발행 확인](https://docs.aio-pika.com/rabbitmq-tutorial/7-publisher-confirms.html)
- [팀 LLM2·3 인터페이스](docs/upstream/INTERFACE.md)
