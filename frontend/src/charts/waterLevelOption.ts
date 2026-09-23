import type { EChartsOption } from 'echarts';
import type { ForecastHorizon, HistoryPoint, PredictionPoint } from '@/types/api';
import { NON_OFFICIAL_LABEL, RESEARCH_THRESHOLD_LABEL } from '@/types/view';

export type ChartPair = [number | string, number | null];

export interface WaterLevelChartInput {
  observed: HistoryPoint[];
  predictions?: PredictionPoint[];
  threshold?: number | null;
  title?: string | null;
}

function toAxisTime(value: string): number | string {
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? value : parsed;
}

/** 观测序列：后端字段为 water_level_m，缺失值保留为 null，形成断点，绝不转换为 0。 */
export function toObservedPairs(points: HistoryPoint[]): ChartPair[] {
  return (points ?? []).map((point) => [toAxisTime(point.observed_at), point.water_level_m ?? null]);
}

/** 预测序列：后端字段为 target_at / water_level_m，空预测返回空数组（不绘制零线）。 */
export function toForecastPairs(points: PredictionPoint[], source: string): ChartPair[] {
  return (points ?? [])
    .filter((point) => point.source === source && point.water_level_m !== null && point.water_level_m !== undefined)
    .map((point) => [toAxisTime(point.target_at), point.water_level_m as number]);
}

/** 只有存在可绘制预测点的模型才生成序列，不可用模型不出现零线。 */
export function groupPredictionModels(points: PredictionPoint[]): string[] {
  const sources: string[] = [];
  (points ?? []).forEach((point) => {
    if (point.water_level_m === null || point.water_level_m === undefined) return;
    const source = point.source || '未知来源';
    if (!sources.includes(source)) sources.push(source);
  });
  return sources;
}

export function formatHorizonLabels(horizons: ForecastHorizon[]): string {
  if (!horizons || horizons.length === 0) return '未提供预测时域';
  return horizons.map((h) => `${h} 小时`).join('、');
}

/**
 * 生成 ECharts option。
 *
 * 只做数据转换：阈值线使用研究性高水位标签，缺失点保持断点。
 */
export function buildWaterLevelOption(input: WaterLevelChartInput): EChartsOption {
  const observed = input.observed ?? [];
  const predictions = input.predictions ?? [];
  const models = groupPredictionModels(predictions);

  const series: EChartsOption['series'] = [
    {
      name: '历史回放水位',
      type: 'line',
      data: toObservedPairs(observed),
      connectNulls: false,
      showSymbol: false,
      smooth: false,
      lineStyle: { width: 2 },
      z: 3,
      ...(input.threshold !== null && input.threshold !== undefined
        ? {
            markLine: {
              silent: true,
              symbol: 'none',
              label: {
                formatter: `${RESEARCH_THRESHOLD_LABEL}（${NON_OFFICIAL_LABEL}）`,
                position: 'insideEndTop',
              },
              lineStyle: { type: 'dashed', color: '#c8811a' },
              data: [{ yAxis: input.threshold }],
            },
          }
        : {}),
    },
    ...models.map((model) => ({
      name: `预测 · ${model}`,
      type: 'line' as const,
      data: toForecastPairs(predictions, model),
      connectNulls: false,
      showSymbol: true,
      symbolSize: 5,
      lineStyle: { type: 'dashed' as const, width: 2 },
      z: 2,
    })),
  ];

  return {
    title: {
      text: input.title ?? '水位序列（历史回放）',
      left: 0,
      textStyle: { fontSize: 14 },
    },
    tooltip: { trigger: 'axis' },
    legend: { top: 28, data: ['历史回放水位', ...models.map((m) => `预测 · ${m}`)] },
    grid: { left: 56, right: 24, top: 72, bottom: 48 },
    xAxis: { type: 'time', name: '时间', nameLocation: 'middle', nameGap: 26 },
    yAxis: { type: 'value', name: '水位 (m)', scale: true },
    series,
  };
}
