---
id: knowledge-research
name: 知识研究与沉淀
description: 读取当前项目，检索 Wiki 依据，将研究、总结、学习或问答结果写入可追溯的知识页面。
version: 1.0.0
enabled: true
task_types: ["wiki_task", "work_task"]
scopes: ["home", "wiki"]
allowed_tools: ["get_project_context", "search_wiki_context", "write_wiki_qa", "write_agent_report"]
mcp_servers: []
---
# 知识研究与沉淀 Skill

你在执行一个项目内的知识任务。必须先建立项目边界和本地依据，再写入结论。

## 执行原则

1. 先调用 `get_project_context` 确认当前项目及其产物概况。
2. 调用 `search_wiki_context` 检索与目标直接相关的本地来源，不得伪造引用。
3. 研究、总结、学习任务使用 `write_agent_report`；明确问答任务使用 `write_wiki_qa`。
4. 写入内容必须区分已有依据、分析结论和待补信息。Wiki 无命中时要明确说明，不能把推断写成事实。
5. 结束时返回 Wiki 页面标识、标题、当前项目和未完成项。

## 完成标准

- 项目上下文已读取。
- Wiki 已实际检索。
- 结果已写入真实 Wiki 页面，且保留关联来源。
- 任务历史可追溯到当前项目。
