# Level 3 L3-03 — 예약작업 정상 수행률 (평가 방법론 초안)

**상태: DRAFT / 공식 평가계획·시험 범위 승인 전.** 성능목표는 협약 승인치 **90% 이상**이며 변경하지 않는다. 본 PR은 측정 도구만 구현하며 운영 예약을 생성하거나 실행하지 않는다.

## 1. 기존 운영 데이터 관측 (참고, 공식 평가 아님)

2026-11-19 읽기 전용 운영 조회(최근 30일): 발생 이력 20건 중 `COMPLETED + SUCCEEDED` 9건, `SKIPPED/MISFIRE_SKIP` 10건, `FAILED/TARGET_PRECONDITION_FAILED` 1건. **기록된 20건 전체를 실행 성공 분모로 보면 9/20 = 45%**. 단, 이 구간에는 사전 고정된 L3-03 시험계획/기대 due slot 목록이 없으므로 수치를 소급해 공식 성능평가로 주장하지 않는다. 기존 이력 삭제·수정·제외 금지.

`MISFIRE_SKIP`는 스케줄러의 의도된 미실행 정책일 수 있지만 **업무 실행 완료는 아니므로 성공으로 세지 않는다**. 의도된 정책 동작의 정확성은 별도 기능검증으로 설명한다. 해당 건을 분모에서 임의 제거해 9/10 = 90%로 주장하지 않는다.

## 2. 사전 고정된 분모

공식 평가 전에 실시간 예약 상태와 독립적으로 아래 항목을 plan 파일에 확정한다.

- `evaluation_window.start/end`: UTC 반개구간 `[start, end)`.
- `slots[]`: 전체 시험 예약 시각별 `(schedule_id, scheduled_for)`의 고정 목록.
- `completion_grace_seconds`: 평가 종료 후 최대 1시간 이내 고정된 완료 유예 시간.
- `example: false`와 `plan_version: level3-schedule-completion.v1`.
- plan 파일 바이트의 SHA256을 평가 **시작 전** `--preflight --write-artifact` 증적에 기록한다.

시험 환경과 일정을 정할 때 특정 결과를 보고 목표를 바꾸거나 불리한 예약만 제외해서는 안 된다. 한 스케줄을 평가하는 동안 발생하도록 설계된 **모든 due 시각**을 포함하고, 일시중지·정책 건너뜀·자원 장애까지 기록한다. 시각 목록과 실제 예약 주기가 일치하는지는 시험 담당자가 사전 확인한다.

평가 도구는 `schedule_occurrences`와 `executions`에서 계획 대상 스케줄의 **구간 내 모든 행**을 SELECT한다. 계획에 빠진 구간 내 Occurrence가 발견되면 임의로 버리지 않고 `INCOMPLETE / EXTRA_OCCURRENCES`로 처리한다.

## 3. 정상 수행의 정의

고정 slot 1개당 분모 1건.

- **성공(1)**: 정확히 1개 `schedule_occurrences` 행이 있고 상태가 `COMPLETED`이며, 정확히 1개 연결 `executions` 행의 `source_type=SCHEDULE_OCCURRENCE`, `status=SUCCEEDED`, `finished_at`이 존재한다.
- **실패(0)**: `SKIPPED`(`MISFIRE_SKIP` 포함), `FAILED`(`TARGET_PRECONDITION_FAILED` 포함), 예약행 부재, Execution 부재/실패/시간초과/미종료, Occurrence 상태 `PLANNED/ENQUEUED/RUNNING` 지속.
- **평가불가**: 예정 slot 중복, 예상 외 추가 Occurrence, 같은 Occurrence에 Execution 둘 이상, 알 수 없는 상태, 불일치하는 사전 계획/증적.

`success_rate_percent = succeeded_slots / all_frozen_slots * 100`; 90% 이상이면 `MET`, 미만이면 `NOT_MET`. 실패는 분모에서 빠지지 않는다.

## 4. 증빙 및 운영 안전

1. 버전 고정된 plan을 시험 전에 생성한다. 시험 시작 전 `--preflight --write-artifact --plan ...` 실행 (선택 대상 스케줄 존재·ACTIVE 검사).
2. 실제 실행은 기존 MCPFlow 예약 기능으로만 진행한다. 평가기는 예약 생성, 즉시 실행, 스케줄러 호출, 외부 Tool 호출, DB UPDATE를 하지 않는다.
3. 마지막 due 시각이 종료되고 사전 설정 유예 시간이 지난 후 `--run --write-artifact --plan ... --preflight-artifact ...` 실행. **사전 증적의 plan SHA256 및 사전 측정시각이 시험 시작 이전**임을 확인한다.
4. 공식 측정기는 PostgreSQL `REPEATABLE READ READ ONLY`로 행을 읽고 occurrence/Execution ID·상태·시각·사유만 기록한다. `input_template`, 요청 payload, token/secret을 수집하지 않는다.
5. 증적은 `artifacts/level3/l3_03_schedule_completion_<SHA>_<UTC>.json`에 새 파일 생성(`x` 모드, 덮어쓰기 금지). 기존 L3-01/L3-05/L3-06 및 내부 KPI 증적과 분리한다.
6. 변경/장애 이후 재시험은 **신규 plan과 신규 window**로만 진행하고, 최초 결과는 보존한다.

## 5. 주의점

- 이 측정은 **사전에 고정한 due slot들의 실제 예약 완료율**을 평가한다. 저장되지 않은 임의 외부 스케줄이나 스케줄 엔진이 생성하지 않은 due 시각을 자동 추론하지 않는다. 그러므로 사전 슬롯 완전성과 예약 주기 대표성을 검토해야 한다.
- `SKIPPED`는 정책에 따라 의도된 정상 동작일 수 있지만 `정상 실행 완료`라는 본 지표의 성공에는 포함하지 않는다. 승인 기관에서 '정상 수행'의 정의를 다르게 요구하면 실행 후 기준을 바꿔 재채점하지 않고 방법론을 별도 버전으로 승인받아야 한다.
- 평가는 시험계획 승인 및 운영에서 충분한 실증 자료 확보 후 진행한다. 기존 20건은 진단 자료일 뿐 공식 `MET/NOT_MET` 근거가 아니다.
