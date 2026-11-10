/** Canvas-only projection. The server Execution Plan remains the source of truth. */
export interface GraphStep {
  id: string
  name: string
  type: string
  depends_on: string[]
  config?: unknown
  when?: unknown
}

export interface GraphPosition {
  x: number
  y: number
}

export interface SimpleBranchWhen {
  conditionId: string
  expected: boolean
}

function plainObject(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

/** Only the exact canonical condition-result gate is safe for visual editing. */
export function simpleBranchWhen(value: unknown): SimpleBranchWhen | null {
  const when = plainObject(value)
  if (!when || Object.keys(when).sort().join() !== "left,op,right" || when.op !== "eq")
    return null
  const left = plainObject(when.left)
  const right = plainObject(when.right)
  if (
    !left || !right ||
    Object.keys(left).sort().join() !== "kind,path,step_id" ||
    Object.keys(right).sort().join() !== "kind,value" ||
    left.kind !== "STEP_OUTPUT" || typeof left.step_id !== "string" ||
    left.path !== "/condition_result" || right.kind !== "LITERAL" ||
    typeof right.value !== "boolean"
  ) return null
  return { conditionId: left.step_id, expected: right.value }
}

/** Only upstream CONDITION nodes outside LOOP scope are branch sources. */
export function branchConditionSources(steps: GraphStep[], stepId: string): GraphStep[] {
  const byId = new Map(steps.map((step) => [step.id, step]))
  const protectedIds = loopProtectedStepIds(steps)
  if (!byId.has(stepId) || protectedIds.has(stepId)) return []
  const ancestors = new Set<string>()
  const visit = (id: string) => {
    if (id === stepId || ancestors.has(id)) return
    ancestors.add(id)
    for (const parent of byId.get(id)?.depends_on ?? []) visit(parent)
  }
  for (const parent of byId.get(stepId)?.depends_on ?? []) visit(parent)
  return steps.filter((step) =>
    ancestors.has(step.id) && step.type === "CONDITION" &&
    !protectedIds.has(step.id),
  )
}

/** Layout is intentionally derived, never persisted as execution semantics. */
export function layoutGraphSteps(
  steps: GraphStep[],
): Map<string, GraphPosition> {
  const byId = new Map(steps.map((step) => [step.id, step]))
  const depths = new Map<string, number>()

  const depthOf = (id: string, visiting: Set<string>): number => {
    if (depths.has(id)) return depths.get(id)!
    const step = byId.get(id)
    if (!step || visiting.has(id)) return 0
    const path = new Set(visiting).add(id)
    const upstream = step.depends_on.filter((parent) => byId.has(parent))
    const depth = upstream.length
      ? Math.min(
          steps.length,
          1 + Math.max(...upstream.map((parent) => depthOf(parent, path))),
        )
      : 0
    depths.set(id, depth)
    return depth
  }

  for (const step of steps) depthOf(step.id, new Set())
  const groups = new Map<number, string[]>()
  for (const step of steps) {
    const depth = depths.get(step.id) ?? 0
    groups.set(depth, [...(groups.get(depth) ?? []), step.id])
  }

  const positions = new Map<string, GraphPosition>()
  for (const [depth, ids] of groups) {
    ids.forEach((id, index) => {
      positions.set(id, {
        x: depth * 280 + 60,
        y: index * 150 - ((ids.length - 1) * 150) / 2 + 250,
      })
    })
  }
  return positions
}

/** No user-authored new step types, scripts, or semantics are introduced here. */
export function connectionProblem(
  steps: GraphStep[],
  sourceId: string,
  targetId: string,
): string | null {
  const byId = new Map(steps.map((step) => [step.id, step]))
  const source = byId.get(sourceId)
  const target = byId.get(targetId)
  if (!source || !target) return "연결할 단계가 존재하지 않습니다."
  if (sourceId === targetId)
    return "자기 자신을 선행 단계로 연결할 수 없습니다."
  const loopScoped = loopProtectedStepIds(steps)
  if (loopScoped.has(sourceId) || loopScoped.has(targetId)) {
    return "LOOP 범위 및 본문 연결은 고급 JSON에서 설정하세요."
  }
  if (target.depends_on.includes(sourceId)) return "이미 존재하는 연결입니다."
  if (target.type === "TOOL" && target.depends_on.length > 0) {
    return "도구 단계의 다중 선행 단계는 지원하지 않습니다. JOIN 단계를 사용하세요."
  }

  const visiting = new Set<string>()
  const reachesTarget = (id: string): boolean => {
    if (id === targetId) return true
    if (visiting.has(id)) return false
    visiting.add(id)
    return (byId.get(id)?.depends_on ?? []).some(reachesTarget)
  }
  if (reachesTarget(sourceId))
    return "순환 의존성이 만들어져 연결할 수 없습니다."
  return null
}

/** The graph cannot safely change LOOP parent links or body-template scope. */
export function loopProtectedStepIds(steps: GraphStep[]): Set<string> {
  const protectedIds = new Set<string>()
  for (const step of steps) {
    if (step.type !== "LOOP") continue
    protectedIds.add(step.id)
    const config = step.config
    if (!config || typeof config !== "object" || Array.isArray(config))
      continue
    const bodyIds = (config as Record<string, unknown>).body_step_ids
    if (Array.isArray(bodyIds)) {
      for (const id of bodyIds) {
        if (typeof id === "string") protectedIds.add(id)
      }
    }
  }
  return protectedIds
}

function stepOutputSources(value: unknown, found: Set<string>): void {
  if (!value || typeof value !== "object") return
  if (Array.isArray(value)) {
    value.forEach((entry) => stepOutputSources(entry, found))
    return
  }
  const item = value as Record<string, unknown>
  if (item.kind === "STEP_OUTPUT" && typeof item.step_id === "string")
    found.add(item.step_id)
  Object.values(item).forEach((entry) => stepOutputSources(entry, found))
}

/** Removing one edge may invalidate transitive STEP_OUTPUT sources downstream. */
export function wouldBreakStepOutputAncestry(
  steps: GraphStep[],
  sourceId: string,
  targetId: string,
): boolean {
  if (!steps.some((step) => step.id === targetId && step.depends_on.includes(sourceId)))
    return false
  const after = steps.map((step) =>
    step.id === targetId
      ? { ...step, depends_on: step.depends_on.filter((id) => id !== sourceId) }
      : step,
  )

  const isAncestor = (graph: GraphStep[], ownerId: string, upstreamId: string) => {
    const byId = new Map(graph.map((step) => [step.id, step]))
    const visited = new Set<string>()
    const visit = (id: string): boolean => {
      if (id === upstreamId) return true
      if (visited.has(id)) return false
      visited.add(id)
      return (byId.get(id)?.depends_on ?? []).some(visit)
    }
    return (byId.get(ownerId)?.depends_on ?? []).some(visit)
  }

  return steps.some((step) => {
    const sources = new Set<string>()
    stepOutputSources({ config: step.config, when: step.when }, sources)
    return [...sources].some(
      (id) =>
        isAncestor(steps, step.id, id) &&
        !isAncestor(after, step.id, id),
    )
  })
}

/** Conservatively prevent breaking an explicitly referenced STEP_OUTPUT binding. */
export function hasDirectStepOutputReference(
  value: unknown,
  sourceId: string,
): boolean {
  if (!value || typeof value !== "object") return false
  if (Array.isArray(value))
    return value.some((item) => hasDirectStepOutputReference(item, sourceId))
  const item = value as Record<string, unknown>
  if (item.kind === "STEP_OUTPUT" && item.step_id === sourceId) return true
  return Object.values(item).some((nested) =>
    hasDirectStepOutputReference(nested, sourceId),
  )
}
