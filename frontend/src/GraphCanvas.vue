<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, shallowRef, watch } from 'vue'
import type { BlockGraph, GraphEdge, GraphNode } from './types'

const props = defineProps<{
  graph: BlockGraph
  txIndex: number | null
  relayoutNonce: number
  localLayout: boolean
  hideIsolatedTransfers: boolean
  prunedEdgeIds: Set<string>
  highlightedEdgeIds: Set<string>
  sandwichEdgeRoles: Map<string, 'front' | 'victim' | 'back'>
  focusTxIndexes: Set<number> | null
  timelineStep: number
}>()
const emit = defineEmits<{ copied: [value: string] }>()
const canvas = shallowRef<HTMLCanvasElement | null>(null)
const host = shallowRef<HTMLDivElement | null>(null)
const positions = new Map<string, { x: number; y: number; vx: number; vy: number }>()
const globalPositions = new Map<string, { x: number; y: number }>()
let animation = 0
let resizeObserver: ResizeObserver | null = null
let view = { x: 0, y: 0, scale: 1 }
let dragging = false
let dragMoved = false
let dragStart = { x: 0, y: 0, vx: 0, vy: 0 }
let hovered: { node?: GraphNode; edge?: GraphEdge; x: number; y: number } | null = null

const baseScopeEdges = computed(() => {
  if (props.focusTxIndexes) return props.graph.edges.filter(edge => props.focusTxIndexes!.has(edge.tx_index))
  if (props.txIndex !== null) return props.graph.edges.filter(edge => edge.tx_index === props.txIndex)
  return props.graph.edges
})
const scopeEdges = computed(() => {
  if (!props.hideIsolatedTransfers) return baseScopeEdges.value
  return baseScopeEdges.value.filter(edge =>
    !props.prunedEdgeIds.has(edge.id)
      || props.sandwichEdgeRoles.has(edge.id)
      || props.highlightedEdgeIds.has(edge.id),
  )
})
const edges = computed(() => {
  if (props.focusTxIndexes) return scopeEdges.value
  if (props.txIndex !== null) {
    return [...scopeEdges.value]
      .sort((a, b) => a.order - b.order || a.id.localeCompare(b.id))
      .slice(0, props.timelineStep)
  }
  const visibleTransactions = new Set(
    props.graph.transactions.slice(0, props.timelineStep).map(transaction => transaction.index),
  )
  return scopeEdges.value.filter(edge => visibleTransactions.has(edge.tx_index))
})
const nodes = computed(() => {
  const ids = new Set(scopeEdges.value.flatMap(e => [e.source, e.target]))
  if (props.focusTxIndexes) {
    for (const tx of props.graph.transactions) {
      if (!props.focusTxIndexes.has(tx.index)) continue
      ids.add(tx.from)
      if (tx.to) ids.add(tx.to)
    }
    return props.graph.nodes.filter(node => ids.has(node.id))
  }
  if (props.txIndex === null) {
    if (!props.hideIsolatedTransfers) return props.graph.nodes
    return props.graph.nodes.filter(node => ids.has(node.id))
  }
  const tx = props.graph.transactions.find(item => item.index === props.txIndex)
  if (tx) { ids.add(tx.from); if (tx.to) ids.add(tx.to) }
  return props.graph.nodes.filter(node => ids.has(node.id))
})

interface EdgeLane { bend: number; loopIndex: number }
interface EdgeGeometry {
  start: { x: number; y: number }
  control: { x: number; y: number }
  end: { x: number; y: number }
}

function pairKey(edge: GraphEdge) {
  return edge.source < edge.target
    ? `${edge.source}\u0000${edge.target}`
    : `${edge.target}\u0000${edge.source}`
}

const edgeLanes = computed(() => {
  const groups = new Map<string, GraphEdge[]>()
  for (const edge of scopeEdges.value) {
    const group = groups.get(pairKey(edge)) ?? []
    group.push(edge)
    groups.set(pairKey(edge), group)
  }

  const lanes = new Map<string, EdgeLane>()
  const compare = (a: GraphEdge, b: GraphEdge) =>
    a.order - b.order || a.token_address.localeCompare(b.token_address) || a.id.localeCompare(b.id)
  for (const group of groups.values()) {
    group.sort(compare)
    if (group[0].source === group[0].target) {
      group.forEach((edge, index) => lanes.set(edge.id, { bend: 0, loopIndex: index }))
      continue
    }

    const canonicalSource = group[0].source < group[0].target ? group[0].source : group[0].target
    const forward = group.filter(edge => edge.source === canonicalSource)
    const reverse = group.filter(edge => edge.source !== canonicalSource)
    if (forward.length && reverse.length) {
      // Edge-relative normals point in opposite directions, separating the two flows.
      forward.forEach((edge, index) => lanes.set(edge.id, { bend: index + .75, loopIndex: 0 }))
      reverse.forEach((edge, index) => lanes.set(edge.id, { bend: index + .75, loopIndex: 0 }))
    } else {
      const oneDirection = forward.length ? forward : reverse
      oneDirection.forEach((edge, index) => {
        lanes.set(edge.id, { bend: index - (oneDirection.length - 1) / 2, loopIndex: 0 })
      })
    }
  }
  return lanes
})

const layoutLinks = computed(() => {
  const unique = new Map<string, { source: string; target: string }>()
  for (const edge of scopeEdges.value) {
    if (edge.source !== edge.target && !unique.has(pairKey(edge))) {
      unique.set(pairKey(edge), { source: edge.source, target: edge.target })
    }
  }
  return [...unique.values()]
})

function sizeCanvas() {
  if (!canvas.value || !host.value) return
  const ratio = window.devicePixelRatio || 1
  const box = host.value.getBoundingClientRect()
  canvas.value.width = Math.max(1, Math.round(box.width * ratio))
  canvas.value.height = Math.max(1, Math.round(box.height * ratio))
  canvas.value.style.width = `${box.width}px`
  canvas.value.style.height = `${box.height}px`
  draw()
}

function resetLayout() {
  cancelAnimationFrame(animation)
  positions.clear()
  const list = nodes.value
  const radius = Math.max(120, Math.sqrt(list.length) * 42)
  list.forEach((node, i) => {
    const anchored = !props.localLayout && props.txIndex !== null ? globalPositions.get(node.id) : undefined
    const angle = (i / Math.max(1, list.length)) * Math.PI * 2
    positions.set(node.id, { x: anchored?.x ?? Math.cos(angle) * radius, y: anchored?.y ?? Math.sin(angle) * radius, vx: 0, vy: 0 })
  })
  view = { x: 0, y: 0, scale: fitScale(list.length) }
  if (props.localLayout || props.txIndex === null) simulate(0)
  else draw()
}

function fitScale(count: number) { return Math.max(.18, Math.min(1.1, 12 / Math.sqrt(Math.max(count, 1)))) }

function simulate(iteration: number) {
  const list = nodes.value
  const links = layoutLinks.value
  const byId = positions
  const repulsion = list.length > 250 ? 1100 : 2200
  for (let i = 0; i < list.length; i++) {
    const a = byId.get(list[i].id)!
    for (let j = i + 1; j < list.length; j++) {
      const b = byId.get(list[j].id)!
      let dx = a.x - b.x, dy = a.y - b.y
      const d2 = Math.max(80, dx * dx + dy * dy)
      const force = repulsion / d2
      const d = Math.sqrt(d2)
      a.vx += dx / d * force; a.vy += dy / d * force
      b.vx -= dx / d * force; b.vy -= dy / d * force
    }
  }
  for (const edge of links) {
    const a = byId.get(edge.source), b = byId.get(edge.target)
    if (!a || !b) continue
    const dx = b.x - a.x, dy = b.y - a.y, distance = Math.max(1, Math.hypot(dx, dy))
    const force = (distance - 115) * .006
    a.vx += dx / distance * force; a.vy += dy / distance * force
    b.vx -= dx / distance * force; b.vy -= dy / distance * force
  }
  for (const item of byId.values()) {
    item.vx += -item.x * .0008; item.vy += -item.y * .0008
    item.vx *= .82; item.vy *= .82; item.x += item.vx; item.y += item.vy
  }
  draw()
  if (iteration < (list.length > 350 ? 80 : 150)) animation = requestAnimationFrame(() => simulate(iteration + 1))
  else if (props.txIndex === null) {
    globalPositions.clear()
    for (const [id, p] of positions) globalPositions.set(id, { x: p.x, y: p.y })
  }
}

function edgeGeometry(edge: GraphEdge): EdgeGeometry | null {
  const a = positions.get(edge.source), b = positions.get(edge.target)
  if (!a || !b) return null
  const lane = edgeLanes.value.get(edge.id) ?? { bend: 0, loopIndex: 0 }

  if (edge.source === edge.target) {
    const spread = 24 + lane.loopIndex * 12
    return {
      start: { x: a.x + 11, y: a.y - 7 },
      control: { x: a.x, y: a.y - 38 - spread },
      end: { x: a.x - 11, y: a.y - 7 },
    }
  }

  const dx = b.x - a.x, dy = b.y - a.y
  const length = Math.max(1, Math.hypot(dx, dy))
  const ux = dx / length, uy = dy / length
  const gap = Math.min(30, Math.max(12, length * .09))
  const control = {
    x: (a.x + b.x) / 2 - uy * lane.bend * gap,
    y: (a.y + b.y) / 2 + ux * lane.bend * gap,
  }
  const startDx = control.x - a.x, startDy = control.y - a.y
  const startLength = Math.max(1, Math.hypot(startDx, startDy))
  const endDx = b.x - control.x, endDy = b.y - control.y
  const endLength = Math.max(1, Math.hypot(endDx, endDy))
  return {
    start: { x: a.x + startDx / startLength * 13, y: a.y + startDy / startLength * 13 },
    control,
    end: { x: b.x - endDx / endLength * 17, y: b.y - endDy / endLength * 17 },
  }
}

function draw() {
  const el = canvas.value
  if (!el) return
  const ratio = window.devicePixelRatio || 1
  const ctx = el.getContext('2d')!
  const width = el.width / ratio, height = el.height / ratio
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0)
  ctx.clearRect(0, 0, width, height)
  ctx.save()
  ctx.translate(width / 2 + view.x, height / 2 + view.y)
  ctx.scale(view.scale, view.scale)
  for (const edge of edges.value) drawEdge(ctx, edge)
  for (const node of nodes.value) drawNode(ctx, node)
  ctx.restore()
  if (hovered) drawTooltip(ctx, hovered, width, height)
}

function drawEdge(ctx: CanvasRenderingContext2D, edge: GraphEdge) {
  const geometry = edgeGeometry(edge)
  if (!geometry) return
  const { start, control, end } = geometry
  ctx.beginPath(); ctx.moveTo(start.x, start.y); ctx.quadraticCurveTo(control.x, control.y, end.x, end.y)
  const sandwichRole = props.sandwichEdgeRoles.get(edge.id)
  if (sandwichRole) {
    const roleColors = { front: '#ff5c72', victim: '#f4c95d', back: '#51d88a' }
    const roleDashes = { front: [11, 6], victim: [3, 5], back: [15, 5, 3, 5] }
    ctx.strokeStyle = roleColors[sandwichRole]
    ctx.globalAlpha = 1
    ctx.lineWidth = 8.5
    ctx.lineCap = 'round'
    ctx.setLineDash(roleDashes[sandwichRole])
    ctx.stroke()
  }
  const highlighting = props.highlightedEdgeIds.size > 0
  const highlighted = props.highlightedEdgeIds.has(edge.id)
  ctx.strokeStyle = edge.color
  ctx.globalAlpha = highlighting ? (highlighted ? 1 : .24) : .82
  ctx.lineWidth = highlighted ? 2.8 : edge.kind === 'transfer' ? 2.2 : 1.8
  ctx.setLineDash(edge.kind === 'transfer' ? [] : [7, 5]); ctx.stroke(); ctx.setLineDash([])
  const tangentX = end.x - control.x, tangentY = end.y - control.y
  const tangentLength = Math.max(1, Math.hypot(tangentX, tangentY))
  const ux = tangentX / tangentLength, uy = tangentY / tangentLength
  ctx.beginPath(); ctx.moveTo(end.x, end.y); ctx.lineTo(end.x - ux * 9 + uy * 5, end.y - uy * 9 - ux * 5); ctx.lineTo(end.x - ux * 9 - uy * 5, end.y - uy * 9 + ux * 5); ctx.closePath(); ctx.fillStyle = edge.color; ctx.fill()
  ctx.globalAlpha = 1
}

function drawNode(ctx: CanvasRenderingContext2D, node: GraphNode) {
  const p = positions.get(node.id); if (!p) return
  ctx.beginPath()
  if (node.kind === 'user') ctx.arc(p.x, p.y, 12, 0, Math.PI * 2)
  else ctx.roundRect(p.x - 15, p.y - 11, 30, 22, 5)
  ctx.fillStyle = '#111d31'; ctx.strokeStyle = '#93a4bf'; ctx.lineWidth = 1.5; ctx.fill(); ctx.stroke()
  ctx.fillStyle = '#e6edf7'; ctx.font = '600 11px "Fira Sans", sans-serif'; ctx.textAlign = 'center'; ctx.fillText(node.alias, p.x, p.y + 27)
}

function drawTooltip(ctx: CanvasRenderingContext2D, item: NonNullable<typeof hovered>, width: number, height: number) {
  const balanceChanges = item.node && props.txIndex !== null
    ? props.graph.transactions.find(tx => tx.index === props.txIndex)?.balance_changes[item.node.id] ?? []
    : []
  const lines = item.node ? [
    item.node.alias,
    item.node.address,
    ...(props.txIndex !== null
      ? balanceChanges.length
        ? ['Final balance changes', ...balanceChanges.map(change => `${change.amount.startsWith('-') ? '' : '+'}${change.amount} ${change.token_symbol}`)]
        : ['Final balance changes: 0']
      : []),
  ] : item.edge ? [
    `${item.edge.token_symbol} · ${item.edge.kind}`,
    `${item.edge.amount} ${item.edge.token_symbol}`,
    ...(props.sandwichEdgeRoles.has(item.edge.id)
      ? [`Sandwich role: ${{ front: 'Front-run', victim: 'Victim', back: 'Back-run' }[props.sandwichEdgeRoles.get(item.edge.id)!]}`]
      : []),
  ] : []
  ctx.font = '12px "Fira Code", monospace'
  const boxWidth = Math.min(410, Math.max(...lines.map(line => ctx.measureText(line).width), 120) + 24)
  const x = Math.min(item.x + 14, width - boxWidth - 8), y = Math.min(item.y + 14, height - lines.length * 19 - 18)
  ctx.fillStyle = 'rgba(7,16,31,.96)'; ctx.strokeStyle = '#31425e'; ctx.lineWidth = 1; ctx.beginPath(); ctx.roundRect(x, y, boxWidth, lines.length * 19 + 12, 7); ctx.fill(); ctx.stroke()
  ctx.fillStyle = '#e6edf7'; ctx.textAlign = 'left'; lines.forEach((line, i) => ctx.fillText(line, x + 12, y + 20 + i * 19))
}

function worldPoint(event: PointerEvent | WheelEvent) {
  const box = canvas.value!.getBoundingClientRect()
  return { x: (event.clientX - box.left - box.width / 2 - view.x) / view.scale, y: (event.clientY - box.top - box.height / 2 - view.y) / view.scale }
}

function nodeAt(point: { x: number; y: number }) {
  return nodes.value.find(item => {
    const p = positions.get(item.id)
    return p && Math.hypot(p.x - point.x, p.y - point.y) < 16
  })
}

function pointerDown(event: PointerEvent) {
  dragging = true
  dragMoved = false
  dragStart = { x: event.clientX, y: event.clientY, vx: view.x, vy: view.y }
  canvas.value?.setPointerCapture(event.pointerId)
}
function pointerMove(event: PointerEvent) {
  if (dragging) {
    const dx = event.clientX - dragStart.x, dy = event.clientY - dragStart.y
    if (Math.hypot(dx, dy) > 4) dragMoved = true
    view.x = dragStart.vx + dx; view.y = dragStart.vy + dy; hovered = null; draw(); return
  }
  const point = worldPoint(event)
  const node = nodeAt(point)
  let edge: GraphEdge | undefined
  if (!node) {
    let closest = 7 / view.scale
    for (const candidate of edges.value) {
      const distance = distanceToEdge(point, candidate)
      if (distance < closest) { closest = distance; edge = candidate }
    }
  }
  const box = canvas.value!.getBoundingClientRect()
  hovered = node || edge ? { node, edge, x: event.clientX - box.left, y: event.clientY - box.top } : null
  draw()
}
async function pointerUp(event: PointerEvent) {
  if (dragging && !dragMoved) {
    const node = nodeAt(worldPoint(event))
    if (node) {
      try {
        if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(node.address)
        else {
          const area = document.createElement('textarea')
          area.value = node.address; area.style.position = 'fixed'; area.style.opacity = '0'
          document.body.appendChild(area); area.select(); document.execCommand('copy'); area.remove()
        }
        emit('copied', node.address)
      } catch { /* Clipboard access can be unavailable outside a secure context. */ }
    }
  }
  dragging = false
  canvas.value?.releasePointerCapture(event.pointerId)
}
function distanceToEdge(point: {x:number;y:number}, edge: GraphEdge) {
  const geometry = edgeGeometry(edge)
  if (!geometry) return Infinity
  let closest = Infinity
  let previous = geometry.start
  for (let index = 1; index <= 24; index++) {
    const t = index / 24, inverse = 1 - t
    const current = {
      x: inverse * inverse * geometry.start.x + 2 * inverse * t * geometry.control.x + t * t * geometry.end.x,
      y: inverse * inverse * geometry.start.y + 2 * inverse * t * geometry.control.y + t * t * geometry.end.y,
    }
    const dx = current.x - previous.x, dy = current.y - previous.y
    const projection = Math.max(0, Math.min(1,
      ((point.x - previous.x) * dx + (point.y - previous.y) * dy) / (dx * dx + dy * dy || 1),
    ))
    closest = Math.min(closest, Math.hypot(
      point.x - (previous.x + projection * dx),
      point.y - (previous.y + projection * dy),
    ))
    previous = current
  }
  return closest
}
function wheel(event: WheelEvent) { event.preventDefault(); view.scale = Math.max(.08, Math.min(4, view.scale * Math.exp(-event.deltaY * .001))); draw() }

watch(() => [props.txIndex, props.focusTxIndexes, props.relayoutNonce, props.localLayout, props.hideIsolatedTransfers, props.graph] as const, async () => { await nextTick(); resetLayout() })
watch(() => props.highlightedEdgeIds, draw)
watch(() => props.sandwichEdgeRoles, async () => {
  hovered = null
  if (props.hideIsolatedTransfers) {
    await nextTick()
    resetLayout()
    return
  }
  draw()
})
watch(() => props.timelineStep, () => { hovered = null; draw() })
onMounted(() => { resizeObserver = new ResizeObserver(sizeCanvas); if (host.value) resizeObserver.observe(host.value); resetLayout() })
onBeforeUnmount(() => { cancelAnimationFrame(animation); resizeObserver?.disconnect() })
</script>

<template>
  <div ref="host" class="graph-host">
    <canvas ref="canvas" :aria-label="hideIsolatedTransfers ? 'Token flow graph with cycle core extracted; drag to pan and scroll to zoom' : 'Token flow graph; drag to pan and scroll to zoom'" tabindex="0"
      @pointerdown="pointerDown" @pointermove="pointerMove" @pointerup="pointerUp" @pointercancel="pointerUp" @wheel="wheel" />
    <div v-if="nodes.length === 0" class="empty">No token flows in this view</div>
  </div>
</template>
