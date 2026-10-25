# 저장된 실구동 평가의 재집계와 감사

2026-10-23 서비스 평가 200건의 정본은
[deliverables 평가 번들](https://github.com/mro-agent-proj/deliverables/tree/main/evaluations/2026-10-23-isolated-200)입니다.
실행 결과와 체크포인트 감사 기록을 보관하며, 보고서와는 별개입니다.
이 폴더의 도구는 모델·MCP·Backend API를 호출하지 않습니다.

## 결과 재집계 (Python 표준 라이브러리만 사용)

세 저장소를 같은 상위 폴더에 받은 경우, LLM 루트에서:

```bash
python3 scripts/evaluation/summarize_full_eval.py \
  --bundle ../deliverables/evaluations/2026-10-23-isolated-200 \
  --dataset ../backend/data/eval/service/scenarios.jsonl --check
```

`--dataset`은 선택 사항입니다. 지정하면 실행 당시 SHA256·라벨·금지 이력 ID를 대조합니다.
데이터셋이 변경됐다면 평가 당시 버전을 받아야 하며, 현재 파일을 자동으로 동일시하지 않습니다.
케이스 ID 중복, 저장된 채점·집계 불일치, 감사 대상 불일치, LLM2 HISTORY/B02 기록이 있으면 실패합니다.
`--out 새파일.json`은 재집계 결과를 저장하며 기존 파일은 덮어쓰지 않습니다.

자동배정 통과에는 부서 일치와 금지 이력 미인용이 모두 필요합니다.
LLM2 부서 예측 일치와 최종 배정 성공은 별도 지표이며, 예측이 맞은 보류 42건을 성공에 더하지 않습니다.

## SQLite 체크포인트에서 메타데이터 추출

실행 당시 체크포인트에 접근할 수 있을 때만 사용합니다. 공개 번들에는 SQLite 파일이 없습니다.

```bash
uv run --extra openai python scripts/evaluation/audit_live_isolation.py \
  --checkpoint-db /path/to/checkpoints.sqlite \
  --results ../deliverables/evaluations/2026-10-23-isolated-200/full-eval-results.json \
  --out /tmp/checkpoint-audit-new.json
```

원본은 SQLite `mode=ro`로 열고 커밋된 WAL까지 임시 DB로 백업합니다.
LangGraph saver의 초기화·인덱스 생성은 그 임시 복사본에서만 일어납니다.
결과에 지정한 요청만 선택하며, 누락이나 workflow가 중복된 요청은 실패합니다.
현재 구현은 SQLite 전용입니다. 모델명은 결과 파일에서 옮긴 메타데이터로, 이 도구가 제공자에게 확인하는 값은 아닙니다.

감사는 부모 상태를 `InvestigatorState` 필드로 투영한 입력 경계와 저장된 `llm2_context`를 검사합니다.
실제 전송 프롬프트를 별도로 캡처한 증거는 아닙니다. LangGraph 입력 격리 회귀 테스트와 함께 해석해야 합니다.
노드별 도구 수는 체크포인트 trace 증가분이며 HTTP 재시도까지 포함한 서버 요청 수가 아닙니다.
`DUPLICATE_CALL`은 내부 차단 기록이므로 MCP 호출로 세지 않습니다.

## 검증

```bash
uv run --extra openai pytest -q
uv run --extra openai ruff check app tests scripts/evaluation
```

CI의 합성 단위·회귀 테스트와 위 오프라인 재집계는 실모델 200건 재실행을 대신하지 않습니다.
새 실구동 평가는 Backend 평가 도구와 별도 실행 환경을 사용합니다.
