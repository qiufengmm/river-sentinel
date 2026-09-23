import { describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import DataModeBanner from '@/components/DataModeBanner.vue';
import MetricTable from '@/components/MetricTable.vue';
import RiskCard from '@/components/RiskCard.vue';
import UnavailableState from '@/components/UnavailableState.vue';
import WaterLevelChart from '@/components/WaterLevelChart.vue';
import { adaptComparison, adaptHistory, selectEventMetrics } from '@/api/adapters';
import { buildWaterLevelOption, toObservedPairs } from '@/charts/waterLevelOption';
import { DISCLAIMER, NON_OFFICIAL_LABEL, RESEARCH_THRESHOLD_LABEL } from '@/types/view';
import { backendSnapshots, cloneSnapshot, staleHistoryResponse, viewResponses } from './helpers/backend-snapshots';

vi.mock('@/charts/renderer', () => ({
  initChart: () => ({
    setOption: vi.fn(),
    resize: vi.fn(),
    dispose: vi.fn(),
  }),
}));

/** 事件级指标行：显式选择 segment=test / horizon=1 / 全部模型。 */
const eventRows = selectEventMetrics(viewResponses.comparison.data?.event_rows ?? [], {
  segment: 'test',
  horizon: 1,
  models: null,
});

describe('UnavailableState', () => {
  it('展示不可用状态而不是补造数值', () => {
    const wrapper = mount(UnavailableState, {
      props: { title: 'XGBoost 模型结果不可用', status: 'unavailable', reason: 'model_artifact_unavailable' },
    });
    expect(wrapper.text()).toContain('XGBoost 模型结果不可用');
    expect(wrapper.text()).toContain('model_artifact_unavailable');
    expect(wrapper.text()).toContain(DISCLAIMER);
    expect(wrapper.text()).not.toContain('0.000');
  });

  it('接口失败与空数据使用各自文案', () => {
    const error = mount(UnavailableState, { props: { status: 'error' } });
    expect(error.text()).toContain('接口请求失败');
    const empty = mount(UnavailableState, { props: { status: 'empty' } });
    expect(empty.text()).toContain('暂无数据');
  });
});

describe('DataModeBanner', () => {
  it('标注历史回放、观测时间、新鲜度和免责声明', () => {
    const wrapper = mount(DataModeBanner, {
      props: { evidence: viewResponses.history.evidence, warnings: [] },
    });
    const text = wrapper.text();
    expect(text).toContain('历史回放');
    expect(text).toContain('2026-01-01');
    expect(text).toContain(DISCLAIMER);
    expect(text).toContain('非实时监测');
  });

  it('陈旧快照显示数据陈旧标记', () => {
    const stale = staleHistoryResponse();
    const wrapper = mount(DataModeBanner, {
      props: { evidence: stale.evidence, warnings: ['STALE_SNAPSHOT'] },
    });
    expect(wrapper.text()).toContain('本地快照');
    expect(wrapper.text()).toContain('数据陈旧');
    expect(wrapper.text()).toContain('STALE_SNAPSHOT');
  });

  it('无证据时不声称任何数据模式', () => {
    const wrapper = mount(DataModeBanner, { props: { evidence: [], warnings: [] } });
    expect(wrapper.text()).toContain('数据模式未知');
    expect(wrapper.text()).not.toContain('实时监测');
  });
});

describe('RiskCard', () => {
  it('展示研究性阈值、依据摘要和免责声明，不出现 [object Object]', () => {
    const wrapper = mount(RiskCard, { props: { verdict: viewResponses.risk.data, status: 'ok' } });
    const text = wrapper.text();
    expect(text).toContain(RESEARCH_THRESHOLD_LABEL);
    expect(text).toContain('7.18');
    expect(text).toContain(NON_OFFICIAL_LABEL);
    expect(text).toContain(DISCLAIMER);
    expect(text).toContain('不表示官方预警或超警信息');
    expect(text).toContain('deterministic_rule');
    expect(text).not.toContain('[object Object]');
  });

  it('展示最后观测时间、锚点与触发时域', () => {
    const wrapper = mount(RiskCard, { props: { verdict: viewResponses.risk.data, status: 'ok' } });
    const text = wrapper.text();
    expect(text).toContain('数据最后观测');
    expect(text).toContain('预测锚点');
    expect(text).toContain('触发时域');
    expect(text).toContain('缺失不参评时域');
  });

  it('无风险结果时显示不可用与跳过原因', () => {
    const wrapper = mount(RiskCard, {
      props: { verdict: null, status: 'unavailable', skippedReason: '缺少观测水位或观测时间，未发送风险判定请求' },
    });
    expect(wrapper.text()).toContain('风险判定不可用');
    expect(wrapper.text()).toContain('缺少观测水位或观测时间');
    expect(wrapper.text()).not.toContain(RESEARCH_THRESHOLD_LABEL);
  });
});

describe('MetricTable', () => {
  it('展示后端 metrics 行与版本、样本数', () => {
    const wrapper = mount(MetricTable, {
      props: {
        rows: viewResponses.comparison.data?.rows ?? [],
        eventRows,
        selectionLabel: 'segment=test，时域=h1，模型=全部模型',
        limitations: viewResponses.comparison.data?.limitations ?? null,
      },
    });
    const text = wrapper.text();
    expect(text).toContain('持久性基线');
    expect(text).toContain('ds6-e1');
    expect(text).toContain('segment=test');
  });

  it('test 段零事件时显示事件级指标无法计算，不渲染 F1=0', () => {
    const wrapper = mount(MetricTable, {
      props: { rows: viewResponses.comparison.data?.rows ?? [], eventRows },
    });
    const text = wrapper.text();
    expect(text).toContain('事件级指标无法计算');
    expect(text).toContain('no_observed_events');
    expect(text).not.toMatch(/F1\s*0(\.0+)?\b/);
  });

  it('不可用模型显示不可用/未提供并排在末尾', () => {
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
    const wrapper = mount(MetricTable, { props: { rows, eventRows } });
    const text = wrapper.text();
    expect(text).toContain('不可用/未提供');
    expect(text).toContain('valid_samples_insufficient');
    const tableRows = wrapper.findAll('tbody tr');
    expect(tableRows[tableRows.length - 1]?.text()).toContain('LSTM E1');
  });

  it('空指标表显示空状态', () => {
    const wrapper = mount(MetricTable, { props: { rows: [] } });
    expect(wrapper.text()).toContain('暂无模型指标');
  });
});

describe('WaterLevelChart', () => {
  it('缺失水位保留为 null 断点，不转换为 0', () => {
    const snapshot = cloneSnapshot(backendSnapshots.history);
    if (snapshot.data) snapshot.data.points[2]!.water_level_m = null;
    const points = adaptHistory(snapshot.data)?.points ?? [];
    const pairs = toObservedPairs(points);
    expect(pairs).toHaveLength(10);
    expect(pairs[2]?.[1]).toBeNull();
    expect(pairs.some((pair) => pair[1] === 0)).toBe(false);
  });

  it('option 保留断点且阈值线使用研究性标签', () => {
    const snapshot = cloneSnapshot(backendSnapshots.history);
    if (snapshot.data) snapshot.data.points[2]!.water_level_m = null;
    const option = buildWaterLevelOption({
      observed: adaptHistory(snapshot.data)?.points ?? [],
      threshold: 7.18,
    });
    const series = option.series as { data: [number, number | null][]; connectNulls: boolean }[];
    expect(series[0]?.connectNulls).toBe(false);
    expect(series[0]?.data[2]?.[1]).toBeNull();
    expect(JSON.stringify(option)).toContain(RESEARCH_THRESHOLD_LABEL);
    expect(JSON.stringify(option)).not.toContain('官方警戒水位');
  });

  it('不可用预测不生成预测序列', () => {
    const option = buildWaterLevelOption({
      observed: viewResponses.history.data?.points ?? [],
      predictions: [{ horizon: 1, target_at: '2026-01-01T12:00:00+08:00', water_level_m: null, source: 'lstm' }],
    });
    const series = option.series as { name: string }[];
    expect(series.map((item) => item.name)).not.toContain('预测 · lstm');
  });

  it('有效预测生成带 target_at 的序列', () => {
    const option = buildWaterLevelOption({
      observed: viewResponses.history.data?.points ?? [],
      predictions: viewResponses.forecast.data?.predictions ?? [],
    });
    const series = option.series as { name: string; data: [string, number][] }[];
    const forecastSeries = series.find((item) => item.name === '预测 · persistence');
    expect(forecastSeries?.data[0]?.[1]).toBe(3.71);
  });

  it('空数据时不绘制曲线，显示空状态', () => {
    const wrapper = mount(WaterLevelChart, { props: { observed: [], predictions: [] } });
    expect(wrapper.text()).toContain('暂无可绘制的点位');
  });

  it('有效数据渲染图表容器', () => {
    const wrapper = mount(WaterLevelChart, {
      props: { observed: viewResponses.history.data?.points ?? [], threshold: 7.18 },
    });
    expect(wrapper.find('.chart__canvas').exists()).toBe(true);
    expect(wrapper.text()).not.toContain('暂无可绘制的点位');
  });
});
