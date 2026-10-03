<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import BlockDetailView from './BlockDetailView.vue'
import HomeView from './HomeView.vue'

const path = ref(window.location.pathname)
const blockNumber = computed(() => {
  const match = path.value.match(/^\/blocks\/(\d+)\/?$/)
  return match ? Number(match[1]) : null
})

function navigate(next: string) {
  if (window.location.pathname !== next) window.history.pushState({}, '', next)
  path.value = next
}

function onPopState() { path.value = window.location.pathname }
onMounted(() => window.addEventListener('popstate', onPopState))
onBeforeUnmount(() => window.removeEventListener('popstate', onPopState))
</script>

<template>
  <BlockDetailView v-if="blockNumber !== null" :key="blockNumber" :block-number="blockNumber" @home="navigate('/')" />
  <HomeView v-else @open-block="number => navigate(`/blocks/${number}`)" />
</template>
