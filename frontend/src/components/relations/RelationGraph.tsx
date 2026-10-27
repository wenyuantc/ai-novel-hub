// 关系图谱（React.lazy 懒加载的独立 chunk；@xyflow/react 与 @dagrejs/dagre 只允许在本文件引用）

import { graphlib, layout } from '@dagrejs/dagre'
import {
  Background,
  Controls,
  MarkerType,
  MiniMap,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Edge,
  type Node,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useEffect, useMemo } from 'react'
import { intimacyColor } from '../../api/relationships'
import type { GraphEdge, GraphNode } from '../../api/types'
import { theme } from 'antd'
import { usePalette } from '../../theme/usePalette'
import { useResolvedTheme } from '../../theme/themeStore'
import { nodeColor } from '../../utils/graphFilter'

interface Props {
  nodes: GraphNode[]
  edges: GraphEdge[]
  onNodeClick?: (id: number) => void
  height?: number
}

const NODE_W = 120
const NODE_H = 44

function layoutGraph(nodes: GraphNode[], edges: GraphEdge[]): Map<number, { x: number; y: number }> {
  const g = new graphlib.Graph()
  g.setGraph({ rankdir: 'LR', nodesep: 40, ranksep: 120 })
  g.setDefaultEdgeLabel(() => ({}))
  for (const n of nodes) g.setNode(String(n.id), { width: NODE_W, height: NODE_H })
  for (const e of edges) g.setEdge(String(e.source), String(e.target))
  layout(g)
  const out = new Map<number, { x: number; y: number }>()
  for (const n of nodes) {
    const p = g.node(String(n.id)) as { x: number; y: number } | undefined
    out.set(n.id, { x: (p?.x ?? 0) - NODE_W / 2, y: (p?.y ?? 0) - NODE_H / 2 })
  }
  return out
}

/** 节点按定位着色，边标注称呼并按亲密度红 → 灰 → 绿着色；点击节点回调角色 id。 */
export default function RelationGraph({ nodes, edges, onNodeClick, height = 600 }: Props) {
  const palette = usePalette()
  const resolved = useResolvedTheme()
  const { token } = theme.useToken()
  const { flowNodes, flowEdges } = useMemo(() => {
    const pos = layoutGraph(nodes, edges)
    const flowNodes: Node[] = nodes.map((n) => {
      const color = nodeColor(n, palette)
      const alive = n.life_status === 'alive'
      return {
        id: String(n.id),
        position: pos.get(n.id) ?? { x: 0, y: 0 },
        data: { label: alive ? n.name : `${n.name}（${n.life_status === 'dead' ? '已死亡' : '非存活'}）` },
        style: {
          width: NODE_W,
          border: `2px solid ${color}`,
          borderRadius: 8,
          background: alive ? palette.nodeBg : palette.nodeBgMuted,
          color: alive ? palette.text : palette.textSecondary,
          fontWeight: n.is_protagonist ? 700 : 400,
          borderStyle: alive ? 'solid' : 'dashed',
          fontSize: 13,
          padding: 6,
        },
      }
    })
    const flowEdges: Edge[] = edges.map((e) => {
      const color = intimacyColor(e.intimacy)
      return {
        id: String(e.id),
        source: String(e.source),
        target: String(e.target),
        label: e.label,
        animated: e.status === 'complicated',
        style: { stroke: color, strokeWidth: 2, strokeDasharray: e.status === 'past' || e.status === 'broken' ? '6 4' : undefined },
        labelStyle: { fill: color, fontWeight: 600 },
        markerEnd: e.role_a === e.role_b ? undefined : { type: MarkerType.ArrowClosed, color },
      }
    })
    return { flowNodes, flowEdges }
  }, [nodes, edges, palette])
  // 节点可拖动：本地状态，筛选 / 数据变化时重置为新的布局
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState(flowNodes)
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState(flowEdges)
  useEffect(() => {
    setRfNodes(flowNodes)
    setRfEdges(flowEdges)
  }, [flowNodes, flowEdges, setRfNodes, setRfEdges])

  return (
    <div style={{ height, border: `1px solid ${token.colorBorderSecondary}`, borderRadius: 8 }}>
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        fitView
        colorMode={resolved}
        nodesConnectable={false}
        onNodeClick={(_, node) => onNodeClick?.(Number(node.id))}
      >
        <Background />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable />
      </ReactFlow>
    </div>
  )
}
