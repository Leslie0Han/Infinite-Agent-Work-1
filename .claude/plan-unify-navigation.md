# 第三步：统一导航（左栏唯一导航，顶栏只留全局动作）

## 目标
消除"顶栏 4 名词按钮"和"左栏 工作台/Agent 切换"两套并存、且指向同一批 iframe 的导航。改成：左栏是唯一导航源，顶栏只保留全局动作。main-panel 仍是 agentView/workbenchView 二选一（这层保留），但由左栏统一驱动。

## 现状关键事实
- 顶栏 `.top-nav` 4 按钮（模型市场/工具库/知识库/资源）→ `handleTopAction` → 都落到 workbench 换 iframe。
- 左栏 `.mode-tabs`（工作台/Agent segment）→ `switchShellMode`，并据此切换左栏内容：agent-mode 显示 `.recent-wrap`（最近），workbench-mode 显示 `.workbench-tools-wrap`（8 工具）。
- `WORKBENCH_TOOLS` = zimage/online/klein/enhance/angle/gpt-chat/canvas/library（library 在内，wiki 不在）。
- `switchShellMode` 里有一段给顶栏 tools 按钮加 active 的逻辑；`setTopNavActive` 在 activateFrame 里点亮顶栏。

## 新左栏结构（始终完整显示，不再按 mode 切换内容）
```
[≡ 折叠]
┌──────────────┐
│ ✦ Agent            │  ← 点击 = switchUI('agent')，高亮当前
│ + 新对话            │
├──────────────┤
│ 工具                │  (分组标签)
│  · 智能画布          │
│  · 在线生图          │
│  · 图片编辑          │
│  · 图片增强          │
│  · 角度控制          │
│  · GPT 对话          │
│  · Z-Image          │
├──────────────┤
│ 知识库              │  ← openStudioPage('wiki', ...)
│ 资源库              │  ← openStudioPage('library', ...)
├──────────────┤
│ 最近 (Agent 对话)    │  ← recentAgentList，始终显示
└──────────────┘
```
- 把 library 从"工具"组移到独立的"资源库"入口；新增"知识库"入口（wiki）。
- "最近"列表常驻（不再只在 agent-mode 显示）。

## 顶栏变化
- 删除整个 `.top-nav`（4 个 top-nav-btn）。
- 保留 brand、nano-monitor、搜索、通知、设置(齿轮，data-top-action="settings"→api-settings 保留)、主题、账户。

## 具体改动（仅 static/index.html）

### DOM
1. 删 `<nav class="top-nav">` 整段。
2. 重构 `.left-panel` 内部：
   - 删 `.mode-tabs`（工作台/Agent segment 按钮）。
   - 顶部放 Agent 入口按钮（data-nav="agent"）+ 新对话按钮（沿用 newAgentBtn）。
   - "工具"组：`workbenchToolList` 容器保留（渲染逻辑复用），但 WORKBENCH_TOOLS 去掉 library，单列。
   - 新增"知识库/资源库"两个导航项（data-nav-page="wiki"/"library"）。
   - "最近"组 `recentAgentList` 容器保留，移到底部，常驻。
   - drawer-toggle 保留。

### CSS
3. 删除 `.left-panel.workbench-mode` / `.left-panel.agent-mode` 控制 `.workbench-tools-wrap`/`.recent-wrap`/`.new-chat-btn` 显隐的规则（让它们常显）。
4. 删 `.mode-tabs`、`.segment-btn` 及 drawer-collapsed 下针对 segment 的规则。
5. 删 `.top-nav` 相关 CSS。
6. 给新导航项加样式（复用 tool-list-item 风格）。

### JS
7. `switchShellMode`：删掉操作 mode-tabs（agentTab/workbenchTab active）、`.top-nav-btn`、agent-mode/workbench-mode class 的代码；保留 agentView/workbenchView 显隐 + 右栏切换。
8. 删 `setTopNavActive` 函数及其 4 处调用（activateFrame 内）。
9. `handleTopAction`：只保留 settings→api-settings；删 models/tools/knowledge/resources 分支（顶栏按钮已删）。data-top-action="settings" 齿轮仍走它。
10. mode-tab 绑定（`[data-mode-tab]`）改为新左栏导航项绑定：Agent 入口→switchUI('agent')，知识库/资源库→openStudioPage。
11. 删 agentTab/workbenchTab 的 const 引用（若仅用于上述已删逻辑）。
12. `WORKBENCH_TOOLS` 移除 library 项（它成为独立"资源库"入口）。

## 不做
- 不改 main-panel 的 agentView/workbenchView 双 view 机制。
- 不改 Agent 执行逻辑、知识库逻辑。
- 不改 iframe 加载机制。
- 右栏（agentRightPanel/workbenchRightPanel）本期不动。

## 验证
1. 起服务，开 `/`。
2. 顶栏只剩 brand + 全局动作，无 4 名词按钮。
3. 左栏一屏显示：Agent 入口、工具组(7项)、知识库、资源库、最近。
4. 点 Agent→进对话页；点某工具→workbench 开对应 iframe；点知识库→开 wiki；点资源库→开 library。
5. 折叠/展开 drawer 正常。
6. 控制台无 JS 报错，无未定义元素引用；node --check 通过。
7. 服务端无 500。

## 风险
- index.html 是大单文件，左栏 DOM 重构 + 多处 JS/CSS 删除要保持引用一致。改完用 grep 扫孤儿引用 + node 语法检查 + 起服务实测兜底。
- `switchShellMode` 被多处调用，删其内部逻辑时只删 mode-tabs/top-nav 相关，保留 view 切换主干。
