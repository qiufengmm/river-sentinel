<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import type { HistoryPoint, PredictionPoint } from '@/types/api';
import { buildWaterLevelOption } from '@/charts/waterLevelOption';
import { initChart, type ChartHandle } from '@/charts/renderer';
import { NON_OFFICIAL_LABEL, RESEARCH_THRESHOLD_LABEL } from '@/types/view';
import UnavailableState from './UnavailableState.vue';

const props = withDefaults(
  defineProps<{
    observed?: HistoryPoint[];
    predictions?: PredictionPoint[];
    threshold?: number | null;
    title?: string | null;
  }>(),
  { observed: () => [], predictions: () => [], threshold: null, title: null },
);

const container = ref<HTMLDivElement | null>(null);
const chartFailed = ref(false);
let chart: ChartHandle | null = null;

const hasDrawablePoint = computed(
  () =>
    (props.observed ?? []).some((point) => point.water_level_m !== null && point.water_level_m !== undefined) ||
    (props.predictions ?? []).some((point) => point.water_level_m !== null && point.water_level_m !== undefined),
);

const option = computed(() =>
  buildWaterLevelOption({
    observed: props.observed ?? [],
    predictions: props.predictions ?? [],
    threshold: props.threshold,
    title: props.title ?? '水位序列（历史回放）',
  }),
);

function renderChart(): void {
  if (!chart || chartFailed.value) return;
  chart.setOption(option.value);
}

onMounted(() => {
  if (!container.value) return;
  try {
    chart = initChart(container.value);
    renderChart();
  } catch {
    chart = null;
    chartFailed.value = true;
  }
});

watch(option, () => renderChart());

onBeforeUnmount(() => {
  chart?.dispose();
  chart = null;
});
</script>

<template>
  <section class="chart">
    <p class="chart__legend">
      虚线为研究性高水位阈值：{{ RESEARCH_THRESHOLD_LABEL }}（{{ NON_OFFICIAL_LABEL }}）；缺失水位保留断点，不绘制为 0。
    </p>
    <UnavailableState
      v-if="chartFailed"
      status="error"
      title="图表初始化失败"
      hint="ECharts 未能初始化，页面不展示任何替代曲线。"
    />
    <UnavailableState
      v-else-if="!hasDrawablePoint"
      status="empty"
      title="暂无可绘制的点位"
      hint="后端未返回有效水位或预测点，不绘制任何替代曲线。"
    />
    <div v-show="!chartFailed && hasDrawablePoint" ref="container" class="chart__canvas"></div>
  </section>
</template>

<style scoped>
.chart {
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 12px 16px;
  background: #ffffff;
}

.chart__legend {
  margin: 0 0 8px;
  font-size: 12px;
  color: #475569;
}

.chart__canvas {
  width: 100%;
  height: 340px;
}
</style>
