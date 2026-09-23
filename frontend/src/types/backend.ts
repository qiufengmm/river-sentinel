/**
 * DS-8A 后端原始 DTO（backend/app/schemas/domain.py、common.py）。
 *
 * 本文件是**后端契约的镜像**：字段名、嵌套结构和可空性必须与后端一致，
 * 前端展示层不得直接使用这些类型，必须经 `src/api/adapters.ts` 转换为视图模型。
 * 禁止为了让前端方便而修改后端字段；如需调整契约应走后端任务。
 */

/** 后端统一响应状态。 */
export type BackendStatus = 'ok' | 'degraded' | 'unavailable' | 'error';

/** 后端数据模式：历史回放或本地快照，不存在 realtime。 */
export type BackendDataMode = 'history_replay' | 'local_snapshot';

/** 后端数据新鲜度。 */
export type BackendFreshness = 'fresh' | 'stale' | 'unknown';

/** 后端风险等级（确定性规则产出）。 */
export type BackendRiskLevel = 'normal' | 'watch' | 'elevated';

/** 后端证据条目。 */
export interface BackendEvidence {
  source_url: string | null;
  data_mode: BackendDataMode;
  observed_at: string | null;
  freshness: BackendFreshness;
  model_version: string | null;
  tool_trace: string[];
  notes: string[];
}

/** 后端统一响应外壳：request_id / status / data / evidence / warnings。 */
export interface BackendApiResponse<T> {
  request_id: string;
  status: BackendStatus;
  data: T | null;
  evidence: BackendEvidence[];
  warnings: string[];
}

/** 历史水位点：水位字段为 `water_level_m`。 */
export interface BackendWaterLevelPoint {
  station_id: string;
  observed_at: string;
  water_level_m: number | null;
  quality_flag: string;
  is_imputed: boolean;
}

/** GET /api/v1/water-level/history 的 data。 */
export interface BackendHistoryResult {
  station_id: string;
  data_mode: BackendDataMode;
  points: BackendWaterLevelPoint[];
  quality_notes: string[];
  status: BackendStatus;
  freshness: BackendFreshness;
  latest_observed_at: string | null;
  warnings: string[];
  evidence: BackendEvidence[];
  disclaimer: string;
}

/** 预测点：目标时刻字段为 `target_at`，水位字段为 `water_level_m`，不可用为 null。 */
export interface BackendPredictionPoint {
  horizon: number;
  target_at: string;
  water_level_m: number | null;
  source: string;
}

/** POST /api/v1/forecast 的 data。 */
export interface BackendForecastResult {
  station_id: string;
  requested_model: string;
  model: string;
  model_version: string;
  as_of: string;
  anchor_at: string | null;
  step_minutes: number | null;
  horizons: number[];
  predictions: BackendPredictionPoint[];
  status: BackendStatus;
  fallback_reason: string | null;
  freshness: BackendFreshness;
  is_stale: boolean;
  data_age_minutes: number | null;
  warnings: string[];
  evidence: BackendEvidence[];
  disclaimer: string;
}

/** 回归指标格：(segment, horizon, model)。 */
export interface BackendMetricCell {
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
}

/** 事件级指标格；零事件时 precision / recall / f1 为 null 并给出 reason。 */
export interface BackendEventMetricCell {
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
}

/** GET /api/v1/model-comparison 的 data：对比数组字段为 `metrics`。 */
export interface BackendModelComparison {
  threshold: number;
  threshold_unit: string;
  threshold_source: string;
  primary_comparison: string[];
  primary_note: string;
  metrics: BackendMetricCell[];
  event_metrics: Record<string, Record<string, Record<string, BackendEventMetricCell>>>;
  limitations: string[];
  source: string;
  status: BackendStatus;
  warnings: string[];
  evidence: BackendEvidence[];
  disclaimer: string;
}

/** POST /api/v1/risk/evaluate 的 data：`basis` 是对象，没有 `computable` 字段。 */
export interface BackendRiskResult {
  station_id: string;
  level: BackendRiskLevel;
  is_stale: boolean;
  threshold: number;
  threshold_unit: string;
  threshold_source: string;
  metric_name: string;
  triggered_horizons: number[];
  missing_horizons: number[];
  predicted_values: Record<string, number | null>;
  anchor_at: string;
  last_observed_at: string;
  evaluated_at: string;
  data_age_hours: number;
  model: string;
  model_version: string;
  basis_source: string;
  basis: Record<string, unknown>;
  guidance: string;
  limitations: string[];
  status: BackendStatus;
  warnings: string[];
  evidence: BackendEvidence[];
  disclaimer: string;
}

/** POST /api/v1/agent/chat 的 data。 */
export interface BackendAgentResponse {
  answer: string;
  intent: string;
  tool_trace: string[];
  status: BackendStatus;
  station_id: string;
  data_mode: BackendDataMode;
  observed_at: string | null;
  model_version: string | null;
  provider: string;
  risk_verdict: BackendRiskResult | null;
  evidence_summaries: string[];
  warnings: string[];
  disclaimer: string;
}

/** GET /api/v1/health 的 data。 */
export interface BackendHealthPayload {
  service: string;
  version: string;
  data_mode: BackendDataMode;
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

/** POST /api/v1/brief 的 data。 */
export interface BackendBriefResult {
  title: string;
  station_id: string;
  data_mode: BackendDataMode;
  generated_at: string;
  data_time: string | null;
  model_version: string | null;
  risk_level: BackendRiskLevel | null;
  highlights: string[];
  sections: { heading: string; lines: string[] }[];
  tool_trace: string[];
  status: BackendStatus;
  warnings: string[];
  evidence: BackendEvidence[];
  disclaimer: string;
}
