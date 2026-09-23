import { inject, reactive, type InjectionKey } from 'vue';
import { createApiClient, createRequestId, type ApiClient } from '@/api/client';
import type {
  AgentData,
  ApiResponse,
  ApiStatus,
  BriefData,
  DataMode,
  Evidence,
  ForecastData,
  ForecastHorizon,
  HealthData,
  HistoryData,
  ModelComparison,
  RiskVerdict,
} from '@/types/api';
import type { BriefRequestPayload, ForecastRequestPayload, HistoryQuery, RiskRequestPayload } from '@/api/endpoints';
import {
  buildBriefRequest,
  buildChatRequest,
  buildForecastRequest,
  buildRiskRequest,
  DEFAULT_HORIZONS,
} from '@/api/endpoints';
import { latestObservedPoint, toPredictionMap } from '@/api/adapters';

export const DEFAULT_STATION_ID = 'cata_12720';

export interface DashboardState {
  loading: boolean;
  status: ApiStatus | null;
  error: string | null;
  requestId: string | null;
  fallbackReason: string | null;
  /** 因缺少观测证据而跳过风险判定请求时的原因，页面据此展示明确不可用状态。 */
  riskSkippedReason: string | null;
  stationId: string;
  health: HealthData | null;
  history: HistoryData | null;
  forecast: ForecastData | null;
  comparison: ModelComparison | null;
  risk: RiskVerdict | null;
  agent: AgentData | null;
  brief: BriefData | null;
  evidence: Evidence[];
  warnings: string[];
}

export interface DashboardStore {
  readonly state: DashboardState;
  loadHealth(): Promise<ApiResponse<HealthData>>;
  loadHistory(query?: Partial<HistoryQuery>): Promise<ApiResponse<HistoryData>>;
  requestForecast(payload?: Partial<ForecastRequestPayload>): Promise<ApiResponse<ForecastData>>;
  loadComparison(horizons?: ForecastHorizon[]): Promise<ApiResponse<ModelComparison>>;
  evaluateRisk(payload?: Partial<RiskRequestPayload>): Promise<ApiResponse<RiskVerdict>>;
  askAgent(message: string): Promise<ApiResponse<AgentData>>;
  loadBrief(payload?: Partial<BriefRequestPayload>): Promise<ApiResponse<BriefData>>;
  reset(): void;
}

export const DashboardStoreKey: InjectionKey<DashboardStore> = Symbol('river-sentinel-dashboard');

/** 失败状态描述：unavailable 与 error 都返回可展示原因，不补造数据。 */
export function describeFailure(status: ApiStatus | null, warnings: string[]): string | null {
  if (status === 'unavailable') {
    return `后端或数据不可用（${warnings.join('；') || '未提供原因'}）`;
  }
  if (status === 'error') {
    return `请求失败（${warnings.join('；') || '未提供原因'}）`;
  }
  return null;
}

/** 证据是否表示陈旧数据。 */
export function isStaleEvidence(evidence: Evidence[]): boolean {
  return evidence.some(
    (item) =>
      (typeof item.freshness === 'string' && item.freshness.toLowerCase().includes('stale')) ||
      (typeof item.freshness === 'string' && item.freshness.includes('陈旧')),
  );
}

/** 当前数据模式，取最近一条证据。 */
export function currentDataMode(evidence: Evidence[]): DataMode | null {
  for (let i = evidence.length - 1; i >= 0; i -= 1) {
    const mode = evidence[i]?.data_mode;
    if (mode) return mode;
  }
  return null;
}

function mergeEvidence(current: Evidence[], incoming: Evidence[]): Evidence[] {
  const seen = new Set(current.map((item) => JSON.stringify(item)));
  const merged = [...current];
  incoming.forEach((item) => {
    const key = JSON.stringify(item);
    if (seen.has(key)) return;
    seen.add(key);
    merged.push(item);
  });
  return merged.slice(-20);
}

function mergeWarnings(current: string[], incoming: string[]): string[] {
  const seen = new Set(current);
  const merged = [...current];
  incoming.forEach((item) => {
    if (seen.has(item)) return;
    seen.add(item);
    merged.push(item);
  });
  return merged.slice(-20);
}

/**
 * 创建看板状态容器。
 *
 * degraded 不作为错误，只暴露 fallbackReason；unavailable / error 只保留空数据 + 错误文案。
 */
export function createDashboardStore(client: ApiClient, stationId = DEFAULT_STATION_ID): DashboardStore {
  const state = reactive<DashboardState>({
    loading: false,
    status: null,
    error: null,
    requestId: null,
    fallbackReason: null,
    riskSkippedReason: null,
    stationId,
    health: null,
    history: null,
    forecast: null,
    comparison: null,
    risk: null,
    agent: null,
    brief: null,
    evidence: [],
    warnings: [],
  });

  function apply<T>(response: ApiResponse<T>): ApiResponse<T> {
    state.loading = false;
    state.status = response.status;
    state.requestId = response.request_id;
    state.evidence = mergeEvidence(state.evidence, response.evidence ?? []);
    state.warnings = mergeWarnings(state.warnings, response.warnings ?? []);
    state.error = describeFailure(response.status, response.warnings ?? []);
    state.fallbackReason =
      response.status === 'degraded'
        ? response.warnings?.join('；') || '未提供降级原因'
        : null;
    return response;
  }

  async function run<T>(task: () => Promise<ApiResponse<T>>): Promise<ApiResponse<T>> {
    state.loading = true;
    try {
      const response = await task();
      return apply(response);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return apply<T>({
        request_id: state.requestId ?? 'local-error',
        status: 'error',
        data: null,
        evidence: [],
        warnings: [`CLIENT_EXCEPTION: ${message}`],
      });
    }
  }

  return {
    state,
    loadHealth: async () => {
      const response = await run(() => client.health());
      state.health = response.data;
      return response;
    },
    loadHistory: async (query) => {
      const response = await run(() =>
        client.history({
          stationId: query?.stationId ?? state.stationId,
          start: query?.start ?? null,
          end: query?.end ?? null,
          limit: query?.limit ?? null,
        }),
      );
      state.history = response.data;
      return response;
    },
    requestForecast: async (payload) => {
      // 枚举字段只发送合法值：默认持久性基线 + 历史回放，绝不发送 null。
      const request = buildForecastRequest({
        station_id: payload?.station_id ?? state.stationId,
        as_of: payload?.as_of ?? null,
        horizons: payload?.horizons ?? DEFAULT_HORIZONS,
        model: payload?.model ?? 'persistence',
        data_mode: payload?.data_mode ?? 'history_replay',
      });
      const response = await run(() => client.forecast(request));
      state.forecast = response.data;
      return response;
    },
    loadComparison: async (horizons) => {
      const response = await run(() => client.comparison(horizons ?? null));
      state.comparison = response.data;
      return response;
    },
    /**
     * 风险判定：观测水位与时间取最后一次有效观测，预测取当前预测结果。
     *
     * 缺少观测证据时**跳过请求**并返回 unavailable，不补造水位（AGENTS.md §7）。
     */
    evaluateRisk: async (payload) => {
      state.riskSkippedReason = null;
      const observed = latestObservedPoint(state.history);
      const request = buildRiskRequest({
        station_id: payload?.station_id ?? state.stationId,
        water_level: payload?.water_level ?? observed?.water_level_m ?? null,
        observed_at: payload?.observed_at ?? observed?.observed_at ?? state.history?.latest_observed_at ?? null,
        horizons: payload?.horizons ?? DEFAULT_HORIZONS,
        predictions: payload?.predictions ?? toPredictionMap(state.forecast),
        model: payload?.model ?? state.forecast?.model ?? 'persistence',
        model_version: payload?.model_version ?? state.forecast?.model_version ?? 'single_level_input',
        data_mode: payload?.data_mode ?? 'history_replay',
      });
      if (!request) {
        state.risk = null;
        state.riskSkippedReason = '缺少观测水位或观测时间，未发送风险判定请求';
        return apply<RiskVerdict>({
          request_id: createRequestId(),
          status: 'unavailable',
          data: null,
          evidence: [],
          warnings: [`DATA_UNAVAILABLE: ${state.riskSkippedReason}`],
        });
      }
      const response = await run(() => client.risk(request));
      state.risk = response.data;
      return response;
    },
    askAgent: async (message) => {
      const response = await run(() =>
        client.chat(
          buildChatRequest({
            message,
            station_id: state.stationId,
            horizons: DEFAULT_HORIZONS,
            model: state.forecast?.model ?? 'persistence',
            data_mode: 'history_replay',
          }),
        ),
      );
      state.agent = response.data;
      return response;
    },
    loadBrief: async (payload) => {
      const response = await run(() =>
        client.brief(
          buildBriefRequest({
            station_id: payload?.station_id ?? state.stationId,
            horizons: payload?.horizons ?? DEFAULT_HORIZONS,
            model: payload?.model ?? state.forecast?.model ?? 'persistence',
            data_mode: payload?.data_mode ?? 'history_replay',
          }),
        ),
      );
      state.brief = response.data;
      return response;
    },
    reset: () => {
      state.loading = false;
      state.status = null;
      state.error = null;
      state.requestId = null;
      state.fallbackReason = null;
      state.riskSkippedReason = null;
      state.health = null;
      state.history = null;
      state.forecast = null;
      state.comparison = null;
      state.risk = null;
      state.agent = null;
      state.brief = null;
      state.evidence = [];
      state.warnings = [];
    },
  };
}

let defaultStore: DashboardStore | null = null;

/** 默认 store（使用真实 API 客户端），仅在未注入时使用。 */
export function getDefaultDashboardStore(): DashboardStore {
  if (!defaultStore) defaultStore = createDashboardStore(createApiClient());
  return defaultStore;
}

/** 组件获取 store：优先使用注入的测试 store。 */
export function useDashboardStore(): DashboardStore {
  return inject(DashboardStoreKey, null) ?? getDefaultDashboardStore();
}
