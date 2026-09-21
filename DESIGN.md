---
name: 庆事通
description: 以依据、路径和状态为核心的校园事务服务工作台
colors:
  ink: "#17262b"
  muted: "#637276"
  quiet: "#899596"
  teal: "#0f766e"
  teal-soft: "#d9eee9"
  paper: "#ffffff"
  wash: "#f4f6f3"
  wash-deep: "#e9eeeb"
  line: "#d8dfdb"
  line-strong: "#c5d0ca"
  amber: "#a66a18"
  amber-soft: "#fff0d3"
  red: "#b74f3f"
typography:
  display:
    fontFamily: "Aptos, Source Han Sans SC, Noto Sans SC, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "3rem"
    fontWeight: 500
    lineHeight: 1.06
    letterSpacing: "-0.045em"
  title:
    fontFamily: "Aptos, Source Han Sans SC, Noto Sans SC, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "1.6rem"
    fontWeight: 600
    lineHeight: 1.16
    letterSpacing: "-0.03em"
  body:
    fontFamily: "Aptos, Source Han Sans SC, Noto Sans SC, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.75
  label:
    fontFamily: "Aptos, Source Han Sans SC, Noto Sans SC, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "10px"
    fontWeight: 750
    lineHeight: 1.5
rounded:
  sm: "4px"
  md: "5px"
  lg: "8px"
spacing:
  xs: "7px"
  sm: "12px"
  md: "16px"
  lg: "24px"
  xl: "32px"
  section: "58px"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    rounded: "{rounded.md}"
    padding: "0 13px"
    height: "38px"
  button-secondary:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "0 13px"
    height: "38px"
  input-search:
    backgroundColor: "{colors.wash}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "5px 6px 5px 14px"
    height: "52px"
  state-chip:
    backgroundColor: "{colors.wash-deep}"
    textColor: "{colors.muted}"
    rounded: "{rounded.sm}"
    padding: "0 8px"
---

# Design System: 庆事通

## Overview

**Creative North Star: “校园事务登记台”**

庆事通把校园事务当作一张需要被准确登记、核对和推进的工作单。视觉语言借鉴纸面登记台与流程谱：清晰的编号、细边界线、纵向步骤和少量状态色，让学生在高频查找和填写时可以快速扫描，不需要先理解一套装饰性的界面隐喻。

整体气质是克制、可靠、安静而不冷漠。白纸色承载内容，石墨色负责阅读，青绿色只标记可继续的路径和已确认状态，琥珀色只提醒边界与待核验事项。首页直接进入查询工作台，不使用营销式 Hero、巨大装饰标题或堆叠式展示卡片。

**Key Characteristics:**
- 依据、步骤、任务状态使用同一条清晰的秩序线索。
- 以分隔线和留白建立层级，默认不依赖阴影。
- 学生端只展示业务语言和下一步，不暴露工程追踪字段。
- 所有页面在移动端折叠为稳定的单列办理路径。

## Colors

这是一个低饱和的纸张与石墨系统，青绿色是唯一主行动色，琥珀色和红色只用于风险与错误。

### Primary
- **登记青绿** (#0f766e): 用于主导航当前项、继续操作、可用状态和依据新鲜度。

### Secondary
- **边界琥珀** (#a66a18): 用于服务边界、待核验和需要补充的提醒。
- **校正红** (#b74f3f): 只用于字段错误、不可继续和需要重试的状态。

### Neutral
- **石墨墨色** (#17262b): 标题、正文和主要操作。
- **纸张白** (#ffffff): 主要内容表面和工具面板。
- **雾面底色** (#f4f6f3): 页面背景、输入框和次级区域。
- **分隔线** (#d8dfdb): 内容分组与列表秩序。

### Named Rules
**The Rare Accent Rule.** 青绿色只出现在当前路径、继续动作和明确的业务状态，不用来装饰每个区域。

## Typography

**Display Font:** Aptos with Source Han Sans SC, Noto Sans SC, PingFang SC and Microsoft YaHei fallbacks
**Body Font:** 同一套系统无衬线字体
**Label Font:** 同一套字体的 10px 高字重标签

**Character:** 字形保持现代、紧凑和易读，中文不依赖特殊字体文件也能保持稳定。标题有明确层级但不做海报式夸张，正文保留足够行距支撑长说明。

### Hierarchy
- **Display** (500, 3rem, 1.06): 首页和办理页的主要任务标题。
- **Title** (600, 1.6rem, 1.16): 页面分区和事项标题。
- **Body** (400, 13px, 1.75): 事项说明、依据和边界文本。
- **Label** (750, 10px, 1.5): 状态、编号、来源和辅助说明。

## Layout

桌面端使用最大 1220px 的内容宽度，首页是主内容加窄提示栏的工作台结构；办理页是办理路径、主表单和上下文提示的三段关系。内容区间距以 12、16、24、32 和 58px 为主，列表用细分隔线保持密度而不是用卡片堆叠。

移动端隐藏次要顶部导航，将入口切换保留在紧凑工具栏中；首页提示栏下移为单列，回答和依据上下排列，办理路径改为两列步骤网格，表单字段单列。字号采用固定层级，不用视口宽度缩放文字。

## Elevation & Depth

系统默认采用平面分层。纸张白、雾面底色和分隔线负责深度，只有庆小通提示和交互状态使用非常克制的柔和阴影。没有渐变背景或装饰性玻璃效果；动效只表达查询进行、回答出现、焦点和可继续路径。

### Named Rules
**The Flat Workbench Rule.** 内容静止时依靠颜色层和边界线建立层级，阴影只服务于可移动提示，不用于制造卡片海洋。

## Shapes

控件使用 4–8px 的轻微圆角，保持清晰的纸面边角；按钮、输入框、标签和工具面板各自使用同一组形状。状态标签可以是紧凑的小胶囊或短矩形，但不把大段内容包成圆角胶囊。表单错误通过红色边框和说明文字同时表达。

## Components

### Buttons
- **Shape:** 轻微圆角（5px），高度 38px。
- **Primary:** 石墨背景、白色文字；悬停切换为登记青绿。
- **Secondary / Quiet:** 白底细边框或透明背景；用于返回、取消和低风险动作。
- **Focus:** 使用青绿色可见焦点环，键盘操作不依赖颜色差异。

### Chips
- **Style:** 雾面底色、4px 圆角、10px 标签文字。
- **State:** 已确认使用青绿色底色，待核验使用琥珀底色，错误使用红色底色。

### Cards / Containers
- **Corner Style:** 工具面板 8px，材料条目 5px。
- **Background:** 纸张白用于主要内容，雾面底色用于次级上下文。
- **Shadow Strategy:** 默认无阴影；庆小通提示使用低强度阴影。
- **Border:** 1px 分隔线优先于阴影。

### Inputs / Fields
- **Style:** 雾面底色、1px 分隔线、4px 圆角，保持足够的 42px 控件高度。
- **Focus:** 变为纸张白并使用青绿色边界。
- **Error / Disabled:** 错误使用红色边界和恢复说明；禁用状态降低对比度但保留可读文字。

### Navigation
- **Style:** 浅色服务栏，当前项用底部 2px 青绿色线标记；移动端优先保留入口切换和品牌，不挤压内容。

### Process Rail

办理路径使用带编号的纵向步骤谱。已完成、当前和未开始三种状态同时通过颜色、边框和文字表达，学生可以在任何办理页知道自己处在哪一步。

## Do's and Don'ts

### Do:
- **Do** 让查询、回答、依据、材料和状态共享同一套青绿色状态语义。
- **Do** 使用边界线、编号和留白帮助学生扫描长内容。
- **Do** 保持所有关键状态有文字，不让图标成为唯一信息。
- **Do** 让移动端的主要操作保持稳定尺寸和单列顺序。

### Don't:
- **Don't** 恢复深色大导航、营销式 Hero 或红色超大标题。
- **Don't** 用工程 ID、Trace、Publication 或 Provider 信息占用学生端内容。
- **Don't** 用大量同尺寸卡片、渐变、装饰性光晕或无意义的页面加载动画制造层次。
- **Don't** 把模拟边界藏起来，也不要把办理状态写成真实学校系统已经接收。
