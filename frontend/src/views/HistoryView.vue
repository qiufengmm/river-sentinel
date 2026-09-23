<script setup lang="ts">
import { computed, onMounted, reactive } from 'vue';
import DataModeBanner from '@/components/DataModeBanner.vue';
import UnavailableState from '@/components/UnavailableState.vue';
import WaterLevelChart from '@/components/WaterLevelChart.vue';
import { useDashboardStore } from '@/stores/dashboard';
import { latestObservedPoint } from '@/api/adapters';
import { PROJECT_TIME_ZONE } from '@/utils/time';
import { DISCLAIMER, formatMetric, formatTime } from '@/types/view';

const store = useDashboardStore();
const state = store.state;

const form = reactive({
  stationId: state.stationId,
  start: '',
  end: '',
});

const failed = computed(() => state.status === 'unavailable' || state.status === 'error');
const points = computed(() => state.history?.points ?? []);
const missingCount = computed(() => state.history?.missing_count ?? 0);
const latestPoint = computed(() => latestObservedPoint(state.history));
const timeZoneNote = computed(() => `开始/结束时间按项目时区 ${PROJECT_TIME_ZONE} 发送，避免浏览器时区漂移`);

async function queryHistory(): Promise<void> {
  state.stationId = form.stationId.trim() || state.stationId;
  await store.loadHistory({ stationId: state.stationId, start: form.start || null, end: form.end || null });
}

onMounted(async () => {
  if (!state.history) await store.loadHistory();
});
</script>

<template>
  <div class="view">
    <header class="view__header">
      <h2>历史回放</h2>
      <p class="view__desc">
        历史回放：查询已归档的小型样例水位数据并绘制时间序列。本页数据不是实时监测，不代表平台向本系统持续推送。
      </p>
    </header>

    <form class="filters" @submit.prevent="queryHistory">
      <label>
        目标断面
        <input v-model="form.stationId" type="text" placeholder="cata_12720" />
      </label>
      <label>
        开始时间
        <input v-model="form.start" type="datetime-local" />
      </label>
      <label>
        结束时间
        <input v-model="form.end" type="datetime-local" />
      </label>
      <button type="submit" :disabled="state.loading">{{ state.loading ? '查询中…' : '查询历史回放' }}</button>
      <span class="filters__note">{{ timeZoneNote }}</span>
    </form>

    <DataModeBanner :evidence="state.evidence" :warnings="state.warnings" :status="state.status" />

    <UnavailableState
      v-if="failed"
      :status="state.status ?? 'unavailable'"
      title="历史水位接口不可用"
      :reason="state.error"
      hint="未获取到历史水位，不展示任何替代曲线。"
    />
    <UnavailableState
      v-else-if="points.length === 0"
      status="empty"
      title="该时间范围内没有历史水位点"
      hint="请调整目标断面或时间范围后重新查询，页面不展示任何替代数值。"
    />

    <section v-else class="summary">
      <span>返回点数：{{ points.length }}</span>
      <span>缺失点数：{{ missingCount }}</span>
      <span>最新观测时间：{{ latestPoint ? formatTime(latestPoint.observed_at) : '未提供' }}</span>
      <span>
        最新水位：{{
          latestPoint && latestPoint.water_level_m !== null
            ? `${formatMetric(latestPoint.water_level_m, 2)} m`
            : '未提供'
        }}
      </span>
    </section>

    <WaterLevelChart
      :observed="points"
      :threshold="state.risk?.threshold ?? null"
      title="历史回放水位序列"
    />

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
  gap: 12px;
  flex-wrap: wrap;
  align-items: end;
  margin-bottom: 12px;
}

.filters label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: #475569;
}

.filters input {
  padding: 6px 8px;
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  font-size: 13px;
}

.filters button {
  padding: 8px 14px;
  border: none;
  border-radius: 6px;
  background: #1d4ed8;
  color: #ffffff;
  font-size: 13px;
  cursor: pointer;
}

.filters button:disabled {
  background: #94a3b8;
  cursor: not-allowed;
}

.filters__note {
  font-size: 12px;
  color: #64748b;
}

.summary {
  display: flex;
  gap: 18px;
  flex-wrap: wrap;
  margin-top: 12px;
  font-size: 13px;
  color: #334155;
}

.view__disclaimer {
  margin: 12px 0 0;
  font-size: 13px;
  font-weight: 600;
  color: #b91c1c;
}
</style>
