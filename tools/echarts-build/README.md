# ECharts 裁剪版构建

`web/vendor/echarts.slim.js` 不是官方发行包，是用本目录的 `entry.js` 现打的裁剪版。

| 版本 | 原始 | gzip |
| --- | --- | --- |
| 官方 `echarts.min.js` 5.6.1（整包） | 1034 KB | 327 KB |
| 本目录打出的 `echarts.slim.js` | 508 KB | 170 KB |

省下的 157 KB 是桌面端首屏（手机端根本不会加载这个文件，见 `web/index.html` 的 `loadEcharts`）。

## 重新构建

```bash
cd tools/echarts-build
npm install          # echarts 5.6.0 + esbuild 0.24.0，只有这两个包
npm run build        # 直接覆盖 ../../web/vendor/echarts.slim.js
```

## 什么时候必须重建

- 升级 echarts 版本（改 `package.json` 里的 `echarts` 后重跑 `npm install && npm run build`）
- 页面里新加了图表类型或组件。当前只注册了：
  折线图 + 直角坐标系 + tooltip + markLine + axisPointer + legend + title + canvas 渲染器。
  一旦用了柱状图、饼图、dataZoom、visualMap、SVG 渲染器，**必须**先在 `entry.js` 里
  `import` 并 `echarts.use([...])`，否则运行时静默不渲染（控制台会提示 component not exists）。

## 为什么不用整包

本站只有三处画图：指数分时、个股 K 线（双网格 + 均价 markLine）、分时折线。
整包里 80% 的模块（地图、关系图、树图、3D、数据集、变换器…）一个都用不到。

## 回滚

需要从官方发行包（`echarts@5.6.1` 的 `dist/echarts.min.js`）取回原文件，
放到 `web/vendor/echarts.min.js`，再把 `web/index.html` 里的 `ECHARTS_SRC`
改回 `vendor/echarts.min.js`。仓库不附带整包文件。
