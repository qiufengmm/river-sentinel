import type { DataMode, ForecastHorizon, ModelName } from '@/types/api';
import { toIsoWithTimeZone } from '@/utils/time';

/** API 路径集中定义，组件与 store 不得自行拼接。 */
export const API_ENDPOINTS = {
  health: '/health',
  history: '/water-level/history',
  forecast: '/forecast',
  comparison: '/model-comparison',
  risk: '/risk/evaluate',
  chat: '/agent/chat',
  brief: '/brief',
} as const;

/** 后端允许的模型枚举（backend/app/schemas/domain.py: ModelName）。只能用这些值。 */
export const MODEL_NAMES: ModelName[] = ['persistence', 'xgboost', 'ridge', 'lstm'];

/** 后端允许的数据模式（不存在 realtime）。 */
export const DATA_MODES: DataMode[] = ['history_replay', 'local_snapshot'];

/** 默认预测时域。 */
export const DEFAULT_HORIZONS: ForecastHorizon[] = [1, 3, 6];

/** 历史水位查询参数（对应后端 GET /water-level/history 的 Query）。 */
export interface HistoryQuery {
  stationId: string;
  /** datetime-local 本地时间会被转换为带项目时区偏移的 ISO 字符串。 */
  start?: string | null;
  end?: string | null;
  limit?: number | null;
}

/** POST /forecast 请求体。model 与 data_mode 必须是合法枚举，绝不发送 null。 */
export interface ForecastRequestPayload {
  station_id: string;
  as_of?: string;
  horizons?: ForecastHorizon[];
  model?: ModelName;
  data_mode?: DataMode;
}

/** POST /risk/evaluate 请求体。water_level 与 observed_at 为必填。 */
export interface RiskRequestPayload {
  station_id: string;
  water_level: number;
  observed_at: string;
  as_of?: string;
  horizons?: ForecastHorizon[];
  predictions?: Record<number, number>;
  model?: string;
  model_version?: string;
  data_mode?: DataMode;
}

/** POST /agent/chat 请求体。 */
export interface ChatRequestPayload {
  message: string;
  station_id?: string;
  as_of?: string;
  horizons?: ForecastHorizon[];
  model?: ModelName;
  data_mode?: DataMode;
}

/** POST /brief 请求体。 */
export interface BriefRequestPayload {
  station_id: string;
  as_of?: string;
  horizons?: ForecastHorizon[];
  model?: ModelName;
  data_mode?: DataMode;
}

/**
 * 删除 null / undefined 键，确保不会把 `model: null` 之类非法枚举发给后端。
 *
 * 后端 Pydantic 对枚举字段会返回 400 INVALID_REQUEST，因此空值只能省略。
 */
export function stripNulls(payload: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  Object.entries(payload).forEach(([key, value]) => {
    if (value === null || value === undefined || value === '') return;
    result[key] = value;
  });
  return result;
}

/** 时域列表序列化，非法值直接丢弃，避免把非法参数发到后端。 */
export function serializeHorizons(horizons?: ForecastHorizon[] | null): string | null {
  if (!horizons || horizons.length === 0) return null;
  const valid = horizons.filter((h): h is ForecastHorizon => h === 1 || h === 3 || h === 6);
  return valid.length > 0 ? valid.join(',') : null;
}

/** 归一化时域：只保留后端支持的 1、3、6，空则回退 [1,3,6]。 */
export function normalizeHorizons(horizons?: ForecastHorizon[] | null): ForecastHorizon[] {
  if (!horizons || horizons.length === 0) return [...DEFAULT_HORIZONS];
  const valid = horizons.filter((h): h is ForecastHorizon => h === 1 || h === 3 || h === 6);
  return valid.length > 0 ? valid : [...DEFAULT_HORIZONS];
}

/** 只接受后端允许的模型名，其余回退为持久性基线。 */
export function normalizeModel(model?: string | null): ModelName {
  const candidate = (model ?? '').toLowerCase() as ModelName;
  return MODEL_NAMES.includes(candidate) ? candidate : 'persistence';
}

/** 只接受后端允许的数据模式，其余回退为历史回放。 */
export function normalizeDataMode(mode?: string | null): DataMode {
  const candidate = (mode ?? '').toLowerCase() as DataMode;
  return DATA_MODES.includes(candidate) ? candidate : 'history_replay';
}

/** 构造预测请求：as_of 为 datetime-local 输入时附加项目时区；模型与数据模式取合法值。 */
export function buildForecastRequest(input: {
  station_id: string;
  as_of?: string | null;
  horizons?: ForecastHorizon[] | null;
  model?: string | null;
  data_mode?: string | null;
}): ForecastRequestPayload {
  const payload: ForecastRequestPayload = {
    station_id: input.station_id,
    horizons: normalizeHorizons(input.horizons),
    model: normalizeModel(input.model),
    data_mode: normalizeDataMode(input.data_mode),
  };
  const asOf = toIsoWithTimeZone(input.as_of);
  if (asOf) payload.as_of = asOf;
  return payload;
}

/**
 * 构造风险判定请求。
 *
 * 缺少观测水位或观测时间时返回 null：调用方必须展示明确不可用状态，
 * 不得补造水位或时间（AGENTS.md §7）。
 */
export function buildRiskRequest(input: {
  station_id: string;
  water_level: number | null;
  observed_at: string | null;
  horizons?: ForecastHorizon[] | null;
  predictions?: Record<number, number> | null;
  model?: string | null;
  model_version?: string | null;
  data_mode?: string | null;
}): RiskRequestPayload | null {
  if (
    input.water_level === null ||
    input.water_level === undefined ||
    Number.isNaN(input.water_level) ||
    !input.observed_at
  ) {
    return null;
  }
  const payload: RiskRequestPayload = {
    station_id: input.station_id,
    water_level: input.water_level,
    observed_at: toIsoWithTimeZone(input.observed_at) ?? input.observed_at,
    horizons: normalizeHorizons(input.horizons),
    model: input.model ?? 'persistence',
    model_version: input.model_version ?? 'single_level_input',
    data_mode: normalizeDataMode(input.data_mode),
  };
  if (input.predictions && Object.keys(input.predictions).length > 0) {
    payload.predictions = input.predictions;
  }
  return payload;
}

/** 构造智能体对话请求。 */
export function buildChatRequest(input: {
  message: string;
  station_id?: string;
  horizons?: ForecastHorizon[] | null;
  model?: string | null;
  data_mode?: string | null;
}): ChatRequestPayload {
  return {
    message: input.message,
    station_id: input.station_id ?? 'cata_12720',
    horizons: normalizeHorizons(input.horizons),
    model: normalizeModel(input.model),
    data_mode: normalizeDataMode(input.data_mode),
  };
}

/** 构造简报请求。 */
export function buildBriefRequest(input: {
  station_id: string;
  horizons?: ForecastHorizon[] | null;
  model?: string | null;
  data_mode?: string | null;
}): BriefRequestPayload {
  return {
    station_id: input.station_id,
    horizons: normalizeHorizons(input.horizons),
    model: normalizeModel(input.model),
    data_mode: normalizeDataMode(input.data_mode),
  };
}

/** 生成查询串，自动忽略空值。 */
export function buildQueryString(params: Record<string, string | number | null | undefined>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === null || value === undefined || value === '') return;
    search.append(key, String(value));
  });
  const query = search.toString();
  return query ? `?${query}` : '';
}
