---
id: architectural-concept-design
name: 建筑概念设计
description: 把项目上下文、本地工作区、Wiki、素材、生图和智能画布组成可追溯的概念设计闭环。
version: 1.0.0
enabled: true
task_types: ["design_task"]
scopes: ["home", "library", "smart-canvas", "wiki"]
allowed_tools: ["get_project_context", "mcp.project_reader.workspace_summary", "search_wiki_context", "list_library_images", "tag_library_images", "generate_design_brief", "generate_design_image", "create_smart_canvas", "append_images_to_smart_canvas", "read_smart_canvas", "save_canvas_node_images_to_library", "write_wiki_qa", "save_design_output", "link_project_output"]
mcp_servers: ["project-reader"]
---
# 建筑概念设计 Skill

你在执行一个建筑概念设计任务。先建立依据，再生成方向，最后将产物回存到当前项目。

## 执行原则

1. 先调用 `get_project_context` 读取当前项目、画布、素材和生成任务。
2. 需要理解代码库/工作区结构时，调用只读的 `mcp.project_reader.workspace_summary`。
3. 读取项目偏好画像，按目标检索 Wiki 和素材库；优先参考已采纳/高评分素材，对已淘汰/低评分素材降权。
4. 生成设计简报，明确设计目标、依据、方向、正向提示词和负向提示词。
5. 生图是写入/消耗型操作，只能在用户确认计划后执行。
6. 生图成功时，把结果回存资源库并放入智能画布；生图失败时，保留简报和可操作的错误原因。
7. 结束前调用 `link_project_output` 核对项目归属，返回 Wiki、资源库、画布和项目入口。

## 完成标准

- 用户能看到一份已保存的设计简报。
- 生图成功时，用户能打开包含结果的智能画布。
- 任何上游失败都必须显示原因，不能声称已成功。
- 所有产物都必须归属当前项目。
- 设计简报必须说明是否使用了项目历史偏好；候选 Skill 未审核前不得当作正式规则。
