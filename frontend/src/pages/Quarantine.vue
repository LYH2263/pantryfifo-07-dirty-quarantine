<template>
  <div>
    <h1>脏批隔离</h1>
    <p class="muted">
      data_quality 为 dirty 或余量非正的批只出现在这里，不进全层正区 / 按临期候选，顶条也不把它们当普通到期紧急。
    </p>
    <p v-if="loaded && !rows.length" class="muted">隔离区已空，没有待清洗批。</p>

    <div v-for="x in rows" :key="x.id" class="qcard">
      <div class="qhead">
        <strong>{{ x.name }}</strong>
        <span :class="['lot-qty', { neg: x.qty_remain <= 0 }]">余量 {{ x.qty_remain }}</span>
        <span class="muted">到期 {{ x.expiry || '—' }}</span>
        <span class="tag" v-for="r in x.preview.reasons" :key="r">{{ reasonLabel[r] || r }}</span>
        <span class="tag expired" v-if="x.preview.expired">日历已过期</span>
      </div>
      <div class="muted">
        清洗预览（只读，不改状态）：
        <template v-if="x.preview.restore_allowed">余量为正且未过期，恢复后回正区</template>
        <template v-else>恢复不成立，确认将收口为下架（从正区与顶条消失）</template>
      </div>
      <div class="actions">
        <input type="number" v-model.number="qty[x.id]" :placeholder="'核实余量（当前 ' + x.qty_remain + '）'" />
        <button :disabled="busy === x.id" @click="confirm(x, 'restore')">确认恢复</button>
        <button class="danger" :disabled="busy === x.id" @click="confirm(x, 'discard')">报废丢弃</button>
        <button class="ghost" :disabled="busy === x.id" @click="preview(x)">刷新预览</button>
      </div>
      <div v-if="msg[x.id]" :class="['msg', msgOk[x.id] ? 'ok' : 'err']">{{ msg[x.id] }}</div>
    </div>
  </div>
</template>
<script setup>
import { reactive, ref, onMounted } from 'vue'
import { api } from '../api'

const rows = ref([])
const loaded = ref(false)
const qty = reactive({})
const msg = reactive({})
const msgOk = reactive({})
const busy = ref(null)

const reasonLabel = { dirty: 'dirty 数据', qty_non_positive: '余量非正' }

async function load() {
  rows.value = await api('/quarantine')
  loaded.value = true
}

async function preview(x) {
  // 只读取最新预览，不触碰 status
  x.preview = await api(`/quarantine/${x.id}/preview`)
  msg[x.id] = '预览已刷新（状态未改）'
  msgOk[x.id] = true
}

async function confirm(x, action) {
  busy.value = x.id
  msg[x.id] = ''
  const payload = { action }
  if (typeof qty[x.id] === 'number' && !Number.isNaN(qty[x.id])) payload.qty = qty[x.id]
  try {
    const r = await api(`/quarantine/${x.id}/clean`, { method: 'POST', body: JSON.stringify(payload) })
    if (r.status === 'on_shelf') {
      msg[x.id] = `清洗完成：批 #${x.id} 已回正区`
    } else {
      msg[x.id] = `清洗完成：批 #${x.id} 收口为下架，正区与顶条均不再出现`
    }
    msgOk[x.id] = true
    await load()  // 按收口结果：回正区则从隔离消失，下架则全部入口都消失
    window.dispatchEvent(new Event('quarantine-changed'))
  } catch (e) {
    // 清洗失败：保持隔离，服务端已回滚，正区条数不变
    msg[x.id] = '清洗被拒绝（' + e.message + '），批仍留在隔离'
    msgOk[x.id] = false
    await load()
  } finally {
    busy.value = null
  }
}

onMounted(load)
</script>
