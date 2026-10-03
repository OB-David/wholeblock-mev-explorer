<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { apiUrl, readApiJson } from './api'
import type { ExplorerBlock, ExplorerResponse } from './types'

const emit = defineEmits<{ openBlock: [number: number] }>()
const data = ref<ExplorerResponse | null>(null)
const loading = ref(true)
const error = ref('')
const blockInput = ref('')
const selected = ref<ExplorerBlock | null>(null)
const pageIndex = ref(0)
const pageAnchors = ref<Array<number | null>>([null])
const PAGE_SIZE = 180
let timer: number | undefined
let refreshVersion = 0

const trackedBlocks = computed(() => data.value?.blocks.filter(block => block.scan_status === 'scanned').length ?? 0)
const flaggedBlocks = computed(() => data.value?.blocks.filter(block => block.arbitrage_count || block.sandwich_count).length ?? 0)
const medianScan = computed(() => {
  const times = (data.value?.blocks ?? []).map(block => block.scan_ms).filter((value): value is number => value !== null)
  if (!times.length) return '—'
  times.sort((a, b) => a - b)
  return `${Math.round(times[Math.floor(times.length / 2)])} ms`
})
const scannerLabel = computed(() => {
  if (data.value?.scanner_error) return 'scanner degraded'
  if (data.value?.scanning_block) return `scanning #${data.value.scanning_block}`
  if (data.value && data.value.last_scanned === data.value.latest) return 'synced to head'
  return 'catching up'
})
const pageRange = computed(() => {
  const blocks = data.value?.blocks ?? []
  if (!blocks.length) return 'No blocks'
  return `#${formatNumber(blocks[0].number)} – #${formatNumber(blocks[blocks.length - 1].number)}`
})

function tileClass(block: ExplorerBlock) {
  return {
    arbitrage: block.arbitrage_count > 0 && block.sandwich_count === 0,
    sandwich: block.sandwich_count > 0 && block.arbitrage_count === 0,
    both: block.arbitrage_count > 0 && block.sandwich_count > 0,
    pending: block.scan_status !== 'scanned',
    newest: block.number === data.value?.latest,
    selected: block.number === selected.value?.number,
  }
}

function tileStyle(block: ExplorerBlock, index: number) {
  const load = block.gas_limit ? Math.max(.08, Math.min(1, block.gas_used / block.gas_limit)) : .08
  return { '--gas': `${Math.round(load * 100)}%`, '--stagger': `${Math.min(index, 30) * 18}ms` }
}

function age(timestamp: number) {
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - timestamp))
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  return `${Math.floor(seconds / 3600)}h ago`
}

function formatNumber(value: number) { return new Intl.NumberFormat('en-US').format(value) }
function shortBlock(value: number) { return String(value).slice(-5).padStart(5, '0') }

async function refresh(silent = false) {
  const version = ++refreshVersion
  if (!silent) loading.value = true
  try {
    const anchor = pageAnchors.value[pageIndex.value]
    const query = new URLSearchParams({ limit: String(PAGE_SIZE) })
    if (anchor !== null && anchor !== undefined) query.set('before', String(anchor))
    const response = await fetch(apiUrl(`/api/explorer?${query}`))
    const next = await readApiJson<ExplorerResponse>(response)
    if (version !== refreshVersion) return
    const selectedNumber = selected.value?.number
    data.value = next
    selected.value = next.blocks.find(block => block.number === selectedNumber) ?? next.blocks[0] ?? null
    error.value = ''
  } catch (reason) {
    if (version !== refreshVersion) return
    error.value = reason instanceof Error ? reason.message : String(reason)
  } finally {
    if (version === refreshVersion) loading.value = false
  }
}

function selectBlock(block: ExplorerBlock) {
  selected.value = block
}

async function showOlderBlocks() {
  const blocks = data.value?.blocks ?? []
  const oldest = blocks[blocks.length - 1]
  if (!oldest || !data.value?.has_older) return
  pageAnchors.value[pageIndex.value + 1] = oldest.number - 1
  pageIndex.value += 1
  await refresh()
}

async function showNewerBlocks() {
  if (pageIndex.value === 0) return
  pageIndex.value -= 1
  await refresh()
}

function openEnteredBlock() {
  const value = blockInput.value.trim().replace(/^#/, '')
  if (!/^\d+$/.test(value)) {
    error.value = 'Please enter a non-negative block number.'
    return
  }
  emit('openBlock', Number(value))
}

onMounted(async () => {
  await refresh()
  timer = window.setInterval(() => refresh(true), 4_000)
})
onBeforeUnmount(() => { if (timer !== undefined) window.clearInterval(timer) })
</script>

<template>
  <main class="explorer-page">
    <header class="explorer-nav">
      <a class="brand" href="/" @click.prevent><i />WHOLEBLOCK <span>/ TFG</span></a>
      <div class="chain-state" :class="{ degraded: data?.scanner_error }"><i />ETHEREUM · {{ scannerLabel }}</div>
    </header>

    <section class="explorer-hero">
      <div class="hero-copy">
        <p class="eyebrow">LIVE VALUE-FLOW OBSERVATORY</p>
        <h1>Explore the chain.<br /><em>See the hidden flow.</em></h1>
        <p class="hero-note">Every cell is a block. Quiet blocks recede; arbitrage and sandwich activity surface as luminous signals.</p>
      </div>
      <form class="hero-search" @submit.prevent="openEnteredBlock">
        <label for="home-block">OPEN A BLOCK TFG</label>
        <div>
          <span>#</span>
          <input id="home-block" v-model="blockInput" inputmode="numeric" autocomplete="off" placeholder="Block number" />
          <button type="submit">Explore →</button>
        </div>
        <button v-if="data" type="button" class="latest-link" @click="emit('openBlock', data.latest)">or open latest #{{ formatNumber(data.latest) }}</button>
      </form>
    </section>

    <section class="explorer-stats" aria-label="Scanner status">
      <article><span>CHAIN HEAD</span><strong>{{ data ? `#${formatNumber(data.latest)}` : '—' }}</strong><small>updates every 4 seconds</small></article>
      <article><span>PAGE SCANNED</span><strong>{{ trackedBlocks }} / {{ data?.blocks.length ?? 0 }}</strong><small>{{ data?.backfill_block ? `backfilling #${formatNumber(data.backfill_block)}` : `${formatNumber(data?.history_blocks ?? 0)}-block startup window` }}</small></article>
      <article><span>FLAGGED BLOCKS</span><strong>{{ flaggedBlocks }}</strong><small>in the visible field</small></article>
      <article><span>MEDIAN SCAN</span><strong>{{ medianScan }}</strong><small>receipt-level detector</small></article>
    </section>

    <section class="block-field">
      <div class="field-heading">
        <div><p class="eyebrow">BLOCK EXPLORATION</p><h2>Newest blocks first</h2></div>
        <div class="field-legend">
          <span><i class="clean" />Scanned</span><span><i class="arb" />Arbitrage</span><span><i class="sand" />Sandwich</span><span><i class="dual" />Both</span>
        </div>
      </div>

      <div v-if="loading" class="field-message"><i class="loader" />Reading the chain head…</div>
      <div v-else-if="error && !data" class="field-message error-message">{{ error }}<button @click="refresh()">Retry</button></div>
      <div v-else class="field-results">
        <div class="field-layout">
          <div class="block-grid" role="list" aria-label="Ethereum blocks on this page">
            <button
              v-for="(block, index) in data?.blocks" :key="block.number" role="listitem"
              class="block-cell" :class="tileClass(block)" :style="tileStyle(block, index)"
              :aria-label="`Select block ${block.number}, ${block.arbitrage_count} arbitrages, ${block.sandwich_count} sandwiches`"
              :aria-pressed="selected?.number === block.number"
              @pointerenter="selectBlock(block)" @focus="selectBlock(block)" @click="selectBlock(block)"
            >
              <span class="gas-fill" />
              <span class="block-number">{{ shortBlock(block.number) }}</span>
              <span v-if="block.arbitrage_count || block.sandwich_count" class="signal-dots"><i v-if="block.arbitrage_count" /><i v-if="block.sandwich_count" /></span>
            </button>
          </div>

          <aside v-if="selected" class="block-peek">
            <p class="eyebrow">SELECTED BLOCK</p>
            <h3>#{{ formatNumber(selected.number) }}</h3>
            <p class="block-age">{{ age(selected.timestamp) }}</p>
            <div class="peek-metrics">
              <span>Transactions<strong>{{ selected.transaction_count }}</strong></span>
              <span>DEX swaps<strong>{{ selected.swap_count || '—' }}</strong></span>
              <span>Gas used<strong>{{ selected.gas_limit ? `${Math.round(selected.gas_used / selected.gas_limit * 100)}%` : '—' }}</strong></span>
              <span>Quick scan<strong>{{ selected.scan_ms === null ? 'pending' : `${Math.round(selected.scan_ms)} ms` }}</strong></span>
            </div>
            <div class="peek-signals">
              <div class="arb"><i /><span>Arbitrage routes</span><strong>{{ selected.arbitrage_count }}</strong></div>
              <div class="sand"><i /><span>Sandwich attacks</span><strong>{{ selected.sandwich_count }}</strong></div>
            </div>
            <button class="peek-open" @click="emit('openBlock', selected.number)">Open full TFG <span>→</span></button>
            <small v-if="selected.scan_status !== 'scanned'">This block is still queued for quick scanning. Its full TFG remains available on demand.</small>
          </aside>
        </div>

        <nav class="block-pagination" aria-label="Block pages">
          <button class="secondary" :disabled="pageIndex === 0" @click="showNewerBlocks">← Newer blocks</button>
          <span><strong>Page {{ pageIndex + 1 }}</strong><small>{{ pageRange }}</small></span>
          <button class="secondary" :disabled="!data?.has_older" @click="showOlderBlocks">Older blocks →</button>
        </nav>
      </div>
      <p v-if="error && data" class="soft-error">Live refresh paused: {{ error }}</p>
    </section>
  </main>
</template>
