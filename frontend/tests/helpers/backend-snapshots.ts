/**
 * 真实后端响应快照（DS-8A TestClient 现场抓取）。
 *
 * 这些 JSON 由 `tests/fixtures/backend/capture_contract_snapshots.py` 用后端
 * `TestClient` 真实调用生成，代表后端实际契约；前端测试不得再使用自造字段冒充后端。
 * 展示层测试通过 `viewResponses` 走一遍“后端 DTO → 展示模型”适配层，保证契约漂移可被发现。
 */

import { adaptAgent, adaptBrief, adaptComparison, adaptForecast, adaptHealth, adaptHistory, adaptRisk } from '@/api/adapters';
import type { ApiResponse, HistoryData } from '@/types/api';
import type {
  BackendAgentResponse,
  BackendApiResponse,
  BackendBriefResult,
  BackendForecastResult,
  BackendHealthPayload,
  BackendHistoryResult,
  BackendModelComparison,
  BackendRiskResult,
} from '@/types/backend';
import agentRaw from '../fixtures/backend/agent.json';
import briefRaw from '../fixtures/backend/brief.json';
import comparisonRaw from '../fixtures/backend/comparison.json';
import forecastDegradedRaw from '../fixtures/backend/forecast-degraded.json';
import forecastRaw from '../fixtures/backend/forecast.json';
import healthRaw from '../fixtures/backend/health.json';
import historyEmptyRaw from '../fixtures/backend/history-empty.json';
import historyRaw from '../fixtures/backend/history.json';
import requestsRaw from '../fixtures/backend/requests.json';
import riskRaw from '../fixtures/backend/risk.json';

/** 后端原始响应（字段名与后端一致）。 */
export const backendSnapshots = {
  health: healthRaw as unknown as BackendApiResponse<BackendHealthPayload>,
  history: historyRaw as unknown as BackendApiResponse<BackendHistoryResult>,
  historyEmpty: historyEmptyRaw as unknown as BackendApiResponse<BackendHistoryResult>,
  forecast: forecastRaw as unknown as BackendApiResponse<BackendForecastResult>,
  forecastDegraded: forecastDegradedRaw as unknown as BackendApiResponse<BackendForecastResult>,
  comparison: comparisonRaw as unknown as BackendApiResponse<BackendModelComparison>,
  risk: riskRaw as unknown as BackendApiResponse<BackendRiskResult>,
  agent: agentRaw as unknown as BackendApiResponse<BackendAgentResponse>,
  brief: briefRaw as unknown as BackendApiResponse<BackendBriefResult>,
};

/** 与后端联调实际发送并被接受（200）的请求体。 */
export const backendRequests = requestsRaw as unknown as {
  forecast: Record<string, unknown>;
  forecast_degraded: Record<string, unknown>;
  risk: Record<string, unknown>;
  agent: Record<string, unknown>;
  brief: Record<string, unknown>;
  history_query: Record<string, unknown>;
  comparison_query: Record<string, unknown>;
};

/** 把后端原始响应转换为展示模型响应（与 ApiClient 内部行为一致）。 */
function toView<TDto, TView>(
  response: BackendApiResponse<TDto>,
  adapt: (dto: TDto | null) => TView | null,
): ApiResponse<TView> {
  return {
    request_id: response.request_id,
    status: response.status,
    data: adapt(response.data),
    evidence: (response.evidence ?? []).map((item) => ({
      source_url: item.source_url ?? null,
      data_mode: item.data_mode ?? 'history_replay',
      observed_at: item.observed_at ?? null,
      freshness: item.freshness ?? 'unknown',
      model_version: item.model_version ?? null,
      tool_trace: item.tool_trace ?? [],
      notes: item.notes ?? [],
    })),
    warnings: response.warnings ?? [],
  };
}

/** 真实快照经适配层得到的展示模型响应。 */
export const viewResponses = {
  health: toView(backendSnapshots.health, adaptHealth),
  history: toView(backendSnapshots.history, adaptHistory),
  historyEmpty: toView(backendSnapshots.historyEmpty, adaptHistory),
  forecast: toView(backendSnapshots.forecast, adaptForecast),
  forecastDegraded: toView(backendSnapshots.forecastDegraded, adaptForecast),
  comparison: toView(backendSnapshots.comparison, adaptComparison),
  risk: toView(backendSnapshots.risk, adaptRisk),
  agent: toView(backendSnapshots.agent, adaptAgent),
  brief: toView(backendSnapshots.brief, adaptBrief),
} as const;

/** 复制快照并做脱敏修改：仅用于缺失值/陈旧/不可用等展示分支，不代表真实观测。 */
export function cloneSnapshot<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

/**
 * 陈旧快照变体：在真实历史快照上只修改 freshness 与 data_mode，
 * 用于验证“数据陈旧 / 本地快照”展示分支（后端样例数据本身为 fresh）。
 */
export function staleHistoryResponse(): ApiResponse<HistoryData> {
  const snapshot = cloneSnapshot(backendSnapshots.history);
  const markStale = (items: unknown[]) =>
    (items ?? []).map((item) => {
      const evidence = item as Record<string, unknown>;
      return { ...evidence, freshness: 'stale', data_mode: 'local_snapshot' };
    });
  if (snapshot.data) {
    snapshot.data.freshness = 'stale';
    snapshot.data.data_mode = 'local_snapshot';
    snapshot.data.evidence = markStale(snapshot.data.evidence ?? []) as never;
  }
  snapshot.evidence = markStale(snapshot.evidence ?? []) as never;
  return toView(snapshot, adaptHistory);
}

/** 不可用响应（503）。 */
export function unavailableResponse<T>(requestId = 'req-unavailable'): ApiResponse<T> {
  return { request_id: requestId, status: 'unavailable', data: null, evidence: [], warnings: ['HTTP_503'] };
}

/** 网络/解析失败响应。 */
export function errorResponse<T>(requestId = 'req-error'): ApiResponse<T> {
  return {
    request_id: requestId,
    status: 'error',
    data: null,
    evidence: [],
    warnings: ['NETWORK_ERROR: fetch failed'],
  };
}
