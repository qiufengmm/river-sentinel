import { describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import AgentView from '@/views/AgentView.vue';
import DashboardView from '@/views/DashboardView.vue';
import ForecastView from '@/views/ForecastView.vue';
import HistoryView from '@/views/HistoryView.vue';
import { DashboardStoreKey, createDashboardStore } from '@/stores/dashboard';
import type { ApiClient } from '@/api/client';
import type { HistoryData } from '@/types/api';
import { DISCLAIMER, RESEARCH_THRESHOLD_LABEL } from '@/types/view';
import { createFakeClient } from './helpers/fake-client';
import {
  errorResponse,
  staleHistoryResponse,
  unavailableResponse,
  viewResponses,
} from './helpers/backend-snapshots';

vi.mock('@/charts/renderer', () => ({
  initChart: () => ({ setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() }),
}));

function mountView(component: unknown, client: ApiClient) {
  const store = createDashboardStore(client);
  return mount(component as never, {
    global: {
      provide: { [DashboardStoreKey as unknown as symbol]: store },
    },
  });
}

describe('总览视图', () => {
  it('接口不可用时不展示替代数值', async () => {
    const client = createFakeClient({
      health: () => Promise.resolve(unavailableResponse<never>()),
      history: () => Promise.resolve(unavailableResponse<never>()),
      risk: () => Promise.resolve(unavailableResponse<never>()),
    });
    const wrapper = mountView(DashboardView, client);
    await flushPromises();
    const text = wrapper.text();
    expect(text).toContain('后端或数据不可用');
    expect(text).toContain('HTTP_503');
    expect(text).toContain('未提供');
    expect(text).toContain('数据模式未知');
    expect(text).toContain(DISCLAIMER);
    expect(text).not.toContain('3.74');
  });

  it('成功时展示数据模式、观测时间、模型版本和风险卡片', async () => {
    const wrapper = mountView(DashboardView, createFakeClient());
    await flushPromises();
    const text = wrapper.text();
    expect(text).toContain('历史回放');
    expect(text).toContain('2026-01-01');
    expect(text).toContain('ds5b-hourly-baseline-1');
    expect(text).toContain(RESEARCH_THRESHOLD_LABEL);
    expect(text).toContain('3.74');
    expect(text).toContain(DISCLAIMER);
  });
});

describe('历史回放视图', () => {
  it('明确标注历史回放并展示真实回放数据', async () => {
    const wrapper = mountView(HistoryView, createFakeClient());
    await flushPromises();
    const text = wrapper.text();
    expect(text).toContain('历史回放');
    expect(text).toContain('返回点数：10');
    expect(text).toContain('3.74 m');
    expect(text).toContain(DISCLAIMER);
  });

  it('陈旧快照显示本地快照与数据陈旧', async () => {
    const wrapper = mountView(
      HistoryView,
      createFakeClient({ history: () => Promise.resolve(staleHistoryResponse()) }),
    );
    await flushPromises();
    const text = wrapper.text();
    expect(text).toContain('本地快照');
    expect(text).toContain('数据陈旧');
  });

  it('空数据仍可渲染且给出空状态', async () => {
    const emptyHistory = {
      ...viewResponses.history,
      data: { ...(viewResponses.history.data as HistoryData), points: [] },
    };
    const wrapper = mountView(HistoryView, createFakeClient({ history: () => Promise.resolve(emptyHistory) }));
    await flushPromises();
    expect(wrapper.text()).toContain('该时间范围内没有历史水位点');
    expect(wrapper.text()).toContain(DISCLAIMER);
  });
});

describe('预测与模型对比视图', () => {
  it('展示事件级指标无法计算而不是 F1=0', async () => {
    const wrapper = mountView(ForecastView, createFakeClient());
    await flushPromises();
    const text = wrapper.text();
    expect(text).toContain('事件级指标无法计算');
    expect(text).toContain('no_observed_events');
    expect(text).not.toMatch(/F1\s*0(\.0+)?\b/);
    expect(text).toContain(DISCLAIMER);
  });

  it('预测结果展示 target_at 与水位', async () => {
    const wrapper = mountView(ForecastView, createFakeClient());
    await flushPromises();
    await wrapper.findAll('.filters__actions button')[0]?.trigger('click');
    await flushPromises();
    const text = wrapper.text();
    expect(text).toContain('3.71');
    expect(text).toContain('persistence');
  });

  it('降级结果展示回退原因与基线模型', async () => {
    const wrapper = mountView(
      ForecastView,
      createFakeClient({ forecast: () => Promise.resolve(viewResponses.forecastDegraded) }),
    );
    await flushPromises();
    await wrapper.findAll('.filters__actions button')[0]?.trigger('click');
    await flushPromises();
    const text = wrapper.text();
    expect(text).toContain('降级结果');
    expect(text).toContain('model_artifact_unavailable');
    expect(text).toContain('persistence');
  });

  it('预测接口失败时保持页面可用', async () => {
    const wrapper = mountView(
      ForecastView,
      createFakeClient({
        forecast: () => Promise.resolve(errorResponse<never>()),
        comparison: () => Promise.resolve(unavailableResponse<never>()),
        risk: () => Promise.resolve(errorResponse<never>()),
      }),
    );
    await flushPromises();
    await wrapper.findAll('.filters__actions button')[0]?.trigger('click');
    await flushPromises();
    const text = wrapper.text();
    expect(text).toContain('请求失败');
    expect(text).toContain(DISCLAIMER);
  });
});

describe('智能体对话视图', () => {
  it('回答保留工具摘要、证据、模型版本和风险依据', async () => {
    const wrapper = mountView(AgentView, createFakeClient());
    await flushPromises();
    await wrapper.findAll('.presets button')[0]?.trigger('click');
    await flushPromises();
    const text = wrapper.text();
    expect(text).toContain('forecast_water_level');
    expect(text).toContain('evaluate_research_risk');
    expect(text).toContain('ds5b-hourly-baseline-1');
    expect(text).toContain('历史回放');
    expect(text).toContain(RESEARCH_THRESHOLD_LABEL);
    expect(text).toContain(DISCLAIMER);
  });

  it('后端失败时展示明确失败状态，不补造回答', async () => {
    const wrapper = mountView(
      AgentView,
      createFakeClient({ chat: () => Promise.resolve(unavailableResponse<never>('req-agent-503')) }),
    );
    await wrapper.find('textarea').setValue('当前水位是多少');
    await wrapper.find('form').trigger('submit');
    await flushPromises();
    const text = wrapper.text();
    expect(text).toContain('智能体服务不可用');
    expect(text).toContain('HTTP_503');
    expect(text).not.toContain('3.74');
  });

  it('未发送问题时显示空状态', async () => {
    const wrapper = mountView(AgentView, createFakeClient());
    await flushPromises();
    expect(wrapper.text()).toContain('暂无回答');
  });
});
