# Level 3 — L3-01 MCP 서버 연동 성공률

승인 성능지표 **L3-01**: MCP 서버 연동 성공률 **≥ 90%**.

이 문서는 내부 KPI harness와 **분리된** Level 3 승인 평가 방법이다.  
연결 시도 증적은 기존 `mcp_server_checks` 행만 SELECT로 재사용하며,  
**실제 Connection Test / 외부 HTTP / Discovery 쓰기를 수행하지 않는다**.

## 측정 정의

분모(사전 고정):

```text
required_attempts = servers × attempts_per_server
```

분자:

```text
status == SUCCEEDED 인 매칭된 check 수
```

성공률:

```text
success_rate_percent = succeeded / required_attempts × 100
```

포함:

- `mcp_server_checks`의 `SUCCEEDED` / `FAILED` / `TIMED_OUT`
- 평가 구간에 포함되고 plan에 pin된 `check_id`

제외 / 금지:

- 실시간 연결 검사, Discovery, ACTIVE 전환
- 실패·타임아웃을 분모에서 제거
- 성공 건만 골라 재계산
- KPI harness 결과 JSON 재사용

## 평가계획

| 파일 | 역할 |
|---|---|
| `targets.v1.yaml` | L3-01 목표 (≥ 90%) |
| `plan_l301.example.v1.yaml` | **예시** 5서버×2회 plan (공식 측정 전 교체) |

Plan에 사전 고정:

1. `evaluation_window.start` / `end` (UTC)
2. `attempts_per_server`
3. 대상 `server_id` 목록
4. 시도별 정확한 `check_id`

`example: true` plan은 `--run` 공식 측정을 거부한다.  
운영자는 실제 서버/체크 ID와 구간으로 plan을 복사·수정한 뒤 `example: false`로 둔다.

## check_id 매칭

각 plan attempt는 정확히 하나의 `mcp_server_checks.id`와 매칭되어야 한다.

Fail-closed → `INCOMPLETE`:

- check 행 누락
- `check.mcp_server_id` ≠ plan `server_id`
- `checked_at`이 evaluation window 밖
- 동일 `check_id` 중복 pin (모호)
- status가 `SUCCEEDED`/`FAILED`/`TIMED_OUT` 이외
- 분모 개수와 매칭 결과 수 불일치

## 인터페이스

```bash
export PYTHONPATH=backend
# Plan path (default: plan_l301.example.v1.yaml — example only)
python -m tests.evaluation.level3.measure_l3_01 --preflight \
  --plan path/to/plan_l301.v1.yaml

python -m tests.evaluation.level3.measure_l3_01 --run --write-artifact \
  --plan path/to/plan_l301.v1.yaml
```

또는:

```bash
./scripts/level3-mcp-connection-success.sh --preflight --plan ...
./scripts/level3-mcp-connection-success.sh --run --write-artifact --plan ...
```

## 판정

1. 필수 시도 수 = plan 고정 분모
2. 모든 시도가 유효 check_id에 매칭되고 상태가 단말일 때만 성공률 산출
3. `success_rate_percent ≥ 90` → `MET`, 미만 → `NOT_MET`
4. 누락·모호·오류 → `INCOMPLETE` (성공만 골라 평균/비율 금지)
5. 증적 파일 덮어쓰기 금지

## 증적 필드 (요약)

- 지표명 / 목표 / 측정 정의 / plan·targets 버전
- evaluation_window, required_attempts, 시도별 server_id·check_id·status
- success_rate_percent, judgment
- `as_of`, `read_snapshot` (PostgreSQL REPEATABLE READ READ ONLY)
- git_sha, measured_at (UTC), environment

## 운영 사전조건

- Plan의 모든 check_id가 이미 `mcp_server_checks`에 존재
- `DATABASE_URL` / `TEST_DATABASE_URL` / `MCPFLOW_DATABASE_URL`
- `artifacts/level3` 쓰기 가능
- `MCPFLOW_GIT_SHA` 또는 git checkout으로 SHA 해석 가능

운영 측정·배포·연결검사·DB 데이터 변경은 이 도구 구현 PR 범위가 아니다.
