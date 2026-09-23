import { describe, expect, it, vi } from 'vitest';
import { createApiClient, resolveApiBaseUrl, statusFromHttp } from '@/api/client';
import { backendSnapshots, unavailableResponse } from './helpers/backend-snapshots';

type FetchArgs = { url: string; init?: RequestInit };

function jsonFetch(body: unknown, status = 200): typeof fetch {
  return ((...args: Parameters<typeof fetch>) => {
    const [input, init] = args;
    void input;
    void init;
    return Promise.resolve(
      new Response(typeof body === 'string' ? body : JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json', 'x-request-id': 'server-req-1' },
      }),
    );
  }) as unknown as typeof fetch;
}

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

describe('API 基础地址', () => {
  it('未配置 VITE_API_BASE_URL 时使用 /api/v1', () => {
    expect(resolveApiBaseUrl()).toBe('/api/v1');
  });

  it('503 映射为 unavailable，其余失败映射为 error', () => {
    expect(statusFromHttp(503)).toBe('unavailable');
    expect(statusFromHttp(500)).toBe('error');
    expect(statusFromHttp(422)).toBe('error');
  });
});

describe('API 客户端状态处理', () => {
  it('ok 响应保留 data、evidence 和 request_id', async () => {
    const client = createApiClient({ baseUrl: '/api/v1', fetchImpl: jsonFetch(backendSnapshots.history) });
    const response = await client.history({ stationId: 'cata_12720' });
    expect(response.status).toBe('ok');
    expect(response.data?.station_id).toBe('cata_12720');
    expect(response.data?.points).toHaveLength(10);
    expect(response.data?.points.at(-1)?.water_level_m).toBe(3.74);
    expect(response.request_id).toBe(backendSnapshots.history.request_id);
    expect(response.evidence[0]?.data_mode).toBe('history_replay');
  });

  it('degraded 响应保留降级状态与 warning，不当作错误吞掉', async () => {
    const client = createApiClient({
      baseUrl: '/api/v1',
      fetchImpl: jsonFetch(backendSnapshots.forecastDegraded),
    });
    const response = await client.forecast({ station_id: 'cata_12720', horizons: [1, 3, 6] });
    expect(response.status).toBe('degraded');
    expect(response.warnings.join()).toContain('MODEL_UNAVAILABLE');
    expect(response.data?.model).toBe('persistence');
    expect(response.data?.requested_model).toBe('xgboost');
    expect(response.data?.is_fallback).toBe(true);
  });

  it('后端返回 unavailable 时原样透传，数据为 null', async () => {
    const client = createApiClient({
      baseUrl: '/api/v1',
      fetchImpl: jsonFetch({
        request_id: 'x',
        status: 'unavailable',
        data: null,
        evidence: [],
        warnings: ['DATA_UNAVAILABLE'],
      }),
    });
    const response = await client.comparison([1, 3, 6]);
    expect(response.status).toBe('unavailable');
    expect(response.data).toBeNull();
    expect(response.warnings).toContain('DATA_UNAVAILABLE');
  });

  it('503 映射为 unavailable 且不抛异常', async () => {
    const client = createApiClient({ baseUrl: '/api/v1', fetchImpl: jsonFetch({}, 503) });
    const response = await client.history({ stationId: 'cata_12720' });
    expect(response.status).toBe('unavailable');
    expect(response.data).toBeNull();
    expect(response.warnings.some((item) => item.includes('HTTP_503'))).toBe(true);
  });

  it('网络失败映射为 error 且不抛异常', async () => {
    const fetchImpl = (() => Promise.reject(new TypeError('fetch failed'))) as unknown as typeof fetch;
    const client = createApiClient({ baseUrl: '/api/v1', fetchImpl });
    const response = await client.chat({ message: '当前水位' });
    expect(response.status).toBe('error');
    expect(response.data).toBeNull();
    expect(response.warnings.join()).toContain('NETWORK_ERROR');
    expect(response.request_id).toBeTruthy();
  });

  it('非 JSON 响应映射为 error，不补造数据', async () => {
    const client = createApiClient({ baseUrl: '/api/v1', fetchImpl: jsonFetch('not-json', 200) });
    const response = await client.health();
    expect(response.status).toBe('error');
    expect(response.data).toBeNull();
    expect(response.warnings).toContain('INVALID_JSON');
  });

  it('请求 URL 使用配置的 baseUrl 与查询参数', async () => {
    const { fetchImpl, calls } = recordingFetch(() => ({ status: 200, body: backendSnapshots.comparison }));
    const client = createApiClient({ baseUrl: 'http://127.0.0.1:8000/api/v1/', fetchImpl });
    await client.comparison([1, 3, 6]);
    expect(calls[0]?.url).toBe('http://127.0.0.1:8000/api/v1/model-comparison?horizons=1%2C3%2C6');
  });

  it('历史查询拼接断面与带时区的时间范围', async () => {
    const { fetchImpl, calls } = recordingFetch(() => ({ status: 200, body: backendSnapshots.history }));
    const client = createApiClient({ baseUrl: '/api/v1', fetchImpl });
    await client.history({ stationId: 'cata_12720', start: '2024-06-01T00:00', end: '2024-06-01T04:00' });
    const url = calls[0]?.url ?? '';
    expect(url).toContain('/api/v1/water-level/history?');
    expect(url).toContain('station_id=cata_12720');
    expect(url).toContain('start=2024-06-01T00%3A00%3A00%2B08%3A00');
    expect(url).toContain('end=2024-06-01T04%3A00%3A00%2B08%3A00');
  });

  it('智能体对话提交到 /api/v1/agent/chat', async () => {
    const { fetchImpl, calls } = recordingFetch(() => ({ status: 200, body: backendSnapshots.agent }));
    const client = createApiClient({ baseUrl: '/api/v1', fetchImpl });
    await client.chat({ message: '当前是否达到研究性高水位' });
    expect(calls[0]?.url).toContain('/api/v1/agent/chat');
    expect(calls[0]?.init?.method).toBe('POST');
  });

  it('风险请求缺少观测证据时返回 unavailable 且不发请求', async () => {
    const { fetchImpl, calls } = recordingFetch(() => ({ status: 200, body: backendSnapshots.risk }));
    const client = createApiClient({ baseUrl: '/api/v1', fetchImpl });
    const response = await client.risk({ station_id: 'cata_12720' } as never);
    expect(calls).toHaveLength(0);
    expect(response.status).toBe('unavailable');
    expect(response.warnings.join()).toContain('INVALID_RISK_REQUEST');
  });

  it('超时中断映射为 error', async () => {
    const fetchImpl = vi.fn(
      (_url: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
        }),
    ) as unknown as typeof fetch;
    const client = createApiClient({ baseUrl: '/api/v1', fetchImpl, timeoutMs: 5 });
    const response = await client.health();
    expect(response.status).toBe('error');
    expect(response.warnings).toContain('REQUEST_TIMEOUT');
  });

  it('健康响应状态来自外层响应', async () => {
    const client = createApiClient({ baseUrl: '/api/v1', fetchImpl: jsonFetch(backendSnapshots.health) });
    const response = await client.health();
    expect(response.status).toBe('ok');
    expect(response.data?.sample_available).toBe(true);
    expect(response.data?.persistence_fallback_available).toBe(true);
    expect(response.data?.external_provider_available).toBe(false);
  });

  it('unavailable 响应对象可直接作为降级结果使用', () => {
    const response = unavailableResponse('req-1');
    expect(response.status).toBe('unavailable');
    expect(response.data).toBeNull();
  });
});
