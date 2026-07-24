import asyncio
import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
from domain_store import DomainStore
from agent_kernel import ToolExecutionContext


def check_project_context_compiler():
    with tempfile.TemporaryDirectory() as temp_dir:
        original_store = main.DOMAIN_STORE
        original_wiki_search = main.agent_wiki_search_items

        def fake_wiki_search(goal, context, limit=4):
            return [{
                "item": {
                    "id": "wiki_calm_commercial",
                    "title": "克制的商业空间",
                    "excerpt": "控制装饰密度，使用低饱和材质和清晰的人流层级。",
                },
                "content": "控制装饰密度，使用低饱和材质和清晰的人流层级。",
            }]

        try:
            store = DomainStore(os.path.join(temp_dir, "domain.db"))
            project = store.create_project("项目约束验收", "CONTEXT")
            with store.connect() as db:
                db.execute(
                    "UPDATE projects SET settings_json=? WHERE id=?",
                    ('{"style_rules":["保持低饱和石材与金属"],"negative_rules":["避免欧式雕花"]}', project["id"]),
                )
            adopted = store.register_asset(
                project["id"],
                "/assets/output/adopted-reference.png",
                title="已采纳参考",
                source="project_library",
            )
            store.record_preference_event(project["id"], adopted["id"], "final_adopted", {"source": "test"})
            main.DOMAIN_STORE = store
            main.agent_wiki_search_items = fake_wiki_search

            with TestClient(main.app) as client:
                response = client.post(
                    f"/api/projects/{project['id']}/design-context/compile",
                    json={
                        "goal": "设计一处安静、未来感的商业入口",
                        "reference_images": [{
                            "url": "/assets/input/explicit-reference.png",
                            "name": "明确参考图",
                            "asset_id": "asset_explicit",
                        }],
                    },
                )
                assert response.status_code == 200, response.text
                compiled = response.json()["compilation"]
                texts = [item["text"] for item in compiled["constraints"]]
                assert any("安静、未来感" in item for item in texts)
                assert "保持低饱和石材与金属" in texts
                assert "避免欧式雕花" in texts
                assert any("克制的商业空间" in item for item in texts)
                assert any(item["polarity"] == "negative" for item in compiled["constraints"])
                assert {item["source_type"] for item in compiled["reference_assets"]} == {
                    "positive_feedback_asset",
                    "task_reference",
                }

                execution = ToolExecutionContext(
                    goal="生成一版低饱和、克制的项目入口",
                    context={"project_id": project["id"]},
                    state={},
                    history=[],
                    step=1,
                )
                tool_result = asyncio.run(main.build_design_agent_tool_registry().execute(
                    "get_project_context",
                    {"project_id": project["id"]},
                    execution,
                    allow_writes=False,
                    allowed=["get_project_context"],
                    granted_permissions=["project:read"],
                    current_scope="home",
                ))
                assert tool_result["compiled_context"]["id"]
                assert execution.state["context_compilation_id"] == tool_result["compiled_context"]["id"]
                assert execution.state["wiki_matches"], "Agent context tool should pre-load compiled Wiki evidence"

                task = store.create_generation_task(
                    project["id"],
                    prompt=main.apply_compiled_context_to_prompt("生成入口方案", compiled),
                    parameters={"context_compilation_id": compiled["id"]},
                    context_compilation_id=compiled["id"],
                )
                durable = store.get_generation_task(task["id"])
                assert durable["context_compilation"]["id"] == compiled["id"]
                assert "项目约束（必须遵守）" in durable["prompt"]

                history = client.get(f"/api/projects/{project['id']}/design-context").json()
                assert len(history["compilations"]) == 2
                linked = next(item for item in history["compilations"] if item["id"] == compiled["id"])
                assert linked["task_count"] == 1
                workspace = client.get(f"/api/projects/{project['id']}/workspace").json()
                assert workspace["counts"]["context_compilations"] == 2
                assert workspace["recent_tasks"][0]["context_compilation"]["digest"] == compiled["digest"]
        finally:
            main.DOMAIN_STORE = original_store
            main.agent_wiki_search_items = original_wiki_search


if __name__ == "__main__":
    check_project_context_compiler()
    print("project context compiler checks passed")
