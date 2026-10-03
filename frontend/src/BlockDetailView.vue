<script setup lang="ts">
import { computed, onMounted, ref, shallowRef, watch } from 'vue'
import { apiUrl, readApiJson } from './api'
import GraphCanvas from './GraphCanvas.vue'
import { recursivelyPrunedEdgeIds } from './graphFilters'
import type { BlockGraph } from './types'

const props = defineProps<{ blockNumber: number }>()
const emit = defineEmits<{ home: [] }>()
const graph = shallowRef<BlockGraph | null>(null)
const loading = ref(false)
const status = ref(`Preparing block ${props.blockNumber}`)
const progress = ref(0)
const selectedTx = ref<number | null>(null)
const localLayout = ref(true)
const hideBlockIsolatedTransfers = ref(false)
const hideTransactionIsolatedTransfers = ref(false)
const legendExpanded = ref(false)
const relayoutNonce = ref(0)
const cycleKind = ref<'token' | null>(null)
const cycleIndex = ref(0)
const timelineStep = ref(0)
const mevMode = ref<'sandwich' | 'arbitrage' | null>(null)
const sandwichIndex = ref(0)
const sandwichTxPosition = ref(0)
const arbitrageTxIndex = ref(0)
const arbitrageCycleIndex = ref(0)

const selected = computed(() => selectedTx.value === null ? null : graph.value?.transactions.find(tx => tx.index === selectedTx.value) ?? null)
const txPosition = computed(() => selectedTx.value === null ? -1 : graph.value?.transactions.findIndex(tx => tx.index === selectedTx.value) ?? -1)
const selectedEdges = computed(() => selectedTx.value === null
  ? []
  : (graph.value?.edges ?? [])
      .filter(edge => edge.tx_index === selectedTx.value)
      .sort((a, b) => a.order - b.order || a.id.localeCompare(b.id)))
const timelineTotal = computed(() => selectedTx.value === null
  ? graph.value?.transactions.length ?? 0
  : selectedEdges.value.length)
const timelineItemLabel = computed(() => {
  if (selectedTx.value === null) {
    const transaction = graph.value?.transactions[timelineStep.value - 1]
    return transaction ? `TX #${transaction.index}` : ''
  }
  const edge = selectedEdges.value[timelineStep.value - 1]
  return edge ? `order #${edge.order}` : ''
})
const activeCycles = computed(() => cycleKind.value === 'token' ? selected.value?.token_cycles ?? [] : [])
const activeCycle = computed(() => activeCycles.value[cycleIndex.value] ?? null)
const activeSandwich = computed(() => selectedTx.value === null && mevMode.value === 'sandwich'
  ? graph.value?.sandwiches[sandwichIndex.value] ?? null
  : null)
const activeSandwichTransactions = computed(() => {
  const sandwich = activeSandwich.value
  if (!sandwich) return []
  const victimIds = new Set(sandwich.victim_edge_ids)
  return [
    { index: sandwich.front_tx_index, role: 'front' as const, label: 'Front-run', edgeIds: sandwich.front_edge_ids },
    ...sandwich.victim_tx_indexes.map((index, position) => ({
      index,
      role: 'victim' as const,
      label: `Victim ${position + 1}/${sandwich.victim_tx_indexes.length}`,
      edgeIds: (graph.value?.edges ?? [])
        .filter(edge => edge.tx_index === index && victimIds.has(edge.id))
        .map(edge => edge.id),
    })),
    { index: sandwich.back_tx_index, role: 'back' as const, label: 'Back-run', edgeIds: sandwich.back_edge_ids },
  ]
})
const activeSandwichTx = computed(() => activeSandwichTransactions.value[sandwichTxPosition.value] ?? null)
const arbitrageTransactions = computed(() => graph.value?.transactions.filter(tx => tx.token_cycles.length) ?? [])
const activeArbitrageTx = computed(() => selectedTx.value === null && mevMode.value === 'arbitrage'
  ? arbitrageTransactions.value[arbitrageTxIndex.value] ?? null
  : null)
const activeArbitrageCycle = computed(() => activeArbitrageTx.value?.token_cycles[arbitrageCycleIndex.value] ?? null)
const viewTxIndex = computed(() => selectedTx.value ?? activeSandwichTx.value?.index ?? activeArbitrageTx.value?.index ?? null)
const viewEdges = computed(() => viewTxIndex.value === null
  ? graph.value?.edges ?? []
  : (graph.value?.edges ?? []).filter(edge => edge.tx_index === viewTxIndex.value))
const hideIsolatedTransfers = computed({
  get: () => viewTxIndex.value === null ? hideBlockIsolatedTransfers.value : hideTransactionIsolatedTransfers.value,
  set: value => {
    if (viewTxIndex.value === null) hideBlockIsolatedTransfers.value = value
    else hideTransactionIsolatedTransfers.value = value
  },
})
const prunedEdgeIds = computed(() => recursivelyPrunedEdgeIds(viewEdges.value))
const prunedEdgeCount = computed(() => prunedEdgeIds.value.size)
const visibleEdgeCount = computed(() => hideIsolatedTransfers.value
  ? viewEdges.value.length - prunedEdgeCount.value
  : viewEdges.value.length)
const tokenFlowMetric = computed(() => {
  const total = graph.value?.edges.length ?? 0
  if (viewTxIndex.value !== null || hideBlockIsolatedTransfers.value) return `${visibleEdgeCount.value}/${total}`
  return String(total)
})
const visibleTokens = computed(() => {
  if (!graph.value) return []
  if (viewTxIndex.value === null) return graph.value.tokens
  const addresses = new Set(viewEdges.value.map(edge => edge.token_address.toLowerCase()))
  return graph.value.tokens.filter(token => addresses.has(token.address.toLowerCase()))
})
const focusTxIndexes = computed(() => {
  if (activeSandwichTx.value) return new Set([activeSandwichTx.value.index])
  if (activeArbitrageTx.value) return new Set([activeArbitrageTx.value.index])
  return null
})
const sandwichEdgeRoles = computed(() => {
  const roles = new Map<string, 'front' | 'victim' | 'back'>()
  const sandwich = activeSandwich.value
  if (!sandwich) return roles
  sandwich.front_edge_ids.forEach(id => roles.set(id, 'front'))
  sandwich.victim_edge_ids.forEach(id => roles.set(id, 'victim'))
  sandwich.back_edge_ids.forEach(id => roles.set(id, 'back'))
  return roles
})
const highlightedEdgeIds = computed(() => {
  if (activeArbitrageCycle.value) return new Set(activeArbitrageCycle.value.edge_ids)
  if (activeCycle.value) return new Set(activeCycle.value.edge_ids)
  return new Set(sandwichEdgeRoles.value.keys())
})

function englishStatus(message: string) {
  return message
    .replace(/^等待执行$/, 'Queued')
    .replace(/^完成$/, 'Complete')
    .replace(/^开始抓取整块 opcode trace$/, 'Fetching full-block opcode traces')
    .replace(/^整块 TFG 已生成$/, 'Full-block TFG generated')
    .replace(/^已完成交易 (\d+\/(?:\d+))$/, 'Transactions completed: $1')
    .replace(/^复用区块 (\d+) 的已有分析结果$/, 'Using cached analysis for block $1')
}

watch(selectedTx, () => {
  cycleKind.value = null; cycleIndex.value = 0; mevMode.value = null
  sandwichIndex.value = 0; sandwichTxPosition.value = 0; arbitrageTxIndex.value = 0; arbitrageCycleIndex.value = 0
})
watch(viewTxIndex, (next, previous) => {
  if (next !== null && next !== previous) hideTransactionIsolatedTransfers.value = false
})
watch([selectedTx, graph], () => {
  timelineStep.value = timelineTotal.value
  if (sandwichIndex.value >= (graph.value?.sandwiches.length ?? 0)) sandwichIndex.value = 0
  if (arbitrageTxIndex.value >= arbitrageTransactions.value.length) arbitrageTxIndex.value = 0
})

async function analyze() {
  loading.value = true; graph.value = null; selectedTx.value = null; mevMode.value = null; progress.value = 0
  try {
    const response = await fetch(apiUrl('/api/analyze'), { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({block: props.blockNumber}) })
    const { job_id } = await readApiJson<{ job_id: string }>(response)
    while (true) {
      await new Promise(resolve => setTimeout(resolve, 1000))
      const job = await fetch(apiUrl(`/api/jobs/${job_id}`)).then(response => readApiJson<any>(response))
      status.value = englishStatus(job.message)
      progress.value = job.total ? job.completed / job.total : 0
      if (job.status === 'error') throw new Error(job.message)
      if (job.status === 'complete') {
        graph.value = await fetch(apiUrl(`/api/blocks/${job.block}`)).then(response => readApiJson<BlockGraph>(response))
        status.value = `Block ${job.block} loaded`; progress.value = 1; break
      }
    }
  } catch (error) { status.value = englishStatus(error instanceof Error ? error.message : String(error)) }
  finally { loading.value = false }
}

function moveTx(delta: number) {
  if (!graph.value?.transactions.length) return
  const current = txPosition.value < 0 ? (delta > 0 ? -1 : 0) : txPosition.value
  const next = Math.max(0, Math.min(graph.value.transactions.length - 1, current + delta))
  selectedTx.value = graph.value.transactions[next].index
}

function moveTimeline(delta: number) {
  if (!timelineTotal.value) return
  timelineStep.value = Math.max(1, Math.min(timelineTotal.value, timelineStep.value + delta))
}

function toggleTokenCycles() {
  if (cycleKind.value === 'token') cycleKind.value = null
  else { cycleKind.value = 'token'; cycleIndex.value = 0 }
}

function moveCycle(delta: number) {
  const count = activeCycles.value.length
  if (count) cycleIndex.value = (cycleIndex.value + delta + count) % count
}

function showFullGraph() {
  selectedTx.value = null
  mevMode.value = null
}

function showMev(mode: 'sandwich' | 'arbitrage') {
  const count = mode === 'sandwich' ? graph.value?.sandwiches.length ?? 0 : arbitrageTransactions.value.length
  if (!count) return
  if (mevMode.value === mode) {
    mevMode.value = null
    return
  }
  mevMode.value = mode
  hideTransactionIsolatedTransfers.value = false
  sandwichIndex.value = 0
  sandwichTxPosition.value = 0
  arbitrageTxIndex.value = 0
  arbitrageCycleIndex.value = 0
  timelineStep.value = timelineTotal.value
}

function moveSandwich(delta: number) {
  const count = graph.value?.sandwiches.length ?? 0
  if (!count) return
  sandwichIndex.value = (sandwichIndex.value + delta + count) % count
  sandwichTxPosition.value = 0
  timelineStep.value = timelineTotal.value
}

function moveSandwichTx(delta: number) {
  const count = activeSandwichTransactions.value.length
  if (count) sandwichTxPosition.value = (sandwichTxPosition.value + delta + count) % count
}

function moveArbitrageTx(delta: number) {
  const count = arbitrageTransactions.value.length
  if (!count) return
  arbitrageTxIndex.value = (arbitrageTxIndex.value + delta + count) % count
  arbitrageCycleIndex.value = 0
}

function moveArbitrageCycle(delta: number) {
  const count = activeArbitrageTx.value?.token_cycles.length ?? 0
  if (count) arbitrageCycleIndex.value = (arbitrageCycleIndex.value + delta + count) % count
}

function tokenSymbol(address: string) {
  return graph.value?.tokens.find(token => token.address.toLowerCase() === address.toLowerCase())?.symbol ?? address
}

function cyclePathLabel(cycle: { token_address_path: string[]; token_branches?: Array<{token_in_address: string; token_out_address: string}>; is_branched?: boolean }) {
  if (cycle.is_branched && cycle.token_branches?.length) {
    return cycle.token_branches
      .map(branch => `${tokenSymbol(branch.token_in_address)} → ${tokenSymbol(branch.token_out_address)}`)
      .join(' · ')
  }
  return cycle.token_address_path.map(tokenSymbol).join(' → ')
}

function nodeAlias(address: string) {
  return graph.value?.nodes.find(node => node.id.toLowerCase() === address.toLowerCase())?.alias ?? address
}

function formatRawTokenAmount(raw: string, tokenAddress: string) {
  const decimals = graph.value?.tokens.find(token => token.address.toLowerCase() === tokenAddress.toLowerCase())?.decimals ?? 0
  const negative = raw.startsWith('-')
  const digits = (negative ? raw.slice(1) : raw).padStart(decimals + 1, '0')
  if (!decimals) return `${negative ? '-' : ''}${digits}`
  const whole = digits.slice(0, -decimals)
  const fraction = digits.slice(-decimals).replace(/0+$/, '')
  return `${negative ? '-' : ''}${whole}${fraction ? `.${fraction}` : ''}`
}

function shortHash(value: string) { return `${value.slice(0, 10)}…${value.slice(-6)}` }

async function copyText(value: string, label: string) {
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(value)
    else {
      const area = document.createElement('textarea')
      area.value = value; area.style.position = 'fixed'; area.style.opacity = '0'
      document.body.appendChild(area); area.select(); document.execCommand('copy'); area.remove()
    }
    status.value = `${label} copied: ${value}`
  } catch { status.value = `Could not copy ${label.toLowerCase()}` }
}
onMounted(async () => {
  try {
    graph.value = await fetch(apiUrl(`/api/blocks/${props.blockNumber}`)).then(response => readApiJson<BlockGraph>(response))
    status.value = `Block ${props.blockNumber} loaded from cache`
  } catch {
    await analyze()
  }
})
</script>

<template>
  <main>
    <section class="metrics top-metrics">
      <article class="block-metric">
        <button class="secondary compact-button detail-home" type="button" @click="emit('home')">← Explorer</button>
        <div><span>BLOCK</span><strong>{{ graph?.block.number ?? props.blockNumber }}</strong></div>
      </article>
      <article><span>TRANSACTIONS</span><strong>{{ graph?.block.transaction_count ?? '—' }}</strong></article>
      <article><span>TRACE SUCCESS</span><strong>{{ graph ? `${graph.block.successful_traces}/${graph.block.transaction_count}` : '—' }}</strong></article>
      <article><span>{{ viewTxIndex !== null ? 'CURRENT / TOKEN FLOWS' : hideBlockIsolatedTransfers ? 'VISIBLE / TOKEN FLOWS' : 'TOKEN FLOWS' }}</span><strong>{{ graph ? tokenFlowMetric : '—' }}</strong></article>
      <article><span>TOKENS</span><strong>{{ graph ? visibleTokens.length : '—' }}</strong></article>
    </section>

    <section class="statusbar" aria-live="polite">
      <span>{{ status }}</span>
      <div v-if="loading" class="progress"><i :style="{width: `${progress * 100}%`}" /></div>
    </section>

    <template v-if="graph">
      <section class="workspace">
        <aside class="sidebar">
          <div class="panel-heading"><h2>View Scope</h2><span>{{ activeSandwichTx ? `Sandwich · ${activeSandwichTx.label} · TX #${activeSandwichTx.index}` : activeArbitrageTx ? `Arbitrage · TX #${activeArbitrageTx.index}` : selectedTx === null ? 'Full block' : `TX #${selectedTx}` }}</span></div>
          <button class="wide secondary compact-button" :class="{active: selectedTx === null && mevMode === null}" @click="showFullGraph">Full Block TFG</button>
          <label for="transaction">Transaction</label>
          <div class="transaction-control" :class="{'has-navigation': selectedTx !== null}">
            <select id="transaction" v-model="selectedTx">
              <option :value="null">Full block</option>
              <option v-for="tx in graph.transactions" :key="tx.hash" :value="tx.index">#{{ tx.index }} · {{ shortHash(tx.hash) }} · {{ tx.edge_count }} edges</option>
            </select>
            <template v-if="selectedTx !== null">
              <button class="secondary compact-button transaction-page" :disabled="txPosition <= 0" aria-label="Previous transaction" title="Previous transaction" @click="moveTx(-1)">←</button>
              <button class="secondary compact-button transaction-page" :disabled="txPosition >= graph.transactions.length - 1" aria-label="Next transaction" title="Next transaction" @click="moveTx(1)">→</button>
            </template>
          </div>
          <div v-if="mevMode === null" class="timeline-control">
            <div class="timeline-heading">
              <h3>Timeline</h3>
              <span>{{ selectedTx === null ? 'BY TRANSACTION' : 'BY EDGE' }}</span>
            </div>
            <div class="timeline-stepper">
              <button class="secondary" :disabled="timelineStep <= 1" aria-label="Previous timeline item" @click="moveTimeline(-1)">←</button>
              <output aria-live="polite">
                <strong>{{ timelineStep }}/{{ timelineTotal }}</strong>
                <small v-if="timelineItemLabel">{{ timelineItemLabel }}</small>
              </output>
              <button class="secondary" :disabled="timelineStep >= timelineTotal" aria-label="Next timeline item" @click="moveTimeline(1)">→</button>
            </div>
          </div>
          <div v-if="selectedTx === null" class="mev-tools">
            <h3>MEV Detection</h3>
            <div class="mev-actions">
              <button class="secondary compact-button" :class="{active: mevMode === 'sandwich'}" :disabled="!graph.sandwiches.length" @click="showMev('sandwich')">Sandwiches</button>
              <button class="secondary compact-button" :class="{active: mevMode === 'arbitrage'}" :disabled="!arbitrageTransactions.length" @click="showMev('arbitrage')">Arbitrage</button>
            </div>
            <div v-if="activeSandwich" class="sandwich-result">
              <div class="sandwich-stepper">
                <button class="secondary" aria-label="Previous sandwich" @click="moveSandwich(-1)">←</button>
                <span>{{ sandwichIndex + 1 }}/{{ graph.sandwiches.length }}</span>
                <button class="secondary" aria-label="Next sandwich" @click="moveSandwich(1)">→</button>
              </div>
              <div class="sandwich-tx-stepper">
                <button class="secondary" aria-label="Previous sandwich transaction" @click="moveSandwichTx(-1)">←</button>
                <span>{{ activeSandwichTx?.label }} · TX #{{ activeSandwichTx?.index }} · {{ sandwichTxPosition + 1 }}/{{ activeSandwichTransactions.length }}</span>
                <button class="secondary" aria-label="Next sandwich transaction" @click="moveSandwichTx(1)">→</button>
              </div>
              <dl>
                <dt>Pool</dt><dd :title="activeSandwich.pool_address">{{ nodeAlias(activeSandwich.pool_address) }}</dd>
                <dt>Path</dt><dd>{{ tokenSymbol(activeSandwich.token_in_address) }} → {{ tokenSymbol(activeSandwich.token_out_address) }}</dd>
                <dt>Transactions</dt><dd>#{{ activeSandwich.front_tx_index }} → {{ activeSandwich.victim_tx_indexes.map(index => `#${index}`).join(', ') }} → #{{ activeSandwich.back_tx_index }}</dd>
                <dt>Gross Profit</dt><dd class="good">+{{ formatRawTokenAmount(activeSandwich.gross_profit_amount_raw, activeSandwich.profit_token_address) }} {{ tokenSymbol(activeSandwich.profit_token_address) }}</dd>
              </dl>
              <div class="sandwich-roles" aria-label="Sandwich edge roles">
                <span><i class="front" />Front-run</span><span><i class="victim" />Victim</span><span><i class="back" />Back-run</span>
              </div>
            </div>
            <div v-else-if="activeArbitrageTx && activeArbitrageCycle" class="sandwich-result">
              <div class="sandwich-stepper">
                <button class="secondary" aria-label="Previous arbitrage transaction" @click="moveArbitrageTx(-1)">←</button>
                <span>Transaction {{ arbitrageTxIndex + 1 }}/{{ arbitrageTransactions.length }} · TX #{{ activeArbitrageTx.index }}</span>
                <button class="secondary" aria-label="Next arbitrage transaction" @click="moveArbitrageTx(1)">→</button>
              </div>
              <dl>
                <dt>Hash</dt><dd class="copy-value" :title="activeArbitrageTx.hash"><span>{{ shortHash(activeArbitrageTx.hash) }}</span><button class="secondary icon-button" aria-label="Copy transaction hash" title="Copy transaction hash" @click="copyText(activeArbitrageTx.hash, 'Transaction hash')"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 8h11v11H8zM5 16H4V4h12v1" /></svg></button></dd>
                <dt>{{ activeArbitrageCycle.is_branched ? 'Branch Paths' : 'Arbitrage Path' }}</dt><dd>{{ cyclePathLabel(activeArbitrageCycle) }}</dd>
                <dt>Profit Change</dt><dd :class="activeArbitrageCycle.amount_delta_raw.startsWith('-') ? 'bad' : 'good'">{{ formatRawTokenAmount(activeArbitrageCycle.amount_delta_raw, activeArbitrageCycle.token_address_path[0]) }} {{ tokenSymbol(activeArbitrageCycle.token_address_path[0]) }}</dd>
              </dl>
              <div v-if="activeArbitrageTx.token_cycles.length > 1" class="inner-cycle-stepper">
                <button class="secondary" aria-label="Previous arbitrage cycle" @click="moveArbitrageCycle(-1)">←</button>
                <span>Cycle {{ arbitrageCycleIndex + 1 }}/{{ activeArbitrageTx.token_cycles.length }}</span>
                <button class="secondary" aria-label="Next arbitrage cycle" @click="moveArbitrageCycle(1)">→</button>
              </div>
            </div>
            <p v-else-if="mevMode === null && (!graph.sandwiches.length || !arbitrageTransactions.length)" class="detection-note">
              {{ !graph.sandwiches.length && !arbitrageTransactions.length ? 'No MEV candidates found' : !graph.sandwiches.length ? 'No sandwich candidates found' : 'No arbitrage candidates found' }}
            </p>
          </div>
          <div class="tools-panel">
            <h3>Tools</h3>
            <div class="filter-control">
              <label class="check">
                <input v-model="hideIsolatedTransfers" type="checkbox" />
                Extract cycles
              </label>
              <span v-if="hideIsolatedTransfers">{{ prunedEdgeCount }} non-cycle edges hidden</span>
            </div>
            <label class="check"><input v-model="localLayout" type="checkbox" /> Auto Relayout</label>
            <button class="wide compact-button tool-action" @click="relayoutNonce++">Relayout View</button>
          </div>

          <div v-if="selected" class="tx-detail">
            <h3>Transaction Details</h3>
            <dl><dt>Hash</dt><dd class="copy-value" :title="selected.hash"><span>{{ shortHash(selected.hash) }}</span><button class="secondary icon-button" aria-label="Copy transaction hash" title="Copy transaction hash" @click="copyText(selected.hash, 'Transaction hash')"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 8h11v11H8zM5 16H4V4h12v1" /></svg></button></dd><dt>Flows</dt><dd>{{ selected.edge_count }}</dd><dt>Status</dt><dd :class="selected.status === 'error' ? 'bad' : 'good'">{{ selected.status }}</dd></dl>
          </div>

          <div v-if="selected?.token_cycles.length" class="cycle-tools">
            <button class="wide secondary compact-button" :class="{active: cycleKind === 'token'}" @click="toggleTokenCycles">Arbitrage Cycles ({{ selected.token_cycles.length }})</button>
            <div v-if="activeCycle" class="cycle-result">
              <button class="secondary" aria-label="Previous cycle" @click="moveCycle(-1)">←</button>
              <span>{{ cycleIndex + 1 }}/{{ activeCycles.length }} · {{ cyclePathLabel(activeCycle) }}</span>
              <button class="secondary" aria-label="Next cycle" @click="moveCycle(1)">→</button>
            </div>
          </div>

          <div class="legend">
            <button class="legend-toggle" :aria-expanded="legendExpanded" @click="legendExpanded = !legendExpanded"><span>Token Colors</span><span>{{ legendExpanded ? 'Hide' : `Show (${visibleTokens.length})` }}</span></button>
            <ul v-if="legendExpanded"><li v-for="token in visibleTokens" :key="token.address" :title="token.address"><i :style="{background: token.color}" /><span>{{ token.symbol }}</span></li></ul>
          </div>
        </aside>
        <section class="canvas-panel">
          <GraphCanvas
            :graph="graph"
            :tx-index="selectedTx"
            :relayout-nonce="relayoutNonce"
            :local-layout="localLayout"
            :hide-isolated-transfers="hideIsolatedTransfers"
            :pruned-edge-ids="prunedEdgeIds"
            :highlighted-edge-ids="highlightedEdgeIds"
            :sandwich-edge-roles="sandwichEdgeRoles"
            :focus-tx-indexes="focusTxIndexes"
            :timeline-step="timelineStep"
            @copied="(value: string) => status = `Node address copied: ${value}`"
          />
        </section>
      </section>
    </template>
    <section v-else-if="!loading" class="welcome"><div class="orbit" /><h2>Block {{ props.blockNumber }} could not be loaded</h2><p>{{ status }}</p><button @click="analyze">Retry analysis</button></section>
  </main>
</template>
