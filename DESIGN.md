---
name: 知识运营中枢
description: 以“轨道任务台账”为北极星的可信企业知识运营控制台。
colors:
  ink: "#13233f"
  ink-soft: "#52627a"
  rail: "#f8fbff"
  rail-soft: "#edf4ff"
  canvas: "#f3f7ff"
  surface: "#ffffff"
  border: "#dce6f4"
  border-strong: "#bfd0e6"
  muted: "#667892"
  action: "#1769e8"
  action-deep: "#0f56c6"
  action-hover: "#0f56c6"
  signal: "#0b836f"
  signal-pale: "#e8f7f2"
  warning: "#a66b0b"
  warning-pale: "#fff5dc"
  danger: "#b63243"
  danger-pale: "#fff0f2"
  focus: "#2f7af8"
typography:
  display:
    fontFamily: '"Noto Sans SC Display", "Noto Sans SC", "Microsoft YaHei", sans-serif'
    fontSize: "clamp(28px, 3vw, 38px)"
    fontWeight: 760
    lineHeight: 1.16
    letterSpacing: "-0.03em"
  headline:
    fontFamily: '"Noto Sans SC Display", "Noto Sans SC", "Microsoft YaHei", sans-serif'
    fontSize: "17px"
    fontWeight: 760
    letterSpacing: "-0.02em"
  body:
    fontFamily: '"Noto Sans SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif'
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.55
  label:
    fontFamily: '"Noto Sans SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif'
    fontSize: "11px"
    fontWeight: 700
  micro-label:
    fontFamily: '"Noto Sans SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif'
    fontSize: "9px"
    fontWeight: 700
    letterSpacing: "0.1em"
  numeric-display:
    fontFamily: '"Noto Sans SC Display", "Noto Sans SC", "Microsoft YaHei", sans-serif'
    fontSize: "34px"
    fontWeight: 760
    letterSpacing: "-0.03em"
rounded:
  compact: "7px"
  icon: "8px"
  control: "9px"
  field: "10px"
  card: "11px"
  brand: "12px"
  panel: "14px"
  pill: "999px"
spacing:
  xxs: "5px"
  xs: "8px"
  sm: "10px"
  md: "14px"
  lg: "18px"
  xl: "22px"
  "2xl": "24px"
components:
  button-primary:
    backgroundColor: "{colors.action}"
    textColor: "{colors.surface}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "9px 14px"
    height: "40px"
  button-primary-hover:
    backgroundColor: "{colors.action-hover}"
    textColor: "{colors.surface}"
    rounded: "{rounded.control}"
  button-quiet:
    backgroundColor: "{colors.surface}"
    textColor: "#23495f"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "9px 14px"
    height: "40px"
  input-text:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "10px 12px"
    height: "42px"
  nav-item:
    backgroundColor: "{colors.rail}"
    textColor: "#40536d"
    typography: "14px / 560"
    rounded: "12px"
    padding: "8px 10px"
    height: "46px"
  nav-item-active:
    backgroundColor: "{colors.action}"
    textColor: "{colors.surface}"
    rounded: "{rounded.control}"
  panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.panel}"
    padding: "22px"
  status-chip:
    backgroundColor: "#e9f4f7"
    textColor: "#24677e"
    typography: "{typography.micro-label}"
    rounded: "{rounded.pill}"
    padding: "4px 8px"
    height: "25px"
---

# Design System: 知识运营中枢

## Overview

**Creative North Star: "轨道任务台账"**

这套系统把企业知识运营呈现为一条可追踪的工作轨道：浅蓝白导航轨承载长期方位，冰蓝画布与纯白工作面承载当天任务，蓝灰边界负责把资料、状态与操作组织成可快速扫描的台账。科技感来自精确对齐、连续状态信号和可靠的数据层级，而不是霓虹、渐变或装饰性图表。

界面处于高频 Operate 模式，密度紧凑但不拥挤。主标题建立明确页级定位，任务入口、表格、表单和审核结果按职责分组；蓝色只承担主要动作，青绿色表示在线或已生效，琥珀色表示等待，深红色表示失败或破坏性风险。状态始终同时提供文字，确保颜色不是唯一线索。

**Key Characteristics:**

- 浅蓝白导航轨、冰蓝画布与纯白工作面形成明快而稳定的三区层级。
- 细蓝灰边界、14px 面板圆角和紧凑控件构成克制的企业控制台秩序。
- 自托管展示字用于页标题与面板标题，正文保持高可读系统字体栈。
- 线性 SVG 图标、等宽数字与显式状态文本强化可扫描性和可信感。
- 桌面固定轨道在窄屏转为横向任务轨，内容与表单顺序降为单列。

## Colors

调色板以高明度蓝白和深蓝墨色为骨架，用钴蓝、信号青绿、警示琥珀与风险深红建立单义状态语义。

### Primary

- **行动钴蓝**（#1769e8）：唯一的主操作色，用于主要按钮、当前主导航、文件选择提示与关键链接。
- **深行动蓝**（#0f56c6）：行动蓝的悬停和高对比层级。

### Secondary

- **信号青绿**（#0b836f）：表示在线、已生效和可信链路；其浅底色（#e8f7f2）承载低强度状态背景。
- **警示琥珀**（#a66b0b）：表示待发布、待复核和需要注意；其浅底色（#fff5dc）只用于警示容器和状态徽标。

### Tertiary

- **风险深红**（#b63243）：表示失败、删除与不可恢复风险；其浅底色（#fff0f2）用于危险状态背景，不承担普通强调。
- **焦点蓝**（#2f7af8）：用于 2px 键盘焦点轮廓，以区别于点击和状态色。

### Neutral

- **控制台墨色**（#13233f）：正文与主要信息。
- **柔化墨色**（#52627a）：次级说明和辅助内容。
- **浅蓝白轨道**（#f8fbff）与**轨道悬停层**（#edf4ff）：侧栏与导航层级。
- **冰蓝画布**（#f3f7ff）与**纯白表面**（#ffffff）：页面底与工作面板。
- **蓝灰边界**（#dce6f4）与**强调边界**（#bfd0e6）：默认分隔和需要更强结构的分隔。
- **静音蓝灰**（#667892）：说明、占位与低优先级信息。

### Named Rules

**The Signal Reserve Rule.** 行动蓝只表示可执行主动作，青绿色只表示在线或已生效，琥珀色只表示等待或注意，深红色只表示失败或破坏性风险；任何状态都必须同时显示文字。

**The No Decorative Gradient Rule.** 表面使用纯色和边界建立层级，不使用渐变、霓虹发光或装饰性数据色带。

## Typography

**Display Font:** Noto Sans SC Display（后备为 Noto Sans SC、Microsoft YaHei、sans-serif；项目自托管 `NotoSansSC-Display.woff2`，覆盖 500–900 字重）  
**Body Font:** Noto Sans SC（后备为 PingFang SC、Microsoft YaHei、system-ui、sans-serif）  
**Label/Mono Font:** 标签沿用正文字体栈；数字依靠 `font-variant-numeric: tabular-nums` 保持列对齐。

**Character:** 展示字承担黑蓝、高对比、稍紧缩的控制台标题；正文与控件保持中性、紧凑和连续阅读友好。中英文标签不混用装饰字体，英文字段通过大写和字距建立微层级。

### Hierarchy

- **Display**（760，clamp(28px, 3vw, 38px)，1.16，-0.03em）：页级标题；手机端固定为 28px。
- **Headline**（760，17px，-0.02em）：面板标题和表单段标题。
- **Numeric Display**（760，34px，-0.03em）：指标数值；使用等宽数字，手机端为 29px。
- **Body**（400，14px，1.55）：页面正文与长说明；引导段落不超过 70ch。
- **Label**（700，11px）：按钮、字段标签和指标标题。
- **Micro Label**（700，9px，0.1em）：元数据、状态代码与控制台标记；仅短标签可使用大写。

### Named Rules

**The Display Ownership Rule.** 自托管展示字只用于页标题、面板标题与关键数字；正文、表单和导航使用正文字体栈，避免整页视觉过重。

**The Numeric Ledger Rule.** 指标、时间和表格数字使用等宽数字特性，不使用额外等宽字体破坏中文界面的统一性。

## Layout

桌面采用固定左轨与流式工作区：导航轨宽 268px，工作区左边距与其相等，横向内边距为 `clamp(24px, 3.2vw, 52px)`，顶栏高 70px。总览指标为四等分连续面板，任务区使用 `1.35fr / 0.65fr` 两栏和 18px 间距；标准面板内边距为 22px，页面段落从顶栏后 34px 开始。

在 1180px 以下，导航轨缩为 232px，工作区左右内边距缩为 26px，任务区降为单列。管理台以 1024px 为最小桌面宽度，不提供手机端布局；低于该宽度时保持桌面结构并允许浏览器横向查看，避免出现未经维护的导航重排。

**The Desktop Rail Rule.** 管理台始终保留稳定左轨，不转换为手机横向导航，也不把主导航压缩成无标签图标列。

**The Task-Order Rule.** 响应式重排保持“确认状态 → 选择任务 → 执行操作 → 查看结果”的 DOM 顺序，不因视觉换列改变键盘和屏幕阅读器顺序。

## Elevation & Depth

系统采用“扁平为默认、响应才抬升”的混合深度。普通面板、指标、卡片和导航都依靠纯色、1px 边界与相邻色差分层，静止时无阴影；主要按钮悬停、选中录入标签、Toast 与模态对话框才使用阴影，表示即时反馈或脱离文档流的临时层。

### Shadow Vocabulary

- **Action Lift**（`0 7px 18px rgba(23, 107, 206, 0.2)`）：仅用于主要按钮悬停。
- **Selected Tab**（`0 3px 10px rgba(28, 66, 90, 0.1)`）：仅用于当前录入方式标签。
- **Transient Overlay**（`0 18px 50px rgba(17, 43, 62, 0.16)`）：Toast 等短暂浮层。
- **Modal Depth**（`0 28px 90px rgba(7, 25, 39, 0.32)`）：模态对话框；同时配合 `rgba(5, 19, 30, 0.62)` 遮罩。

### Named Rules

**The Flat-by-Default Rule.** 工作面板和数据容器静止时不使用阴影；阴影只表示交互响应、选中状态或脱离页面流的浮层。

## Shapes

形态语言以轻微圆角和细边界为主：按钮与输入为 9px，侧栏主导航为 12px，上传区和辅助容器为 10px，卡片为 11px，品牌标记为 12px，主面板和模态框为 14px。状态芯片使用胶囊形，链路编号与服务状态点使用圆形；表格与连续指标保持共享外轮廓，不把每个单元格做成独立浮卡。

边界默认 1px，使用蓝灰边界色；上传区使用 1px 虚线，焦点使用独立的 2px 外轮廓。图标统一为 1.75px 圆头圆角线性 SVG，常规尺寸 20px，内联图标 15px，禁止图标字体和装饰性 Emoji。

**The Radius Hierarchy Rule.** 控件小于容器、容器小于主面板；不得把所有元素统一成同一个大圆角，也不得使用超大胶囊按钮替代普通操作按钮。

## Components

### Buttons

- **Shape:** 紧凑矩形按钮（9px 圆角），默认最小高 40px，内边距 9px 14px，11px/750 标签字重。
- **Primary:** 行动蓝底与纯白文字，1px 同色边界；悬停变为行动悬停色，并使用 Action Lift 阴影。
- **Hover / Focus:** 颜色与阴影过渡为 160ms ease；键盘焦点始终显示 2px 焦点青轮廓和 2px 偏移。系统启用减少动态时，过渡时长降至 0.01ms。
- **Secondary / Quiet:** 纯白底、蓝灰边界与深蓝文字；悬停变为冷蓝灰浅底，不使用阴影。
- **Text:** 透明背景的行动蓝文字，保持足够命中区；适合“查看记录”等次级跳转。

### Chips

- **Style:** 状态代码为 25px 高、9px 加粗微标签和胶囊轮廓；默认使用冷蓝浅底与蓝青文字。
- **State:** 已生效使用青绿语义，待发布或待复核使用琥珀语义，失败或删除使用深红语义；每个芯片都保留可读文字。

### Cards / Containers

- 产品列表按“选择、产品信息、价格、件数、资料、操作”六列排列；价格、件数、资料三列等宽、均匀分布，表头与行内容共用列宽且在各自单元格内水平居中。操作表头与按钮组水平居中；价格、件数、资料和按钮在产品行内垂直居中，产品说明保持左对齐。件数表示手动维护的当前库存，未填写显示“未登记”，零显示“0 件”，使用等宽数字便于扫描。在较窄桌面维持表头和行对齐，表格内部允许横向滚动。

- **Corner Style:** 主面板 14px，列表卡片 11px，内部辅助容器 10px。
- **Background:** 主工作面为纯白，页面画布为冰蓝；回答链路与测试指南使用浅蓝强调面，与全局高明度配色保持一致。
- **Shadow Strategy:** 默认无阴影，使用 1px 蓝灰边界；悬停卡片只改变边界与表面色。
- **Internal Padding:** 主面板 22px，表格面板 20px，列表卡片 14px 15px；手机端主面板统一为 16px。

### Inputs / Fields

- **Style:** 42px 最小高，10px 12px 内边距，1px 蓝灰边界，9px 圆角，纯白底和 12px 输入字。
- **Focus:** 边界变为 `#2b91c5`，同时显示 `0 0 0 3px rgba(43, 145, 197, 0.12)` 焦点晕圈；键盘导航仍保留全局 2px 焦点青轮廓。
- **Error / Disabled:** 破坏性确认使用深红语义；禁用按钮保持结构但透明度为 0.5，并显示不可用光标。

### Navigation

- **Desktop:** 268px 浅蓝白轨道，主项使用 14px 字号、46px 最小高、12px 圆角；图标置于 30px 浅蓝圆角底中。激活项使用钴蓝表面和白色文字。
- **Submenu:** 左缩进 40px，不使用外层底框或树状竖线；项目使用 13px 字号、32px 最小高和 8px 圆角。当前项以淡蓝平面、钴蓝文字和 5px 定位圆点表示，不使用阴影或描边制造嵌套卡片；空数量徽标不显示。
- **Desktop only:** 左轨在支持的桌面宽度始终保留完整分组、图标和文字。

### Mission Tiles

任务入口是 2×2 的连续网格单元，每项最小高 92px、内边距 17px，以共享 1px 边界构成台账而非四张浮卡。悬停仅将背景变为冷青浅色，并把文字变为深行动蓝；右侧使用 17px 线性箭头。620px 以下改为单列，保持任务顺序。

### Signal Path

回答链路使用浅蓝强调面和 29px 白底圆形编号，从“接收”到“交付”以细线串联。编号为 9px 加粗等宽数字，钴蓝只用于步骤定位，青绿色只表示在线或成功；末端原则提示始终显示“无可靠资料时不生成业务结论”。

### Dialogs

模态框使用 14px 圆角、纯白表面、Modal Depth 阴影和无模糊深色遮罩。标题栏与产品编辑操作栏可粘滞，确保关闭与保存始终可达；关闭按钮必须带可访问名称并使用内联 SVG。

## Do's and Don'ts

### Do:

- **Do** 保持“浅蓝白轨道 + 冰蓝画布 + 纯白工作面 + 细蓝灰边界”的高明度结构。
- **Do** 用行动蓝、信号青绿、警示琥珀和风险深红表达单义状态，并同时给出文字标签。
- **Do** 在页标题、面板标题与关键数字使用自托管展示字，在正文与表单使用高可读系统字体栈。
- **Do** 使用内联线性 SVG、可见的 `:focus-visible` 焦点和语义化控件。
- **Do** 在 1024px 及以上保持完整桌面任务轨，并在较窄桌面宽度保留清晰的滚动边界。

### Don't:

- **Don't** 使用霓虹、渐变、玻璃拟态或装饰性数据可视化代替清晰的状态和任务层级。
- **Don't** 让颜色成为状态的唯一线索，也不要复用深红色表达普通强调。
- **Don't** 给静止面板、指标或列表卡片添加投影；阴影只属于交互响应和临时浮层。
- **Don't** 使用图标字体、Emoji 或不一致的实心图标替代 1.75px 线性 SVG。
- **Don't** 用大面积留白降低高频操作密度，或在移动端打乱状态、任务、操作、结果的阅读顺序。
