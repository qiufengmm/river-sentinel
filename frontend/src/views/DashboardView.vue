<script setup lang="ts">
import { computed, onMounted } from 'vue';
import DataModeBanner from '@/components/DataModeBanner.vue';
import RiskCard from '@/components/RiskCard.vue';
import UnavailableState from '@/components/UnavailableState.vue';
import WaterLevelChart from '@/components/WaterLevelChart.vue';
import { useDashboardStore } from '@/stores/dashboard';
import { latestObservedPoint } from '@/api/adapters';
import { DISCLAIMER, formatMetric, formatTime, statusLabel } from '@/types/view';

const store = useDashboardStore();
const state = store.state;

const failed = computed(() => state.status === 'unavailable' || state.status === 'error');

/** 最新有效观测点（后端字段 water_level_m，缺失为 null）。 */
const latestPoint = computed(() => latestObservedPoint(state.history));

const missingCount = computed(() => state.history?.missing_count ?? 0);

/** 服务状态来自外层响应 status，数据体只描述可用性。 */
const serviceStatus = computed(
  () =>
    `${statusLabel(state.status ?? 'unavailable')}｜样例数据：${
      state.health?.sample_available ? '可用' : '不可用'
    }｜持久性回退：${state.health?.persistence_fallback_available ? '可用' : '不可用'}`,
);

const modelVersion = computed(() => {
  for (let i = state.evidence.length - 1; i >= 0; i -= 1) {
    const version = state.evidence[i]?.model_version;
    if (version) return version;
  }
  return '未提供';
});

onMounted(async () => {
  await store.loadHealth();
  await store.loadHistory();
  await store.evaluateRisk();
});
</script>

<template>
  <div class="view">
    <header class="view__header">
      <h2>总览</h2>
      <p class="view__desc">
        展示当前数据模式、最新观测时间、数据新鲜度、模型版本与研究性风险结果。数据来自历史回放，不是实时监测。
      </p>
    </header>

    <DataModeBanner :evidence="state.evidence" :warnings="state.warnings" :status="state.status" />

    <UnavailableState
      v-if="failed"
      :status="state.status ?? 'unavailable'"
      title="后端或数据不可用"
      :reason="state.error"
      hint="未获取到可用数据，总览页不展示任何替代水位或风险数值。"
    />

    <section class="panel">
      <h3>最新观测</h3>
      <dl class="panel__grid">
        <div>
          <dt>目标断面</dt>
          <dd>{{ state.stationId }}</dd>
        </div>
        <div>
          <dt>最新观测时间</dt>
          <dd>{{ latestPoint ? formatTime(latestPoint.observed_at) : '未提供' }}</dd>
        </div>
        <div>
          <dt>最新水位</dt>
          <dd>
            {{
              latestPoint && latestPoint.water_level_m !== null
                ? `${formatMetric(latestPoint.water_level_m, 2)} m`
                : '未提供'
            }}
          </dd>
        </div>
        <div>
          <dt>模型版本</dt>
          <dd>{{ modelVersion }}</dd>
        </div>
        <div>
          <dt>返回点数 / 缺失点数</dt>
          <dd>{{ state.history?.points?.length ?? 0 }} / {{ missingCount }}</dd>
        </div>
        <div>
          <dt>服务状态（外层响应）</dt>
          <dd>{{ serviceStatus }}</dd>
        </div>
      </dl>
      <p v-if="state.fallbackReason" class="panel__fallback">已降级展示，原因：{{ state.fallbackReason }}</p>
    </section>

    <RiskCard :verdict="state.risk" :status="state.status" :skipped-reason="state.riskSkippedReason" />

    <WaterLevelChart
      :observed="state.history?.points ?? []"
      :threshold="state.risk?.threshold ?? null"
      title="历史回放水位（总览）"
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

.panel {
  margin-top: 12px;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 12px 16px;
  background: #ffffff;
}

.panel h3 {
  margin: 0 0 8px;
  font-size: 15px;
}

.panel__grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px 20px;
  margin: 0;
}

.panel__grid dt {
  font-size: 12px;
  color: #64748b;
}

.panel__grid dd {
  margin: 2px 0 0;
  font-size: 14px;
  font-weight: 600;
}

.panel__fallback {
  margin: 10px 0 0;
  font-size: 13px;
  color: #92400e;
}

.view__disclaimer {
  margin: 12px 0 0;
  font-size: 13px;
  font-weight: 600;
  color: #b91c1c;
}
</style>
