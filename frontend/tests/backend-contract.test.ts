/**
 * 后端真实契约回归测试。
 *
 * fixture 来自 `tests/fixtures/backend/*.json`，由后端 TestClient 现场抓取；
 * 请求体与 `requests.json` 一致（该请求体已在后端真实接口上返回 200）。
 * 这些用例覆盖 P1-1（字段映射）与 P1-2（请求体非法枚举/缺字段）。
 */

import { describe, expect, it } from 'vitest';
import { createApiClient, sanitizeForecastPayload, sanitizeRiskPayload } from '@/api/client';
import {
  adaptComparison,
  adaptForecast,
  adaptHistory,
  latestObservedPoint,
  selectEventMetrics,
  summarizeBasis,
  toPredictionMap,
} from '@/api/adapters';
import { buildForecastRequest, buildRiskRequest } from '@/api/endpoints';
import { toObservedPairs, groupPredictionModels } from '@/charts/waterLevelOption';
import {
  EVENT_METRICS_UNAVAILABLE,
  eventMetricsText,
  formatMetric,
  isRiskRenderable,
  sortMetricRows,
} from '@/types/view';
import { PROJECT_TIME_ZONE, toIsoWithTimeZone } from '@/utils/time';
import {
  backendRequests,
  backendSnapshots,
  cloneSnapshot,
  viewResponses,
} from './helpers/backend-snapshots';

type FetchArgs = { url: string; init?: RequestInit };

function recordingFetch(handler: (url: string) => { status: number; body: unknown }): {
  fetchImpl: typeof fetch;
  calls: FetchArgs[];
} {
  const calls: FetchArgs[] = [];
  const fetchImpl = ((...args: Parameters<typeof fetch>) => {
    const url = String(args[0]);
    calls.push({ url, init: args[1] });
    const { status, body } = handler(url);
    return Promise.resolve(
      new Response(typeof body === 'string' ? body : JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
  }) as unknown as typeof fetch;
  return { fetchImpl, calls };
}

describe('历史接口真实字段（P1-1）', () => {
  it('历史点读取 water_level_m，最新观测时间与后端一致', () => {
    const history = adaptHistory(backendSnapshots.history.data);
    expect(history?.points).toHaveLength(10);
    expect(history?.points[0]?.water_level_m).toBe(3.1);
    expect(history?.points.at(-1)?.water_level_m).toBe(3.74);
    expect(history?.latest_observed_at).toBe('2026-01-01T11:00:00+08:00');
    expect(history?.data_mode).toBe('history_replay');
    expect(history?.missing_count).toBe(0);
    // 后端真实响应不存在旧前端使用的 value 字段。
    expect(Object.keys(backendSnapshots.history.data?.points[0] ?? {})).not.toContain('value');
  });

  it('缺失水位保持 null，图表保留断点且不绘制为 0', () => {
    const snapshot = cloneSnapshot(backendSnapshots.history);
    if (snapshot.data) snapshot.data.points[2]!.water_level_m = null;
    const history = adaptHistory(snapshot.data);
    expect(history?.missing_count).toBe(1);
    expect(history?.points[2]?.water_level_m).toBeNull();

    const pairs = toObservedPairs(history?.points ?? []);
    expect(pairs[2]?.[1]).toBeNull();
    expect(pairs.some(([, value]) => value === 0)).toBe(false);
  });

  it('历史空区间响应为 unavailable，不绘制任何点', () => {
    const empty = viewResponses.historyEmpty;
    expect(empty.status).toBe('unavailable');
    expect(empty.data?.points ?? []).toHaveLength(0);
  });
});

describe('预测接口真实字段（P1-1）', () => {
  it('预测点读取 target_at / water_level_m', () => {
    const forecast = adaptForecast(backendSnapshots.forecast.data);
    expect(forecast?.model).toBe('persistence');
    expect(forecast?.model_version).toBe('ds5b-hourly-baseline-1');
    expect(forecast?.predictions).toHaveLength(3);
    const h1 = forecast?.predictions.find((point) => point.horizon === 1);
    expect(h1?.target_at).toBe('2026-01-01T12:00:00+08:00');
    expect(h1?.water_level_m).toBe(3.71);
    expect(h1?.source).toBe('persistence');
    expect(groupPredictionModels(forecast?.predictions ?? [])).toEqual(['persistence']);
    // 后端真实响应不存在旧前端使用的 target_time / value 字段。
    expect(Object.keys(backendSnapshots.forecast.data?.predictions[0] ?? {})).not.toContain('target_time');
    expect(Object.keys(backendSnapshots.forecast.data?.predictions[0] ?? {})).not.toContain('value');
  });

  it('模型不可用时降级为已验证基线并保留原因', () => {
    const degraded = adaptForecast(backendSnapshots.forecastDegraded.data);
    expect(viewResponses.forecastDegraded.status).toBe('degraded');
    expect(degraded?.requested_model).toBe('xgboost');
    expect(degraded?.model).toBe('persistence');
    expect(degraded?.is_fallback).toBe(true);
    expect(degraded?.fallback_reason).toBe('model_artifact_unavailable');
  });
});

describe('模型对比真实字段（P1-1 / 需求 3）', () => {
  it('后端 metrics 映射为展示行，保留版本与原因', () => {
    const comparison = viewResponses.comparison.data;
    expect(comparison?.rows).toHaveLength(21);
    const lstm = comparison?.rows.find((row) => row.model === 'lstm_e1' && row.horizon === 1 && row.segment === 'test');
    expect(lstm?.display_name).toBe('LSTM E1');
    expect(lstm?.mae).toBeCloseTo(0.111667, 6);
    expect(lstm?.rmse).toBeCloseTo(0.146782, 6);
    expect(lstm?.r2).toBeCloseTo(0.837538, 6);
    expect(lstm?.nse).toBeCloseTo(0.837538, 6);
    expect(lstm?.model_version).toBe('ds6-e1');
    expect(lstm?.sample_count).toBe(144);
    expect(lstm?.supported).toBe(true);
    // 后端真实响应使用 metrics，不是旧前端的 rows。
    expect(Object.keys(backendSnapshots.comparison.data ?? {})).not.toContain('rows');
  });

  it('不可用模型显示为不可用/未提供，不参与排序', () => {
    const snapshot = cloneSnapshot(backendSnapshots.comparison);
    const target = snapshot.data?.metrics.find((cell) => cell.model === 'lstm_e1' && cell.horizon === 1);
    if (target) {
      target.mae = null;
      target.rmse = null;
      target.r2 = null;
      target.nse = null;
      target.sample_count = null;
      target.reason = 'valid_samples_insufficient';
    }
    const rows = adaptComparison(snapshot.data)?.rows ?? [];
    const lstm = rows.find((row) => row.model === 'lstm_e1' && row.horizon === 1);
    expect(lstm?.supported).toBe(false);
    expect(lstm?.mae).toBeNull();
    expect(lstm?.reason).toBe('valid_samples_insufficient');
    expect(formatMetric(lstm?.mae)).toBe('不可用/未提供');
    // 不可用行被排在末尾，不参与 MAE 升序比较。
    expect(sortMetricRows(rows).at(-1)?.model).toBe('lstm_e1');
  });

  it('test 段零事件显示“事件级指标无法计算”，不渲染 F1=0', () => {
    const comparison = viewResponses.comparison.data;
    const rows = selectEventMetrics(comparison?.event_rows ?? [], { segment: 'test', horizon: 1, models: null });
    expect(rows).toHaveLength(7);
    rows.forEach((row) => {
      expect(row.computable).toBe(false);
      expect(row.f1).toBeNull();
      expect(row.reason).toBe('no_observed_events');
      const text = eventMetricsText(row);
      expect(text.computable).toBe(false);
      expect(text.text).toBe(EVENT_METRICS_UNAVAILABLE);
      expect(text.text).not.toContain('0.000');
    });
  });

  it('事件指标选择条件显式包含 segment / horizon / model', () => {
    const comparison = viewResponses.comparison.data;
    const rows = selectEventMetrics(comparison?.event_rows ?? [], {
      segment: 'test',
      horizon: 3,
      models: ['persistence'],
    });
    expect(rows).toHaveLength(1);
    expect(rows[0]?.segment).toBe('test');
    expect(rows[0]?.horizon).toBe(3);
    expect(rows[0]?.model).toBe('persistence');
  });
});

describe('风险判定真实字段（P1-1 / 需求 4）', () => {
  it('没有 computable 字段时，存在 level 仍可展示', () => {
    const raw = backendSnapshots.risk.data;
    expect(raw && 'computable' in raw).toBe(false);
    const verdict = viewResponses.risk.data;
    expect(verdict?.level).toBe('normal');
    expect(isRiskRenderable(verdict)).toBe(true);
    expect(verdict?.threshold).toBe(7.18);
    expect(verdict?.basis_source).toBe('deterministic_rule');
    expect(verdict?.last_observed_at).toBe('2026-01-01T11:00:00+08:00');
    expect(verdict?.anchor_at).toBe('2026-01-01T11:00:00+08:00');
    expect(verdict?.triggered_horizons).toEqual([]);
    expect(verdict?.missing_horizons).toEqual([]);
  });

  it('预测值键规范化为 1/3/6，缺失保持 null', () => {
    const verdict = viewResponses.risk.data;
    expect(verdict?.predicted_values).toEqual({ '1': 3.71, '3': 3.52, '6': 3.2 });
  });

  it('basis 对象格式化为稳定中文摘要，不出现 [object Object]', () => {
    const { basis_summary, basis_details } = summarizeBasis(backendSnapshots.risk.data?.basis ?? {});
    expect(basis_summary).not.toContain('[object Object]');
    expect(basis_summary).toContain('研究性阈值：7.18');
    expect(basis_summary).toContain('模型：persistence');
    expect(basis_summary).toContain('h1=3.71');
    basis_details.forEach((entry) => {
      expect(entry.value).not.toContain('[object Object]');
      expect(entry.value).not.toContain('undefined');
    });
    const predicted = basis_details.find((entry) => entry.label === '预测值');
    expect(predicted?.value).toBe('h1=3.71、h3=3.52、h6=3.2');
  });
});

describe('健康检查真实字段（P1-1 / 需求 5）', () => {
  it('状态来自外层 ApiResponse.status，数据体只描述可用性', () => {
    expect(backendSnapshots.health.status).toBe('ok');
    const health = viewResponses.health.data;
    expect(health?.sample_available).toBe(true);
    expect(health?.persistence_fallback_available).toBe(true);
    expect(health?.external_provider_available).toBe(false);
    expect(health?.supported_horizons).toEqual([1, 3, 6]);
    expect(health?.research_threshold).toBe(7.18);
    // 数据体不存在旧前端读取的 status / model_artifacts_available。
    expect(health && 'status' in health).toBe(false);
  });
});

describe('智能体回答保留证据与依据', () => {
  it('保留工具摘要、模型版本、观测时间与风险等级', () => {
    const agent = viewResponses.agent.data;
    expect(agent?.intent).toBe('risk');
    expect(agent?.tool_trace).toEqual(['forecast_water_level', 'evaluate_research_risk']);
    expect(agent?.model_version).toBe('ds5b-hourly-baseline-1');
    expect(agent?.observed_at).toBe('2026-01-01T11:00:00+08:00');
    expect(agent?.risk_verdict?.level).toBe('normal');
    expect(agent?.risk_verdict?.basis_summary).not.toContain('[object Object]');
    expect(agent?.evidence_summaries.join()).toContain('h1=3.71');
  });
});

describe('请求体契约（P1-2）', () => {
  it('预测请求不发送 model: null / data_mode: null，并与后端已验证请求体一致', async () => {
    const { fetchImpl, calls } = recordingFetch(() => ({ status: 200, body: backendSnapshots.forecast }));
    const client = createApiClient({ baseUrl: '/api/v1', fetchImpl });
    const payload = buildForecastRequest({
      station_id: 'cata_12720',
      horizons: null,
      model: null,
      data_mode: null,
      as_of: null,
    });
    const body = sanitizeForecastPayload(payload);
    expect(Object.values(body).every((value) => value !== null)).toBe(true);
    expect(body.model).toBe('persistence');
    expect(body.data_mode).toBe('history_replay');
    expect(body).toEqual(backendRequests.forecast);

    const response = await client.forecast(payload);
    expect(response.status).toBe('ok');
    const sent = JSON.parse(String(calls[0]?.init?.body ?? '{}')) as Record<string, unknown>;
    expect(sent.model).toBe('persistence');
    expect(sent.data_mode).toBe('history_replay');
    expect(Object.values(sent).every((value) => value !== null)).toBe(true);
    expect(String(calls[0]?.init?.body)).not.toContain('null');
  });

  it('风险请求包含 water_level / observed_at / predictions，并与后端已验证请求体一致', async () => {
    const history = adaptHistory(backendSnapshots.history.data);
    const forecast = adaptForecast(backendSnapshots.forecast.data);
    const observed = latestObservedPoint(history);
    const payload = buildRiskRequest({
      station_id: 'cata_12720',
      water_level: observed?.water_level_m ?? null,
      observed_at: observed?.observed_at ?? null,
      horizons: [1, 3, 6],
      predictions: toPredictionMap(forecast),
      model: forecast?.model ?? null,
      model_version: forecast?.model_version ?? null,
      data_mode: 'history_replay',
    });
    expect(payload).not.toBeNull();
    expect(payload?.water_level).toBe(3.74);
    expect(payload?.observed_at).toBe('2026-01-01T11:00:00+08:00');
    expect(payload?.predictions).toEqual({ 1: 3.71, 3: 3.52, 6: 3.2 });
    expect(payload).toEqual(backendRequests.risk);

    const { fetchImpl, calls } = recordingFetch(() => ({ status: 200, body: backendSnapshots.risk }));
    const client = createApiClient({ baseUrl: '/api/v1', fetchImpl });
    const response = await client.risk(payload!);
    expect(response.status).toBe('ok');
    const sent = JSON.parse(String(calls[0]?.init?.body ?? '{}')) as Record<string, unknown>;
    expect(sent.water_level).toBe(3.74);
    expect(sent.observed_at).toBe('2026-01-01T11:00:00+08:00');
    expect(sanitizeRiskPayload({ ...payload!, water_level: Number.NaN })).toBeNull();
  });

  it('缺少观测证据时风险请求返回 null，不补造水位', () => {
    expect(buildRiskRequest({ station_id: 'cata_12720', water_level: null, observed_at: null })).toBeNull();
    expect(
      buildRiskRequest({ station_id: 'cata_12720', water_level: 3.74, observed_at: '' }),
    ).toBeNull();
  });

  it('datetime-local 时间附加项目时区 Asia/Shanghai', () => {
    expect(PROJECT_TIME_ZONE).toBe('Asia/Shanghai');
    expect(toIsoWithTimeZone('2026-01-01T11:00')).toBe('2026-01-01T11:00:00+08:00');
    // 已带偏移的后端时间原样透传，避免二次转换。
    expect(toIsoWithTimeZone('2026-01-01T11:00:00+08:00')).toBe('2026-01-01T11:00:00+08:00');
    expect(toIsoWithTimeZone('')).toBeNull();
    expect(toIsoWithTimeZone('not-a-time')).toBeNull();
  });
});
