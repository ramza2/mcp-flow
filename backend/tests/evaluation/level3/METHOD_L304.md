# Level 3 L3-04 — 복합 실행 시나리오 완료율 (평가 방법론 초안)

**상태: DRAFT / 공식 시험계획 사전 확정 및 운영 실행 전.**

- 승인 목표: **85% 이상** (`backend/tests/evaluation/level3/targets.v1.yaml`).
- 평가 대상: `workflow-scenarios/executable.v1.yaml`의 6개 복합 실행 **유형**을 활용하되, 내부 `KPI-05` 결과를 L3-04 성공으로 바로 복제하지 않는다.
- 공식 분모: **서로 다른 WorkflowVersion으로 구성한 6개 시나리오 × 각 1회 = 6회**, 사전에 동결.
- 정수 임계값: 6/6 = 100% MET, **5/6 = 83.3333% NOT_MET**. 목표 85%를 5/6으로 반올림하거나 목표를 조정하지 않는다.

## 1. 시나리오와 판정 조건

| 시나리오 ID | 유형 | 완료 증거 |
|---|---|---|
| `sequential_tool_chain` | 순차 | 실행 계획에 TOOL 종속 관계, 실제 성공 TOOL Step 2개 이상 |
| `parallel_join_wave` | 병렬 | 계획에 JOIN fan-in, 성공 TOOL Step 2개 이상 및 JOIN 성공 |
| `condition_branch` | 조건 | CONDITION 결과, 선택 분기 SUCCEEDED, 비선택 분기 SKIPPED, JOIN 성공 |
| `foreach_loop` | FOR_EACH | 실제 LOOP Step SUCCEEDED 및 ToolCall 성공 |
| `while_loop` | WHILE | 실제 LOOP Step SUCCEEDED 및 ToolCall 성공 |
| `approval_wait_resume` | 승인 후 재개 | 독립 Workflow의 명시적 `APPROVAL` Step SUCCEEDED, 해당 Step에 연결된 `AUTHORABLE_STEP` ApprovalRequest APPROVED 및 후속 TOOL Step/ToolCall 성공 |

모든 유형에서 `executions.source_type=WORKFLOW_VERSION`, `status=SUCCEEDED`, `finished_at` 존재 및 사전 완료마감 이내를 요구한다. 실행 계획 형태는 기존 `plan_matches_scenario_kind()`와 비교한다. 단위 테스트 결과, LLM 서술, 환경변수의 성공 플래그만으로 성공 판정 금지.

**승인 시나리오 구현 원칙:** 현재 `suggest_questions` ToolPolicy는 `requires_approval=false`다. 이 공통 ToolPolicy를 `true`로 변경하지 말고, 평가 전용 Workflow에 `APPROVAL` 단계를 추가하여 해당 Workflow에만 승인 흐름을 적용한다. 활성 `ApprovalPolicy`를 명시적으로 선택하고 실제 승인 권한을 가진 담당자가 승인해야 하며, 승인 요청의 `approval_kind=AUTHORABLE_STEP`·`step_execution_id`와 완료된 Step ID가 일치해야 한다. 과거 ToolPolicy 기반 승인 이력 또는 단순 승인 상태만으로 이번 공식 승인 시나리오 성공을 인정하지 않는다.

병렬의 실제 시간 겹침은 별도 진단이다. JOIN fan-in 실행과 정상 종료 여부가 본 완료율의 판정 근거이며, 벽시계상의 동시성 여부를 단정하지 않는다.

## 2. 계획 사전 동결

운영 실행 전에 `plan_l304.example.v1.yaml` 형식을 복사하여 다음을 고정한다.

- `evaluation_window.start/end`: UTC `[start, end)`, 변경 불가
- `completion_grace_seconds`: 0~3600초. 사전 결정
- `cases[]`: 위 6개 `scenario_id`와 대응하는 `kind`, **고유한** `workflow_version_id` 하나씩
- `example: false` (실제 WorkflowVersion 대상과 시간 확인 후만 변경)
- 파일 SHA256 및 Preflight 실행 시각을 첫 Execution 전에 증적에 보존

각 WorkflowVersion은 사전검사에서 `PUBLISHED/VALID`, 원본 Workflow는 `ACTIVE`, 계획 형태 일치여야 한다. 평가 시작 전 해당 버전의 Execution이 평가 구간에 0건인지 확인한다.

**중요:** 기존 KPI-05에서 이미 실행한 Execution을 뒤늦게 골라 `--run`으로 공식 L3-04 성능을 주장하지 않는다. 과거 6개 실행을 재활용하려면 독립 검토용 회고 분석과 사전 동결 공식 평가를 엄격히 구분한다. 이번 도구의 공식 경로는 시험 전에 플랜 및 버전을 동결한 **전향적 평가**만 지원한다.

## 3. 공식 실행 및 채점

1. 평가 시작 전에 `--preflight --write-artifact --plan <FILE>`로 시험계획 SHA256, 6개 WorkflowVersion 및 DB 상태를 고정한다.
2. 별도 허가된 정상 Workflow 실행 경로로 **각 버전 정확히 1회**를 실행한다. 평가기는 Workflow 생성·실행·승인·Tool 호출 등을 하지 않는다.
3. 종료시각+유예시간이 지난 후 `--run --write-artifact --plan <FILE> --preflight-artifact <PREFLIGHT_JSON>`으로 한 번 판정한다.
4. 검사기는 PostgreSQL `REPEATABLE READ READ ONLY`에서 평가 구간 내 해당 WorkflowVersion의 **모든** Execution을 SELECT한다. `source_type`이나 성공 상태를 조건으로 미리 걸러내지 않는다.
5. 버전별 0건이면 실패(분모 유지), 2건 이상이면 평가불가(`INCOMPLETE`)로 처리. 한 건 존재해도 FAILED/PARTIALLY_SUCCEEDED/CANCELLED/TIMED_OUT, 잘못된 계획, Step 불완전, ToolCall 미검증, 승인 대기/거절 등은 실패 0점. `SUCCEEDED` 실행 안에 `PENDING`·`READY`·`RUNNING`·`WAITING_INPUT`·`WAITING_APPROVAL` 등 미종결 Step이 남아 있어도 실패로 판정한다. 정상 비선택 분기는 `SKIPPED`를 허용한다.
6. Preflight와 비교해 WorkflowVersion의 상태·원본 Workflow 상태·계획 형태가 달라졌다면 평가불가.
7. `completion_rate_percent = passed_cases / 6 × 100`, 값이 ≥85%이면 `MET`, 미달 `NOT_MET`. 최초 결과·실패·중단 이력은 삭제/덮어쓰기 금지.

## 4. 보안 및 증적

- 공식 측정은 **SELECT 전용**. 외부 MCP Tool 호출, 승인/상태 변경, DB 마이그레이션 없음.
- 평가기가 읽는 `plan_snapshot`은 형태 확인에만 사용하며 **증적에 원문, Tool 파라미터, 입력 및 secrets를 출력하지 않는다.**
- 증적: `artifacts/level3/l3_04_composite_completion_<SHA>_<UTC>.json` 새 파일 생성(`x`, 덮어쓰기 금지). 시나리오별 Execution ID·상태·Step 유형 및 상태·승인요청 ID·성공 ToolCall 수 등만 기록.
- Preflight와 공식 실행은 같은 `plan_sha256`, 환경, Git SHA를 요구한다.
- `example: true` 계획은 구조 점검만 가능하고 공식 실행에 사용할 수 없다.
- L3-03 예약 `2026-11-23 14:00~15:00 KST`은 영향을 받지 않는다.

## 5. 다음 실제 준비

운영에서 **읽기 전용으로** 기존 6개 시나리오의 WorkflowVersion/Execution 이력을 확인한 후, 실제 평가용 6개 고유 Published/Valid 버전과 수행 시간대를 확정해야 한다. 기존 이력의 결과로 대상/분모를 골라선 안 된다. 새 Workflow 생성·승인 실행·운영 변경은 별도 범위 검토와 허가가 필요하다.
