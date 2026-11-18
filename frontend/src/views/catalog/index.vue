<template>
  <section class="page" data-module="catalog">
    <header class="page-head">
      <div>
        <h2>数据目录同步管理</h2>
        <p class="page-desc">目录同步沿用同一版本的调度执行批次，并在目录清单记录依赖完成顺序；处理结果回写调度审计台账，历史记录先兼容再核对版本边界。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出数据目录同步清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] || '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actionsFor(row)"
              :key="action"
              class="link"
              type="button"
              :disabled="pendingKeys.has(actionKey(action, row.id))"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无数据目录同步数据，可先登记同步作业</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据目录同步记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/catalog'
const columns = ['作业编号', '源路径', '目标目录', '同步模式', '调度批次版本', '依赖完成顺序', '同步频率', '作业状态']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const pendingKeys = ref<Set<string>>(new Set())

const stats = computed(() => [
  { label: '同步作业总数', value: rows.value.length },
  { label: '同步中作业', value: rows.value.filter((row) => row.status === '同步中').length },
  { label: '已暂停作业', value: rows.value.filter((row) => row.status === '已暂停').length },
  { label: '已绑定调度批次', value: rows.value.filter((row) => row.调度批次版本).length },
])

function actionsFor(row: Row): string[] {
  const base: string[] = []
  switch (row.status) {
    case '草稿':
      base.push('启动同步', '按批次同步')
      break
    case '同步中':
      base.push('暂停同步', '完成同步')
      break
    case '已暂停':
      base.push('启动同步', '归档作业')
      break
    case '已完成':
      base.push('归档作业')
      break
    default:
      break
  }
  return base
}

function actionKey(action: string, id: number | string): string {
  return `${action}-${id}`
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function postOnce(key: string, path: string, body: Record<string, any> = {}): Promise<Row | null> {
  if (pendingKeys.value.has(key)) {
    errorMessage.value = '该操作已在处理中，请勿重复提交'
    return null
  }
  pendingKeys.value.add(key)
  try {
    const response = await request(path, { method: 'POST', body: JSON.stringify({ values: body }) })
    const payload = await response.json()
    if (!payload.ok) {
      errorMessage.value = payload.message || '数据目录同步动作未生效'
      return null
    }
    errorMessage.value = ''
    return payload
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据目录同步操作失败'
    return null
  } finally {
    pendingKeys.value.delete(key)
  }
}

async function runAction(action: string, row: Row) {
  const key = actionKey(action, row.id)
  if (action === '按批次同步') {
    const batchNo = window.prompt('请输入要沿用的调度执行批次编号（留空则按历史记录口径兼容处理）：', '')
    if (batchNo === null) return
    const payload = await postOnce(key, `${ENDPOINT}/${row.id}/sync`, { 批次编号: batchNo.trim() })
    if (payload) await reload()
    return
  }
  if (action === '完成同步') {
    const batchNo = window.prompt('请输入完成同步对应的调度执行批次编号：', '')
    if (batchNo === null) return
    const payload = await postOnce(key, `${ENDPOINT}/${row.id}/complete`, { 批次编号: batchNo.trim() })
    if (payload) await reload()
    return
  }
  const payload = await postOnce(key, `${ENDPOINT}/${row.id}/actions`, { action })
  if (payload) await reload()
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) throw new Error('同步作业列表读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据目录同步列表读取失败'
  }
}

onMounted(reload)
</script>
