import { describe, expect, it } from 'vitest';
import { createDashboardStore, describeFailure, isStaleEvidence, currentDataMode } from '@/stores/dashboard';
import type { RiskVerdict } from '@/types/api';
import { createFakeClient } from './helpers/fake-client';
import { errorResponse, staleHistoryResponse, unavailableResponse, viewResponses } from './helpers/backend-snapshots';

describe('看板状态容器', () => {
  it('成功响应写入数据并合并证据', async () => {
    const store = createDashboardStore(createFakeClient());
    await store.loadHistory();
    expect(store.state.status).toBe('ok');
    expect(store.state.history?.points).toHaveLength(10);
    expect(store.state.history?.points.at(-1)?.water_level_m).toBe(3.74);
    expect(store.state.evidence.length).toBeGreaterThan(0);
    expect(store.state.error).toBeNull();
  });

  it('degraded 不视为错误，但暴露降级原因', async () => {
    const store = createDashboardStore(
      createFakeClient({ forecast: () => Promise.resolve(viewResponses.forecastDegraded) }),
    );
    await store.requestForecast({ horizons: [1, 3, 6] });
    expect(store.state.status).toBe('degraded');
    expect(store.state.error).toBeNull();
    expect(store.state.fallbackReason).toContain('MODEL_UNAVAILABLE');
    expect(store.state.forecast?.fallback_reason).toBe('model_artifact_unavailable');
    expect(store.state.forecast?.model).toBe('persistence');
    expect(store.state.forecast?.is_fallback).toBe(true);
  });

  it('预测请求发送合法枚举值，不发送 null', async () => {
    const captured: Record<string, unknown>[] = [];
    const store = createDashboardStore(
      createFakeClient({
        forecast: (payload) => {
          captured.push({ ...payload } as unknown as Record<string, unknown>);
          return Promise.resolve(viewResponses.forecast);
        },
      }),
    );
    await store.requestForecast({ horizons: [1, 3, 6] });
    const body = captured[0] ?? {};
    expect(body.model).toBe('persistence');
    expect(body.data_mode).toBe('history_replay');
    expect(Object.values(body).every((value) => value !== null)).toBe(true);
  });

  it('风险请求带上观测水位、观测时间与预测映射', async () => {
    const captured: Record<string, unknown>[] = [];
    const store = createDashboardStore(
      createFakeClient({
        risk: (payload) => {
          captured.push({ ...payload } as unknown as Record<string, unknown>);
          return Promise.resolve(viewResponses.risk);
        },
      }),
    );
    await store.loadHistory();
    await store.requestForecast({ horizons: [1, 3, 6] });
    await store.evaluateRisk();
    const body = captured[0] ?? {};
    expect(body.water_level).toBe(3.74);
    expect(body.observed_at).toBe('2026-01-01T11:00:00+08:00');
    expect(body.predictions).toEqual({ 1: 3.71, 3: 3.52, 6: 3.2 });
    expect(store.state.risk?.level).toBe('normal');
  });

  it('缺少观测证据时跳过风险请求并显示不可用，不补造水位', async () => {
    let called = false;
    const store = createDashboardStore(
      createFakeClient({
        history: () => Promise.resolve(viewResponses.historyEmpty),
        risk: () => {
          called = true;
          return Promise.resolve(viewResponses.risk);
        },
      }),
    );
    await store.loadHistory();
    const response = await store.evaluateRisk();
    expect(called).toBe(false);
    expect(response.status).toBe('unavailable');
    expect(store.state.risk).toBeNull();
    expect(store.state.riskSkippedReason).toContain('缺少观测水位或观测时间');
    expect(response.warnings.join()).toContain('DATA_UNAVAILABLE');
  });

  it('unavailable 只保留空数据与错误文案', async () => {
    const store = createDashboardStore(
      createFakeClient({
        history: () => Promise.resolve(unavailableResponse<never>()),
        forecast: () => Promise.resolve(unavailableResponse<never>()),
      }),
    );
    const response = await store.loadHistory();
    expect(response.status).toBe('unavailable');
    expect(store.state.history).toBeNull();
    expect(store.state.error).toContain('不可用');
    expect(store.state.error).toContain('HTTP_503');
  });

  it('接口失败时预测与风险数据均为空', async () => {
    const store = createDashboardStore(
      createFakeClient({
        forecast: () => Promise.resolve(errorResponse<never>()),
        risk: () => Promise.resolve(errorResponse<never>()),
      }),
    );
    await store.loadHistory();
    await store.requestForecast({ horizons: [1, 3, 6] });
    await store.evaluateRisk();
    expect(store.state.forecast).toBeNull();
    expect(store.state.risk).toBeNull();
    expect(store.state.error).toContain('请求失败');
  });

  it('客户端异常被捕获为 error，不向上抛出', async () => {
    const store = createDashboardStore(createFakeClient({ chat: () => Promise.reject(new Error('boom')) }));
    const response = await store.askAgent('当前水位');
    expect(response.status).toBe('error');
    expect(store.state.warnings.join()).toContain('CLIENT_EXCEPTION');
  });

  it('多次请求合并证据与告警并去重', async () => {
    const store = createDashboardStore(createFakeClient());
    await store.loadHistory();
    await store.loadHistory();
    await store.loadComparison([1, 3, 6]);
    const modes = new Set(store.state.evidence.map((item) => item.data_mode));
    expect(modes.has('history_replay')).toBe(true);
    const unique = new Set(store.state.evidence.map((item) => JSON.stringify(item)));
    expect(unique.size).toBe(store.state.evidence.length);
  });

  it('reset 清空所有结果与错误', async () => {
    const store = createDashboardStore(createFakeClient());
    await store.loadHistory();
    store.reset();
    expect(store.state.history).toBeNull();
    expect(store.state.evidence).toHaveLength(0);
    expect(store.state.error).toBeNull();
    expect(store.state.riskSkippedReason).toBeNull();
  });
});

describe('失败与陈旧状态判定', () => {
  it('只有 unavailable 与 error 产生错误文案', () => {
    expect(describeFailure('unavailable', ['HTTP_503'])).toContain('HTTP_503');
    expect(describeFailure('error', ['NETWORK_ERROR: x'])).toContain('NETWORK_ERROR');
    expect(describeFailure('ok', [])).toBeNull();
    expect(describeFailure('degraded', ['model_artifact_unavailable'])).toBeNull();
  });

  it('识别陈旧快照与数据模式', () => {
    const stale = staleHistoryResponse();
    expect(isStaleEvidence(stale.evidence)).toBe(true);
    expect(isStaleEvidence(viewResponses.history.evidence)).toBe(false);
    expect(currentDataMode(viewResponses.history.evidence)).toBe('history_replay');
    expect(currentDataMode(stale.evidence)).toBe('local_snapshot');
    expect(currentDataMode([])).toBeNull();
  });

  it('风险数据缺失时不返回等级', () => {
    const store = createDashboardStore(
      createFakeClient({ risk: () => Promise.resolve(unavailableResponse<RiskVerdict>()) }),
    );
    return store.evaluateRisk().then((response) => {
      expect(response.data).toBeNull();
      expect(store.state.risk).toBeNull();
    });
  });
});
