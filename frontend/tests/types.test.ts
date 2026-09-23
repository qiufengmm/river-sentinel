import { describe, expect, it } from 'vitest';
import type { ApiResponse, EventMetricRow, MetricRow } from '@/types/api';
import {
  DISCLAIMER,
  EVENT_METRICS_UNAVAILABLE,
  NON_OFFICIAL_LABEL,
  RESEARCH_THRESHOLD_LABEL,
  dataModeLabel,
  eventMetricsText,
  eventSelectionLabel,
  formatMetric,
  formatNumber,
  formatTime,
  isRiskRenderable,
  riskLevelLabel,
  sortMetricRows,
} from '@/types/view';
import { viewResponses } from './helpers/backend-snapshots';

function eventRow(overrides: Partial<EventMetricRow> = {}): EventMetricRow {
  return {
    segment: 'test',
    horizon: 1,
    model: 'persistence',
    display_name: 'persistence',
    precision: null,
    recall: null,
    f1: null,
    tp: 0,
    fp: 0,
    fn: 0,
    observed_event_points: 0,
    observed_event_segments: 0,
    predicted_event_points: 0,
    predicted_event_segments: 0,
    reason: 'no_observed_events',
    computable: false,
    ...overrides,
  };
}

function metricRow(overrides: Partial<MetricRow> = {}): MetricRow {
  return {
    segment: 'test',
    horizon: 1,
    model: 'persistence',
    display_name: 'persistence',
    model_version: 'single_level_input',
    sample_count: 720,
    mae: null,
    rmse: null,
    r2: null,
    nse: null,
    reason: null,
    supported: false,
    ...overrides,
  };
}

describe('统一响应类型契约', () => {
  it('接受 degraded 且 data 为 null 的响应', () => {
    const response: ApiResponse<MetricRow[]> = {
      request_id: 'r1',
      status: 'degraded',
      data: null,
      evidence: [],
      warnings: ['model_artifact_unavailable'],
    };
    expect(response.status).toBe('degraded');
    expect(response.data).toBeNull();
  });

  it('四种状态均可构造，不依赖后端实现', () => {
    const statuses: ApiResponse<null>['status'][] = ['ok', 'degraded', 'unavailable', 'error'];
    expect(statuses).toHaveLength(4);
  });
});

describe('展示层文案与格式化', () => {
  it('缺失指标显示为不可用/未提供，不显示为 0', () => {
    expect(formatMetric(null)).toBe('不可用/未提供');
    expect(formatMetric(undefined)).toBe('不可用/未提供');
    expect(formatMetric(0.082)).toBe('0.082');
  });

  it('basis 内数值去掉多余 0，缺失显示未提供', () => {
    expect(formatNumber(7.18)).toBe('7.18');
    expect(formatNumber(3)).toBe('3');
    expect(formatNumber(null)).toBe('未提供');
  });

  it('test 段零事件时事件指标显示无法计算', () => {
    const result = eventMetricsText(eventRow());
    expect(result.computable).toBe(false);
    expect(result.text).toBe(EVENT_METRICS_UNAVAILABLE);
    expect(result.reason).toBe('no_observed_events');
  });

  it('事件指标可选时展示精确率、召回率与 F1', () => {
    const result = eventMetricsText(eventRow({ computable: true, precision: 0.8, recall: 0.5, f1: 0.61 }));
    expect(result.computable).toBe(true);
    expect(result.text).toContain('F1 0.610');
  });

  it('事件指标选择条件可读，避免隐式取第一个嵌套格', () => {
    expect(eventSelectionLabel({ segment: 'test', horizon: 1, models: ['persistence'] })).toBe(
      'segment=test，时域=h1，模型=persistence',
    );
    expect(eventSelectionLabel({ segment: 'test', horizon: null, models: null })).toContain('全部模型');
  });

  it('数据模式标签只使用历史回放或本地快照', () => {
    expect(dataModeLabel('history_replay')).toBe('历史回放');
    expect(dataModeLabel('local_snapshot')).toBe('本地快照');
    expect(dataModeLabel(null)).not.toContain('实时');
  });

  it('风险等级标签不出现官方预警措辞', () => {
    expect(riskLevelLabel('elevated')).toBe('达到或超过研究性高水位');
    expect(riskLevelLabel('elevated')).not.toContain('官方');
    expect(riskLevelLabel(null)).toBe('风险等级未提供');
  });

  it('时间格式化对非法输入原样返回', () => {
    expect(formatTime(null)).toBe('未提供');
    expect(formatTime('not-a-time')).toBe('not-a-time');
    expect(formatTime('2026-01-01T11:00:00+08:00')).toContain('2026-01-01');
  });

  it('固定免责与研究性阈值文案存在', () => {
    expect(DISCLAIMER).toBe('教学科研辅助，不替代官方防汛决策');
    expect(RESEARCH_THRESHOLD_LABEL).toBe('研究性高水位阈值（训练集 p90，7.18 m）');
    expect(NON_OFFICIAL_LABEL).toBe('非官方警戒标准');
    expect(EVENT_METRICS_UNAVAILABLE).toBe('事件级指标无法计算');
  });
});

describe('风险卡片可展示性', () => {
  it('后端没有 computable 字段，有 level 即可展示', () => {
    expect(isRiskRenderable(viewResponses.risk.data)).toBe(true);
    expect(isRiskRenderable(null)).toBe(false);
    expect(isRiskRenderable(undefined)).toBe(false);
  });
});

describe('指标表排序', () => {
  const rows: MetricRow[] = [
    metricRow({ model: 'lstm', display_name: 'lstm', reason: 'valid_samples_insufficient' }),
    metricRow({ model: 'persistence', mae: 0.146, rmse: 0.205, r2: 0.742, nse: 0.731, supported: true }),
    metricRow({ model: 'xgboost', mae: 0.082, rmse: 0.121, r2: 0.913, nse: 0.907, supported: true }),
  ];

  it('可用模型按 MAE 升序，不可用模型固定在末尾', () => {
    const sorted = sortMetricRows(rows);
    expect(sorted.map((row) => row.model)).toEqual(['xgboost', 'persistence', 'lstm']);
  });

  it('不可用模型的空指标不参与排序比较', () => {
    const sorted = sortMetricRows(rows);
    const last = sorted[sorted.length - 1];
    expect(last?.supported).toBe(false);
    expect(last?.mae).toBeNull();
  });
});
