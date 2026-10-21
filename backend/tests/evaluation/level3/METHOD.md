# Level 3 — L3-05 Tool 검색 평균 응답시간

승인 성능지표 **L3-05**: Tool 검색 평균 응답시간 **3초 이내**.

이 문서는 내부 KPI-01~06(`targets.yaml` / `kpi_measure`)과 **분리된** Level 3 승인 평가 방법이다.

## 측정 구간

`app.search.tool_retrieval.ToolRetrievalService.retrieve()` 호출 직전 ~ 반환까지.

포함:

- Active Embedding Profile 기반 Query Embedding
- 권한 기반 Hybrid Search (Hard Filter → lexical/vector → RRF)

제외:

- 자연어 분석 / StructuredRequest
- LLM Rerank / ToolSelector
- MCP Tool 실행, Workflow/Execution 생성

시간 해상도: `time.perf_counter_ns()` → `elapsed_ms`.

## 데이터셋

| 파일 | 역할 |
|---|---|
| `targets.v1.yaml` | L3-01~L3-06 목표 (이번 버전은 L3-05만 측정 구현) |
| `dataset.v1.yaml` | 고정 질의, warm-up/공식 반복 횟수 |

- `dataset_version` / `targets_version`으로 버전 고정
- 런타임에 질의·결과를 생성하지 않음
- 운영 평가 전, 고정 질의가 **등록·인가된 Tool**에 대응하는지 환경에서 확인

## 인터페이스

```bash
# Local
export PYTHONPATH=backend
python -m tests.evaluation.level3.measure_l3_05 --preflight \
  --user-id <ACTIVE_USER_UUID> \
  --agent-version-id <AGENT_VERSION_UUID>

python -m tests.evaluation.level3.measure_l3_05 --run --write-artifact \
  --user-id <ACTIVE_USER_UUID> \
  --agent-version-id <AGENT_VERSION_UUID>
```

서버용 compose 명령은 도구 출력의 `compose_commands`를 따른다  
(`.env.server` + `compose.yaml` + `compose.server.yaml`, backend read-only mount, `artifacts/level3` uid 10001).

## 판정

1. 분모 = `query_count × official_repeats_per_query` (사전 고정)
2. Warm-up 샘플은 별도 기록, **공식 평균에서 제외**
3. 필수 공식 호출 중 하나라도 실패 → `INCOMPLETE`  
   (성공 호출만 골라 평균 산출 금지)
4. 전부 성공 시 `average_seconds = mean(elapsed_ms) / 1000`
5. `average_seconds ≤ 3.0` → `MET`, 초과 → `NOT_MET`
6. 실패/지연 샘플을 삭제하거나 재측정으로 덮어쓰지 않음
7. 증적 파일 충돌 시 덮어쓰기 금지 (`artifacts/level3/…`)

## 증적 필드 (요약)

- 지표명 / 승인 목표 / 측정 정의
- dataset·targets 버전, 반복 횟수
- warm-up / official 개별 측정값
- average_seconds, judgment
- git_sha, measured_at (UTC), environment
- Embedding Profile 식별정보 (secret/token 제외)

## 운영 사전조건

- ACTIVE User + 명시적 AgentVersion (권한 우회 금지)
- Active Embedding Profile for tools
- 평가 질의가 인가된 Tool 검색에 대응
- `artifacts/level3` 쓰기 가능 (컨테이너 uid 10001)
- `MCPFLOW_GIT_SHA` 또는 git checkout으로 SHA 해석 가능
