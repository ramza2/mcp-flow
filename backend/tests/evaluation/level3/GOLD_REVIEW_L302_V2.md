# L3-02 v2 신규 정답셋 검토표 — 승인 전

**상태: PENDING_REVIEW / 과제 담당자 및 독립 검토자 미승인 / LLM 실행 미실시.**

- 방법론: `METHOD_L302_V2.md` — 2026-10-21 **방법론만** 과제 담당자 채팅에서 승인.
- 검토 대상: `dataset_l302.v2.yaml` — 신규 20개 문항 및 아래 문항별 gold는 **별도 승인 필요**.
- 유형 분포: 조회 6, 등록 4, 수정 4, 원문 반복 4, 의도 불명확 2.
- 대상과 사람 이름/프로젝트/티켓/문서 ID는 테스트용 가상 예시다. 외부 서비스·Tool 존재를 전제하지 않는다.
- v1 20건에 대한 모델 출력과 실패 사례가 이미 알려져 있어 **정답셋 작성자가 완전한 블라인드 상태는 아니었다**. 그러나 신규 v2 문장의 **모델 출력을 보거나 시범 실행하지 않았음**. 독립 검토/사전 동결이 필요하다.
- `intent` 및 `capability`는 업무 **의미** 명세다. 키워드·도메인 동작 문자열의 일치 여부만으로 성공/실패를 결정하지 않는다.

## 신규 문항 20건 및 의미 정답 검토

| ID | 구분 | 자연어 지시문 | 사전 고정 업무 의미(도메인/동작) | 필수 엔터티 / 시간 / 위험 등 |
|---|---|---|---|---|
| R01 | 조회 | 프로젝트 아틀라스의 2026년 9월 2일자 검토 문서를 찾아줘 | document/search | project=아틀라스 (USER_EXPLICIT); 시점={"kind":"date","dates":["2026-09-02"]}; risk=READ_ONLY |
| R02 | 조회 | 품목 코드 RB-204의 현재 재고 수량을 확인해줘 | inventory/lookup | item_code=RB-204 (USER_EXPLICIT); risk=READ_ONLY |
| R03 | 조회 | 지원 티켓 INC-7315의 처리 상태를 조회해줘 | support_ticket/lookup | ticket_id=INC-7315 (USER_EXPLICIT); risk=READ_ONLY |
| R04 | 조회 | 2026년 9월 1일부터 9월 7일까지 접수된 문의 목록을 보여줘 | inquiry/list | 시점={"kind":"range","dates":["2026-09-01","2026-09-07"]}; risk=READ_ONLY |
| R05 | 조회 | 거래처 해솔테크의 미결제 청구서 목록을 찾아줘 | invoice/list | customer=해솔테크 (USER_EXPLICIT); risk=READ_ONLY |
| R06 | 조회 | 2026년 8월의 서비스 장애 기록을 조회해줘 | incident/list | 시점={"kind":"month","months":["2026-08"]}; risk=READ_ONLY |
| C01 | 등록 | 프로젝트 오로라에 '데이터 검증' 작업을 새로 등록해줘 | task/create | project=오로라 (USER_EXPLICIT); title=데이터 검증 (USER_EXPLICIT); risk=IDEMPOTENT_WRITE/NON_IDEMPOTENT_WRITE |
| C02 | 등록 | '고객 피드백 검토' 작업을 새로 만들고 마감 시각을 2026년 12월 3일 오전 10시로 지정해줘 | task/create | title=고객 피드백 검토 (USER_EXPLICIT); 시점={"kind":"datetime","dates":["2026-12-03"],"time_24h":"10:00","role":"deadline"}; risk=IDEMPOTENT_WRITE/NON_IDEMPOTENT_WRITE |
| C03 | 등록 | 김민준을 담당자로 지정해서 '대시보드 점검' 할 일을 생성해줘 | task/create | assignee=김민준 (USER_EXPLICIT); title=대시보드 점검 (USER_EXPLICIT); risk=IDEMPOTENT_WRITE/NON_IDEMPOTENT_WRITE |
| C04 | 등록 | 거래처 솔빛상사에 대한 '후속 연락' 작업을 새로 추가해줘 | task/create | customer=솔빛상사 (USER_EXPLICIT); title=후속 연락 (USER_EXPLICIT); risk=IDEMPOTENT_WRITE/NON_IDEMPOTENT_WRITE |
| U01 | 수정 | 작업 TK-918의 상태를 완료로 변경해줘 | task/update | task_id=TK-918 (USER_EXPLICIT); status=완료 (USER_EXPLICIT); risk=IDEMPOTENT_WRITE/NON_IDEMPOTENT_WRITE |
| U02 | 수정 | 버그 BUG-420의 우선순위를 높음으로 바꿔줘 | issue/update | issue_id=BUG-420 (USER_EXPLICIT); priority=높음 (USER_EXPLICIT); risk=IDEMPOTENT_WRITE/NON_IDEMPOTENT_WRITE |
| U03 | 수정 | 문서 DOC-256의 제목을 '서비스 운영 지침'으로 수정해줘 | document/update | document_id=DOC-256 (USER_EXPLICIT); title=서비스 운영 지침 (USER_EXPLICIT); risk=IDEMPOTENT_WRITE/NON_IDEMPOTENT_WRITE |
| U04 | 수정 | 고객 문의 CS-190의 담당자를 이지훈으로 변경해줘 | inquiry/update | inquiry_id=CS-190 (USER_EXPLICIT); assignee=이지훈 (USER_EXPLICIT); risk=IDEMPOTENT_WRITE/NON_IDEMPOTENT_WRITE |
| E01 | 문자열 반환 | 'QA_PASS_2026' 문자열을 한 글자도 바꾸지 말고 돌려줘 | text/echo | text=QA_PASS_2026 (USER_EXPLICIT); risk=READ_ONLY |
| E02 | 문자열 반환 | 괄호 안의 내용까지 포함해서 '(검수 대기)'를 그대로 출력해줘 | text/echo | text=(검수 대기) (USER_EXPLICIT); risk=READ_ONLY |
| E03 | 문자열 반환 | 다음 문장을 수정 없이 반복해줘: 오늘 점검은 14:30에 시작합니다. | text/echo | text=오늘 점검은 14:30에 시작합니다. (USER_EXPLICIT); risk=READ_ONLY |
| E04 | 문자열 반환 | 문자열 '문서-A/B-03'을 그대로 반환해줘 | text/echo | text=문서-A/B-03 (USER_EXPLICIT); risk=READ_ONLY |
| A01 | 불명확 | 지난번에 이야기한 그 항목도 바꿔줘 | unknown/update | risk=UNKNOWN; 명확화 필수 |
| A02 | 불명확 | 이어서 처리해줘 | unknown/continue | risk=UNKNOWN; 명확화 필수 |

## 검토 시 반드시 확인할 판정 기준

1. **의도:** 단순 키워드가 아니라 업무 대상과 동작 일치 여부. 동등한 자연어·영어 의도 허용하되 생성/조회/수정 혼동 금지.
2. **업무 기능:** 요구하는 비즈니스 도메인·동작의 의미 동등성 확인. 실제 MCP Tool ID로 강제 매핑하거나 Tool 존재 여부를 추정하지 않음.
3. **엔터티·출처:** 표의 핵심값과 `USER_EXPLICIT` 출처 일치. 엔터티의 역할은 의미에 따라 판단하되 누락·가공·발명 허용 금지.
4. **날짜·시간:** R01 문서 날짜, R04 기간의 양쪽 경계, R06 월 전체, C02 마감 일시(등록 실행 예약 시각이 아님)까지 확인. E03의 `14:30`은 인용문 내부 문자열이지 작업 예약 시각이 아님.
5. **위험:** 조회·원문 반복은 READ_ONLY, 등록·수정은 적절한 쓰기 위험도. 위험도를 낮춰서 통과시키지 않음.
6. **모호성:** A01/A02는 외부 맥락 부재로 대상 식별 불가. capability를 임의로 발명하지 않고 `needs_clarification=true`; intent는 비어 있으면 안 됨.
7. **구조:** StructuredRequestV1 유효. 필수 필드 누락이나 출력 오류는 실패이며 20개 전체 분모 유지.
8. **판정:** 각 문항 필수 검사 전부 성공 시 1건 성공. 성공 17건 이상이면 목표 85% 충족; 분석범위 밖의 Tool 검색/실행 성공을 포함하지 않음.

## 실제 모델 확인 전 독립 검토 기록 (미작성)

| 검토 단계 | 검토자 | 일자 | 판정 | 비고 |
|---|---|---|---|---|
| 1차 독립 검토 | **미지정** | 미기재 | PENDING | 문항 의미와 골드값 검토 |
| 2차 독립 검토 | **미지정** | 미기재 | PENDING | 1차 검토와 독립 검토 |
| 이견 조정 | 필요 시 별도 | 미기재 | PENDING | 결론 및 변경 이력 기록 |
| 과제 담당자 최종 정답셋 승인 | **미완료** | 미기재 | PENDING | 20건/판정기준/해시 최종 동결 |

**진행 게이트:** 독립 검토 → 이견 해결 → 정답셋 승인 PR/CI/머지 → v2 의미 평가기 검증 → 모델 단일 실측 → 증적 SHA-256 보존. 이 과정이 완료되기 전에는 v2를 공식 `MET` 근거로 제시하지 않는다.
