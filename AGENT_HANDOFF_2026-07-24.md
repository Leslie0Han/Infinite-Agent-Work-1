# Infinite Agent Work 完整对话交接

更新时间：2026-07-24（Asia/Shanghai）  
项目目录：`/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1`  
用途：交给新的 Agent 继续开发、验收和发布。

## 0. 关于“全部内容”和分析

这不是系统提示、开发者指令或模型私有逐字思维链的转储；这些内容不能导出。本文提供可用于继续工作的完整替代材料：用户需求演变、全部重要上下文、决策依据、实现结果、文件和接口、验收证据、失败与修复、当前仓库状态、风险以及下一步建议。

新 Agent 应以本文为导航，但对会变化的状态（服务、Git、数据库、文件）必须现场重新验证。

## 1. 用户最终目标

用户想把 Infinite Agent Work 做成接近申江海工作室 AI App 的建筑设计工作平台，而不是一个只会聊天或生成孤立图片的 Demo。核心特征是：

1. 用统一外壳组织助手、素材库、AI 生图、项目管理等模块。
2. Agent 在正确项目中调用真实工具，留下画布、素材、任务历史和血缘，刷新后仍可追溯。
3. 素材从生成、入库、采用/淘汰、评分到反馈重试形成闭环。
4. 专业建筑工作流逐步固化成产品能力；当前重点是“建筑强排 PPT”，先实现彩总、鸟瞰、户型平面替换。
5. 保持现有图像工作台独立，不要为追求通用 Agent 架构而破坏已有能力。

用户偏好：直接执行、做完可运行、用真实数据验收；尽量少问无必要的问题。项目有大量混合未提交工作，禁止粗暴重置或 `git add -A`。

## 2. 重要参考资料

### 申江海资料

- 审计报告目录：`/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1/reports/shenjianghai-ai-system-audit`
- 文字稿：`/Users/leslie/Downloads/申江海工作室AI介入设计工作分享_original_原文.docx`
- 视频：`/Users/leslie/Documents/爬取专家/申江海工作室AI介入设计工作分享_original.mp4`
- 对话中提供了 11:12、28:00、34:18 等视频位置截图，以及助手页、素材库、画布/展廊等 UI 截图。

### PPT 资料

- 原始母版：`/Users/leslie/Desktop/浙江保利杭州运河中地块立项方案-0720(1).pptx`
- 同事 Demo：`https://github.com/largo166/0716`
- 同事 Demo 的可取点：三栏编辑器、模板/编辑/校验模式、左侧页面列表、中间 16:9 画布、右侧属性/任务区域、明确的导入与导出动作。

## 3. 对话时间线与已经形成的结论

### 阶段 A：统一地基与申江海模式研究

1. 用户要求基于申江海审计报告仔细研究项目库，找出可以学习和提升的部分，并先做“第一阶段统一地基 + 纵向闭环”。
2. 随后要求直接做“项目工作台”，并询问整个最外层 UI 是否可以学习申江海 App：顶部切换助手、素材库、AI 生图、项目管理等。
3. 用户提供视频、文字稿和截图，要求分析每个页面的按钮、功能以及对应转写稿/视频位置；工时模块明确不做。
4. 用户质疑左侧抽屉里的 Agent、图像工具、资源库是否有存在必要。形成的原则是：保留能力，但抽屉必须按当前任务提供上下文，不应成为长期堆叠菜单；主导航承担模块切换。
5. UI 方向最终是：统一黑色顶部产品壳 + 项目选择；下层页面根据任务使用自己的工作区。不要机械复制申江海，而是学习其“项目、助手、素材、技能、执行结果同一上下文”的组织逻辑。

### 阶段 B：真实 Agent 与工具闭环

1. 用户反复询问 Agent 是否要做成类似 Pi Agent / Hermes Agent 的真实 Agent，并能连接 MCP、Skills。
2. 决策：当前优先做“真实可用的工作流 Agent / Agent Kernel”，不要先完整内嵌 Pi 或 Hermes。原因是项目的价值在建筑专业工具、项目数据、资产血缘和可恢复执行，不在重新实现一个通用终端 Agent。
3. 目标是把现有素材库、画布、Wiki、生图等能力注册成真实工具，让模型负责判断，代码负责路由、重试和确定性变换。
4. 用户指定 Agent 真实可用性验收标准：不是回复一段文字，而是在正确项目中留下真实画布、素材、任务历史和血缘，刷新后仍可查。
5. 验收任务设计：
   - 只读：读取并总结当前项目。
   - 轻写：在当前项目创建智能画布。
   - 完整设计：读取项目与素材 → 生成简报 → 生图 → 入库 → 加入画布 → 核对项目归属。
6. 后续做了稳定化 Sprint、核心工具补齐、项目上下文、质量门/replay、素材反馈闭环等工作。可从当前这些文件和测试继续核对：
   - `agent_runtime.py`
   - `agent_skills/architectural-concept-design/SKILL.md`
   - `agent_skills/knowledge-research/`
   - `tests/check_agent_kernel.py`
   - `tests/check_agent_runtime_api.py`
   - `tests/check_agent_skills_mcp.py`
   - `tests/check_agent_core_tools.py`
   - `tests/check_project_context_compiler.py`
   - `tests/check_quality_gate_replay.py`
   - `tests/check_material_feedback_loop.py`
7. “ArchLib case”是历史/测试案例或可选目录，不应在每次 Agent 回复中作为默认主角。缺少 ArchLib 目录时应明确为可选跳过，不要把它写进所有用户结果。

### 阶段 C：复杂度反思与简化

1. 用户问“我们是不是搞复杂了”。判断是：产品曾出现过把 Agent、工作台、抽屉、画布、技能、反馈等概念同时暴露给用户的问题。
2. 简化原则：用户只需要看到项目、任务、素材和结果；Kernel、MCP、Skills、血缘等属于系统能力，只在需要解释、排错或审计时出现。
3. 用户要求按建议简化，随后又要求回退上一版。后续 Agent 必须尊重当前实际代码和截图，不要凭历史描述再次重做 UI。

### 阶段 D：建筑强排 PPT

1. 用户提出学习申江海排版 AI/PPT 逻辑：用模板固化建筑强排 PPT，可替换彩总、鸟瞰、户型平面。
2. 用户提供 15 页真实 PPT 母版，并要求先做简单、真实可用版本。
3. 用户提供同事 Demo `largo166/0716` 作为功能和 UI 参考，并要求运行、直接做。
4. 产品取舍：V1 不做任意模板编辑器，不做通用 PPT Agent，不做工时；先固定六个语义槽，确保真实母版可替换、可导出、可持久化、可追溯。
5. 六个槽：
   - 彩总：第 2 页
   - 鸟瞰：第 7 页
   - 户型 A：第 12 页
   - 户型 B：第 13 页
   - 户型 C：第 14 页
   - 户型 D：第 15 页

## 4. PPT 工作台已经实现的内容

### 前端

新页面：`static/ppt-workbench.html`

- 统一产品顶部壳下面是 PPT 专业工作区。
- 模式：模板 / 编辑 / 校验。
- 左栏：六个替换页面和 READY 状态。
- 中栏：真实原始 PPT 页面预览、原图框位置和替换图叠加。
- 右栏：当前母版、槽位说明、项目素材、质量报告、导出历史。
- 支持上传母版、直接上传替换图、选择当前项目已有素材、导出 PPTX。
- 刷新后恢复当前 job、六槽分配、质量状态和历史导出。
- 项目工作台增加“建筑强排 PPT”入口。
- `static/studio-shell.js` 将 PPT 工作台归入项目管理模块。

### 后端与真实 PPT 处理

新模块：`ppt_workbench.py`

- 存储项目级 manifest，当前 schema version 2。
- 检查 PPTX 文件大小和解压后大小，避免异常压缩包。
- 读取真实 slide XML、关系 ID、媒体路径、shape ID、对象几何和旋转。
- 用 LibreOffice + `pdftoppm` 生成 15 页预览。
- 替换的是 PPTX zip 中真实 `ppt/media/...` 媒体，不是把整页转成截图，因此文字和其他版式仍可编辑。
- 不覆盖原始母版，导出独立 PPTX。
- 处理原始图片对象旋转：记录 `rotation_degrees`，写入前对替换媒体做逆向预旋转。
- 质量门检查模板、六槽分配、图片尺寸/比例和项目归属。

`main.py` 新增接口：

- `GET /api/projects/{project_id}/ppt-workbench`
- `GET /api/projects/{project_id}/ppt-workbench/{job_id}`
- `POST /api/projects/{project_id}/ppt-workbench/templates`
- `POST /api/projects/{project_id}/ppt-workbench/{job_id}/images`
- `PUT /api/projects/{project_id}/ppt-workbench/{job_id}/slots/{slot_id}`
- `POST /api/projects/{project_id}/ppt-workbench/{job_id}/export`
- `GET /api/projects/{project_id}/ppt-workbench/{job_id}/exports/{export_id}/download`

`domain_store.py` 相关修复：

- `register_asset` 支持可选 `kind`，默认仍为 image。
- `create_generation_task` 使用既有输入素材时，不再覆盖该素材原来的 title/source/provenance。
- 导出 PPT 会创建真实 generation task、输出 presentation asset 和 lineage edges。

### 真实验收数据

- 项目 ID：`project_590db9b73b224e1699b51088199e5abd`
- PPT job：`pptjob_8c6c9440628c43ecaacc8e5a88d86295`
- 正确最终 export：`export_47e77ed41c4448e1afef8f10b99ca36c`
- generation task：`generation_1d6f36501c864e71a42631af111cc611`
- 输出 asset：`asset_cc81c8a2f41b492796350e6ac737b912`
- 彩总使用素材标签：`image40`
- 鸟瞰使用素材标签：`image36`
- 户型 A-D 使用/复用素材标签：`image43`
- 质量结果：100/100，6/6 READY，0 error，0 warning。
- 输出记录有 7 条 upstream lineage（模板 + 六个槽输入）。

识别到的原始 shape：

- slide 2 / shape 6146
- slide 7 / shape 2
- slide 12 / shape 17
- slide 13 / shape 9
- slide 14 / shape 20
- slide 15 / shape 13

最终 PPT：

`assets/ppt_workbench/project_590db9b73b224e1699b51088199e5abd/pptjob_8c6c9440628c43ecaacc8e5a88d86295/exports/浙江保利杭州运河中地块立项方案-0720(1)-建筑强排-20260723-184214-33d438.pptx`

完整 job 数据在：

`assets/ppt_workbench/project_590db9b73b224e1699b51088199e5abd/pptjob_8c6c9440628c43ecaacc8e5a88d86295/`

其中包含 `manifest.json`、`template.pptx`、15 张 previews、替换图片和两个 exports。

## 5. PPT 实现中发现并修复的坑

1. 全局 shell 的间距最初使用 margin，发生 margin collapse，导致 PPT 本地工具栏被遮住。已改为 body top padding。
2. 第 14 页原始主图对象自带 270° 旋转。第一次导出图像方向错误；现已记录对象旋转并预旋转媒体，第二次导出正确。
3. 第一次导出曾暴露 `create_generation_task` 覆盖既有素材 provenance 的问题，已修复并增加测试。
4. 第 12 页存在 slide content overflow；对原始母版和导出文件检查结果相同，是母版遗留，不是本次替换造成。
5. 第一份错误/旧质量导出仍保留在历史中：`...-20260723-182535-05deb9.pptx`。不要把它当最终交付；最终是 18:42:14 那一份。
6. 本地端口可见不等于 HTTP 一定可用。此前 shell 内 `curl` 曾失败，但 in-app browser 可正常刷新；交接时应同时验证进程、真实页面 DOM 和浏览器控制台。

## 6. 验收与证据

测试文件：

- `tests/check_ppt_workbench.py`
- `tests/check_project_workbench.py`

覆盖内容：

- 15 页测试母版上传。
- 六个槽替换前失败、替换后通过。
- 真实 PPTX 导出和下载。
- project generation task 成功。
- presentation asset 类型正确。
- 7 条 upstream lineage。
- 输入素材 provenance 不被任务创建覆盖。
- 导出包中的目标媒体确实变化。

2026-07-23 最后一次已记录验收：

- 全部 `tests/check_*.py` 通过。
- 仅有 FastAPI/Starlette 弃用提示和 ArchLib 可选目录跳过提示。
- 浏览器刷新后 6/6 READY、100/100。
- 浏览器 console error 数量为 0。
- 原始输入 PPT 未修改。

证据文件：

- `.codex-ppt-workbench-v1-edit.png`
- `.codex-ppt-workbench-v1-qa.png`
- `.codex-ppt-workbench-source-vs-implementation.png`
- `design-qa.md` 中的 `Design QA — Architectural PPT workbench` 章节。

注意：这是 2026-07-23 的验收结果。下一位 Agent 修改代码后必须重新跑测试和页面验收，不能直接沿用“已通过”。

## 7. 当前 Git 与运行状态（2026-07-24 现场检查）

- 当前分支：`codex/improve-smart-canvas-workflows`
- 当前 HEAD：`e880f2f`
- HEAD 已与 `new-origin/codex/improve-smart-canvas-workflows` 对齐。
- 最近提交：`e880f2f build unified agent workspace and feedback loop`
- `new-origin`：`https://github.com/Leslie0Han/Infinite-Agent-Work-1.git`
- `origin`：`https://github.com/Leslie0Han/Infinite-Agent-Work.git`
- PPT 工作台改动目前没有单独 commit/push。
- 2026-07-24 检查时端口 3000 没有监听；需要重新启动服务。

启动命令：

```bash
cd '/Users/leslie/Documents/leslie/60_claude项目库/Infinite-Agent-Work-1'
.venv-macos/bin/python main.py
```

工作台 URL：

`http://127.0.0.1:3000/static/ppt-workbench.html?project_id=project_590db9b73b224e1699b51088199e5abd`

完整测试命令：

```bash
source .venv-macos/bin/activate
for check in tests/check_*.py; do python "$check" || exit 1; done
```

提交前至少再运行：

```bash
git diff --check -- ppt_workbench.py main.py domain_store.py static/ppt-workbench.html static/project-workbench.html static/studio-shell.js tests/check_ppt_workbench.py tests/check_project_workbench.py design-qa.md
```

## 8. 当前工作树边界

PPT 工作台直接相关改动：

- `ppt_workbench.py`（新）
- `static/ppt-workbench.html`（新）
- `tests/check_ppt_workbench.py`（新）
- `design-qa.md`（当前为未跟踪文件，但含多项历史 QA，不只 PPT）
- `main.py`（共享文件，含其他 Agent 功能改动）
- `domain_store.py`（共享文件）
- `static/project-workbench.html`
- `static/studio-shell.js`
- `tests/check_project_workbench.py`
- 三张 `.codex-ppt-workbench-*.png` 证据图。

工作树同时还有大量此前 Agent、素材库、画布、Wiki、审计和截图改动，例如：

- `agent_runtime.py`
- `agent_skills/...`
- `static/index.html`
- `static/library.html`
- 多个 `tests/check_agent_*.py`
- `tests/check_material_feedback_loop.py`
- `data/wiki/...`
- `reports/`
- 多张 `.codex-*.png`

这些都应视为用户已有工作。禁止：

- `git reset --hard`
- `git checkout -- .`
- `git add -A`
- 未审查就删除 screenshots、reports、runtime data 或 assets。

若要发布 PPT 功能，应逐文件检查 diff、显式暂存，并先确认目标远端是 `new-origin` 而不是 `origin`。

## 9. 关键产品判断摘要

这些是对话中影响实现的分析结论，不是逐字私有思维链：

1. 学申江海最重要的是“专业工作流被系统化”，不是复制黑白 UI。
2. 当前最缺的不是更大的通用 Agent，而是可复用的建筑专业闭环和真实数据沉淀。
3. Agent 先做可验证的 Kernel + tools；Pi/Hermes 仅在未来代码/电脑操作 Agent 需要更强 runtime 时再评估。
4. 任何 Agent 成功都必须以项目内副作用和持久化为标准：画布、素材、任务、血缘、刷新恢复。
5. 用户界面应隐藏 MCP、Skills、Kernel 等系统复杂度，让用户围绕项目、素材、结果操作。
6. PPT V1 固定六槽是有意的范围控制；先稳定成功路径，再做模板配置器。
7. PPT 导出必须保留真实母版和可编辑性，因此选择替换 OOXML 媒体，而不是截图拼页或重建整套版式。
8. 质量门、任务历史、血缘不是装饰，它们是让这个功能从脚本变成产品的关键。

## 10. 推荐下一步

优先做“模板槽位配置器”，不要立刻扩成通用 PPT Agent。

建议范围：

1. 用户上传任意 PPT 后，显示所有页面预览。
2. 用户在页面中选择图片对象，标记为彩总、鸟瞰、户型或自定义语义槽。
3. 保存一份可复用的模板定义，包括 slide number、shape id、relationship/media path、geometry、rotation、裁切策略和语义说明。
4. 新项目复用模板定义，只需批量匹配素材。
5. 仍沿用当前项目资产、任务、质量门、导出历史和 lineage，不另建第二套系统。
6. 验收至少覆盖：任意新模板、旋转对象、同图复用、多次导出、刷新恢复、错误回滚。

第二优先级才是：

- 文本字段替换（项目名、指标、日期等）。
- 户型数量动态化。
- 批量生成不同版本。
- Agent 根据项目素材自动推荐槽位图片，但最终由用户确认。
- 再评估是否让专门的代码/电脑操作 Agent 接 Pi/Hermes runtime。

## 11. 给下一位 Agent 的第一条建议指令

可以直接使用下面这段：

> 先阅读 `AGENT_HANDOFF_2026-07-24.md`，不要修改代码。核对当前 Git diff、项目 3000 端口、PPT manifest、最终导出文件和 `tests/check_ppt_workbench.py`。确认现状后，给我一份“已验证事实 / 与交接不一致之处 / 模板槽位配置器最小实现范围”的短报告，再开始编码。保护所有未提交工作，不要 `git add -A` 或重置工作树。

## 12. 完成定义

下一阶段只有同时满足以下条件才算完成：

- 新模板可以在 UI 中完成槽位标记。
- 槽位定义按项目或模板持久化。
- 替换后导出真实可编辑 PPTX。
- 任务、输出 asset、输入 lineage 完整。
- 刷新页面后可恢复。
- 对真实 PPT 渲染检查无新增旋转、裁切、越界问题。
- 全量测试通过，浏览器控制台无错误。
- 明确说明哪些文件准备提交，哪些仍是用户的其他未提交工作。

## 13. 用户请求索引（按对话顺序）

下面保留整个任务链的用户意图，便于新 Agent 知道每个决定从哪里来。相邻的“继续 / 下一步 / 直接做”均承接上一项，不应脱离上下文理解。

1. 根据 `reports/shenjianghai-ai-system-audit` 的分析内容仔细研究项目库，判断可以提升和借鉴的地方，目标是做成申江海那样。
2. 先做“第一阶段统一地基 + 上述纵向闭环”。
3. 询问下一步做什么。
4. 要求直接做。
5. 明确要求直接做“项目工作台”。
6. 询问整个 UI 是否可以学习申江海 AI App。
7. 提供申江海视频、DOCX 转写稿和多张截图，要求分析各页面按钮、按钮功能、转写稿和视频位置。
8. 要求直接做，但工时模块不做。
9. 询问左侧抽屉里的 Agent、图像工具、资源库等是否还有存在必要。
10. 要求直接按建议处理，并在完成后推荐下一步。
11. 询问助手 Agent 是否要做成真实 Agent，类似内置 Pi Agent 或 Hermes Agent，以连接 MCP 和 Skills。
12. 询问下一步建议。
13. 询问能否吸收市面 Agent 的结构，以及“把素材库、画布、Wiki、生图注册成真正可调用工具”是否仍属于工作流 Agent。
14. 同意并要求直接开干。
15. 要求直接进行下一步。
16. 要求继续。
17. 要求重新梳理上下文，检查做过的内容是否不到位、是否有坑。
18. 同意直接做“稳定化 Sprint”。
19. 要求根据上下文判断与申江海相比最缺什么。
20. 询问是否应该先做功能再处理细节，特别是 Agent 当前是否可用。
21. 明确要求做“Agent 真实可用性验收 Sprint”，成功标准是正确项目中留下真实画布、素材、任务历史、血缘，刷新后仍可查。
22. 对所需操作表示允许。
23. 询问现在是否按不同项目区分生图和其他分类，并要求参考别人做法。
24. 询问下一步推荐。
25. 同意按建议实施。
26. 明确要求开始做素材反馈闭环。
27. 询问 ArchLib case 是什么，为什么每次回复都有它。
28. 表示 Chrome 打不开，要求重启项目。
29. 要求把项目更新到 GitHub。
30. 回复“好了”。
31. 引用此前的完整路线图，追问 Agent 真实可用性验收、核心工具、统一 Agent、数据飞轮、视觉细节目前完成了多少。
32. 要求按建议继续。
33. 要求进行下一步。
34. 询问是否需要学习 Hermes 或 Pi Agent 的模式。
35. 同意并要求下一步。
36. 要求再次仔细研究申江海内容与项目，判断应该怎么学习。
37. 询问下一步需要做什么。
38. 连续要求进行下一步。
39. 在项目工作台页面提出“我们是不是搞复杂了”，要求仔细研判。
40. 要求按建议简化。
41. 随后要求回退上一版。
42. 提出建筑强排 PPT 功能：学习申江海排版 AI/PPT 逻辑，用模板固化并替换彩总、鸟瞰等。
43. 提供 `浙江保利杭州运河中地块立项方案-0720(1).pptx`，要求先分析做一个简单的彩总、鸟瞰、户型替换版本。
44. 提供同事 Demo `https://github.com/largo166/0716`，要求研究可取点并参考 UI。
45. 要求运行。
46. 要求直接做。
47. 当前请求：把整个对话全部整理出来，包括分析，交给另一个 Agent 继续工作。

### 对话中出现过的主要本地页面

- 智能画布：`/static/smart-canvas.html?id=c4d7c864135a4dd597630ba58443a374`
- 素材库：`/?page=library`
- AI 生图工作台：`/?page=workbench`
- 项目工作台：`/static/project-workbench.html?project_id=project_590db9b73b224e1699b51088199e5abd`
- 建筑强排 PPT：`/static/ppt-workbench.html?project_id=project_590db9b73b224e1699b51088199e5abd`

这些 URL 只描述对话中的页面状态；打开前仍需确认本地服务正在运行。
