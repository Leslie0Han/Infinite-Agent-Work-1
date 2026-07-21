import tempfile
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

import main
from domain_store import DomainStore


def check_agent_runtime_api_contract():
    with tempfile.TemporaryDirectory() as temp_dir:
        main.AGENT_TASK_DIR = str(Path(temp_dir) / "agent_tasks")
        main.DOMAIN_STORE = DomainStore(str(Path(temp_dir) / "domain.db"))
        project = main.DOMAIN_STORE.ensure_default_project()
        linked_project = main.DOMAIN_STORE.create_project("关联素材项目", "LINKED")

        original_library_loader = main.load_library_images
        original_canvas_dir = main.CANVAS_DIR
        main.CANVAS_DIR = str(Path(temp_dir) / "canvases")
        Path(main.CANVAS_DIR).mkdir(parents=True, exist_ok=True)
        main.load_library_images = lambda: [{
            "id": "img_project_linked",
            "asset_id": "asset_project_linked",
            "project_id": linked_project["id"],
            "filename": "linked.png",
            "url": "/api/library/file/test/linked.png",
            "width": 100,
            "height": 80,
            "size_bytes": 123,
        }]
        try:
            main.sync_legacy_domain_records()
        finally:
            main.load_library_images = original_library_loader
            main.CANVAS_DIR = original_canvas_dir
        assert main.DOMAIN_STORE.asset_by_url("/api/library/file/test/linked.png")["project_id"] == linked_project["id"]

        async def deterministic_planner(payload, context):
            if not payload["history"]:
                return {
                    "action": "tool",
                    "tool": "get_project_context",
                    "arguments": {"project_id": project["id"]},
                    "reason": "read the active project before acting",
                }
            if len(payload["history"]) == 1:
                return {
                    "action": "tool",
                    "tool": "mcp.project_reader.workspace_summary",
                    "arguments": {"max_files": 3},
                    "reason": "inspect the read-only workspace through MCP",
                }
            return {"action": "finish", "answer": "project context inspected"}

        original_planner = main.plan_agent_kernel_action
        main.plan_agent_kernel_action = deterministic_planner
        try:
            client = TestClient(main.app)
            token_response = client.get("/api/config/token")
            assert token_response.status_code == 200
            assert token_response.json()["token"] == ""
            assert "configured" in token_response.json()
            blocked_cors = client.options("/api/agent/skills", headers={
                "Origin": "https://malicious.example",
                "Access-Control-Request-Method": "GET",
            })
            assert blocked_cors.headers.get("access-control-allow-origin") is None
            tools_response = client.get("/api/agent/tools")
            assert tools_response.status_code == 200
            tools_body = tools_response.json()
            assert tools_body["runtime"] == "tool_calling_v1"
            assert any(
                item.get("id") == "get_project_context"
                and item.get("callable") is True
                and item.get("input_schema")
                for item in tools_body["tools"]
            )

            plan_response = client.post("/api/agent/plan", json={
                "goal": "读取当前项目并准备设计方向",
                "page": "agent",
                "context": {
                    "mode": "design",
                    "task_type": "design_task",
                    "project_id": project["id"],
                    "provider": "test-provider",
                    "model": "test-model",
                },
            })
            assert plan_response.status_code == 200
            task = plan_response.json()
            assert task["plan"]["runtime"] == "tool_calling_v1"
            assert task["plan"]["requires_confirmation"] is True
            assert task["plan"]["skill_ids"] == ["architectural-concept-design"]
            assert task["context"]["active_skills"][0]["id"] == "architectural-concept-design"

            denied_run = client.post("/api/agent/run", json={"task_id": task["id"]})
            assert denied_run.status_code == 403
            run_response = client.post("/api/agent/run", json={
                "task_id": task["id"],
                "confirmation_token": task["confirmation_token"],
            })
            assert run_response.status_code == 200
            status_response = client.get(f"/api/agent/tasks/{task['id']}")
            assert status_response.status_code == 200
            completed = status_response.json()
            assert completed["status"] == "succeeded"
            assert completed["result"]["runtime"] == "tool_calling_v1"
            assert completed["result"]["executed_tools"] == [
                "get_project_context",
                "mcp.project_reader.workspace_summary",
            ]
            assert completed["result"]["skill_ids"] == ["architectural-concept-design"]
            assert completed["result"]["mcp_servers"] == ["project-reader"]
            assert completed["result"]["mcp_tools"] == ["mcp.project_reader.workspace_summary"]
            event_types = [event.get("type") for event in completed.get("events") or []]
            assert "tool_started" in event_types
            assert "tool_completed" in event_types
            assert "confirmed" in event_types
            history_response = client.get(f"/api/agent/history?project_id={project['id']}")
            assert history_response.status_code == 200
            assert history_response.json()["tasks"][0]["id"] == task["id"]
            assert history_response.json()["tasks"][0]["project_id"] == project["id"]

            skills_response = client.get("/api/agent/skills")
            assert skills_response.status_code == 200
            assert skills_response.json()["skills"][0]["id"] == "architectural-concept-design"
            capabilities_response = client.get("/api/agent/capabilities")
            assert capabilities_response.status_code == 200
            capabilities = capabilities_response.json()
            assert capabilities["connected_mcp_servers"] == 1
            assert capabilities["mcp_servers"][0]["tools"] == ["mcp.project_reader.workspace_summary"]
        finally:
            main.plan_agent_kernel_action = original_planner


if __name__ == "__main__":
    check_agent_runtime_api_contract()
    print("agent runtime API checks passed")
