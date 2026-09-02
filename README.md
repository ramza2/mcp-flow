# MCPFlow

> **MCPFlow - MCP-based AI Agent Automation Platform**

MCPFlow는 자연어 기반 업무 요청을 분석하여 적절한 MCP(Model Context Protocol) Tool을 선택하고, 여러 Tool을 순차·병렬·조건부로 실행할 수 있도록 지원하는 AI Agent 기반 업무 자동화 플랫폼입니다.

본 프로젝트는 **MCP 서버 및 Tool의 통합 관리**, **AI 기반 Tool 선택**, ​**복합 실행 흐름 관리**, ​**예약·승인·권한·감사 기능**을 하나의 플랫폼으로 제공하는 것을 목표로 합니다.

---

## 1. Project Overview

- **Project Name:** MCPFlow
- **Repository:** `mcp-flow`
- **Docker Project:** `mcpflow`
- **Backend Package:** `mcpflow`
- **Project Type:** MCP-based AI Agent Automation Platform

MCPFlow의 기본 실행 흐름은 다음과 같습니다.

```text
User Request
    ↓
Natural Language Analysis
    ↓
AI Agent / Planner
    ↓
MCP Tool Selection
    ↓
Execution Plan
    ↓
Execution Engine
    ├─ Sequential
    ├─ Parallel
    ├─ Conditional
    ├─ Retry
    └─ Approval
    ↓
MCP Tool Execution
    ↓
Result Validation
    ↓
Response
    ↓
Execution / Audit Log
```

---

## 2. Goals

MCPFlow는 다음 기능을 중심으로 개발합니다.

- MCP Server 등록 및 통합 관리
- MCP Tool Discovery 및 Tool 메타데이터 관리
- 자연어 요청 분석
- AI 기반 MCP Tool 자동 선택
- Tool 입력 Parameter 자동 구성
- 다중 Tool 실행 계획 생성
- 순차 실행
- 병렬 실행
- 조건 기반 실행
- 반복 및 재시도 처리
- 예약 실행
- 사용자 승인 기반 실행
- 사용자 및 Role 기반 권한 관리
- 실행 이력 관리
- 감사 로그 관리
- 운영 및 모니터링 UI 제공

---

## 3. Core Architecture

MCPFlow는 다음과 같은 주요 컴포넌트로 구성할 예정입니다.

```text
┌───────────────────────────────┐
│            Web UI             │
│      React + TypeScript       │
└───────────────┬───────────────┘
                │
                ▼
┌───────────────────────────────┐
│          Backend API          │
│            FastAPI            │
├───────────────────────────────┤
│ Agent Runtime                 │
│ - Request Analyzer            │
│ - Planner                     │
│ - Tool Selector               │
│ - Parameter Generator         │
├───────────────────────────────┤
│ Execution Engine              │
│ - Sequential                  │
│ - Parallel                    │
│ - Conditional                 │
│ - Retry                       │
│ - Approval Wait               │
├───────────────────────────────┤
│ MCP Manager                   │
│ - MCP Server Registry         │
│ - Tool Discovery              │
│ - Tool Registry               │
│ - Tool Execution              │
├───────────────────────────────┤
│ Scheduler                     │
│ Approval                      │
│ RBAC                          │
│ Audit                         │
└───────────────┬───────────────┘
                │
        ┌───────┴────────┐
        ▼                ▼
   PostgreSQL        MCP Servers
                     ├─ Internal
                     └─ External
```

상세 아키텍처는 `docs/` 하위 설계 문서에서 정의합니다.

---

## 4. Technology Stack

현재 기본 기술 스택은 다음을 기준으로 설계합니다.

### Frontend

- React
- TypeScript
- Vite
- Figma Make 기반 UI/UX 설계 및 초기 화면 구현

### Backend

- Python
- FastAPI
- Pydantic
- SQLAlchemy 또는 이에 준하는 ORM

### Database

- PostgreSQL

### AI / Agent

- OpenAI-compatible LLM API
- Agent Runtime
- Tool Selection
- Execution Planning
- MCP Client

구체적인 모델 및 Agent Framework 적용 여부는 상세설계 과정에서 결정합니다.

### MCP

- Model Context Protocol
- MCP Server Registry
- MCP Tool Discovery
- MCP Tool Execution

### Infrastructure

- Docker
- Docker Compose
- Reverse Proxy
- GitHub

향후 운영환경 및 필요성에 따라 배포구조를 확장할 수 있도록 구성합니다.

---

## 5. Repository Structure

초기 Repository 구조는 다음을 기준으로 합니다.

```text
mcp-flow/
│
├── README.md
│
├── docs/
│   ├── 01-requirements.md
│   ├── 02-functional-specification.md
│   ├── 03-system-architecture.md
│   ├── 04-agent-mcp-architecture.md
│   ├── 05-data-model.md
│   ├── 06-api-design.md
│   ├── 07-ui-ux-design.md
│   ├── 08-deployment-architecture.md
│   └── 09-test-strategy.md
│
├── backend/
│
├── frontend/
│
├── infra/
│
├── tests/
│
├── docker-compose.yml
│
├── .env.example
│
└── .gitignore
```

프로젝트가 진행되면서 구조는 실제 구현에 맞게 현행화합니다.

---

## 6. Design Documents

MCPFlow는 구현에 앞서 주요 설계를 Markdown 문서로 관리합니다.

설계 문서는 단순 제출용 산출물이 아니라 **개발 과정에서 Cursor Agent가 참조하는 Source of Truth**로 사용합니다.

| Document | Description |
|---|---|
| `01-requirements.md` | 요구사항 정의 |
| `02-functional-specification.md` | 기능 정의 및 상세 기능 |
| `03-system-architecture.md` | 전체 시스템 아키텍처 |
| `04-agent-mcp-architecture.md` | AI Agent 및 MCP 실행구조 상세설계 |
| `05-data-model.md` | 데이터 모델 및 ERD |
| `06-api-design.md` | Backend API 설계 |
| `07-ui-ux-design.md` | IA, 화면구성 및 UI/UX 설계 |
| `08-deployment-architecture.md` | Docker 및 배포 아키텍처 |
| `09-test-strategy.md` | 기능·통합·성능 시험 전략 |

설계와 구현이 변경되는 경우 관련 Markdown 문서를 함께 현행화하는 것을 원칙으로 합니다.

---

## 7. Development Principles

MCPFlow 개발은 다음 원칙을 따릅니다.

### Design First

기능 구현 전에 요구사항과 아키텍처를 먼저 정의합니다.

```text
Requirement
    ↓
Design
    ↓
UI/UX
    ↓
Implementation
    ↓
Test
    ↓
Design Update
```

### Documentation as Source of Truth

`docs/`의 설계 문서를 Cursor 및 개발자의 공통 개발 기준으로 사용합니다.

설계와 실제 코드가 충돌하는 경우 임의로 구현을 변경하기보다 설계 변경 필요성을 먼저 검토합니다.

### Separation of Responsibilities

다음 영역의 책임을 명확하게 분리합니다.

```text
Agent Runtime
≠
Execution Engine
≠
MCP Manager
≠
Scheduler
≠
Approval
≠
RBAC / Audit
```

LLM이 모든 실행 로직을 직접 담당하지 않도록 하고, 가능한 실행 제어는 명시적인 Application Logic과 Execution Engine에서 처리합니다.

### Incremental Development

전체 기능을 한 번에 구현하지 않고 End-to-End 동작 가능한 기능 단위로 개발합니다.

```text
Design
→ Implement
→ Test
→ Review
→ Refactor
→ Next Feature
```

---

## 8. UI/UX Development

UI/UX는 Figma Make를 활용하여 설계합니다.

초기 주요 화면은 다음을 대상으로 합니다.

- Dashboard
- Agent Chat / Task Execution
- Execution Detail
- MCP Server Management
- MCP Tool Management
- Agent Management
- Execution History
- Schedule Management
- Approval Management
- User / Role / Permission Management
- Audit Log

Figma에서 확정된 UI/UX 및 생성 코드는 Frontend에 반영한 후 실제 Backend API와 연계합니다.

---

## 9. AI-assisted Development

개발은 Cursor의 Agents Window를 적극 활용합니다.

각 Agent가 동일한 개발 원칙과 시스템 구조를 준수할 수 있도록 설계 문서를 공통 Context로 제공합니다.

설계 및 초기 UI/UX 구현이 안정화된 이후에는 별도의 Agent Rule 문서를 작성하여 다음 항목을 공통 적용할 예정입니다.

- Architecture rules
- Coding conventions
- Naming conventions
- Directory structure rules
- API implementation rules
- Database rules
- Error handling
- Logging
- Security
- Test requirements
- Documentation update policy
- Cursor Agent 작업 원칙

해당 규칙은 프로젝트 진행 상황에 맞추어 별도 Markdown 문서로 관리합니다.

---

## 10. Development Roadmap

초기 개발은 다음 순서를 기준으로 진행합니다.

```text
1. Requirements
        ↓
2. Functional Specification
        ↓
3. System Architecture
        ↓
4. Agent / MCP Architecture
        ↓
5. Data Model & API Design
        ↓
6. UI/UX Design with Figma
        ↓
7. Project Skeleton
        ↓
8. MCP Server / Tool Management
        ↓
9. Agent Runtime
        ↓
10. Execution Engine
        ↓
11. Schedule / Approval / RBAC / Audit
        ↓
12. Integration Test
        ↓
13. Performance Test
        ↓
14. Pilot Operation
```

---

## 11. Development Status

현재 단계:

```text
[■] Project initialization
[ ] Requirements
[ ] Functional specification
[ ] System architecture
[ ] Agent / MCP architecture
[ ] Data model
[ ] API design
[ ] UI/UX design
[ ] Development environment
[ ] Core implementation
[ ] Integration test
[ ] Deployment
```

---

## 12. License

현재 프로젝트는 내부 개발 프로젝트로 운영하며 License는 추후 결정합니다.