# L3-02 내부 자체평가 실행기 및 판정기 안내 — 정답 승인 후 관측 대기

**내부 방법론 승인:** 2026-10-22 KST (`METHOD_L302_INTERNAL_V1.md`).

**정답셋 상태:** `APPROVED`(2026-10-22 과제 담당자 일괄 확인·승인). **실제 모델 측정 미실시**, 승인 PR의 CI/머지 후에만 실측 가능.

## 구성

| 역할 | 파일/구성 | 현상 |
|---|---|---|
| 기존 2인 독립 평가 v2 | `METHOD_L302_V2.md`, `scoring_l302_v2.py`, `collect_l302_v2.py` | 기존 2인 조건 유지, 별도 미승인 |
| 내부 자체평가 방법 | `METHOD_L302_INTERNAL_V1.md` | 실제 담당자 1인 방식 **승인** |
| 내부 자체평가 정답셋 | `dataset_l302.internal.v1.yaml` | 신규 20개 정답 **2026-10-22 일괄 승인**, SHA-256 지문 고정 |
| 담당자 정답 확인표 | `L302_INTERNAL_OWNER_GOLD_CHECKLIST.md` | 실제 과제 담당자 채팅의 20건 일괄 승인 기록 반영 |
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

**승인 PR이 CI를 통과하고 머지된 이후 예상 결과:** `ok=true`, `run_permitted=true`, `gate_reason=null`, `judgment=INCOMPLETE`. `ok=true`는 사전점검 성공이지 성능 결과가 아니다.

**추가 안전 조건:** `--run`에는 `--ack-gold-reviewed`와 `--write-artifact`, 정확한 `--agent-version-id` 및 DB/모델 사전점검이 필요하다. 승인 기록을 임의 위조하거나 실측을 성능 합격으로 대체하지 않는다.

## 실제 담당자 정답셋 승인 이후 단계

1. 과제 담당자가 2026-10-22 대화에서 20건을 모두 확인하고 정답셋을 일괄 승인하였다. 같은 확인 사실을 gold `owner_review.case_decisions`와 검토표에 반영하였다. 개별 문항에 대한 별도 수정 의견·구체 시각은 제공되지 않았다.
2. 아래 오프라인 명령으로 20문항의 형태·유형·정답 누락과 `case_set_sha256`을 확인한다. 이 검사는 **실제 사람의 확인이나 승인 대신이 아니다**.

   ```bash
   cd ~/mcp-flow/backend
   python -m tests.evaluation.level3.audit_l302_internal_gold
   ```

   결과의 `case_set_sha256`은 검토한 문장과 정답 내용의 지문이다. `ok: true`는 정적 형식 검사 통과이지 최종 승인 뜻이 아니다.
3. 정답셋 `owner_review.case_decisions`의 20개 일괄 확인 기록과 `owner_review.case_set_sha256`을 검증한다. 지문은 `bbcfb06e881e64be9bfa0176c5a4e0bc79573193083711c1edc165d5a0db1b37`이며 정답의 어느 필드든 수정되면 재검토가 필요하다.
4. 골드셋을 새로운 승인 PR로 동결하고 CI를 검증한다. 모델 출력은 이 전 단계에서 열어보지 않는다.
5. 승인된 데이터셋을 이용해 `collect_l302_internal.py --run --ack-gold-reviewed --write-artifact --agent-version-id <uuid>`를 **실측 1회** 수행한다. `--write-artifact`가 private/public 증적을 서로 다른 파일로 배타 생성한다.
6. 접근통제된 관측을 실제 담당자 1인이 검토한다. 각 문항당 의도/기능/엔터티/출처/시간/위험/모호성/스키마 8개 bool과 평가 근거·일시를 기록한다.
7. `score_reviewed_dataset`에 같은 승인 `gold_sha256`, 동일 `run_ref`, 정확히 20문항의 판정 기록을 전달해 `INTERNAL_MET` 또는 `INTERNAL_NOT_MET` 결과를 계산한다.

- 성능 목표는 항상 **85% 이상(20건 중 17건 이상)**.
- DB 쓰기나 MCP Tool 실제 실행은 이 지표에 포함하지 않는다.
- 일반 증적에는 인용 원문·모델 응답 전문·Secret을 저장하지 않는다.
- private 저장 경로: `artifacts/level3/l302-internal-private`(0700), 각 파일(0600).
- 평가 결과 공개/제출 가능 범위는 해당 사업의 공식 제출 요건 확인 뒤 결정한다.

## 운영 서버 실측 절차 (내부 승인 gold만 대상)

**전제:** 아래는 기존 실행기 사용법이며, 운영자 서버에서 수행해야 합니다. 이 문서를 작성했다고 측정한 것이 아닙니다. 기존 독립평가 v2 수집기를 호출하지 않습니다.

### A. 실제 Published AgentVersion 조회 (DB SELECT만)

**주의:** `docker compose exec api python`은 컨테이너 PID 1의 환경설정을 자동 상속하지 않아 `MCPFLOW_DATABASE_URL`이 없을 수 있습니다. `/entrypoint.sh python`을 명시적으로 실행하면 기존 secret file로부터 이 변수를 현재 Python 프로세스용으로 안전하게 조립합니다. DB URL이나 비밀번호는 절대 출력하지 않습니다.

```bash
cd ~/mcp-flow
docker compose --env-file .env.server \
  -f compose.yaml -f compose.server.yaml \
  exec -T api /entrypoint.sh python - <<'PY'
import asyncio
import os
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

SQL = text("""
SELECT av.id::text AS id, a.name AS agent, av.version_no AS version_no,
       av.request_schema_version AS schema_version,
       p.provider AS provider, p.model AS model, p.base_url AS base_url,
       (p.credential_secret_id IS NOT NULL) AS requires_secret
FROM agent_versions AS av
JOIN agents AS a ON a.id = av.agent_id
JOIN llm_profiles AS p ON p.id = av.llm_profile_id
WHERE av.status = 'PUBLISHED' AND a.status = 'ACTIVE'
ORDER BY a.name, av.version_no DESC
""")

async def main():
    engine = create_async_engine(os.environ["MCPFLOW_DATABASE_URL"])
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(SQL)).mappings().all()
        if not rows:
            print("NO_ACTIVE_PUBLISHED_AGENT_VERSIONS")
        for row in rows:
            url = urlsplit(row["base_url"])
            is_modelops = (
                url.scheme == "https"
                and url.hostname == "modelops-gateway.openlink.kr"
                and url.port in (None, 443)
            )
            eligible = (
                is_modelops
                and row["model"] == "qwen3-14b"
                and row["provider"] == "OPENAI_COMPATIBLE"
                and row["schema_version"] == "1.0"
                and not row["requires_secret"]
            )
            print(
                f"agent_version_id={row['id']} agent={row['agent']!r} "
                f"version={row['version_no']} model={row['model']} "
                f"gateway={is_modelops} schema={row['schema_version']} "
                f"secret_ref={row['requires_secret']} ready_candidate={eligible}"
            )
    finally:
        await engine.dispose()

asyncio.run(main())
PY
```

검증할 Published AgentVersion이 없거나 `ready_candidate=True`인 항목이 없다면 **임의 UUID를 넣거나 실측을 강행하지 말고** 프로파일·AgentVersion 설정 상태를 먼저 확인해야 합니다. 현재 `ModelProviderClient`는 비어 있지 않은 `credential_secret_id`를 일반적인 실행 경로에서 해석하지 못할 수 있으므로 실제 비밀 참조 상태도 확인합니다.

### B. 승인 gold 읽기 전용 확인 (DB/LLM 호출 없음)

```bash
cd ~/mcp-flow
docker compose --env-file .env.server \
  -f compose.yaml -f compose.server.yaml \
  run --rm --no-deps -T \
  -v "$(pwd)/backend:/opt/kpi-src:ro" \
  -e PYTHONPATH=/opt/kpi-src \
  api python -m tests.evaluation.level3.collect_l302_internal --preflight
```

현재 승인 상태에서 예상되는 필수 필드: `run_permitted=true`, `gold_review_status=APPROVED`, `gate_reason=null`, `judgment=INCOMPLETE`. 이 사전검사는 실제 프로파일·DB/Gateway 연결을 확인하지 않습니다.

### C. 운영 실측 20건 1회 (실제 비용·증적 발생)

먼저 A의 `ready_candidate=True`인 **실제 Published AgentVersion ID**를 사용합니다. B가 정상이고 증적 디렉터리 권한을 확인한 뒤에만 실행합니다.

```bash
cd ~/mcp-flow
export AGENT_VERSION_ID="A에서 확인한 실제 UUID"
mkdir -p artifacts
chmod 700 artifacts

docker compose --env-file .env.server \
  -f compose.yaml -f compose.server.yaml \
  run --rm --no-deps -T \
  --user "$(id -u):$(id -g)" \
  -v "$(pwd)/backend:/opt/kpi-src:ro" \
  -v "$(pwd)/artifacts:/opt/kpi-artifacts" \
  -e PYTHONPATH=/opt/kpi-src \
  -e MCPFLOW_ARTIFACT_DIR=/opt/kpi-artifacts \
  -e MCPFLOW_GIT_SHA="$(git rev-parse HEAD)" \
  -e MCPFLOW_ENV_LABEL=pilot \
  api python -m tests.evaluation.level3.collect_l302_internal \
     --run --ack-gold-reviewed --write-artifact \
     --agent-version-id "$AGENT_VERSION_ID"
unset AGENT_VERSION_ID
```

`--user` 옵션은 기록 파일을 서버 실행자의 UID/GID로 저장하여 Docker 서비스 계정과 호스트 디렉터리 권한 불일치를 예방합니다. 모델 호출 **전에** public/private 증적 경로에 임시 파일을 생성·삭제해 쓰기 가능 여부를 확인하며, 검증 실패 시 `EVIDENCE_PRECHECK_FAILED`로 DB/LLM 요청 전에 종료합니다. 보안상 민감한 원본은 `artifacts/level3/l302-internal-private` (0700/0600)에 보관하며 외부 공유하지 않습니다.

콘솔의 `artifact_path`에 해당하는 **공개 요약 JSON**만 확인·제공합니다. `private_evidence_path` 경로 자체와 원본 데이터는 게시하지 않습니다. 결과는 `PENDING_ACTUAL_HUMAN_SEMANTIC_REVIEW`이며 **성공률이 아닙니다**. 관측이 실패하더라도 무단 재측정하여 불리한 결과를 대체해서는 안 됩니다.

최종 `INTERNAL_MET` / `INTERNAL_NOT_MET`는 실제 담당자가 20건 각각의 의도/기능/엔터티/출처/시간/위험/모호성/스키마를 직접 평가한 뒤 `scoring_l302_internal.score_reviewed_dataset`으로 계산합니다. **목표 85%는 변경하지 않습니다.**

---

**유의:** 본 문서는 시험 실행 **준비**와 절차만 기록한다. 신규 v2/internal 실측 결과나 85% 목표 충족을 증명하지 않는다.
