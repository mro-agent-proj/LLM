# MCP v0.3 정합성·검증 결과 — 2026-10-21

## 확인한 결과

- Python 3.12 새 가상환경에서 uv sync --locked --offline 성공.
- pytest: 65 passed, failures/errors/skipped 0.
- Ruff check, Ruff format --check, git diff --check 통과.
- localhost 실제 Streamable HTTP MCP로 최신 8개 읽기 도구 발견 및 전체 LLM2·3 부분 그래프 실행.
- 요청별 인증 헤더 전달, 쓰기 도구 제외, 구조화 응답과 MCP isError 재시도 정보 보존 확인.
- 합성 기대 경로 회귀 8/8 일치. scripted 모델 응답 대역 결과이며 실제 LLM 품질 점수가 아니다.
- 실제 OpenAI/다른 모델 API 호출 0회. 사용자 결정에 따라 실제 LLM 평가 생략.

## 반영한 수정

1. 도구 이름 B01~B03, R01~R05 및 items/result·업무 status·evidence_id 처리.
2. 접수/검색 조건 버전 주입, 원문 범위·고정 필터·스냅샷·페이지 커서 검증.
3. 이력 시각·JASC·조치·결과·미결·출처와 경고/범위를 metadata·조회 기록에 보존.
4. 코드표로 확인한 JASC 후보 사용. 후보 코드와 사용자 고정 조건 분리.
5. LLM3 추가 근거를 최종 목록과 원문 재검증에 포함.
6. 최종 검증을 그래프 노드로 넣어 검증 호출·결과를 체크포인트에 보존.
7. 컨텍스트 변경 시 기존 판단 무효화·보관 및 1회 갱신 후 재조사. 반복 변경은 중단.
8. 원문/출처/위치/버전 변경, 부서 비활성, 접수 상태 변경, 재조사 한도 도달 시 자동 인계 중단.
9. R04 내부 참고 유지. 기술자료가 핵심 결론의 근거이면 기존 외부 형식으로 사람 검토.
10. 평가 모델 기본 설정 OpenAI gpt-4o-mini. 환경변수·주입으로 교체 가능.

## 실제로 확인하지 않은 것

- 팀의 실제 business-mcp 및 RAG 서버·공유 데이터에 대한 종단 간 실행.
- Backend 위임 토큰/컨텍스트 발급 연결과 고객 항공사→SDR 운항사 매핑.
- 실제 LLM의 조사·검토·인계 정확도·토큰 비용.
- RabbitMQ 실행기·Spring 상태 변경·실제 배정·Postgres 영구 복구.
- LLM1의 초기 분류 및 충분한 이력의 직접 배정 경로.

로컬 전송 시험은 합성 MCP이며 실제 팀 데이터의 권한·검색 성능 검증을 대신하지 않는다.
InMemorySaver 복구 시험은 프로세스 재시작 후 Postgres 복구 검증을 대신하지 않는다.
어댑터 명칭 변경 DeprecationWarning 12건이 남으며 테스트 실패는 아니다.

기준과 실제 팀 소스 대조 기록은 INTERFACE.md, 실행·후속 평가 안내는 README.md/EVALUATION.md 참조.
