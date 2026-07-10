import json
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional


AGENT_TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "id": "list_library_images",
        "name": "读取资源库图片",
        "description": "按筛选条件读取当前资源库图片列表，用于筛图、打标签或送入画布。",
        "writes": False,
        "scopes": ["library"],
    },
    {
        "id": "tag_library_images",
        "name": "批量标注资源库图片",
        "description": "为资源库图片批量生成标签和分类建议。",
        "writes": True,
        "scopes": ["library"],
    },
    {
        "id": "create_smart_canvas",
        "name": "创建智能画布",
        "description": "新建一个智能画布草稿，作为后续插图和生成的承载容器。",
        "writes": True,
        "scopes": ["home", "library", "smart-canvas"],
    },
    {
        "id": "append_images_to_smart_canvas",
        "name": "插入图片到智能画布",
        "description": "将资源库图片或参考图插入智能画布中，形成参考节点。",
        "writes": True,
        "scopes": ["library", "smart-canvas"],
    },
    {
        "id": "read_smart_canvas",
        "name": "读取智能画布摘要",
        "description": "读取当前智能画布的节点、连接和选中内容，供助手分析。",
        "writes": False,
        "scopes": ["smart-canvas"],
    },
    {
        "id": "save_canvas_node_images_to_library",
        "name": "画布结果回存资源库",
        "description": "把智能画布中的结果图片批量存回资源库。",
        "writes": True,
        "scopes": ["smart-canvas"],
    },
    {
        "id": "list_workflows",
        "name": "读取工作流列表",
        "description": "查看当前可用的 ComfyUI 工作流模板和配置。",
        "writes": False,
        "scopes": ["home", "smart-canvas"],
    },
    {
        "id": "suggest_generation_route",
        "name": "推荐生成路线",
        "description": "根据目标推荐走在线 API、ModelScope 还是 ComfyUI。",
        "writes": False,
        "scopes": ["home", "library", "smart-canvas"],
    },
    {
        "id": "search_wiki_context",
        "name": "检索 LLM Wiki",
        "description": "先检索本地知识库中的来源、摘要、概念和问答档案，作为 Agent 的长期上下文。",
        "writes": False,
        "scopes": ["home", "wiki", "library", "smart-canvas"],
    },
    {
        "id": "compile_wiki_source",
        "name": "编译 Wiki 来源",
        "description": "把原始资料编译为摘要、概念和索引关系。",
        "writes": True,
        "scopes": ["wiki", "home"],
    },
    {
        "id": "write_wiki_qa",
        "name": "写入问答档案",
        "description": "根据知识库上下文回答问题，并把结果保存为 Q&A 档案。",
        "writes": True,
        "scopes": ["wiki", "home"],
    },
    {
        "id": "write_agent_report",
        "name": "生成工作报告",
        "description": "把检索到的知识库上下文整理为报告、方案或学习材料。",
        "writes": True,
        "scopes": ["wiki", "home"],
    },
    {
        "id": "generate_design_brief",
        "name": "生成设计简报",
        "description": "结合 Wiki 和资源库上下文生成设计方向、依据和生图提示词。",
        "writes": True,
        "scopes": ["home", "wiki", "library", "smart-canvas"],
    },
    {
        "id": "generate_design_image",
        "name": "调用在线生图",
        "description": "使用现有在线 API 生图能力生成设计结果。",
        "writes": True,
        "scopes": ["home", "wiki", "library", "smart-canvas"],
    },
    {
        "id": "save_design_output",
        "name": "保存设计产物",
        "description": "把设计简报和可用图片结果保存到 Wiki、资源库和智能画布。",
        "writes": True,
        "scopes": ["home", "wiki", "library", "smart-canvas"],
    },
    {
        "id": "code_agent_placeholder",
        "name": "代码智能体入口",
        "description": "代码模式第一版只保留入口，后续再接 Pi Coding Agent。",
        "writes": False,
        "scopes": ["home"],
    },
]

PAGE_ROLE_ALIASES = {
    "library": "library",
    "wiki": "wiki",
    "knowledge": "wiki",
    "canvas": "smart-canvas",
    "smart-canvas": "smart-canvas",
    "home": "home",
    "zimage": "home",
    "enhance": "home",
    "klein": "home",
    "angle": "home",
    "online": "home",
    "gpt-chat": "home",
    "api-settings": "home",
    "comfyui-settings": "home",
}

PAGE_LABELS = {
    "library": "资源库",
    "wiki": "知识库",
    "smart-canvas": "智能画布",
    "home": "首页工作台",
}

FINAL_TASK_STATUSES = {"succeeded", "failed", "cancelled"}


def now_ms() -> int:
    return int(time.time() * 1000)


def ensure_agent_task_dir(task_dir: str) -> None:
    os.makedirs(task_dir, exist_ok=True)


def list_agent_tools() -> List[Dict[str, Any]]:
    return [dict(tool) for tool in AGENT_TOOL_DEFINITIONS]


def resolve_page_role(page: str) -> str:
    key = str(page or "").strip().lower()
    return PAGE_ROLE_ALIASES.get(key, "home")


def page_label(page_role: str) -> str:
    return PAGE_LABELS.get(page_role, PAGE_LABELS["home"])


def task_path(task_dir: str, task_id: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", task_id or "")
    if not cleaned:
        raise ValueError("无效的 Agent 任务 ID")
    return os.path.join(task_dir, f"{cleaned}.json")


def save_task(task_dir: str, task: Dict[str, Any]) -> Dict[str, Any]:
    ensure_agent_task_dir(task_dir)
    task["updated_at"] = now_ms()
    with open(task_path(task_dir, task["id"]), "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)
    return task


def load_task(task_dir: str, task_id: str) -> Dict[str, Any]:
    path = task_path(task_dir, task_id)
    if not os.path.exists(path):
        raise FileNotFoundError(task_id)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def cancel_task(task_dir: str, task_id: str) -> Dict[str, Any]:
    task = load_task(task_dir, task_id)
    if task.get("status") in FINAL_TASK_STATUSES:
        return task
    task["status"] = "cancelled"
    task["current_step"] = None
    task.setdefault("events", []).append({
        "type": "cancelled",
        "message": "任务已取消",
        "at": now_ms(),
    })
    return save_task(task_dir, task)


def summarize_context(page_role: str, context: Dict[str, Any]) -> List[str]:
    context = context or {}
    parts: List[str] = []
    if page_role == "library":
        selected_count = int(context.get("selected_count") or 0)
        source_name = str(context.get("source_name") or "").strip()
        query = str(context.get("query") or "").strip()
        if selected_count:
            parts.append(f"当前选中 {selected_count} 张图片")
        if source_name:
            parts.append(f"来源：{source_name}")
        if query:
            parts.append(f"搜索词：{query}")
    elif page_role == "smart-canvas":
        canvas_title = str(context.get("canvas_title") or "").strip()
        selected_nodes_count = int(context.get("selected_nodes_count") or 0)
        if canvas_title:
            parts.append(f"当前画布：{canvas_title}")
        if selected_nodes_count:
            parts.append(f"当前选中 {selected_nodes_count} 个节点")
    else:
        active_page = str(context.get("active_page") or "").strip()
        if active_page:
            parts.append(f"当前入口：{active_page}")
    if not parts:
        parts.append(f"当前入口：{page_label(page_role)}")
    return parts


def default_goal_for_page(page_role: str) -> str:
    if page_role == "library":
        return "整理当前资源库结果并给出下一步建议"
    if page_role == "wiki":
        return "根据当前知识库生成一个可执行的问答或整理计划"
    if page_role == "smart-canvas":
        return "整理当前智能画布并给出下一步建议"
    return "根据当前目标生成一个可执行的工作台计划"


def select_tool(tool_id: str) -> Dict[str, Any]:
    for tool in AGENT_TOOL_DEFINITIONS:
        if tool["id"] == tool_id:
            return dict(tool)
    raise ValueError(f"未知工具：{tool_id}")


def step_label_map() -> Dict[str, str]:
    return {tool["id"]: tool["name"] for tool in AGENT_TOOL_DEFINITIONS}


def infer_agent_mode(goal: str, context: Dict[str, Any]) -> str:
    raw = str((context or {}).get("mode") or (context or {}).get("agent_mode") or "").strip().lower()
    aliases = {
        "design": "design",
        "summary": "summary",
        "summarize": "summary",
        "research": "research",
        "inspiration": "inspiration",
        "deep": "deep",
        "learn": "learn",
        "learning": "learn",
        "code": "code",
        "wiki": "wiki",
    }
    if raw in aliases:
        return aliases[raw]
    text = str(goal or "")
    if any(token in text for token in ["生图", "出图", "设计", "方案", "效果图", "参考图", "画布"]):
        return "design"
    if any(token in text for token in ["总结", "摘要", "整理"]):
        return "summary"
    if any(token in text for token in ["研究", "调研", "资料", "知识库", "问答"]):
        return "research"
    if any(token in text for token in ["代码", "项目", "编程", "bug", "实现"]):
        return "code"
    return "research"


def task_type_for_mode(mode: str, page_role: str) -> str:
    if mode == "design" or page_role in {"library", "smart-canvas"}:
        return "design_task"
    if mode in {"wiki", "research", "deep", "learn"} or page_role == "wiki":
        return "wiki_task"
    return "work_task"


def build_plan(goal: str, page: str, context: Dict[str, Any]) -> Dict[str, Any]:
    page_role = resolve_page_role(page)
    context = context or {}
    raw_goal = re.sub(r"\s+", " ", str(goal or "")).strip()
    final_goal = raw_goal or default_goal_for_page(page_role)
    lc_goal = final_goal.lower()
    intent = str(context.get("intent") or "").strip().lower()
    mode = infer_agent_mode(final_goal, context)
    task_type = task_type_for_mode(mode, page_role)
    requested_task_type = str(context.get("task_type") or "").strip()
    if requested_task_type in {"design_task", "work_task", "wiki_task"}:
        task_type = requested_task_type
    blockers: List[str] = []
    outputs: List[Dict[str, Any]] = []
    used_context = ["当前页面上下文"]

    if page_role == "library":
        task_type = "design_task"
        mode = "design"
        if intent == "batch_tag":
            tool_ids = ["list_library_images", "tag_library_images"]
            plan_title = "资源库批量标注计划"
            output_target = "为当前资源库结果生成分类和标签，并批量写回图片记录"
            outputs = [{"type": "library_tags", "label": "资源库标签"}]
        elif intent == "send_to_smart_canvas":
            tool_ids = ["list_library_images", "create_smart_canvas", "append_images_to_smart_canvas"]
            plan_title = "资源库送入智能画布计划"
            output_target = "创建一个新的智能画布草稿，并把当前图片送入画布"
            outputs = [{"type": "canvas", "label": "智能画布"}]
        elif any(token in final_goal for token in ["标签", "标注", "分类"]):
            tool_ids = ["list_library_images", "tag_library_images"]
            plan_title = "资源库批量标注计划"
            output_target = "生成标签和分类建议，必要时批量写回资源库"
            outputs = [{"type": "library_tags", "label": "资源库标签"}]
        elif any(token in final_goal for token in ["画布", "参考图", "送进", "插入"]):
            tool_ids = ["list_library_images", "create_smart_canvas", "append_images_to_smart_canvas"]
            plan_title = "资源库送入智能画布计划"
            output_target = "创建一个新的智能画布草稿，并把当前图片送入画布"
            outputs = [{"type": "canvas", "label": "智能画布"}]
        else:
            tool_ids = ["list_library_images", "suggest_generation_route"]
            plan_title = "资源库整理与路线建议"
            output_target = "总结当前资源情况，并推荐后续生成路线"
            outputs = [{"type": "advice", "label": "路线建议"}]
    elif page_role == "smart-canvas":
        task_type = "design_task"
        mode = "design"
        if intent == "save_to_library" or any(token in final_goal for token in ["入库", "回存", "保存到资源库"]):
            tool_ids = ["read_smart_canvas", "save_canvas_node_images_to_library"]
            plan_title = "智能画布结果回存计划"
            output_target = "读取当前画布结果，并准备把选中的输出回存到资源库"
            outputs = [{"type": "library_images", "label": "资源库图片"}]
        else:
            tool_ids = ["read_smart_canvas", "list_workflows", "suggest_generation_route"]
            plan_title = "智能画布整理计划"
            output_target = "分析当前画布结构，并给出补节点、补路线或补工作流建议"
            outputs = [{"type": "advice", "label": "画布整理建议"}]
    elif mode == "code":
        task_type = "work_task"
        tool_ids = ["code_agent_placeholder"]
        plan_title = "代码智能体入口计划"
        output_target = "代码模式第一版只作为入口，后续接入 Pi Coding Agent 后执行。"
        blockers = ["代码模式暂未接入 Pi Coding Agent；当前只保留入口和任务记录。"]
        outputs = [{"type": "placeholder", "label": "代码智能体入口"}]
    elif task_type == "design_task":
        used_context = ["LLM Wiki", "资源库图片", "提示词库", "当前页面上下文"]
        tool_ids = ["search_wiki_context", "list_library_images", "generate_design_brief", "generate_design_image", "save_design_output"]
        plan_title = "设计 Agent 出图计划"
        output_target = "生成设计依据、提示词和图片结果，并保存到资源库 / Wiki / 智能画布。"
        outputs = [
            {"type": "wiki_design", "label": "设计简报"},
            {"type": "library_images", "label": "资源库图片"},
            {"type": "canvas", "label": "智能画布"},
        ]
    elif task_type == "wiki_task":
        used_context = ["LLM Wiki 来源", "摘要", "概念", "问答档案"]
        tool_ids = ["search_wiki_context", "write_wiki_qa"]
        plan_title = "知识库问答计划"
        output_target = "检索 LLM Wiki，生成回答并保存为 Q&A 档案。"
        outputs = [{"type": "wiki_qa", "label": "问答档案"}]
    else:
        used_context = ["LLM Wiki", "问答档案", "当前页面上下文"]
        tool_ids = ["search_wiki_context", "write_agent_report"]
        plan_title = "工作 Agent 资料整理计划"
        output_target = "检索知识库并生成总结、研究报告、学习材料或方案文档。"
        outputs = [{"type": "wiki_report", "label": "工作报告"}]

    tools = [select_tool(tool_id) for tool_id in tool_ids]
    requires_confirmation = any(tool["writes"] for tool in tools)
    steps = []
    for index, tool in enumerate(tools, start=1):
        steps.append({
            "id": f"step_{index}",
            "title": tool["name"],
            "description": tool["description"],
            "tool_id": tool["id"],
            "writes": tool["writes"],
        })

    return {
        "title": plan_title,
        "summary": f"围绕“{final_goal}”生成一份 {page_label(page_role)} 可执行计划。",
        "page_role": page_role,
        "context_summary": summarize_context(page_role, context),
        "requires_confirmation": requires_confirmation,
        "can_run": not blockers,
        "blockers": blockers,
        "preview": None,
        "output_target": output_target,
        "task_type": task_type,
        "mode": mode,
        "used_context": used_context,
        "outputs": outputs,
        "steps": steps,
        "tool_ids": tool_ids,
    }


def create_plan_task(task_dir: str, goal: str, page: str, context: Dict[str, Any]) -> Dict[str, Any]:
    page_role = resolve_page_role(page)
    plan = build_plan(goal, page_role, context or {})
    timestamp = now_ms()
    task = {
        "id": f"agent_{uuid.uuid4().hex}",
        "status": "planned",
        "goal": re.sub(r"\s+", " ", str(goal or "")).strip() or default_goal_for_page(page_role),
        "page": str(page or "").strip() or page_role,
        "page_role": page_role,
        "task_type": plan.get("task_type", "work_task"),
        "mode": plan.get("mode", ""),
        "context": context or {},
        "plan": plan,
        "outputs": plan.get("outputs") or [],
        "current_step": None,
        "progress_current": 0,
        "progress_total": 0,
        "events": [
            {
                "type": "planned",
                "message": "已生成任务计划，等待用户确认后执行。",
                "at": timestamp,
            }
        ],
        "result": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    return save_task(task_dir, task)


def update_task_plan(
    task_dir: str,
    task_id: str,
    *,
    preview: Optional[Dict[str, Any]] = None,
    blockers: Optional[List[str]] = None,
    can_run: Optional[bool] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    task = load_task(task_dir, task_id)
    plan = task.setdefault("plan", {})
    if preview is not None:
        plan["preview"] = preview
    if blockers is not None:
        plan["blockers"] = list(blockers)
    if can_run is not None:
        plan["can_run"] = bool(can_run)
    if context is not None:
        task["context"] = context
    return save_task(task_dir, task)


def start_task(
    task_dir: str,
    task_id: str,
    *,
    message: str = "任务开始执行。",
    step_id: str = "",
    progress_current: int = 0,
    progress_total: int = 0,
) -> Dict[str, Any]:
    task = load_task(task_dir, task_id)
    if task.get("status") in FINAL_TASK_STATUSES:
        return task
    task["status"] = "running"
    task["current_step"] = {
        "id": step_id,
        "title": step_label_map().get(step_id, step_id or ""),
        "message": message,
    } if step_id else None
    task["progress_current"] = max(0, int(progress_current or 0))
    task["progress_total"] = max(0, int(progress_total or 0))
    task.setdefault("events", []).append({
        "type": "running",
        "message": message,
        "step_id": step_id,
        "at": now_ms(),
    })
    return save_task(task_dir, task)


def update_task_progress(
    task_dir: str,
    task_id: str,
    *,
    step_id: str = "",
    message: str = "",
    progress_current: Optional[int] = None,
    progress_total: Optional[int] = None,
    event_type: str = "progress",
) -> Dict[str, Any]:
    task = load_task(task_dir, task_id)
    if task.get("status") in FINAL_TASK_STATUSES:
        return task
    if step_id or message:
        task["current_step"] = {
            "id": step_id,
            "title": step_label_map().get(step_id, step_id or ""),
            "message": message or task.get("current_step", {}).get("message", ""),
        }
    if progress_current is not None:
        task["progress_current"] = max(0, int(progress_current))
    if progress_total is not None:
        task["progress_total"] = max(0, int(progress_total))
    task.setdefault("events", []).append({
        "type": event_type,
        "message": message or "任务进度已更新。",
        "step_id": step_id,
        "progress_current": task.get("progress_current", 0),
        "progress_total": task.get("progress_total", 0),
        "at": now_ms(),
    })
    return save_task(task_dir, task)


def task_is_cancelled(task_dir: str, task_id: str) -> bool:
    try:
        task = load_task(task_dir, task_id)
    except FileNotFoundError:
        return True
    return task.get("status") == "cancelled"


def update_task_result(task_dir: str, task_id: str, result: Dict[str, Any], message: str = "") -> Dict[str, Any]:
    task = load_task(task_dir, task_id)
    if task.get("status") == "cancelled":
        return task
    task["status"] = "succeeded"
    task["result"] = result
    task["current_step"] = None
    task.setdefault("events", []).append({
        "type": "succeeded",
        "message": message or "任务执行完成。",
        "progress_current": task.get("progress_current", 0),
        "progress_total": task.get("progress_total", 0),
        "at": now_ms(),
    })
    return save_task(task_dir, task)


def fail_task(task_dir: str, task_id: str, message: str) -> Dict[str, Any]:
    task = load_task(task_dir, task_id)
    if task.get("status") == "cancelled":
        return task
    task["status"] = "failed"
    task["result"] = {"error": message}
    task["current_step"] = None
    task.setdefault("events", []).append({
        "type": "failed",
        "message": message,
        "progress_current": task.get("progress_current", 0),
        "progress_total": task.get("progress_total", 0),
        "at": now_ms(),
    })
    return save_task(task_dir, task)
