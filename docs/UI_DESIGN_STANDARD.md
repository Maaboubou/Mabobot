# Mabobot 控制台 UI 设计标准

版本 v1.0 · 最后更新 2026-09-12
适用范围：`web/` 目录下的全部界面 —— `web/index.html`、`web/assets/css/`、以及 `web/assets/js/` 渲染出的所有 DOM。

这份文档是控制台界面的唯一权威标准。新增功能、改造旧界面、评审改动都以它为准。

---

## 1. 与其他文档的关系

| 文档 | 角色 | 冲突时 |
| --- | --- | --- |
| `docs/UI_DESIGN_STANDARD.md`（本文） | 控制台 UI 的唯一标准：令牌、组件、交互、文案 | **以本文为准** |
| `docs/WEB_CONSOLE_ARCHITECTURE.md` | 信息架构与路由契约：页面划分、配置层级、兼容与移除规则 | 架构问题以它为准 |
| `web/assets/css/style.css` 顶部 `:root` | 令牌的唯一代码实现 | 代码与本文不一致时，改代码；若是有意变更，同步改本文 |
| `tests/browser/`（快照 + 活性探针） | 样式决策的证据：104 视图计算样式快照、逐规则活性判定、截图与溢出检查 | 改样式的结论以它的输出为准（见第 15 章） |

> ⚠️ 视觉溯源的原始分析 `tmp/DESIGN.md` 位于被 `.gitignore` 忽略的临时目录，2026-09-12 已随临时目录清理丢失。本文档即控制台实际采用的固化版本；样式改动的验证证据改由 `tests/browser/` 的快照与活性探针产出，不再依赖任何临时目录文件。

**为什么需要这份文档：** 控制台已经积累了 7300+ 行 CSS、12000+ 行 JS。缺少统一标准的结果是同一件事有多种写法 —— 目前代码里存在 95 个不同的 `font-size` 取值、200 处硬编码的圆角（对比 56 处使用令牌）、两套 `escapeHtml` 实现、13 种不同的媒体查询断点。这些都是"新功能和原有设计格格不入"的直接原因。

---

## 2. 设计原则

新增任何界面前，先确认它符合以下 8 条。它们是评审时的判断依据。

1. **先范围，后内容。**
   同一个能力有两套配置（全局默认 / 单个聊天）时，界面必须先说清"我现在改的是哪一层"，再展示字段。顺序颠倒会让用户不知道自己在改什么。

2. **一个开关回答一个问题。**
   一个布尔量对应一个开关，文案就是这个问题的答案。禁止用一个开关同时表达"是否覆盖"和"覆盖成什么"。

3. **显式状态优于隐式派生。**
   模式（如双语 / 三语）显式列出让用户选，而不是从其它字段反推。用户看到的选项就是系统支持的选项。

4. **保存即生效，差异即覆盖。**
   设置改完直接保存到它所属的层，不需要"先在 A 页改、再到 B 页保存"。只提交被改动的字段；未动过的字段继续跟随上层默认。

5. **默认安静，操作明确。**
   界面默认是安静的、信息优先的。强调色（coral）稀缺，一个视图只服务一个主操作。

6. **沿用既有系统，不新增第四种表面。**
   cream 画布 + coral 主操作 + 深色产品面 是既定三件套。不要引入紫色卡片、绿色区块或新的灰阶，也不要在组件里写十六进制颜色。

7. **页面是持续表面，不是卡片堆。**
   目录、工作台、运行时中心这类连续信息流用一整块表面承载；只有真正可点击、可进入的对象才做成卡片。

8. **危险操作要付出代价，日常操作不要。**
   删除、重启、停止服务必须二次确认并说明后果；日常保存、切换、刷新不弹确认框。

---

## 3. 设计令牌

令牌定义在 `web/assets/css/style.css` 顶部的 `:root` 中，暗色覆盖在同文件的 `:root[data-theme='dark']`。

**铁律：组件样式只能引用第 3.1 节的语义别名。** 第 3.2 节的"原始调色板"变量只允许在 `:root` 及其暗色覆盖块内出现。

### 3.1 语义别名（组件唯一可用集合）

| 分组 | 令牌 | 说明 |
| --- | --- | --- |
| 页面底色 | `--bg-body` | 页面最底层画布 |
| | `--bg-card` | 卡片 / 面板默认底 |
| | `--bg-soft` | 次级底：输入框内的分组、提示条、行内区块 |
| | `--bg-subtle` | 比 soft 再深一档：hover、选中行、chip 底 |
| | `--bg-strong` | 最重的浅色台阶：分隔填充、进度轨道 |
| 侧栏 | `--bg-sidebar` `--bg-sidebar-hover` `--bg-sidebar-active` `--sidebar-text` `--sidebar-muted` `--sidebar-border` `--sidebar-control-bg` `--sidebar-active-text` | 主导航专用 |
| 日志终端 | `--log-surface` `--log-surface-raised` `--log-surface-sunken` `--log-hairline` `--log-on-surface` `--log-on-surface-strong` `--log-on-surface-soft` `--log-ln` `--log-time` `--log-info` `--log-warning` `--log-module` `--log-hover` `--log-line-error` `--log-line-error-strong` `--log-line-warn` `--log-line-warn-strong` `--log-line-critical` `--log-match-bg` `--log-match-ring` `--log-match-color` `--log-current-ring` `--log-float-bg` `--log-float-ring` `--log-float-shadow` | 日志页专用；浅色／暗色各定义一次，组件区只引用令牌（见 6.14） |
| 文字 | `--text-main` | 标题、强调值、当前生效值 |
| | `--text-body` | 正文 |
| | `--text-secondary` | 次要说明、字段说明、未选中的 tab |
| | `--text-soft` | 最弱：占位、计数、时间戳 |
| | `--text-on-primary` | 主色块上的文字 |
| | `--text-on-dark` `--text-muted-on-dark` | 深色表面上的文字与次级文字 |
| 边框 | `--border-color` | 默认 1px 发丝线 |
| | `--border-soft` | 更淡：同区块内分隔 |
| | `--border-strong` | 更重：需要被看见的边界、虚线框 |
| 主色 | `--primary` `--primary-hover` `--primary-active` `--primary-disabled` | 文本 / 图标 / 边框形态用 coral |
| | `--action-primary` `--action-primary-hover` | **实心填充**形态用这组（暗色下自动降饱和） |
| | `--action-disabled-bg` `--action-disabled-text` | 禁用态填充 |
| 主色淡底 | `--accent-soft` `--accent-strong` | 选中底 / 选中边，由 `color-mix` 派生，自动适配主题 |
| 语义色 | `--success` `--warning` `--danger` `--info` | 状态点、图标、强调文本 |
| | `--success-soft` `--warning-soft` `--danger-soft` `--info-soft` | 提示条与状态徽章的底 |
| 圆角 | `--radius-xs` `--radius-sm` `--radius-md` `--radius-lg` | 见 3.4 |
| 阴影 | `--shadow-sm` `--shadow-md` `--shadow-lg` | 见 3.5 |
| 字体 | `--font-display` `--font-body` `--font-code` | 见 3.6 |
| 动效 | `--transition-base` | 颜色类过渡统一用它 |

### 3.2 原始调色板（禁止在组件中直接引用）

唯一来源，值固定：

| 类别 | 令牌 | 值 |
| --- | --- | --- |
| 主色 | `--primary` | `#cc785c` |
| | `--primary-active` | `#a9583e` |
| | `--primary-disabled` | `#e6dfd8` |
| 墨色 | `--ink` | `#141413` |
| | `--body-strong` | `#252523` |
| | `--body` | `#3d3d3a` |
| | `--muted` | `#6c6a64` |
| | `--muted-soft` | `#8e8b82` |
| 发丝线 | `--hairline` | `#e6dfd8` |
| | `--hairline-soft` | `#ebe6df` |
| 浅表面 | `--canvas` | `#faf9f5` |
| | `--surface-soft` | `#f5f0e8` |
| | `--surface-card` | `#efe9de` |
| | `--surface-cream-strong` | `#e8e0d2` |
| 深表面 | `--surface-dark` | `#181715` |
| | `--surface-dark-soft` | `#1f1e1b` |
| | `--surface-dark-elevated` | `#252320` |
| 表层文字 | `--on-primary` | `#ffffff` |
| | `--on-dark` | `#faf9f5` |
| | `--on-dark-soft` | `#a09d96` |
| 强调 | `--accent-teal` | `#5db8a6` |
| | `--accent-amber` | `#e8a55a` |
| 状态 | `--success` | `#5db872` |
| | `--warning` | `#d4a017` |
| | `--danger` | `#c64545` |

因为 `--bg-body`、`--text-main` 这类别名在暗色主题下会被整体重映射，而 `--canvas`、`--ink` 不会。**在组件里写 `var(--canvas)` 等于写死浅色主题。**

唯一的例外是**刻意固定深色的区域**：日志终端配色、Codex 深色产品面、二维码白底、以及"亮色语义按钮上的深色文字"（如 `.btn-success { color: var(--ink) }`）。这些地方的主题无关性是有意为之，允许直接使用原始调色板，但必须满足两条：同一区块内有说明性注释，且该区块不随 `data-theme` 切换表面颜色。

### 3.3 透明度与派生色

不新增十六进制值。需要透明度时只有两种合法写法：

```css
/* 1) 用 RGB 伴生变量（只读，不新增） */
box-shadow: 0 0 0 3px rgba(var(--primary-rgb), 0.15);

/* 2) 用 color-mix 从既有令牌派生（新派生色一律用这个） */
background: color-mix(in srgb, var(--success) 14%, var(--canvas));
```

RGB 伴生变量是只读的：`--primary-rgb`、`--primary-active-rgb`、`--canvas-rgb`、`--ink-rgb`、`--body-rgb`、`--muted-rgb`、`--on-primary-rgb`、`--on-dark-rgb`、`--on-dark-soft-rgb`、`--surface-dark-elevated-rgb`、`--success-rgb`、`--warning-rgb`、`--danger-rgb`、`--info-rgb`、`--surface-current-rgb`。**不要新增 `--xxx-rgb`**，需要新色请用 `color-mix`。

### 3.4 圆角

| 令牌 | 值 | 用途 |
| --- | --- | --- |
| `--radius-xs` | 4px | 徽章、微型标签、行内 chip |
| `--radius-sm` | 8px | 按钮、输入框、下拉、下拉项 |
| `--radius-md` | 12px | 分组容器、提示条、摘要条 |
| `--radius-lg` | 16px | 卡片、面板、模态框内容区 |
| `999px` | — | 药丸：筛选标签、状态徽章、开关轨道（允许字面量） |
| `50%` | — | 圆形：头像、图标按钮（允许字面量） |

除 `999px` 与 `50%` 外，**不允许出现其它字面量圆角**。当前代码里有 `9px` `10px` `11px` `14px` `18px` 等 200 处硬编码，属于待收敛的历史遗留，新代码不得再增加。

### 3.5 阴影与层次

层次的表达顺序是 **色块优先，阴影克制**：

1. 换底色（`--bg-body` → `--bg-card` → `--bg-soft`）—— 首选
2. 加 1px 发丝线边框（`--border-color`）
3. 加阴影 —— 只在浮层上使用

| 令牌 | 用途 |
| --- | --- |
| `--shadow-sm` | 卡片静止态 |
| `--shadow-md` | hover 抬升、sticky 工具栏 |
| `--shadow-lg` | 模态框、抽屉、浮层 |

大多数卡片 hover 时**只换边框色**（`--border-color` → `--border-strong` 或 `rgba(var(--primary-rgb), .22)`），不要位移、不要放大。列表卡片禁止 `transform: translateY()` 之类会改变点击热区的效果。

### 3.6 字体

```css
--font-display: "Cormorant Garamond", "Noto Serif SC", "Songti SC", STSong, serif;
--font-body:    "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
--font-code:    "JetBrains Mono", ui-monospace, monospace;
```

- **衬线体只用于页面标题和区块大标题**（`h1`/`h2`/`h3`、`.page-title`），字重 400，字距 `-0.02em`。绝不用于正文、按钮、表单标签、数值。
- **无衬线体是控件与正文的默认字体**，字重 400（正文）/ 500（标签、按钮、强调短语）/ 600–650（小节标题，谨慎使用）。
- **等宽体用于**：代码、配置键、内部 ID、路径、模型名、日志正文、要原样复制的文本。提示词编辑框在聊天上下文中用正文字体（可读性优先），在全局配置调试场景中可用等宽体。

### 3.7 字号阶梯

目标是收敛到 6 档。**新代码只能使用这 6 个值**（历史值按"就近归并"迁移）：

| 档位 | 值 | 用途 |
| --- | --- | --- |
| meta-xs | 10px | 密集数据面的次要信息：Codex 控制台、日志流、数据表单元格里的时间戳 / ID / 状态副文案 |
| meta | 11px | 徽章文字、计数、极小说明 |
| label | 12px | 字段标签、chip、辅助说明、`.form-text` |
| control | 13px | **表单控件、按钮、列表项、正文默认值** |
| title-sm | 15px | 卡片标题、小节标题、值强调 |
| title | 18px | 模态框标题、区块标题 |
| display | `clamp(1.3rem, 1.12rem + 0.3vw, 1.55rem)` | 页面标题（`.page-title`），全文只允许一处定义 |

- 10px 是**下限**不是常规值：`meta-xs` 只服务于"同一屏要放下几十行结构化数据"的密集数据面，普通页面最低用 `meta` 11px。**任何情况下不得再出现 8px、9px、9.9px 这类字号**（历史上有 177 处，已在 2026-09-12 归并）。
- 数值型大数字（KPI、用量统计）用 **20 / 24 / 28 / 32px**，且必须配小号标签（12px）。例：`.metric-value` 32px + `.metric-label` 15px。
- 页面标题只有 `.page-title` 一处，字号取 display 档的 clamp 表达式；**不要在媒体查询里再改它的 `font-size`**，clamp 已经处理了缩放。
- 聊天配置区已经令牌化为 `--chat-font-title: 15px` / `--chat-font-control: 13px` / `--chat-font-label: 12px` / `--chat-font-meta: 11px`，与该阶梯一致；同一界面内复用这组令牌，不要另起名字。

**写法统一用 `px`**（10/11/12/13/15/18），不要混用 `0.68rem`、`.62rem` 这类换算值 —— 它们让"这个字号和那个字号是不是同一个"变得无法一眼判断。

**图标尺寸是另一套阶梯**（图标不是文字，不参与文本层级）：

| 档位 | 值 | 用途 |
| --- | --- | --- |
| icon-sm | 14px | 行内图标、按钮图标、输入框前置图标 |
| icon | 16px | 导航图标、列表项图标、工具条图标 |
| icon-lg | 20px | 卡片主图标、区块内空态图标 |
| icon-xl | 24px | 区块级空态图标 |
| icon-2xl | 32px | 页面级空态图标 |
| icon-hero | 64px | 纯装饰性大图标（水印式，如 `.metric-card .icon-wrapper`） |

状态点、色块一类纯几何尺寸（6px 以下）不受此表约束。

**归并规则（就近取值）**：`< 10.5 → 10`、`10.5–11.4 → 11`、`11.5–12.4 → 12`、`12.5–13.9 → 13`、`14.0–16.4 → 15`、`16.5–19.9 → 18`；图标同样就近取 14/16/20/24/32/64。归并时必须把 `rem` 换算值一并改写成 `px`。

同一区域内**最多使用 3 档字号**。分区标题 + 字段标签 + 字段值 就是三档，再加就是层级失控。

### 3.8 间距

基准单位 **4px**。常用值为 `4 / 8 / 12 / 16 / 24 / 32`。

- 卡片内边距：16px（紧凑）或 24px（宽松），同一页保持一致。
- 同一分区的字段之间：12px；不同分区之间：16–24px。
- 表单行的标签与控件之间：4–6px。
- 页面区块底部间距：12–16px；模态框内容区顶部：16px。

### 3.9 动效

- 颜色 / 边框 / 背景过渡统一使用 `var(--transition-base)`（180ms ease）。
- 位移与淡入动画控制在 200ms 内，只用于出现的一次，不做循环装饰。
- 唯一的循环动效例外是**状态指示**（如概览实时脉搏的呼吸圆点）：它表达"系统仍在运行"的持续状态而非装饰，必须同时给出静态等价物（静默时圆点静止、文案照常说明状态），并在 `prefers-reduced-motion: reduce` 下关闭动画。
- **必须尊重 `prefers-reduced-motion`**：元素定位统一调用 `UI.scrollIntoView(element, options)`，在 reduce 时降级为 `behavior: 'auto'`；直接调用 `scrollTo` 的滚动动画也必须遵循该偏好。

---

## 4. 主题（浅色 / 暗色）

1. 两套主题都是一等公民。**任何新增界面必须同时在两种主题下检查**。
2. 只用语义别名，暗色主题即自动生效；这是唯一正确的适配方式，不要写 `:root[data-theme='dark'] .my-xxx { ... }` 补丁块。只有当语义别名无法表达时才允许追加暗色覆盖，且必须写在同一组件的样式旁边并注明原因。**自带配色的区域（日志终端、Codex 深色产品面等）要用组件令牌组表达**：在 `:root` 与 `:root[data-theme='dark']` 各定义一次（如 `--log-*`），组件区只引用令牌 —— 日志页原先两段共 157 行补丁（暗色覆盖 + 浅色反补丁）已于 2026-09-12 按此方式合并。
3. 主题选择在样式加载前由 `index.html` 头部内联脚本从 `mabobot.colorTheme` 恢复，同时写入 `data-theme` 与 Bootstrap 的 `data-bs-theme`，并更新 `meta[name="theme-color"]`。新增依赖主题的第三方组件时，必须同时适配这两个属性。
4. `color-scheme` 由主题块声明，原生控件（滚动条、日期选择器）跟随主题，不要自行覆盖。
5. 对比度：正文文字与其背景至少 4.5:1；大号标题至少 3:1。`--text-soft` 只用于非关键信息，不要承载用户必须读到内容。

---

## 5. 页面骨架与命名

### 5.1 骨架层级

```text
body
└── .wrapper
    ├── nav.sidebar#sidebarNavigation          主导航（含主题开关、版本号）
    └── main.main-content
        ├── header.page-header                 ← 全站唯一，包含 .page-title 与 .page-global-actions
        └── div#<tabId>.tab-content            ← 每个标签页一个根
            └── div.<domain>-shell             ← 页面外壳
                ├── .<domain>-toolbar          ← 工具栏：搜索、筛选、主操作
                ├── .<domain>-section          ← 分区（可多个）
                │   └── .<domain>-panel / -card
                └── .empty-state-panel         ← 空态
```

约定：

- `page-header` 与 `page-title` 是**全站唯一**的标题位，切标签页时改文字，不要在每个标签页里再写一个 `<h1>`。
- 每个标签页根节点使用 `class="tab-content d-none workspace-page"`（`dashboard` 除外，它不带 `d-none`）。
- 标签页内的分区切换用 `.workspace-section` + Bootstrap `tab-pane`，用 `console-subnav nav-pills` 做页内子导航。
- 需要粘性的工具栏加 `position: sticky; top: 0` 并配 `--shadow-md`、`z-index: 20`。

### 5.2 类名前缀

前缀 = 该界面所属的领域。**新组件必须落在已有前缀下，不新建同义前缀。**

| 标签页 / 路由 | 前缀 | 已有示例 |
| --- | --- | --- |
| `/` 概览 | `dashboard-` | `dashboard-shell` `dashboard-kpi` |
| `/codex` Codex 中心 | `codex-profile-` `codex-skill-` `codex-oauth-` | `codex-profile-card` |
| `/chats` 聊天 | `chat-` `chat-policy-` `chat-picker-` | `chat-policy-plugin` |
| `/assistant` 助手 | `assistant-` `roles-` | `assistant-chat-card` |
| `/plugins` 能力 | `capability-` `cap-` `automation-` | `capability-card` `cap-settings-shell` |
| `/ai` AI 资源 | `llm-` `model-` `usage-` | `llm-model-card` |
| `/operations` 运维 | `logs-` `system-` `archive-` | `logs-toolbar` |
| `/system` 系统 | `system-` `backup-` | `system-settings-nav` |

已知的历史漂移（`cap-` 与 `capability-` 并存、`chat-` 与 `users-` 混用）保留不动，但**不要新增第三套写法**。

BEM 风格不强制，但同一区块内建议保持 `块-元素` 与 `is-/has-` 状态类（`.is-custom` `.is-dirty` `.is-saving` `.is-empty` `.is-warning`）。状态类不加 `!important`，不靠内联 `style` 表达状态。

### 5.3 路由与历史

- 每个主视图有稳定 URL，支持刷新、深链、前进后退（清单见 `docs/WEB_CONSOLE_ARCHITECTURE.md`）。
- 编程式导航用 `window.history.pushState(...)` + `UI.switchTab(tab, { history: false })`，**不要直接改 `location`**。
- 菜单项用真实 `href`，并 `onclick="event.preventDefault()"` 后再 pushState。
- 上下文入口（如从概览跳到系统运维）必须落到同一个编辑器，禁止复制出第二份表单。

### 5.4 静态资源与缓存

- 页面只加载 3 个自有文件：`style.css`、`dashboard.css`、`ui.js`/`main.js` 等模块。
- 缓存串格式：`?v=YYYYMMDD-<slug>-<n>`（如 `?v=20260912-translation-settings-1`）；纯模块内部小改可沿用最小版本号风格。
- `tests/test_web_console_contract.py` 会断言部分缓存串的**字面值**。改 `web/index.html` 的缓存串时必须同步更新该测试，否则契约测试失败。

---

## 6. 组件规范

### 6.1 按钮层级

一个视图里只允许出现 **1 个实心主操作**。

| 层级 | 类 | 用途 | 示例 |
| --- | --- | --- | --- |
| 主操作 | `btn btn-primary` | 保存 / 创建 / 确认 | 「保存」 |
| 次操作 | `btn btn-secondary`、`btn btn-outline-secondary`、`btn btn-surface` | 取消、测试、次要动作 | 「取消」「试译」 |
| 三级 | `btn btn-sm btn-surface`、`btn btn-sm btn-quiet-accent` | 就地小动作、带 coral 图标的安静按钮 | 「查看日志」 |
| 文本操作 | `dashboard-text-action`、`translation-text-button` | 链接式动作，出现在标题右侧 | 「查看全部」 |
| 危险操作 | `btn-outline-danger`（次级）、`btn-danger`（最终确认） | 删除 / 停止 | 「删除 Profile」 |
| 图标按钮 | `btn btn-sm btn-outline-secondary codex-profile-icon-action` | 只有图标，必须有 `aria-label` + `title` | 编辑 / 刷新 |

规则：

- 按钮文案用**动词**，不用「确定 / 提交 / 是」。
- 尺寸：默认按钮 32–40px 高；表格行内与卡片角标用 `btn-sm`。
- 图标配文字时，图标在左（`<i class="bi bi-xxx me-1"></i>`）；只有图标时必须有 `aria-label`。
- 禁用态用 `disabled` 属性，样式由 `--action-disabled-bg/text` 统一接管，不要手写灰色。
- `btn-light` 仅用于需要与 cream 底区分的中性按钮，不要用它表达主操作。

### 6.2 表单

- 一行一个字段，标签在控件**上方**（`<label class="form-label">`），控件下方 4–6px 处放 `.form-text` 说明。
- 工具栏内的紧凑字段可以把标签放在左侧，形成"标签 + 控件"的横向行（`.chat-setting-row` 模式），但同一表单不要混用两种布局。
- 输入控件使用 Bootstrap 的 `.form-control` / `.form-select`，样式已统一（底 `--bg-card`、圆角 `--radius-sm`、聚焦 coral 边框 + `0 0 0 3px rgba(var(--primary-rgb), .15)` 光晕）。不要自造 `.xxx-input`。
- 密码与密钥字段：`type="password"` + 显示/隐藏按钮（`UI.togglePassword(id)`），`autocomplete="new-password"`，说明文案明确写"不会返回网页"。
- 敏感值展示：只显示"是否已配置"，不回显明文。
- 长文本（提示词、模板、JSON）：使用 `textarea.form-control`，`font-family: var(--font-code)`；可编辑时给出 `resize: vertical`；只读预览时使用等宽体且不给出光标。
- 校验：错误消息显示在字段下方（`<div class="form-text text-danger">` 或该模块的 `*-error` 类），不要用 `alert()`，也不要把整个表单弹红。
- 提交中：按钮进入 loading 态（见 6.9），表单控件整体 `disabled` 或 `fieldset:disabled`，`opacity: .65`。

### 6.3 开关

- 结构固定为：

```html
<div class="form-check form-switch modern-toggle">
    <input class="form-check-input" type="checkbox" role="switch" id="xxx" checked>
    <label class="form-check-label" for="xxx">启用某项能力</label>
</div>
```

- `.modern-toggle` 是控制台的开关外观（圆角药丸，选中为 `--success` 绿），不要使用原生裸 checkbox 表达开关语义。
- 标签文案写成**读出来就是答案的句子**（「跟随默认」「自动发送译文」），不要写「设置」「模式」「开关」。
- 开关的开启/关闭必须立即或明确地表达生效方式：能立即生效的就立即保存；需要点保存的在开关旁注明。
- 服务级开关（Bot 服务、运行中状态）使用 `service-power-switch` + `role="switch"` + `aria-checked`，其状态由后端真实状态驱动，不做乐观翻转。

### 6.4 卡片

卡片 = **可点击、可进入、可独立操作的对象**。以下情况不要用卡片：连续的信息流、纯展示的键值对、某个表单里的分组（用 `.section` 或 `fieldset`）。

- 结构：`.capability-card` / `.chat-policy-plugin` / `.codex-profile-card` 模式 —— `head`（图标 + 标题 + 状态徽章）/ `body`（1–3 行摘要）/ `foot`（操作按钮）。
- 卡片内边距 16px，圆角 `--radius-lg`，边框 `--border-color`，阴影 `--shadow-sm`。
- 卡片 hover 只换边框色，不做位移。
- 卡片内的操作按钮不超过 3 个，其余收进下拉菜单。
- 网格：`grid-template-columns: repeat(auto-fill, minmax(280px, 1fr))`，间距 12px。移动端自动降为单列。

### 6.5 徽章与 chip

状态徽章的标准形态：`padding: .06rem .4rem; border-radius: 999px; font-size: meta(11px); font-weight: 600;`，底 `--bg-subtle`、字 `--text-soft`；表示"已脱离默认"时用 `is-custom` 变体（底 `--accent-soft`、字 `--primary`）。

| 语义 | 颜色 | 文案示例 |
| --- | --- | --- |
| 中性 / 默认 | `--bg-subtle` + `--text-soft` | 「跟随默认」「默认设置」 |
| 已自定义 / 偏离默认 | `--accent-soft` + `--primary`（`.is-custom`） | 「本聊天已自定义」 |
| 成功 / 已生效 | `--success-soft` + `--success` | 「已启用」 |
| 警告 / 需注意 | `--warning-soft` + `--warning` | 「待同步」 |
| 失败 | `--danger-soft` + `--danger` | 「连接失败」 |

- **徽章只表达状态，不做按钮。** 需要点击的用 chip 按钮（`.capability-filter` 模式：药丸 + `--bg-card` 底 + 选中时 `--accent-soft` 底）。
- 禁止把徽章当装饰贴在每张卡片上 —— 只有"状态会变化、且用户需要知道"时才给徽章。

### 6.6 提示条

| 类型 | 用法 | 样式 |
| --- | --- | --- |
| 分区说明（常驻） | 解释这一屏在改什么、会影响谁 | 普通文字（`--text-secondary`，12–13px） |
| 信息提示 | 有副作用需要提前知道 | `.settings-info-alert` 模式：`rgba(var(--primary-rgb), .05)` 底 + 左侧 4px coral 边 |
| 成功 / 信息 / 失败（临时） | 操作结果 | `UI.showSuccess` / `UI.showInfo` / `UI.showError` 的 toast |
| 页面级错误（常驻） | 数据加载失败、需要用户处理的错误 | `<div class="alert alert-danger" role="alert">` |

- **信息提示条不超过 2 行。** 更长的解释写在字段的 `.form-text` 里，或收进折叠区。
- 提示条文案先说结论、再说原因，最后给动作（可选）。
- 不叠加：同一屏不要既给 toast 又在页面上放 alert 说同一件事。

### 6.7 空态

- 空态是**一句话 + 一个下一步**，不是插图展位。
- 结构：`div.<domain>-empty`（或 `.empty-state-panel`）—— 图标（可选，`--text-soft`）、一句说明、可选一个主操作按钮。
- 文案写"为什么是空的 + 怎么让它不空"，例如「尚未启用任何能力。到能力目录里挑一个开始。」不要只写「暂无数据」。
- 过滤/搜索导致的空结果必须与"真的没有数据"区分：前者写「没有匹配「xxx」的结果」并给「清除筛选」。

### 6.8 加载态

- 首次加载：`<div class="loading-wrapper">` + 转圈 + 正在做某事的说明（「正在读取配置与额度…」），不要只放转圈。
- 局部/后续加载：`.loading-wrapper.small`，或骨架屏（已有 `.assistant-loading-card` 模式）。
- 表格内的加载不要清空表头 —— 保留表头 + 一行占位。
- 加载态文案用**进行时**，并说明加载的对象。

### 6.9 异步操作的按钮状态

按钮需要在发送请求期间表达状态。标准三步：

1. `disabled` + 替换内容为 `<span class="spinner-border spinner-border-sm me-2"></span>正在保存…`
2. 成功：恢复原文案 + toast 成功 + 关闭模态框（如果是模态框内的操作）
3. 失败：恢复原文案 + `UI.showError`，**保留用户已填内容**，不要关闭模态框

- 请求期间防重复提交：先判断 `disabled` 再发请求。
- 乐观更新只允许用于"翻转一个开关"这种原子操作，并且失败时必须回滚到服务端的真实值。

### 6.10 模态框

- 结构固定：

```html
<div class="modal fade" id="xxxModal" tabindex="-1" aria-labelledby="xxxModalTitle" aria-hidden="true">
  <div class="modal-dialog">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title" id="xxxModalTitle">标题</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button>
      </div>
      <div class="modal-body">…</div>
      <div class="modal-footer">
        <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">取消</button>
        <button type="button" class="btn btn-primary" id="saveXxxButton">保存</button>
      </div>
    </div>
  </div>
</div>
```

- 尺寸：默认 `modal-dialog`；配置类等待时间较长的编辑器用 `max-width: 780px`（聊天配置）或 `860px`（能力设置）。**不要**用 `modal-lg`/`modal-xl` 混搭自定义宽度。
- 页脚按钮固定「取消在左、主操作在右」，顺序不可颠倒。取消一律 `btn-outline-secondary`。
- 模态框标题用 18px/650，不要再套一层衬线大字。
- 模态框标题必须与打开它的按钮文案对应（点「默认设置」进来的标题就是「翻译助手 · 默认设置」）。
- 危险确认统一走 `UI.confirm`：

```js
if (!await UI.confirm('将移除「日语」，只保留双语互译。确定继续吗？', {
    title: '切换为双语', confirmText: '切换为双语', variant: 'danger'
})) return;
```

  - `variant` 用 `danger` 表示不可逆操作。
  - `confirmText` 必须是**具体动词**（「删除」「切换为双语」），不是「确定」。
  - 消息正文说明**后果**，用 `\n` 分行，不要用 HTML。
  - 公共确认框按请求顺序展示，每次请求都必须在确认或取消后结束；不得直接移除仍在等待响应的窗口。打开后焦点落在「取消」，关闭后回到触发控件，并关联标题与说明供读屏读取。

### 6.11 折叠区

- 用原生 `<details class="system-fold">`（或该模块的折叠类）承载"次要但可能要看"的内容：原始 JSON、诊断信息、高级选项、调用历史。
- `<summary>` 文案要说清里面是什么（「查看原始配置」「高级选项」），并按需显示数量（「历史记录 3」）。
- 默认展开 vs 收起：**用户每次都要看的展开；偶发查看的收起。** 提示词编辑框在聊天场景默认收起，在全局默认场景默认展开。
- 折叠状态不持久化，除非业务明确要求。

### 6.12 工具栏与筛选

- 工具栏结构：左侧搜索（`.capability-search` 模式，带放大镜图标）+ 右侧筛选 chip 组；主操作按钮放最右或独立一行。
- 筛选 chip 与状态徽章复用同一套药丸样式，选中态用 `--accent-soft` 底 + `--primary` 字，不改变尺寸。
- 搜索是即时的（输入即过滤），不需要「搜索」按钮；需要防抖时用 120–200ms。
- 数量统计（「共 12 项」）用 meta 字号 + `--text-soft`，放在工具栏右侧或标题旁。

### 6.13 表格

- 表格用于**结构化的多条同构数据**，字段多于 4 列或需要纵向比较时使用；否则用列表行。
- 表头 `--text-secondary` + label 字号，行高紧凑；金额、数量右对齐并使用等宽体数字。
- 移动端：允许横向滚动（`overflow-x: auto`），或改用 `td[data-label]` 卡片化（`.system-compact-table` 模式），不要压缩成不可读。
- **固定列宽**：只要某一列的内容长度会随数据变化（状态、进度、活动摘要、错误文本），该表必须使用 `table-layout: fixed` 并逐列声明宽度（`.codex-sessions-table` / `.codex-jobs-table` 是范例）。自动布局会随文本长度重新分配所有列宽，整张表会跳动。
  - 表格单元格的 `box-sizing` 是 `content-box`，`width` 不含内边距。给这类表格的 `th`/`td` 显式声明 `box-sizing: border-box`，让 CSS 里的数字就是最终渲染宽度。
  - 列宽用像素声明（窄屏靠 `.table-responsive` 横滚）；只有整表按比例收缩的宽表才用百分比，且各列相加等于 100%。
  - 可截断的长文本由 `.codex-cell-main` / `.codex-cell-sub` 一类带 `overflow: hidden` + `text-overflow: ellipsis` 的容器承载，不要靠压缩列宽容纳；标识类文本（名称、标题）允许换行，不做省略号截断。
  - 窄屏下让 `.table-responsive` 横向滚动，不要为了塞进屏幕而放弃固定列宽。
  - 已固定：`.codex-sessions-table`、`.codex-jobs-table`、`.system-operations-table`、`.system-plugins-table`、`.system-incidents-table`、`.system-audit-table`、`.system-backup-table`、`.usage-table`、`.llm-route-table`、`.archive-message-table`、`.archive-members-table`、`.archive-lookups-table`、`.proxy-test-table`。新增同类表格照此办理。
  - 固定表格里的 `.visually-hidden`（例如空表头文字）是 `position: absolute`，在 `th` 未定位时会以视口为包含块、把页面撑宽。使用它时给所在 `th` 加 `position: relative`。
- 排序指示器放在表头内，一次只有一个排序生效。「展开」类操作独立成行内按钮，不带排序。
- **整行可点**：列表行整体可触发「展开 / 查看」时，行上标 `data-*-row`、`cursor: pointer`、hover 用 `--bg-soft` 高亮（`.usage-table tbody tr[data-usage-row]` 为范例），键盘可达性仍由行内按钮承担（`aria-expanded` / `aria-controls` 留在按钮上）。行内已有的控件（按钮、链接、输入、`summary`）不参与整行点击；用户选中文本后再点击不触发展开，避免误触。
  - **首列不特殊：名称列与其他列的反馈必须完全一致**。一行里只允许存在一种 hover 底色（`tr:hover > td`），行内名称按钮不再声明自己的 `:hover` 填充（写成 `.llm-usage button:not(.usage-name):hover` 或就地归零），否则展开态下鼠标放到首列会多出一块颜色。排除写法就是唯一的实现，不要再补一条 `background: transparent` 兜底副本 —— 2026-09-12 补的那条经悬停探针实测为死代码（删前删后 9 个悬停／展开状态逐字节相同），已删除。
  - **鼠标点击不移动焦点**：只有键盘触发（`event.detail === 0`）才把焦点还给同一行的**同类**按钮（名称按钮 → `.usage-name`，动作按钮 → `.usage-row-action`）。若鼠标点击也调 `focus()`，首列会留下 `:focus` 底色，看起来和点其它列不一样。
  - 行高亮只用 `tr:hover > td`，不要再补 `:focus-within` 行高亮，避免与 hover 叠加出第二种状态；键盘可达性用按钮自己的 `:focus-visible` 轮廓表示。
- `.table-light` 在暗色主题下会失去对比，已在 CSS 中统一覆盖；新表不要再引入其它 Bootstrap 表格变体。

### 6.15 图表读数（tooltip）

- **不要用原生 `title` 承载图表读数**：它有约 1 秒延迟、样式不可控，且**手机端完全不出现**。
- 图表的每根柱子／每个数据点都要有自定义读数层：桌面 `mousemove` 悬浮显示，手机 `click` 点按显示，键盘用方向键 + `Home` / `End` 逐点切换（图表容器 `tabindex="0"`，读数层 `role="status"`）。再点一次同一位置或点击图表外收起。
- **触屏按位置吸附，不要求点中目标**：手机上一根柱子约 10px，远小于手指落点精度，必须用横向比例算出最近的一格（`.dashboard-hour` 的 `data-hour-index` 为范例）。
- 读数层贴近对应数据点但**不得超出图表边界**（首尾两格按半宽收边），并给当前点加 `.is-active` 高亮作为视觉锚点。
- 柱子由多段构成时，每段的高度百分比必须相对**柱身**（`position: relative`），不能相对整条轨道 —— 否则低流量的小时也会画出满高的分段。
- 小时柱子的内部网格必须显式使用 `grid-template-columns: minmax(0, 1fr)`，避免时间刻度的内容宽度把柱轨撑宽、覆盖相邻柱子。≤575.98px 保留全部 24 根柱子，仅将刻度改为每六小时一个，逐小时读数仍由点按和键盘提供。
- 手机事件时间线的标题、脉搏状态、筛选区分行排列，状态文字允许换行，状态点不可被压缩；不能用裁剪或省略最后消息时间来避免溢出。

### 6.14 样式覆盖与 `!important`

- **同项目样式之间禁止用 `!important` 抢优先级。** 冲突用「就地改原规则 / 提高选择器特异性 / 调整顺序」解决。`style.css` 的 `!important` 已从 124 处收敛到 70 处（2026-09-12）：48 处在 Bootstrap 工具类重映射块内，其余 22 处多为移动端与表格列硬约束；`dashboard.css` 已清零（原 `.min-h-0` 从未被任何标记使用）。新增代码不得再增加。
- **判断一条 `!important` 能不能删，要看目标元素上有没有同属性的 Bootstrap 工具类。** `.text-*` / `.bg-*` / `.m*-*` / `.p*-*` / `.d-*` / `.border-*` 这些工具类本身带 `!important`，同元素上的组件规则无论特异性多高都赢不了它：删掉 `!important` 后规则就变成死代码（暗色主题下 `.bg-light` 徽章的文字颜色、侧边栏页脚的边框与链接 hover、Codex 行内危险按钮的 hover 都属此类，2026-09-12 去重时误删了这 5 处，其中徽章颜色是可见回归）。删之前用浏览器实测一遍：取该选择器匹配的元素，确认它带不带同属性的工具类。
- **覆盖 Bootstrap 工具类必须用 `!important`。** `.p-3`、`.mb-1`、`.gap-2`、`.text-*`、`.bg-*`、`.border-*` 自身带 `!important`，选择器特异性赢不了（`.sidebar-footer` 的移动端 `padding` / `gap` / `margin`、`.usage-row-action` 的颜色即属此类，必须保留）。这类声明集中写在 `style.css` 的 Bootstrap 兼容块内，不要散落到组件区。
- **解析结果相同的覆盖直接删。** 若一条 `!important` 最终解析出的值与工具类一致（本项目把 `--bs-secondary-rgb` / `--bs-secondary-color` 等 Bootstrap 变量映射到了自己的令牌），这条声明就是冗余的：去掉 `!important` 或整条删除，并在改动说明里写清依据。
- **不要新增重复副本。** 同一个选择器先搜全文，改就改原来那条；禁止再追加「最终覆盖」区块把前面的规则压过去。
- **先判活性，再决定删还是折算。** 一条规则「看起来被覆盖」有两种结局：完全被别人赢（DEAD，直接删）或仍在某几个宽度/状态下生效（LIVE，必须把声明折算进拥有该属性的那条规则）。用 `tests/browser/css_rule_liveness.py <基线快照> <起始行> <结束行>` 逐条判定：它会把每条规则单独删除后重抓计算样式，`DEAD` 才允许删，`LIVE` 会列出仍受影响的视图、元素与属性。2026-09-12 的批次 7 就是据此处理 `style.css` 尾部「Final density overrides」块的 19 条规则（5 条 DEAD 删除、2 条折算进组件规则、12 条并入组件自己的 `@media (max-width: 767.98px)` 块）。
- **删重复副本前必须逐组确认它是否仍在参与层叠。** 「被后面的规则完全覆盖」不等于可以删：本项目的重复副本大多是级联补丁，去掉会改变渲染（例：`.automation-config-button` 组件区那条 `display: flex` 之后，还有一条副本把 `width` 收回 grid 上下文；`.capability-card` / `.capability-description` / `.capability-metrics` 的主干 flex 与两行截断由更早那份定义，后面的密度副本只是重新声明）。2026-09-12 的去重对 7 组这类规则按「删前面、留后面」处理，直接导致功能插件页卡片显示异常；正确做法是**两个方向各删一次、各跑一遍 104 视图快照**（或先用第 15 章的活性探针判定谁仍在参与层叠），只有删一侧零差异时才合并，两侧都有差异就两份都留，并在 §14 登记原因。
- **这类改动必须用浏览器校验。** 删除 `!important`、合并重复选择器、把补丁改成令牌，都属于可能改变渲染结果的改动：改完后用 104 视图（13 视图 × 4 档宽度 × 明/暗）计算样式快照对比基线，确认零差异，再跑第 15 章的验证命令。任何有意的视觉修正（例如批次 7 删除尾部覆盖块后，执行顺序详情卡的配置按钮在 ≤1199.98px 恢复为 30px 方形）必须单独列出受影响视图与属性，并附截图。

---

## 7. 配置层级与作用范围（本项目核心）

控制台的配置有三层，任何配置界面都必须让用户随时知道自己站在哪一层：

```text
系统依赖 → 能力全局默认 → 聊天覆盖
```

这是最容易出错的地方，因此单独成章。

### 7.1 两个入口，一套字段

| 入口 | 位置 | 标题 | 作用对象 |
| --- | --- | --- | --- |
| 全局默认 | 能力目录 → 卡片 → 「默认设置」 | `<能力名> · 默认设置` | 所有未单独设置的聊天 |
| 聊天覆盖 | 聊天 → 插件 → 「设置」 | `<能力名> · 本聊天设置` | 当前聊天 |

两个入口必须渲染**同一套字段定义**（同一个渲染函数，例如 `renderTranslationSettingsView`）。禁止为聊天场景另写一份表单。

### 7.2 聊天页的第一屏是"作用范围"

进入聊天级设置时，**第一屏先回答"这个聊天是否使用默认设置"**，再展示字段：

- 单选式的显式选择：`跟随默认` / `本聊天自定义`，不用开关 + 二次确认的组合。
- 选「跟随默认」时，字段区域整体禁用并显示当前的全局生效值（只读）。
- 选「本聊天自定义」时字段解锁，预填当前生效值作为起点。
- 切换回「跟随默认」会丢弃该聊天的全部覆盖，属破坏性操作，需要 `UI.confirm`。

**禁止**为每个字段单独提供「继承 / 覆盖」选择器。逐字段继承选择带来的组合数是用户无法管理的，这也是旧版翻译助手配置页面最令人迷惑的地方。

### 7.3 覆盖粒度 = 字段差异

- 只把**被改动过的字段**写入覆盖，未动的字段不写。
- 字段恢复为默认值（清空输入、选择「默认」）等价于**取消该字段的覆盖**，而不是写入一个等于默认值的显式值。
- 界面要能显示"哪些字段已偏离默认"：在字段旁或区块标题旁加 `.is-custom` 徽章。
- 需要一个"全部恢复默认"的动作时，放在作用范围区域，不要在每个字段旁各放一个「重置」。

### 7.4 保存语义

- **聊天级设置保存即生效**：点击保存后直接 `PATCH` 到该聊天的策略（`plugin_configs`），不需要回到聊天页二次保存。
- 保存成功后同步更新内存中的策略版本与该聊天的徽章状态，避免下次读取到旧值。
- 全局默认页保存后作用于所有跟随默认的聊天，保存前明确提示影响范围。
- 长文本字段（提示词）也遵循差异覆盖：不改就不写，改了才写。

### 7.5 模板与预设：降级到字段内

配置模板、预设、示例这类辅助物是**字段的辅助，不是独立的一屏**：

- 放在它所服务的字段内部（提示词编辑框上方的模板条），而不是页面顶部的独立区块。
- 应用模板的提示语必须写清"这会覆盖当前内容"，且只影响该字段。
- 模板是复制而非引用：应用后形成独立副本，之后修改模板不回溯影响已应用的聊天（已有实现保持此语义）。

### 7.6 显式模式优于隐式派生

能力存在的形态（如翻译的「双语 / 三语」）必须显式列出：

- 用分段控件（`.translation-scope-toggle` 或 `btn-group` + `active`）列出全部模式，选中态清晰。
- 每个模式下方给出**输出示例**（如 `中文 ⇄ English`），让用户不用试就知道结果。
- 当切换模式会丢数据（三语 → 双语要移除一种语言）时才弹确认。
- 不要用「语言数量」这种数值输入 + 推断的方式表达模式。

### 7.7 术语与徽章文案（固定）

| 概念 | 使用 | 禁止 |
| --- | --- | --- |
| 全局层 | 「默认设置」「全局默认」 | 「公共配置」「主配置」 |
| 聊天层 | 「本聊天设置」 | 「单独配置」「独立配置」「聊天配置」 |
| 未覆盖 | 「跟随默认」 | 「继承全局」「使用默认」 |
| 已覆盖 | 「本聊天已自定义」 | 「自定义配置」「已修改」 |
| 恢复 | 「恢复默认」「清除此项自定义」 | 「重置覆盖」 |
| 生效 | 「保存并生效」 | 「提交」「应用更改」 |

---

## 8. 交互与反馈

### 8.1 状态

| 状态 | 表达 | 实现 |
| --- | --- | --- |
| 空闲 | 主操作按钮可点 | — |
| 有未保存改动 | 按钮可点 +（可选）按钮上出现 `is-dirty` | 与初始值比较；无改动时禁用保存 |
| 保存中 | 按钮 `disabled` + 转圈 + 进行时文案 | `is-saving` |
| 保存成功 | 关闭模态框（如适用）+ 成功 toast | `UI.showSuccess` |
| 保存失败 | 保留表单 + 错误 toast + 就地错误 | `UI.showError` |
| 加载失败 | 页面级 alert 或空态 + 重试按钮 | `alert alert-danger` |

### 8.2 反馈只给一次

- 请求失败的信息只在一个位置出现（toast 或就地错误，二选一），不要双写。
- 列表刷新成功不提示（用户看得到）；单条操作成功给 toast。
- 后台静默刷新（自动轮询）不产生任何提示，也不打断用户正在编辑的表单。

### 8.3 键盘与焦点

- 模态框打开时焦点进入模态框内第一个可交互元素（Bootstrap 默认行为，不要阻止）。
- 关闭模态框后焦点回到触发它的按钮（`UI.confirm` 已实现 `returnFocus`，自定义模态框照做）。
- 自定义可点击元素（非 `<button>`/`<a>`）必须加 `tabindex="0"` 并处理 Enter/Space，或直接改用 `<button>`。**优先改用 `<button>`。**
- 弹 toast 使用 `role="status"` + `aria-live="polite"`（`UI.showToast` 已实现）。

### 8.4 破坏性操作

必须二次确认 + 说明后果的操作：删除任何持久化对象、重启/停止服务、清空覆盖、导入并覆盖现有配置、断开已连接的账号。

不确认的操作：保存、切换标签页、展开折叠、搜索筛选、切换主题。

确认框文案模板：`<会发生什么>。确定继续吗？` + 破坏性时第二行补充 `<不可逆的后果>`。

---

## 9. 文案规范

1. **语言**：界面文案使用简体中文，术语与代码标识符（字段名、模型名、API 地址）保留原文。
2. **人称**：直接说明操作对象，不用「您」。例如「保存设置」而不是「保存您的设置」。
3. **按钮**：动词或动词 + 名词（「保存」「试译」「从 GitHub 安装」）。不用「确定」「提交」「是」。
4. **标题**：名词短语，不带句号（「翻译助手 · 默认设置」「资源占用」）。
5. **说明文字**：一句话说清"这是什么 / 会发生什么"。需要两句就换行或改用提示条。
6. **空态**：说明 + 下一步（「尚未添加语言。至少添加两种语言才能翻译。」）。
7. **错误信息**：`动作失败：<原因>`。原因用用户能理解的话，不要贴原始异常栈；技术细节放折叠区或日志。
8. **占位符**：给出格式示例而非重复标签（`https://api.example.com/v1`、`例如 weekly-report`）。
9. **数字与单位**：数值与单位之间不加空格的情况保持与邻近文案一致。**计数、用量、金额一律走 `UI` 的格式化函数**（`UI.formatNumber()` / `UI.formatMoney()`），不要在各模块里另写 `toLocaleString()`；`NaN`/空值必须回落到 `—` 而不是渲染出 `NaN`。
10. **时间**：绝对时间一律用 `UI.formatDateTime()`（zh-CN、24 小时制、固定 `Asia/Shanghai`，换部署机器显示一致）；紧凑位置（列表、额度重置）用 `UI.formatShortDateTime()`（MM-DD HH:mm），只显示时刻用 `UI.formatTimeOfDay()`。相对时间（「3 分钟前」）只在"多久之前"是重点时使用。**不要手写拼接日期字符串，也不要直接调用 `toLocaleString`。**
11. **转义**：所有插入 DOM 的动态内容必须转义，统一使用 `UI.escapeHtml()`（唯一实现，正则链 + 引号转义，文本与属性上下文都安全）。`main.js`、`llm_manager.js`、`codex_center.js`、`codex_skills.js` 里的同名方法只是委托入口，新增代码直接写 `UI.escapeHtml()`，不要再新开一份实现。
12. **计数与复数**：中文不需要复数形式；数字与量词之间保持「3 项」「12 个聊天」的一致写法。

---

## 10. 响应式与移动端

### 10.1 断点

只允许使用以下 3 个断点（Bootstrap 5 的相邻值）：

| 断点 | 宽度 | 说明 |
| --- | --- | --- |
| 移动 | `max-width: 575.98px` | 手机：单列、控件撑满、隐藏次要信息 |
| 平板 | `max-width: 767.98px` | 侧栏折叠为抽屉、网格降为 1–2 列 |
| 桌面窄幅 | `max-width: 991.98px` | 双栏布局降为单列 |

**新代码不得引入新断点。** 2026-09-12 已把写法统一到上表：`768px` → `767.98px`（含 `min-width: 769px` → `min-width: 767.98px`，桌面形态从 767.98 起生效，不再有 768px 空档）、`575px` → `575.98px`，`dashboard.css` 的 `767/991/1199` → `767.98/991.98/1199.98`。

快照审计的默认宽度矩阵（1440 / 1024 / 800 / 390）就是为覆盖这三档断点选的：1024 落在 991.98–1199.98 之间，800 落在 767.98–980 之间，改断点相关样式时必须用它跑完 104 个视图，不能只看桌面宽度。

JS 侧判断移动端统一用 `UI.isMobileViewport()`（内部是 `matchMedia('(max-width: 767.98px)')`），不要再写 `innerWidth <= 768` —— 阈值写死在两处就会在恰好 768px 时出现「CSS 认为是桌面、JS 认为是移动」的错位。

以下 6 处是**组件密度过渡断点**，与上表的三种设备档位不是一回事，2026-09-12 保留了它们并记录依据（合并会改变 992–1280 区间的版式，属于设计改动而非等价重构）：

| 断点 | 影响的组件 | 为什么保留 |
| --- | --- | --- |
| `max-width: 900px` | `.assistant-*` 网格、`.system-settings-shell` / `-nav`、`.capability-toolbar` | 实测 `.system-settings-nav` 在 ≤900 为 2 列、≥950 为 1 列，规则是活的；合并到 991.98 会把 901–991 区间的桌面版式整体改成收窄版式 |
| `max-width: 980px` | 自动化工作台的紧凑行、聊天预览控件 | 与 1280/1199.98 三档一起构成密度递减；合并到 991.98 会把 981–991.98 区间的紧凑行换成更宽松的版式（属版式改动）。桩环境现已渲染执行顺序工作台，可逐宽度验证后再决定是否合并 |
| `max-width: 1050px` | `.system-tool-card-main` 单列、`.system-tool-facts` 缩进 | 桩数据不渲染工具卡，合并方向属于版式改动；验证前先给 `console_stub.py` 补上工具卡数据 |
| `max-width: 1100px` | `#llmModelsList` / `.roles-grid` 降为 2 列、模型配置单列 | 实测模型卡 1100 及以下 2 列、1101 起 3 列；合并到 991.98 会让 992–1100 挤成 3 列（反而更差），合并到 1199.98 又会整体变 2 列 |
| `max-width: 1280px` | 自动化紧凑行的列宽与结果列显隐 | 与 980/1199.98 同属自动化工作台的渐进降级 |
| `max-width: 1199.98px` | 自动化工作台栅格、`.backup-profile-copy` | 992–1199 区间需要这一档；删除或移动都会改变该区间版式 |

合并这类断点的唯一安全做法是先把组件改成流体（`repeat(auto-fit, minmax(...))`），再用宽度扫描（见第 15 章）证明各宽度零差异；在那之前不要按「凑够三个值」硬合并。

### 10.2 移动端规则

- 布局：双栏 → 单列；网格 `auto-fill` → 1 列；工具栏横向操作改为整行按钮。
- 主导航：侧栏变为抽屉，靠 `mobile-nav-trigger` 开关，背景加 `.mobile-overlay`。触摸目标不小于 40px。
- 触控目标：可点击区域最小 36×36px，主要操作 40px 以上；行内图标按钮之间至少留 8px。
- 表格：横向滚动或卡片化；禁止把列压到不可读。
- 模态框：手机上接近全屏，页脚按钮撑满宽度。
- 移动端**不要折叠掉用户正在编辑的字段**，只隐藏辅助信息（说明文字、次要指标）。
- 每个组件自己的移动端规则写在组件样式块之后，用 `@media (max-width: 575.98px)` 就地声明，不要集中堆到文件末尾。

---

## 11. 可访问性

1. 所有交互元素可键盘到达，焦点可见（`outline: 2px solid var(--primary); outline-offset: 2px;`，已有 `.focus-visible` 实现可参考）。
2. 图标按钮必须有 `aria-label` 与 `title`；纯装饰图标加 `aria-hidden="true"`。
3. 表单控件必须有 `<label for>`；没有可见标签时用 `aria-label`。
4. 状态变化用 `role="status"` / `aria-live="polite"` 播报（toast、进度、扫描结果）。
5. 开关用 `role="switch"` + `aria-checked`；切换标签用 `role="tab"` 语义或 Bootstrap 的 tab 组件。
6. 颜色不单独承载信息：状态徽章的底色之外必须有文字；危险按钮除颜色外有明确文案。
7. 折叠区用原生 `<details>`/`<summary>`，键盘行为免费获得。
8. 动画尊重 `prefers-reduced-motion`。
9. 不嵌套交互元素（按钮里套按钮、`<a>` 里套 `<button>`）。

---

## 12. 新增 / 修改界面的检查清单

提交前逐条确认：

- [ ] 只用第 3.1 节的语义别名；没有新增十六进制色、`--xxx-rgb`、字面量圆角（999px / 50% 除外）
- [ ] 字号取自 3.7 的 6 档；同一区域不超过 3 档；没有新增 `rem` 换算字号
- [ ] 图标尺寸取自 3.7 的图标阶梯；时间与数字走 `UI.formatDateTime` / `formatShortDateTime` / `formatTimeOfDay` / `formatNumber` / `formatMoney`
- [ ] 浅色与暗色主题都实际看过
- [ ] 页面骨架沿用第 5.1 节层级，类名前缀落在已有领域内
- [ ] 一个视图只有一个实心主操作按钮
- [ ] 所有动态内容经 `UI.escapeHtml()` 转义
- [ ] 异步操作有加载态、成功反馈、失败保留输入
- [ ] 破坏性操作走 `UI.confirm` 且 `confirmText` 是具体动词
- [ ] 空态有"为什么空 + 下一步"；加载态有进行时说明
- [ ] 移动端（≤575.98px）检查过：无横向溢出、触控目标足够、无被遮挡的操作
- [ ] 键盘可完成主要流程，焦点可见，图标按钮有 `aria-label`
- [ ] 涉及配置时：明确了当前层（默认 / 聊天）、保存语义、覆盖粒度（见第 7 章）
- [ ] 若改了 `web/index.html` 的缓存串，同步更新 `tests/test_web_console_contract.py`
- [ ] 没有新增 `!important`（覆盖 Bootstrap 工具类除外，见 6.14）；没有新增同一选择器的第二个定义
- [ ] 改了样式表（合并规则、删 `!important`、改令牌）后，用 104 视图计算样式快照对比基线确认零差异；有意的视觉修正要列出受影响视图/元素并附截图
- [ ] 若引入了新令牌或新组件模式，同步更新本文档

---

## 13. 反面模式（禁止事项）

以下都来自当前代码库的真实情况，是"和原设计格格不入"的具体成因。

**颜色**

- ❌ 在组件里写 `var(--canvas)` / `var(--ink)` / `var(--muted)` / `var(--hairline)`（原始调色板）。这些在暗色主题下不会重映射，等于写死浅色。现存 77 处，除第 3.2 节的"刻意固定深色"例外，新增代码一律用别名。
- ❌ 新增十六进制颜色或 `--xxx-rgb`。需要新色用 `color-mix` 从既有令牌派生。
- ❌ 把 coral 用在非主操作上（导航选中、重复的次要按钮、装饰图标）。coral 是稀缺资源。

**尺寸与形状**

- ❌ 硬编码圆角 `9px` `10px` `11px` `14px` `18px`（现存约 94 处）。改用 `--radius-*`。
- ❌ 新增 `font-size: .62rem` 这类换算值。96 个不同字号就是这么来的。
- ❌ 新增媒体查询断点。用 575.98 / 767.98 / 991.98。

**结构与选择器**

- ❌ 追加「最终覆盖」区块去压过前面的样式。`style.css` 尾部的 `Final density overrides` 区块就是这样长出来的，2026-09-12 已按活性逐条折算回组件规则（见 6.14 / §14）。**正确做法是就地改原规则，让同一条规则只存在一处。**
- ❌ 同一个选择器分散在多个区块重复定义（2026-09-12 已合并 26 条被完全覆盖的重复顶层规则，规则总数 2266 → 2240、声明 7313 → 7264；仍有 287 个选择器在多处出现（合计多出 364 处），其中多数是响应式分组、状态分组与上文 6.14 说的级联补丁，需逐组双向确认后再合并）。改样式前先搜全文，把所有定义一起改。
- ❌ 用 `!important` 压过同项目样式。全表现存 70 处（2026-09-12 由 124 处降到 70 处），除 Bootstrap 工具类重映射块里的 48 处外，只剩 22 处硬约束，新增代码不得再增加 —— 覆盖 Bootstrap 工具类时必须用 `!important`，判断依据见 6.14。
- ❌ 用内联 `style="..."` 表达状态或主题相关的颜色（位置/宽度计算除外）。
- ❌ 用 `:has()` 做页面级布局分派（如 `#configModal:has(.xxx)` 调整模态框宽度）时不做兜底。这类写法可读性差，新代码优先给模态框加显式类或 `data-` 属性。

**交互**

- ❌ 逐字段的"继承 / 覆盖"下拉。用第 7 章的作用范围模型。
- ❌ 需要"保存两次"的设置。保存即生效。
- ❌ 乐观翻转一个后端驱动的开关（服务级开关必须由真实状态驱动）。
- ❌ 失败时清空用户输入或关闭模态框。
- ❌ 一个动作同时给 toast 和页面 alert。
- ❌ 用 `alert()` / `window.confirm()`（`UI.confirm` 已有降级兜底，业务代码不要直接调用）。

**文案**

- ❌ 「单独配置」「主配置」这类无法判断层次的词。
- ❌ 「确定 / 提交 / 是」作为按钮文案。
- ❌ 只写「暂无数据」的空态。
- ❌ 把原始异常堆栈直接显示给用户。

**主题**

- ❌ 只做浅色；暗色留给以后。
- ❌ 用 `:root[data-theme='dark'] .my-component { ... }` 打补丁，而不是使用语义别名。
- ❌ 给固定配色的区域（日志终端等）写一整套暗色覆盖加浅色反覆盖，而不是抽成 `--log-*` 这样的组件令牌组。
- ❌ 只设置 `data-bs-theme` 或只设置 `data-theme`（两者都要，且要更新 `meta[name="theme-color"]`）。

---

## 14. 已知技术债与迁移方向

不要求在近期一次性完成，但**新增代码不应加重**，并且改造相关模块时顺手收敛：

| 项目 | 现状 | 目标 |
| --- | --- | --- |
| 字号 | 已收敛（2026-09-12）：CSS 525 处数值声明只剩 12 个取值（10/11/12/13/14/15/16/18/20/24/32/64），全部落在 3.7 的文本/图标阶梯上；JS 内联 `font-size` 已清零，43 处改用 `.u-text-10/11/12/13/15`、`.u-icon-32`、`.status-dot`（新增工具类写在 `style.css` 末段） | 新增字号一律取阶梯值；JS 里不要写内联 `font-size` |
| 圆角 | 已收敛（2026-09-12）：189 处使用令牌；剩余字面量只有允许的 `999px`(39)、`50%`(14)、`0`(24)、`inherit`(4)，以及 1 处 `0 var(--radius-md) var(--radius-md) 0` 的四值简写 | — |
| 调色板直引 | 已复查（2026-09-12，批次 6）：删除 `.mobile-overlay` 死规则与 3 处重复的 `.custom-scrollbar`；剩余 77 处中，`.logs-toolbar` 的 3 处、`.automation-overview-strip` 的 2 处、`.btn-success/.btn-warning/.btn-info` 与 `.badge.bg-*` 的 6 处、移动端遮罩的 1 处均为刻意固定（深色终端 / 深色条带 / 浅底深字），已在原处留注释 | 新代码只用语义别名；确需字面量时在原规则上方写一行 `/* 刻意：原因 */` |
| 阴影 | 已收敛（2026-09-12，批次 6）：7 处字面量并入 `--shadow-sm/md/lg`。累计对照批次 4 之前的基线，可见差异**只有 5 个元素**，全部是这次归并：`.service-command-bar` 与 `.service-power-switch > span`（滑块）由 `0 1px 2px/3px @4%/22%` 变为 `--shadow-sm`；`.mobile-nav-trigger`、移动端 `.sidebar` 抽屉由 `@16%/22%` 的 9–22px 投影变为 `--shadow-lg`；`.system-settings-mobile-toggle` 由 `@7% 5px18px` 变为 `--shadow-md`。其余 36 视图逐元素零差异；这 5 处已在明暗两主题、桌面/移动四档宽度下截图确认（浅色下更轻、深色下随令牌加深） | 新增阴影只用这三个令牌；确需第四档时先改本节 |
| 断点 | 已收敛（2026-09-12）：写法统一到 575.98 / 767.98 / 991.98（`dashboard.css` 另统一到 767.98 / 991.98 / 1199.98），JS 与 CSS 共用 `UI.isMobileViewport()`；另有 6 处组件密度过渡断点（900 / 980 / 1050 / 1100 / 1280 / 1199.98），见 10.1 的逐条依据 | 新增代码不得引入新断点；过渡断点要么随组件流体化消失，要么补上宽度扫描证据后保留 |
| 重复选择器 | 已收敛（2026-09-12）：合并 26 条重复顶层规则 + 批次 7 的尾部覆盖块（规则 2266 → 2231、声明 7313 → 7236）；剩余 287 个多定义选择器（合计多出 355 处）多为响应式 / 状态分组 | 新增规则前先搜全文；禁止再追加「最终覆盖」副本；合并前先用 `css_rule_liveness.py` 判活性，再用 104 视图快照验证（见 6.14） |
| `!important` | 已收敛（2026-09-12）：`style.css` 124 处 → 70 处，其中 48 处在 Bootstrap 工具类重映射块内（工具类自带 `!important`，必须保留），其余 22 处为组件覆盖工具类、移动端与布局硬约束；`dashboard.css` → 0 处（删除从未被使用的 `.min-h-0`） | 判据见 6.14；新增代码不得增加 |
| 暗色补丁 | 已收敛（2026-09-12）：日志页 157 行 `:root[data-theme='dark'] .logs-*` 暗色补丁与浅色反补丁合并为 `--log-*` 令牌组，浅色／暗色各定义一次 | 新组件只用语义别名或自有令牌组，不写主题补丁 |
| 转义实现 | 已统一（2026-09-12）：`UI.escapeHtml` 是唯一实现（正则链，无 DOM 分配，引号同样转义）；`main.js`、`llm_manager.js`、`codex_center.js`、`codex_skills.js` 的同名方法降级为一行委托 | 新代码直接调 `UI.escapeHtml` |
| 公共交互 | 已补齐（2026-09-12）：能力设置、移动系统导航、模型校验提示统一走 `UI.scrollIntoView`；确认框增加顺序展示、焦点恢复、标题/说明关联和实例清理；恢复备份改用 `variant: 'danger'`，移除第三语言使用危险确认，重置上下文按钮使用具体动词 | `tests/browser/ui_interactions.py` 验证明暗主题、390/1440px、动态效果偏好，以及排队、取消、确认与焦点恢复 |
| 时间与数字格式 | 已统一（2026-09-12）：新增 `UI.formatDateTime` / `formatShortDateTime` / `formatTimeOfDay` / `formatNumber` / `formatMoney`，替换 30+ 处裸 `toLocaleString()` 与手写日期拼接 | 新代码只用这组函数 |
| 样式覆盖块 | 已清除（2026-09-12，批次 7）：`style.css` 尾部 "Final density overrides" 区块的 19 条规则按活性处理——5 条被完全覆盖（`.console-subnav.nav-pills` ×3、`.roles-top-bar`、`.roles-grid`）直接删除；`.automation-route-actions` 的 26px 与配置按钮宽度折算进组件区规则；其余 12 条并入组件自己的 `@media (max-width: 767.98px)` 块。104 视图快照零差异，唯一有意的视觉修正已记在 6.14 | 新规则只写在组件自己的区块里；发现「最终覆盖」副本一律按 6.14 的活性流程处理 |
| 空规则块 | 已清理（2026-09-12，删除 5 处空的 `@media` 块） | — |
| 规则跳动的表格 | 已收敛（2026-09-12）：`codex-sessions-table`、`codex-jobs-table`、`system-operations-table`、`system-plugins-table`、`system-incidents-table`、`system-audit-table`、`system-backup-table`、`usage-table`、`archive-message-table`、`archive-members-table`、`archive-lookups-table`、`proxy-test-table` 全部改为固定列宽；`llm-route-table` 原本已是固定布局 | 新增表格按 6.13 的固定列宽规则处理 |
| 渲染证据 | 已入库（2026-09-12）：视觉/样式证据由 `tests/browser/` 的桩 + 快照审计 + 活性探针产出，不再依赖临时目录里的脚本；每个批次的基线快照留在 `/tmp` 仅供参考，结论写回本文档与 `CHANGELOG.md` | 样式批次必须附「基线 → 改动 → 对比」三步命令与结果行 |

---

## 15. 文件地图

| 文件 | 内容 | 改它的注意事项 |
| --- | --- | --- |
| `web/index.html` | 页面骨架、侧栏、全部模态框的静态结构、缓存串 | 缓存串改动需同步 `tests/test_web_console_contract.py` |
| `web/assets/css/style.css` | `:root` 令牌（含 `--log-*` 日志令牌组）、Bootstrap 兼容块、全部组件样式（7200+ 行） | 令牌只能改在 `:root` / `:root[data-theme='dark']`；改组件样式前先搜全文，同一选择器只留一处 |
| `web/assets/css/dashboard.css` | 概览页专用：一整块 `workspace-surface` 上按分带排列状态带、待处理、KPI、24 小时活动、额度用量与事件时间线 | 不新增令牌；分带线由 `.dashboard-shell > * + *` 与 1px 间隙提供，不要改回逐个卡片加边框 |
| `web/assets/js/ui.js` | `UI`：toast、确认框、模态框辅助、通用渲染器、时间与数字格式化、`escapeHtml` | 通用交互的唯一入口 |
| `web/assets/js/main.js` | `App`：标签页路由、聊天、能力目录 | 路由改动见 5.3；概览只负责调度 `Dashboard.load()` |
| `web/assets/js/dashboard.js` | `Dashboard`：概览的分带渲染（状态、待处理、KPI、24 小时活动、用量、事件时间线）与 Codex 额度 | 图表与迷你趋势用内联 SVG／DOM 实现，不引入图表库；失败只标记对应分带的 `data-stale`；实时脉搏是独立于整页 30 秒刷新的 5 秒计时器，切标签／切后台必须调用 `stopPulse()` |
| `web/assets/js/llm_manager.js` | `LLMManager`：模型、路由、用量 | 已含重复的 `escapeHtml` |
| `web/assets/js/codex_center.js` / `codex_skills.js` | Codex 中心与 Skills | 各自带局部 `escape` |
| `web/assets/js/system_operations.js` / `system_tools.js` | 运维与系统工具 | `formatTime` / `formatBytes` 的现有实现；运行状态、备份表带固定列宽类名（见 6.13） |
| `web/assets/js/chat_archive.js` | 聊天归档 | 成员 / 查阅表带 `archive-members-table` / `archive-lookups-table`，列宽写在 CSS |
| `tests/browser/console_stub.py` | 浏览器审计用的确定性 API 桩：真实 `index.html` + 真实 CSS/JS，桩数据覆盖能力卡、角色/判断器、模型、会话、日志，以及执行顺序工作台的 `event_types` / `routes` / `chats` | 新组件要有桩数据才能进快照；桩里留空的区块等于对审计隐形 |
| `tests/browser/ui_snapshot_audit.py` | 计算样式快照审计：`capture` / `compare` / `report` / `shots`；默认 13 视图 × 4 宽度 × 明暗 = 104 个视图，`--css-dir` 可换用变体样式表 | 改样式前后各抓一次；有意的视觉修正要另附 `report` 明细与截图 |
| `tests/browser/css_rule_liveness.py` | 逐规则活性探针：给定 `style.css` 的行范围，逐条删除后重抓并对比，输出 `DEAD` / `LIVE` | 判定 `DEAD` 才可直接删；`LIVE` 必须折算进拥有该属性的规则，再整体复验 104 视图 |
| `tests/browser/ui_interactions.py` | 公共确认框生命周期、键盘焦点、动态效果偏好及横向溢出验证 | `MABOBOT_TEST_BOOTSTRAP_DIR` 可指定 Bootstrap 5.3.0 缓存目录离线运行；`MABOBOT_TEST_SCREENSHOTS` 可指定截图目录 |
| `tests/browser/dashboard_mobile.py` | 概览柱轨/柱身/刻度不重叠、手机脉搏文字容纳及分行、首尾触屏读数、键盘切换和横向溢出 | 320–1440px 十档宽度 × 明暗；`MABOBOT_TEST_SCREENSHOTS` 可保存 320/390/1440px 的图表与时间线截图 |
| `tests/test_web_console_contract.py` | Web 端契约断言（含缓存串字面值） | 改动界面后必须跑 |

### 验证命令

```bash
# JS 语法
node --check web/assets/js/ui.js && node --check web/assets/js/main.js

# Web 契约与相关服务测试（需在可访问无沙箱网络的环境下运行）
python -m pytest tests/test_web_console_contract.py tests/test_capability_service.py
```

浏览器级验证脚本位于 `tests/browser/`：快照审计 `ui_snapshot_audit.py` 与活性探针 `css_rule_liveness.py` 只用 Chromium + `console_stub.py` 桩数据即可运行（`MABOBOT_TEST_CHROMIUM` 指向无头 Chromium）；`translation_chat_config.py`、`push_recipients.py`、`chat_role_refresh.py` 等行为脚本同样自带桩，按需运行。

2026-09-12 公共交互批次验证：`python -m pytest tests/test_web_console_contract.py tests/test_capability_service.py -q` 为 70 passed（391 subtests passed）；`python tests/browser/ui_interactions.py` 的 390/1440px × 明暗四组全部通过，涵盖两种动态效果偏好、确认排队、Esc/关闭取消、确认、焦点恢复、遮罩清理与无横向溢出；`python tests/browser/translation_chat_config.py` 的八项行为检查全部通过。本批未修改样式表，截图通过 `MABOBOT_TEST_SCREENSHOTS` 生成。

2026-09-12 手机概览修复批次：在改动前保存 `style.css` / `dashboard.css` 到 `/tmp/mabobot-mobile-before/`，验证步骤如下（设置 `MABOBOT_TEST_CHROMIUM` 后运行）：

```bash
python tests/browser/ui_snapshot_audit.py capture /tmp/mabobot-mobile-before.json --css-dir /tmp/mabobot-mobile-before
# 修改 dashboard.css：柱子内部列宽、手机刻度密度、时间线标题区分行。
python tests/browser/ui_snapshot_audit.py capture /tmp/mabobot-mobile-after.json
python tests/browser/ui_snapshot_audit.py compare /tmp/mabobot-mobile-before.json /tmp/mabobot-mobile-after.json
python tests/browser/ui_snapshot_audit.py report /tmp/mabobot-mobile-before.json /tmp/mabobot-mobile-after.json
MABOBOT_TEST_SCREENSHOTS=/tmp/mabobot-mobile-after python tests/browser/dashboard_mobile.py
```

结果为 `72/104 views identical, 32 differ, 0 class-only, 0 missing`，无 DOM 数量变化。逐元素复核：概览 1440/800/390px 的刻度所在柱轨恢复正确列宽，390px 的刻度及时间线标题/状态/筛选与面板高度为有意修正；1024px 的两主题仅有脉搏动画采样时点差异。其他 24 个手机视图的差异均为隐藏概览中五个时间线元素的计算样式，不影响当前可见页面。快照另外包含脉搏背景色、圆点透明度与 transform 的动画采样噪声。320–1440px 十档宽度 × 明暗的 20 组专项检查全部通过，图表与时间线截图已复核；Web 与概览契约测试为 64 passed（401 subtests passed）。

改动样式表（合并规则、删除 `!important`、令牌替换）时，除上述测试外还要做计算样式零差异校验：同一份页面在改动前后各抓一次「13 视图 × 4 档宽度 × 明/暗」共 104 个视图的逐元素计算样式快照（display / 盒模型 / 字体 / 颜色 / 阴影 / transform 等 75 项），逐视图比对哈希，必须完全一致才算通过。

```bash
# 1) 基线（改动前）
python tests/browser/ui_snapshot_audit.py capture /tmp/ui-before.json
# 2) 改样式
# 3) 对比：104/104 identical 才算通过；有差异时看明细
python tests/browser/ui_snapshot_audit.py capture /tmp/ui-after.json
python tests/browser/ui_snapshot_audit.py compare /tmp/ui-before.json /tmp/ui-after.json
python tests/browser/ui_snapshot_audit.py report  /tmp/ui-before.json /tmp/ui-after.json
# 4) 截图与横向溢出检查（--routes/--widths/--themes 可裁剪）
python tests/browser/ui_snapshot_audit.py shots /tmp/ui-shots

# 想验证「删掉某段样式会不会变」时，把变体样式表放进 --css-dir（缺的文件自动回退到仓库内版本）
python tests/browser/ui_snapshot_audit.py capture /tmp/ui-variant.json --css-dir /tmp/css-variant

# 判断一段规则里每条是否还在参与层叠（DEAD 可删 / LIVE 要折算）
python tests/browser/css_rule_liveness.py /tmp/ui-before.json 4049 4071
```

快照脚本的 API 桩必须返回**真实结构的动态数据**：能力卡片（`/api/capabilities/` 是扁平结构）、角色与判断器、模型卡这些区块过去因为桩返回空数组而从未进入快照，2026-09-12 功能插件页的样式回归就是这样漏掉的。改这些区块的样式前，先确认快照里能看到它们。
