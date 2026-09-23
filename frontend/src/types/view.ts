import type {
  ApiStatus,
  DataMode,
  EventMetricRow,
  ForecastHorizon,
  MetricRow,
  RiskLevel,
  RiskVerdict,
} from './api';

/** 视图标识。 */
export type ViewKey = 'dashboard' | 'history' | 'forecast' | 'agent';

/** 全局固定文案，页面、卡片和图表统一引用，避免措辞漂移。 */
export const DISCLAIMER = '教学科研辅助，不替代官方防汛决策';
export const RESEARCH_THRESHOLD_LABEL = '研究性高水位阈值（训练集 p90，7.18 m）';
export const NON_OFFICIAL_LABEL = '非官方警戒标准';
export const EVENT_METRICS_UNAVAILABLE = '事件级指标无法计算';
export const METRIC_UNAVAILABLE = '不可用/未提供';

/** 数据模式中文标签。 */
export function dataModeLabel(mode: DataMode | null | undefined): string {
  if (mode === 'history_replay') return '历史回放';
  if (mode === 'local_snapshot') return '本地快照';
  return '数据模式未知';
}

/** 数据模式补充说明，强调不是实时监测。 */
export function dataModeHint(mode: DataMode | null | undefined): string {
  if (mode === 'history_replay') return '历史回放：使用已归档的小型样例数据，非实时监测。';
  if (mode === 'local_snapshot') return '本地快照：使用最近一次成功的本地数据快照，非实时监测。';
  return '未获取到数据模式，不展示任何水位与风险数值。';
}

/** 响应状态中文标签。 */
export function statusLabel(status: ApiStatus): string {
  switch (status) {
    case 'ok':
      return '正常';
    case 'degraded':
      return '降级';
    case 'unavailable':
      return '不可用';
    case 'error':
      return '请求失败';
    default:
      return '状态未知';
  }
}

/** 风险等级中文标签，仅表达研究性判断。 */
export function riskLevelLabel(level: RiskLevel | null | undefined): string {
  switch (level) {
    case 'normal':
      return '研究区间内';
    case 'watch':
      return '接近研究性高水位';
    case 'elevated':
      return '达到或超过研究性高水位';
    default:
      return '风险等级未提供';
  }
}

/**
 * 风险卡片是否可展示。
 *
 * 后端 RiskResult 没有 computable 字段：只要存在判定对象与等级即可展示，
 * 缺失时展示不可用状态，不补造等级。
 */
export function isRiskRenderable(verdict: RiskVerdict | null | undefined): boolean {
  return Boolean(verdict && verdict.level);
}

/** 指标显示：null 统一显示“不可用/未提供”，不得显示为 0。 */
export function formatMetric(value: number | null | undefined, digits = 3): string {
  if (value === null || value === undefined || Number.isNaN(value)) return METRIC_UNAVAILABLE;
  return value.toFixed(digits);
}

/** 数值显示（basis 等对象内数值）：去掉多余 0，缺失显示“未提供”。 */
export function formatNumber(value: number | null | undefined, digits = 3): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '未提供';
  return value
    .toFixed(digits)
    .replace(/\.0+$/, '')
    .replace(/(\.\d*?)0+$/, '$1');
}

/** 时间戳显示：解析失败时原样返回，不做推断。 */
export function formatTime(value: string | null | undefined): string {
  if (!value) return '未提供';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

/**
 * 事件级指标文案。
 *
 * test 段零事件时后端返回 precision/recall/f1 = null 与 reason=no_observed_events，
 * 此处必须显示“事件级指标无法计算”，不能渲染 F1=0。
 */
export function eventMetricsText(event: EventMetricRow | null | undefined): {
  computable: boolean;
  text: string;
  reason: string | null;
} {
  if (!event || !event.computable) {
    return {
      computable: false,
      text: EVENT_METRICS_UNAVAILABLE,
      reason: event?.reason ?? 'no_observed_events',
    };
  }
  return {
    computable: true,
    text: `事件级指标：精确率 ${formatMetric(event.precision)}，召回率 ${formatMetric(event.recall)}，F1 ${formatMetric(event.f1)}`,
    reason: null,
  };
}

/** 事件级指标选择条件的可读说明，避免隐式取第一个嵌套格。 */
export function eventSelectionLabel(selection: {
  segment?: string | null;
  horizon?: number | null;
  models?: string[] | null;
}): string {
  const segment = selection.segment ?? 'test';
  const horizon = selection.horizon ? `h${selection.horizon}` : '全部时域';
  const models = selection.models && selection.models.length > 0 ? selection.models.join('、') : '全部模型';
  return `segment=${segment}，时域=${horizon}，模型=${models}`;
}

/** 指标表排序：可用模型按 MAE 升序，不可用模型固定在末尾且不参与排序。 */
export function sortMetricRows(rows: MetricRow[]): MetricRow[] {
  const supported = rows
    .filter((row) => row.supported && row.mae !== null)
    .sort((a, b) => (a.mae as number) - (b.mae as number));
  const supportedNoMetric = rows.filter((row) => row.supported && row.mae === null);
  const unsupported = rows.filter((row) => !row.supported);
  return [...supported, ...supportedNoMetric, ...unsupported];
}

/** 预测时域选项。 */
export const HORIZON_OPTIONS: { value: ForecastHorizon; label: string }[] = [
  { value: 1, label: '未来 1 小时' },
  { value: 3, label: '未来 3 小时' },
  { value: 6, label: '未来 6 小时' },
];
