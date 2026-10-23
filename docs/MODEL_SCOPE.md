# 단계별 SDR 모델 검색 범위 (2026-10-23)

사용자가 승인한 검색 범위 확장 계약이다. 원본 팀 PDF v0.3·Google MCP v1.0 자체가 개정됐다는 뜻은 아니다. 기존 위임 인증·운항사 정책·컨텍스트 버전·고정 릴리스/색인·고정 JASC/날짜 조건은 유지한다.

| 단계 | 동작 |
| --- | --- |
| LLM1 / B02 | 동일 기체 수리이력 확인과 직접 배정 경로 유지 |
| LLM2 첫 R02 | Backend가 정한 기본 MODEL_GENERATION: 같은 기본 모델·ceo/neo 세대. 예: A320-232 → A320-214/-212 등 |
| R02 추가 조사 | 기본 범위 조사 후 근거 부족·충돌 사유를 기록하고 FAMILY를 명시적으로 선택. A318/A319/A320/A321 계열의 허용 목록으로 확대 |
| R02 EXACT | 요청과 같은 상세 model_key만 검색 |
| R05 / SDR의 AMM 참고번호 | 실행이 허용한 최대 목록 안에서 원문 재검증·참고번호 조회 |
| LLM3 | 확장 SDR마다 대상·현상·발생 조건과 구성 차이가 부서 선택에 미치는 영향을 명시적으로 검토. 누락·미해결이면 보완/기존 재조사 한도 후 사람 검토 |
| Backend 배정 | 근거 ID의 실제 모델·릴리스·운항사·고정 조건·활성/유효기간을 다시 확인 |

`search_mode="exact"`는 영어 구절 일치 방식이다. `scope_level="EXACT"`(모델 범위)와 독립적이다. 키워드와 벡터 모두 동일한 모델 목록으로 필터링하며 기존 임베딩·점수 결합 비율은 유지한다. 정확 모델 사례를 우선 비교하도록 AI에 안내하되 점수보다 모델 일치를 무조건 앞세우는 강제 정렬은 하지 않는다.

## Backend 소유 정책

Flyway V11은 `public.rag_request_contexts.model_scope`(nullable JSONB)를 추가하고 조회 전용 `agent_run_context` 뷰에 노출한다. 새 실행은 context_version=2로 발행하고 이전 버전 컨텍스트를 비활성화한다. 기존 NULL 정책 요청은 EXACT로 처리한다. 공유 DB DDL·복원은 Backend만 수행한다.

```json
{"policy_version":"airbus-model-scope-v1","default_level":"MODEL_GENERATION","levels":{"EXACT":["A320232"],"MODEL_GENERATION":["A320232","A320214"],"FAMILY":["A320232","A320214","A321211"]}}
```

위 JSON은 형태를 설명하는 축약 예시다. 실제 허용 목록의 단일 기준은 Backend `ModelSearchScope.java`다. 명시된 상세 모델만 그룹화하며 알 수 없는/계열 수준 표기와 목록 밖 모델은 EXACT만 제공한다. 이 목록은 검색용 프로젝트 정책이고 항공기 인증·엔진/부품 호환성 검증 목록이 아니다. 모델 목록·정책을 변경할 때 컨텍스트 버전도 갱신해야 한다. MCP/AI에서 모델 접두어로 권한을 추정하지 않는다.

B01 `resolved_scope`와 RAG `applied_scope`는 `model_scope`, `search_level`을 제공한다. `model_key`는 요청 모델을 그대로 나타낸다. R02 `scope_level`은 선택 입력이며 생략/null이면 Backend 기본값이다. FAMILY 선택은 LLM2 실행기가 앞선 기본/EXACT R02 성공(OK/NO_MATCH/NO_DATA)을 확인한 뒤 허용한다. MCP는 실행 순서를 저장하지 않으며 허용 목록 경계만 검사한다.

R02/R05 및 SDR-AMM 참고번호 결과에는 실제 `model_key`, `requested_model_key`, `model_relation`(EXACT/MODEL_GENERATION/FAMILY), `configuration_match="UNVERIFIED"`가 포함된다. 계열 일치만으로 구성 일치를 주장하지 않는다. AI는 R05 응답의 모델 메타데이터가 조회 당시와 같은지도 확인한다. 확장 근거를 사용한 최종 summary에는 실제 모델·요청 모델·구성 미확인 안내를 덧붙인다. Evidence.ref_id 형식과 RabbitMQ 이벤트 계약은 바꾸지 않는다.

R03의 독립 AMM 번호 검색은 EXACT를 유지한다. 배정 후 SDR의 참고번호 전달은 선택한 SDR을 따라 확장된다. AMM은 판단 근거나 실제 정비 절차로 승격하지 않는다. R04 기술자료의 기존 계열/적용성 처리는 유지한다.

## 배포와 검증 경계

Backend V11, MCP, AI를 함께 준비한 뒤 새 작업을 수락한다. 구버전 MCP/AI와 새 정책을 혼합해 운영하지 않는다. 진행 중 작업을 마친 뒤 세 서비스를 교체하는 통합 배포를 권장한다. V11 자체는 기존 데이터/임베딩을 수정하지 않는다. Backend가 먼저 마이그레이션하고 MCP/AI가 시작하는 순서는 기존과 같다.

검증은 격리 PostgreSQL/pgvector, 조회 전용 계정, 실제 MCP 구현과 HTTP 인증, 합성 모델의 LangGraph 경로를 사용한다. 이 변경의 실제 LLM 분류 정확도·검색 품질 향상은 별도 평가가 필요하다. 전체 서비스 200건의 실제 모델 평가는 이 PR에 포함하지 않는다.
