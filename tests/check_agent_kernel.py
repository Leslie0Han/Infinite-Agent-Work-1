import asyncio
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_kernel import AgentLoop, ToolPermissionError, ToolRegistry, ToolSpec
from agent_runtime import build_plan


def object_schema(properties=None, required=None):
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


async def exercise_agent_loop():
    registry = ToolRegistry()

    async def read_context(arguments, execution):
        execution.state["project"] = {"id": arguments["project_id"]}
        return execution.state["project"]

    async def save_output(arguments, execution):
        execution.state["saved"] = arguments["title"]
        return {"saved": True, "title": arguments["title"]}

    registry.register(ToolSpec(
        "project.get_context",
        "Read project context",
        object_schema({"project_id": {"type": "string"}}, ["project_id"]),
        read_context,
        permissions=["project:read"],
        scopes=["home"],
    ))
    registry.register(ToolSpec(
        "project.save_output",
        "Save project output",
        object_schema({"title": {"type": "string"}}, ["title"]),
        save_output,
        writes=True,
        permissions=["project:write"],
        scopes=["home"],
    ))

    async def planner(payload):
        if not payload["history"]:
            return {
                "action": "tool",
                "tool": "project.get_context",
                "arguments": {"project_id": "project_test"},
                "reason": "need context",
            }
        if not payload["state"].get("saved"):
            return {
                "action": "tool",
                "tool": "project.save_output",
                "arguments": {"title": "first result"},
                "reason": "persist result",
            }
        return {"action": "finish", "answer": "done"}

    events = []
    result = await AgentLoop(registry, planner, max_steps=5).run(
        "build a project output",
        {"project_id": "project_test"},
        allowed_tools=["project.get_context", "project.save_output"],
        allow_writes=True,
        granted_permissions=["project:read", "project:write"],
        current_scope="home",
        required_tools=["project.get_context", "project.save_output"],
        on_event=events.append,
    )
    assert result["status"] == "succeeded"
    assert result["state"]["saved"] == "first result"
    assert [item["tool"] for item in result["history"]] == ["project.get_context", "project.save_output"]
    assert [item["type"] for item in events] == [
        "tool_started", "tool_completed", "tool_started", "tool_completed", "agent_finished"
    ]

    denied = False
    try:
        await registry.execute(
            "project.save_output",
            {"title": "blocked"},
            type("Context", (), {"goal": "", "context": {}, "state": {}, "history": [], "step": 1})(),
            allow_writes=False,
            granted_permissions=["project:read"],
            current_scope="home",
        )
    except ToolPermissionError:
        denied = True
    assert denied, "write tools must require an explicitly confirmed run"

    scope_denied = False
    try:
        await registry.execute(
            "project.get_context",
            {"project_id": "project_test"},
            type("Context", (), {"goal": "", "context": {}, "state": {}, "history": [], "step": 1})(),
            allow_writes=False,
            granted_permissions=["project:read"],
            current_scope="library",
        )
    except ToolPermissionError:
        scope_denied = True
    assert scope_denied, "tool scopes must be enforced by the runtime"

    contract_events = []
    contract_calls = 0

    async def contract_planner(payload):
        nonlocal contract_calls
        contract_calls += 1
        if contract_calls == 1:
            return {"action": "finish", "answer": "too early"}
        if not any(item.get("tool") == "project.get_context" for item in payload["history"]):
            return {"action": "tool", "tool": "project.get_context", "arguments": {"project_id": "project_test"}}
        return {"action": "finish", "answer": "contract satisfied"}

    contract_result = await AgentLoop(registry, contract_planner, max_steps=4).run(
        "inspect project",
        allowed_tools=["project.get_context"],
        required_tools=["project.get_context"],
        granted_permissions=["project:read"],
        current_scope="home",
        on_event=contract_events.append,
    )
    assert contract_result["status"] == "succeeded"
    assert any(event["type"] == "completion_blocked" for event in contract_events)

    async def fail_output(arguments, execution):
        raise RuntimeError("upstream unavailable")

    registry.register(ToolSpec(
        "project.fail_output",
        "Fail output",
        object_schema(),
        fail_output,
        writes=True,
        permissions=["project:write"],
        scopes=["home"],
    ))
    partial_calls = 0

    async def partial_planner(payload):
        nonlocal partial_calls
        partial_calls += 1
        if partial_calls == 1:
            return {"action": "tool", "tool": "project.fail_output", "arguments": {}}
        return {"action": "finish", "answer": "saved the usable part"}

    partial_result = await AgentLoop(registry, partial_planner, max_steps=3).run(
        "attempt output",
        allowed_tools=["project.fail_output"],
        allow_writes=True,
        required_tools=["project.fail_output"],
        granted_permissions=["project:write"],
        current_scope="home",
    )
    assert partial_result["status"] == "partial"
    assert partial_result["missing_tools"] == ["project.fail_output"]


def check_agent_kernel_contract():
    asyncio.run(exercise_agent_loop())
    plan = build_plan(
        "为当前项目生成一版建筑立面方向并整理到画布",
        "home",
        {"mode": "design", "task_type": "design_task"},
    )
    assert plan["runtime"] == "tool_calling_v1"
    assert plan["max_steps"] >= 8
    assert "get_project_context" in plan["tool_ids"]
    assert "generate_design_image" in plan["tool_ids"]
    assert "save_design_output" in plan["tool_ids"]
    assert plan["requires_confirmation"] is True
    assert "get_project_context" in plan["required_tools"]

    read_only_plan = build_plan(
        "读取并检查当前项目摘要，只读取项目上下文，不创建或修改任何内容",
        "home",
        {"mode": "design", "task_type": "design_task"},
    )
    assert read_only_plan["tool_ids"] == ["get_project_context"]
    assert read_only_plan["required_tools"] == ["get_project_context"]
    assert read_only_plan["requires_confirmation"] is False

    canvas_only_plan = build_plan(
        "在当前项目创建一个验收画布，只创建画布，不生图",
        "home",
        {"mode": "design", "task_type": "design_task"},
    )
    assert canvas_only_plan["tool_ids"] == ["get_project_context", "create_smart_canvas", "link_project_output"]
    assert canvas_only_plan["required_tools"] == ["get_project_context", "create_smart_canvas"]
    assert "generate_design_image" not in canvas_only_plan["tool_ids"]


if __name__ == "__main__":
    check_agent_kernel_contract()
    print("agent kernel checks passed")
