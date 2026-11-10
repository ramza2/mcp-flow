import { useEffect, useMemo, useRef } from "react"
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import { layoutGraphSteps, type GraphStep } from "./workflowGraphModel"

type GraphNodeData = {
  label: string
  type: string
  dependencies: string[]
}

type WorkflowGraphNode = Node<GraphNodeData, "workflow-step">

const LABELS: Record<string, string> = {
  TOOL: "도구 실행",
  CONDITION: "조건 분기",
  JOIN: "병렬 합류",
  APPROVAL: "승인 대기",
  LOOP: "반복 실행",
}

const COLORS: Record<string, string> = {
  TOOL: "#6366f1",
  CONDITION: "#f59e0b",
  JOIN: "#0891b2",
  APPROVAL: "#16a34a",
  LOOP: "#9333ea",
}

function StepNode({ id, data, selected }: NodeProps<WorkflowGraphNode>) {
  return (
    <div
      className={`rounded-xl border-2 bg-white px-4 py-3 shadow-md min-w-[212px] max-w-[230px] ${
        selected
          ? "border-indigo-500 ring-2 ring-indigo-100"
          : "border-slate-200"
      }`}
    >
      <Handle
        type="target"
        position={Position.Left}
        className="!h-3 !w-3 !bg-slate-500"
      />
      <div
        className="text-[11px] font-semibold mb-1"
        style={{ color: COLORS[data.type] ?? "#475569" }}
      >
        {LABELS[data.type] ?? data.type}
      </div>
      <div className="text-sm font-semibold text-slate-900 break-words">
        {data.label}
      </div>
      <div className="mt-1 text-[11px] font-mono text-slate-500 break-all">
        {id}
      </div>
      <div className="mt-2 text-[10px] text-slate-500">
        {data.dependencies.length
          ? `선행 ${data.dependencies.length}개`
          : "선행 단계 없음"}
      </div>
      <Handle
        type="source"
        position={Position.Right}
        className="!h-3 !w-3 !bg-indigo-500"
      />
    </div>
  )
}

const nodeTypes = { "workflow-step": StepNode }

interface Props {
  steps: GraphStep[]
  selected: string | null
  onSelect: (id: string) => void
  onConnect: (sourceId: string, targetId: string) => void
  onDisconnect: (links: Array<{ source: string; target: string }>) => void
  canConnect: (sourceId: string, targetId: string) => boolean
  readOnly: boolean
}

/** React Flow is only a visual graph. No node coordinates are saved to the Plan. */
export default function WorkflowGraphCanvas({
  steps,
  selected,
  onSelect,
  onConnect,
  onDisconnect,
  canConnect,
  readOnly,
}: Props) {
  const projected = useMemo(() => {
    const positions = layoutGraphSteps(steps)
    const ids = new Set(steps.map((step) => step.id))
    const nodes: WorkflowGraphNode[] = steps.map((step) => ({
      id: step.id,
      type: "workflow-step",
      position: positions.get(step.id) ?? { x: 0, y: 0 },
      selected: step.id === selected,
      data: {
        label: step.name,
        type: step.type,
        dependencies: step.depends_on,
      },
    }))
    const edges: Edge[] = steps.flatMap((step) =>
      step.depends_on
        .filter((source) => ids.has(source))
        .map((source) => ({
          id: `dep:${source}:${step.id}`,
          source,
          target: step.id,
          deletable: !readOnly,
          type: "smoothstep",
          animated: false,
          style: { stroke: "#818cf8", strokeWidth: 2 },
          markerEnd: { type: MarkerType.ArrowClosed, color: "#818cf8" },
        })),
    )
    return { nodes, edges }
  }, [steps, selected, readOnly])

  const [nodes, setNodes, onNodesChange] = useNodesState<WorkflowGraphNode>(
    projected.nodes,
  )
  const topology = steps
    .map((step) => `${step.id}:${step.depends_on.join(",")}`)
    .join("|")
  const topologyRef = useRef(topology)

  useEffect(() => {
    // A changed graph topology gets a fresh DAG layout. Merely selecting or
    // renaming nodes preserves positions the user dragged in this UI session.
    const needsLayout = topologyRef.current !== topology
    topologyRef.current = topology
    setNodes((previous) => {
      const byId = new Map(previous.map((node) => [node.id, node]))
      return projected.nodes.map((node) => ({
        ...node,
        position: needsLayout
          ? node.position
          : (byId.get(node.id)?.position ?? node.position),
      }))
    })
  }, [projected.nodes, setNodes, topology])

  const acceptConnection = (connection: Connection | Edge) =>
    !readOnly && canConnect(connection.source, connection.target)

  return (
    <div
      className="h-[570px] rounded-lg border border-slate-200 bg-white"
      aria-label="워크플로 그래프"
    >
      {steps.length ? (
        <ReactFlow<WorkflowGraphNode>
          nodes={nodes}
          edges={projected.edges}
          nodeTypes={nodeTypes}
          onNodesChange={onNodesChange}
          onNodeClick={(_, node) => onSelect(node.id)}
          onConnect={(connection) =>
            onConnect(connection.source, connection.target)
          }
          isValidConnection={acceptConnection}
          onEdgesDelete={(deleted) => {
            if (!readOnly)
              onDisconnect(
                deleted.map((edge) => ({
                  source: edge.source,
                  target: edge.target,
                })),
              )
          }}
          nodesConnectable={!readOnly}
          edgesReconnectable={false}
          edgesFocusable={!readOnly}
          nodesDraggable
          fitView
          fitViewOptions={{ padding: 0.2, maxZoom: 1.1 }}
          minZoom={0.2}
          maxZoom={1.6}
          defaultEdgeOptions={{ type: "smoothstep" }}
          colorMode="light"
        >
          <Background gap={18} color="#e2e8f0" />
          <Controls showInteractive={false} />
          <MiniMap
            pannable
            zoomable
            nodeColor={(node) => COLORS[String(node.data?.type)] ?? "#94a3b8"}
            style={{ backgroundColor: "#f8fafc" }}
          />
        </ReactFlow>
      ) : (
        <p className="flex h-full items-center justify-center text-sm text-slate-500">
          실행 단계가 없습니다. 왼쪽에서 도구를 선택해 추가하세요.
        </p>
      )}
    </div>
  )
}
