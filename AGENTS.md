# MCPFlow Agent Development Guide

This repository implements **MCPFlow**, an MCP-based AI Agent automation platform.

This file defines repository-wide rules for coding agents and automated development tools.

It is a **development guardrail**, not a replacement for the canonical design documents.

All implementation decisions MUST remain consistent with the documents under `docs/`.

---

# 1. Source of Truth

The canonical project documents are:

```text
docs/01-requirements.md
docs/02-functional-specification.md
docs/03-system-architecture.md
docs/04-agent-mcp-architecture.md
docs/05-data-model.md
docs/06-api-design.md
docs/07-ui-ux-design.md
docs/08-deployment-architecture.md
docs/09-test-strategy.md
```

Use the following authority model.

```text
01 — What the product must support
02 — Functional behavior
03 — System/module responsibility
04 — Agent, MCP, StructuredRequest, Execution Plan contracts
05 — Persistent domain entities, lifecycle and canonical enums
06 — API representation of 04/05
07 — Frontend IA, routes and UX representation
08 — Deployment topology, services and queues
09 — Verification and test requirements
```

When implementation and documentation conflict:

1. Do NOT silently invent a workaround.
2. Identify the relevant canonical document.
3. Preserve the documented contract.
4. If the contract itself must change, update the authoritative document first.
5. Then update dependent documents and code.

---

# 2. Critical Canonical Rule

Never invent a new:

* Domain status
* lifecycle state
* Plan Step Type
* `risk_class`
* Execution source type
* Schedule target type
* API enum
* MCP discovery mode
* MCP authentication type
* Binding kind
* Predicate operator

only because it is convenient for implementation.

Before introducing a new value, check:

```text
docs/04-agent-mcp-architecture.md
docs/05-data-model.md
docs/06-api-design.md
```

If a genuinely new canonical value is required:

```text
change 04/05 first
→ update 06 if API-visible
→ update 07 if UI-visible
→ update tests
→ then change code
```

Do not let implementation become the accidental Source of Truth.

---

# 3. Canonical Domain States

## MCP Server

```text
DRAFT
ACTIVE
INACTIVE
ERROR
```

## MCP Tool

```text
DISCOVERED
ACTIVE
INACTIVE
MISSING
BLOCKED
```

## ToolVersion Validation

```text
VALID
INVALID
WARNING
```

## Tool Verification

```text
PENDING
VERIFIED
FAILED
EXPIRED
```

Verification belongs to a specific **ToolVersion**.

A new ToolVersion does not automatically inherit verification from an older version.

## Agent

Logical Agent:

```text
DRAFT
ACTIVE
INACTIVE
ARCHIVED
```

AgentVersion:

```text
DRAFT
PUBLISHED
DEPRECATED
```

## Workflow

Logical Workflow:

```text
DRAFT
ACTIVE
INACTIVE
ARCHIVED
```

WorkflowVersion:

```text
DRAFT
PUBLISHED
DEPRECATED
```

Do not use `PUBLISHED` as the logical Agent/Workflow status.

---

# 4. AgentRequest and Execution Must Remain Separate

Agent planning and actual execution are different lifecycles.

## AgentRequest

```text
RECEIVED
ANALYZING
RETRIEVING
SELECTING
BUILDING_PARAMETERS
PLANNING
VALIDATING
WAITING_INPUT
WAITING_CONFIRMATION
READY
REJECTED
FAILED
CANCELLED
```

## Execution

```text
CREATED
QUEUED
RUNNING
WAITING_INPUT
WAITING_APPROVAL
CANCEL_REQUESTED
SUCCEEDED
PARTIALLY_SUCCEEDED
FAILED
CANCELLED
TIMED_OUT
```

Never add the following to Execution:

```text
PLANNING
WAITING_CONFIRMATION
REJECTED
EXPIRED
PARTIAL
```

The Agent Runtime performs:

```text
natural-language analysis
→ capability retrieval
→ Tool selection
→ parameter construction
→ Execution Plan construction
→ validation
```

The Agent Runtime does **not** directly invoke MCP Tools.

Actual Tool execution begins only after the AgentRequest is ready and an Execution is created.

For safe policies, `AUTO_EXECUTE_SAFE` may create an Execution automatically after a validated safe plan.

---

# 5. Step States

Canonical Step states:

```text
PENDING
READY
RUNNING
WAITING_INPUT
WAITING_APPROVAL
SUCCEEDED
FAILED
SKIPPED
TIMED_OUT
CANCELLED
UNKNOWN_OUTCOME
```

`UNKNOWN_OUTCOME` is important for external side effects.

Do not automatically retry a Step with `UNKNOWN_OUTCOME`.

The external system may already have performed the action.

---

# 6. Execution Source Types

Canonical Execution source types:

```text
AGENT_REQUEST
WORKFLOW_VERSION
SCHEDULE_OCCURRENCE
MANUAL_TOOL_TEST
FACTORY_TEST
```

Retry is NOT an Execution source type.

Use relationships such as:

```text
parent_execution_id
trigger_type = RETRY
```

Do not persist presentation labels such as:

```text
Agent
Workflow
Schedule
```

as Domain values.

Convert canonical values to human-readable labels only in the presentation layer.

---

# 7. Risk Classification

Canonical `risk_class`:

```text
READ_ONLY
IDEMPOTENT_WRITE
NON_IDEMPOTENT_WRITE
DESTRUCTIVE
UNKNOWN
```

Do not introduce simplified values such as:

```text
WRITE
HIGH
MEDIUM
LOW
```

as canonical Tool risk values.

Risk policy must distinguish whether an operation can cause repeatable or irreversible external side effects.

---

# 8. StructuredRequest Contract

StructuredRequest v1 must preserve the contract defined in `docs/04-agent-mcp-architecture.md`.

Core fields include:

```text
schema_version
request_text
intent
entities
constraints
expected_outputs
required_capabilities
risk_hints
missing_inputs
ambiguities
needs_clarification
```

Entity structure:

```text
name
value
source
```

Do not add model-internal chain-of-thought fields to public contracts.

Never expose hidden reasoning or internal model deliberation to the UI/API.

---

# 9. Parameter Provenance

Canonical parameter provenance values:

```text
USER_EXPLICIT
WORKFLOW_INPUT
CONVERSATION_CONFIRMED
STEP_OUTPUT
POLICY_DEFAULT
MODEL_DERIVED
SECRET_REFERENCE
```

When a parameter is derived, preserve its provenance where required by the contract.

Do not flatten user-provided, policy-provided and model-derived values into an indistinguishable parameter map when provenance is required.

---

# 10. Binding Contract

Canonical Binding kinds:

```text
LITERAL
PLAN_INPUT
STEP_OUTPUT
EXECUTION_CONTEXT
LOOP_CONTEXT
SECRET_REF
```

Binding paths use the supported RFC 6901 JSON Pointer subset.

Do not allow arbitrary:

```text
JavaScript
Python
shell expression
template code
eval()
```

inside workflow bindings.

Do not replace the canonical Binding model with an unrestricted expression language.

---

# 11. Execution Plan v1

Canonical authorable Step Types are:

```text
TOOL
CONDITION
JOIN
APPROVAL
LOOP
```

Do NOT create persisted authorable types such as:

```text
USER_INPUT
PARALLEL
END
SCRIPT
PYTHON
JAVASCRIPT
```

### Parallel

Parallelism is represented by graph dependencies.

`PARALLEL` may exist as a visual UI concept but not as a persisted canonical Step Type.

### End

`END` may be displayed as a visual marker.

It is not a persisted Step Type.

### Runtime User Input

Do not add an authorable `USER_INPUT` Step.

Planning input uses:

```text
AgentRequest WAITING_INPUT
```

MCP runtime input uses:

```text
Execution / Step WAITING_INPUT
```

---

# 12. JOIN

Canonical JOIN policies:

```text
ALL_SUCCESS
ALL_COMPLETE
ANY_SUCCESS
```

Do not invent alternative persisted join semantics without changing the canonical contract first.

---

# 13. LOOP

Canonical Loop modes:

```text
FOR_EACH
WHILE
```

Every Loop must have a bounded:

```text
max_iterations
```

Do not implement an unbounded loop.

Runtime safeguards must prevent infinite execution.

---

# 14. Predicate AST

Conditions and WHILE expressions use the restricted Predicate AST.

Supported comparison operators:

```text
eq
ne
gt
gte
lt
lte
in
contains
```

Unary operators:

```text
exists
is_null
```

Logical operators:

```text
and
or
not
```

Semantics:

```text
comparison → left Binding + right Binding/value
exists/is_null → unary Binding
not → single Predicate child
and/or → Predicate children
```

The Predicate structure is recursive.

Do not implement all operators as a flat `left / op / right` structure.

Do not allow arbitrary scripts or executable expressions.

---

# 15. Agent and Workflow Versioning

Published versions are immutable.

Lifecycle:

```text
DRAFT
→ PUBLISHED
→ DEPRECATED
```

Rules:

* Only DRAFT versions may be edited.
* PUBLISHED versions are read-only.
* DEPRECATED versions are read-only.
* Changing a PUBLISHED version requires creation of a new DRAFT version.
* Tool Grants are version-scoped.
* Execution and Schedule must reference explicit versions.

Never mutate a published AgentVersion or WorkflowVersion in place.

---

# 16. Schedule Contract

Schedule status:

```text
ACTIVE
PAUSED
COMPLETED
ERROR
```

Schedule target:

```text
AGENT_VERSION
WORKFLOW_VERSION
```

Do not use implicit runtime `latest version`.

Schedules must point to an explicit version.

Overlap policies:

```text
ALLOW
SKIP
QUEUE
REPLACE
```

Misfire policies:

```text
SKIP
RUN_ONCE
CATCH_UP_LIMITED
```

Occurrence states:

```text
PLANNED
SKIPPED
ENQUEUED
RUNNING
COMPLETED
FAILED
```

Do not introduce:

```text
INACTIVE
CANCEL_RUNNING
RUN_ALL
```

as canonical Schedule values.

---

# 17. Approval Contract

Approval status:

```text
PENDING
APPROVED
REJECTED
EXPIRED
CANCELLED
```

Approval Entity status and Execution status are different concepts.

Do not map:

```text
Approval PENDING
```

to:

```text
Approval WAITING_APPROVAL
```

`WAITING_APPROVAL` belongs to Execution/Step.

Approval reject/expiry does NOT create Execution statuses such as:

```text
REJECTED
EXPIRED
```

The plan completion policy determines the resulting Execution status.

Reusable ApprovalPolicy includes concepts such as:

```text
decision_mode
required_approvals
approver_scope
default_expiry_seconds
allow_self_approval
reject_comment_required
lock_version
```

Decision modes:

```text
ANY
ALL
QUORUM
```

An approval is bound to the approved snapshot.

If the actual input changes after approval, do not reuse the old approval.

---

# 18. MCP Runtime Input / MRTR

Current MCP runtime user input follows MRTR semantics.

Conceptual flow:

```text
tools/call
→ resultType = input_required
→ inputRequests
→ opaque requestState
→ Step / Execution WAITING_INPUT
→ user response
→ schema validation
→ retry original call with inputResponses
→ echo requestState unchanged
→ complete or another input_required round
```

Critical rules:

* `requestState` is opaque.
* Never interpret it using an LLM.
* Never modify it.
* Never show it to the user.
* Never allow the user to edit it.
* Preserve it exactly when resuming the request.
* Enforce a maximum MRTR round count.
* Enforce total Step timeout.

After valid runtime input:

```text
WAITING_INPUT
→ RUNNING
```

Planning clarification and runtime MCP input must remain visually and semantically distinct.

---

# 19. Current MCP vs Legacy MCP

Current MCP reference version used by this repository:

```text
2026-07-28
```

Current MCP protocol core is stateless and requests are self-describing.

Explicit discovery is optional.

An MCP Server without `server/discover` is not automatically incompatible.

When compatible Current MCP behavior is inferred without explicit discovery, use:

```text
INFERRED_CURRENT
```

Canonical discovery modes:

```text
EXPLICIT_DISCOVERY
INFERRED_CURRENT
LEGACY_HANDSHAKE
```

Legacy:

```text
initialize
initialized
```

behavior belongs in `LegacyMCPAdapter`.

Do not implement Legacy handshake assumptions in the Current MCP path.

Current MCP uses the protocol headers defined by the canonical architecture, including:

```text
Mcp-Method
Mcp-Name
```

where applicable.

---

# 20. MCP Authentication

Canonical auth types:

```text
NONE
BEARER
API_KEY_HEADER
BASIC
OAUTH2
CUSTOM_HEADERS
STDIO_ENV
```

The UI may show friendly labels, but persisted/API values remain canonical.

---

# 21. Secrets

Secrets are reference-only after registration.

Never persist or expose raw secrets in normal Domain/API responses.

Use Secret references.

Do not:

* write secrets into source code
* write secrets into Git
* write secrets into audit logs
* expose raw secret values in frontend state after registration
* include secrets in LLM prompts unless explicitly permitted by the security design

Mask sensitive input/output in:

```text
Approval
Audit
Execution
Logs
UI
```

where required.

---

# 22. STDIO MCP

STDIO MCP execution is restricted.

Do not provide an arbitrary command editor.

Use repository-managed allowlisted manifests:

```text
infra/mcp-manifests/*.yaml
```

The user chooses a registered manifest.

Actual STDIO processes run only in:

```text
mcp-worker
```

Do not execute arbitrary user-provided shell commands through the API service or frontend.

---

# 23. MCP Tool Factory

Tool Factory supports controlled generation from sources such as:

```text
OpenAPI
Python
```

Generated Tool does NOT mean activated Tool.

Flow:

```text
Source
→ Analyze
→ Candidate Tools
→ Build
→ Security / Contract Test
→ Review
→ Publish / Import
→ Draft MCP Server
→ Connection Test
→ Discovery
→ Activate
```

Do not automatically activate generated Tools without the required registration/validation workflow.

---

# 24. External MCP Discovery

External discovery is a candidate discovery process.

Flow:

```text
Search
→ Candidate
→ Review
→ Import
→ Draft MCP Server
→ Connection Test
→ Discovery
→ Activate
```

Never treat search results as trusted active MCP Servers.

---

# 25. Frontend Rules

Frontend stack:

```text
React
TypeScript
Vite
Tailwind
```

The frontend is a standalone Vite application.

Do not reintroduce Figma Make runtime dependencies.

Do not add:

```text
.figma runtime
FIGMA_PUBLIC_URL
figmaSiteConfiguration
figmaErrorOverlayReplay
figmaReactRefreshBoundaryFallback
figmaMakeKitPlugin
window.__FIGMA__
```

Figma may remain mentioned in design documentation, but not as a runtime dependency.

---

# 26. Frontend Domain Types

Canonical frontend Domain types belong under:

```text
frontend/src/domain/
```

Do not duplicate canonical status unions independently across screens.

Prefer the central canonical constants/types.

Presentation labels must be separate from persisted Domain values.

Example:

```text
AGENT_REQUEST
```

may display as:

```text
Agent
```

but the stored Domain value remains `AGENT_REQUEST`.

---

# 27. Frontend Status Representation

UI must not create alternate Domain status values for convenience.

Examples:

Wrong:

```text
Approval PENDING → WAITING_APPROVAL
Workflow ACTIVE → PUBLISHED
Schedule PAUSED → INACTIVE
```

Correct:

```text
Domain value remains canonical
→ StatusBadge / formatter maps presentation only
```

Status representation should use:

```text
label
icon
color
```

and must not rely on color alone.

---

# 28. Frontend Agent Run

Agent Run must preserve:

```text
AgentRequest planning lifecycle
```

separately from:

```text
Execution lifecycle
```

Planning WAITING_INPUT:

```text
Agent needs information
```

Runtime MCP WAITING_INPUT:

```text
MCP Tool requests information
```

Do not expose:

* system prompt
* hidden chain-of-thought
* internal planning traces
* secret values
* MRTR requestState

Public analysis summaries may be shown where designed.

---

# 29. Workflow Designer

Workflow Designer implements **Execution Plan v1**, not a general BPMN engine.

Persist only canonical Step Types.

Canvas position is visual.

Execution semantics come from:

```text
step IDs
dependencies
edges
bindings
predicates
policies
```

Do not infer execution order from x/y canvas position.

---

# 30. Frontend Permissions

Frontend PermissionGate is a UX helper only.

Backend authorization remains authoritative.

Never treat:

```text
hidden button
disabled menu
frontend route guard
```

as a security boundary.

All sensitive backend operations must perform server-side authorization.

---

# 31. API Design

API is versioned under:

```text
/api/v1
```

API contracts expose canonical Domain semantics from `04` and `05`.

Do not create API-specific status enums that diverge from the Domain model.

For long-running operations, use the Job/Execution patterns defined by the design rather than blocking HTTP calls indefinitely.

SSE Execution events are exposed under the documented Execution event endpoint.

The frontend must handle:

```text
snapshot
→ SSE
→ event id
→ reconnect using Last-Event-ID
→ polling fallback
```

Duplicate events must never regress displayed state.

---

# 32. Backend Architecture

Backend implementation must preserve module boundaries defined in `docs/03-system-architecture.md`.

Do not collapse all responsibilities into one service simply because it is easier during initial implementation.

Logical responsibilities include:

* API / control plane
* Agent Runtime
* Execution Engine
* MCP integration
* MCP stdio isolation
* Tool Factory
* Scheduler
* Outbox/event delivery
* persistence
* audit
* authorization

Early skeleton implementations may share code/processes where explicitly planned, but the architectural boundaries must remain visible.

---

# 33. Canonical Deployment Services

Use service names from `docs/08-deployment-architecture.md`.

Canonical names:

```text
traefik
frontend
api
worker
mcp-worker
factory-worker
scheduler
outbox
postgres
redis
object-storage
migration
```

Do not casually rename these in Compose, documentation or code.

Traefik routes:

```text
/                 → frontend
/api/v1/*         → api
/health/*         → API health
```

Execution SSE remains under the API route.

Do not create a separate external `/events` proxy unless the design changes.

---

# 34. Canonical Queues

Canonical queues:

```text
agent
execution
mcp_stdio
factory
maintenance
```

Do not invent ad hoc queue names without updating the deployment architecture.

---

# 35. Database and Persistence

Persistent state must follow `docs/05-data-model.md`.

Do not use UI mock models as the database schema authority.

Use explicit version IDs for versioned entities.

Published immutable versions must remain immutable at the persistence layer, not only in the frontend.

Use migrations for schema changes.

Do not rely on automatic schema mutation in production.

---

# 36. Concurrency and Idempotency

Side-effecting operations require explicit consideration of:

* idempotency
* retry safety
* duplicate delivery
* unknown outcome
* optimistic locking
* approval snapshot consistency

Use `lock_version` or the concurrency mechanism defined by the Domain contract where required.

Do not silently retry a non-idempotent external operation.

---

# 37. Outbox / Events

Where transactional event delivery is required, use the Outbox pattern defined by the architecture.

Do not:

```text
commit database
then hope an unrelated publish succeeds
```

for state transitions that require reliable event emission.

Event consumers must tolerate duplicate delivery.

---

# 38. Audit

Security- and operation-relevant changes must be auditable where required by the functional specification.

Audit records should identify concepts such as:

```text
actor
action
resource
result
request/correlation identifier
timestamp
change summary
```

Do not put raw secrets into audit data.

---

# 39. Error Handling

Prefer explicit domain errors over generic exceptions.

Errors crossing API boundaries should:

* be structured
* have stable codes where defined
* avoid leaking secrets/internal stack traces
* preserve correlation identifiers

Do not expose raw database, model provider or infrastructure exception strings directly to end users.

---

# 40. LLM and Model Profiles

LLM and embedding provider settings use Model Profiles.

Do not hardcode a specific provider/model into Agent logic when a configured Model Profile should be used.

The system supports separate:

```text
LLM profiles
Embedding profiles
```

The embedding profile used for Tool retrieval/search is configured explicitly.

Secrets such as provider API keys remain references/configured-secret state only.

---

# 41. Tool Selection and Retrieval

Tool retrieval and Tool selection must operate on valid/available ToolVersion information.

Consider:

* logical Tool status
* ToolVersion validation
* verification state
* risk class
* Agent Tool Grant
* capability/tags
* policy
* user permissions

Do not allow an inactive/blocked Tool to become executable merely because semantic similarity is high.

---

# 42. Cancellation

Execution cancellation uses:

```text
RUNNING
→ CANCEL_REQUESTED
→ CANCELLED
```

Do not jump directly from a running execution to `CANCELLED` if cancellation is asynchronous.

The execution engine must handle in-flight Steps according to the cancellation policy.

---

# 43. Retry

Retry safety depends on operation semantics.

Safe retry is not determined only by HTTP failure.

Pay attention to:

```text
risk_class
idempotency
Step status
UNKNOWN_OUTCOME
external side effects
```

Retry may create a new Attempt or new Execution according to the documented design.

Keep those concepts explicit.

---

# 44. Testing

`docs/09-test-strategy.md` is the testing Source of Truth.

All meaningful changes should include or update tests appropriate to the layer.

Frontend target tooling includes:

```text
TypeScript
Vitest
React Testing Library
Playwright
```

Backend tests should cover:

* domain lifecycle
* API contracts
* policy
* AgentRequest / Execution separation
* Plan validation
* Tool selection
* MRTR
* Approval
* retry
* timeout
* cancellation
* scheduling
* authorization
* audit

Contract tests should verify that frontend/backend/API enum values remain aligned with canonical definitions.

---

# 45. Required Validation Before Finishing Work

For frontend changes, run at minimum:

```bash
cd frontend
pnpm exec tsc --noEmit
pnpm build
```

Once frontend tests are introduced, also run the configured unit/component tests.

For backend changes, run the backend lint/type/test commands established by the project.

For infrastructure changes, validate configuration syntax and service names.

Never claim validation succeeded unless it was actually executed.

If a validation cannot be executed, report that explicitly.

---

# 46. Change Scope

Prefer focused changes.

Do not combine unrelated:

```text
feature implementation
architecture redesign
dependency upgrade
large formatting rewrite
documentation rewrite
```

into one task unless specifically requested.

Avoid modifying files outside the requested scope.

Do not perform opportunistic refactors that obscure the requested change.

---

# 47. Documentation Changes

When code changes a canonical contract, update documentation in the same change.

When code merely implements an existing contract, do not rewrite canonical docs unnecessarily.

Preserve existing terminology.

Do not rename established project concepts simply for stylistic preference.

---

# 48. Repository and Generated Files

Do not commit:

* secrets
* local credentials
* local environment files containing secrets
* build output
* temporary artifacts
* editor caches

Do not reintroduce deleted Figma Make runtime files.

Generated code must still comply with repository contracts and security constraints.

---

# 49. Git / Pull Requests

Use a focused branch/PR per logical change.

Do not automatically merge unless explicitly requested.

Before presenting a PR as complete:

* inspect the diff
* run relevant validation
* summarize important contract impact
* mention any known limitations
* do not claim tests that were not run

For significant changes, include in the PR summary:

```text
What changed
Why
Canonical docs used
Validation performed
Known limitations / out-of-scope items
```

---

# 50. Agent Working Method

Before implementing a non-trivial task:

1. Read this `AGENTS.md`.
2. Inspect the relevant canonical docs.
3. Inspect existing code before designing replacement code.
4. Identify which layer owns the behavior.
5. Reuse canonical types/contracts.
6. Make the smallest coherent change.
7. Run validation.
8. Review the final diff against the requested scope.

Do not assume an existing implementation is canonical merely because it already exists.

Do not assume a mock value is a Domain contract.

Do not silently reinterpret ambiguous architecture.

When uncertain about a canonical value, search the docs before coding.

---

# 51. Nested AGENTS.md Files

A subdirectory may later define an additional `AGENTS.md`.

The nearest applicable `AGENTS.md` may add implementation-specific rules.

A nested file must not silently weaken or contradict repository-wide canonical contracts.

For example, a future:

```text
frontend/AGENTS.md
backend/AGENTS.md
infra/AGENTS.md
```

may define local coding conventions, but must still obey this root file and `docs/01~09`.

---

# 52. Non-Negotiable Guardrails

The following rules are especially important:

```text
Do not invent canonical states in code.

Do not merge AgentRequest and Execution lifecycles.

Do not let Agent Runtime directly execute Tools.

Do not create an authorable USER_INPUT Step.

Do not persist PARALLEL or END as Plan Step Types.

Do not allow arbitrary JavaScript/Python expressions in Workflow conditions or bindings.

Do not mutate PUBLISHED AgentVersion/WorkflowVersion.

Do not expose or reinterpret MRTR requestState.

Do not expose raw secrets.

Do not allow arbitrary STDIO shell commands.

Do not auto-retry UNKNOWN_OUTCOME or unsafe non-idempotent operations.

Do not use implicit latest versions for Schedules.

Do not treat frontend PermissionGate as authorization.

Do not reintroduce Figma Make runtime dependencies.

Do not bypass docs/04 and docs/05 when adding a new Domain enum.
```

If implementation pressure conflicts with one of these rules, stop and resolve the canonical design first.
