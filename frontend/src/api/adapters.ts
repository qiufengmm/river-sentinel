/**
 * 后端 DTO → 前端展示模型的显式适配层。
 *
 * 所有后端字段（water_level_m / target_at / metrics / basis 对象等）只在这里转换一次，
 * 组件与视图不直接读取后端字段名，避免契约漂移时出现“字段存在但页面空白”。
 * 缺失值一律保持 null，绝不补 0、补等级或补造时间。
 */

import type {
  AgentData,
  BriefData,
  DetailEntry,
  EventMetricRow,
  Evidence,
  ForecastData,
  HealthData,
  HistoryData,
  HistoryPoint,
  MetricRow,
  ModelComparison,
  PredictionPoint,
  RiskVerdict,
} from '@/types/api';
import type {
  BackendAgentResponse,
  BackendBriefResult,
  BackendEventMetricCell,
  BackendEvidence,
  BackendForecastResult,
  BackendHealthPayload,
  BackendHistoryResult,
  BackendMetricCell,
  BackendModelComparison,
  BackendPredictionPoint,
  BackendRiskResult,
  BackendWaterLevelPoint,
} from '@/types/backend';
import { formatNumber, formatTime } from '@/types/view';

/** 数值安全转换：非法值与缺失一律返回 null，不转成 0。 */
export function toNumberOrNull(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function toNumber(value: unknown, fallback = 0): number {
  return toNumberOrNull(value) ?? fallback;
}

function toStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)) : [];
}

function toNumberList(value: unknown): number[] {
  return Array.isArray(value) ? value.map((item) => toNumber(item)).filter((item) => item > 0) : [];
}

/** 后端证据 → 展示层证据（字段名一致，保留 notes）。 */
export function adaptEvidence(evidence: BackendEvidence[] | null | undefined): Evidence[] {
  return (evidence ?? []).map((item) => ({
    source_url: item?.source_url ?? null,
    data_mode: item?.data_mode ?? 'history_replay',
    observed_at: item?.observed_at ?? null,
    freshness: item?.freshness ?? 'unknown',
    model_version: item?.model_version ?? null,
    tool_trace: toStringList(item?.tool_trace),
    notes: toStringList(item?.notes),
  }));
}

function adaptPoint(point: BackendWaterLevelPoint): HistoryPoint {
  return {
    observed_at: point?.observed_at ?? '',
    water_level_m: toNumberOrNull(point?.water_level_m),
    quality_flag: point?.quality_flag ?? 'unknown',
    is_imputed: Boolean(point?.is_imputed),
  };
}

/** 历史结果适配：统计缺失点数，缺失仍为 null。 */
export function adaptHistory(dto: BackendHistoryResult | null): HistoryData | null {
  if (!dto) return null;
  const points = (dto.points ?? []).map(adaptPoint).filter((point) => point.observed_at);
  return {
    station_id: dto.station_id ?? '',
    data_mode: dto.data_mode ?? 'history_replay',
    points,
    quality_notes: toStringList(dto.quality_notes),
    freshness: dto.freshness ?? 'unknown',
    latest_observed_at: dto.latest_observed_at ?? points[points.length - 1]?.observed_at ?? null,
    missing_count: points.filter((point) => point.water_level_m === null).length,
    sample_count: points.length,
  };
}

function adaptPrediction(point: BackendPredictionPoint): PredictionPoint {
  return {
    horizon: toNumber(point?.horizon),
    target_at: point?.target_at ?? '',
    water_level_m: toNumberOrNull(point?.water_level_m),
    source: point?.source ?? 'unknown',
  };
}

/** 预测结果适配：目标时刻取 target_at，不可用预测值保持 null。 */
export function adaptForecast(dto: BackendForecastResult | null): ForecastData | null {
  if (!dto) return null;
  const fallbackReason = dto.fallback_reason ?? null;
  const predictions = (dto.predictions ?? [])
    .map(adaptPrediction)
    .filter((point) => point.target_at)
    .sort((left, right) => left.horizon - right.horizon);
  return {
    station_id: dto.station_id ?? '',
    requested_model: dto.requested_model ?? '',
    model: dto.model ?? '',
    model_version: dto.model_version ?? '',
    as_of: dto.as_of ?? '',
    anchor_at: dto.anchor_at ?? null,
    step_minutes: toNumberOrNull(dto.step_minutes),
    horizons: toNumberList(dto.horizons),
    predictions,
    is_fallback: Boolean(fallbackReason) || Boolean(dto.model && dto.requested_model && dto.model !== dto.requested_model),
    fallback_reason: fallbackReason,
    freshness: dto.freshness ?? 'unknown',
    is_stale: Boolean(dto.is_stale),
  };
}

function adaptMetricRow(cell: BackendMetricCell | null): MetricRow | null {
  if (!cell) return null;
  const mae = toNumberOrNull(cell.mae);
  const rmse = toNumberOrNull(cell.rmse);
  const r2 = toNumberOrNull(cell.r2);
  const nse = toNumberOrNull(cell.nse);
  const reason = cell.reason ?? null;
  return {
    segment: cell.segment ?? '',
    horizon: toNumber(cell.horizon),
    model: cell.model ?? '',
    display_name: cell.display_name ?? cell.model ?? '',
    model_version: cell.model_version ?? '',
    sample_count: toNumberOrNull(cell.sample_count),
    mae,
    rmse,
    r2,
    nse,
    reason,
    supported: mae !== null || rmse !== null || r2 !== null || nse !== null,
  };
}

function adaptEventRow(cell: BackendEventMetricCell | null): EventMetricRow | null {
  if (!cell) return null;
  const precision = toNumberOrNull(cell.precision);
  const recall = toNumberOrNull(cell.recall);
  const f1 = toNumberOrNull(cell.f1);
  return {
    segment: cell.segment ?? '',
    horizon: toNumber(cell.horizon),
    model: cell.model ?? '',
    display_name: cell.display_name ?? cell.model ?? '',
    precision,
    recall,
    f1,
    tp: toNumber(cell.tp),
    fp: toNumber(cell.fp),
    fn: toNumber(cell.fn),
    observed_event_points: toNumber(cell.observed_event_points),
    observed_event_segments: toNumber(cell.observed_event_segments),
    predicted_event_points: toNumber(cell.predicted_event_points),
    predicted_event_segments: toNumberOrNull(cell.predicted_event_segments),
    reason: cell.reason ?? null,
    computable: precision !== null || recall !== null || f1 !== null,
  };
}

/** 把后端嵌套 event_metrics[segment][h{n}][model] 拉平为行，零事件格的 f1 为 null。 */
export function flattenEventMetrics(dto: BackendModelComparison | null): EventMetricRow[] {
  if (!dto?.event_metrics) return [];
  const rows: EventMetricRow[] = [];
  Object.entries(dto.event_metrics).forEach(([segment, horizons]) => {
    Object.entries(horizons ?? {}).forEach(([horizonKey, models]) => {
      const horizon = Number(String(horizonKey).replace(/^h/i, ''));
      Object.values(models ?? {}).forEach((cell) => {
        const row = adaptEventRow(cell);
        if (!row) return;
        rows.push({ ...row, segment: row.segment || segment, horizon: row.horizon || horizon });
      });
    });
  });
  return rows.sort((left, right) => left.horizon - right.horizon || left.model.localeCompare(right.model));
}

/**
 * 按明确条件选择事件级指标行。
 *
 * 页面只展示单一汇总时必须显式给出 segment / horizon / model，不得隐式取第一个。
 */
export function selectEventMetrics(
  rows: EventMetricRow[],
  selection: { segment?: string | null; horizon?: number | null; models?: string[] | null },
): EventMetricRow[] {
  const segment = selection.segment ?? 'test';
  const bySegment = rows.filter((row) => row.segment === segment);
  const pool = bySegment.length > 0 ? bySegment : rows;
  const byHorizon =
    selection.horizon === null || selection.horizon === undefined
      ? pool
      : pool.filter((row) => row.horizon === selection.horizon);
  const scoped = byHorizon.length > 0 ? byHorizon : pool;
  const models = selection.models;
  if (!models || models.length === 0) return scoped;
  const byModel = scoped.filter((row) => models.includes(row.model));
  return byModel.length > 0 ? byModel : scoped;
}

/** 模型对比适配：后端 `metrics` → 展示行 rows。 */
export function adaptComparison(dto: BackendModelComparison | null): ModelComparison | null {
  if (!dto) return null;
  const rows = (dto.metrics ?? [])
    .map(adaptMetricRow)
    .filter((row): row is MetricRow => row !== null)
    .sort((left, right) => left.horizon - right.horizon || left.model.localeCompare(right.model));
  return {
    threshold: toNumber(dto.threshold),
    threshold_unit: dto.threshold_unit ?? 'm',
    threshold_source: dto.threshold_source ?? '',
    primary_comparison: toStringList(dto.primary_comparison),
    primary_note: dto.primary_note ?? '',
    rows,
    event_rows: flattenEventMetrics(dto),
    limitations: toStringList(dto.limitations),
    source: dto.source ?? '',
  };
}

const BASIS_LABELS: Record<string, string> = {
  threshold: '研究性阈值',
  threshold_unit: '阈值单位',
  threshold_source: '阈值来源',
  metric: '判定指标',
  model_name: '模型',
  model_version: '模型版本',
  anchor_at: '预测锚点',
  data_last_observed_at: '数据最后观测',
  data_age_hours: '数据龄（小时）',
  is_stale: '陈旧标记',
  stale_after_hours: '陈旧门槛（小时）',
  evaluated_at: '判定时间',
  considered_horizons: '参与时域',
  triggered_horizons: '触发时域',
  missing_horizons: '缺失不参评时域',
  predicted_values: '预测值',
  disclaimer: '限制',
};

/** 把任意 basis 值格式化为中文文本，绝不输出 [object Object]。 */
export function formatBasisValue(value: unknown): string {
  if (value === null || value === undefined) return '未提供';
  if (typeof value === 'number') return Number.isFinite(value) ? formatNumber(value, 3) : '未提供';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.length > 0 ? value.map(formatBasisValue).join('、') : '无';
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>);
    return entries.length > 0
      ? entries.map(([key, item]) => `${key}=${formatBasisValue(item)}`).join('、')
      : '无';
  }
  return String(value);
}

/**
 * basis 对象 → 稳定中文摘要 + 关键字段明细。
 *
 * 后端 basis 是嵌套 dict，直接渲染会得到 `[object Object]`，因此统一在此格式化。
 */
export function summarizeBasis(basis: Record<string, unknown> | null | undefined): {
  basis_summary: string;
  basis_details: DetailEntry[];
} {
  if (!basis || typeof basis !== 'object') {
    return { basis_summary: '未提供判定依据', basis_details: [] };
  }
  const details: DetailEntry[] = [];
  Object.entries(BASIS_LABELS).forEach(([key, label]) => {
    if (!(key in basis)) return;
    details.push({ label, value: formatBasisValue(basis[key]) });
  });
  const parts = details.map((item) => `${item.label}：${item.value}`);
  return {
    basis_summary: parts.length > 0 ? parts.join('；') : '未提供判定依据',
    basis_details: details,
  };
}

function normalizePredictedValues(values: Record<string, unknown> | null | undefined): Record<string, number | null> {
  const result: Record<string, number | null> = {};
  Object.entries(values ?? {}).forEach(([key, value]) => {
    result[String(key).replace(/^h/i, '')] = toNumberOrNull(value);
  });
  return result;
}

/** 风险判定适配：没有 computable 字段，等级存在即可展示；basis 对象格式化为摘要。 */
export function adaptRisk(dto: BackendRiskResult | null): RiskVerdict | null {
  if (!dto) return null;
  const { basis_summary, basis_details } = summarizeBasis(dto.basis);
  const predictedValues = normalizePredictedValues(dto.predicted_values);
  return {
    station_id: dto.station_id ?? '',
    level: dto.level ?? 'normal',
    threshold: toNumberOrNull(dto.threshold) ?? 0,
    threshold_unit: dto.threshold_unit ?? 'm',
    threshold_source: dto.threshold_source ?? '',
    metric_name: dto.metric_name ?? 'research_high_water',
    is_stale: Boolean(dto.is_stale),
    triggered_horizons: toNumberList(dto.triggered_horizons),
    missing_horizons: toNumberList(dto.missing_horizons),
    predicted_values: predictedValues,
    anchor_at: dto.anchor_at ?? '',
    last_observed_at: dto.last_observed_at ?? '',
    evaluated_at: dto.evaluated_at ?? '',
    data_age_hours: toNumber(dto.data_age_hours),
    model: dto.model ?? '',
    model_version: dto.model_version ?? '',
    basis_source: dto.basis_source ?? '',
    basis: (dto.basis ?? {}) as Record<string, unknown>,
    basis_summary,
    basis_details,
    guidance: dto.guidance ?? '',
    limitations: toStringList(dto.limitations),
  };
}

/** 智能体回答适配：风险判定一并适配。 */
export function adaptAgent(dto: BackendAgentResponse | null): AgentData | null {
  if (!dto) return null;
  return {
    answer: dto.answer ?? '',
    intent: dto.intent ?? '',
    tool_trace: toStringList(dto.tool_trace),
    status: dto.status ?? 'ok',
    station_id: dto.station_id ?? '',
    data_mode: dto.data_mode ?? 'history_replay',
    observed_at: dto.observed_at ?? null,
    model_version: dto.model_version ?? null,
    provider: dto.provider ?? '',
    risk_verdict: adaptRisk(dto.risk_verdict),
    evidence_summaries: toStringList(dto.evidence_summaries),
    warnings: toStringList(dto.warnings),
    disclaimer: dto.disclaimer ?? '',
  };
}

/** 健康检查适配：外层响应 status 才是接口状态，数据体只描述可用性。 */
export function adaptHealth(dto: BackendHealthPayload | null): HealthData | null {
  if (!dto) return null;
  return {
    service: dto.service ?? '',
    version: dto.version ?? '',
    data_mode: dto.data_mode ?? 'history_replay',
    station_id: dto.station_id ?? '',
    sample_available: Boolean(dto.sample_available),
    sample_path: dto.sample_path ?? '',
    artifact_dir: dto.artifact_dir ?? null,
    llm_provider: dto.llm_provider ?? '',
    external_provider_available: Boolean(dto.external_provider_available),
    persistence_fallback_available: Boolean(dto.persistence_fallback_available),
    supported_horizons: toNumberList(dto.supported_horizons),
    research_threshold: toNumber(dto.research_threshold),
    disclaimer: dto.disclaimer ?? '',
  };
}

/** 简报适配。 */
export function adaptBrief(dto: BackendBriefResult | null): BriefData | null {
  if (!dto) return null;
  return {
    title: dto.title ?? '',
    station_id: dto.station_id ?? '',
    data_mode: dto.data_mode ?? 'history_replay',
    generated_at: dto.generated_at ?? '',
    data_time: dto.data_time ?? null,
    model_version: dto.model_version ?? null,
    risk_level: dto.risk_level ?? null,
    highlights: toStringList(dto.highlights),
    sections: (dto.sections ?? []).map((section) => ({
      heading: section?.heading ?? '',
      lines: toStringList(section?.lines),
    })),
    tool_trace: toStringList(dto.tool_trace),
  };
}

/** 取最后一个有效观测点（水位非 null），用于风险判定的观测锚点。 */
export function latestObservedPoint(data: HistoryData | null): HistoryPoint | null {
  if (!data?.points?.length) return null;
  for (let index = data.points.length - 1; index >= 0; index -= 1) {
    const point = data.points[index];
    if (point && point.water_level_m !== null && point.observed_at) return point;
  }
  return null;
}

/** 预测结果 → 后端 RiskRequest.predictions 需要的 {1: v, 3: v, 6: v}。缺失时域不写入。 */
export function toPredictionMap(data: ForecastData | null): Record<number, number> {
  const result: Record<number, number> = {};
  (data?.predictions ?? []).forEach((point) => {
    if (point.water_level_m === null || point.horizon <= 0) return;
    result[point.horizon] = point.water_level_m;
  });
  return result;
}

/** 观测时间展示（沿用展示层统一格式）。 */
export function displayObservedAt(value: string | null): string {
  return formatTime(value);
}
