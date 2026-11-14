<template>
  <section class="resume-panel" :data-module="module">
    <header class="resume-head">
      <div>
        <h3>失败续做</h3>
        <p class="resume-desc">
          按报表任务、数据日期和指标集合复用已完成产物；相同批次重复执行只生效一次。
        </p>
      </div>
      <div class="resume-actions">
        <button class="btn" type="button" :disabled="loading" @click="loadStatus">刷新状态</button>
        <button class="btn primary" type="button" :disabled="loading" @click="runRetry">
          重试失败批次
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">已完成</span>
        <strong class="stat-value" data-field="completed">{{ stats['已完成'] ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待重试</span>
        <strong class="stat-value" data-field="pending_retry">{{ stats['待重试'] ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">复用</span>
        <strong class="stat-value" data-field="reused">{{ stats['复用'] ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">当前版本</span>
        <strong class="stat-value">{{ status?.当前版本号 || '—' }}</strong>
      </article>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th>产物 id</th>
          <th>数据日期</th>
          <th>指标集合</th>
          <th>状态</th>
          <th>是否确认</th>
          <th>版本</th>
          <th>来源任务</th>
          <th>冲突原因</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="artifact in status?.产物 ?? []" :key="String(artifact.产物id)">
          <td>{{ artifact.产物id }}</td>
          <td>{{ artifact.数据日期 }}</td>
          <td>{{ artifact.指标集合 }}</td>
          <td>{{ artifact.状态 }}</td>
          <td>{{ artifact.是否确认 ? '是' : '否' }}</td>
          <td>{{ artifact.版本号 || '—' }}</td>
          <td>{{ artifact.来源任务 || '—' }}</td>
          <td>{{ artifact.冲突原因 || '—' }}</td>
        </tr>
        <tr v-if="!(status?.产物 ?? []).length">
          <td colspan="8" class="empty-state">暂无续做产物，可先重试失败批次</td>
        </tr>
      </tbody>
    </table>

    <p v-if="message" class="resume-message" :class="{ 'error-text': failed }">{{ message }}</p>
    <p class="resume-desc">
      下载记录 {{ status?.下载记录数 ?? 0 }} 条，重复记录 {{ status?.重复下载记录数 ?? 0 }} 条；
      返回字段：{{ (status?.返回字段 ?? []).join('、') || '—' }}
    </p>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'

import { fetchJson, request } from '@/api/client'

type Artifact = {
  产物id: number
  任务编号: string
  数据日期: string
  指标集合: string
  状态: string
  是否确认: boolean
  版本号: string
  来源任务: string | null
  冲突原因: string | null
}

type ResumeStatus = {
  ok: boolean
  任务编号: string
  服务编号: string
  统计: Record<string, number>
  返回字段: string[]
  当前版本号: string
  产物: Artifact[]
  下载记录数: number
  重复下载记录数: number
}

const props = defineProps<{
  module: 'reportsch' | 'delivery'
  entryId: number | null
}>()

const status = ref<ResumeStatus | null>(null)
const stats = ref<Record<string, number>>({})
const message = ref('')
const failed = ref(false)
const loading = ref(false)

function endpoint(): string {
  return props.entryId == null ? '' : `/api/${props.module}/${props.entryId}`
}

async function loadStatus() {
  if (!endpoint()) return
  loading.value = true
  failed.value = false
  try {
    const payload = await fetchJson<ResumeStatus>(`${endpoint()}/resume`)
    status.value = payload
    stats.value = payload.统计 ?? {}
    if (!payload.ok) {
      failed.value = true
      message.value = '当前记录暂无可汇总的续做状态'
    }
  } catch (error) {
    failed.value = true
    message.value = error instanceof Error ? error.message : '续做状态读取失败'
  } finally {
    loading.value = false
  }
}

async function runRetry() {
  if (!endpoint()) return
  loading.value = true
  failed.value = false
  message.value = ''
  try {
    const response = await request(`${endpoint()}/resume/retry`, {
      method: 'POST',
      body: JSON.stringify({}),
    })
    if (!response.ok) {
      throw new Error('失败续做请求未送达')
    }
    const payload = await response.json()
    stats.value = payload.统计 ?? {}
    message.value = payload.message ?? ''
    failed.value = !payload.ok
    await loadStatus()
  } catch (error) {
    failed.value = true
    message.value = error instanceof Error ? error.message : '失败续做执行失败'
  } finally {
    loading.value = false
  }
}

watch(() => [props.module, props.entryId], () => void loadStatus())
onMounted(loadStatus)
</script>

<style scoped>
.resume-panel { margin-top: 20px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }
.resume-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.resume-head h3 { margin: 0; font-size: 15px; }
.resume-actions { display: flex; gap: 8px; }
.resume-desc { color: var(--muted); font-size: 12px; margin: 4px 0; }
.resume-message { font-size: 12px; margin: 8px 0 0; }
</style>
