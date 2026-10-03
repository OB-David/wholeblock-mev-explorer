import type { GraphEdge } from './types'

/**
 * Repeatedly remove every edge incident to a degree-one node.
 *
 * This computes the edges outside the graph's undirected 2-core. Removing a
 * leaf may expose another leaf, so a single degree pass is not sufficient.
 */
export function recursivelyPrunedEdgeIds(edges: readonly GraphEdge[]): Set<string> {
  const edgeById = new Map(edges.map(edge => [edge.id, edge]))
  const remaining = new Set(edgeById.keys())
  const incident = new Map<string, Set<string>>()

  for (const edge of edges) {
    const sourceEdges = incident.get(edge.source) ?? new Set<string>()
    sourceEdges.add(edge.id)
    incident.set(edge.source, sourceEdges)

    const targetEdges = incident.get(edge.target) ?? new Set<string>()
    targetEdges.add(edge.id)
    incident.set(edge.target, targetEdges)
  }

  const queue = [...incident.entries()]
    .filter(([, edgeIds]) => edgeIds.size === 1)
    .map(([nodeId]) => nodeId)
  const removed = new Set<string>()

  for (let cursor = 0; cursor < queue.length; cursor++) {
    const nodeId = queue[cursor]
    const nodeEdges = incident.get(nodeId)
    if (!nodeEdges || nodeEdges.size !== 1) continue

    const edgeId = nodeEdges.values().next().value as string
    if (!remaining.delete(edgeId)) continue
    removed.add(edgeId)

    const edge = edgeById.get(edgeId)
    if (!edge) continue
    for (const endpoint of new Set([edge.source, edge.target])) {
      const endpointEdges = incident.get(endpoint)
      if (!endpointEdges) continue
      endpointEdges.delete(edgeId)
      if (endpointEdges.size === 1) queue.push(endpoint)
    }
  }

  return removed
}
