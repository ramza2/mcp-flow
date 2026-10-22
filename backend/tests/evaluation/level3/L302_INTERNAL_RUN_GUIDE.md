# L3-02 내부 자체평가 실행기 및 판정기 안내 — 승인 전

**내부 방법론 승인:** 2026-10-22 KST (`METHOD_L302_INTERNAL_V1.md`).

**정답셋 상태:** `PENDING_REVIEW`; 담당자 1인의 실제 확인 및 별도 정답셋 승인 전에는 **LLM 측정 실행 금지**.

## 구성

| 역할 | 파일/구성 | 현상 |
|---|---|---|
| 기존 2인 독립 평가 v2 | `METHOD_L302_V2.md`, `scoring_l302_v2.py`, `collect_l302_v2.py` | 기존 2인 조건 유지, 별도 미승인 |
| 내부 자체평가 방법 | `METHOD_L302_INTERNAL_V1.md` | 실제 담당자 1인 방식 **승인** |
| 내부 자체평가 정답셋 | `dataset_l302.internal.v1.yaml` | 신규 20개 복사·별도 버전, **미승인** |
| 담당자 정답 확인표 | `L302_INTERNAL_OWNER_GOLD_CHECKLIST.md` | 20건 확인란 **미기재** |
| 내부 전용 관측 수집기 | `collect_l302_internal.py` | 승인 전 LLM/DB 차단 |
| 내부 전용 채점기 | `scoring_l302_internal.py` | 8개 실제 담당자 판정이 있어야 계산 |
| 기존 공식 v1 | `dataset_l302.v1.yaml`, `scoring_l302.py` | 공식 측정 원본 결과·기준 불변 |

**서로 다른 데이터셋·방법을 혼합하지 않는다.** 내부 판정은 `INTERNAL_MET`, `INTERNAL_NOT_MET`으로 표시하며 외부 기관 독립 검증으로 주장하지 않는다.

## 사전점검 (LLM/DB를 호출하지 않음)

PR이 CI를 통과하고 머지된 뒤 아래를 통해 정답셋의 승인 상태만 검사할 수 있다.

```bash
cd ~/mcp-flow

docker compose --env-file .env.server \
  -f compose.yaml -f compose.server.yaml \
  run --rm --no-deps -T \
  -v "$(pwd)/backend:/opt/kpi-src:ro" \
  -e PYTHONPATH=/opt/kpi-src \
  api sh -c 'python -m tests.evaluation.level3.collect_l302_internal --preflight'
```

**지금 예상 결과:** `ok=true`, `run_permitted=false`, `gate_reason=INTERNAL_GOLD_NOT_APPROVED`, `judgment=INCOMPLETE`. `ok=true`는 사전점검 수행 성공일 뿐 성능 평가 성공이 아니다.

**미승인 상태에서 `--run`은 차단된다.** 직접 `review_status`를 수정하거나 가짜 검토자 이력을 채워서 우회해서는 안 된다.

## 실제 담당자 1인 검토 이후 단계

1. 담당자가 `L302_INTERNAL_OWNER_GOLD_CHECKLIST.md` 20건을 실제로 검토한 뒤, 정답에 대한 의견을 확정한다.
2. 정답셋 `owner_review.case_decisions`에 그 사람의 20개 실제 검토 결과와 근거를 반영한다. 과제 담당자 별도 승인·날짜 및 증적을 남긴다.
3. 골드셋을 새로운 승인 PR로 동결하고 CI를 검증한다. 모델 출력은 이 전 단계에서 열어보지 않는다.
4. 승인된 데이터셋을 이용해 `collect_l302_internal.py --run --ack-gold-reviewed --write-artifact --agent-version-id <uuid>`를 **실측 1회** 수행한다. `--write-artifact`가 private/public 증적을 서로 다른 파일로 배타 생성한다.
5. 접근통제된 관측을 실제 담당자 1인이 검토한다. 각 문항당 의도/기능/엔터티/출처/시간/위험/모호성/스키마 8개 bool과 평가 근거·일시를 기록한다.
6. `score_reviewed_dataset`에 같은 승인 `gold_sha256`, 동일 `run_ref`, 정확히 20문항의 판정 기록을 전달해 `INTERNAL_MET` 또는 `INTERNAL_NOT_MET` 결과를 계산한다.

- 성능 목표는 항상 **85% 이상(20건 중 17건 이상)**.
- DB 쓰기나 MCP Tool 실제 실행은 이 지표에 포함하지 않는다.
- 일반 증적에는 인용 원문·모델 응답 전문·Secret을 저장하지 않는다.
- private 저장 경로: `artifacts/level3/l302-internal-private`(0700), 각 파일(0600).
- 평가 결과 공개/제출 가능 범위는 해당 사업의 공식 제출 요건 확인 뒤 결정한다.

**유의:** 본 문서는 시험 실행 **준비**와 절차만 기록한다. 신규 v2/internal 실측 결과나 85% 목표 충족을 증명하지 않는다.
