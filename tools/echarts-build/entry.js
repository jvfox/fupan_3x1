/* ECharts 裁剪版入口：只注册本站真正用到的模块。
   增删图表类型时先改这里，再 `npm run build`（见 README.md）。 */
import * as echarts from 'echarts/core';
import { LineChart } from 'echarts/charts';
import {
  GridComponent,       // 直角坐标系（折线/分时要用；本站不再画 K 线）
  TooltipComponent,    // 悬浮提示
  MarkLineComponent,   // 均价线、昨收基准线
  AxisPointerComponent,// 十字光标 + 双图联动 axisPointer.link
  LegendComponent,     // 目前没用到，留着是为了以后加多序列图例
  TitleComponent,      // 同上
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

echarts.use([
  LineChart,
  GridComponent,
  TooltipComponent,
  MarkLineComponent,
  AxisPointerComponent,
  LegendComponent,
  TitleComponent,
  CanvasRenderer,
]);

/* 前端用的是全局 `echarts`（loadEcharts 里判断 window.echarts），
   esbuild 打成 IIFE 没有 global-name，所以这里显式挂到 window。 */
window.echarts = echarts;
