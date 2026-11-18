<template>
  <section class="page" data-module="schedule">
    <header class="page-head">
      <div>
        <h2>治理任务调度管理</h2>
        <p class="page-desc">维护治理任务的调度配置、依赖关系与重试策略；下游只能在上游依赖完成后启动，同一依赖窗口只有一个有效调度执行批次。</p>
      </div>
      <div v-if="view === 'list'" class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出治理任务调度清单</button>
      </div>
      <div v-else class="page-actions">
        <button class="btn" type="button" @click="backToList">返回调度列表</button>
      </div>
    </header>

    <!-- 列表视图 -->
    <template v-if="view === 'list'">
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
              <button class="link" type="button" @click="openDetail(row)">调度详情</button>
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
            <td :colspan="columns.length + 1" class="empty-state">暂无治理任务调度数据</td>
          </tr>
        </tbody>
      </table>

      <footer class="page-foot">
        <span>共 {{ total }} 条治理任务调度记录</span>
        <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      </footer>
    </template>

    <!-- 调度详情视图：唯一有效批次、依赖完成顺序、调度审计台账同版本核对 -->
    <template v-else>
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">任务状态</span>
          <strong class="stat-value">{{ detail?.task.status ?? '—' }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">有效调度批次（唯一）</span>
          <strong class="stat-value">{{ detail?.active_batch?.批次编号 ?? '无' }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">批次版本</span>
          <strong class="stat-value">{{ detail?.active_batch ? 'v' + detail.active_batch.批次版本 : '—' }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">失败原因</span>
          <strong class="stat-value" style="font-size:14px">{{ detail?.task.失败原因 || '—' }}</strong>
        </article>
      </div>

      <h3>任务与依赖</h3>
      <table class="data-table">
        <tbody>
          <tr><th style="width:160px">任务编号</th><td>{{ detail?.task.任务编号 }}</td></tr>
          <tr><th>任务类型</th><td>{{ detail?.task.任务类型 }}</td></tr>
          <tr><th>调度表达式</th><td>{{ detail?.task.调度表达式 }}</td></tr>
          <tr><th>依赖任务</th><td>{{ detail?.task.依赖任务 || '无上游依赖' }}</td></tr>
          <tr><th>超时 / 重试策略</th><td>{{ detail?.task.超时分钟 }} 分钟 / {{ detail?.task.重试策略 }} 次</td></tr>
        </tbody>
      </table>

      <h3>调度执行批次（同窗口唯一有效，版本只进不退）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in batchColumns" :key="column">{{ column }}</th>
            <th>批次操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="batch in detail?.batches ?? []" :key="String(batch.id)">
            <td v-for="column in batchColumns" :key="column">{{ batch[column] || '—' }}</td>
            <td class="row-actions">
              <button class="link" type="button" :disabled="pendingKeys.has('advance-' + batch.id)" @click="advanceBatch(batch)">推进批次</button>
              <button class="link" type="button" :disabled="pendingKeys.has('fail-' + batch.id)" @click="failBatch(batch)">登记失败</button>
            </td>
          </tr>
          <tr v-if="!(detail?.batches?.length)">
            <td :colspan="batchColumns.length + 1" class="empty-state">暂无调度执行批次，等待上游依赖完成后唤醒生成</td>
          </tr>
        </tbody>
      </table>

      <h3>依赖数据记录与依赖完成顺序</h3>
      <table class="data-table">
        <thead>
          <tr><th v-for="column in dependencyColumns" :key="column">{{ column }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="dep in detail?.dependencies ?? []" :key="String(dep.id)">
            <td v-for="column in dependencyColumns" :key="column">{{ dep[column] || '—' }}</td>
          </tr>
          <tr v-if="!(detail?.dependencies?.length)">
            <td :colspan="dependencyColumns.length" class="empty-state">暂无依赖记录</td>
          </tr>
        </tbody>
      </table>

      <h3>调度审计台账（同一批次版本）</h3>
      <table class="data-table">
        <thead>
          <tr><th v-for="column in auditColumns" :key="column">{{ column }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in detail?.audit ?? []" :key="String(row.id)">
            <td v-for="column in auditColumns" :key="column">{{ row[column] || '—' }}</td>
          </tr>
          <tr v-if="!(detail?.audit?.length)">
            <td :colspan="auditColumns.length" class="empty-state">暂无审计记录</td>
          </tr>
        </tbody>
      </table>

      <footer class="page-foot">
        <button class="btn" type="button" @click="backToList">返回调度列表</button>
        <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      </footer>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/schedule'
const columns = ['任务编号', '任务类型', '调度表达式', '依赖任务', '重试策略', '超时分钟', '任务状态', '失败原因']
const batchColumns = ['批次编号', '批次版本', '依赖窗口编号', '依赖完成顺序', '触发来源', '批次状态', '已重试次数', '重试上限', '超时分钟', '失败原因']
const dependencyColumns = ['窗口编号', '上游任务编号', '下游任务编号', '触发来源', '依赖完成顺序', '窗口状态', '窗口关闭', '创建时间']
const auditColumns = ['批次编号', '批次版本', '来源模块', '事件', '处理结果', '明细', '记录时间']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const view = ref<'list' | 'detail'>('list')
const detail = ref<Record<string, any> | null>(null)
const currentTaskId = ref<number | null>(null)
// 进行中的请求键：重复提交调度唤醒/重试时只生效一次，按钮同步禁用。
const pendingKeys = ref<Set<string>>(new Set())

const stats = computed(() => [
  { label: '治理任务总数', value: rows.value.length },
  { label: '运行中任务', value: rows.value.filter((row) => row.status === '运行中').length },
  { label: '已暂停任务', value: rows.value.filter((row) => row.status === '已暂停').length },
  { label: '异常/失败任务', value: rows.value.filter((row) => row.abnormal || row.失败原因).length },
])

function actionsFor(row: Row): string[] {
  switch (row.status) {
    case '草稿':
      return row.依赖任务 ? ['启动任务', '依赖唤醒'] : ['启动任务']
    case '运行中':
      return ['暂停任务', '标记上游完成', '依赖唤醒', '超时重试']
    case '已暂停':
      return ['启动任务', '上游重跑']
    case '已完成':
      return ['上游重跑', '标记上游完成']
    default:
      return []
  }
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
      errorMessage.value = payload.message || '操作未生效'
      return null
    }
    errorMessage.value = ''
    return payload
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '治理任务调度操作失败'
    return null
  } finally {
    pendingKeys.value.delete(key)
  }
}

async function runAction(action: string, row: Row) {
  const key = actionKey(action, row.id)
  let path = `${ENDPOINT}/${row.id}/actions`
  const body: Record<string, any> = { action }
  if (action === '依赖唤醒') {
    path = `${ENDPOINT}/${row.id}/wake`
    delete body.action
    body.trigger = '手动唤醒'
  } else if (action === '超时重试') {
    path = `${ENDPOINT}/${row.id}/timeout-retry`
    delete body.action
  } else if (action === '标记上游完成') {
    path = `${ENDPOINT}/${row.id}/upstream-complete`
    delete body.action
  } else if (action === '上游重跑') {
    path = `${ENDPOINT}/${row.id}/rerun`
    delete body.action
  }
  const payload = await postOnce(key, path, body)
  if (payload) await reload()
}

async function advanceBatch(batch: Row) {
  const target = window.prompt('按确认顺序推进批次，请输入目标状态（运行中 / 已完成）：', '已完成')
  if (!target) return
  const payload = await postOnce('advance-' + batch.id, `${ENDPOINT}/batches/${batch.id}/advance`, { target_state: target.trim() })
  if (payload && currentTaskId.value !== null) await loadDetail(currentTaskId.value)
}

async function failBatch(batch: Row) {
  const reason = window.prompt('请输入批次失败原因（失败任务将保留原因）：')
  if (!reason) return
  const payload = await postOnce('fail-' + batch.id, `${ENDPOINT}/batches/${batch.id}/fail`, { reason })
  if (payload && currentTaskId.value !== null) await loadDetail(currentTaskId.value)
}

async function openDetail(row: Row) {
  await loadDetail(Number(row.id))
}

async function loadDetail(taskId: number) {
  currentTaskId.value = taskId
  try {
    const response = await request(`${ENDPOINT}/${taskId}`)
    if (!response.ok) throw new Error('调度详情读取失败')
    detail.value = await response.json()
    view.value = 'detail'
    errorMessage.value = ''
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '调度详情读取失败'
  }
}

async function backToList() {
  // 核对完批次后先回到调度列表；再次打开详情会重新拉取，确保看到刷新后的唯一批次。
  view.value = 'list'
  detail.value = null
  await reload()
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) throw new Error('治理任务列表读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '治理任务调度列表读取失败'
  }
}

onMounted(reload)
</script>
