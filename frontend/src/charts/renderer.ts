import * as echarts from 'echarts/core';
import { LineChart } from 'echarts/charts';
import {
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TitleComponent,
  TooltipComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { EChartsOption } from 'echarts';

// 只注册折线图与所需组件，避免把整个 ECharts 打进产物。
echarts.use([
  LineChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  MarkLineComponent,
  CanvasRenderer,
]);

export interface ChartHandle {
  setOption(option: EChartsOption): void;
  resize(): void;
  dispose(): void;
}

/**
 * 初始化 ECharts 实例。
 *
 * 单独成模块，便于测试环境替换；初始化失败由调用方降级为不可用状态。
 */
export function initChart(element: HTMLElement): ChartHandle {
  const chart = echarts.init(element);
  return {
    setOption: (option: EChartsOption) => chart.setOption(option, true),
    resize: () => chart.resize(),
    dispose: () => chart.dispose(),
  };
}
