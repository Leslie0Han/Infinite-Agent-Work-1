import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_kernel import ToolExecutionContext, ToolRegistry
from agent_skills import SkillRegistry
from agent_runtime import build_plan
from mcp_gateway import MCPGateway, MCPServerConfig


async def exercise_mcp_gateway():
    gateway = MCPGateway([
        MCPServerConfig(
            id="project-reader",
            name="Project Reader",
            command=sys.executable,
            args=[str(ROOT / "mcp_servers" / "project_reader.py")],
            cwd=str(ROOT),
            env={"IAW_MCP_WORKSPACE": str(ROOT)},
            read_only=True,
        )
    ])
    discovered = await gateway.discover_tools()
    assert len(discovered) == 1
    assert discovered[0]["name"] == "mcp.project_reader.workspace_summary"
    assert discovered[0]["writes"] is False
    assert discovered[0]["permissions"] == ["mcp:project-reader:read"]

    summary = await gateway.call_tool("project-reader", "workspace_summary", {"max_files": 5})
    assert summary["read_only"] is True
    assert summary["workspace"] == ROOT.name
    assert len(summary["sample_files"]) <= 5
    assert summary["total_files"] >= len(summary["sample_files"])

    registry = ToolRegistry()
    registered = await gateway.register_tools(registry, ["project-reader"])
    assert len(registered) == 1
    result = await registry.execute(
        "mcp.project_reader.workspace_summary",
        {"max_files": 3},
        ToolExecutionContext(goal="inspect workspace", context={}, state={}, history=[], step=1),
        allow_writes=False,
    )
    assert result["read_only"] is True
    assert len(result["sample_files"]) <= 3
    health = await gateway.health("project-reader")
    assert health["status"] == "connected"
    assert health["tool_count"] == 1


def check_agent_skills_and_mcp_contract():
    skills = SkillRegistry(str(ROOT / "agent_skills"))
    resolved = skills.resolve(task_type="design_task", scope="home")
    assert len(resolved) == 1
    skill = resolved[0]
    assert skill["id"] == "architectural-concept-design"
    assert skill["enabled"] is True
    assert "mcp.project_reader.workspace_summary" in skill["allowed_tools"]
    assert skill["mcp_servers"] == ["project-reader"]
    assert "完成标准" in skill["instructions"]
    plan = build_plan("生成建筑概念方案", "home", {"mode": "design", "task_type": "design_task"})
    assert set(plan["tool_ids"]).issubset(set(skill["allowed_tools"]))
    knowledge = skills.resolve(task_type="wiki_task", scope="home")
    assert len(knowledge) == 1
    assert knowledge[0]["id"] == "knowledge-research"
    assert "write_agent_report" in knowledge[0]["allowed_tools"]
    knowledge_plan = build_plan("研究当前知识并生成报告", "home", {"mode": "research"})
    assert set(knowledge_plan["tool_ids"]).issubset(set(knowledge[0]["allowed_tools"]))
    asyncio.run(exercise_mcp_gateway())


if __name__ == "__main__":
    check_agent_skills_and_mcp_contract()
    print("agent skills and MCP checks passed")
