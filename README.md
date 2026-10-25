# MRO AI 서버: LLM1 · LLM2 · LLM3

정비 요청을 받아 **증상 분류(LLM1) → 근거 조사(LLM2) → 검토(LLM3)** 를 거쳐 담당 정비 부서를 제안하는 AI 서버입니다.
FastAPI가 서버 수명과 상태 확인을, LangGraph가 단계 흐름을, LangChain이 모델과 MCP 호출을 맡습니다.
backend와는 RabbitMQ로 주고받고, 근거는 agent 레포의 MCP 서버 두 개로만 조회합니다. 실제 배정은 backend가 합니다.

> 모델 검색 범위 확장과 서비스 간 적용 순서는 [모델 검색 범위 계약](docs/MODEL_SCOPE.md)을 따릅니다.

## 전체 구성에서의 위치

```
[backend] ──mro.agent.run (+x-mro-business-token, x-mro-rag-token)──▶ [이 레포: AI 서버 :8100]
    ▲                                                                     │  Authorization: Bearer
    └────────────── mro.agent.events (단계·최종 이벤트) ◀──────────────────┤──▶ [business-mcp :8101] B01~B03
                                                                          └──▶ [rag-mcp      :8102] R01~R05, SDR→AMM 조회
```

| 레포 | 역할 |
| --- | --- |
| [dashboard](https://github.com/mro-agent-proj/dashboard) | 신고자·정비팀·관리자 화면 |
| [backend](https://github.com/mro-agent-proj/backend) | 요청 상태·배정, 실행 요청 발행, MCP 토큰 발급, 최종 근거 재검증 |
| [LLM](https://github.com/mro-agent-proj/LLM) | 이 레포 |
| [agent](https://github.com/mro-agent-proj/agent) | MCP 서버 두 개 (읽기 전용) |
| [integration](https://github.com/mro-agent-proj/integration) | 서비스 4개의 검증된 커밋 조합, 로컬 실행·평가 스크립트 (봇이 자동 갱신) |
| [infra](https://github.com/mro-agent-proj/infra) | AWS 인프라(Terraform)와 운영 배포. 배포는 여기서만 합니다 |
| [deliverables](https://github.com/mro-agent-proj/deliverables) | 최종 결과보고서, 아키텍처 다이어그램 등 산출물 |

## 변경과 배포

1. 이 레포에 PR을 올립니다. main은 PR과 CI 통과가 필수입니다.
2. main에 병합되면 Release 워크플로가 이 커밋의 이미지를 ECR에(태그 = 커밋 SHA) 올리고 [integration](https://github.com/mro-agent-proj/integration)에 알립니다.
3. integration이 네 서비스 main을 모아 통합 검증을 통과하면 `verified-*` 태그를 붙입니다.
4. [infra](https://github.com/mro-agent-proj/infra)에 release PR이 올라오고, 병합·승인하면 운영에 배포됩니다.

## 흐름

```text
실행 요청 수신 (RabbitMQ, prefetch=1)
  → LLM1 분류 (business-mcp B01·B02·B03)
      1단계: 증상 문장만 보고 ATA 후보 1~3개 선택 (이력을 보기 전)
      2단계: 후보 ATA의 같은 기체 이력을 맥락으로 비교. 현재·과거 원문 인용과 담당 ATA를 코드가 검증
      ├─ 근거 확인됨 → 이력·접수·부서 재조회 후 바로 배정 제안
      └─ 근거 없음/미확인 → LLM2로
  → LLM2 조사 (AI가 RAG 도구를 직접 골라 호출) → LLM3 검토 → 필요하면 재조사(최대 2회)
  → 배정이 확정되면 근거로 쓴 SDR에 인용된 AMM 번호를 조회해 결과에 `reference_amm`으로 첨부 (근거 아님)
  → 단계·최종 이벤트를 체크포인트에 저장 → 브로커 확인 → ACK
  → backend가 이벤트를 반영하고 근거·부서를 다시 검증해 배정 또는 사람 검토
```

- `app/agent/full_workflow.py`: 전체 그래프. `app/service.py`: 실행·복구·이벤트 전달. `app/server/rabbit.py`: 큐 소비.
- LLM1 판단 기준: [docs/classification-criteria.md](docs/classification-criteria.md). 같은 ATA나 한 단어만 같은 이력은 연결하지 않습니다.

### 안전장치

| 위치 | 장치 |
| --- | --- |
| LLM1 | 현재·과거 증상의 대상·현상·부정·상충을 모델이 비교. 코드는 각각의 원문 인용, 연결 ATA, 현재 부서 담당 범위를 확인. 불충분하면 LLM2로 |
| LLM2 | LangGraph 전용 입력으로 원본 신고·자체 RAG 근거만 독립 조사. LLM1 판단·수리이력·조회 기록은 전달하지 않고 B02 호출과 HISTORY 인용도 차단 |
| LLM3 | LLM1·2 판단과 원문을 대조. 미해결 충돌·담당 ATA 불일치는 재조사, 근거로 해소한 차이는 통과 가능 |
| LLM2 | 근거와 부서 후보가 있는데 멈추려 하면 한 번 되돌려 보냄. 도구 예산이 끝나도 근거가 있으면 결론 작성 |
| 도구 예산 | 고정 확인 호출(B01·B03)은 제외하고, AI가 고른 호출만 단계당 `MAX_TOOL_CALLS`회 |
| 모델 입력 | LLM2는 자체 RAG 자료만 사용. LLM3의 이력 입력은 인용분·추가 조회분과 최근 이력 일부로 제한. 도구 기록은 상태·범위·ID로 요약 |
| 출력 형식 | 모든 LLM 출력을 pydantic으로 검증. 실패하면 오류 내용을 알려주고 1회 다시 받음. 비어 있는 `NextStep.question`은 행동별 기본 문구로 채움(빈 값 두 번으로 실행이 실패하던 간헐 오류의 원인) |
| 접수 확인 (B01) | LLM1 시작과 최종 확정에 호출하며, 격리된 LLM2도 자체 컨텍스트를 처음 구성할 때 호출. 단계 합류 시 범위·버전·스냅샷 비교 |
| 실패 로그 | 실행 실패 시 서버 로그에 예외 종류와 코드 위치, 스키마 검증 실패 필드·종류만 남김(모델 응답·키·URL은 남기지 않음) |
| 근거·범위 | 인용 근거가 실제로 조회됐는지, 발췌가 원문에 있는지, 부서 코드가 유효한지, 검색 범위·스냅샷이 바뀌지 않았는지 확인 |
| AMM | 판단 근거로 쓰지 않음. LLM2·LLM3 도구 목록에서 AMM 도구(R03, SDR→AMM 조회)를 빼고, AMM 인용은 근거에서 제외. 배정 후 실행기가 근거 SDR의 AMM 번호를 조회해 `reference_amm`(SDR 근거 ID, AMM 번호, 인용 문장)으로 붙임. 조회가 실패하거나 도구가 없으면 첨부 없이 배정은 그대로 진행 |

맥락 비교의 내부 필드, 독립 조사 경계, 검증 한계는 [판단 분리 설계](docs/contextual-handoff.md)를 참고합니다.

## 실행

### Docker (backend·MCP와 함께)

backend(`AI_MODE=rabbit`)와 agent 레포의 MCP 두 개를 먼저 띄웁니다.

```bash
cp .env.example .env     # LLM_API_KEY, AI_MODE=rabbit, AI_CONTAINER_RABBITMQ_URL 채우기
docker compose up -d --build
curl http://127.0.0.1:8100/health/ready   # {"status":"ready",...}
```

Compose는 MCP 주소를 `http://business-mcp:8101/mcp`, `http://rag-mcp:8102/mcp`로 지정하고 backend 네트워크(`BACKEND_NETWORK`, 기본 `backend_default`)에 붙습니다. LLM 호출 키는 AI 서버에 둡니다. RAG 질의 임베딩 키 `RAG_OPENAI_API_KEY`는 별도로 MCP 환경에 설정합니다. `ai-state` 볼륨(체크포인트)을 `down -v`로 지우지 않습니다.

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

## 버전 교체

신규 접수를 멈추고 진행 중 실행·이벤트 처리를 완료한 후 AI 이미지를 재빌드합니다. workflow key는 `llm123-isolated-v2` / `llm23-isolated-v2`이며 이전 key의 체크포인트는 자동 재개하지 않습니다. 기존 DB·큐·AI 상태 볼륨은 보존합니다. 미완료 구버전 요청은 담당자가 기존 처리 결과를 확인한 뒤 복구 절차를 결정해야 합니다.

LLM2에는 LLM3 자유문장 피드백을 그대로 전달하지 않습니다. 재조사는 자신의 RAG 자료와 고정된 독립 재검토 지시를 사용하며, 구체적 피드백 계약은 후속 과제입니다.

## RabbitMQ와 복구

- backend가 선언한 exchange `mro.agent`, queue `mro.agent.run`을 확인만 하고 새로 선언하지 않습니다.
- 최종 이벤트 발행이 브로커에서 확인된 뒤 ACK합니다. 영구 입력·인증 오류는 `reject(requeue=false)`(backend가 사람 검토로 처리).
- LangGraph 체크포인트로 중단된 단계부터 재개하고, 완료된 요청은 모델을 다시 부르지 않고 저장된 이벤트를 재전송합니다. 이벤트 ID가 같으므로 backend가 중복을 거릅니다.
- 단일 replica·단일 Uvicorn worker로 배포합니다(로컬 상태 경로에 프로세스 잠금).
- 실패 시 이벤트에는 `AI_EXECUTION_FAILED`·`RUN_TIMEOUT` 같은 코드만 남기고 모델 응답·URL·키는 남기지 않습니다.

## 테스트

```bash
uv run --extra openai pytest        # 합성 도구·모델로 실행 (OpenAI·MCP 불필요)
uv run --extra openai ruff check app tests
```

## 검증 상태 (2026-10-23)

200건의 [실행 결과·감사 기록·체크섬](https://github.com/mro-agent-proj/deliverables/tree/main/evaluations/2026-10-23-isolated-200)을 공개했습니다.
[오프라인 재집계·체크포인트 감사 도구](scripts/evaluation/README.md)로 저장된 결과를 확인할 수 있습니다.
2026-10-26 자료 정리는 재실행이 아니며, 아래 수치는 10월 23일 실행 기준입니다.

상태 격리 적용 버전으로 Backend → RabbitMQ → AI(gpt-5.4-mini) → HTTP MCP → Docker DB의 서비스 평가 200건을 완료했습니다. 단위·회귀 테스트는 173건 통과했습니다. 실제 실행 감사에서는 LLM2 HISTORY 기록·B02 호출이 모두 0건이었습니다.

팀 라벨 기준 전체 통과 97/200, LLM2 마지막 부서 일치 71/82입니다. 정답 부서를 예측하고 보류한 42건은 별도 검토 대상이며 성공에 합산하지 않습니다. 미검토 라벨 161건이 있어 실제 운영 안전성이나 개선 전후 성능으로 일반화하지 않습니다.

현재 평가 대상은 Backend `data/eval/service/scenarios.jsonl`의 200건이며, `data/seed/maintenance_history_v2.csv` 1,600건과 함께 사용합니다. 접수 검증 `data/eval/scenario/validation.jsonl` 6건은 별도입니다. 파일 기준과 입력 범위는 [분류 기준의 데이터 안내](docs/classification-criteria.md#현재-목적과-기준-데이터)를 따릅니다.

이전 평가셋의 성적표와 기대 분기는 [과거 기록](docs/archive/legacy-scenarios-2026-10-21.md)으로 분리했습니다. 현재 평가·성능 집계에 사용하지 않습니다.

## 알려진 한계와 다음 과제

- **LLM1 직접 배정의 근거 연결**: 이번 평가에서 실패 자동배정 49건이 모두 직접 경로에서 발생했습니다. 부서는 맞지만 금지 이력을 인용한 36건, 부서 불일치 11건, 사람 검토 기대인데 배정한 2건을 구분해 개선해야 합니다.
- **LLM3 보류 사유**: 정답 부서 예측 후 보류한 42건에는 기종 차이 검토 미해소 21건, 기술자료 적용성 확인 11건, ID·인용 검증 실패 10건이 포함됩니다. 실질적인 근거 충돌과 형식 오류를 분리하고, 보류의 적절성을 추가 검토해야 합니다.
- **평가 한계**: 단일 실행이며 라벨은 미검토 161건·최종 검토 필요 24건·결정 완료 15건입니다. 변경 효과는 고정된 조건의 비교 실행과 별도 미사용 자료로 검증해야 합니다.
- LLM2·LLM3 노드·프롬프트는 팀원 브랜치(`from-scratch-llm23-service`)에서 통합한 코드이며, 이후 안전장치를 추가했습니다.
