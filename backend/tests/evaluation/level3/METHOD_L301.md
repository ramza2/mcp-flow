# Level 3 — L3-01 MCP 서버 연동 성공률

승인 성능지표 **L3-01**: MCP 서버 연동 성공률 **≥ 90%**.

이 문서는 내부 KPI harness와 **분리된** Level 3 승인 평가 방법이다.  
연결 시도 증적은 기존 `mcp_server_checks` 행만 SELECT로 재사용하며,  
**실제 Connection Test / 외부 HTTP / Discovery 쓰기를 수행하지 않는다**.

## 측정 정의

분모(시험 전 사전 고정):

```text
required_attempts = servers × attempts_per_server
```

공식 `--run`은 평가 구간에 존재하는 **해당 서버의 모든** `mcp_server_checks`를 수집한다.

서버별 규칙:

- 수집 건수 == `attempts_per_server` → `checked_at` ASC, `id` ASC로 attempt 매핑
- 수집 건수 < 기대 → `CHECK_COUNT_MISSING` → `INCOMPLETE`
- 수집 건수 > 기대 → `CHECK_COUNT_EXCESS` → `INCOMPLETE`
- 비단말 status / 모호한 증적 → `INCOMPLETE`

분자:

```text
status == SUCCEEDED 인 매칭 시도 수
```

성공률:

```text
success_rate_percent = succeeded / required_attempts × 100
```

`FAILED` / `TIMED_OUT`은 분모에 남긴다. 성공만 골라 재계산하지 않는다.

## 평가계획

| 파일 | 역할 |
|---|---|
| `targets.v1.yaml` | L3-01 목표 (≥ 90%) |
| `plan_l301.example.v1.yaml` | **예시** 5서버×2회 (공식 측정 전 교체) |

시험 전 plan에 고정:

1. `evaluation_window.start` / `end` (UTC)
2. `attempts_per_server`
3. 대상 `server_id` 목록

`check_id` 사전 pin은 **금지**한다.  
`example: true` plan은 `--run`을 거부한다.  
증적에 plan 파일 **SHA256**을 기록한다.

## Preflight

`--preflight`는 연결검사 전에 가능하다.

- plan 구조 / SHA256 / 분모 / 구간 검증
- example plan은 DB 없이 통과 가능
- 실plan은 서버 존재 확인 (check 행 존재는 요구하지 않음)
- check 수집·성공률 판정은 `--run`에서만 수행

## 인터페이스

```bash
export PYTHONPATH=backend
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

서버 compose 명령은 도구 출력 `compose_commands`를 따른다:

- `--workdir /opt/kpi-src`
- `backend:/opt/kpi-src:ro`
- `artifacts/level3:/opt/kpi-artifacts/level3` 직접 마운트
- 기존 api 이미지 유지
- `artifacts/` 전체 `chown` 금지

## 판정

1. 필수 시도 수 = plan 고정 분모
2. 서버별 window 수집 건수가 정확히 일치하고 상태가 단말일 때만 성공률 산출
3. `success_rate_percent ≥ 90` → `MET`, 미만 → `NOT_MET`
4. 누락·초과·모호·오류 → `INCOMPLETE`
5. 증적 파일 덮어쓰기 금지

## 증적 필드 (요약)

- 지표명 / 목표 / 측정 정의 / plan·targets 버전
- `plan_sha256`
- evaluation_window, required_attempts
- 서버별 수집 check 목록 및 attempt 매핑 (check_id는 수집 결과)
- success_rate_percent, judgment
- `as_of`, `read_snapshot` (PostgreSQL REPEATABLE READ READ ONLY)
- git_sha, measured_at (UTC), environment

## 운영 사전조건

- 시험 전 plan 고정 → 이후 연결검사 수행 → `--run`으로 window 수집
- `DATABASE_URL` / `TEST_DATABASE_URL` / `MCPFLOW_DATABASE_URL` (실plan/run)
- `artifacts/level3` 쓰기 가능
- `MCPFLOW_GIT_SHA` 또는 git checkout으로 SHA 해석 가능

운영 측정·배포·연결검사·DB 데이터 변경은 이 도구 구현 PR 범위가 아니다.
