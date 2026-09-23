import type {
  AgentData,
  ApiResponse,
  ApiStatus,
  BriefData,
  ForecastData,
  ForecastHorizon,
  HealthData,
  HistoryData,
  ModelComparison,
  RiskVerdict,
} from '@/types/api';
import type {
  BackendAgentResponse,
  BackendBriefResult,
  BackendForecastResult,
  BackendHealthPayload,
  BackendHistoryResult,
  BackendModelComparison,
  BackendRiskResult,
} from '@/types/backend';
import type {
  BriefRequestPayload,
  ChatRequestPayload,
  ForecastRequestPayload,
  HistoryQuery,
  RiskRequestPayload,
} from './endpoints';
import {
  API_ENDPOINTS,
  buildQueryString,
  normalizeDataMode,
  normalizeHorizons,
  normalizeModel,
  serializeHorizons,
  stripNulls,
} from './endpoints';
import {
  adaptAgent,
  adaptBrief,
  adaptComparison,
  adaptEvidence,
  adaptForecast,
  adaptHealth,
  adaptHistory,
  adaptRisk,
} from './adapters';
import { toIsoWithTimeZone } from '@/utils/time';

export const DEFAULT_API_BASE_URL = '/api/v1';

/** 缺失 request_id 时的本地兜底，保证错误可追踪。 */
export function createRequestId(): string {
  const globalCrypto = globalThis.crypto;
  if (globalCrypto && typeof globalCrypto.randomUUID === 'function') {
    return globalCrypto.randomUUID();
  }
  return `local-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
}

/** 读取 VITE_API_BASE_URL，未配置时使用 /api/v1。 */
export function resolveApiBaseUrl(): string {
  const raw = import.meta.env?.VITE_API_BASE_URL;
  if (typeof raw === 'string' && raw.trim().length > 0) return raw.trim();
  return DEFAULT_API_BASE_URL;
}

function emptyShell<T>(status: ApiStatus, requestId: string, warnings: string[]): ApiResponse<T> {
  return { request_id: requestId, status, data: null, evidence: [], warnings };
}

/** 判断响应体是否符合统一响应外壳。 */
function isApiResponseLike(body: unknown): body is ApiResponse<unknown> {
  if (typeof body !== 'object' || body === null) return false;
  const candidate = body as Record<string, unknown>;
  return typeof candidate.status === 'string' || 'data' in candidate;
}

/** 把后端返回体规范成统一外壳，缺失字段用安全默认值补齐。 */
export function normalizeResponse<T>(body: unknown, fallbackStatus: ApiStatus, requestId: string): ApiResponse<T> {
  if (!isApiResponseLike(body)) {
    return {
      request_id: requestId,
      status: fallbackStatus,
      data: (body ?? null) as T | null,
      evidence: [],
      warnings: [],
    };
  }
  const candidate = body as Partial<ApiResponse<T>>;
  const status = (candidate.status ?? fallbackStatus) as ApiStatus;
  return {
    request_id: candidate.request_id ?? requestId,
    status,
    data: (candidate.data ?? null) as T | null,
    evidence: adaptEvidence(candidate.evidence as never),
    warnings: Array.isArray(candidate.warnings) ? candidate.warnings : [],
  };
}

/** 把原始 DTO 响应转换为展示模型响应，data 为 null 时不伪造。 */
function toViewResponse<TDto, TView>(
  response: ApiResponse<TDto>,
  adapt: (dto: TDto | null) => TView | null,
): ApiResponse<TView> {
  return { ...response, data: adapt(response.data) };
}

export interface ApiClientOptions {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

export interface ApiClient {
  readonly baseUrl: string;
  health(): Promise<ApiResponse<HealthData>>;
  history(query: HistoryQuery): Promise<ApiResponse<HistoryData>>;
  forecast(payload: ForecastRequestPayload): Promise<ApiResponse<ForecastData>>;
  comparison(horizons?: ForecastHorizon[] | null): Promise<ApiResponse<ModelComparison>>;
  risk(payload: RiskRequestPayload): Promise<ApiResponse<RiskVerdict>>;
  chat(payload: ChatRequestPayload): Promise<ApiResponse<AgentData>>;
  brief(payload: BriefRequestPayload): Promise<ApiResponse<BriefData>>;
}

/** HTTP 状态到统一响应状态的映射：503 为 unavailable，其余失败为 error。 */
export function statusFromHttp(httpStatus: number): ApiStatus {
  if (httpStatus === 503) return 'unavailable';
  return 'error';
}

/** 预测请求净化：省略空值，枚举字段只发送合法值，绝不发送 model: null / data_mode: null。 */
export function sanitizeForecastPayload(payload: ForecastRequestPayload): Record<string, unknown> {
  const source = stripNulls(payload as unknown as Record<string, unknown>) as unknown as ForecastRequestPayload;
  const result: Record<string, unknown> = {
    station_id: source.station_id ?? 'cata_12720',
    horizons: normalizeHorizons(source.horizons ?? null),
    model: normalizeModel(source.model ?? null),
    data_mode: normalizeDataMode(source.data_mode ?? null),
  };
  if (source.as_of) result.as_of = source.as_of;
  return result;
}

/** 对话请求净化：省略空值，枚举字段只发送合法值。 */
export function sanitizeChatPayload(payload: ChatRequestPayload): Record<string, unknown> {
  const source = stripNulls(payload as unknown as Record<string, unknown>) as unknown as ChatRequestPayload;
  return {
    message: source.message,
    station_id: source.station_id ?? 'cata_12720',
    horizons: normalizeHorizons(source.horizons ?? null),
    model: normalizeModel(source.model ?? null),
    data_mode: normalizeDataMode(source.data_mode ?? null),
  };
}

/** 简报请求净化。 */
export function sanitizeBriefPayload(payload: BriefRequestPayload): Record<string, unknown> {
  const source = stripNulls(payload as unknown as Record<string, unknown>) as unknown as BriefRequestPayload;
  return {
    station_id: source.station_id ?? 'cata_12720',
    horizons: normalizeHorizons(source.horizons ?? null),
    model: normalizeModel(source.model ?? null),
    data_mode: normalizeDataMode(source.data_mode ?? null),
  };
}

/**
 * 风险请求净化。
 *
 * 缺少 water_level 或 observed_at 时返回 null：调用方不应发请求，
 * 也不能补造水位（AGENTS.md §7）。
 */
export function sanitizeRiskPayload(payload: RiskRequestPayload): Record<string, unknown> | null {
  const waterLevel = payload?.water_level;
  const observedAt = payload?.observed_at;
  if (typeof waterLevel !== 'number' || !Number.isFinite(waterLevel) || !observedAt) return null;
  const result: Record<string, unknown> = {
    station_id: payload.station_id ?? 'cata_12720',
    water_level: waterLevel,
    observed_at: toIsoWithTimeZone(observedAt) ?? observedAt,
    horizons: normalizeHorizons(payload.horizons ?? null),
    model: payload.model ?? 'persistence',
    model_version: payload.model_version ?? 'single_level_input',
    data_mode: normalizeDataMode(payload.data_mode ?? null),
  };
  if (payload.predictions && Object.keys(payload.predictions).length > 0) {
    result.predictions = payload.predictions;
  }
  return result;
}

/**
 * 创建类型化 API 客户端。
 *
 * 请求体经净化后发送（枚举字段不发送 null），响应经 adapters 转换为展示模型；
 * 所有失败（HTTP 错误、网络中断、超时、非 JSON 响应）都转换为 ApiResponse，
 * 不抛未处理异常，也绝不补造水位、预测值、指标或风险等级。
 */
export function createApiClient(options: ApiClientOptions = {}): ApiClient {
  const baseUrl = (options.baseUrl ?? resolveApiBaseUrl()).replace(/\/$/, '');
  const timeoutMs = options.timeoutMs ?? 10000;
  const doFetch = options.fetchImpl ?? ((...args: Parameters<typeof fetch>) => fetch(...args));

  async function request<TDto>(
    path: string,
    init: RequestInit = {},
    query: Record<string, string | number | null | undefined> = {},
  ): Promise<ApiResponse<TDto>> {
    const requestId = createRequestId();
    const url = `${baseUrl}${path}${buildQueryString(query)}`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);

    try {
      const response = await doFetch(url, {
        ...init,
        signal: controller.signal,
        headers: { Accept: 'application/json', ...(init.headers ?? {}) },
      });
      const serverRequestId = response.headers?.get?.('x-request-id') ?? requestId;

      if (!response.ok) {
        const detail = await safeReadErrorDetail(response);
        return emptyShell<TDto>(statusFromHttp(response.status), serverRequestId, [
          `HTTP_${response.status}`,
          ...(detail ? [detail] : []),
        ]);
      }

      let body: unknown = null;
      try {
        body = await response.json();
      } catch {
        return emptyShell<TDto>('error', serverRequestId, ['INVALID_JSON']);
      }
      return normalizeResponse<TDto>(body, 'ok', serverRequestId);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const errorName = (error as { name?: string } | null | undefined)?.name;
      const aborted = errorName === 'AbortError';
      return emptyShell<TDto>('error', requestId, [aborted ? 'REQUEST_TIMEOUT' : `NETWORK_ERROR: ${message}`]);
    } finally {
      clearTimeout(timer);
    }
  }

  function post<TDto>(path: string, payload: unknown): Promise<ApiResponse<TDto>> {
    return request<TDto>(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload ?? {}),
    });
  }

  return {
    baseUrl,
    health: async () =>
      toViewResponse(await request<BackendHealthPayload>(API_ENDPOINTS.health), adaptHealth),
    history: async (query) =>
      toViewResponse(
        await request<BackendHistoryResult>(
          API_ENDPOINTS.history,
          {},
          {
            station_id: query.stationId,
            start: toIsoWithTimeZone(query.start ?? null),
            end: toIsoWithTimeZone(query.end ?? null),
            limit: query.limit ?? null,
          },
        ),
        adaptHistory,
      ),
    forecast: async (payload) =>
      toViewResponse(
        await post<BackendForecastResult>(API_ENDPOINTS.forecast, sanitizeForecastPayload(payload)),
        adaptForecast,
      ),
    comparison: async (horizons) =>
      toViewResponse(
        await request<BackendModelComparison>(
          API_ENDPOINTS.comparison,
          {},
          { horizons: serializeHorizons(horizons) },
        ),
        adaptComparison,
      ),
    risk: async (payload) => {
      const body = sanitizeRiskPayload(payload);
      if (!body) {
        return emptyShell<RiskVerdict>('unavailable', createRequestId(), [
          'INVALID_RISK_REQUEST: 缺少 water_level 或 observed_at，未发送风险判定请求',
        ]);
      }
      return toViewResponse(await post<BackendRiskResult>(API_ENDPOINTS.risk, body), adaptRisk);
    },
    chat: async (payload) =>
      toViewResponse(await post<BackendAgentResponse>(API_ENDPOINTS.chat, sanitizeChatPayload(payload)), adaptAgent),
    brief: async (payload) =>
      toViewResponse(await post<BackendBriefResult>(API_ENDPOINTS.brief, sanitizeBriefPayload(payload)), adaptBrief),
  };
}

async function safeReadErrorDetail(response: Response): Promise<string | null> {
  try {
    const body: unknown = await response.json();
    if (typeof body === 'object' && body !== null) {
      const candidate = body as Record<string, unknown>;
      const detail = candidate.detail ?? candidate.message;
      if (typeof detail === 'string') return detail;
    }
    return null;
  } catch {
    return null;
  }
}
