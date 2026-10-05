import { reactive } from 'vue'
import { api } from './api'

// Shared top-bar state: expiry urgency (clean lots only) and the separate
// quarantine count, so the bar never mixes dirty batches into 临期预警.
export const store = reactive({ alerts: [], quarantine: 0 })

export async function refreshAlerts() {
  try { store.alerts = await api('/alerts') } catch { store.alerts = [] }
  try { store.quarantine = (await api('/quarantine')).length } catch { store.quarantine = 0 }
}
