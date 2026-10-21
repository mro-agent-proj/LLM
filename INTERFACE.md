# LLM2·3 인터페이스 적용 기록 — 2026-10-21

## 기준과 사용자 선택

1. 최신 팀 인터페이스 PDF 우선. 이전 Word의 근거 관리 의미를 충돌하지 않는 범위에서 유지.
2. Spring만 실제 업무 상태·배정을 변경. 두 MCP는 읽기 전용.
3. 사용자 선택: 외부 출력 형식 유지 + 내부 보류 처리.
4. LLM1 변경 시 입력 연결을 조정한다. 현재 GraphState 원래 필드는 삭제·이름 변경하지 않는다.

## 외부 출력

| 모델 | 필드 |
|---|---|
| Investigation | ata_chapter, finding, severity, evidence, proposed_department, rationale |
| Review | decision, department, comments, supplement_request |
| Evidence | source, ref_id, excerpt, why |

- source = HISTORY / SDR / AMM. TECHNICAL_DOC 및 JASC 도구는 아직 추가하지 않았다.
- PASS이면 department, SUPPLEMENT이면 supplement_request가 필수다.
- evidence는 최소 1건이며 excerpt는 원문 연속 발췌 300자 이내다.
- severity 값은 PDF의 LOW / MEDIUM / HIGH / AOG를 유지한다. 미상을 LOW로 변환하지 않는다.
- 정상 조사안의 필수값을 뒷받침할 수 없으면 조사안 생성을 보류한다.
- 부서 코드가 존재한다는 검증과 실제 업무에 맞는다는 판단은 구분한다.

## AI 내부 확장

GraphState에 선택 필드 `agent_context` 하나를 추가했다. Spring의 DTO·STAGE_RESULT 출력에는 포함하지 않는다.
추가 필드는 LLM1이 생성할 필요가 없으며, 없으면 LLM2에서 생성한다.

```text
agent_context
  request_id          다른 접수의 근거 혼입 방지
  records[]           도구 실행기가 기록한 출처·ID·원문·메타데이터
  departments[]       최근 조회한 활성 부서 목록
  traces[]            확인할 질문·도구·실제 인자·OK/EMPTY/ERROR·시도 횟수
  hold                내부 보류 사유 또는 null
```

hold.reason은 INSUFFICIENT_EVIDENCE / INSUFFICIENT_INFORMATION / DEPARTMENT_UNAVAILABLE /
TOOL_LIMIT / VALIDATION_FAILED / RETRY_LIMIT이다.

- 정상 LLM2: investigations에 새 Investigation을 추가한다.
- 근거 부족 LLM2: investigations를 바꾸지 않고 hold와 조회 기록만 반환한다.
- hold를 받은 LLM3: 허구의 조사안을 요구하지 않고 유효한 Review(HUMAN)를 반환한다.
- 최종 콜백에서는 RETRY_LIMIT·VALIDATION_FAILED를 PDF의 동일 사유로,
  나머지 보류는 AGENT_NEEDS_HUMAN으로 표현한다. 상세 사유는 summary와 내부 기록에 남는다.

이 확장은 공유 GraphState에 반영해야 하지만 외부 JSON 모델은 바꾸지 않는다.
체크포인트에도 포함해야 재시작 후 근거와 미확인 사항을 복원할 수 있다.

## 도구 계약

| 도구 | 필요한 입력 | 결과의 최소 의미 |
|---|---|---|
| get_aircraft | tail_number | 모델·항공사·MSN |
| get_maintenance_history | tail_number, ata_chapter?, since?, limit? | 이력 ID, 기번, 증상, 조치, 시각, 부서 |
| search_similar_history | query, aircraft_model, ata_chapter?, top_k? | 다른 기체일 수 있는 참고 이력 |
| list_departments | 없음 | code, 이름, 담당 범위, 가능하면 active |
| search_sdr | query, aircraft_model?, ata_chapter?, top_k? | SDR 원문 발췌와 모델·항공사·원본 위치 |
| search_amm | query, aircraft_model, ata_chapter?, top_k? | AMM 참조번호와 원본 SDR 인용 맥락 |
| get_document | id | 이미 조회된 SDR/AMM 자료의 본문·출처 |

결과는 {"items": [...]}이다. 빈 목록과 MCP 오류는 구분한다.
MCP structuredContent 및 LangChain ToolMessage.artifact.structured_content도 읽는다.
모든 인용 자료에는 문자열 id와 content가 있어야 한다. CSV 이력은 symptom_text/action_taken/outcome을
원문 필드 그대로 묶어 읽을 수 있다. closed_at만으로 성공 결과를 합성하지 않는다.

권장 metadata: aircraft_model, airline_code, tail_number(이력), source_case_id, evidence_group_id,
material_type, applicability_status, source_version, snapshot_id, mock.
모르는 값은 누락/null로 유지하며 일치나 적용성 확인으로 간주하지 않는다.
search_amm은 실제 본문 확보 전까지 derived_amm_reference / not_verified로 취급한다.
같은 SDR와 그 파생 AMM은 독립 근거 2개가 아니다.

## 제어·검증

- 재조사는 기본 2회, LLM2 최초 조사 포함 최대 3회.
- 각 노드 조회 한도는 환경변수 MAX_TOOL_CALLS 기본 8회. 부서 사전 조회 1회를 포함한 기술적 시작값이다.
- 인자 JSON 오류·없는 도구 선택도 한도에 포함한다. 예상하지 못한 프로그램 예외는 실행기까지 전파한다.
- 목록은 기본적으로 교체하므로 각 노드는 기존 investigations/reviews를 포함한 전체 새 목록을 반환한다.
  팀 그래프가 누적 reducer를 사용한다면 이 부분을 함께 변경해야 한다. 두 방식을 중복 적용하면 안 된다.
- 최종 검증은 모든 고유 인용 근거를 재조회하며 상한은 20건이다. 그 이상은 VALIDATION_FAILED로 인계한다.
- HISTORY는 원래 조회한 이력 도구·인자로 다시 조회한다. get_document로 이력을 검증하지 않는다.
- 근거 ID·인용·부서의 기계적 검증은 의미적 타당성이나 실제 고장 진단 성능을 입증하지 않는다.

## 실행기가 연결할 항목

- LLM1 sufficient=False에서 이 부분 그래프로 진입. sufficient=True 직접 경로는 별도 소유.
- STAGE_STARTED와 정상 STAGE_RESULT는 단계·시도 횟수와 함께 전송.
- 내부 보류 시 가짜 Investigation STAGE_RESULT를 보내지 않는다. 이후 Review(HUMAN)는 정상 기록 가능.
- completion_payload 후 COMPLETED 전송. 예외는 FAILED. 상태 변경·배정은 Spring이 수행.
- 내부 조회 기록은 checkpoint에, 사용자용 판단 기록은 Spring agent_assessments에 저장.
- 최종 검증 단계의 도구 호출 기록을 영구 보존하려면 실행기의 감사 로그 연결이 추가로 필요하다.
- 검증 통과 후에도 Spring이 진행 상태·부서·권한을 재확인한다. 늦은 콜백이 사람 인계를 되돌리면 안 된다.
