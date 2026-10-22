# L3-02 v2 실모델 관측 수집기 — 승인 전 안전 실행 가이드

**상태 (2026-10-22): 신규 gold는 PENDING_REVIEW, 실모델 측정 미실시.**
`collect_l302_v2.py`는 이번 PR에서 **수집 기능만 구현**한다.
v1 평가기, gold, 점수 및 운영 Analyzer는 수정하지 않는다.

## 실행 구분

- `--preflight`: **정답셋 승인 여부만 확인**한다.
  DB·ModelOps·MCP Tool은 호출하지 않으며 현재 정상 출력은
  `ok=true, run_permitted=false, gate_reason=GOLD_SET_NOT_APPROVED,
  judgment=INCOMPLETE`이다. 이 응답의 `ok`는 사전점검 프로그램
  실행 성공이지 정답셋이나 성능 승인 뜻이 아니다.
- `--run`: 독립 검토자 2인의 **실제 승인 기록**, 과제 담당자
  gold 승인, `--ack-gold-reviewed`가 모두 충족되어야 실행한다.
  지금 상태에서는 실제 LLM·DB 접속 이전에 거부한다.
- 모델 호출은 승인된 **20개 고정 합성 지시문을 순서대로 1회** 수행하고
  `RequestAnalyzerService._build_messages`,
  `ModelProviderClient.generate_json`,
  `StructuredRequestV1.model_validate` 및 Analyzer 명확화
  후처리를 v1과 같은 방식으로 이용한다.
- `--run`은 **관측 수집만** 하며 성공률을 계산하지 않는다.
  문항별 판정·최종 점수는 검토자 평가가 끝난 뒤
  `scoring_l302_v2.py`에서 집계한다.
- 한 건의 모델/JSON/스키마 호출 실패도 20개 분모에 남고,
  출력은 `schema_valid=false`와 안전한 오류 코드로 보존한다.
  재시도나 임의 제외는 하지 않는다.

## 현재 사용할 수 있는 사전점검

서버에 이번 PR이 머지되고 로컬에 반영되었다면:

```bash
cd ~/mcp-flow
docker compose --env-file .env.server \
  -f compose.yaml -f compose.server.yaml \
  run --rm --no-deps -T \
  -v "$(pwd)/backend:/opt/kpi-src:ro" \
  -e PYTHONPATH=/opt/kpi-src \
  api sh -c 'exec python -m tests.evaluation.level3.collect_l302_v2 --preflight'
```

**예상 결과:** `GOLD_SET_NOT_APPROVED`.
현재는 별도의 운영 배포/LLM 측정을 할 필요가 없다.

## gold 승인 후에만 허용할 실측 명령 예시

**이 명령은 아직 실행하지 않는다.** 검토자 2인의 실제 정답별
검토·이견조정, 담당자 개별 최종 승인, 별도 승인 PR/CI/머지가
선행해야 한다.

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
  api sh -c 'export DATABASE_URL="$MCPFLOW_DATABASE_URL"; exec python -m tests.evaluation.level3.collect_l302_v2 --run --ack-gold-reviewed --write-artifact --agent-version-id 45e06703-7af8-4e1b-9da9-518cea072759'
```

운영에서 실측하는 경우 고정된 git SHA, gold SHA, 모델 프로파일 ID·
lock_version 및 실행 고유 `run_ref`를 남긴다.
신규 gold 승인·점수 평가 전에 실제 시험을 미리 호출하면
데이터셋의 독립성이 훼손될 수 있으므로 금지한다.

## 파일과 최소 수집 정보

- 일반 JSON: `artifacts/level3/l3_02_v2_*.json`.
  케이스 ID/유형, 구조 유효 여부, 오류 코드, 소요시간,
  관측 증거 SHA-256, 실행 Git SHA, gold SHA, 모델 프로파일
  메타데이터, `judgment=INCOMPLETE`만 포함한다.
- 접근 제한 JSON: `artifacts/level3/l302-v2-private/l3_02_v2_*.json`.
  각 케이스의 `StructuredRequestV1` 계약 필드(관측 내용),
  스키마 유효 여부, 안전한 오류 진단 및 동일 실행 메타데이터.
  생성 폴더는 `0700`, 생성 파일은 `0600`이고
  같은 이름 파일 덮어쓰기/심볼릭 링크 추적을 허용하지 않는다.
- 두 파일 모두 별도 불변 이름으로 작성하고 재측정으로 덮어쓰지
  않는다. private 증적은 승인된 검토자만 접근해야 한다.
  **이 디렉터리를 GitHub, 공개 CI 로그, 메신저, 외부 제출
  산출물에 올리지 않는다.**
- 각 private observation의 canonical JSON SHA-256이 공개
  `observed_sha256`과 동일해야 한다. 최종 의미 평가에는
  두 명의 **실제 사람**이 이 통제된 관측과 승인 gold를
  독립적으로 확인한 기록이 필요하다.
- 외부 일반 사용자의 입력을 이 평가에 사용하지 않는다.
  원문 모델 응답이나 예외 메시지(잠재적 credential 포함)는
  일반 JSON/CLI 결과에 출력하지 않는다.

## 아직 남아 있는 단계

1. **정답셋 검토:** 두 독립 검토자가 20개 문장·정답을 각각 검토하고
   이견을 조정한 뒤 과제 담당자가 최종 승인.
2. 신규 정답셋 승인 증적을 별도 PR에 남기고 CI 확인.
3. 수집된 private 응답을 접근통제한 상태로 검토자에게 제공하며
   문항별 8개 요소를 판단; 제3자 조정이 필요하면 기록.
4. `scoring_l302_v2.score_reviewed_dataset`에 gold 해시가
   같은 20건의 검토 완료 증거를 전달하여 85% 판정.
5. 기존 v1 공식 40% 및 두 회귀 30%, 50%와 v2 성과를
   별도 판정 기준·데이터셋으로 구분해 수행결과보고서에 기록한다.

**현재 이 파일은 개발 준비 문서이며, 신규 v2 평가 결과 또는
독립적 성능목표 달성을 주장하지 않는다.**
