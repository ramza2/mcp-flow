# Level 3 — L3-06 연계·검증 완료 MCP Tool 수

승인 성능지표 **L3-06**: 연계·검증 완료 MCP Tool 수 **≥ 10**.

이 문서는 내부 KPI-01~06(`targets.yaml` / `kpi_measure`)과 **분리된** Level 3 승인 평가 방법이다.  
도메인 집계 SQL은 `MCPToolVerificationRepository`를 재사용하되, **KPI harness 결과 JSON을 재사용하지 않는다**.

## 측정 정의

`MCPToolVerificationRepository.count_tools_with_effective_current_verification()`:

- `MCPTool.deleted_at IS NULL`
- `current_version_id`가 존재
- 해당 current ToolVersion에 미만료 `VERIFIED` Verification이 존재
- `DISTINCT MCPTool.id`

증거 목록은 동일 조인 기준의  
`list_tools_with_effective_current_verification()` SELECT만 사용한다.

포함하지 않음:

- Tool.status = `ACTIVE` 강제
- 실시간 연결 테스트 / Discovery / Verification 생성
- MCP Tool 실행, Workflow/Execution 쓰기
- KPI harness (`measure_official_kpi_03` / `artifacts/kpi/*`) 결과 재사용

Verification이 `VERIFIED`가 되려면 도메인(FNC-TOOL-006)에서 이미 연결·Discovery·schema VALID·정상/오류 MANUAL_TOOL_TEST 증빙이 필요하지만,  
L3-06 측정기는 그 사전조건을 **다시 실행하지 않고** 저장된 유효 VERIFIED 행만 집계한다.

## 중복(alias/복제) 처리

1. 집계된 Tool 목록에서 `remote_name` / `display_name` 정규화 키가 동일한 **서로 다른 tool_id**를 의심 그룹으로 기록한다.
2. 의심 그룹을 **임의로 자동 제외하지 않는다**.
3. 의심 그룹을 모두 1개로 붕괴했을 때의 하한(`unique_lower_bound`)을 계산한다.
4. 원본 `counted`와 하한이 MET/NOT_MET을 서로 다르게 만들면 → `INCOMPLETE`  
   (`DUPLICATE_SUSPECTS_AFFECT_JUDGMENT`).
5. 하한·원본이 같은 판정이면 MET/NOT_MET을 유지하고 의심 목록은 증적에 남긴다.

## 인터페이스

```bash
# Local
export PYTHONPATH=backend
python -m tests.evaluation.level3.measure_l3_06 --preflight
python -m tests.evaluation.level3.measure_l3_06 --run --write-artifact
```

또는:

```bash
./scripts/level3-verified-tool-count.sh --preflight
./scripts/level3-verified-tool-count.sh --run --write-artifact
```

서버용 compose 명령은 도구 출력의 `compose_commands`를 따른다  
(`.env.server` + `compose.yaml` + `compose.server.yaml`, backend read-only mount, `artifacts/level3` uid 10001).

## 판정

1. `counted` = DISTINCT 유효 VERIFIED current Tool 수
2. `gap_to_target_min = max(target_min_count - counted, 0)` (`target_min_count=10`)
3. alias/복제 의심이 판정에 영향을 주면 `INCOMPLETE`
4. `counted >= 10` → `MET`, 미만 → `NOT_MET`
5. DB/집계 오류·count≠inventory 불일치 → `INCOMPLETE`
6. 증적 파일 충돌 시 덮어쓰기 금지 (`artifacts/level3/…`)

## 증적 필드 (요약)

- 지표명 / 승인 목표 / 측정 정의
- targets 버전, `counted`, `gap_to_target_min`, `judgment`
- Tool / ToolVersion / Verification / Server ID
- `test_execution_id`, `error_test_execution_id`, `evidence_blob_id` 참조
- `duplicate_suspect_groups`, `unique_lower_bound`
- git_sha, measured_at (UTC), environment

## 운영 사전조건

- 측정 대상 DB에 FNC-TOOL-006 유효 VERIFIED 행이 존재
- `DATABASE_URL` / `TEST_DATABASE_URL` / `MCPFLOW_DATABASE_URL`
- `artifacts/level3` 쓰기 가능 (컨테이너 uid 10001)
- `MCPFLOW_GIT_SHA` 또는 git checkout으로 SHA 해석 가능

운영 측정·배포·DB 데이터 변경은 이 도구 구현 PR 범위가 아니다.
