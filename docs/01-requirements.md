# MCPFlow 요구사항 정의서

> **MCPFlow - MCP-based AI Agent Automation Platform**

| 항목 | 내용 |
|---|---|
| 문서 ID | `MCPF-REQ-001` |
| 문서 버전 | `v0.2` |
| 상태 | Draft - 개발 기준 초안 |
| 기준 저장소 | `ramza2/mcp-flow` |
| 공식 과제명 | MCP 연계 업무 자동화 AI 에이전트 개발 |
| 개발 프로젝트명 | MCPFlow |
| 최종 수정일 | 2026-09-02 |

---

## 1. 문서 목적

본 문서는 MCPFlow의 제품 범위, 기능 요구사항, 비기능 요구사항, 업무 규칙 및 검증 기준을 정의한다. 이후 작성되는 기능정의서, 시스템 아키텍처, Agent/MCP 상세설계, 데이터 모델, API, UI/UX, 배포 및 시험 문서는 이 문서의 요구사항 ID를 공통 추적키로 사용한다.

본 문서는 다음 용도로 사용한다.

- 개발 범위의 포함·제외 판단
- Cursor Agents Window를 이용한 구현 작업의 공통 기준
- Figma Make 기반 UI/UX 설계의 기능 기준
- 기능, API, 데이터 모델 및 테스트케이스 간 추적성 확보
- 과제 성능지표 및 제출 산출물의 근거 자료

요구사항과 구현이 충돌할 경우 코드를 임의로 변경하지 않고, 영향받는 요구사항과 설계 문서를 먼저 식별하여 변경 여부를 검토한다.

---

## 2. 제품 목표와 범위

### 2.1 제품 목표

MCPFlow는 사용자의 자연어 업무 요청을 분석하여 등록된 MCP Tool 중 적절한 Tool을 선택하고, 실행 가능한 계획으로 변환하여 안전하게 수행하는 AI Agent 기반 업무 자동화 플랫폼이다.

플랫폼은 다음 목표를 갖는다.

1. 내부·외부 MCP Server 및 Tool을 하나의 관리체계에서 등록·검증·운영한다.
2. 자연어 요청에서 실행 의도를 분석하고 적절한 Tool과 입력값을 결정한다.
3. 단일 호출뿐 아니라 순차·병렬·조건·반복·재시도·승인대기를 포함한 복합 실행을 지원한다.
4. 예약, 승인, 권한, 감사, 실행이력 및 모니터링을 통해 실제 업무에 적용 가능한 운영 기능을 제공한다.
5. 외부 MCP 탐색 및 OpenAPI/Python 기반 Tool Factory를 통해 Tool 확장성을 확보한다.

### 2.2 시스템 범위

| 범위 | 포함 내용 |
|---|---|
| 사용자 기능 | 자연어 요청, 실행계획 확인, 실행, 결과 확인, 이력 조회, 예약 관리 |
| Agent 기능 | 요청 분석, Tool 후보 검색, Tool 선택, 파라미터 구성, 실행계획 생성, 결과 요약 |
| 실행 기능 | 단일, 순차, 병렬, 조건, 제한 반복, 재시도, 타임아웃, 취소, 승인대기 |
| MCP 관리 | Server 등록, 연결 검증, protocol version·capability discovery, Tool Discovery, Tool 호출, 상태 관리 |
| Workflow 관리 | 실행계획 및 재사용 가능한 Workflow의 작성, 검증, 버전, 실행 |
| 운영 기능 | 사용자·역할·권한, 승인, 예약, 실행이력, 감사로그, 대시보드, 알림 연계점 |
| 확장 기능 | 외부 MCP 후보 탐색·검토·등록, OpenAPI/Python 기반 Tool 생성·검증 |
| 배포 | Docker 및 Docker Compose 기반 설치·실행·업데이트 |

### 2.3 범위 제외

다음 항목은 현재 개발범위에 포함하지 않는다. 향후 요구가 확정되면 별도 변경요청으로 관리한다.

- 범용 RPA 편집기 및 데스크톱 화면 좌표 기반 자동화
- MCP와 무관한 모든 외부 SaaS의 개별 전용 커넥터 직접 개발
- 자체 범용 LLM의 사전학습 또는 대규모 파인튜닝
- 사용자가 작성한 임의 코드를 무제한 권한으로 실행하는 기능
- 멀티테넌트 SaaS 과금, 청구, 구독 및 마켓플레이스 정산
- Kubernetes를 필수 전제로 한 운영 기능
- 모바일 네이티브 애플리케이션

---

## 3. 요구사항 적용 원칙

### 3.1 우선순위

| 우선순위 | 의미 |
|---|---|
| Must | 제품 완료 및 과제 목표 달성에 필수 |
| Should | 운영 품질과 활용성 확보를 위해 필요하나 대체수단을 허용 |
| Could | 기본 범위 완료 후 적용 가능한 개선 기능 |

### 3.2 개발 증분

`개발 증분`은 구현 순서와 검증 단위를 의미하며 공식 과제의 단계 구분이 아니다.

| 증분 | 목표 |
|---|---|
| Foundation | 공통 기반, 인증·권한, MCP Server/Tool 등록 및 단일 Tool 실행 |
| Intelligence | 자연어 분석, Tool 선택, 파라미터 구성, 실행계획 생성 |
| Orchestration | 순차·병렬·조건·반복·재시도 및 Workflow 실행 |
| Operation | 예약, 승인, 감사, 운영 대시보드 및 시험지표 수집 |
| Extension | 외부 MCP 탐색, OpenAPI/Python Tool Factory, 시범운영 보완 |

### 3.3 요구사항 상태

| 상태 | 의미 |
|---|---|
| Proposed | 검토 전 제안 |
| Approved | 구현 기준으로 승인 |
| Implemented | 구현 완료 |
| Verified | 시험으로 검증 완료 |
| Deferred | 현재 범위에서 보류 |

본 문서 `v0.1`의 모든 요구사항 상태는 별도 표기가 없으면 `Proposed`이다.

---

## 4. 용어 정의

| 용어 | 정의 |
|---|---|
| MCP Server | MCP 규격에 따라 Tool 등의 capability를 제공하는 서버 또는 로컬 프로세스 |
| MCP Tool | MCP Server가 공개하며 정형 입력을 받아 작업을 수행하는 호출 단위 |
| MCP Manager | Server 연결, protocol version·capability discovery/협상, Tool Discovery 및 호출을 담당하는 MCPFlow 모듈 |
| Agent | 자연어 요청을 분석하고 사용 가능한 Tool을 바탕으로 실행계획을 생성하는 논리적 실행 주체 |
| Agent Runtime | Agent 설정, 프롬프트, LLM 호출, Tool 후보 탐색 및 계획 생성을 담당하는 모듈 |
| Execution Plan | 실행할 Step, 의존관계, 입력 바인딩, 조건, 정책을 포함한 실행 명세 |
| Execution | 특정 버전의 실행계획을 실제 수행한 인스턴스 |
| Execution Step | 실행계획을 구성하는 Tool 호출, 조건, 병렬 그룹, 승인대기 등의 최소 실행 단위 |
| Workflow | 검증 후 저장하여 재사용할 수 있는 실행계획 템플릿 |
| Tool Factory | OpenAPI 명세 또는 제한된 Python 구현을 기반으로 MCP Tool을 생성·검증·패키징하는 기능 |
| 승인 | 위험하거나 정책상 보호된 Step을 실행하기 전에 권한자가 허용 또는 거절하는 절차 |
| 감사로그 | 누가, 언제, 어떤 자원에 어떤 행위를 수행했는지 변경 불가능한 형태로 추적하는 기록 |
| 외부 MCP 탐색 | 신뢰 가능한 Registry 또는 사용자가 지정한 출처에서 MCP Server 후보 정보를 조회하는 기능 |

---

## 5. 사용자 및 권한 주체

| 역할 | 주요 책임 | 기본 권한 범위 |
|---|---|---|
| System Administrator | 시스템 설정, 사용자·역할, 보안, 전체 자원 관리 | 전체 관리 |
| MCP Administrator | MCP Server/Tool 등록, 검증, 활성화 및 비활성화 | MCP 관리 |
| Agent Designer | Agent와 Workflow 정의, Tool 허용범위 및 정책 설정 | Agent/Workflow 관리 |
| Operator | 실행상태 확인, 실패 분석, 허용된 재시도·취소 | 운영 관리 |
| Approver | 배정된 승인 요청 검토 및 승인·거절 | 승인 처리 |
| User | 자연어 요청, 허용된 Agent/Workflow 실행, 본인 이력 조회 | 일반 사용 |
| Auditor | 실행이력과 감사로그의 읽기 전용 조회·내보내기 | 감사 조회 |

한 사용자는 여러 역할을 가질 수 있으며, 실제 권한은 역할에 연결된 Permission의 합집합에서 자원 범위와 정책 제한을 적용한 결과로 결정한다.

---

## 6. 대표 업무 시나리오

### UC-01. MCP Server 등록 및 Tool 동기화

1. MCP 관리자가 Server 연결정보와 인증정보 참조값을 입력한다.
2. 시스템이 연결 후 Current MCP의 `server/discover` 또는 legacy handshake로 protocol version과 capability를 확인한다.
3. 시스템이 Tool 목록과 스키마를 조회한다.
4. 관리자가 변경내역을 확인하고 Tool을 활성화한다.
5. 등록·검증·활성화 행위가 감사로그에 기록된다.

### UC-02. 자연어 요청 기반 단일 Tool 실행

1. 사용자가 Agent 화면에서 자연어로 업무를 요청한다.
2. Agent가 요청 의도와 사용자에게 허용된 Tool 후보를 분석한다.
3. Agent가 Tool과 파라미터를 선택하여 실행계획을 생성한다.
4. 정책상 확인이 필요하면 사용자에게 계획 또는 누락 파라미터를 확인받는다.
5. 실행엔진이 Tool을 호출하고 결과를 검증한다.
6. 사용자에게 결과와 실행 근거를 제공하고 이력을 저장한다.

### UC-03. 복합 Workflow 실행

1. Agent 또는 사용자가 다중 Step 실행계획을 구성한다.
2. 시스템이 Step 간 의존성, 입력 바인딩, 조건 및 권한을 사전 검증한다.
3. 실행엔진이 순차·병렬·조건·반복 정책에 따라 Step을 수행한다.
4. 실패 시 설정된 재시도 또는 중단 정책을 적용한다.
5. 전체 및 Step별 상태·입출력·소요시간을 저장한다.

### UC-04. 승인 후 실행 재개

1. 실행엔진이 승인 필요 Step 직전에 실행을 `WAITING_APPROVAL` 상태로 전환한다.
2. 승인자에게 요청 목적, Tool, 입력값, 예상 영향 및 선행 결과를 제공한다.
3. 승인 시 동일 실행 인스턴스가 재개되고, 거절·만료 시 정책에 따라 종료한다.
4. 승인 판단과 이후 실행 결과를 감사로그에 연결한다.

### UC-05. 예약 실행

1. 사용자가 허용된 Agent 또는 Workflow와 실행 입력, 일정, 시간대를 지정한다.
2. 시스템이 권한과 실행 가능성을 검증한 후 예약을 활성화한다.
3. 예약 시점마다 별도의 Execution을 생성하여 실행한다.
4. 실패, 중복 실행 및 장기 실행은 예약 정책에 따라 처리한다.

### UC-06. Tool Factory를 통한 Tool 확장

1. 개발자 또는 MCP 관리자가 OpenAPI 명세나 허용된 Python 소스를 입력한다.
2. 시스템이 구조와 보안정책을 검증하고 생성 후보를 미리 보여준다.
3. Tool 코드를 격리된 환경에서 빌드·시험한다.
4. 검토 승인 후 생성 산출물을 버전화하여 등록한다.

---

## 7. 기능 요구사항

### 7.1 공통 기반

| ID | 요구사항 | 우선순위 | 개발 증분 | 수용기준 |
|---|---|---:|---|---|
| REQ-CORE-001 | 시스템은 Web UI와 Backend API를 분리하여 제공해야 한다. | Must | Foundation | UI가 공개 API 계약을 통해 기능을 호출하며 서버 내부 구현에 직접 의존하지 않는다. |
| REQ-CORE-002 | 모든 업무 자원은 시스템이 생성한 전역 고유 식별자를 가져야 한다. | Must | Foundation | 사용자, Server, Tool, Agent, Workflow, Execution 및 승인 건을 ID로 단일 조회할 수 있다. |
| REQ-CORE-003 | 목록 API는 검색, 필터, 정렬 및 페이지네이션을 일관된 형식으로 제공해야 한다. | Must | Foundation | 주요 관리 목록에서 조건 조회와 다음 페이지 이동이 가능하고 전체 건수를 확인할 수 있다. |
| REQ-CORE-004 | 생성·변경 API는 입력 스키마를 검증하고 구조화된 오류를 반환해야 한다. | Must | Foundation | 필드별 오류코드·메시지·추적 ID를 반환하며 잘못된 입력으로 데이터가 일부 저장되지 않는다. |
| REQ-CORE-005 | 시스템 시각은 UTC로 저장하고 사용자 화면에서 설정된 시간대로 표시해야 한다. | Must | Foundation | 동일 이벤트가 API에서는 표준시각으로, UI에서는 사용자 시간대로 일관되게 표시된다. |
| REQ-CORE-006 | 삭제가 이력과 참조 무결성을 훼손하는 자원은 비활성화 또는 소프트 삭제해야 한다. | Must | Foundation | 실행에서 참조한 Tool·Agent·Workflow를 삭제해도 과거 실행을 재현·조회할 수 있다. |
| REQ-CORE-007 | 모든 장기 작업은 요청 처리와 분리된 Job으로 실행하고 상태 조회를 제공해야 한다. | Should | Foundation | Discovery, 생성, 동기화 등 장기 작업의 진행·성공·실패 상태와 오류를 조회할 수 있다. |
| REQ-CORE-008 | API의 중복 생성 위험 작업은 idempotency key 또는 동등한 중복 방지 방식을 지원해야 한다. | Must | Foundation | 동일 키로 재요청해도 중복 Execution·예약·승인 건이 생성되지 않는다. |

### 7.2 인증, 사용자 및 RBAC

| ID | 요구사항 | 우선순위 | 개발 증분 | 수용기준 |
|---|---|---:|---|---|
| REQ-AUTH-001 | 시스템은 인증된 사용자만 보호된 기능에 접근하도록 해야 한다. | Must | Foundation | 미인증 요청은 표준 인증 오류로 거절되고 보호 데이터가 반환되지 않는다. |
| REQ-AUTH-002 | 관리자는 사용자를 생성·조회·변경·비활성화할 수 있어야 한다. | Must | Foundation | 비활성 사용자는 신규 로그인과 실행을 할 수 없고 기존 이력은 유지된다. |
| REQ-AUTH-003 | 관리자는 역할과 Permission을 관리하고 사용자에게 하나 이상의 역할을 부여할 수 있어야 한다. | Must | Foundation | 역할 변경 후 신규 요청부터 변경된 권한이 적용된다. |
| REQ-AUTH-004 | 시스템은 화면 노출뿐 아니라 모든 Backend API에서 권한을 검증해야 한다. | Must | Foundation | URL 직접 호출 또는 변조된 UI 요청도 권한이 없으면 거절된다. |
| REQ-AUTH-005 | Agent, Workflow, MCP Server 및 Tool 단위의 사용 가능 범위를 설정할 수 있어야 한다. | Must | Intelligence | 권한이 없는 자원은 후보 검색·실행·직접 API 호출에서 모두 제외 또는 거절된다. |
| REQ-AUTH-006 | 사용자는 본인 실행이력을 조회하고, 운영·감사 역할은 권한 범위 내 타 사용자 이력을 조회할 수 있어야 한다. | Must | Operation | 역할별 테스트에서 허용된 범위 밖의 이력은 노출되지 않는다. |
| REQ-AUTH-007 | 관리자 권한, 승인 권한 및 감사 조회 권한은 독립 Permission으로 분리해야 한다. | Must | Operation | 한 역할에 전체 관리자 권한을 주지 않고 승인 또는 감사 권한만 부여할 수 있다. |
| REQ-AUTH-008 | 인증 실패, 권한 거부 및 중요 권한 변경은 보안 감사 이벤트로 기록해야 한다. | Must | Operation | 행위자, 대상, 결과, 시각, 요청 추적 ID를 조회할 수 있다. |

### 7.3 MCP Server 관리

| ID | 요구사항 | 우선순위 | 개발 증분 | 수용기준 |
|---|---|---:|---|---|
| REQ-MCP-001 | MCP 관리자는 MCP Server의 이름, 설명, 전송방식, 연결정보 및 운영상태를 등록·조회·변경할 수 있어야 한다. | Must | Foundation | 필수값 검증 후 Server가 저장되고 목록·상세 화면에서 확인된다. |
| REQ-MCP-002 | 시스템은 로컬 프로세스 연계용 `stdio`와 원격 연계용 `Streamable HTTP` 전송방식을 지원해야 한다. | Must | Foundation | 각 전송방식의 시험 Server에서 protocol discovery/협상 및 Tool 호출이 성공한다. |
| REQ-MCP-003 | 구형 HTTP+SSE 연계는 필요 시 호환 어댑터로 제공하고 핵심 도메인 로직과 분리해야 한다. | Could | Extension | 어댑터 비활성화가 표준 전송방식에 영향을 주지 않는다. |
| REQ-MCP-004 | 시스템은 Server 연결 시 Current MCP의 `server/discover` 또는 legacy handshake로 protocol version과 capability를 확인·협상하고 결과를 저장해야 한다. | Must | Foundation | 선택된 protocol 정보, capability 및 protocol era를 Server 상세에서 확인할 수 있다. |
| REQ-MCP-005 | 시스템은 연결 시험 기능을 제공하고 DNS, 네트워크, 인증, protocol 및 timeout 오류를 구분해야 한다. | Must | Foundation | 실패 원인이 분류된 오류코드와 운영자가 이해할 수 있는 메시지로 표시된다. |
| REQ-MCP-006 | 인증정보와 비밀값은 자원 메타데이터와 분리하여 secret 참조로 관리해야 한다. | Must | Foundation | 조회 API·로그·화면에 원문 secret이 반환되지 않는다. |
| REQ-MCP-007 | Server를 `DRAFT`, `ACTIVE`, `INACTIVE`, `ERROR` 상태로 관리하고 `ACTIVE` 상태만 실행에 사용해야 한다. | Must | Foundation | 비활성 또는 오류 Server의 Tool이 신규 실행 후보에서 제외된다. |
| REQ-MCP-008 | 시스템은 Server별 연결 및 Tool 호출 timeout, 재시도, 동시실행 제한을 설정할 수 있어야 한다. | Must | Orchestration | Server 정책을 변경하면 이후 실행부터 적용되고 정책값이 실행이력에 남는다. |
| REQ-MCP-009 | 시스템은 Server 상태를 수동 또는 주기적으로 점검하고 마지막 성공·실패 시각과 지연시간을 기록해야 한다. | Should | Operation | 상태 점검 결과와 최근 오류를 관리 화면에서 확인할 수 있다. |
| REQ-MCP-010 | Server 변경·활성화·비활성화·삭제 시 영향받는 Tool, Agent, Workflow를 사전에 보여줘야 한다. | Must | Operation | 참조 중인 자원의 영향목록 확인 없이 파괴적 변경을 완료할 수 없다. |
| REQ-MCP-011 | 원격 Server 주소는 보안정책에 따른 허용 protocol, host 및 네트워크 범위를 검증해야 한다. | Must | Foundation | 금지된 scheme, loopback·내부주소 또는 미허용 host 접근이 정책에 따라 차단된다. |
| REQ-MCP-012 | 로컬 `stdio` Server는 사전 승인된 실행파일·이미지·인자 정책 안에서만 기동해야 한다. | Must | Foundation | 임의 shell 문자열이나 미허용 경로를 Server 설정만으로 실행할 수 없다. |

### 7.4 MCP Tool Discovery 및 Registry

| ID | 요구사항 | 우선순위 | 개발 증분 | 수용기준 |
|---|---|---:|---|---|
| REQ-TOOL-001 | 시스템은 연결된 Server에서 Tool 목록을 Discovery하여 로컬 Registry에 동기화해야 한다. | Must | Foundation | 시험 Server의 Tool 이름·설명·입력 스키마가 누락 없이 등록된다. |
| REQ-TOOL-002 | Tool 메타데이터는 Server, 이름, 설명, 입력 스키마, 출력 스키마 또는 결과형식, annotation 및 원본 정보를 보존해야 한다. | Must | Foundation | Discovery 응답과 저장된 메타데이터를 비교했을 때 의미 있는 정보 손실이 없다. |
| REQ-TOOL-003 | 시스템은 Tool 메타데이터의 해시 또는 버전을 관리하고 재동기화 시 추가·변경·삭제 후보를 식별해야 한다. | Must | Foundation | 동기화 미리보기에서 변경유형과 이전·신규 값의 차이를 확인할 수 있다. |
| REQ-TOOL-004 | Server에서 사라진 Tool은 즉시 물리 삭제하지 않고 사용중지 상태로 전환해야 한다. | Must | Foundation | 과거 실행은 기존 Tool 스냅샷을 계속 조회할 수 있고 신규 실행만 차단된다. |
| REQ-TOOL-005 | 관리자는 Discovery된 Tool을 개별 활성화·비활성화하고 사용자용 이름·설명·태그를 보완할 수 있어야 한다. | Must | Foundation | 원본 메타데이터와 운영자 보완 메타데이터가 구분되어 저장된다. |
| REQ-TOOL-006 | Tool 입력 스키마는 저장 및 호출 전에 유효성을 검사해야 한다. | Must | Foundation | 유효하지 않은 스키마는 활성화되지 않고 검증 오류가 표시된다. |
| REQ-TOOL-007 | Tool별 위험등급, 승인 필요 여부, timeout, 재시도, 결과크기 제한 및 사용 권한을 설정할 수 있어야 한다. | Must | Orchestration | 실행 시 Tool 정책이 일관되게 적용되고 정책 스냅샷이 기록된다. |
| REQ-TOOL-008 | Tool annotation은 위험 판단의 참고정보로 사용하되 자체 보안정책을 대체해서는 안 된다. | Must | Orchestration | annotation이 안전을 나타내도 관리자 정책상 승인 대상이면 승인 없이 실행되지 않는다. |
| REQ-TOOL-009 | Tool 목록은 이름, 설명, 태그, Server, 상태 및 위험등급으로 검색·필터할 수 있어야 한다. | Must | Foundation | 각 조건의 단독·복합 검색결과가 일관된다. |
| REQ-TOOL-010 | 관리자는 Tool 상세에서 입력 예시와 별도 테스트 입력으로 시험 호출할 수 있어야 한다. | Should | Foundation | 시험 호출은 일반 실행과 구분되어 기록되고 결과·오류·소요시간을 확인할 수 있다. |
| REQ-TOOL-011 | Tool 시험 호출에도 사용자 권한, secret 보호, timeout, 결과 제한 및 감사정책을 동일하게 적용해야 한다. | Must | Foundation | 관리 화면의 시험 기능으로 운영 정책을 우회할 수 없다. |
| REQ-TOOL-012 | 연계·검증 완료 Tool을 식별할 수 있는 검증상태와 증빙정보를 관리해야 한다. | Must | Operation | 검증일, 검증자, 시험결과 및 대상 버전을 바탕으로 완료 Tool 목록을 산출할 수 있다. |

### 7.5 Agent 및 자연어 요청 분석

| ID | 요구사항 | 우선순위 | 개발 증분 | 수용기준 |
|---|---|---:|---|---|
| REQ-AGT-001 | Agent Designer는 Agent의 이름, 목적, 지침, 사용 모델, 허용 Tool, 실행정책을 등록·변경·버전화할 수 있어야 한다. | Must | Intelligence | 실행이 어느 Agent 버전을 사용했는지 이력에서 확인할 수 있다. |
| REQ-AGT-002 | Agent는 현재 사용자의 권한과 Agent의 Tool allowlist를 모두 만족하는 Tool만 후보로 사용해야 한다. | Must | Intelligence | 프롬프트 조작으로 미허용 Tool을 선택하거나 호출할 수 없다. |
| REQ-AGT-003 | 시스템은 자연어 요청에서 업무 목적, 제약조건, 주요 엔터티, 필요한 입력 및 기대 결과를 구조화해야 한다. | Must | Intelligence | 정의된 테스트셋에서 분석결과가 지정된 구조 스키마를 충족한다. |
| REQ-AGT-004 | 시스템은 Tool 이름뿐 아니라 설명, 태그, 입력 스키마, Agent 목적을 이용해 후보 Tool을 검색해야 한다. | Must | Intelligence | 동의어나 업무 표현을 사용한 요청에도 정답 Tool이 후보군에 포함된다. |
| REQ-AGT-005 | 시스템은 후보별 선택 근거와 신뢰도를 산출하고 최종 선택 결과와 함께 저장해야 한다. | Must | Intelligence | 실행 상세에서 선택된 Tool, 주요 근거 및 후보평가 결과를 조회할 수 있다. |
| REQ-AGT-006 | 신뢰도가 정책 기준 미만이거나 복수 후보가 경합하면 임의 실행하지 않고 사용자 확인을 요청해야 한다. | Must | Intelligence | 모호성 테스트에서 확인 요청 상태로 전환되고 Tool이 호출되지 않는다. |
| REQ-AGT-007 | 필수 파라미터가 부족하면 사용자에게 필요한 값만 구조적으로 요청하고 응답 후 계획을 계속 생성해야 한다. | Must | Intelligence | 누락값 보완 전 실행이 시작되지 않으며 보완된 값이 스키마 검증을 통과한다. |
| REQ-AGT-008 | Tool 입력 파라미터는 사용자 입력, 대화 맥락, 이전 Step 출력 및 정책 기본값에서 출처를 추적할 수 있어야 한다. | Must | Intelligence | 실행 상세에서 주요 파라미터의 값과 출처 유형을 확인할 수 있다. |
| REQ-AGT-009 | LLM 출력은 정의된 구조 스키마로 검증하고 파싱 실패 시 제한된 보정 또는 재요청 후 안전하게 실패해야 한다. | Must | Intelligence | 비정형·손상 응답이 실행엔진에 직접 전달되지 않는다. |
| REQ-AGT-010 | LLM Provider와 모델은 환경 및 Agent 설정으로 교체 가능하며 OpenAI-compatible API를 기본 연계방식으로 지원해야 한다. | Must | Intelligence | 동일 Agent가 허용된 다른 모델 설정으로 실행되고 Provider별 설정이 코드에 하드코딩되지 않는다. |
| REQ-AGT-011 | Agent는 Tool 실행결과를 근거로 최종 사용자 응답을 작성하고 성공·부분성공·실패를 구분해야 한다. | Must | Intelligence | 응답에서 실행되지 않은 작업을 성공했다고 표현하지 않고 Step 결과와 상태가 일치한다. |
| REQ-AGT-012 | 외부 Tool 결과는 신뢰할 수 없는 입력으로 취급하고 시스템 지침이나 권한정책을 변경하는 명령으로 해석하지 않아야 한다. | Must | Intelligence | Tool 결과의 프롬프트 인젝션 문자열이 추가 Tool 권한이나 정책 우회를 발생시키지 않는다. |
| REQ-AGT-013 | Agent의 planning 횟수, LLM 호출 횟수, 입력·출력 크기 및 최대 실행시간을 정책으로 제한해야 한다. | Must | Intelligence | 설정된 한도 도달 시 종료사유가 명확한 실패 또는 사용자 확인 상태가 된다. |
| REQ-AGT-014 | 자연어 요청과 정답 Tool을 연결한 평가 데이터셋을 버전 관리하고 Tool 매핑 평가를 반복 실행할 수 있어야 한다. | Must | Operation | 동일 데이터셋·모델·설정으로 평가결과를 재산출하고 비교할 수 있다. |

### 7.6 실행계획 및 Workflow

| ID | 요구사항 | 우선순위 | 개발 증분 | 수용기준 |
|---|---|---:|---|---|
| REQ-WF-001 | 시스템은 실행 전 Step, 의존관계, 입력 바인딩, 조건, 오류정책을 포함한 Execution Plan을 생성해야 한다. | Must | Orchestration | 모든 실행이 검증된 계획 스냅샷을 참조한다. |
| REQ-WF-002 | Execution Plan은 JSON Schema 또는 동등한 기계 검증 가능한 명세로 정의해야 한다. | Must | Orchestration | 잘못된 Step 유형, 누락된 의존성 및 잘못된 바인딩이 실행 전에 거절된다. |
| REQ-WF-003 | 계획은 순차 실행을 표현할 수 있어야 한다. | Must | Orchestration | 선행 Step 성공 후 후행 Step이 시작되고 출력값을 입력으로 전달할 수 있다. |
| REQ-WF-004 | 계획은 상호 의존성이 없는 Step의 병렬 실행을 표현할 수 있어야 한다. | Must | Orchestration | 병렬 그룹이 설정된 동시실행 한도 안에서 실행되고 모든 결과를 수집한다. |
| REQ-WF-005 | 계획은 이전 결과 또는 검증된 입력값에 따른 조건 분기를 표현할 수 있어야 한다. | Must | Orchestration | 조건식 결과에 따라 하나의 유효 경로만 실행되고 미선택 Step은 `SKIPPED`로 기록된다. |
| REQ-WF-006 | 계획은 최대 횟수와 종료조건이 명시된 제한 반복을 표현할 수 있어야 한다. | Must | Orchestration | 최대 횟수 없는 반복은 저장·실행되지 않으며 한도 도달 사유가 기록된다. |
| REQ-WF-007 | 계획은 승인대기 Step을 표현하고 승인 결과에 따른 후속 경로를 지정할 수 있어야 한다. | Must | Operation | 승인·거절·만료 각각의 전이가 계획대로 수행된다. |
| REQ-WF-008 | Step 간 데이터 바인딩은 명시적 경로와 타입 검증을 사용해야 한다. | Must | Orchestration | 존재하지 않는 출력 경로나 타입 불일치는 실행 전 또는 해당 Step 시작 전에 검출된다. |
| REQ-WF-009 | 시스템은 순환 의존성, 도달 불가능 Step, 무한 반복 가능성 및 권한 부족을 사전 검증해야 한다. | Must | Orchestration | 유효하지 않은 계획은 실행 ID를 생성하기 전에 오류목록과 함께 거절된다. |
| REQ-WF-010 | 사용자는 실행 전 계획 요약과 영향도 높은 Tool·입력값을 확인할 수 있어야 한다. | Must | Intelligence | 정책상 사전확인 대상 계획은 사용자 확인 전 실행되지 않는다. |
| REQ-WF-011 | 검증된 Execution Plan을 재사용 가능한 Workflow로 저장·복제·변경·버전화할 수 있어야 한다. | Must | Orchestration | 게시된 Workflow 변경 시 새 버전이 생성되고 기존 실행은 이전 버전을 유지한다. |
| REQ-WF-012 | Workflow는 `DRAFT`, `PUBLISHED`, `DEPRECATED` 상태를 가져야 하며 게시 버전만 일반 실행에 사용해야 한다. | Must | Orchestration | 초안·폐기 버전은 권한 없는 일반 사용자의 실행 후보에 나타나지 않는다. |
| REQ-WF-013 | Workflow 입력은 이름, 타입, 필수 여부, 기본값, 비밀 여부 및 설명을 정의할 수 있어야 한다. | Must | Orchestration | 실행 전 입력 폼과 API 검증이 동일한 입력 정의를 사용한다. |
| REQ-WF-014 | Agent가 생성한 계획과 사람이 작성한 Workflow는 동일한 실행엔진 계약으로 실행해야 한다. | Must | Orchestration | 두 출처의 계획이 동일한 검증·상태·로그·정책 처리를 거친다. |

### 7.7 Execution Engine

| ID | 요구사항 | 우선순위 | 개발 증분 | 수용기준 |
|---|---|---:|---|---|
| REQ-EXE-001 | 시스템은 단일 MCP Tool을 실행하고 입력, 출력, 상태, 오류 및 소요시간을 저장해야 한다. | Must | Foundation | 테스트 Tool 호출 결과와 실행이력이 일치한다. |
| REQ-EXE-002 | Execution은 최소 `QUEUED`, `PLANNING`, `WAITING_CONFIRMATION`, `WAITING_APPROVAL`, `RUNNING`, `SUCCEEDED`, `PARTIAL`, `FAILED`, `CANCELLED`, `TIMED_OUT` 상태를 구분해야 한다. | Must | Orchestration | 허용되지 않은 상태전이가 거절되고 모든 종료상태에 종료사유가 존재한다. |
| REQ-EXE-003 | Step은 최소 `PENDING`, `READY`, `RUNNING`, `SUCCEEDED`, `FAILED`, `SKIPPED`, `CANCELLED`, `TIMED_OUT` 상태를 구분해야 한다. | Must | Orchestration | 실행 상세에서 전체 상태와 Step 상태의 정합성을 확인할 수 있다. |
| REQ-EXE-004 | 실행엔진은 검증 완료된 계획만 실행하고 실행 중 계획 스냅샷을 변경하지 않아야 한다. | Must | Orchestration | Agent·Workflow 설정을 변경해도 진행 중 실행의 계획은 바뀌지 않는다. |
| REQ-EXE-005 | 각 Tool 호출 직전에 사용자·Agent·Tool 권한과 정책을 재검증해야 한다. | Must | Orchestration | 계획 생성 후 권한이 회수되면 해당 Tool 호출이 시작되지 않는다. |
| REQ-EXE-006 | Step별 timeout과 전체 Execution timeout을 적용해야 한다. | Must | Orchestration | timeout 후 상태가 종료되고 후속 Step 처리방식이 오류정책에 따른다. |
| REQ-EXE-007 | 재시도 가능 오류에 대해 최대 횟수, 지연 및 backoff 정책을 적용할 수 있어야 한다. | Must | Orchestration | 재시도 횟수와 각 시도의 오류·시간이 이력에 남고 한도를 초과하지 않는다. |
| REQ-EXE-008 | 부작용 가능 Tool은 기본적으로 자동 재시도하지 않으며 명시적 idempotency 보장이 있을 때만 허용해야 한다. | Must | Orchestration | 정책이 없는 쓰기성 Tool은 네트워크 오류 후 중복 호출되지 않는다. |
| REQ-EXE-009 | 사용자는 권한 범위 내 대기·실행 중인 Execution의 취소를 요청할 수 있어야 한다. | Must | Orchestration | 취소 요청 후 신규 Step이 시작되지 않고 취소 가능 호출에는 취소를 전달한다. |
| REQ-EXE-010 | 실행엔진은 프로세스 재시작 후에도 대기·예약·승인 및 복구 가능한 실행상태를 복원해야 한다. | Must | Operation | 비정상 종료 복구 시험에서 실행이 정책에 따라 재개되거나 명시적으로 실패 처리된다. |
| REQ-EXE-011 | 동시실행 수를 시스템, 사용자, Server 및 Tool 단위로 제한할 수 있어야 한다. | Must | Orchestration | 한도 초과 작업은 유실되지 않고 Queue 대기 또는 제한 오류로 처리된다. |
| REQ-EXE-012 | 대용량 Tool 결과는 설정된 크기 제한에 따라 저장소 분리, 요약 또는 잘라내기를 적용하고 원본 보존 여부를 기록해야 한다. | Should | Operation | API·LLM context·DB에 무제한 결과가 적재되지 않으며 처리방식이 이력에 표시된다. |
| REQ-EXE-013 | 민감 파라미터와 결과 필드는 저장·표시·로그 단계에서 masking 또는 별도 보호정책을 적용해야 한다. | Must | Foundation | secret·token·비밀번호 필드가 평문 로그와 일반 조회 API에 노출되지 않는다. |
| REQ-EXE-014 | 각 Execution과 Step에 상관관계 추적 ID를 부여해야 한다. | Must | Foundation | API 요청, Agent 호출, Tool 호출 및 로그를 하나의 실행 단위로 연결할 수 있다. |
| REQ-EXE-015 | 사용자는 Execution 목록 및 상세에서 요청, 계획, Step 타임라인, 상태, 입출력 요약, 오류 및 최종 결과를 확인할 수 있어야 한다. | Must | Operation | 대표 시나리오의 전체 진행과 실패지점을 UI에서 식별할 수 있다. |
| REQ-EXE-016 | 실패한 Execution은 원본 계획과 입력을 보존한 새 Execution으로 재실행할 수 있어야 한다. | Should | Operation | 재실행은 새 ID를 가지며 원본 실행과 연결되고 현재 권한·정책을 다시 검증한다. |
| REQ-EXE-017 | 부분 성공 판단기준과 후속처리 정책을 Workflow에서 정의할 수 있어야 한다. | Must | Orchestration | 선택적 Step 실패 시 계획에 따라 `PARTIAL` 또는 `FAILED`로 일관되게 종료된다. |
| REQ-EXE-018 | 실행결과 검증은 MCP 호출 성공 여부와 업무 결과 유효성을 구분해야 한다. | Must | Orchestration | HTTP/protocol 성공이더라도 출력 스키마나 검증규칙 불일치 시 업무 성공으로 처리되지 않는다. |

### 7.8 승인 관리

| ID | 요구사항 | 우선순위 | 개발 증분 | 수용기준 |
|---|---|---:|---|---|
| REQ-APR-001 | Tool 또는 Workflow 정책에 따라 실행 전 승인 요청을 자동 생성해야 한다. | Must | Operation | 승인 대상 Step은 승인 건 없이 실행되지 않는다. |
| REQ-APR-002 | 승인 요청에는 요청자, 목적, Agent/Workflow, Tool, 입력값 요약, 예상 영향, 선행결과 및 만료시각이 포함되어야 한다. | Must | Operation | 승인자가 실행 영향을 판단하는 데 필요한 정보를 한 화면에서 확인할 수 있다. |
| REQ-APR-003 | 승인자는 권한과 배정범위 내 요청만 승인·거절할 수 있어야 한다. | Must | Operation | 요청자 본인 승인 금지 등 설정된 분리정책을 우회할 수 없다. |
| REQ-APR-004 | 승인, 거절, 회수 및 만료 상태를 구분하고 의견을 기록해야 한다. | Must | Operation | 상태, 처리자, 처리시각, 의견이 Execution과 감사로그에 연결된다. |
| REQ-APR-005 | 승인 시 승인받은 Tool·입력·정책 스냅샷과 실제 실행값이 동일한지 재검증해야 한다. | Must | Operation | 승인 이후 입력값이 변경되면 기존 승인을 사용할 수 없다. |
| REQ-APR-006 | 승인 요청은 만료시간을 가져야 하며 만료 후 자동 실행되지 않아야 한다. | Must | Operation | 만료 건은 실행 재개가 차단되고 재요청 여부가 정책에 따라 처리된다. |
| REQ-APR-007 | 동일 Step의 중복 승인 요청을 방지해야 한다. | Must | Operation | 재시도 또는 화면 중복 요청으로 열린 승인 건이 여러 개 생성되지 않는다. |
| REQ-APR-008 | 승인 대기 중인 실행은 시스템 재시작 후에도 복구되어야 한다. | Must | Operation | 재시작 전후 승인 건과 Execution 연결 및 상태가 유지된다. |
| REQ-APR-009 | 승인 처리 알림을 위한 내부 이벤트를 발행하고 알림 채널은 교체 가능하게 구성해야 한다. | Should | Operation | 알림 연계가 실패해도 승인 데이터가 유실되지 않고 재처리할 수 있다. |

### 7.9 예약 실행

| ID | 요구사항 | 우선순위 | 개발 증분 | 수용기준 |
|---|---|---:|---|---|
| REQ-SCH-001 | 사용자는 허용된 Agent 또는 게시된 Workflow의 일회성·반복 예약을 생성할 수 있어야 한다. | Must | Operation | 지정 시각에 별도 Execution이 생성되고 예약과 연결된다. |
| REQ-SCH-002 | 예약은 timezone, 시작·종료, 반복 규칙 및 실행 입력을 명시해야 한다. | Must | Operation | 시간대가 다른 환경에서도 동일한 사용자 의도로 다음 실행시각이 계산된다. |
| REQ-SCH-003 | 예약 생성·변경 시 Agent/Workflow 상태, 입력 스키마 및 사용자 권한을 검증해야 한다. | Must | Operation | 유효하지 않은 입력이나 권한 없는 자원으로 예약을 활성화할 수 없다. |
| REQ-SCH-004 | 예약을 활성화·일시정지·재개·종료할 수 있어야 한다. | Must | Operation | 일시정지 기간에는 신규 Execution이 생성되지 않고 이력은 유지된다. |
| REQ-SCH-005 | 동일 예약의 이전 실행이 종료되지 않은 경우 중복 허용, 건너뛰기, 대기 중 하나의 정책을 적용할 수 있어야 한다. | Must | Operation | 동시실행 정책에 따라 중복 실행이 일관되게 제어된다. |
| REQ-SCH-006 | 시스템 중단 중 놓친 실행에 대해 건너뛰기 또는 제한된 보충실행 정책을 적용할 수 있어야 한다. | Should | Operation | 복구 시 무제한 과거 실행이 한꺼번에 생성되지 않는다. |
| REQ-SCH-007 | 예약 실행 시점에 사용자, Agent, Workflow 및 Tool 권한을 다시 검증해야 한다. | Must | Operation | 예약 후 권한이 회수된 경우 실행이 시작되지 않고 사유가 기록된다. |
| REQ-SCH-008 | 예약별 다음 실행시각, 최근 실행결과, 실패횟수 및 상태를 조회할 수 있어야 한다. | Must | Operation | 예약 목록에서 운영자가 이상 예약을 식별할 수 있다. |
| REQ-SCH-009 | 반복 실패 시 자동 일시정지 또는 알림 이벤트 발생 기준을 설정할 수 있어야 한다. | Should | Operation | 설정 횟수 연속 실패 후 정책이 한 번만 적용되고 원인이 기록된다. |

### 7.10 실행이력, 감사 및 운영

| ID | 요구사항 | 우선순위 | 개발 증분 | 수용기준 |
|---|---|---:|---|---|
| REQ-OPS-001 | 대시보드는 Server/Tool 상태, 실행 건수, 성공·실패·대기 현황, 평균 또는 백분위 소요시간, 승인·예약 현황을 제공해야 한다. | Must | Operation | 기간 필터에 따라 집계값이 원본 실행이력과 일치한다. |
| REQ-OPS-002 | 운영자는 상태, 사용자, Agent, Workflow, Tool, 기간 및 오류유형으로 Execution을 검색할 수 있어야 한다. | Must | Operation | 복합 필터 결과와 내보내기 결과가 동일하다. |
| REQ-OPS-003 | 시스템은 실패 원인을 계획, 권한, 연결, timeout, Tool, 출력검증, 사용자취소 및 시스템 오류 등으로 분류해야 한다. | Must | Operation | 실패 Execution마다 하나 이상의 표준 오류분류와 추적 ID가 존재한다. |
| REQ-OPS-004 | 시스템은 운영자가 비밀값을 노출하지 않고 문제를 분석할 수 있는 구조화 로그를 제공해야 한다. | Must | Operation | 로그에 시각, 수준, 서비스, 추적 ID, 이벤트명, 결과가 포함되고 secret 검사가 통과한다. |
| REQ-OPS-005 | 주요 운영지표를 기계 수집 가능한 metric으로 제공해야 한다. | Must | Operation | 실행량, 지연, 오류, Queue, LLM, MCP 호출 및 Scheduler 지표를 수집할 수 있다. |
| REQ-OPS-006 | 시스템은 Backend, Database 및 의존서비스의 liveness/readiness 상태를 제공해야 한다. | Must | Foundation | Docker health check가 서비스 준비 전 트래픽을 허용하지 않는다. |
| REQ-OPS-007 | 관리자는 운영 설정을 조회·변경할 수 있고 변경 이력과 적용범위를 확인할 수 있어야 한다. | Must | Operation | 설정 변경 전후 값, 행위자, 적용시각이 감사로그에 기록된다. |
| REQ-OPS-008 | 실행 및 감사 데이터를 권한 범위 내에서 CSV 또는 JSON으로 내보낼 수 있어야 한다. | Should | Operation | 필터 결과와 내보낸 데이터의 건수·필드가 일치하고 민감정보 정책이 적용된다. |
| REQ-AUD-001 | 시스템은 로그인, 권한 변경, MCP/Tool/Agent/Workflow/예약/승인 변경 및 실행 행위를 감사로그에 기록해야 한다. | Must | Operation | 정의된 중요 이벤트 테스트에서 누락 없이 로그가 생성된다. |
| REQ-AUD-002 | 감사로그는 행위자, 행위, 대상, 결과, 시각, 요청 출처, 추적 ID 및 허용된 범위의 변경 전후 정보를 포함해야 한다. | Must | Operation | 감사 사건을 사용자 행위와 Execution까지 역추적할 수 있다. |
| REQ-AUD-003 | 일반 애플리케이션 기능으로 감사로그를 수정·삭제할 수 없어야 한다. | Must | Operation | UI와 일반 API에 감사로그 변경 기능이 없고 DB 권한도 분리된다. |
| REQ-AUD-004 | 감사로그 조회와 내보내기는 감사 Permission을 가진 사용자로 제한해야 한다. | Must | Operation | 권한 없는 사용자에게 레코드 존재 여부와 내용이 노출되지 않는다. |
| REQ-AUD-005 | 감사로그 보존기간과 파기정책을 설정할 수 있어야 한다. | Should | Operation | 설정된 보존정책의 실행결과와 파기 건수가 별도 감사 이벤트로 남는다. |

### 7.11 외부 MCP 탐색 및 도입

| ID | 요구사항 | 우선순위 | 개발 증분 | 수용기준 |
|---|---|---:|---|---|
| REQ-DISC-001 | 시스템은 신뢰 가능한 Registry 또는 사용자가 지정한 출처에서 MCP Server 후보를 검색할 수 있어야 한다. | Must | Extension | 키워드·분류 검색으로 후보 이름, 설명, 제공자, 출처 및 설치정보를 조회한다. |
| REQ-DISC-002 | 후보 정보의 출처, 조회시각, 버전, 라이선스, 배포방식 및 검증상태를 표시해야 한다. | Must | Extension | 관리자가 출처와 적용조건을 확인하지 못하는 후보는 자동 등록되지 않는다. |
| REQ-DISC-003 | 외부 후보 검색결과와 내부 등록 Server를 구분해야 한다. | Must | Extension | 검색결과를 조회한 것만으로 내부 실행 후보에 포함되지 않는다. |
| REQ-DISC-004 | 외부 Server 도입은 후보 선택, 보안검토, 연결검증, Tool 검토 및 관리자 활성화 절차를 거쳐야 한다. | Must | Extension | 검토 미완료 Server 또는 Tool을 일반 Agent가 사용할 수 없다. |
| REQ-DISC-005 | 외부 출처에서 받은 명령, 설명 및 설치 스크립트는 신뢰할 수 없는 입력으로 처리해야 한다. | Must | Extension | 검색결과만으로 shell 실행, secret 전달 또는 네트워크 허용목록 변경이 발생하지 않는다. |
| REQ-DISC-006 | 동일 후보의 버전 변경 및 출처 변경을 감지하고 재검토 필요 상태로 표시해야 한다. | Should | Extension | 변경된 후보가 기존 승인만으로 자동 업데이트되지 않는다. |
| REQ-DISC-007 | 도입 검토결과와 거절사유를 기록하여 동일 위험 후보를 식별할 수 있어야 한다. | Should | Extension | 후보 상세에서 검토 이력과 최종 결정자를 확인할 수 있다. |

### 7.12 OpenAPI/Python 기반 Tool Factory

| ID | 요구사항 | 우선순위 | 개발 증분 | 수용기준 |
|---|---|---:|---|---|
| REQ-FAC-001 | 사용자는 OpenAPI 문서 파일 또는 허용된 URL을 입력하여 Tool 생성 후보를 만들 수 있어야 한다. | Must | Extension | 유효한 명세를 분석하여 operation별 후보 목록을 생성한다. |
| REQ-FAC-002 | OpenAPI 문서의 형식, 참조, operationId, 입력·출력 스키마 및 Server URL을 생성 전에 검증해야 한다. | Must | Extension | 치명적 오류가 있는 명세는 코드 생성 단계로 진행되지 않는다. |
| REQ-FAC-003 | 사용자는 생성 대상 operation을 선택하고 Tool 이름·설명·태그·위험정책을 보완할 수 있어야 한다. | Must | Extension | 선택하지 않은 operation은 생성 산출물에 포함되지 않는다. |
| REQ-FAC-004 | OpenAPI 인증방식은 secret 참조 가능한 설정으로 매핑하고 생성 코드에 실제 credential을 포함하지 않아야 한다. | Must | Extension | 생성 산출물과 로그에서 입력한 credential 원문이 검출되지 않는다. |
| REQ-FAC-005 | Python 기반 Tool은 정의된 함수 인터페이스, 타입, 설명 및 의존성 목록을 검증해야 한다. | Must | Extension | 계약을 만족하지 않는 함수는 Tool 후보로 등록되지 않는다. |
| REQ-FAC-006 | Python 빌드·시험은 네트워크, 파일, 프로세스 및 자원 사용이 제한된 격리환경에서 수행해야 한다. | Must | Extension | 금지된 파일·네트워크·프로세스 접근 시험이 차단되고 기록된다. |
| REQ-FAC-007 | 생성 산출물에는 재현 가능한 소스, 설정 템플릿, 의존성 잠금정보, 실행방법 및 Tool 메타데이터가 포함되어야 한다. | Must | Extension | 동일 입력과 버전으로 산출물을 다시 빌드할 수 있다. |
| REQ-FAC-008 | 생성된 Tool은 자동 구조검사, 기동검사, Discovery 및 시험호출을 통과해야 등록 가능해야 한다. | Must | Extension | 어느 한 검사가 실패하면 `ACTIVE` 상태로 전환되지 않는다. |
| REQ-FAC-009 | 생성 과정은 Job으로 실행하고 단계별 로그, 결과, 오류 및 산출물 버전을 제공해야 한다. | Must | Extension | 장기 생성 작업 중 UI 요청이 timeout되지 않고 진행상태를 조회할 수 있다. |
| REQ-FAC-010 | 생성 산출물의 변경, 재생성, 폐기 및 이전 버전 복원을 지원해야 한다. | Should | Extension | 새 버전 오류 시 검증된 이전 버전을 다시 활성화할 수 있다. |
| REQ-FAC-011 | 생성 결과는 자동 배포하지 않고 권한 있는 관리자의 검토·승인 후 등록해야 한다. | Must | Extension | 생성 완료만으로 운영 Server가 기동되거나 Agent 후보에 포함되지 않는다. |
| REQ-FAC-012 | Factory 템플릿과 생성기 버전을 기록하여 생성 Tool의 출처를 추적해야 한다. | Must | Extension | Tool 상세에서 원본 명세, 생성기 버전 및 생성 Job을 확인할 수 있다. |

### 7.13 Web UI/UX

| ID | 요구사항 | 우선순위 | 개발 증분 | 수용기준 |
|---|---|---:|---|---|
| REQ-UI-001 | UI는 Dashboard, Agent 실행, Execution 상세, MCP Server, MCP Tool, Agent, Workflow, 예약, 승인, 사용자·권한, 감사로그 메뉴를 제공해야 한다. | Must | Operation | 권한별로 허용된 메뉴와 기능만 노출된다. |
| REQ-UI-002 | Agent 실행 화면은 대화, 입력 보완, 계획 확인, 승인 상태, 진행상태 및 최종 결과를 하나의 흐름에서 제공해야 한다. | Must | Intelligence | 대표 단일·복합 시나리오를 화면 전환 누락 없이 완료할 수 있다. |
| REQ-UI-003 | Execution 상세는 Step 의존관계와 상태를 타임라인 또는 그래프로 표시해야 한다. | Must | Orchestration | 순차·병렬·분기·건너뜀·실패 지점을 시각적으로 구분할 수 있다. |
| REQ-UI-004 | 위험 Tool 실행 및 승인은 Tool, 입력, 영향, 요청자 및 정책을 명확히 구분하여 표시해야 한다. | Must | Operation | 승인자가 일반 정보와 위험정보를 혼동하지 않고 판단할 수 있다. |
| REQ-UI-005 | 모든 주요 목록은 로딩, 빈 결과, 오류, 권한 없음 및 부분 데이터 상태를 구분해 표시해야 한다. | Must | Foundation | API 실패가 빈 목록으로 오인되지 않고 재시도 경로가 제공된다. |
| REQ-UI-006 | 파괴적 또는 영향도 높은 작업은 대상과 영향을 명시한 확인 절차를 제공해야 한다. | Must | Foundation | 비활성화·폐기·취소 등 작업이 단일 오조작으로 완료되지 않는다. |
| REQ-UI-007 | Figma Make 결과를 반영하더라도 Backend 계약, 접근권한, 상태모델 및 디자인 토큰을 임의 변경하지 않아야 한다. | Must | Foundation | 생성 UI 코드 반영 후 API·권한·상태 회귀시험이 통과한다. |
| REQ-UI-008 | 기본 데스크톱 해상도와 주요 브라우저에서 관리 기능을 사용할 수 있어야 하며 핵심 화면은 반응형으로 구성해야 한다. | Should | Operation | 지원 브라우저·해상도 매트릭스의 핵심 사용자 시나리오가 통과한다. |

---

## 8. 비기능 요구사항

### 8.1 성능 및 확장성

| ID | 요구사항 | 우선순위 | 수용기준 |
|---|---|---:|---|
| NFR-PERF-001 | 시스템은 요청 수신, planning, Queue 대기, Step, MCP 호출, LLM 호출 및 최종 응답 시간을 구분 측정해야 한다. | Must | 하나의 Execution에서 전체시간과 구간별 시간을 재구성할 수 있다. |
| NFR-PERF-002 | 일반 관리 API의 성능목표는 외부 LLM/MCP 지연과 분리하여 시험해야 한다. | Must | `docs/09-test-strategy.md`에서 데이터량·동시사용자·백분위 기준과 목표치를 확정한다. |
| NFR-PERF-003 | 장기 Tool 호출과 Workflow 실행은 Web 요청 timeout과 분리된 비동기 실행구조를 사용해야 한다. | Must | UI 연결 종료 후에도 실행정책에 따라 작업이 유지되고 상태를 재조회할 수 있다. |
| NFR-PERF-004 | Worker 수와 동시실행 한도는 환경설정으로 확장할 수 있어야 한다. | Must | 코드 변경 없이 Worker 증설 및 제한값 변경이 가능하다. |
| NFR-PERF-005 | Tool 후보가 증가해도 전체 Tool 설명을 매 요청마다 LLM에 무제한 전달하지 않아야 한다. | Must | 후보 검색 또는 단계적 선택으로 LLM context 상한을 준수한다. |

### 8.2 신뢰성 및 복구

| ID | 요구사항 | 우선순위 | 수용기준 |
|---|---|---:|---|
| NFR-REL-001 | 업무 데이터는 트랜잭션 단위로 저장하고 부분 저장으로 상태 정합성이 깨지지 않아야 한다. | Must | 장애 주입 시험에서 Execution과 Step 상태가 허용된 조합을 유지한다. |
| NFR-REL-002 | Backend 또는 Worker 재시작 시 실행 중 작업을 탐지하여 복구정책을 적용해야 한다. | Must | 복구된 건은 재개·재시도·실패 중 하나로 명확히 종료되고 중복 Tool 호출을 방지한다. |
| NFR-REL-003 | 외부 LLM/MCP 장애가 전체 관리기능 장애로 전파되지 않아야 한다. | Must | 외부 서비스 장애 중에도 로그인, 설정 및 과거 이력 조회가 가능하다. |
| NFR-REL-004 | Database 백업·복구 절차와 복구 검증방법을 제공해야 한다. | Must | 별도 환경에서 백업본으로 핵심 자원 및 실행이력을 복원하는 시험이 통과한다. |
| NFR-REL-005 | 모든 비동기 이벤트와 Job은 유실·중복 가능성을 고려하여 멱등 처리해야 한다. | Must | 동일 메시지 재전달 시험에서 중복 업무 결과가 발생하지 않는다. |

### 8.3 보안 및 개인정보 보호

| ID | 요구사항 | 우선순위 | 수용기준 |
|---|---|---:|---|
| NFR-SEC-001 | 전송구간은 운영환경에서 TLS를 적용해야 한다. | Must | 비암호화 외부 접근이 차단되거나 암호화 연결로 전환된다. |
| NFR-SEC-002 | 비밀번호, API key, access token 및 secret은 평문으로 코드·설정·DB·로그에 저장하지 않아야 한다. | Must | 저장소 및 로그 secret scan과 조회 API 검사가 통과한다. |
| NFR-SEC-003 | 입력값은 API 스키마, 허용목록, 크기 및 형식 기준으로 검증해야 한다. | Must | 비정상 payload, 경로조작, 명령삽입 및 과대 입력 시험이 차단된다. |
| NFR-SEC-004 | 원격 MCP 및 OpenAPI URL 호출은 SSRF 방어정책을 적용해야 한다. | Must | metadata endpoint, loopback, 금지된 사설망 및 redirect 우회 접근이 차단된다. |
| NFR-SEC-005 | 로컬 프로세스 및 생성 Tool은 최소권한 컨테이너 또는 격리환경에서 실행해야 한다. | Must | 비특권 실행, 제한된 mount·network·capability 정책이 배포검사에서 확인된다. |
| NFR-SEC-006 | LLM prompt, Tool 설명 및 Tool 결과에 포함된 악성 지시가 시스템 권한과 정책을 변경하지 못해야 한다. | Must | prompt injection 보안 테스트에서 미허용 Tool 호출과 secret 유출이 발생하지 않는다. |
| NFR-SEC-007 | 오류 응답은 내부 stack trace, secret 및 불필요한 개인정보를 사용자에게 노출하지 않아야 한다. | Must | 운영모드 오류 응답 보안검사가 통과하고 상세정보는 보호된 로그에만 남는다. |
| NFR-SEC-008 | 의존 패키지와 컨테이너 이미지는 버전을 고정하고 취약점 점검이 가능해야 한다. | Must | 빌드 산출물에서 의존성 목록과 이미지 digest 또는 고정 버전을 확인할 수 있다. |
| NFR-SEC-009 | 개인정보 또는 민감정보 저장이 필요한 경우 최소수집, 마스킹, 보존 및 파기정책을 적용해야 한다. | Must | 대상 필드별 분류와 처리정책이 데이터 설계 및 시험에 연결된다. |

### 8.4 호환성 및 유지보수성

| ID | 요구사항 | 우선순위 | 수용기준 |
|---|---|---:|---|
| NFR-COMP-001 | MCP protocol 및 SDK 버전을 명시하고 Server별 협상결과와 호환성 상태를 기록해야 한다. | Must | 지원하지 않는 버전은 원인이 명확한 오류로 거절되며 다른 Server에 영향을 주지 않는다. |
| NFR-COMP-002 | MCP 전송 구현은 인터페이스로 분리하여 protocol 변경이나 어댑터 추가가 핵심 실행엔진 변경으로 이어지지 않아야 한다. | Must | transport 구현 교체 시 Agent/Workflow 도메인 코드 변경이 없다. |
| NFR-MNT-001 | Frontend, API, Agent Runtime, Execution Engine, MCP Manager, Scheduler, Approval 및 Audit의 책임을 모듈로 분리해야 한다. | Must | 순환 의존성 검사와 아키텍처 리뷰 기준을 통과한다. |
| NFR-MNT-002 | 환경별 설정은 환경변수 또는 외부 설정으로 주입하고 코드에 환경주소·credential을 하드코딩하지 않아야 한다. | Must | 동일 이미지가 개발·시험·운영 설정으로 실행된다. |
| NFR-MNT-003 | 공개 API와 Execution Plan 등 핵심 계약은 버전을 관리하고 하위호환 또는 명시적 migration을 제공해야 한다. | Must | 계약 변경 PR에 버전·영향·migration 정보가 포함된다. |
| NFR-MNT-004 | 설계 변경과 구현 PR은 관련 요구사항 ID를 참조해야 한다. | Must | 주요 기능 PR과 테스트에서 하나 이상의 요구사항 ID를 추적할 수 있다. |

### 8.5 시험 및 품질

| ID | 요구사항 | 우선순위 | 수용기준 |
|---|---|---:|---|
| NFR-TEST-001 | Backend 도메인·서비스는 단위시험, API는 통합시험, 핵심 시나리오는 E2E 시험을 제공해야 한다. | Must | CI에서 시험 종류별 결과와 실패 원인을 확인할 수 있다. |
| NFR-TEST-002 | 외부 LLM/MCP 없이 반복 가능한 mock 또는 fixture 기반 시험환경을 제공해야 한다. | Must | 네트워크 없이 핵심 상태전이와 오류정책 시험을 수행할 수 있다. |
| NFR-TEST-003 | 실제 MCP Server를 사용하는 protocol 호환성 시험을 별도로 제공해야 한다. | Must | stdio와 Streamable HTTP 시험 Server에서 Current `server/discover` 또는 legacy handshake, `tools/list`, `tools/call`이 통과한다. |
| NFR-TEST-004 | 보안·권한·승인·감사 요구사항은 정상경로뿐 아니라 우회 시나리오를 시험해야 한다. | Must | 권한 없는 API 직접호출, 입력변조, 재사용 승인 및 prompt injection 시험결과가 존재한다. |
| NFR-TEST-005 | 요구사항 ID와 테스트케이스 ID의 추적표를 유지해야 한다. | Must | Must 요구사항마다 하나 이상의 검증방법 또는 테스트케이스가 연결된다. |
| NFR-TEST-006 | CI는 formatting, lint, type check, unit test, migration check 및 secret scan을 수행해야 한다. | Must | 보호 브랜치 반영 전 필수 검사 결과를 확인할 수 있다. |

### 8.6 배포 및 운영환경

| ID | 요구사항 | 우선순위 | 수용기준 |
|---|---|---:|---|
| NFR-DEP-001 | 개발 및 기준 운영환경은 Docker Compose로 기동할 수 있어야 한다. | Must | 문서화된 단일 절차로 Frontend, Backend, DB 및 필수 Worker가 정상 상태가 된다. |
| NFR-DEP-002 | Docker Compose project name은 `mcpflow`를 사용해야 한다. | Must | 기준 실행 명령과 생성 자원명이 프로젝트 규칙을 따른다. |
| NFR-DEP-003 | 애플리케이션 컨테이너는 가능한 stateless하게 구성하고 영속데이터는 명시된 volume 또는 외부 저장소에 보관해야 한다. | Must | 컨테이너 재생성 후 사용자 설정과 실행이력이 유지된다. |
| NFR-DEP-004 | DB schema 변경은 버전이 관리되는 migration으로 수행해야 한다. | Must | 신규 설치와 이전 버전 업그레이드 시험이 모두 통과한다. |
| NFR-DEP-005 | `.env.example`에는 필요한 키와 설명·예시 형식을 제공하되 실제 secret을 포함하지 않아야 한다. | Must | 신규 개발자가 별도 비밀정보 노출 없이 로컬 설정을 구성할 수 있다. |
| NFR-DEP-006 | 서비스별 health check, restart 정책, log 출력 및 자원 제한을 정의해야 한다. | Must | Docker Compose 상태와 운영 로그에서 서비스 이상을 식별할 수 있다. |
| NFR-DEP-007 | 설치, 기동, 중지, 백업, 복구 및 업데이트 절차를 문서화해야 한다. | Must | 별도 검증자가 문서만으로 기준환경을 설치·복구할 수 있다. |

---

## 9. 핵심 업무 규칙

| ID | 규칙 |
|---|---|
| BR-001 | LLM은 의도 분석과 계획 생성을 담당하며 최종 권한판단과 실행 상태전이는 애플리케이션 로직이 담당한다. |
| BR-002 | Agent가 생성한 계획은 스키마·권한·정책 검증을 통과하기 전에는 실행할 수 없다. |
| BR-003 | 사용자 권한, Agent allowlist, Tool 상태 및 Tool 정책 중 하나라도 실행을 허용하지 않으면 호출을 차단한다. |
| BR-004 | 입력이 모호하거나 필수값이 부족한 경우 추측으로 영향도 높은 Tool을 실행하지 않는다. |
| BR-005 | Tool annotation과 외부 Registry 정보는 참고자료이며 내부 보안·승인정책보다 우선하지 않는다. |
| BR-006 | 승인 이후 Tool, 입력값 또는 영향 정책이 변경되면 기존 승인은 무효로 처리한다. |
| BR-007 | Workflow 및 Agent 변경은 새 버전으로 관리하고 과거 Execution의 참조 버전을 유지한다. |
| BR-008 | 외부 MCP 후보와 Factory 생성 Tool은 검토 완료 전 일반 실행 후보에 포함하지 않는다. |
| BR-009 | 자동 재시도는 멱등성이 확인되거나 중복 부작용이 없는 작업에만 적용한다. |
| BR-010 | 반복 실행에는 반드시 최대 횟수 또는 동등한 종료 한도가 있어야 한다. |
| BR-011 | 모든 Execution은 최종 상태와 종료사유를 가져야 하며 영구적인 `RUNNING` 상태로 방치하지 않는다. |
| BR-012 | 비밀값은 LLM 입력, 일반 로그, 감사 변경값 및 사용자용 오류에 포함하지 않는다. |
| BR-013 | 예약 실행은 예약 생성 당시가 아니라 실제 실행시각의 권한·상태·정책을 따른다. |
| BR-014 | 삭제보다 비활성화와 버전 보존을 우선하여 실행 재현성과 감사 추적성을 유지한다. |

---

## 10. 데이터 보존 및 이력 기준

상세 물리 모델과 기간은 `docs/05-data-model.md` 및 운영정책에서 확정하되 다음 원칙을 따른다.

| 데이터 | 필수 보존 내용 | 변경 원칙 |
|---|---|---|
| MCP Server/Tool | 원본 메타데이터, 운영 메타데이터, 버전, 상태, 검증정보 | 참조 이력 존재 시 비활성화 |
| Agent/Workflow | 버전별 정의, 허용 Tool, 정책, 게시상태 | 게시 후 변경 시 새 버전 |
| Execution | 사용자 요청, 계획 스냅샷, 상태, 시간, 결과 요약, 오류 | 완료 후 핵심 필드 불변 |
| Tool Call | 입력·출력 또는 보호된 참조, 정책 스냅샷, 시도별 결과 | 민감정보 정책 우선 |
| Approval | 요청 스냅샷, 승인자, 결정, 의견, 시각 | 결정 후 수정 금지 |
| Schedule | 일정, 입력, 정책, 실행 연결정보 | 변경 이력 보존 |
| Audit Log | 행위자, 행위, 대상, 결과, 변경정보, 추적 ID | 일반 기능에서 수정·삭제 금지 |

---

## 11. 과제 성능지표 연계

정량 목표값과 시험조건은 협약·평가 기준을 확인하여 `docs/09-test-strategy.md`에서 확정한다. 구현 단계에서는 아래 산식에 필요한 원천데이터를 반드시 수집한다.

| 성능지표 | 산식 또는 측정기준 | 관련 요구사항 |
|---|---|---|
| 응답시간 | 요청 수신부터 최종 응답까지의 E2E 시간과 planning·MCP·LLM 등 구간시간을 분리 측정 | `NFR-PERF-001`, `REQ-EXE-014` |
| Tool 매핑 정확도 | 정답 Tool과 자동 선택 Tool의 일치 건수 ÷ 전체 평가 요청 건수 × 100 | `REQ-AGT-004`, `REQ-AGT-005`, `REQ-AGT-014` |
| 연계·검증 완료 MCP Tool 수 | 등록 후 Discovery·스키마 검증·시험호출을 통과한 내부·외부 Tool의 고유 건수, 목표 10건 이상 | `REQ-TOOL-012`, `REQ-DISC-004`, `REQ-FAC-008` |
| 등록 성공률 | 기준 등록 시도 중 연결·protocol 협상·Tool Discovery·저장 완료 건수 ÷ 전체 유효 등록 시도 건수 × 100 | `REQ-MCP-004`, `REQ-MCP-005`, `REQ-TOOL-001` |
| 복합 실행 시나리오 완료율 | 사전 정의된 순차·병렬·조건·반복 시나리오 성공 건수 ÷ 전체 실행 건수 × 100 | `REQ-WF-003`~`REQ-WF-009`, `REQ-EXE-017` |
| 운영 기능 통과율 | 예약·승인·권한·감사 등 운영 시험의 통과 항목 수 ÷ 전체 시험 항목 수 × 100 | `REQ-AUTH-*`, `REQ-APR-*`, `REQ-SCH-*`, `REQ-AUD-*` |

측정 데이터는 실행·평가 버전, 모델, Tool 버전, 입력셋, 시작·종료시각 및 성공판정 근거와 함께 보존해야 한다.

---

## 12. 개발 완료 판정 기준

기능은 다음 조건을 모두 충족해야 `Implemented`로 표시할 수 있다.

1. 요구사항에 대응하는 기능 또는 명시적 대체수단이 구현되어 있다.
2. API, 데이터 모델, UI/UX 및 배포 설계가 실제 구현과 일치한다.
3. 정상, 오류, 권한 거부 및 경계조건 테스트가 작성되어 통과한다.
4. 민감정보, 감사로그 및 권한 관련 보안 기준을 충족한다.
5. 구조화 로그와 필요한 metric을 통해 운영 중 결과를 확인할 수 있다.
6. 관련 사용자·운영 문서가 현행화되어 있다.

요구사항은 독립적인 검증자가 수용기준과 연결된 시험 증빙을 확인한 후 `Verified`로 표시한다.

---

## 13. 설계문서 추적 기준

| 후속 문서 | 본 문서와의 연결 기준 |
|---|---|
| `02-functional-specification.md` | 기능별 사용자 흐름, 입력·처리·출력·예외와 `REQ-*` 연결 |
| `03-system-architecture.md` | 컴포넌트 책임과 `REQ-*`, `NFR-*` 배치 |
| `04-agent-mcp-architecture.md` | `REQ-MCP-*`, `REQ-TOOL-*`, `REQ-AGT-*`, `REQ-WF-*`, `REQ-EXE-*` 상세화 |
| `05-data-model.md` | 엔터티·관계·보존정책과 요구사항 ID 연결 |
| `06-api-design.md` | Endpoint별 요구사항 ID, 권한, 요청·응답, 오류 연결 |
| `07-ui-ux-design.md` | 화면·컴포넌트·상태별 `REQ-UI-*` 및 기능 요구사항 연결 |
| `08-deployment-architecture.md` | `NFR-SEC-*`, `NFR-REL-*`, `NFR-DEP-*` 구현 구조 연결 |
| `09-test-strategy.md` | 요구사항별 테스트케이스, 정량 목표, 환경 및 증빙 연결 |

코드, migration, API, UI 또는 운영정책을 변경하는 PR은 영향받는 요구사항 ID와 설계 문서 현행화 여부를 명시한다.

---

## 14. 미확정 사항

다음 항목은 후속 설계에서 확정하되, 확정 전에는 구현에 하드코딩하지 않는다.

| ID | 미확정 사항 | 확정 문서 |
|---|---|---|
| TBD-001 | 인증방식: 자체 계정, OIDC 또는 조직 인증 연계 범위 | 시스템 아키텍처/API 설계 |
| TBD-002 | Queue/Scheduler 구현제품과 Worker 실행구조 | 시스템/배포 아키텍처 |
| TBD-003 | Agent Framework 적용 여부와 자체 Runtime 범위 | Agent/MCP 상세설계 |
| TBD-004 | Tool 후보 검색의 keyword, embedding, hybrid 적용방식 | Agent/MCP 상세설계 |
| TBD-005 | Workflow 편집 UI의 form 방식과 canvas 방식 적용범위 | UI/UX 설계 |
| TBD-006 | 외부 MCP Registry의 지원 출처와 신뢰정책 | 기능정의/보안 설계 |
| TBD-007 | Python Tool Factory의 허용 패키지와 격리 수준 | Agent/MCP 및 배포 설계 |
| TBD-008 | 로그·실행결과·감사로그의 구체적 보존기간 | 데이터 모델/운영정책 |
| TBD-009 | 정량 성능목표, 데이터셋 규모 및 시험 장비 사양 | 시험전략 |
| TBD-010 | 알림 채널과 외부 메시징 연계범위 | 기능정의/API 설계 |

---

## 15. 변경 이력

| 버전 | 일자 | 변경 내용 |
|---|---|---|
| v0.2 | 2026-09-02 | MCP 2026-07-28 `server/discover` 및 legacy protocol 호환 기준 반영 |
| v0.1 | 2026-09-02 | 실제 개발 기준 요구사항, 수용기준, 성능지표 연계 및 미확정 사항 최초 작성 |
