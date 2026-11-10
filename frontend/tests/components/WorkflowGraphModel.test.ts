import { describe, expect, it } from "vitest"
import {
  connectionProblem,
  hasDirectStepOutputReference,
  layoutGraphSteps,
  wouldBreakStepOutputAncestry,
  type GraphStep,
} from "@/screens/build/workflowGraphModel"

const steps: GraphStep[] = [
  { id: "tool_probe", name: "조회", type: "TOOL", depends_on: [] },
  {
    id: "condition",
    name: "조건",
    type: "CONDITION",
    depends_on: ["tool_probe"],
  },
  { id: "branch_true", name: "참", type: "TOOL", depends_on: ["condition"] },
  { id: "branch_false", name: "거짓", type: "TOOL", depends_on: ["condition"] },
  {
    id: "join",
    name: "합류",
    type: "JOIN",
    depends_on: ["branch_true", "branch_false"],
  },
]

describe("canonical Workflow React Flow graph projection", () => {
  it("layouts the same DAG layers without inventing persisted steps or types", () => {
    const locations = layoutGraphSteps(steps)
    expect(locations.get("tool_probe")!.x).toBeLessThan(
      locations.get("condition")!.x,
    )
    expect(locations.get("condition")!.x).toBeLessThan(
      locations.get("branch_true")!.x,
    )
    expect(locations.get("branch_true")!.x).toBe(
      locations.get("branch_false")!.x,
    )
    expect(locations.get("branch_true")!.y).not.toBe(
      locations.get("branch_false")!.y,
    )
    expect(locations.get("join")!.x).toBeGreaterThan(
      locations.get("branch_true")!.x,
    )
    expect(steps.map((step) => step.type)).toEqual([
      "TOOL",
      "CONDITION",
      "TOOL",
      "TOOL",
      "JOIN",
    ])
  })

  it("rejects self/duplicate/cycle/missing nodes and direct TOOL fan-in", () => {
    expect(connectionProblem(steps, "condition", "condition")).toMatch(
      /자기 자신/,
    )
    expect(connectionProblem(steps, "tool_probe", "condition")).toMatch(
      /이미 존재/,
    )
    expect(connectionProblem(steps, "join", "tool_probe")).toMatch(/순환/)
    expect(connectionProblem(steps, "missing", "condition")).toMatch(/존재하지/)
    expect(connectionProblem(steps, "tool_probe", "branch_true")).toMatch(
      /다중 선행/,
    )
    expect(connectionProblem(steps, "tool_probe", "join")).toBeNull()
  })

  it("protects LOOP scope and recognizes nested STEP_OUTPUT references", () => {
    const withLoop = [
      ...steps,
      {
        id: "loop", name: "반복", type: "LOOP", depends_on: [],
        config: { body_step_ids: ["loop_body"] },
      },
      { id: "loop_body", name: "반복 본문", type: "TOOL", depends_on: ["loop"] },
    ]
    expect(connectionProblem(withLoop, "loop", "join")).toMatch(/LOOP/)
    expect(connectionProblem(withLoop, "join", "loop")).toMatch(/LOOP/)
    expect(connectionProblem(withLoop, "loop_body", "join")).toMatch(/LOOP/)
    expect(connectionProblem(withLoop, "tool_probe", "loop_body")).toMatch(/LOOP/)
    expect(
      hasDirectStepOutputReference(
        {
          config: {
            predicate: {
              children: [
                {
                  left: {
                    kind: "STEP_OUTPUT",
                    step_id: "condition",
                    path: "/result",
                  },
                },
              ],
            },
          },
        },
        "condition",
      ),
    ).toBe(true)
    expect(
      hasDirectStepOutputReference(
        { kind: "LITERAL", value: "condition" },
        "condition",
      ),
    ).toBe(false)
  })

  it("protects transitive STEP_OUTPUT ancestry when an edge is removed", () => {
    const plan: GraphStep[] = [
      { id: "a", name: "A", type: "TOOL", depends_on: [] },
      { id: "b", name: "B", type: "TOOL", depends_on: ["a"] },
      {
        id: "c", name: "C", type: "TOOL", depends_on: ["b"],
        config: {
          bindings: { topic: { kind: "STEP_OUTPUT", step_id: "a", path: "/topic" } },
        },
      },
    ]
    expect(wouldBreakStepOutputAncestry(plan, "a", "b")).toBe(true)
    expect(wouldBreakStepOutputAncestry(plan, "b", "c")).toBe(true)
    expect(wouldBreakStepOutputAncestry(plan, "a", "c")).toBe(false)

    const withAlternatePath: GraphStep[] = [
      ...plan.slice(0, 2),
      { id: "d", name: "D", type: "TOOL", depends_on: ["a"] },
      { ...plan[2], type: "JOIN", depends_on: ["b", "d"] },
    ]
    expect(wouldBreakStepOutputAncestry(withAlternatePath, "a", "b")).toBe(false)
  })

  it("does not hang on an already-invalid cyclic source Plan", () => {
    const invalid: GraphStep[] = [
      { id: "a", name: "A", type: "TOOL", depends_on: ["b"] },
      { id: "b", name: "B", type: "TOOL", depends_on: ["a"] },
    ]
    expect(layoutGraphSteps(invalid).size).toBe(2)
  })
})
