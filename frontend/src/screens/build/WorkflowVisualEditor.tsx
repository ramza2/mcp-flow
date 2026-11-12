import { lazy, Suspense, useEffect, useMemo, useState } from "react"
import {
  ChevronDown,
  GitBranch,
  Link2,
  Plus,
  ShieldAlert,
  Trash2,
  Wrench,
} from "lucide-react"
import { listMCPTools } from "../../api/mcp"
import type { MCPToolDto, JsonValue } from "../../api/types"
import { isAbortError } from "../../api/client"
import {
  branchConditionSources,
  connectionProblem,
  loopProtectedStepIds,
  simpleBranchWhen,
  wouldBreakStepOutputAncestry,
} from "./workflowGraphModel"

// Keep React Flow out of the initial application bundle until the editor opens.
const WorkflowGraphCanvas = lazy(() => import("./WorkflowGraphCanvas"))

type Plan = Record<string, JsonValue>
type Step = Record<string, JsonValue> & {
  id: string
  name: string
  type: string
  depends_on: string[]
}
type Binding = Record<string, JsonValue> & { kind: string }

const STEP_LABELS: Record<string, string> = {
  TOOL: "도구 실행",
  CONDITION: "조건 분기",
  JOIN: "병렬 합류",
  APPROVAL: "승인 대기",
  LOOP: "반복 실행",
}
const BINDING_KINDS = [
  "LITERAL",
  "PLAN_INPUT",
  "STEP_OUTPUT",
  "EXECUTION_CONTEXT",
  "LOOP_CONTEXT",
  "SECRET_REF",
]
const SIMPLE_CONDITION_OPS = ["exists", "is_null", "eq", "ne"] as const
type SimpleConditionOp = (typeof SIMPLE_CONDITION_OPS)[number]

function object(
  value: JsonValue | undefined,
): Record<string, JsonValue> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value
    : null
}

export function getVisualSteps(plan: Plan): Step[] | null {
  if (!Array.isArray(plan.steps)) return null
  const result: Step[] = []
  const ids = new Set<string>()
  for (const value of plan.steps) {
    const step = object(value)
    if (
      !step ||
      typeof step.id !== "string" ||
      typeof step.name !== "string" ||
      typeof step.type !== "string" ||
      !Array.isArray(step.depends_on) ||
      !step.depends_on.every((id) => typeof id === "string") ||
      ids.has(step.id)
    )
      return null
    ids.add(step.id)
    result.push(step as Step)
  }
  return result
}

export function causesDependencyCycle(
  steps: Step[],
  stepId: string,
  candidateId: string,
): boolean {
  const byId = new Map(steps.map((step) => [step.id, step]))
  const visit = (id: string, seen: Set<string>): boolean => {
    if (id === stepId) return true
    if (seen.has(id)) return false
    seen.add(id)
    return (byId.get(id)?.depends_on ?? []).some((parent) =>
      visit(parent, seen),
    )
  }
  return visit(candidateId, new Set())
}

export function addStepToPlan(
  plan: Plan,
  type: "TOOL" | "JOIN",
  toolVersionId?: string,
): Plan {
  const steps = getVisualSteps(plan)
  if (!steps || (type === "TOOL" && !toolVersionId)) return plan
  const prefix = type.toLowerCase()
  let ordinal = steps.length + 1
  while (steps.some((step) => step.id === `${prefix}_${ordinal}`)) ordinal++
  const id = `${prefix}_${ordinal}`
  const step: Step = {
    id,
    name: type === "TOOL" ? `도구 실행 ${ordinal}` : `합류 ${ordinal}`,
    type,
    required: true,
    depends_on: [],
    when: null,
    timeout_seconds: 30,
    on_error: "FAIL_EXECUTION",
    config:
      type === "TOOL"
        ? { tool_version_id: toolVersionId!, bindings: {} }
        : { policy: "ALL_SUCCESS" },
  }
  return { ...plan, steps: [...steps, step] }
}

function pointerToken(value: string): string {
  return value.replace(/~/g, "~0").replace(/\//g, "~1")
}

interface ConditionSource {
  id: string
  label: string
  binding: Binding
  dependsOn: string[]
}

function conditionSources(plan: Plan, steps: Step[]): ConditionSource[] {
  const inputs = object(plan.inputs) ?? {}
  const protectedIds = loopProtectedStepIds(steps)
  const inputSources = Object.entries(inputs)
    .filter(([name, definition]) => {
      const input = object(definition)
      return name.length > 0 && !name.includes("#") && input !== null &&
        typeof input.type === "string" && typeof input.required === "boolean" &&
        input.secret !== true
    })
    .map(([name]): ConditionSource => ({
      id: `input:${name}`,
      label: `실행 입력: ${name}`,
      binding: { kind: "PLAN_INPUT", path: `/${pointerToken(name)}` },
      dependsOn: [],
    }))
  const stepSources = steps
    .filter((step) =>
      ["TOOL", "CONDITION"].includes(step.type) &&
      !protectedIds.has(step.id),
    )
    .map((step): ConditionSource => ({
      id: `step:${step.id}`,
      label: `이전 단계 결과: ${step.name} (${step.id})`,
      binding: { kind: "STEP_OUTPUT", step_id: step.id, path: "/" },
      dependsOn: [step.id],
    }))
  return [...inputSources, ...stepSources]
}

/** An explicit source is required: never create an implicit always-true gate. */
export function addConditionStepToPlan(plan: Plan, sourceId: string): Plan {
  const steps = getVisualSteps(plan)
  if (!steps) return plan
  const source = conditionSources(plan, steps).find((item) => item.id === sourceId)
  if (!source) return plan
  let ordinal = steps.length + 1
  while (steps.some((step) => step.id === `condition_${ordinal}`)) ordinal++
  const step: Step = {
    id: `condition_${ordinal}`,
    name: `조건 분기 ${ordinal}`,
    type: "CONDITION",
    required: true,
    depends_on: source.dependsOn,
    when: null,
    timeout_seconds: 30,
    on_error: "FAIL_EXECUTION",
    config: { predicate: { op: "exists", operand: source.binding } },
  }
  return { ...plan, steps: [...steps, step] }
}

interface SimpleCondition {
  op: SimpleConditionOp
  source: Binding
  value: string
}

/** Only the small canonical subset is editable. Recursive/typed conditions remain intact. */
function simpleCondition(value: JsonValue | undefined): SimpleCondition | null {
  const predicate = object(value)
  if (!predicate || !SIMPLE_CONDITION_OPS.includes(predicate.op as SimpleConditionOp))
    return null
  const op = predicate.op as SimpleConditionOp
  const unary = op === "exists" || op === "is_null"
  const keys = Object.keys(predicate)
  if (!keys.every((key) => (unary ? ["op", "operand"] : ["op", "left", "right"]).includes(key)))
    return null
  const source = toBinding(unary ? predicate.operand : predicate.left)
  if (!source || !["PLAN_INPUT", "STEP_OUTPUT"].includes(source.kind) ||
      typeof source.path !== "string") return null
  const expectedSourceKeys = source.kind === "STEP_OUTPUT"
    ? ["kind", "step_id", "path"] : ["kind", "path"]
  if (!Object.keys(source).every((key) => expectedSourceKeys.includes(key)) ||
      (source.kind === "STEP_OUTPUT" && typeof source.step_id !== "string")) return null
  const right = unary ? null : toBinding(predicate.right)
  if (!unary && (!right || right.kind !== "LITERAL" ||
      typeof right.value !== "string" || Object.keys(right).some((key) => !["kind", "value"].includes(key))))
    return null
  return { op, source, value: right ? right.value as string : "" }
}

function simplePredicate(op: SimpleConditionOp, source: Binding, value: string): JsonValue {
  return op === "exists" || op === "is_null"
    ? { op, operand: source }
    : { op, left: source, right: { kind: "LITERAL", value } }
}

function ancestorStepIds(steps: Step[], stepId: string): Set<string> {
  const byId = new Map(steps.map((step) => [step.id, step]))
  const ancestors = new Set<string>()
  const visit = (id: string) => {
    if (ancestors.has(id) || id === stepId) return
    ancestors.add(id)
    for (const parent of byId.get(id)?.depends_on ?? []) visit(parent)
  }
  for (const parent of byId.get(stepId)?.depends_on ?? []) visit(parent)
  return ancestors
}

export function updateStepInPlan(
  plan: Plan,
  stepId: string,
  change: (step: Step) => Step,
): Plan {
  const steps = getVisualSteps(plan)
  if (!steps) return plan
  return {
    ...plan,
    steps: steps.map((step) => (step.id === stepId ? change(step) : step)),
  }
}

export function getResponseStepIds(plan: Plan): string[] | null {
  const completion = object(plan.completion)
  const ids = completion?.response_step_ids
  return Array.isArray(ids) && ids.every((id) => typeof id === "string")
    ? ids as string[]
    : null
}

function loopBodyTemplateIds(steps: Step[]): Set<string> {
  const ids = new Set<string>()
  for (const step of steps) {
    if (step.type !== "LOOP") continue
    const body = object(step.config)?.body_step_ids
    if (!Array.isArray(body)) continue
    for (const id of body) if (typeof id === "string") ids.add(id)
  }
  return ids
}

/** Patch only response selection; preserve all other canonical/unknown Plan data. */
export function setResponseStepInPlan(
  plan: Plan,
  stepId: string,
  selected: boolean,
): Plan {
  const steps = getVisualSteps(plan)
  const current = getResponseStepIds(plan)
  const completion = object(plan.completion)
  if (
    !steps || !completion || !current ||
    !steps.some((step) => step.id === stepId) ||
    loopBodyTemplateIds(steps).has(stepId) ||
    current.includes(stepId) === selected
  ) return plan

  return {
    ...plan,
    completion: {
      ...completion,
      response_step_ids: selected
        ? [...current, stepId]
        : current.filter((id) => id !== stepId),
    },
  }
}

const WHEN_EDITABLE_STEP_TYPES = ["TOOL", "CONDITION", "JOIN", "APPROVAL"]

/** Change only the exact CONDITION result Boolean gate; fail closed for advanced when. */
export function setStepBranchWhenInPlan(
  plan: Plan,
  stepId: string,
  conditionId: string | null,
  expected = true,
): Plan {
  const steps = getVisualSteps(plan)
  const step = steps?.find((item) => item.id === stepId)
  if (!steps || !step || !WHEN_EDITABLE_STEP_TYPES.includes(step.type) ||
      loopProtectedStepIds(steps).has(stepId)) return plan
  const existing = simpleBranchWhen(step.when)
  const candidates = branchConditionSources(steps, stepId)
  if (step.when != null && (!existing || !candidates.some((item) => item.id === existing.conditionId)))
    return plan
  if (conditionId !== null && !candidates.some((item) => item.id === conditionId)) return plan
  if (conditionId === null && step.when == null) return plan
  if (existing?.conditionId === conditionId && existing.expected === expected) return plan

  return updateStepInPlan(plan, stepId, (current) => ({
    ...current,
    when: conditionId === null ? null : {
      op: "eq",
      left: { kind: "STEP_OUTPUT", step_id: conditionId, path: "/condition_result" },
      right: { kind: "LITERAL", value: expected },
    },
  }))
}

/** Guided tool creation uses only canonical depends_on and Boolean when fields. */
export function addGuidedToolStepToPlan(
  plan: Plan,
  toolVersionId: string,
  predecessorId: string | null = null,
  branchExpected: boolean | null = null,
  displayName = "",
): Plan {
  const steps = getVisualSteps(plan)
  if (!steps || !toolVersionId) return plan
  const predecessor = predecessorId
    ? steps.find((step) => step.id === predecessorId)
    : null
  if (predecessorId && (!predecessor || loopProtectedStepIds(steps).has(predecessorId)))
    return plan
  if ((branchExpected !== null && predecessor?.type !== "CONDITION") ||
      (predecessor?.type === "CONDITION" && branchExpected === null))
    return plan

  let next = addStepToPlan(plan, "TOOL", toolVersionId)
  const created = getVisualSteps(next)?.at(-1)
  if (!created || next === plan) return plan

  if (predecessor) {
    const problem = connectionProblem(getVisualSteps(next)!, predecessor.id, created.id)
    if (problem) return plan
    next = updateStepInPlan(next, created.id, (step) => ({
      ...step, depends_on: [predecessor.id],
    }))
    if (predecessor.type === "CONDITION") {
      const branched = setStepBranchWhenInPlan(next, created.id, predecessor.id, branchExpected!)
      if (branched === next) return plan
      next = branched
    }
  }
  if (displayName.trim()) {
    const name = displayName.trim().slice(0, 120)
    next = updateStepInPlan(next, created.id, (step) => ({
      ...step, name: `${name} 실행`,
    }))
  }
  return next
}

function toBinding(value: JsonValue | undefined): Binding | null {
  const binding = object(value)
  return binding && typeof binding.kind === "string" ? binding as Binding : null
}

function bindingForKind(kind: string): Binding {
  switch (kind) {
    case "SECRET_REF":
      return { kind, secret_id: "" }
    case "STEP_OUTPUT":
      return { kind, step_id: "", path: "/" }
    case "PLAN_INPUT":
    case "EXECUTION_CONTEXT":
    case "LOOP_CONTEXT":
      return { kind, path: "/" }
    default:
      return { kind: "LITERAL", value: "" }
  }
}

interface Props {
  plan: Plan
  onChange: (plan: Plan) => void
  readOnly: boolean
}

/** Visual projection of the canonical Plan. Never rebuild unknown config or step fields. */
export default function WorkflowVisualEditor({
  plan,
  onChange,
  readOnly,
}: Props) {
  const steps = useMemo(() => getVisualSteps(plan), [plan])
  const [selected, setSelected] = useState<string | null>(null)
  const [tools, setTools] = useState<MCPToolDto[]>([])
  const [toolId, setToolId] = useState("")
  const [toolSearch, setToolSearch] = useState("")
  const [appliedToolSearch, setAppliedToolSearch] = useState("")
  const [toolPage, setToolPage] = useState(1)
  const [toolHasNext, setToolHasNext] = useState(false)
  const [toolTotal, setToolTotal] = useState(0)
  const [toolLoading, setToolLoading] = useState(false)
  const [predecessorId, setPredecessorId] = useState("")
  const [branchSelection, setBranchSelection] = useState("")
  const [toolError, setToolError] = useState("")
  const [warning, setWarning] = useState("")
  const [newBindingName, setNewBindingName] = useState("")
  const [newConditionSource, setNewConditionSource] = useState("")
  const [view, setView] = useState<"graph" | "list">("graph")
  const [addSection, setAddSection] = useState<"tool" | "condition" | "join">("tool")
  const [responseExpanded, setResponseExpanded] = useState(false)

  useEffect(() => {
    if (readOnly) return
    const controller = new AbortController()
    setToolLoading(true)
    setToolError("")
    listMCPTools({
      page: toolPage,
      page_size: 100,
      status: "ACTIVE",
      q: appliedToolSearch || undefined,
      signal: controller.signal,
    })
      .then((page) => {
        if (controller.signal.aborted) return
        setTools(page.items)
        setToolHasNext(page.has_next)
        setToolTotal(page.total)
        setToolId((previous) => page.items.some((tool) => tool.id === previous) ? previous : "")
      })
      .catch((error) => {
        if (controller.signal.aborted || isAbortError(error)) return
        setTools([])
        setToolId("")
        setToolHasNext(false)
        setToolError(
          "활성 도구 목록을 불러올 수 없습니다. 기존 실행 계획은 그대로 유지됩니다.",
        )
      })
      .finally(() => {
        if (!controller.signal.aborted) setToolLoading(false)
      })
    return () => controller.abort()
  }, [readOnly, toolPage, appliedToolSearch])

  const active = steps?.find((step) => step.id === selected) ?? null
  const selectedTool = tools.find((tool) => tool.id === toolId)
  const selectedPredecessor = steps?.find((step) => step.id === predecessorId)
  const predecessorOptions = (steps ?? []).filter(
    (step) => !loopProtectedStepIds(steps ?? []).has(step.id),
  )
  const toolAddDisabled = readOnly || toolLoading || !selectedTool?.current_version_id ||
    (selectedPredecessor?.type === "CONDITION" && !["true", "false"].includes(branchSelection))
  const responseStepIds = getResponseStepIds(plan)
  const responseBodyIds = steps ? loopBodyTemplateIds(steps) : new Set<string>()
  const config = object(active?.config)
  const bindings = active?.type === "TOOL" ? object(config?.bindings) : null
  const sources = conditionSources(plan, steps ?? [])
  const condition = active?.type === "CONDITION" && !responseBodyIds.has(active.id)
    ? simpleCondition(config?.predicate) : null
  const ancestors = active ? ancestorStepIds(steps ?? [], active.id) : new Set<string>()
  const availableSources = sources.filter((source) =>
    source.dependsOn.every((id) => ancestors.has(id)),
  )
  const currentConditionSource = condition
    ? availableSources.find((source) =>
      source.binding.kind === condition.source.kind &&
      (source.binding.kind === "PLAN_INPUT"
        ? typeof condition.source.path === "string" &&
          (condition.source.path === source.binding.path ||
            condition.source.path.startsWith(`${source.binding.path}/`))
        : source.binding.step_id === condition.source.step_id),
    )?.id ?? ""
    : ""
  const branchSources = active ? branchConditionSources(steps ?? [], active.id) : []
  const branchWhen = simpleBranchWhen(active?.when)
  const branchEditable = active !== null &&
    WHEN_EDITABLE_STEP_TYPES.includes(active.type) &&
    !responseBodyIds.has(active.id) &&
    !loopProtectedStepIds(steps ?? []).has(active.id) &&
    (active.when == null ||
      (branchWhen !== null && branchSources.some((item) => item.id === branchWhen.conditionId)))

  const replace = (fn: (step: Step) => Step) => {
    if (readOnly || !active) return
    onChange(updateStepInPlan(plan, active.id, fn))
  }
  const updateConfig = (
    change: (current: Record<string, JsonValue>) => Record<string, JsonValue>,
  ) => {
    replace((step) => ({ ...step, config: change(object(step.config) ?? {}) }))
  }
  const updateBinding = (name: string, value: JsonValue | undefined) => {
    updateConfig((current) => {
      const next = { ...(object(current.bindings) ?? {}) }
      if (value === undefined) delete next[name]
      else next[name] = value
      return { ...current, bindings: next }
    })
  }

  const addTool = () => {
    if (!selectedTool?.current_version_id || toolAddDisabled) return
    const next = addGuidedToolStepToPlan(
      plan,
      selectedTool.current_version_id,
      predecessorId || null,
      selectedPredecessor?.type === "CONDITION" ? branchSelection === "true" : null,
      selectedTool.display_name || selectedTool.remote_name,
    )
    if (next === plan) {
      setWarning("선행 단계·조건 연결을 안전하게 구성할 수 없습니다. 기존 계획은 유지됩니다.")
      return
    }
    onChange(next)
    setSelected(getVisualSteps(next)?.at(-1)?.id ?? null)
    setPredecessorId("")
    setBranchSelection("")
    setWarning(
      "도구 단계가 추가되었습니다. 입력 바인딩·완료 응답을 확인한 후 초안 저장·서버 검증하세요.",
    )
  }
  const addJoin = () => {
    if (!steps?.length || readOnly) return
    const next = addStepToPlan(plan, "JOIN")
    onChange(next)
    setSelected(getVisualSteps(next)?.at(-1)?.id ?? null)
    setWarning("합류 단계에는 상위 단계를 한 개 이상 연결해야 합니다.")
  }
  const addCondition = () => {
    if (readOnly) return
    const next = addConditionStepToPlan(plan, newConditionSource)
    if (next === plan) return
    onChange(next)
    setNewConditionSource("")
    setSelected(getVisualSteps(next)?.at(-1)?.id ?? null)
    setWarning(
      "조건 단계가 추가되었습니다. 조건 경로 확인 후 후속 단계에 연결하고, 후속 단계의 실행 조건에서 참/거짓 분기를 설정하세요.",
    )
  }
  const connectGraph = (sourceId: string, targetId: string) => {
    if (readOnly || !steps) return
    const problem = connectionProblem(steps, sourceId, targetId)
    if (problem) {
      setWarning(problem)
      return
    }
    onChange(
      updateStepInPlan(plan, targetId, (step) => ({
        ...step,
        depends_on: [...step.depends_on, sourceId],
      })),
    )
    setSelected(targetId)
    setWarning(
      steps.find((step) => step.id === sourceId)?.type === "CONDITION"
        ? "선행 연결을 추가했습니다. 대상 단계의 실행 조건에서 참/거짓 분기를 선택하세요."
        : "연결을 추가했습니다. 저장 및 서버 검증 전까지 실행에는 적용되지 않습니다.",
    )
  }
  const disconnectGraph = (links: Array<{ source: string; target: string }>) => {
    if (readOnly || !steps) return
    let next = plan
    let blocked = 0
    for (const { source, target: targetId } of links) {
      const latestSteps = getVisualSteps(next)
      if (!latestSteps) continue
      const target = latestSteps.find((step) => step.id === targetId)
      if (!target?.depends_on.includes(source)) continue
      const loopScoped = loopProtectedStepIds(latestSteps)
      if (
        loopScoped.has(targetId) ||
        loopScoped.has(source) ||
        wouldBreakStepOutputAncestry(latestSteps, source, targetId)
      ) {
        blocked++
        continue
      }
      next = updateStepInPlan(next, targetId, (step) => ({
        ...step,
        depends_on: step.depends_on.filter((id) => id !== source),
      }))
    }
    if (next !== plan) onChange(next)
    setWarning(
      blocked
        ? "LOOP 범위 또는 하위 단계의 STEP_OUTPUT 참조가 영향을 받는 연결은 보호했습니다. 고급 JSON에서 안전하게 수정하세요."
        : "연결을 제거했습니다. 저장 전 서버 검증으로 참조 관계를 확인하세요.",
    )
  }
  const deleteStep = () => {
    if (!active || !steps || readOnly) return
    if (!["TOOL", "JOIN", "CONDITION"].includes(active.type)) {
      setWarning(
        "조건·승인·반복 단계의 안전한 제거는 고급 JSON에서 수행하세요.",
      )
      return
    }
    const referred = steps.some(
      (step) =>
        step.id !== active.id &&
        (step.depends_on.includes(active.id) ||
          (Array.isArray(object(step.config)?.body_step_ids) &&
            (object(step.config)?.body_step_ids as JsonValue[]).includes(
              active.id,
            )) ||
          // Conservative: block deletion if any other advanced config/when refers to this ID.
          JSON.stringify({ config: step.config, when: step.when }).includes(
            JSON.stringify(active.id),
          )),
    )
    const completion = object(plan.completion)
    if (
      referred ||
      (Array.isArray(completion?.response_step_ids) &&
        completion.response_step_ids.includes(active.id))
    ) {
      setWarning(
        "다른 단계 또는 완료 응답에서 참조하고 있습니다. 먼저 참조를 해제하세요.",
      )
      return
    }
    onChange({ ...plan, steps: steps.filter((step) => step.id !== active.id) })
    setSelected(null)
    setWarning("단계를 제거했습니다. 저장 전까지 서버에는 반영되지 않습니다.")
  }

  if (!steps)
    return (
      <p
        role="alert"
        className="p-4 rounded-lg bg-amber-50 text-amber-800 text-sm"
      >
        이 Plan의 단계 구조를 안전하게 해석할 수 없습니다. 고급 JSON에서만
        확인해 주세요.
      </p>
    )

  return (
    <div className="grid grid-cols-1 xl:grid-cols-[250px_minmax(420px,1fr)_310px] 2xl:grid-cols-[280px_minmax(460px,1fr)_340px] min-h-[550px] gap-0 border border-slate-200 rounded-xl overflow-hidden bg-white>
      <aside className="min-w-0 p-3 xl:p-4 border-b xl:border-b-0 xl:border-r border-slate-200 bg-slate-50 space-y-3 xl:max-h-[min(80vh,920px)] xl:overflow-y-auto" aria-label="단계 추가 및 완료 응답">
        <h2 className="font-semibold text-sm text-slate-800">단계 추가</h2>
        <p className="text-xs text-slate-500">추가할 단계 종류를 선택하세요. 설정은 선택한 종류만 표시됩니다.</p>
        <div role="group" aria-label="추가할 단계 종류" className="grid grid-cols-3 gap-1">
          {([
            ["tool", "도구"],
            ["condition", "조건"],
            ["join", "합류"],
          ] as const).map(([kind, label]) => (
            <button key={kind} type="button" aria-pressed={addSection === kind}
              onClick={() => setAddSection(kind)}
              className={`rounded-lg border px-1.5 py-2 text-xs font-medium focus-visible:outline-2 focus-visible:outline-indigo-600 ${
                addSection === kind
                  ? "border-indigo-400 bg-indigo-100 text-indigo-800"
                  : "border-slate-300 bg-white text-slate-700 hover:bg-slate-100"
              }`}>
              {label}
            </button>
          ))}
        </div>
        {addSection === "tool" && (
          <section aria-label="도구 단계 추가 설정" className="space-y-3">
        <label htmlFor="visual-tool-search" className="text-xs font-medium text-slate-600 block">
          MCP 도구 검색
        </label>
        <div className="flex gap-1">
          <input id="visual-tool-search" aria-label="도구 검색어" type="search"
            value={toolSearch} disabled={readOnly}
            onChange={(event) => setToolSearch(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== "Enter") return
              event.preventDefault()
              setToolPage(1)
              setAppliedToolSearch(toolSearch.trim())
            }}
            placeholder="도구 이름 또는 기능"
            className="w-full min-w-0 rounded-md border border-slate-300 p-2 text-xs" />
          <button type="button" disabled={readOnly} onClick={() => {
              setToolPage(1)
              setAppliedToolSearch(toolSearch.trim())
            }}
            className="shrink-0 rounded-md border border-slate-300 bg-white px-2 text-xs">
            검색
          </button>
        </div>
        <label className="text-xs font-medium text-slate-600 block" htmlFor="visual-tool-select">
          사용할 MCP 도구
        </label>
        <select id="visual-tool-select"
          disabled={readOnly || toolLoading}
          value={toolId}
          onChange={(event) => setToolId(event.target.value)}
          className="w-full rounded-md border border-slate-300 p-2 text-xs">
          <option value="">활성 도구 선택...</option>
          {tools.map((tool) => (
            <option key={tool.id} value={tool.id} disabled={!tool.current_version_id}>
              {tool.display_name || tool.remote_name}{tool.display_name && tool.remote_name !== tool.display_name ? ` · ${tool.remote_name}` : ""}
            </option>
          ))}
        </select>
        <div className="flex items-center justify-between gap-1 text-xs text-slate-500">
          <span>{toolLoading ? "조회 중..." : `검색 결과 ${toolTotal}건 · ${toolPage}페이지`}</span>
          <div className="flex items-center gap-1">
            <button type="button" aria-label="이전 도구 페이지"
              disabled={readOnly || toolLoading || toolPage <= 1}
              className="rounded border px-1.5 py-0.5 disabled:opacity-40"
              onClick={() => setToolPage((page) => Math.max(1, page - 1))}>이전</button>
            <button type="button" aria-label="다음 도구 페이지"
              disabled={readOnly || toolLoading || !toolHasNext}
              className="rounded border px-1.5 py-0.5 disabled:opacity-40"
              onClick={() => setToolPage((page) => page + 1)}>다음</button>
          </div>
        </div>
        {selectedTool && (
          <div className="rounded-md border border-indigo-100 bg-indigo-50/70 p-2 text-xs text-slate-700 space-y-1">
            <p className="font-medium break-words">{selectedTool.display_name || selectedTool.remote_name}</p>
            {selectedTool.description_override && <p>{selectedTool.description_override}</p>}
            <p className="break-all">도구 ID: {selectedTool.remote_name}</p>
            <p>현재 활성 버전: {selectedTool.current_version_id ? "있음" : "없음"}</p>
          </div>
        )}
        <label htmlFor="visual-tool-predecessor" className="text-xs font-medium text-slate-600 block">
          이전에 실행할 단계 (선택)
        </label>
        <select id="visual-tool-predecessor" disabled={readOnly || steps.length === 0}
          value={predecessorId} onChange={(event) => {
            setPredecessorId(event.target.value)
            setBranchSelection("")
          }}
          className="w-full rounded-md border border-slate-300 p-2 text-xs">
          <option value="">연결 안 함 (독립 실행)</option>
          {predecessorOptions.map((step) => (
            <option key={step.id} value={step.id}>
              {step.name} ({STEP_LABELS[step.type] ?? step.type})
            </option>
          ))}
        </select>
        {selectedPredecessor?.type === "CONDITION" && (
          <>
            <label htmlFor="visual-tool-branch" className="text-xs font-medium text-slate-600 block">
              어떤 조건 결과에서 실행할까요?
            </label>
            <select id="visual-tool-branch" value={branchSelection}
              disabled={readOnly} onChange={(event) => setBranchSelection(event.target.value)}
              className="w-full rounded-md border border-amber-300 bg-amber-50 p-2 text-xs">
              <option value="">참/거짓을 선택하세요</option>
              <option value="true">참일 때 실행</option>
              <option value="false">거짓일 때 실행</option>
            </select>
            <p className="text-xs text-amber-800">
              선택한 조건 단계와 선행 연결 및 실행 분기를 함께 설정합니다.
            </p>
          </>
        )}
        <button type="button" disabled={toolAddDisabled}
          onClick={addTool}
          className="flex items-center justify-center gap-2 rounded-lg p-2 text-sm bg-indigo-600 text-white w-full disabled:opacity-40">
          <Plus size={15} /> 도구 단계 추가
        </button>
        <p className="text-xs text-slate-500">
          단계를 추가한 뒤 입력 바인딩과 완료 응답을 확인하고 저장·검증해야 실행할 수 있습니다.
        </p>
          </section>
        )}
        {addSection === "condition" && (
          <section aria-label="조건 단계 추가 설정" className="space-y-3">
            <p className="text-xs text-slate-600">
              어떤 입력값이나 이전 단계 결과를 판단할지 선택하세요.
            </p>
        <label
          className="text-xs font-medium text-slate-600 block"
          htmlFor="visual-condition-source"
        >
          새 조건의 판단 대상
        </label>
        <select
          id="visual-condition-source"
          disabled={readOnly || sources.length === 0}
          value={newConditionSource}
          onChange={(event) => setNewConditionSource(event.target.value)}
          className="w-full rounded-md border border-slate-300 p-2 text-xs"
        >
          <option value="">입력값 또는 이전 단계 선택...</option>
          {sources.map((source) => (
            <option key={source.id} value={source.id}>{source.label}</option>
          ))}
        </select>
        <button
          type="button"
          disabled={readOnly || !sources.some((source) => source.id === newConditionSource)}
          onClick={addCondition}
          className="flex items-center justify-center gap-2 rounded-lg p-2 text-sm border border-amber-300 bg-amber-50 text-amber-900 w-full disabled:opacity-40"
        >
          <Plus size={15} /> 조건 단계 추가
        </button>
        <p className="text-xs text-slate-500 leading-relaxed">
          기본 조건 및 참/거짓 실행 분기는 화면에서 편집할 수 있습니다.
          복합 조건·승인·반복의 고급 설정은 JSON에서 유지됩니다.
        </p>
          </section>
        )}
        {addSection === "join" && (
          <section aria-label="합류 단계 추가 설정" className="space-y-3">
            <p className="text-xs text-slate-600">
              여러 선행 단계의 결과를 합칠 때 사용합니다. 단계를 추가한 뒤 오른쪽에서 연결과 합류 정책을 설정하세요.
            </p>
        <button
          type="button"
          disabled={readOnly || steps.length === 0}
          onClick={addJoin}
          className="flex items-center justify-center gap-2 rounded-lg p-2 text-sm border border-slate-300 bg-white w-full disabled:opacity-40"
        >
          <GitBranch size={15} /> 합류 단계 추가
        </button>
          </section>
        )}
        <div className="border-t border-slate-200 pt-3 space-y-2">
          <button type="button"
            aria-expanded={responseExpanded}
            aria-controls="visual-response-steps"
            onClick={() => setResponseExpanded((current) => !current)}
            className="flex w-full items-center justify-between gap-2 rounded-lg py-1 text-left text-sm font-semibold text-slate-800 focus-visible:outline-2 focus-visible:outline-indigo-600">
            <span>완료 응답 설정 ({responseStepIds?.length ?? 0}개)</span>
            <ChevronDown size={16} aria-hidden="true"
              className={responseExpanded ? "rotate-180 transition-transform" : "transition-transform"} />
          </button>
          {!responseExpanded && steps.length > 0 && responseStepIds?.length === 0 && (
            <p className="text-xs text-amber-700">
              응답 단계가 없습니다. 설정을 펼쳐 하나 이상 선택하세요.
            </p>
          )}
          {responseExpanded && (
            <div id="visual-response-steps">
        <fieldset
          disabled={readOnly || responseStepIds === null}
          className="space-y-2 border-t border-slate-200 pt-3"
          aria-label="완료 응답 단계 선택"
        >
          <legend className="font-semibold text-sm text-slate-800">
            완료 응답 단계
          </legend>
          <p className="text-xs text-slate-600 leading-relaxed">
            실행 결과로 반환할 단계를 하나 이상 선택하세요. 선행 연결과는
            별개의 설정입니다.
          </p>
          {responseStepIds === null ? (
            <p role="alert" className="text-xs text-amber-700">
              완료 응답 구조를 읽을 수 없습니다. 고급 JSON에서 수정하세요.
            </p>
          ) : (
            <>
              <div className="max-h-48 overflow-y-auto space-y-2">
                {steps.map((step) => (
                  <label
                    key={step.id}
                    className="flex items-start gap-2 text-xs text-slate-700"
                  >
                    <input
                      type="checkbox"
                      aria-label={`응답 단계: ${step.name} (${step.id})`}
                      disabled={responseBodyIds.has(step.id)}
                      checked={responseStepIds.includes(step.id)}
                      onChange={(event) => {
                        const next = setResponseStepInPlan(
                          plan, step.id, event.target.checked,
                        )
                        if (next === plan) return
                        onChange(next)
                        setWarning(
                          "완료 응답 단계를 변경했습니다. 초안 저장 후 서버 검증하세요.",
                        )
                      }}
                    />
                    <span className="break-words min-w-0">
                      {step.name} ({step.id})
                      {responseBodyIds.has(step.id) && " · 반복 본문(선택 불가)"}
                    </span>
                  </label>
                ))}
              </div>
              {steps.length > 0 && responseStepIds.length === 0 && (
                <p role="alert" className="text-xs text-amber-700">
                  응답 단계가 지정되지 않았습니다. 서버 검증 전에 하나 이상 선택하세요.
                </p>
              )}
            </>
          )}
        </fieldset>
            </div>
          )}
        </div>
        {toolError && (
          <p role="alert" className="text-xs text-amber-700">
            {toolError}
          </p>
        )}
      </aside>

      <section
        className="p-4 bg-slate-50/50 space-y-4"
        aria-label="실행 단계 흐름"
      >
        <div className="flex items-center justify-between gap-2">
          <h2 className="font-semibold text-sm text-slate-800">
            실행 단계 ({steps.length})
          </h2>
          <div
            className="flex items-center gap-1"
            role="group"
            aria-label="단계 표현 방식"
          >
            <button
              type="button"
              aria-pressed={view === "graph"}
              onClick={() => setView("graph")}
              className={`rounded-md px-2 py-1 text-xs border ${
                view === "graph"
                  ? "border-indigo-400 bg-indigo-50 text-indigo-700"
                  : "border-slate-300"
              }`}
            >
              그래프
            </button>
            <button
              type="button"
              aria-pressed={view === "list"}
              onClick={() => setView("list")}
              className={`rounded-md px-2 py-1 text-xs border ${
                view === "list"
                  ? "border-indigo-400 bg-indigo-50 text-indigo-700"
                  : "border-slate-300"
              }`}
            >
              목록
            </button>
          </div>
        </div>
        <p className="text-xs text-slate-500">
          실행 순서는 노드 위치가 아닌 선행 단계 관계로 결정됩니다.
          {view === "graph" &&
            !readOnly &&
            " 오른쪽 핸들에서 왼쪽 핸들로 선을 연결하고, 선을 선택해 Delete로 제거할 수 있습니다."}
        </p>
        {view === "graph" && steps.some((step) => step.type === "CONDITION") && (
          <p className="text-xs text-amber-800 bg-amber-50 rounded-md px-3 py-2">
            연결선은 선행 관계만 나타냅니다. 참/거짓 실행 여부는
            후속 단계의 실행 조건(when)으로 설정하며 노드에 표시합니다.
          </p>
        )}
        {view === "graph" ? (
          <Suspense
            fallback={
              <div className="flex h-[570px] items-center justify-center rounded-lg border border-slate-200 text-sm text-slate-500">
                그래프를 불러오는 중입니다.
              </div>
            }
          >
            <WorkflowGraphCanvas
              steps={steps}
              selected={selected}
              readOnly={readOnly}
              onSelect={(id) => {
                setSelected(id)
                setWarning("")
              }}
              onConnect={connectGraph}
              onDisconnect={disconnectGraph}
              canConnect={(source, target) =>
                connectionProblem(steps, source, target) === null
              }
            />
          </Suspense>
        ) : (
          <div className="space-y-3">
            {steps.length === 0 && (
              <p className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-sm text-slate-500 text-center">
                실행 단계가 없습니다. 왼쪽에서 도구를 선택해 추가하세요.
              </p>
            )}
            {steps.map((step, index) => (
              <button
                key={step.id}
                type="button"
                aria-label={`${step.name} 단계 선택`}
                onClick={() => {
                  setSelected(step.id)
                  setWarning("")
                }}
                className={`block text-left rounded-xl p-4 w-full border-2 bg-white shadow-sm space-y-2 ${
                  selected === step.id
                    ? "border-indigo-500"
                    : "border-slate-200 hover:border-slate-300"
                }`}
              >
                <div className="flex items-center gap-2">
                  <span className="rounded-md bg-indigo-50 text-indigo-700 p-1.5">
                    <Wrench size={16} />
                  </span>
                  <span className="font-medium text-sm text-slate-800">
                    {index + 1}. {step.name}
                  </span>
                  <span className="ml-auto text-xs text-slate-500">
                    {STEP_LABELS[step.type] ?? step.type}
                  </span>
                </div>
                <div className="text-xs text-slate-500">
                  <span className="font-mono">{step.id}</span>
                </div>
                <div className="flex items-start gap-1.5 text-xs text-slate-600">
                  <Link2 size={13} className="shrink-0" />
                  {step.depends_on.length
                    ? `선행 단계: ${step.depends_on.join(", ")}`
                    : "선행 단계 없음 · 병렬 시작 가능"}
                </div>
              </button>
            ))}
          </div>
        )}
        {warning && (
          <p
            role="status"
            className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg p-3"
          >
            {warning}
          </p>
        )}
      </section>

      <aside
        className="p-4 border-t lg:border-t-0 lg:border-l border-slate-200 space-y-4"
        aria-label="단계 설정"
      >
        {!active ? (
          <p className="text-sm text-slate-500">
            중앙의 단계를 선택해 이름·연결·도구 입력을 설정하세요.
          </p>
        ) : (
          <>
            <div className="flex justify-between items-center">
              <h2 className="font-semibold text-sm text-slate-800">
                단계 설정
              </h2>
              <span className="text-xs text-slate-500">
                {STEP_LABELS[active.type] ?? active.type}
              </span>
            </div>
            <label
              htmlFor="visual-step-name"
              className="block text-xs text-slate-600"
            >
              단계 이름
            </label>
            <input
              id="visual-step-name"
              className="w-full rounded-md border border-slate-300 p-2 text-sm"
              disabled={readOnly}
              value={active.name}
              onChange={(event) =>
                replace((step) => ({ ...step, name: event.target.value }))
              }
            />
            <fieldset disabled={readOnly} className="space-y-2">
              <legend className="font-medium text-xs text-slate-700 mb-2">
                선행 단계 선택
              </legend>
              {steps
                .filter((step) => step.id !== active.id)
                .map((step) => (
                  <label
                    key={step.id}
                    className="flex items-start gap-2 text-xs text-slate-600"
                  >
                    <input
                      type="checkbox"
                      checked={active.depends_on.includes(step.id)}
                      onChange={(event) => {
                        if (event.target.checked) {
                          connectGraph(step.id, active.id)
                        } else {
                          disconnectGraph([{ source: step.id, target: active.id }])
                        }
                      }}
                    />
                    {step.name} ({step.id})
                  </label>
                ))}
              {steps.length <= 1 && (
                <p className="text-xs text-slate-400">
                  연결 가능한 다른 단계가 없습니다.
                </p>
              )}
            </fieldset>
            {WHEN_EDITABLE_STEP_TYPES.includes(active.type) && (
              <div className="space-y-2 rounded-lg border border-emerald-200 bg-emerald-50/60 p-3">
                <h3 className="text-sm font-semibold text-emerald-900">실행 조건 (참/거짓 분기)</h3>
                {!branchEditable ? (
                  <p className="text-xs text-amber-800" role="status">
                    기존 복합·특수 when 조건 또는 LOOP 범위는 화면에서 수정하지 않습니다.
                    현재 값은 그대로 유지되며 고급 JSON에서 확인하세요.
                  </p>
                ) : (
                  <>
                    <label htmlFor="visual-when-condition" className="block text-xs text-slate-700">
                      조건 기준 단계
                    </label>
                    <select
                      id="visual-when-condition"
                      disabled={readOnly || branchSources.length === 0}
                      value={branchWhen?.conditionId ?? ""}
                      onChange={(event) => {
                        const next = setStepBranchWhenInPlan(
                          plan, active.id, event.target.value || null,
                        )
                        if (next !== plan) {
                          onChange(next)
                          setWarning("실행 분기를 변경했습니다. 초안 저장 후 서버 검증하세요.")
                        }
                      }}
                      className="w-full rounded-md border border-slate-300 bg-white p-2 text-xs disabled:opacity-60"
                    >
                      <option value="">조건 없음 (항상 실행)</option>
                      {branchSources.map((step) => (
                        <option key={step.id} value={step.id}>{step.name} ({step.id})</option>
                      ))}
                    </select>
                    {branchWhen && (
                      <>
                        <label htmlFor="visual-when-result" className="block text-xs text-slate-700">
                          조건 결과
                        </label>
                        <select
                          id="visual-when-result"
                          disabled={readOnly}
                          value={branchWhen.expected ? "true" : "false"}
                          onChange={(event) => {
                            const next = setStepBranchWhenInPlan(
                              plan, active.id, branchWhen.conditionId,
                              event.target.value === "true",
                            )
                            if (next !== plan) {
                              onChange(next)
                              setWarning("참/거짓 분기를 변경했습니다. 초안 저장 후 서버 검증하세요.")
                            }
                          }}
                          className="w-full rounded-md border border-slate-300 bg-white p-2 text-xs"
                        >
                          <option value="true">참일 때 실행</option>
                          <option value="false">거짓일 때 실행</option>
                        </select>
                      </>
                    )}
                    {branchSources.length === 0 && (
                      <p className="text-xs text-emerald-900">
                        조건 분기 단계를 먼저 연결하면 실행 결과를 선택할 수 있습니다.
                      </p>
                    )}
                    <p className="text-xs text-slate-600">
                      연결과 실행 조건은 별개입니다. 분기 선택 후 저장·서버 검증하세요.
                    </p>
                  </>
                )}
              </div>
            )}
            {active.type === "JOIN" && (
              <>
                <label
                  htmlFor="visual-join-policy"
                  className="block text-xs text-slate-600"
                >
                  합류 정책
                </label>
                <select
                  id="visual-join-policy"
                  disabled={readOnly}
                  value={
                    typeof config?.policy === "string" ? config.policy : ""
                  }
                  onChange={(event) =>
                    updateConfig((current) => ({
                      ...current,
                      policy: event.target.value,
                    }))
                  }
                  className="w-full rounded-md border border-slate-300 p-2 text-sm"
                >
                  <option value="ALL_SUCCESS">모든 단계 성공</option>
                  <option value="ALL_COMPLETE">모든 단계 완료</option>
                  <option value="ANY_SUCCESS">하나 이상 성공</option>
                </select>
              </>
            )}
            {active.type === "CONDITION" && (
              <div className="space-y-2 border border-amber-200 bg-amber-50/60 rounded-lg p-3">
                <h3 className="font-semibold text-sm text-amber-900">기본 조건식</h3>
                {!condition ? (
                  <p className="text-xs text-amber-800">
                    복합 조건식·특수 바인딩 또는 반복 본문 조건은 고급 JSON에서만 수정합니다. 기존 값은 유지됩니다.
                  </p>
                ) : (
                  <>
                    <label htmlFor="visual-condition-op" className="block text-xs text-slate-700">판단 방법</label>
                    <select
                      id="visual-condition-op"
                      disabled={readOnly}
                      value={condition.op}
                      onChange={(event) => updateConfig((current) => ({
                        ...current,
                        predicate: simplePredicate(
                          event.target.value as SimpleConditionOp,
                          condition.source,
                          condition.value,
                        ),
                      }))}
                      className="w-full rounded-md border border-slate-300 bg-white p-2 text-xs"
                    >
                      <option value="exists">값이 존재함</option>
                      <option value="is_null">값이 null임</option>
                      <option value="eq">문자열 값과 같음</option>
                      <option value="ne">문자열 값과 다름</option>
                    </select>
                    <label htmlFor="visual-condition-binding" className="block text-xs text-slate-700">판단 대상</label>
                    <select
                      id="visual-condition-binding"
                      disabled={readOnly || availableSources.length === 0}
                      value={currentConditionSource}
                      onChange={(event) => {
                        const source = availableSources.find((item) => item.id === event.target.value)
                        if (!source) return
                        updateConfig((current) => ({
                          ...current,
                          predicate: simplePredicate(condition.op, source.binding, condition.value),
                        }))
                      }}
                      className="w-full rounded-md border border-slate-300 bg-white p-2 text-xs"
                    >
                      <option value="">기존 참조(선택 항목 외)</option>
                      {availableSources.map((source) => (
                        <option key={source.id} value={source.id}>{source.label}</option>
                      ))}
                    </select>
                    <label htmlFor="visual-condition-path" className="block text-xs text-slate-700">데이터 경로 (JSON Pointer)</label>
                    <input
                      id="visual-condition-path"
                      disabled={readOnly}
                      value={String(condition.source.path)}
                      onChange={(event) => updateConfig((current) => ({
                        ...current,
                        predicate: simplePredicate(
                          condition.op,
                          { ...condition.source, path: event.target.value },
                          condition.value,
                        ),
                      }))}
                      className="w-full rounded-md border border-slate-300 bg-white p-2 text-xs"
                      placeholder="/structured_content/value"
                    />
                    {(condition.op === "eq" || condition.op === "ne") && (
                      <>
                        <label htmlFor="visual-condition-value" className="block text-xs text-slate-700">비교할 문자열</label>
                        <input
                          id="visual-condition-value"
                          disabled={readOnly}
                          value={condition.value}
                          onChange={(event) => updateConfig((current) => ({
                            ...current,
                            predicate: simplePredicate(condition.op, condition.source, event.target.value),
                          }))}
                          className="w-full rounded-md border border-slate-300 bg-white p-2 text-xs"
                        />
                      </>
                    )}
                    <p className="text-xs text-amber-800">
                      단계 결과는 /structured_content 등 실제 반환 구조를 확인하세요. 저장 후 서버 검증이 필요합니다.
                    </p>
                  </>
                )}
              </div>
            )}
            {active.type === "TOOL" && (
              <>
                <div className="text-xs bg-slate-50 rounded-md p-2 space-y-1">
                  <p className="font-medium">고정 ToolVersion ID</p>
                  <p className="font-mono break-all">
                    {String(config?.tool_version_id ?? "미설정")}
                  </p>
                  <p>
                    선택한 버전은 저장 후 고정됩니다. 변경은 고급 JSON에서
                    가능합니다.
                  </p>
                </div>
                <h3 className="font-semibold text-sm text-slate-800">
                  입력 바인딩
                </h3>
                {bindings === null && (
                  <p role="alert" className="text-xs text-amber-700">
                    바인딩 구조를 읽을 수 없습니다. 고급 JSON에서 수정하세요.
                  </p>
                )}
                {bindings &&
                  Object.entries(bindings).map(([name, value]) => {
                    const binding = toBinding(value)
                    return (
                      <div
                        key={name}
                        className="rounded-lg border border-slate-200 p-2 space-y-2"
                      >
                        <div className="flex justify-between items-center">
                          <strong className="text-xs text-slate-700 break-all">
                            {name}
                          </strong>
                          <button
                            type="button"
                            aria-label={`${name} 바인딩 제거`}
                            disabled={readOnly}
                            onClick={() => updateBinding(name, undefined)}
                            className="text-slate-400 hover:text-red-600 disabled:opacity-40"
                          >
                            <Trash2 size={14} />
                          </button>
                        </div>
                        {!binding ? (
                          <p className="text-xs text-amber-700">
                            지원하지 않는 바인딩 구조입니다. 고급 JSON에서
                            수정하세요.
                          </p>
                        ) : (
                          <>
                            <select
                              aria-label={`${name} 바인딩 종류`}
                              className="w-full rounded-md border border-slate-300 p-2 text-xs"
                              disabled={readOnly}
                              value={binding.kind}
                              onChange={(event) =>
                                updateBinding(
                                  name,
                                  bindingForKind(event.target.value),
                                )
                              }
                            >
                              {!BINDING_KINDS.includes(binding.kind) && (
                                <option value={binding.kind}>
                                  {binding.kind} (고급)
                                </option>
                              )}
                              {BINDING_KINDS.map((kind) => (
                                <option key={kind} value={kind}>
                                  {kind}
                                </option>
                              ))}
                            </select>
                            {binding.kind === "LITERAL" ? (
                              <>
                                <input
                                  aria-label={`${name} 값`}
                                  className="w-full rounded-md border border-slate-300 p-2 text-xs"
                                  disabled={
                                    readOnly ||
                                    typeof binding.value !== "string"
                                  }
                                  value={
                                    typeof binding.value === "string"
                                      ? binding.value
                                      : JSON.stringify(binding.value ?? null)
                                  }
                                  onChange={(event) =>
                                    updateBinding(name, {
                                      ...binding,
                                      value: event.target.value,
                                    })
                                  }
                                  placeholder="문자열 값"
                                />
                                {typeof binding.value !== "string" && (
                                  <p className="text-xs text-amber-700">
                                    숫자·배열·객체 리터럴은 타입을 보존하도록
                                    고급 JSON에서만 수정합니다.
                                  </p>
                                )}
                              </>
                            ) : (
                              <>
                                {binding.kind === "STEP_OUTPUT" && (
                                  <select
                                    aria-label={`${name} 출력 단계`}
                                    disabled={readOnly}
                                    value={String(binding.step_id ?? "")}
                                    onChange={(event) =>
                                      updateBinding(name, {
                                        ...binding,
                                        step_id: event.target.value,
                                      })
                                    }
                                    className="w-full rounded-md border border-slate-300 p-2 text-xs"
                                  >
                                    <option value="">출력 단계 선택</option>
                                    {steps
                                      .filter((step) =>
                                        active.depends_on.includes(step.id),
                                      )
                                      .map((step) => (
                                        <option key={step.id} value={step.id}>
                                          {step.name}
                                        </option>
                                      ))}
                                  </select>
                                )}
                                {binding.kind === "SECRET_REF" ? (
                                  <input
                                    aria-label={`${name} 비밀 참조 ID`}
                                    disabled={readOnly}
                                    value={String(binding.secret_id ?? "")}
                                    onChange={(event) =>
                                      updateBinding(name, {
                                        ...binding,
                                        secret_id: event.target.value,
                                      })
                                    }
                                    className="w-full rounded-md border border-slate-300 p-2 text-xs"
                                    placeholder="Secret UUID (원문 입력 금지)"
                                  />
                                ) : (
                                  <input
                                    aria-label={`${name} JSON Pointer`}
                                    disabled={readOnly}
                                    value={String(binding.path ?? "")}
                                    onChange={(event) =>
                                      updateBinding(name, {
                                        ...binding,
                                        path: event.target.value,
                                      })
                                    }
                                    className="w-full rounded-md border border-slate-300 p-2 text-xs"
                                    placeholder="/path"
                                  />
                                )}
                              </>
                            )}
                          </>
                        )}
                      </div>
                    )
                  })}
                {bindings && !readOnly && (
                  <div className="flex gap-1">
                    <input
                      aria-label="새 바인딩 이름"
                      className="min-w-0 flex-1 rounded-md border border-slate-300 p-2 text-xs"
                      value={newBindingName}
                      onChange={(event) =>
                        setNewBindingName(event.target.value)
                      }
                      placeholder="파라미터 이름"
                    />
                    <button
                      type="button"
                      disabled={
                        !newBindingName.trim() ||
                        newBindingName.trim() in bindings
                      }
                      onClick={() => {
                        updateBinding(
                          newBindingName.trim(),
                          bindingForKind("LITERAL"),
                        )
                        setNewBindingName("")
                      }}
                      className="rounded bg-indigo-50 px-2 text-xs text-indigo-700 disabled:opacity-40"
                    >
                      추가
                    </button>
                  </div>
                )}
              </>
            )}
            {!["TOOL", "JOIN", "CONDITION"].includes(active.type) && (
              <div className="text-xs rounded-md bg-amber-50 text-amber-900 p-3 flex gap-2">
                <ShieldAlert size={15} className="shrink-0" />
                <span>
                  이 단계의 조건식·정책·반복 설정은 고급 JSON에서 편집하세요.
                  현재 구성은 변경하지 않습니다.
                </span>
              </div>
            )}
            {!readOnly && (
              <button
                type="button"
                className="text-xs text-red-600 flex items-center gap-2"
                onClick={deleteStep}
              >
                <Trash2 size={14} /> 선택 단계 제거
              </button>
            )}
          </>
        )}
      </aside>
    </div>
  )
}
