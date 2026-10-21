# LLM2·3 인터페이스 적용 기록 — 2026-10-21

## 기준과 결정

1. 최신 팀 PDF 본문 v0.3 우선. 도구 상세 목적·입출력은 Google MCP v1.0과 기존 Word를 참고.
2. MCP 모두 읽기 전용. Spring만 실제 상태·배정 변경.
3. 외부 출력 유지 + 내부 보류. R04도 외부 출처를 위장하지 않고 내부 참고·필요 시 사람 검토.
4. 평가 모델은 OpenAI gpt-4o-mini. 주입 가능한 StructuredModel.complete 인터페이스 유지.
5. LLM1 변경 시 연결 조정. 현재 LLM1과 전체 큐 실행기는 구현 범위 밖.

현재 대조한 실제 팀 RAG 소스:
- https://github.com/mro-agent-proj/agent/tree/5cffa2162227fe26bcdf3913ddf8a5ad52bde5a1
- src/mro_rag/service.py, server.py, contracts.py
- docs/INTEGRATION_CONTRACT.md 및 docs/spec-v1/mcp-tool-contracts.json
- 로컬 카탈로그만의 추가 제약을 팀 합의로 간주하지 않는다. 실제 서버가 광고한 inputSchema로 호출 입력을 검사한다.
- B01~B03의 실제 서버는 이번에 접속하지 못했으며 PDF/Word 계약과 합성 응답으로 검증했다.

## 도구별 입출력 연결

| ID | 도구 | LLM이 선택할 입력 | 실행기가 고정할 입력 | 처리 |
|---|---|---|---|---|
| B01 | get_request_context | 없음 | request_id | result: report/aircraft/resolved_scope/버전/상태 |
| B02 | get_aircraft_history | query, occurred_from/to, limit, cursor | request_id, context_version | 동일 기체 이력, MH-*를 외부 ref_id로 보존 |
| B03 | search_departments | query, candidate_jasc, limit | 위와 같음 | 업무 범위·활성·지원 모델에 맞는 code 후보 |
| R01 | search_jasc_codes | query 또는 codes, limit | 위와 같음 | 공식 코드 후보. 다른 문서나 부서의 정답 아님 |
| R02 | search_sdr_cases | query, candidate_jasc, search_mode, limit, cursor | 위와 같음 | SDR/파생 AMM evidence_id와 출처 보존 |
| R03 | search_amm_references | query 또는 amm_reference_exact, 후보/수준/검색모드/페이지 | 위와 같음 | AMM 참조 맥락, 실제 작업 절차 아님 |
| R04 | search_technical_docs | query, system_keywords, include_unknown_applicability, 페이지 | 위와 같음 | 내부 참고. unknown은 적용 근거로 승격하지 않음 |
| R05 | get_evidence | 이미 검색한 evidence_id | 위와 같음 | result의 출처·버전·원문·위치·인용 범위 재확인 |

R01에서 확인한 코드만 candidate_jasc로 사용한다. 사용자 locked_filters는 후보 코드로 바꾸지 않는다.
R05에 임의 URL/SQL/AMM 번호/이력 ID를 넣지 않는다.
부서는 PDF의 departments.code로 반환한다. B03가 별도 UUID만 주면 매핑을 추측하지 않고 통합 오류로 처리한다.

## 상태·검색 범위

- OK / NO_DATA / NO_MATCH / NEEDS_INPUT / ERROR를 보존. items와 result는 해당 도구 계약대로 해석.
- NEEDS_INPUT은 내부 보완 사유와 missing_fields로 남고 HUMAN_REVIEW로 인계.
- ERROR와 빈 자료를 구분. retryable 오류만 제한 재시도하고 retry_after_ms를 허용 기한 안에서 준수.
- request_id, context_version, trace_id, applied_scope, snapshot_ids, counts, warnings, missing_fields, 원래 응답을 조회 기록에 보존.
- B01의 검색 컨텍스트 버전과 request_version은 다른 값이다.
- 서비스 항공사 코드와 공개 SDR operator_code를 직접 비교·자동 매핑하지 않음.
- 정확 모델 키는 B01이 검증한 값을 사용. 확인되지 않은 항공사 매핑으로 검색 범위를 넓히지 않음.
- SDR/AMM은 운항사·모델·고정 JASC/발견일·고정 데이터/색인 범위를 검사.
- R01은 항공사·모델 필터를 적용하지 않지만 동일 접수 권한·버전을 유지.
- 공개 기술자료는 운항사 필터를 적용하지 않고 모델/기종군 적용성 및 구성 조건을 보존.
- 커서는 발급받은 질의·버전에 묶어 사용. 임의 커서나 다른 질의의 커서는 거부.
- 실제 tenant/행 권한 및 위임 JWT의 검증은 MCP/Backend 책임. request_id는 인증을 대신하지 않음.

컨텍스트 버전 변경은 기존 판단을 무효화하고 내부 superseded_assessments에 보존한다.
동일 기체·신고 내용이 유지되는 한 B01부터 1회 재조회하여 LLM2·3를 새 범위로 다시 수행한다.
이 갱신 횟수는 SUPPLEMENT 재조사 2회와 별개인 기술적 한도다.
범위가 버전 증가 없이 바뀌거나 갱신 후 다시 변경되면 자동 인계를 중단한다.
고정 데이터/색인이 바뀌는 경우는 자동으로 최신 버전으로 옮기지 않는다.

## 외부 계약

| 모델 | 필드 |
|---|---|
| Evidence | source, ref_id, excerpt, why |
| Investigation | ata_chapter, finding, severity, evidence, proposed_department, rationale |
| Review | decision, department, comments, supplement_request |

source는 HISTORY/SDR/AMM, severity는 LOW/MEDIUM/HIGH/AOG를 유지한다.
심각도 미상을 LOW로 바꾸지 않으며 필수값 근거 부족은 내부 보류한다.
HISTORY.ref_id는 MH-000123 같은 수리이력 ID다.
SDR/AMM.ref_id는 EV-SDR-*/EV-AMM-* evidence_id다. case_id/source_id/AMM 절 번호로 대체하지 않는다.
인용은 원문 연속 300자 이내. 근거 ID·source·원문 인용을 모두 검사한다.

## 내부 구조와 최종 결정

GraphState의 선택적 agent_context는 Spring 외부 DTO에 보내지 않는다.

- request_context / context_version / applied_scope / snapshots / cursors
- records: 원문 인용과 원래 행의 전체 metadata. 이력 시각·JASC·미결·조치·결과·출처를 손실 없이 유지.
- traces: 질문·도구·실제 인자·원래 응답·상태·시도 횟수·UTC 시작/종료 시각.
- jasc_candidates / evidence_group_id: 코드 정의와 동일 원본 그룹 보존.
- hold: 필수 근거 부족·컨텍스트·권한·버전·지원하지 않는 외부 출처 등의 보류 사유.
- final_evidence / final_validation / prepared_payload / validated_at: 최종 검증 결과.
- context_refresh_count / superseded_assessments: 무효화된 이전 판단과 제한된 재조사 기록.

내부 모델 호출은 InvestigationAssessment(investigation, technical_basis_ids),
ReviewAssessment(review, additional_evidence, technical_basis_ids)로 구조화한다.
Spring STAGE_RESULT에는 각각 investigation와 review만 내보낸다.
이 내부 래퍼는 외부 DTO 확장이 아니다.

LLM3는 검토에서 새로 사용한 근거를 additional_evidence로 선언한다.
최종 근거 목록은 최신 LLM2 근거와 이 추가 근거를 합쳐 만든다.
같은 evidence_group_id의 SDR/파생 AMM을 독립된 자료 수로 제시하지 않도록 그룹을 모델에 전달한다.
그룹과 인용 존재 검증이 의미적 판단의 정확성을 보장하지는 않는다.

최종 finalize 노드는:
1. B01로 접수 상태와 범위를 재확인.
2. B03로 최종 부서의 현재 적격성 재확인.
3. 최종 고유 근거 최대 20건을 전부 재조회. SDR/AMM은 R05, HISTORY는 원래 B02 질의.
4. 원문·출처·버전·위치·인용 범위 검사.
5. 호출 기록과 준비된 payload를 그래프 상태/체크포인트에 반환.

정상 PASS도 이 검증을 통과해야 ASSIGN payload가 된다. 실제 배정은 Spring이 다시 검사한다.
예외는 실행기로 전파하고 실행기가 FAILED 이벤트를 발행한다.
영구 Postgres·큐·5분 기한·실제 배정은 이 부분 그래프의 검증 범위 밖이다.

## 미확정·통합 확인

- 실제 모델 품질 평가 실행 및 팀 키 관리. 이번 버전은 사용자 결정에 따라 실제 호출을 생략.
- Backend 발급 토큰/컨텍스트 전달과 고객 항공사→공개 SDR 운항사 매핑.
- B01/B02 조회 권한과 실제 B03 부서 코드 반환.
- R04 실제 서버 노출 및 R05의 기술자료 원문 조회 지원.
- LLM1 전체 라우팅, RabbitMQ 이벤트/ACK/DLQ, 실제 Postgres 복구.
