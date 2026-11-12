import { beforeEach, describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import WorkflowVisualEditor, {
  addConditionStepToPlan,
  addGuidedToolStepToPlan,
  addStepToPlan,
  causesDependencyCycle,
  getResponseStepIds,
  getVisualSteps,
  setResponseStepInPlan,
  setStepBranchWhenInPlan,
  updateStepInPlan,
} from "@/screens/build/WorkflowVisualEditor"
import {
  branchConditionSources,
  simpleBranchWhen,
} from "@/screens/build/workflowGraphModel"
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

  it("shows one step-creation section at a time and preserves unfinished selections", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const plan = {
      ...basePlan(),
      inputs: { topic: { type: "string", required: true, secret: false } },
    } as Plan
    render(<WorkflowVisualEditor plan={plan} readOnly={false} onChange={onChange} />)

    expect(screen.getByRole("button", { name: "도구" })).toHaveAttribute("aria-pressed", "true")
    expect(screen.getByLabelText("사용할 MCP 도구")).toBeInTheDocument()
    expect(screen.queryByLabelText("새 조건의 판단 대상")).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "합류 단계 추가" })).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: /완료 응답 설정/ })).toHaveAttribute("aria-expanded", "false")

    await screen.findByRole("option", { name: /날씨 확인/ })
    await user.selectOptions(screen.getByLabelText("사용할 MCP 도구"), activeTool.id)
    await user.click(screen.getByRole("button", { name: "조건" }))
    expect(screen.getByRole("button", { name: "조건" })).toHaveAttribute("aria-pressed", "true")
    await user.selectOptions(screen.getByLabelText("새 조건의 판단 대상"), "input:topic")
    await user.click(screen.getByRole("button", { name: "합류" }))
    expect(screen.getByRole("button", { name: "합류 단계 추가" })).toBeDisabled()
    expect(screen.queryByLabelText("도구 검색어")).not.toBeInTheDocument()

    await user.click(screen.getByRole("button", { name: "조건" }))
    expect(screen.getByLabelText("새 조건의 판단 대상")).toHaveValue("input:topic")
    await user.click(screen.getByRole("button", { name: "도구" }))
    expect(screen.getByLabelText("사용할 MCP 도구")).toHaveValue(activeTool.id)
    expect(onChange).not.toHaveBeenCalled()
  })

  it("collapses completion settings independently of step creation", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const plan = addStepToPlan(basePlan(), "TOOL", toolVersionId)
    render(<WorkflowVisualEditor plan={plan} readOnly={false} onChange={onChange} />)
    expect(screen.getByText(/응답 단계가 없습니다/)).toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: /완료 응답 설정/ }))
    expect(screen.getByRole("button", { name: /완료 응답 설정/ })).toHaveAttribute("aria-expanded", "true")
    expect(screen.getByRole("checkbox", { name: "응답 단계: 도구 실행 1 (tool_1)" })).toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: /완료 응답 설정/ }))
    expect(screen.queryByRole("checkbox", { name: "응답 단계: 도구 실행 1 (tool_1)" })).not.toBeInTheDocument()
    expect(onChange).not.toHaveBeenCalled()
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
    await user.click(screen.getByRole("button", { name: "조건" }))
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
    await user.click(screen.getByRole("button", { name: "조건" }))
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
    expect(screen.queryByRole("checkbox", { name: "응답 단계: 도구 실행 2 (tool_2)" })).not.toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: /완료 응답 설정/ }))
    expect(screen.getByText(/응답 단계가 지정되지 않았습니다/)).toBeInTheDocument()
    await user.click(screen.getByRole("checkbox", { name: "응답 단계: 도구 실행 2 (tool_2)" }))
    const selected = onChange.mock.lastCall?.[0] as Plan
    expect(getResponseStepIds(selected)).toEqual(["tool_2"])

    rerender(<WorkflowVisualEditor plan={selected} readOnly={false} onChange={onChange} />)
    expect(screen.getByRole("checkbox", { name: "응답 단계: 도구 실행 2 (tool_2)" })).toBeChecked()
    expect(screen.queryByText(/응답 단계가 지정되지 않았습니다/)).not.toBeInTheDocument()
    expect(screen.getByRole("status")).toHaveTextContent(/초안 저장 후 서버 검증/)
  })

  it("shows response selection read-only and blocks unsupported JSON structure", async () => {
    const user = userEvent.setup()
    const plan = addStepToPlan(basePlan(), "TOOL", toolVersionId)
    const onChange = vi.fn()
    const { rerender } = render(
      <WorkflowVisualEditor plan={plan} readOnly onChange={onChange} />,
    )
    await user.click(screen.getByRole("button", { name: /완료 응답 설정/ }))
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

  it("adds guided TOOL steps with canonical predecessors and true/false branches", () => {
    const base = addStepToPlan(basePlan(), "TOOL", toolVersionId)
    const condition = addConditionStepToPlan(base, "step:tool_1")
    const conditionId = getVisualSteps(condition)?.[1].id
    expect(conditionId).toBe("condition_2")

    const linked = addGuidedToolStepToPlan(base, toolVersionId, "tool_1", null, "날씨 확인")
    expect(getVisualSteps(linked)?.[1]).toMatchObject({
      name: "날씨 확인 실행", depends_on: ["tool_1"], when: null,
      config: { tool_version_id: toolVersionId, bindings: {} },
    })

    const truePlan = addGuidedToolStepToPlan(condition, toolVersionId, conditionId, true, "조회")
    const trueStep = getVisualSteps(truePlan)?.at(-1)
    expect(trueStep?.depends_on).toEqual([conditionId])
    expect(simpleBranchWhen(trueStep?.when)).toEqual({ conditionId, expected: true })

    const falsePlan = addGuidedToolStepToPlan(condition, toolVersionId, conditionId, false)
    expect(simpleBranchWhen(getVisualSteps(falsePlan)?.at(-1)?.when)).toEqual({
      conditionId, expected: false,
    })
    expect(falsePlan.inputs).toEqual(base.inputs)
    expect(falsePlan.completion).toEqual(base.completion)
    expect(condition.steps).toHaveLength(2)
    expect(addGuidedToolStepToPlan(condition, toolVersionId, conditionId, null)).toBe(condition)
    expect(addGuidedToolStepToPlan(condition, toolVersionId, "missing", true)).toBe(condition)
    expect(addGuidedToolStepToPlan(condition, "", "tool_1")).toBe(condition)
    expect(addGuidedToolStepToPlan(base, toolVersionId, "tool_1", true)).toBe(base)

    const scoped: Plan = { ...basePlan(), steps: [
      { id: "loop", name: "반복", type: "LOOP", depends_on: [], config: { body_step_ids: ["inside"] } },
      { id: "inside", name: "내부", type: "TOOL", depends_on: [], config: {} },
    ] }
    expect(addGuidedToolStepToPlan(scoped, toolVersionId, "loop")).toBe(scoped)
    expect(addGuidedToolStepToPlan(scoped, toolVersionId, "inside")).toBe(scoped)
  })

  it("guides a non-expert through adding a true-branch tool without editing JSON", async () => {
    const user = userEvent.setup()
    const plan = addConditionStepToPlan(
      addStepToPlan(basePlan(), "TOOL", toolVersionId), "step:tool_1",
    )
    const onChange = vi.fn()
    render(<WorkflowVisualEditor plan={plan} readOnly={false} onChange={onChange} />)
    await screen.findByRole("option", { name: /날씨 확인/ })
    await user.selectOptions(screen.getByLabelText("사용할 MCP 도구"), activeTool.id)
    await user.selectOptions(screen.getByLabelText("이전에 실행할 단계 (선택)"), "condition_2")
    expect(screen.getByRole("button", { name: "도구 단계 추가" })).toBeDisabled()
    await user.selectOptions(screen.getByLabelText("어떤 조건 결과에서 실행할까요?"), "true")
    await user.click(screen.getByRole("button", { name: "도구 단계 추가" }))
    const updated = onChange.mock.lastCall?.[0] as Plan
    const step = getVisualSteps(updated)?.at(-1)
    expect(step?.depends_on).toEqual(["condition_2"])
    expect(simpleBranchWhen(step?.when)).toEqual({ conditionId: "condition_2", expected: true })
    expect(step?.name).toBe("날씨 확인 실행")
    expect(screen.getByLabelText("이전에 실행할 단계 (선택)")).toHaveValue("")
    expect(screen.queryByLabelText("어떤 조건 결과에서 실행할까요?")).not.toBeInTheDocument()
  })

  it("searches active tools server-side and pages through results", async () => {
    const user = userEvent.setup()
    const secondTool = {
      ...activeTool,
      id: "55555555-5555-4555-8555-555555555555",
      remote_name: "list_tasks",
      display_name: "업무 목록",
      current_version_id: "66666666-6666-4666-8666-666666666666",
    }
    vi.mocked(listMCPTools).mockImplementation(async (params) => ({
      items: params?.page === 2 ? [secondTool] : [activeTool],
      page: params?.page ?? 1,
      page_size: 100,
      total: 101,
      has_next: params?.page === 1,
    }))
    const onChange = vi.fn()
    render(<WorkflowVisualEditor plan={basePlan()} readOnly={false} onChange={onChange} />)
    await waitFor(() => expect(screen.getByRole("button", { name: "다음 도구 페이지" })).toBeEnabled())
    await user.click(screen.getByRole("button", { name: "다음 도구 페이지" }))
    await waitFor(() => expect(listMCPTools).toHaveBeenCalledWith(
      expect.objectContaining({ page: 2, status: "ACTIVE" }),
    ))
    await screen.findByRole("option", { name: /업무 목록/ })
    await user.selectOptions(screen.getByLabelText("사용할 MCP 도구"), secondTool.id)
    await user.click(screen.getByRole("button", { name: "도구 단계 추가" }))
    expect(getVisualSteps(onChange.mock.lastCall?.[0] as Plan)?.[0].config).toEqual({
      tool_version_id: secondTool.current_version_id, bindings: {},
    })
    await user.type(screen.getByLabelText("도구 검색어"), "날씨")
    await user.click(screen.getByRole("button", { name: "검색" }))
    await waitFor(() => expect(listMCPTools).toHaveBeenCalledWith(
      expect.objectContaining({ page: 1, q: "날씨", status: "ACTIVE" }),
    ))
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

  it("edits only canonical Boolean CONDITION gates while preserving Plan and config", () => {
    const plan: Plan = {
      ...basePlan(),
      marker: { keep: "yes" },
      steps: [
        { id: "probe", name: "조회", type: "TOOL", depends_on: [], config: { bindings: {} } },
        { id: "cond", name: "조건", type: "CONDITION", depends_on: ["probe"], config: { predicate: { op: "exists", operand: { kind: "STEP_OUTPUT", step_id: "probe", path: "/" } } } },
        { id: "branch", name: "후속", type: "TOOL", depends_on: ["cond"], when: null, config: { bindings: { topic: { kind: "LITERAL", value: "x" } }, custom: 15 } },
      ],
    }
    expect(branchConditionSources(getVisualSteps(plan)!, "branch").map((item) => item.id)).toEqual(["cond"])
    const whenTrue = setStepBranchWhenInPlan(plan, "branch", "cond")
    const expected = {
      op: "eq",
      left: { kind: "STEP_OUTPUT", step_id: "cond", path: "/condition_result" },
      right: { kind: "LITERAL", value: true },
    }
    expect(getVisualSteps(whenTrue)?.[2].when).toEqual(expected)
    expect(simpleBranchWhen(getVisualSteps(whenTrue)?.[2].when)).toEqual({ conditionId: "cond", expected: true })
    expect(whenTrue.marker).toEqual(plan.marker)
    expect(getVisualSteps(whenTrue)?.[2].config).toEqual(getVisualSteps(plan)?.[2].config)
    expect(plan.steps).not.toEqual(whenTrue.steps)
    expect(setStepBranchWhenInPlan(whenTrue, "branch", "cond")).toBe(whenTrue)

    const whenFalse = setStepBranchWhenInPlan(whenTrue, "branch", "cond", false)
    expect(getVisualSteps(whenFalse)?.[2].when).toEqual({
      ...expected, right: { kind: "LITERAL", value: false },
    })
    expect(getVisualSteps(setStepBranchWhenInPlan(whenFalse, "branch", null))?.[2].when).toBeNull()
    expect(setStepBranchWhenInPlan(plan, "branch", "probe")).toBe(plan)
    expect(setStepBranchWhenInPlan(plan, "probe", "cond")).toBe(plan)
  })

  it("does not rewrite advanced/malformed/foreign when or LOOP body gates", () => {
    const plan: Plan = {
      ...basePlan(),
      steps: [
        { id: "cond", name: "조건", type: "CONDITION", depends_on: [], config: { predicate: { op: "exists", operand: { kind: "LITERAL", value: "x" } } } },
        { id: "advanced", name: "복합", type: "TOOL", depends_on: ["cond"], when: { op: "and", children: [{ op: "exists", operand: { kind: "PLAN_INPUT", path: "/x" } }] }, config: {} },
        { id: "extra", name: "확장", type: "TOOL", depends_on: ["cond"], when: { op: "eq", left: { kind: "STEP_OUTPUT", step_id: "cond", path: "/condition_result" }, right: { kind: "LITERAL", value: true }, future: 1 }, config: {} },
        { id: "foreign", name: "외부", type: "TOOL", depends_on: [], when: { op: "eq", left: { kind: "STEP_OUTPUT", step_id: "cond", path: "/condition_result" }, right: { kind: "LITERAL", value: true } }, config: {} },
        { id: "loop", name: "반복", type: "LOOP", depends_on: [], config: { body_step_ids: ["inside"] } },
        { id: "inside", name: "반복 본문", type: "TOOL", depends_on: ["cond"], when: null, config: {} },
      ],
    }
    for (const id of ["advanced", "extra", "foreign", "inside", "loop"]) {
      expect(setStepBranchWhenInPlan(plan, id, "cond")).toBe(plan)
      expect(setStepBranchWhenInPlan(plan, id, null)).toBe(plan)
    }
    expect(simpleBranchWhen(getVisualSteps(plan)?.[2].when)).toBeNull()
    expect(branchConditionSources(getVisualSteps(plan)!, "inside")).toEqual([])
  })

  it("sets true/false branch in the visual editor and shows it on the graph", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const plan: Plan = {
      ...basePlan(),
      steps: [
        { id: "cond", name: "조건 판단", type: "CONDITION", depends_on: [], config: { predicate: { op: "exists", operand: { kind: "LITERAL", value: "x" } } } },
        { id: "branch", name: "후속 도구", type: "TOOL", depends_on: ["cond"], when: null, config: { bindings: {} } },
      ],
    }
    const { rerender } = render(<WorkflowVisualEditor plan={plan} readOnly={false} onChange={onChange} />)
    await user.click(screen.getByRole("button", { name: "목록" }))
    await user.click(screen.getByRole("button", { name: "후속 도구 단계 선택" }))
    expect(screen.getByLabelText("조건 기준 단계")).toHaveValue("")
    await user.selectOptions(screen.getByLabelText("조건 기준 단계"), "cond")
    let current = onChange.mock.lastCall?.[0] as Plan
    expect(simpleBranchWhen(getVisualSteps(current)?.[1].when)?.expected).toBe(true)
    rerender(<WorkflowVisualEditor plan={current} readOnly={false} onChange={onChange} />)
    await user.selectOptions(screen.getByLabelText("조건 결과"), "false")
    current = onChange.mock.lastCall?.[0] as Plan
    expect(simpleBranchWhen(getVisualSteps(current)?.[1].when)?.expected).toBe(false)
    rerender(<WorkflowVisualEditor plan={current} readOnly={false} onChange={onChange} />)
    expect(screen.getByLabelText("조건 결과")).toHaveValue("false")
    await user.click(screen.getByRole("button", { name: "그래프" }))
    expect(await screen.findByText("조건 판단: 거짓일 때 실행")).toBeInTheDocument()

    await user.selectOptions(screen.getByLabelText("조건 기준 단계"), "")
    current = onChange.mock.lastCall?.[0] as Plan
    expect(getVisualSteps(current)?.[1].when).toBeNull()
  })

  it("keeps advanced when read-only and protects branch ancestry on disconnect", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const plan: Plan = {
      ...basePlan(),
      steps: [
        { id: "cond", name: "조건", type: "CONDITION", depends_on: [], config: { predicate: { op: "exists", operand: { kind: "LITERAL", value: true } } } },
        { id: "advanced", name: "복합", type: "TOOL", depends_on: ["cond"], when: { op: "not", child: { op: "exists", operand: { kind: "PLAN_INPUT", path: "/topic" } } }, config: {} },
        { id: "simple", name: "기본", type: "TOOL", depends_on: ["cond"], when: { op: "eq", left: { kind: "STEP_OUTPUT", step_id: "cond", path: "/condition_result" }, right: { kind: "LITERAL", value: false } }, config: {} },
      ],
    }
    render(<WorkflowVisualEditor plan={plan} readOnly={false} onChange={onChange} />)
    await user.click(screen.getByRole("button", { name: "목록" }))
    await user.click(screen.getByRole("button", { name: "복합 단계 선택" }))
    expect(screen.getByText(/기존 복합·특수 when/)).toBeInTheDocument()
    expect(screen.queryByLabelText("조건 기준 단계")).not.toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: "기본 단계 선택" }))
    expect(screen.getByLabelText("조건 결과")).toHaveValue("false")
    const dependencies = within(screen.getByRole("group", { name: "선행 단계 선택" }))
    await user.click(dependencies.getByRole("checkbox", { name: "조건 (cond)" }))
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByRole("status")).toHaveTextContent(/STEP_OUTPUT/)
  })

  it("shows existing branch details without allowing published-version edits", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const base: Plan = {
      ...basePlan(),
      steps: [
        { id: "cond", name: "조건", type: "CONDITION", depends_on: [], config: {} },
        { id: "branch", name: "후속", type: "TOOL", depends_on: ["cond"], when: { op: "eq", left: { kind: "STEP_OUTPUT", step_id: "cond", path: "/condition_result" }, right: { kind: "LITERAL", value: false } }, config: {} },
      ],
    }
    render(<WorkflowVisualEditor plan={base} readOnly onChange={onChange} />)
    await user.click(screen.getByRole("button", { name: "목록" }))
    await user.click(screen.getByRole("button", { name: "후속 단계 선택" }))
    expect(screen.getByLabelText("조건 기준 단계")).toBeDisabled()
    expect(screen.getByLabelText("조건 결과")).toBeDisabled()
    expect(onChange).not.toHaveBeenCalled()
  })
})
