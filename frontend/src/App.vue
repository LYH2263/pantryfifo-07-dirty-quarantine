<template>
  <div>
    <div class="alert-bar">
      <span v-if="store.alerts.length">临期预警：{{ store.alerts.map(a => a.name + '(' + a.level + ')').join(' · ') }}</span>
      <span v-else>临期预警带：暂无紧急批次</span>
      <router-link v-if="store.quarantine" to="/quarantine" class="quar-chip">
        隔离区 {{ store.quarantine }} 批待清洗
      </router-link>
    </div>
    <div class="wrap">
      <nav class="layer-tabs">
        <router-link to="/">全层</router-link>
        <router-link to="/layer/upper">上层</router-link>
        <router-link to="/layer/mid">中层</router-link>
        <router-link to="/layer/lower">下层</router-link>
        <router-link to="/inbound">入库</router-link>
        <router-link to="/consume">消费</router-link>
        <router-link to="/quarantine">隔离</router-link>
        <router-link to="/settings">设置</router-link>
      </nav>
      <router-view />
    </div>
  </div>
</template>
<script setup>
import { watch, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { store, refreshAlerts } from './store'
const route = useRoute()
onMounted(refreshAlerts)
watch(() => route.fullPath, refreshAlerts)
</script>
