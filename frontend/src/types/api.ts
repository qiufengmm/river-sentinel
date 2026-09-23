/**
 * 前端展示模型（视图模型）。
 *
 * 由 `src/api/adapters.ts` 从后端 DTO 转换而来；组件与视图只使用本文件中的类型。
 * 所有数值来自后端响应，缺失值保留 `null`，不得在展示层补 0 或补造等级。
 */

/** 前端使用的响应状态。 */
export type ApiStatus = 'ok' | 'degraded' | 'unavailable' | 'error';

/** 数据模式：仅历史回放与本地快照，不存在实时监测。 */
export type DataMode = 'history_replay' | 'local_snapshot';

/** 数据新鲜度。 */
export type Freshness = 'fresh' | 'stale' | 'unknown';

/** 风险等级（由确定性规则产出）。 */
export type RiskLevel = 'normal' | 'watch' | 'elevated';

/** 预测时域（小时或有效时间步，由后端 supported_horizons 决定）。 */
export type ForecastHorizon = 1 | 3 | 6;

/** 模型标识（backend/app/schemas/domain.py: ModelName）：只能发送这些值，绝不发送 null。 */
export type ModelName = 'persistence' | 'xgboost' | 'ridge' | 'lstm';

/** 证据条目：数据源、数据模式、观测时间、新鲜度、模型版本、工具摘要。 */
export interface Evidence {
  source_url: string | null;
  data_mode: DataMode;
  observed_at: string | null;
  freshness: Freshness;
  model_version: string | null;
  tool_trace: string[];
  notes?: string[] | null;
}

/** 统一响应：request_id / status / data / evidence / warnings。 */
export interface ApiResponse<T> {
  request_id: string;
  status: ApiStatus;
  data: T | null;
  evidence: Evidence[];
  warnings: string[];
}

/** 历史水位点（展示模型）。缺失水位保持 null。 */
export interface HistoryPoint {
  observed_at: string;
  water_level_m: number | null;
  quality_flag: string;
  is_imputed: boolean;
}

/** 历史回放结果（展示模型）。 */
export interface HistoryData {
  station_id: string;
  data_mode: DataMode;
  points: HistoryPoint[];
  quality_notes: string[];
  freshness: Freshness;
  latest_observed_at: string | null;
  missing_count: number;
  sample_count: number;
}

/** 预测点：目标时刻与预测值，不可用为 null。 */
export interface PredictionPoint {
  horizon: number;
  target_at: string;
  water_level_m: number | null;
  source: string;
}

/** 预测结果（展示模型）。 */
export interface ForecastData {
  station_id: string;
  requested_model: string;
  model: string;
  model_version: string;
  as_of: string;
  anchor_at: string | null;
  step_minutes: number | null;
  horizons: number[];
  predictions: PredictionPoint[];
  is_fallback: boolean;
  fallback_reason: string | null;
  freshness: Freshness;
  is_stale: boolean;
}

/** 展示用 key/value 行。 */
export interface DetailEntry {
  label: string;
  value: string;
}

/** 回归指标行（展示模型）。不可用模型的指标为 null 并带 reason。 */
export interface MetricRow {
  segment: string;
  horizon: number;
  model: string;
  display_name: string;
  model_version: string;
  sample_count: number | null;
  mae: number | null;
  rmse: number | null;
  r2: number | null;
  nse: number | null;
  reason: string | null;
  supported: boolean;
}

/** 事件级指标行（展示模型）。零事件时 precision/recall/f1 为 null 并带 reason。 */
export interface EventMetricRow {
  segment: string;
  horizon: number;
  model: string;
  display_name: string;
  precision: number | null;
  recall: number | null;
  f1: number | null;
  tp: number;
  fp: number;
  fn: number;
  observed_event_points: number;
  observed_event_segments: number;
  predicted_event_points: number;
  predicted_event_segments: number | null;
  reason: string | null;
  computable: boolean;
}

/** 事件级指标的选择条件（segment / horizon / model 必须显式）。 */
export interface EventMetricSelection {
  segment: string;
  horizon: number | null;
  models: string[] | null;
}

/** 模型对比结果（展示模型）。 */
export interface ModelComparison {
  threshold: number;
  threshold_unit: string;
  threshold_source: string;
  primary_comparison: string[];
  primary_note: string;
  rows: MetricRow[];
  event_rows: EventMetricRow[];
  limitations: string[];
  source: string;
}

/** 风险判定（展示模型）。 `basis` 对象已格式化为摘要与关键字段。 */
export interface RiskVerdict {
  station_id: string;
  level: RiskLevel;
  threshold: number;
  threshold_unit: string;
  threshold_source: string;
  metric_name: string;
  is_stale: boolean;
  triggered_horizons: number[];
  missing_horizons: number[];
  /** 形如 { "1": 3.71, "3": 3.52, "6": 3.2 }，缺失为 null。 */
  predicted_values: Record<string, number | null>;
  anchor_at: string;
  last_observed_at: string;
  evaluated_at: string;
  data_age_hours: number;
  model: string;
  model_version: string;
  basis_source: string;
  /** 后端 basis 原始对象，仅用于二次核对，不直接渲染。 */
  basis: Record<string, unknown>;
  /** basis 的稳定中文摘要，避免渲染 [object Object]。 */
  basis_summary: string;
  basis_details: DetailEntry[];
  guidance: string;
  limitations: string[];
}

/** 智能体回答（展示模型）。 */
export interface AgentData {
  answer: string;
  intent: string;
  tool_trace: string[];
  status: ApiStatus;
  station_id: string;
  data_mode: DataMode;
  observed_at: string | null;
  model_version: string | null;
  provider: string;
  risk_verdict: RiskVerdict | null;
  evidence_summaries: string[];
  warnings: string[];
  disclaimer: string;
}

/** 健康检查（展示模型）：状态来自外层 ApiResponse.status。 */
export interface HealthData {
  service: string;
  version: string;
  data_mode: DataMode;
  station_id: string;
  sample_available: boolean;
  sample_path: string;
  artifact_dir: string | null;
  llm_provider: string;
  external_provider_available: boolean;
  persistence_fallback_available: boolean;
  supported_horizons: number[];
  research_threshold: number;
  disclaimer: string;
}

/** 简报（展示模型）。 */
export interface BriefData {
  title: string;
  station_id: string;
  data_mode: DataMode;
  generated_at: string;
  data_time: string | null;
  model_version: string | null;
  risk_level: RiskLevel | null;
  highlights: string[];
  sections: { heading: string; lines: string[] }[];
  tool_trace: string[];
}
