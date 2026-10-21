# L3-02 v2 의미 기반 판정기 — 오프라인 검토·증적 규약 (구현 단계)

**상태:** SCORER_IMPLEMENTED_ONLY. **정답셋 `PENDING_REVIEW`, 실모델 평가 미실시, 목표 달성 근거 아님.**

## 목적과 경계

`scoring_l302_v2.py`는 문장 내 특정 한국어/영어 키워드 또는
`event.create`와 같은 capability 별칭 목록으로 점수를 결정하지 않는다.
실제 출력의 의미 일치를 **서로 다른 검토자 2명이 독립적으로 8개 항목별
판정**한 기록을 입력받아, 모든 항목이 참인 문항만 성공으로 집계한다.
판정이 일치하지 않으면 두 검토자와 다른 **제3의 검토자가 이견 항목을
조정**해야 한다. 합의된 항목을 임의로 바꿀 수 없다.

이 코드는 사람이 실제로 독립 검토했는지, 기록된 판정이 정확한지까지
**자동으로 증명하지 못한다**. 검토자 식별, 원자료 대조, 서명/증빙과
원본의 진위 확인은 별도 감사·승인 절차의 책임이다. 테스트용 가상
검토자 `test-A`, `test-B` 및 성공률은 실제 평가 이력이 아니다.

## v2 gold 승인 게이트

`dataset_l302.v2.yaml`은 지금 `review_status: PENDING_REVIEW`이며,
`score_reviewed_dataset(...)` 호출 시 무조건 `INCOMPLETE /
GOLD_SET_NOT_APPROVED`를 반환한다. 독립 검토와 과제 담당자 **개별
정답셋 승인 이후 별도 PR**에서 다음 정보를 갖추어야 한다.

- `method_review_status: APPROVED` (방법론만 승인된 상태)
- `review_status: APPROVED`, `reviewed_by`, `reviewed_at`
- `independent_reviews` **정확히 2건**, 각 항목에
  `reviewer_ref`(서로 달라야 함), `decision: APPROVED`,
  `reviewed_at`, `evidence_ref`
- 20개의 고정 `id` 및 유형 구성 조회 6·생성 4·수정 4·문자열 반복 4·
  모호성 2; 외부 모델 출력 확인 후 정답·판정 기준 변경 금지
- 정답셋의 Git SHA / 파일 SHA-256 / 검토 양식 / 승인 로그를 최종 동결

검토 정보는 **실제로 검토자가 작성한 경우에만** 채운다. 증적이 없어도
필드를 임의로 채우거나 코드 게이트를 완화하여 실행해선 안 된다.

## 향후 사용될 문항별 출력 판정 증거의 형태

다음은 **구조 예시**이며 어떤 모델 응답이나 승인 결과도 포함하지 않는다.

```json
{
  "gold_sha256": "<approved dataset sha256>",
  "run_ref": "<immutable approved measurement run ref>",
  "cases": [
    {
      "case_id": "R01",
      "run_ref": "<same measurement run ref>",
      "observed_sha256": "<hash of controlled observed evidence>",
      "schema_valid": true,
      "reviews": [
        {
          "reviewer_ref": "<independent reviewer ref A>",
          "reviewed_at": "<date-time>",
          "evidence_ref": "<auditable external review record>",
          "rationale": "<case-specific reasoning>",
          "checks": {
            "intent": true, "capability": true,
            "entities": true, "provenance": true,
            "temporal": true, "risk": true,
            "clarification": true, "schema": true
          }
        },
        {
          "reviewer_ref": "<independent reviewer ref B>",
          "reviewed_at": "<date-time>",
          "evidence_ref": "<auditable external review record>",
          "rationale": "<case-specific reasoning>",
          "checks": {
            "intent": true, "capability": true,
            "entities": true, "provenance": true,
            "temporal": true, "risk": true,
            "clarification": true, "schema": true
          }
        }
      ]
    }
  ]
}
```

이 예시는 `cases`를 축약한 것이므로 그대로 입력하면
`INCOMPLETE`가 나온다. 이견이 있는 문항에는 `adjudication`으로
다른 검토자의 `reviewer_ref`, `reviewed_at`, `evidence_ref`,
`rationale`, 여덟 `checks`를 추가한다. 각 항목은 bool이어야 한다.
모든 문항의 `run_ref`는 하나로 같아야 하며, 각 `observed_sha256`
및 `gold_sha256`은 64자리 소문자 16진수여야 한다.

**자료 보호:** 본 판정기에는 원문 사용자의 개인정보, 실제 기업의
기밀정보, 모델 응답 전문, Secret, ModelOps 요청·응답을 전달하지 않는다.
합성 테스트 문항의 원문과 관측 결과는 접근 제어된 별도 저장소/검토
절차에서 필요한 최소 기간만 관리하고, 제출 가능한 증적에는 문항별
8개 체크·안전한 해시·이견 조정 결과만 포함한다.

## 점수 정책

1. 독립 평가 문항 **20개 전체**가 존재하고 ID·`run_ref`·해시가
   승인 gold와 일치하며, 서로 다른 2명의 검토가 완료되어야 한다.
2. 여덟 기준 중 **하나라도 거짓이면 실패**.
   스키마 검증에 실패한 문항의 `schema` 체크는 반드시 거짓이다.
3. 검토 충돌 또는 누락이 있으면 임의로 성공 처리하지 않고
   `INCOMPLETE`로 닫는다. 실패 문항 삭제, 일부만으로 점수 계산,
   성공률 재시도 평균치로 교체 금지.
4. 17/20 이상이면 `MET`, 16/20 이하이면 `NOT_MET`. 이 단어는
   **검토 완료한 단일 테스트 런의 판정**일 뿐 과제 전체 승인이나
   외부 전문기관 인증이 아니다.
5. 검토 후 수정 요구는 별도 버전·승인으로 관리한다.
   기존 공식 v1 40%와 두 회귀 30%, 50%는 변경하지 않는다.

## 미구현·다음 단계

- 승인된 정답셋에 대한 개별 2인 검토/서명/담당자 최종 승인
- 승인된 gold SHA-256을 바인딩하는 **실모델 20건 단일 측정 실행기**
- 실제 관측 결과의 최소화된 증거 수집과 접근 제어, 검토자 배정,
  출력 해석 이견 조정 기록의 자동 수집 및 원본 불변성 검증
- 독립 검토를 마친 실측 증적에 대한 과제 성능목표 판정

**절대 금지:** 이 파일이 GitHub main에 머지되었다는 이유만으로
v2 실측이 가능해지거나, 성능지표 85%가 달성되었다고 보고하지 않는다.
