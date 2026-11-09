import { beforeEach, describe, expect, it, vi } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import WorkflowVisualEditor, {
  addStepToPlan,
  causesDependencyCycle,
  getVisualSteps,
  updateStepInPlan,
} from "@/screens/build/WorkflowVisualEditor"
import { listMCPTools } from "@/api/mcp"
import type { JsonValue, MCPToolDto } from "@/api/types"

vi.mock("@/api/mcp", () => ({ listMCPTools: vi.fn() }))

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
    await user.click(screen.getByRole("button", { name: /도구 실행/ }))
    expect(screen.getByRole("textbox", { name: "단계 이름" })).toBeDisabled()
    expect(onChange).not.toHaveBeenCalled()
  })
})
