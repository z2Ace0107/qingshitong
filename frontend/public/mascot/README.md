# 吉祥物资产包

庆事通的吉祥物是体验层的状态提示器。业务层通过 `window.QSTMascot.setState(state, textEquivalent)` 传入有限状态，资产包通过 `manifest.json` 描述素材和动作；它不拥有工具、业务写入或个人数据权限。

## 默认资产

`manifest.json` 当前使用项目生成的原创 PNG 静态资产作为 R0 原型。替换为自己的合法资产时，保持清单入口和状态名不变即可：

```text
web/mascot/
  manifest.json
  assets/
    mascot-companion.png
    mascot-default.svg  （兼容保留）
```

## 支持的清单入口

- `renderer: "svg"`：单张 SVG/静态图，适合默认回退或减少动态效果。
- `renderer: "raster"`：单张 PNG/WebP 静态图，适合 R0 的陪伴式原型资产。
- `renderer: "frame-sequence"`：在 `states.<state>.frames` 中列出同源 PNG/WebP/SVG 序列帧，并设置 `fps`。
- `renderer: "sprite-sheet"`：通过 `assets.atlas` 指定同源 PNG/WebP 图集，在 `frames` 中登记每一帧的 `x/y/width/height`，状态只引用帧 ID。

所有素材路径必须是 `/mascot/` 下的同源相对路径；manifest 不执行脚本、不注册工具，也不接受 URL 参数或令牌。Live2D/Spine 暂不作为启用的渲染器，等运行时许可证、供应链、包大小和浏览器性能完成核验后再增加独立适配器。

自定义资产必须记录来源类型、许可证/授权、署名和内容指纹，并保留静态帧回退。动画缺失、清单损坏、素材加载失败或用户启用 `prefers-reduced-motion` 时，界面仍显示文字等价物，不能影响事项卡、证据、表单或模拟任务。
