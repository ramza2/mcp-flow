import { beforeEach, describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import WorkflowVisualEditor, {
  addConditionStepToPlan,
  addStepToPlan,
  causesDependencyCycle,
  getResponseStepIds,
  getVisualSteps,
  setResponseStepInPlan,
  updateStepInPlan,
} from "@/screens/build/WorkflowVisualEditor"
import { listMCPTools } from "@/api/mcp"
import type { JsonValue, MCPToolDto } from "@/api/types"

vi.mock("@/api/mcp", () => ({ listMCPTools: vi.fn() }))

// React Flow measures nodes in the browser; jsdom needs a no-op observer.
vi.stubGlobal(
  "ResizeObserver",
  class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
)

type Plan = Record<string, JsonValue>

const toolVersionId = "33333333-3333-4333-8333-333333333333"
const activeTool: MCPToolDto = {
  id: "22222222-2222-4222-8222-222222222222",
  mcp_server_id: "11111111-1111-4111-8111-111111111111",
  remote_name: "get_weather",
  display_name: "날씨 확인",
  description_override: null,
  tags: [],
  status: "ACTIVE",
  current_version_id: toolVersionId,
  first_seen_at: "2026-11-09",
  last_seen_at: "2026-11-09",
  created_at: "2026-11-09",
  updated_at: "2026-11-09",
  lock_version: 1,
}

function basePlan(): Plan {
  return {
    schema_version: "1.0",
    goal: "테스트 워크플로",
    source: {
      type: "WORKFLOW",
      workflow_id: "11111111-1111-4111-8111-111111111111",
    },
    inputs: {},
    limits: {
      max_steps: 20,
      max_duration_seconds: 300,
      max_parallelism: 4,
      max_loop_iterations: 50,
    },
    steps: [],
    completion: { success_policy: "ALL_REQUIRED", response_step_ids: [] },
  }
}

describe("Canonical visual workflow editor", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(listMCPTools).mockResolvedValue({
      items: [activeTool],
      page: 1,
      page_size: 100,
      total: 1,
      has_next: false,
    })
  })

  it("adds canonical TOOL/JOIN without introducing persisted PARALLEL or END", () => {
    const base = basePlan()
    const one = addStepToPlan(base, "TOOL", toolVersionId)
    const two = addStepToPlan(one, "JOIN")
    expect(getVisualSteps(two)?.map((step) => step.type)).toEqual([
      "TOOL",
      "JOIN",
    ])
    expect(getVisualSteps(two)?.[0].config).toEqual({
      tool_version_id: toolVersionId,
      bindings: {},
    })
    expect(getVisualSteps(two)?.[1].config).toEqual({ policy: "ALL_SUCCESS" })
    expect(two.source).toEqual(base.source)
    expect(two.completion).toEqual(base.completion)
    expect(base.steps).toEqual([])
  })

  it("adds a CONDITION only from an explicit non-secret source and preserves the Plan", () => {
    const base: Plan = {
      ...basePlan(),
      inputs: {
        "user/topic": { type: "string", required: true, secret: false },
        token: { type: "string", required: true, secret: true },
      },
      custom: { keep: true },
    }
    expect(addConditionStepToPlan(base, "")).toBe(base)
    expect(addConditionStepToPlan(base, "input:token")).toBe(base)
    const withInput = addConditionStepToPlan(base, "input:user/topic")
    expect(getVisualSteps(withInput)?.[0]).toMatchObject({
      id: "condition_1",
      type: "CONDITION",
      depends_on: [],
      config: { predicate: { op: "exists", operand: { kind: "PLAN_INPUT", path: "/user~1topic" } } },
    })
    expect(withInput.custom).toEqual(base.custom)
    expect(base.steps).toEqual([])

    const withTool = addStepToPlan(base, "TOOL", toolVersionId)
    const withOutput = addConditionStepToPlan(withTool, "step:tool_1")
    expect(getVisualSteps(withOutput)?.at(-1)).toMatchObject({
      type: "CONDITION",
      depends_on: ["tool_1"],
      config: { predicate: { op: "exists", operand: { kind: "STEP_OUTPUT", step_id: "tool_1", path: "/" } } },
    })
    const scoped: Plan = {
      ...withTool,
      steps: [
        { id: "loop", name: "반복", type: "LOOP", depends_on: [], config: { body_step_ids: ["tool_1"] } },
        ...getVisualSteps(withTool)!,
      ],
    }
    expect(addConditionStepToPlan(scoped, "step:tool_1")).toBe(scoped)
  })

  it("edits only a simple CONDITION predicate and leaves when/extra Plan fields unchanged", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const plan: Plan = {
      ...basePlan(),
      inputs: { topic: { type: "string", required: true, secret: false } },
      marker: { preserved: true },
    }
    const { rerender } = render(
      <WorkflowVisualEditor plan={plan} readOnly={false} onChange={onChange} />,
    )
    expect(screen.getByRole("button", { name: "조건 단계 추가" })).toBeDisabled()
    await user.selectOptions(screen.getByLabelText("새 조건의 판단 대상"), "input:topic")
    await user.click(screen.getByRole("button", { name: "조건 단계 추가" }))
    let current = onChange.mock.lastCall?.[0] as Plan
    const conditionId = getVisualSteps(current)?.[0].id
    expect(conditionId).toBe("condition_1")
    rerender(<WorkflowVisualEditor plan={current} readOnly={false} onChange={onChange} />)
    await user.selectOptions(screen.getByLabelText("판단 방법"), "eq")
    current = onChange.mock.lastCall?.[0] as Plan
    rerender(<WorkflowVisualEditor plan={current} readOnly={false} onChange={onChange} />)
    fireEvent.change(screen.getByLabelText("비교할 문자열"), { target: { value: "ready" } })
    current = onChange.mock.lastCall?.[0] as Plan
    rerender(<WorkflowVisualEditor plan={current} readOnly={false} onChange={onChange} />)
    expect(getVisualSteps(current)?.[0].config).toEqual({
      predicate: { op: "eq", left: { kind: "PLAN_INPUT", path: "/topic" }, right: { kind: "LITERAL", value: "ready" } },
    })
    fireEvent.change(screen.getByLabelText("데이터 경로 (JSON Pointer)"), { target: { value: "/topic/status" } })
    current = onChange.mock.lastCall?.[0] as Plan
    expect(current.marker).toEqual(plan.marker)
    expect(getVisualSteps(current)?.[0].when).toBeNull()
    expect(getVisualSteps(current)?.[0].config).toMatchObject({
      predicate: { op: "eq", left: { kind: "PLAN_INPUT", path: "/topic/status" }, right: { kind: "LITERAL", value: "ready" } },
    })
  })

  it("never rewrites complex or typed CONDITION predicates from visual controls", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const plan: Plan = {
      ...basePlan(),
      steps: [
        { id: "complex", name: "복합", type: "CONDITION", depends_on: [], config: { predicate: { op: "and", children: [{ op: "exists", operand: { kind: "PLAN_INPUT", path: "/a" } }] } } },
        { id: "typed", name: "숫자", type: "CONDITION", depends_on: [], config: { predicate: { op: "eq", left: { kind: "PLAN_INPUT", path: "/a" }, right: { kind: "LITERAL", value: 2 } } } },
      ],
    }
    render(<WorkflowVisualEditor plan={plan} readOnly={false} onChange={onChange} />)
    await user.click(screen.getByRole("button", { name: "목록" }))
    await user.click(screen.getByRole("button", { name: "복합 단계 선택" }))
    expect(screen.getByText(/복합 조건식·특수 바인딩/)).toBeInTheDocument()
    expect(screen.queryByLabelText("판단 방법")).not.toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: "숫자 단계 선택" }))
    expect(screen.getByText(/복합 조건식·특수 바인딩/)).toBeInTheDocument()
    expect(onChange).not.toHaveBeenCalled()
  })

  it("shows simple CONDITION settings read-only on a published version", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const plan: Plan = {
      ...basePlan(),
      inputs: { topic: { type: "string", required: true, secret: false } },
    }
    const added = addConditionStepToPlan(plan, "input:topic")
    render(<WorkflowVisualEditor plan={added} readOnly onChange={onChange} />)
    expect(screen.getByRole("button", { name: "조건 단계 추가" })).toBeDisabled()
    await user.click(screen.getByRole("button", { name: "목록" }))
    await user.click(screen.getByRole("button", { name: "조건 분기 1 단계 선택" }))
    expect(screen.getByLabelText("판단 방법")).toBeDisabled()
    expect(screen.getByLabelText("판단 대상")).toBeDisabled()
    expect(screen.getByLabelText("데이터 경로 (JSON Pointer)")).toBeDisabled()
    expect(onChange).not.toHaveBeenCalled()
  })

  it("patches response_step_ids only and preserves completion and Plan metadata", () => {
    const base = addStepToPlan(addStepToPlan(basePlan(), "TOOL", toolVersionId), "TOOL", toolVersionId)
    const plan: Plan = {
      ...base,
      completion: {
        success_policy: "ALL_REQUIRED",
        response_step_ids: ["tool_1"],
        custom_metadata: { retain: true },
      },
      extensions: { also_retain: true },
    }
    const selected = setResponseStepInPlan(plan, "tool_2", true)
    expect(getResponseStepIds(selected)).toEqual(["tool_1", "tool_2"])
    expect(selected.extensions).toEqual(plan.extensions)
    expect((selected.completion as Record<string, JsonValue>).custom_metadata).toEqual({ retain: true })
    expect(selected.steps).toEqual(plan.steps)
    expect(setResponseStepInPlan(selected, "tool_1", false)).toMatchObject({
      completion: { response_step_ids: ["tool_2"] },
    })
    expect(setResponseStepInPlan(plan, "missing", true)).toBe(plan)
    expect(setResponseStepInPlan(plan, "tool_1", true)).toBe(plan)
  })

  it("fails closed for malformed completion and protects LOOP body templates", () => {
    const plan: Plan = {
      ...basePlan(),
      steps: [
        { id: "loop", name: "반복", type: "LOOP", depends_on: [], config: { body_step_ids: ["inside"] } },
        { id: "inside", name: "본문", type: "TOOL", depends_on: [], config: { bindings: {} } },
        { id: "after", name: "완료", type: "TOOL", depends_on: ["loop"], config: { bindings: {} } },
      ],
    }
    expect(setResponseStepInPlan(plan, "inside", true)).toBe(plan)
    expect(getResponseStepIds(setResponseStepInPlan(plan, "after", true))).toEqual(["after"])

    const unsupported = { ...plan, completion: { response_step_ids: "after", future: true } }
    expect(getResponseStepIds(unsupported)).toBeNull()
    expect(setResponseStepInPlan(unsupported, "after", true)).toBe(unsupported)
  })

  it("lets users choose the final response step without editing JSON", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const plan = addStepToPlan(addStepToPlan(basePlan(), "TOOL", toolVersionId), "TOOL", toolVersionId)
    const { rerender } = render(
      <WorkflowVisualEditor plan={plan} readOnly={false} onChange={onChange} />,
    )
    expect(screen.getByText(/응답 단계가 지정되지 않았습니다/)).toBeInTheDocument()
    await user.click(screen.getByRole("checkbox", { name: "응답 단계: 도구 실행 2 (tool_2)" }))
    const selected = onChange.mock.lastCall?.[0] as Plan
    expect(getResponseStepIds(selected)).toEqual(["tool_2"])

    rerender(<WorkflowVisualEditor plan={selected} readOnly={false} onChange={onChange} />)
    expect(screen.getByRole("checkbox", { name: "응답 단계: 도구 실행 2 (tool_2)" })).toBeChecked()
    expect(screen.queryByText(/응답 단계가 지정되지 않았습니다/)).not.toBeInTheDocument()
    expect(screen.getByRole("status")).toHaveTextContent(/초안 저장 후 서버 검증/)
  })

  it("shows response selection read-only and blocks unsupported JSON structure", () => {
    const plan = addStepToPlan(basePlan(), "TOOL", toolVersionId)
    const onChange = vi.fn()
    const { rerender } = render(
      <WorkflowVisualEditor plan={plan} readOnly onChange={onChange} />,
    )
    expect(screen.getByRole("checkbox", { name: "응답 단계: 도구 실행 1 (tool_1)" })).toBeDisabled()
    rerender(
      <WorkflowVisualEditor
        plan={{ ...plan, completion: { response_step_ids: false } }}
        readOnly={false}
        onChange={onChange}
      />,
    )
    expect(screen.getByText(/완료 응답 구조를 읽을 수 없습니다/)).toBeInTheDocument()
    expect(screen.queryByRole("checkbox", { name: "응답 단계: 도구 실행 1 (tool_1)" })).not.toBeInTheDocument()
    expect(onChange).not.toHaveBeenCalled()
  })

  it("preserves unknown complex predicates, loop config and plan metadata on visual edits", () => {
    const plan: Plan = {
      ...basePlan(),
      steps: [
        {
          id: "condition_1",
          name: "조건",
          type: "CONDITION",
          depends_on: [],
          required: true,
          timeout_seconds: 30,
          on_error: "FAIL_EXECUTION",
          when: {
            op: "exists",
            operand: { kind: "PLAN_INPUT", path: "/topic" },
          },
          config: {
            predicate: {
              op: "and",
              children: [
                {
                  op: "exists",
                  operand: { kind: "PLAN_INPUT", path: "/topic" },
                },
                {
                  op: "eq",
                  left: { kind: "LITERAL", value: 1 },
                  right: { kind: "LITERAL", value: 1 },
                },
              ],
            },
          },
        },
      ],
      extensions: { should_be_preserved: true },
    }
    const updated = updateStepInPlan(plan, "condition_1", (step) => ({
      ...step,
      name: "새 이름",
    }))
    expect(getVisualSteps(updated)?.[0].name).toBe("새 이름")
    expect(getVisualSteps(updated)?.[0].config).toEqual(
      getVisualSteps(plan)?.[0].config,
    )
    expect(getVisualSteps(updated)?.[0].when).toEqual(
      getVisualSteps(plan)?.[0].when,
    )
    expect(updated.extensions).toEqual(plan.extensions)
  })

  it("rejects cycles and keeps original step IDs and executable semantics", () => {
    const a = addStepToPlan(basePlan(), "TOOL", toolVersionId)
    const b = addStepToPlan(a, "TOOL", toolVersionId)
    const steps = getVisualSteps(b)!
    const bWithParent = { ...steps[1], depends_on: [steps[0].id] }
    expect(
      causesDependencyCycle(
        [steps[0], bWithParent],
        steps[0].id,
        bWithParent.id,
      ),
    ).toBe(true)
    expect(
      causesDependencyCycle(
        [steps[0], bWithParent],
        bWithParent.id,
        steps[0].id,
      ),
    ).toBe(false)
  })

  it("requires an active tool version; adding and binding are local only until parent saves", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const { rerender } = render(
      <WorkflowVisualEditor
        plan={basePlan()}
        readOnly={false}
        onChange={onChange}
      />,
    )
    await waitFor(() =>
      expect(listMCPTools).toHaveBeenCalledWith(
        expect.objectContaining({ status: "ACTIVE" }),
      ),
    )
    expect(
      screen.getByRole("button", { name: "도구 단계 추가" }),
    ).toBeDisabled()
    await user.selectOptions(
      screen.getByLabelText("사용할 MCP 도구"),
      activeTool.id,
    )
    await user.click(screen.getByRole("button", { name: "도구 단계 추가" }))
    expect(onChange).toHaveBeenCalledTimes(1)
    const newPlan = onChange.mock.calls[0][0] as Plan
    expect(getVisualSteps(newPlan)?.[0].config).toEqual({
      tool_version_id: toolVersionId,
      bindings: {},
    })

    rerender(
      <WorkflowVisualEditor
        plan={newPlan}
        readOnly={false}
        onChange={onChange}
      />,
    )
    await user.type(
      screen.getByRole("textbox", { name: "새 바인딩 이름" }),
      "topic",
    )
    await user.click(screen.getByRole("button", { name: "추가" }))
    const withBinding = onChange.mock.lastCall?.[0] as Plan
    expect(getVisualSteps(withBinding)?.[0].config).toEqual({
      tool_version_id: toolVersionId,
      bindings: { topic: { kind: "LITERAL", value: "" } },
    })
  })

  it("never permits writes from the visual editor for a published version", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(
      <WorkflowVisualEditor
        plan={addStepToPlan(basePlan(), "TOOL", toolVersionId)}
        readOnly
        onChange={onChange}
      />,
    )
    expect(
      screen.getByRole("button", { name: "도구 단계 추가" }),
    ).toBeDisabled()
    await user.click(screen.getByRole("button", { name: "목록" }))
    await user.click(screen.getByRole("button", { name: /도구 실행/ }))
    expect(screen.getByRole("textbox", { name: "단계 이름" })).toBeDisabled()
    expect(onChange).not.toHaveBeenCalled()
  })

  it("applies the graph connection guards to right-hand dependency checkboxes", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const plan: Plan = {
      ...basePlan(),
      steps: [
        { id: "a", name: "첫 단계", type: "TOOL", depends_on: [], config: { bindings: {} } },
        { id: "b", name: "둘째 단계", type: "TOOL", depends_on: ["a"], config: { bindings: { input: { kind: "STEP_OUTPUT", step_id: "a", path: "/value" } } } },
        { id: "c", name: "셋째 단계", type: "TOOL", depends_on: [], config: { bindings: {} } },
      ],
    }
    render(<WorkflowVisualEditor plan={plan} readOnly={false} onChange={onChange} />)
    await user.click(screen.getByRole("button", { name: "목록" }))
    await user.click(screen.getByRole("button", { name: "둘째 단계 단계 선택" }))

    const dependencies = within(screen.getByRole("group", { name: "선행 단계 선택" }))
    await user.click(dependencies.getByRole("checkbox", { name: "셋째 단계 (c)" }))
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByRole("status")).toHaveTextContent(/다중 선행/)

    await user.click(dependencies.getByRole("checkbox", { name: "첫 단계 (a)" }))
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByRole("status")).toHaveTextContent(/STEP_OUTPUT/)
  })

  it("shows canonical branch nodes in graph mode without modifying the Plan", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const plan: Plan = {
      ...basePlan(),
      steps: [
        { id: "probe", name: "도구 조회", type: "TOOL", depends_on: [], config: {} },
        { id: "condition", name: "조건 판단", type: "CONDITION", depends_on: ["probe"], config: { predicate: { op: "exists", operand: { kind: "PLAN_INPUT", path: "/topic" } } } },
        { id: "true", name: "참 분기", type: "TOOL", depends_on: ["condition"], config: {} },
        { id: "false", name: "거짓 분기", type: "TOOL", depends_on: ["condition"], config: {} },
        { id: "join", name: "합류", type: "JOIN", depends_on: ["true", "false"], config: { policy: "ALL_COMPLETE" } },
      ],
    }
    render(<WorkflowVisualEditor plan={plan} readOnly={false} onChange={onChange} />)
    const graph = await screen.findByLabelText("워크플로 그래프")
    expect(within(graph).getByText("조건 판단")).toBeInTheDocument()
    expect(within(graph).getByText("참 분기")).toBeInTheDocument()
    expect(within(graph).getByText("거짓 분기")).toBeInTheDocument()
    expect(within(graph).getByText("합류")).toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: "목록" }))
    await user.click(screen.getByRole("button", { name: "그래프" }))
    expect(await screen.findByLabelText("워크플로 그래프")).toBeInTheDocument()
    expect(onChange).not.toHaveBeenCalled()
  })
})
