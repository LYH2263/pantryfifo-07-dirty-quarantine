<template>
  <div>
    <div class="alert-bar" v-if="alerts.length">临期预警：{{ alerts.map(a => a.name + '(' + a.level + ')').join(' · ') }}</div>
    <div class="alert-bar" v-else>临期预警带：暂无紧急批次</div>
    <div class="wrap">
      <nav class="layer-tabs">
        <router-link to="/">全层</router-link>
        <router-link to="/layer/upper">上层</router-link>
        <router-link to="/layer/mid">中层</router-link>
        <router-link to="/layer/lower">下层</router-link>
        <router-link to="/inbound">入库</router-link>
        <router-link to="/consume">消费</router-link>
        <router-link to="/quarantine">隔离<span v-if="quarantineCount" class="qbadge">{{ quarantineCount }}</span></router-link>
        <router-link to="/settings">设置</router-link>
      </nav>
      <router-view />
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from './api'
import { useRouter } from 'vue-router'
const alerts = ref([])
const quarantineCount = ref(0)
const router = useRouter()

async function refresh() {
  // 顶条只反映正区（服务端已排除 dirty/非正余量）；隔离计数独立，不混进紧急预警。
  const [a, q] = await Promise.all([
    api('/alerts').catch(() => []),
    api('/quarantine').catch(() => []),
  ])
  alerts.value = a
  quarantineCount.value = q.length
}

onMounted(() => {
  refresh()
  // 清洗收口后切换页面（回全层/任意页）即按收口结果刷新顶条
  router.afterEach(refresh)
  // 停留在隔离页完成清洗时，角标也要立即更新
  window.addEventListener('quarantine-changed', refresh)
})
</script>
