<template>
  <div>
    <h1>脏批隔离</h1>
    <p class="muted">
      dirty 或余量非正的批只出现在这里：不进全层正区、不进按临期候选、顶条不当普通到期紧急。
      清洗预览不改状态；清洗确认后按收口结果回正区或下架。
    </p>
    <p v-if="!rows.length" class="muted">隔离区已空：没有待清洗的批。</p>
    <section v-for="x in rows" :key="x.id" class="q-row">
      <div class="q-head">
        <b>{{ x.name }}</b>
        <span>余量 {{ x.qty_remain }} {{ x.unit }}</span>
        <span>到期 {{ x.expiry || '—' }}</span>
        <span class="tag" v-for="r in x.reasons" :key="r">{{ tagLabel[r] || r }}</span>
      </div>
      <div class="q-fix">
        <label>清洗余量<input type="number" v-model.number="fix[x.id].qty_remain" /></label>
        <label>清洗到期<input v-model="fix[x.id].expiry" placeholder="YYYY-MM-DD" /></label>
        <button class="ghost" @click="preview(x)">清洗预览</button>
        <button @click="confirm(x)">清洗确认</button>
      </div>
      <p v-if="msg[x.id]" class="q-msg">{{ msg[x.id] }}</p>
    </section>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { refreshAlerts } from '../store'

const rows = ref([])
const fix = ref({})
const msg = ref({})
const tagLabel = { dirty: 'dirty 数据', non_positive_qty: '余量非正' }
const finalLabel = { on_shelf: '回正区（全层可见）', expired: '过期下架（不进顶条）', consumed: '核销清空' }

async function load() {
  rows.value = await api('/quarantine')
  const f = {}
  for (const x of rows.value) f[x.id] = { qty_remain: x.qty_remain, expiry: x.expiry || '' }
  fix.value = f
}
function payload(x) {
  const f = fix.value[x.id]
  const body = { lot_id: x.id, qty_remain: f.qty_remain }
  if (f.expiry) body.expiry = f.expiry
  return body
}
async function preview(x) {
  try {
    const p = await api('/quarantine/preview', { method: 'POST', body: JSON.stringify(payload(x)) })
    if (p.ok === false) { msg.value[x.id] = '该批已不在隔离区'; return }
    msg.value[x.id] = `预览（未改状态）：余量 ${p.current.qty_remain}→${p.proposed.qty_remain}，收口→${finalLabel[p.final_status] || p.final_status}`
  } catch (e) { msg.value[x.id] = '预览失败：' + e.message }
}
async function confirm(x) {
  try {
    const r = await api('/quarantine/clean', { method: 'POST', body: JSON.stringify(payload(x)) })
    if (r.ok) {
      msg.value[x.id] = `清洗完成：${finalLabel[r.lot.status] || r.lot.status}`
    } else {
      msg.value[x.id] = `该批已被收口为 ${r.lot.status}（清洗与下架只留一种状态）`
    }
    rows.value = rows.value.filter(y => y.id !== x.id)
    refreshAlerts()
  } catch (e) {
    // 清洗失败：保持隔离，正区不变
    msg.value[x.id] = '清洗失败，保持隔离：' + e.message
  }
}
onMounted(load)
</script>
