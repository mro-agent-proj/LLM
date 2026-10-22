# MRO AI 서버: LLM1 · LLM2 · LLM3

정비 요청을 받아 **증상 분류(LLM1) → 근거 조사(LLM2) → 검토(LLM3)** 를 거쳐 담당 정비 부서를 제안하는 AI 서버입니다.
FastAPI가 서버 수명과 상태 확인을, LangGraph가 단계 흐름을, LangChain이 모델과 MCP 호출을 맡습니다.
backend와는 RabbitMQ로 주고받고, 근거는 agent 레포의 MCP 서버 두 개로만 조회합니다. 실제 배정은 backend가 합니다.

## 전체 구성에서의 위치

```
[backend] ──mro.agent.run (+x-mro-business-token, x-mro-rag-token)──▶ [이 레포: AI 서버 :8100]
    ▲                                                                     │  Authorization: Bearer
    └────────────── mro.agent.events (단계·최종 이벤트) ◀──────────────────┤──▶ [business-mcp :8101] B01~B03
                                                                          └──▶ [rag-mcp      :8102] R01~R03·R05
```

| 레포 | 역할 |
| --- | --- |
| [dashboard](https://github.com/mro-agent-proj/dashboard) | 신고자·정비팀·관리자 화면 |
| [backend](https://github.com/mro-agent-proj/backend) | 요청 상태·배정, 실행 요청 발행, MCP 토큰 발급, 최종 근거 재검증 |
| [LLM](https://github.com/mro-agent-proj/LLM) | 이 레포 |
| [agent](https://github.com/mro-agent-proj/agent) | MCP 서버 두 개 (읽기 전용) |

## 흐름

```text
실행 요청 수신 (RabbitMQ, prefetch=1)
  → LLM1 분류 (business-mcp B01·B02·B03)
      1단계: 증상 문장만 보고 ATA 후보 1~3개 선택 (이력을 보기 전)
      2단계: 그 ATA의 같은 기체 이력만 비교. 연결 이력마다 "증상과 이력 문장에 둘 다 있는 대상 표현"을 적게 하고 코드가 확인
      ├─ 근거 확인됨 → 이력·접수·부서 재조회 후 바로 배정 제안
      └─ 근거 없음/미확인 → LLM2로
  → LLM2 조사 (AI가 RAG 도구를 직접 골라 호출) → LLM3 검토 → 필요하면 재조사(최대 2회)
  → 단계·최종 이벤트를 체크포인트에 저장 → 브로커 확인 → ACK
  → backend가 이벤트를 반영하고 근거·부서를 다시 검증해 배정 또는 사람 검토
```

- `app/agent/full_workflow.py`: 전체 그래프. `app/service.py`: 실행·복구·이벤트 전달. `app/server/rabbit.py`: 큐 소비.
- LLM1 판단 기준: [docs/classification-criteria.md](docs/classification-criteria.md). 같은 ATA나 한 단어만 같은 이력은 연결하지 않습니다.

### 안전장치

| 위치 | 장치 |
| --- | --- |
| LLM1 | 연결 이력의 ATA = 분류한 ATA, 공통 대상 표현이 두 문장에 실제로 있는지 코드로 확인. 확인 안 되면 사람 검토가 아니라 LLM2로 |
| LLM2·LLM3 | 같은 도구를 같은 인자로 반복 호출하지 않음. LLM1이 연결할 이력이 없다고 본 경우 이력 도구를 제공하지 않음 |
| LLM2 | 근거와 부서 후보가 있는데 멈추려 하면 한 번 되돌려 보냄. 도구 예산이 끝나도 근거가 있으면 결론 작성 |
| 도구 예산 | 고정 확인 호출(B01·B03)은 제외하고, AI가 고른 호출만 단계당 `MAX_TOOL_CALLS`회 |
| 모델 입력 | 근거 원문과 핵심 필드만 전달. 이력은 인용분 + 최근 20건, 도구 응답은 본문 없이 상태·범위·ID만 (128K 한도 초과 방지) |
| 출력 형식 | 모든 LLM 출력을 pydantic으로 검증. 실패하면 오류 내용을 알려주고 1회 다시 받음 |
| 근거·범위 | 인용 근거가 실제로 조회됐는지, 발췌가 원문에 있는지, 부서 코드가 유효한지, 검색 범위·스냅샷이 바뀌지 않았는지 확인 |

## 실행

### Docker (backend·MCP와 함께)

backend(`AI_MODE=rabbit`)와 agent 레포의 MCP 두 개를 먼저 띄웁니다.

```bash
cp .env.example .env     # LLM_API_KEY, AI_MODE=rabbit, AI_CONTAINER_RABBITMQ_URL 채우기
docker compose up -d --build
curl http://127.0.0.1:8100/health/ready   # {"status":"ready",...}
```

Compose는 MCP 주소를 `http://business-mcp:8101/mcp`, `http://rag-mcp:8102/mcp`로 지정하고 backend 네트워크(`BACKEND_NETWORK`, 기본 `backend_default`)에 붙습니다. 모델 키는 AI 서버에만 둡니다. `ai-state` 볼륨(체크포인트)을 `down -v`로 지우지 않습니다.

### 로컬

```bash
uv sync --locked --extra openai
cp .env.example .env
uv run --extra openai mro-worker run      # FastAPI :8100
```

기본 `AI_MODE=disabled`는 상태 확인만 제공하고 큐를 소비하지 않습니다.

## 설정 (.env)

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `LLM_PROVIDER` / `LLM_MODEL` | `openai` / `gpt-5.4-mini` | 허용 모델: `gpt-4o-mini`, `gpt-4.1-mini`, `gpt-5.4-mini` (`app/main.py`) |
| `LLM_API_KEY` | (비움) | OpenAI 키. 비우면 `OPENAI_API_KEY`를 씀. git에 넣지 않음 |
| `AI_MODE` | `disabled` | `rabbit`이면 실행 요청 소비 |
| `MCP_DELEGATION_TRANSPORT` | `amqp_headers` | backend가 붙인 헤더 `x-mro-business-token`·`x-mro-rag-token`을 읽어 각 MCP에 전달. 토큰은 모델·체크포인트·이벤트에 넣지 않음 |
| `RABBITMQ_URL` / `AI_CONTAINER_RABBITMQ_URL` | — | 호스트용 / 컨테이너용(`amqp://mro:mro@rabbitmq:5672/`) |
| `BUSINESS_MCP_URL` / `RAG_MCP_URL` | `http://localhost:8101/mcp` / `:8102/mcp` | 로컬 실행용 (Docker는 compose가 덮어씀) |
| `TOOL_TIMEOUT_SECONDS` / `TOOL_RETRIES` | `10` / `1` | MCP 호출 제한 시간·재시도 |
| `MODEL_TIMEOUT_SECONDS` | `45` | 모델 호출 제한 시간 |
| `MAX_TOOL_CALLS` / `MAX_REINVESTIGATION` | `8` / `2` | 단계당 AI가 고른 도구 호출 수, 재조사 횟수 |
| `RUN_TIMEOUT_SECONDS` | `280` | 그래프 실행 한도 (backend 큐 TTL 5분 안) |
| `STATE_DIR` / `RUN_JOURNAL_PATH` / `CHECKPOINT_DATABASE_URL` | `.state` / … / (비움) | 체크포인트 위치. 비우면 로컬 SQLite |

## RabbitMQ와 복구

- backend가 선언한 exchange `mro.agent`, queue `mro.agent.run`을 확인만 하고 새로 선언하지 않습니다.
- 최종 이벤트 발행이 브로커에서 확인된 뒤 ACK합니다. 영구 입력·인증 오류는 `reject(requeue=false)`(backend가 사람 검토로 처리).
- LangGraph 체크포인트로 중단된 단계부터 재개하고, 완료된 요청은 모델을 다시 부르지 않고 저장된 이벤트를 재전송합니다. 이벤트 ID가 같으므로 backend가 중복을 거릅니다.
- 단일 replica·단일 Uvicorn worker로 배포합니다(로컬 상태 경로에 프로세스 잠금).
- 실패 시 이벤트에는 `AI_EXECUTION_FAILED`·`RUN_TIMEOUT` 같은 코드만 남기고 모델 응답·URL·키는 남기지 않습니다.

## 테스트

```bash
uv run --extra openai pytest        # 130개, 합성 도구·모델로 실행 (OpenAI·MCP 불필요)
uv run --extra openai ruff check app tests
```

## 검증 상태 (2026-10-21, 실제 스택)

backend·MCP·OpenAI를 모두 붙여 backend `data/eval/scenario/scenarios.md`의 S01~S23을 실제로 돌렸습니다(`gpt-5.4-mini`).

| 구분 | 결과 |
| --- | --- |
| 연결 | MCP 인증·호출 정상(401·오류 0), 이벤트·배정 반영 정상. 실행 실패는 23건 중 1건 |
| 바로 배정 (S01~S06, S23) | 7건 중 5건 정답 (S05·S06은 조사 경로로 가서 다른 부서) |
| 이력 없는 신규 증상 (S07~S15) | 9건 중 3건 정답 (S09·S10·S13). 나머지는 다른 부서 배정 또는 사람 검토 |
| 같은 ATA 함정 (S21·S22) | 2건 모두 함정 이력을 연결하지 않고 정답 |
| 모호한 증상 (S16~S20, 기대: 사람 검토) | 5건 모두 후보 부서 중 하나로 배정됨 |

전체 정답 10/23입니다. 같은 시나리오도 실행마다 판단이 달라질 수 있습니다. 목업 시나리오 결과이며 실제 항공사 데이터의 분류 정확도를 뜻하지 않습니다.

## 알려진 한계와 다음 과제

- **모호한 증상의 사람 검토 전환**: LLM2 안전장치("근거가 있으면 결론 작성") 이후 후보가 여럿인 증상도 한 부서로 배정합니다. LLM3가 부서 후보 충돌을 사람 검토로 넘기는 기준 보강이 필요합니다.
- **신규 증상 정확도**: LLM2가 고르는 부서가 기대와 다른 경우가 많습니다. 증상 → 계통(ATA) → 부서 대응을 프롬프트·검증에 더 반영할 여지가 있습니다.
- **판단 편차**: 같은 입력도 실행마다 경로가 달라집니다. 평가는 여러 번 돌려 비율로 봐야 합니다.
- **실패 원인 로그**: 실패 이벤트에 코드만 남아, 원인 확인에 재현이 필요합니다. 서버 로그에 예외 종류를 남기는 보강이 필요합니다.
- LLM2·LLM3 노드·프롬프트는 팀원 브랜치(`from-scratch-llm23-service`)에서 통합한 코드이며, 이후 안전장치를 추가했습니다.
