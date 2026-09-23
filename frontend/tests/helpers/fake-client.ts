import type { ApiClient } from '@/api/client';
import { viewResponses } from './backend-snapshots';

/**
 * 构造假 API 客户端：默认返回“真实后端快照 + 适配层”得到的展示模型响应，
 * 可通过 overrides 覆盖为不可用或错误以验证降级分支。
 */
export function createFakeClient(overrides: Partial<ApiClient> = {}): ApiClient {
  return {
    baseUrl: '/api/v1',
    health: () => Promise.resolve(viewResponses.health),
    history: () => Promise.resolve(viewResponses.history),
    forecast: () => Promise.resolve(viewResponses.forecast),
    comparison: () => Promise.resolve(viewResponses.comparison),
    risk: () => Promise.resolve(viewResponses.risk),
    chat: () => Promise.resolve(viewResponses.agent),
    brief: () => Promise.resolve(viewResponses.brief),
    ...overrides,
  };
}
