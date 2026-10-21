# Level 3 L3-02 — 자연어 업무지시 분석 성공률 (승인된 평가 데이터셋)

승인 성능목표 **85% 이상**. 이 평가는 기존 내부 KPI-02 (자연어→Tool Top-1
매핑 정확도)와 **별개**이며, Tool 선택 성공으로 대체하지 않는다.

## 범위 및 정당성

운영 AgentVersion의 `system_instruction` 및 LLMProfile (실제 ModelOps LLM 연결)을
읽어 **실제 LLM 응답**을 얻는다. 아래 운영 코드와 동일 메서드를 사용한다.

1. `RequestAnalyzerService._build_messages` — system contract + Agent instruction + v1 schema + user text
2. `ModelProviderClient.generate_json` — 운영 프로파일 (모델·파라미터)
3. 시스템 소유 `schema_version`, `request_text` 고정 후 `StructuredRequestV1.model_validate`
4. `RequestAnalyzerService._sanitize_request_level_clarification`
5. 판정용 `scoring_l302.score_case` (실제 출력에 대해서만 채점)

**평가를 위해 운영 AgentRequest / Conversation / Execution을 생성하지 않는다.**
따라서 이 시험은 **자연어 분석 핵심 처리 구간의 라이브 평가**이며
`RequestAnalyzerService.analyze()`의 DB CAS·상태전환 품질 평가는 포함하지 않는다.
Tool 검색/선택, MCP 실행, 임베딩, 예약작업 시간도 포함하지 않는다.

## 정답셋과 판정

- `dataset_l302.v1.yaml`: 고정 자연어 지시 **20건** (날씨조회 5, 일정등록 5,
  일정조회 4, Echo 4, 의도불명확 2)
- `scoring_l302.py`: 사전 고정된 의미상 허용 capability 문자열 집합, 의도 단어,
  리스크 유형, 명시 엔터티와 명확화 여부 검증
- 단일 케이스는 **모든 검사** 통과 시 성공 (부분점수 없음).
- 일정 등록 5건: 날짜(YYYY-MM-DD) 및 시각(HH:MM) 추출 필수. 날짜·시간은
  `USER_EXPLICIT` 시간 엔터티에서만 검사하며 ISO 8601 및 `2026년 11월 2일 오후 3시`
  같은 한국어 동등 표현을 인정. 시간대 `+09:00` 등 offset을 시각으로 오인하지 않음.
- 일정 조회 4건: 단일 날짜, 시작/종료 날짜(양쪽 모두), 또는 전체 월 정보 추출 필수.
  `2026-11`(월 전체)과 `2026-11-02`(하루)는 **동일한 범위가 아님**.
- 실행일에 따라 정답이 달라지지 않도록 날짜 질의는 모두 **2026년의 절대 날짜** 사용.
- 시간 외에 기존 intent, business capability, risk, 명시 엔터티, clarification 검증 유지.
- `success_rate_percent = 성공 케이스 수 / 전체 고정 20건 × 100`.
- 17건 이상 성공 → `MET`(85% 이상), 16건 이하 → `NOT_MET`.
- 모델 호출 실패/JSON 스키마 오류/의도·능력·리스크·엔터티 오류는
  **분모에서 제거하지 않고 실패로 산정**.
- 실제 시행 케이스 수가 고정 20건과 불일치하면 `INCOMPLETE`.
- 채점 시 애매한 신규 synonym을 사후에 추가하여 성공률을 올리지 않는다.

### 중요: 골드셋 검토

**2026-10-21 과제 담당자가 채팅에서 20건의 고정 정답셋과 사전 정의된 채점 기준을 명시적으로 승인**했다.
`dataset_l302.v1.yaml`의 상태는 **`APPROVED`**이며, 세부 승인 경위는 `GOLD_REVIEW_L302.md`에 기록한다.
이번 승인은 정답셋에 대한 담당자 승인이지 별도의 외부 전문기관 인증이나 성능목표 달성 판정이 아니다. `--run` 실행은
`--ack-gold-reviewed`(검토 확인)에 의해 보호된다. 이 플래그는 **실제 검토가
끝난 후에만** 사용해야 한다. 또한 **review_status=APPROVED + reviewed_by + reviewed_at**이
정답셋에 모두 포함되지 않으면 `--ack-gold-reviewed`를 넘겨도 실행을 거부한다.
정답셋 승인 기록은 **별도 승인 PR/커밋**으로 남기고 CI 통과를 확인한다.
평가 결과에 검토자, 검토 시각, 데이터셋 해시를 남긴다.

## 서버 Preflight (DB SELECT만 수행, LLM 호출 없음)

```bash
cd ~/mcp-flow
docker compose --env-file .env.server \
  -f compose.yaml -f compose.server.yaml \
  run --rm --no-deps -T \
  -v "$(pwd)/backend:/opt/kpi-src:ro" \
  -v "$(pwd)/artifacts:/opt/kpi-artifacts" \
  -e PYTHONPATH=/opt/kpi-src \
  -e MCPFLOW_ARTIFACT_DIR=/opt/kpi-artifacts \
  -e MCPFLOW_GIT_SHA="$(git rev-parse HEAD)" \
  -e MCPFLOW_ENV_LABEL=server-live-level3-l302 \
  api sh -c 'export DATABASE_URL="$MCPFLOW_DATABASE_URL"; exec python -m tests.evaluation.level3.measure_l3_02 --preflight --agent-version-id 45e06703-7af8-4e1b-9da9-518cea072759'
```

원문 데이터셋 및 검토표 확정 후 `--run --ack-gold-reviewed --write-artifact`로
동일한 컨테이너 환경에서 20회 **LLM API 호출**을 수행한다.
실측 전후 모델 프로파일 ID/lock_version, 코드 SHA, 데이터셋 SHA를 고정한다.

## 증적

`artifacts/level3/l3_02_natural_language_analysis_<SHA>_<UTC>.json`
(파일 배타적 생성, 기존 L3-05 JSON 수정 없음).

포함: Git SHA, UTC, Model Profile ID 및 lock_version, 데이터셋 SHA256,
고정 분모 20, 항목별 성공/실패 및 체크, 목표/성공률/판정, 검토 확인 여부.
모델 출력 전체, Credential, ModelOps URL, 토큰과 비밀번호는 기록하지 않는다.

## 사전 확인

- AgentVersion `PUBLISHED` / Agent `ACTIVE` / 요청 스키마 v1
- 실제 LLMProfile 참조가 존재하며 `OPENAI_COMPATIBLE` Provider
- 데이터셋과 채점 기준 담당자 승인 (`APPROVED`), 승인 커밋의 CI 통과 및 병합 확인
- ModelOps Gateway 연결 가능 (preflight는 네트워크 조회하지 않음)
- `artifacts/level3` 컨테이너 UID 10001 쓰기 가능
