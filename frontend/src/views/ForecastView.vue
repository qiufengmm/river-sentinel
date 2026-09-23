<script setup lang="ts">
import { computed, onMounted, reactive } from 'vue';
import DataModeBanner from '@/components/DataModeBanner.vue';
import MetricTable from '@/components/MetricTable.vue';
import RiskCard from '@/components/RiskCard.vue';
import UnavailableState from '@/components/UnavailableState.vue';
import WaterLevelChart from '@/components/WaterLevelChart.vue';
import { useDashboardStore } from '@/stores/dashboard';
import { selectEventMetrics } from '@/api/adapters';
import type { ForecastHorizon, ModelName } from '@/types/api';
import {
  DISCLAIMER,
  HORIZON_OPTIONS,
  RESEARCH_THRESHOLD_LABEL,
  eventSelectionLabel,
  formatMetric,
  formatTime,
} from '@/types/view';

const store = useDashboardStore();
const state = store.state;

const form = reactive({
  asOf: '',
  model: '',
});

const selectedHorizons = reactive<{ value: ForecastHorizon[] }>({ value: [1, 3, 6] });

const failed = computed(() => state.status === 'unavailable' || state.status === 'error');
const predictions = computed(() => state.forecast?.predictions ?? []);
const observed = computed(() => state.history?.points ?? []);

/** 事件级指标：显式选择 segment=test、当前首个时域、全部模型，不做隐式取值。 */
const eventSelection = computed(() => ({
  segment: 'test',
  horizon: selectedHorizons.value[0] ?? null,
  models: null as string[] | null,
}));
const eventRows = computed(() => selectEventMetrics(state.comparison?.event_rows ?? [], eventSelection.value));
const eventSelectionText = computed(() => eventSelectionLabel(eventSelection.value));

function toggleHorizon(horizon: ForecastHorizon): void {
  if (selectedHorizons.value.includes(horizon)) {
    selectedHorizons.value = selectedHorizons.value.filter((item) => item !== horizon);
    return;
  }
  selectedHorizons.value = [...selectedHorizons.value, horizon].sort((a, b) => a - b);
}

async function requestForecast(): Promise<void> {
  const model = (form.model.trim() || 'persistence') as ModelName;
  await store.requestForecast({
    as_of: form.asOf || undefined,
    horizons: selectedHorizons.value.length > 0 ? [...selectedHorizons.value] : [1, 3, 6],
    model,
    data_mode: 'history_replay',
  });
  await store.evaluateRisk({ as_of: form.asOf || undefined, horizons: [...selectedHorizons.value] });
}

async function loadComparison(): Promise<void> {
  await store.loadComparison(selectedHorizons.value.length > 0 ? [...selectedHorizons.value] : [1, 3, 6]);
}

onMounted(async () => {
  if (!state.history) await store.loadHistory();
  await loadComparison();
});
</script>

<template>
  <div class="view">
    <header class="view__header">
      <h2>预测与模型对比</h2>
      <p class="view__desc">
        展示未来 1、3、6 小时（真实采样频率不支持时为 1、3、6 个有效时间步）的预测结果、模型指标与限制。不可用模型显示
        “不可用/未提供”，不绘制为 0。
      </p>
    </header>

    <section class="filters">
      <div class="filters__horizons">
        <span class="filters__label">预测时域</span>
        <label v-for="option in HORIZON_OPTIONS" :key="option.value" class="filters__check">
          <input
            type="checkbox"
            :checked="selectedHorizons.value.includes(option.value)"
            @change="toggleHorizon(option.value)"
          />
          {{ option.label }}
        </label>
      </div>
      <label class="filters__field">
        基准时间（as_of）
        <input v-model="form.asOf" type="datetime-local" />
      </label>
      <label class="filters__field">
        模型（留空由后端选择）
        <input v-model="form.model" type="text" placeholder="例如 xgboost / persistence" />
      </label>
      <div class="filters__actions">
        <button type="button" :disabled="state.loading" @click="requestForecast">
          {{ state.loading ? '请求中…' : '请求预测' }}
        </button>
        <button type="button" class="filters__secondary" :disabled="state.loading" @click="loadComparison">
          加载模型对比
        </button>
      </div>
    </section>

    <DataModeBanner :evidence="state.evidence" :warnings="state.warnings" :status="state.status" />

    <p v-if="state.forecast?.is_fallback" class="degraded">
      当前为降级结果，已回退到已验证的持久性基线（{{ state.forecast?.model ?? '未提供' }}）：{{
        state.forecast?.fallback_reason ?? '未提供降级原因'
      }}
    </p>

    <UnavailableState
      v-if="failed"
      :status="state.status ?? 'unavailable'"
      title="预测服务不可用"
      :reason="state.error"
      hint="未获取到预测结果，页面不展示任何替代预测值。"
    />
    <UnavailableState
      v-else-if="predictions.length === 0"
      status="empty"
      title="暂无预测结果"
      hint="请点击“请求预测”获取结果；后端无可用模型时不展示任何替代数值。"
    />

    <section v-else class="forecast">
      <p class="forecast__meta">
        模型：{{ state.forecast?.model ?? '未提供' }}｜模型版本：{{ state.forecast?.model_version ?? '未提供' }}｜基准时间：{{
          formatTime(state.forecast?.as_of)
        }}
      </p>
      <ul class="forecast__list">
        <li v-for="point in predictions" :key="`${point.source}-${point.horizon}`">
          {{ point.source }} · h{{ point.horizon }} · {{ formatTime(point.target_at) }} ·
          <template v-if="point.water_level_m !== null && point.water_level_m !== undefined">
            {{ formatMetric(point.water_level_m, 3) }} m
          </template>
          <template v-else>未提供</template>
        </li>
      </ul>
    </section>

    <WaterLevelChart
      :observed="observed"
      :predictions="predictions"
      :threshold="state.risk?.threshold ?? null"
      title="观测与预测水位（历史回放基线）"
    />

    <RiskCard :verdict="state.risk" :status="state.status" :skipped-reason="state.riskSkippedReason" />

    <MetricTable
      :rows="state.comparison?.rows ?? []"
      :event-rows="eventRows"
      :selection-label="eventSelectionText"
      :limitations="state.comparison?.limitations ?? null"
    />

    <p class="view__note">
      研究性高水位阈值：{{ RESEARCH_THRESHOLD_LABEL }}，仅用于教学科研口径，不作为官方警戒或超警标准。
    </p>
    <p class="view__disclaimer">{{ DISCLAIMER }}</p>
  </div>
</template>

<style scoped>
.view__header h2 {
  margin: 0 0 4px;
  font-size: 18px;
}

.view__desc {
  margin: 0 0 12px;
  font-size: 13px;
  color: #475569;
  line-height: 1.6;
}

.filters {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  align-items: end;
}

.filters__horizons {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}

.filters__label {
  font-size: 12px;
  color: #475569;
}

.filters__check {
  display: flex;
  gap: 4px;
  align-items: center;
  font-size: 13px;
}

.filters__field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: #475569;
}

.filters input[type='text'],
.filters input[type='datetime-local'] {
  padding: 6px 8px;
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  font-size: 13px;
}

.filters__actions {
  display: flex;
  gap: 8px;
}

.filters__actions button {
  padding: 8px 14px;
  border: none;
  border-radius: 6px;
  background: #1d4ed8;
  color: #ffffff;
  font-size: 13px;
  cursor: pointer;
}

.filters__actions .filters__secondary {
  background: #0f766e;
}

.filters__actions button:disabled {
  background: #94a3b8;
  cursor: not-allowed;
}

.degraded {
  margin-top: 12px;
  padding: 8px 12px;
  border-radius: 6px;
  background: #fef9c3;
  color: #854d0e;
  font-size: 13px;
}

.forecast {
  margin-top: 12px;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 12px 16px;
  background: #ffffff;
}

.forecast__meta {
  margin: 0;
  font-size: 13px;
  color: #334155;
}

.forecast__list {
  margin: 8px 0 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.8;
}

.view__note,
.view__disclaimer {
  margin: 12px 0 0;
  font-size: 13px;
  color: #475569;
}

.view__disclaimer {
  font-weight: 600;
  color: #b91c1c;
}
</style>
