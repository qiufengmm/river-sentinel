<script setup lang="ts">
import { computed } from 'vue';
import type { EventMetricRow, MetricRow } from '@/types/api';
import {
  EVENT_METRICS_UNAVAILABLE,
  METRIC_UNAVAILABLE,
  eventMetricsText,
  formatMetric,
  sortMetricRows,
} from '@/types/view';
import UnavailableState from './UnavailableState.vue';

const props = withDefaults(
  defineProps<{
    /** 后端 metrics 适配后的回归指标行。 */
    rows?: MetricRow[];
    /** 后端 event_metrics 嵌套结构按明确条件选择后的行。 */
    eventRows?: EventMetricRow[];
    /** 事件级指标的选择说明（segment / horizon / model）。 */
    selectionLabel?: string | null;
    limitations?: string[] | null;
  }>(),
  { rows: () => [], eventRows: () => [], selectionLabel: null, limitations: null },
);

const sortedRows = computed(() => sortMetricRows(props.rows ?? []));
const hasRows = computed(() => (props.rows ?? []).length > 0);
const eventRows = computed(() => props.eventRows ?? []);
/** 零事件时后端返回 precision/recall/f1 = null，此处必须显示“事件级指标无法计算”。 */
const allEventUnavailable = computed(() => eventRows.value.every((row) => !row.computable));
</script>

<template>
  <section class="metrics">
    <header class="metrics__header">
      <h3 class="metrics__title">模型指标对比</h3>
      <span class="metrics__caption">指标来自后端实验产物，前端不重新计算</span>
    </header>

    <UnavailableState
      v-if="!hasRows"
      status="empty"
      title="暂无模型指标"
      hint="后端未返回模型对比结果，不显示任何替代指标。"
    />
    <table v-else class="metrics__table">
      <thead>
        <tr>
          <th>模型</th>
          <th>版本</th>
          <th>时域</th>
          <th>样本数</th>
          <th>MAE</th>
          <th>RMSE</th>
          <th>R²</th>
          <th>NSE</th>
          <th>状态</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in sortedRows"
          :key="`${row.model}-${row.horizon}-${row.segment}`"
          :class="{ 'metrics__row--off': !row.supported }"
        >
          <td>{{ row.display_name || row.model }}</td>
          <td>{{ row.model_version || '未提供' }}</td>
          <td>{{ row.horizon }} 小时</td>
          <td>{{ row.sample_count ?? '未提供' }}</td>
          <td>{{ row.supported ? formatMetric(row.mae) : METRIC_UNAVAILABLE }}</td>
          <td>{{ row.supported ? formatMetric(row.rmse) : METRIC_UNAVAILABLE }}</td>
          <td>{{ row.supported ? formatMetric(row.r2) : METRIC_UNAVAILABLE }}</td>
          <td>{{ row.supported ? formatMetric(row.nse) : METRIC_UNAVAILABLE }}</td>
          <td>
            <span v-if="row.supported" class="metrics__ok">可用</span>
            <span v-else class="metrics__off">不可用/未提供</span>
            <span v-if="row.reason" class="metrics__reason">{{ row.reason }}</span>
          </td>
        </tr>
      </tbody>
    </table>

    <div class="metrics__event" :class="{ 'metrics__event--off': allEventUnavailable }">
      <p class="metrics__event-caption">
        事件级指标<span v-if="props.selectionLabel">（{{ props.selectionLabel }}）</span>
      </p>
      <p v-if="eventRows.length === 0" class="metrics__event-line">
        {{ EVENT_METRICS_UNAVAILABLE }}（no_observed_events）
      </p>
      <ul v-else class="metrics__event-list">
        <li v-for="row in eventRows" :key="`${row.segment}-${row.horizon}-${row.model}`">
          <template v-if="row.computable">
            h{{ row.horizon }} · {{ row.display_name || row.model }}：{{ eventMetricsText(row).text }}
          </template>
          <template v-else>
            h{{ row.horizon }} · {{ row.display_name || row.model }}：{{ EVENT_METRICS_UNAVAILABLE }}（{{
              row.reason ?? 'no_observed_events'
            }}）
          </template>
        </li>
      </ul>
    </div>

    <ul v-if="props.limitations && props.limitations.length > 0" class="metrics__limitations">
      <li v-for="item in props.limitations ?? []" :key="item">{{ item }}</li>
    </ul>
  </section>
</template>

<style scoped>
.metrics {
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 12px 16px;
  background: #ffffff;
}

.metrics__header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.metrics__title {
  margin: 0;
  font-size: 15px;
}

.metrics__caption {
  font-size: 12px;
  color: #64748b;
}

.metrics__table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 10px;
  font-size: 13px;
}

.metrics__table th,
.metrics__table td {
  border: 1px solid #e2e8f0;
  padding: 6px 8px;
  text-align: left;
}

.metrics__row--off {
  background: #f8fafc;
  color: #64748b;
}

.metrics__ok {
  color: #15803d;
  font-weight: 600;
}

.metrics__off {
  color: #b91c1c;
  font-weight: 600;
}

.metrics__reason {
  display: block;
  font-size: 12px;
  color: #64748b;
}

.metrics__event {
  margin: 10px 0 0;
  font-size: 13px;
}

.metrics__event-caption {
  margin: 0;
  color: #475569;
}

.metrics__event-list {
  margin: 4px 0 0;
  padding-left: 18px;
}

.metrics__event-line {
  margin: 4px 0 0;
}

.metrics__event--off {
  color: #b45309;
  font-weight: 600;
}

.metrics__limitations {
  margin: 8px 0 0;
  padding-left: 18px;
  font-size: 12px;
  color: #475569;
}
</style>
