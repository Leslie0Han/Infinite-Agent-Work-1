import asyncio
import sys
import tempfile
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
from agent_kernel import ToolExecutionContext
from domain_store import DomainStore


async def exercise_core_tools():
    with tempfile.TemporaryDirectory() as temp_dir:
        original_store = main.DOMAIN_STORE
        original_canvas_dir = main.CANVAS_DIR
        original_import = main.import_urls_into_library
        original_tag = main.library_ai_tag
        original_wiki = main.agent_create_wiki_output_page
        original_library_loader = main.load_library_images
        try:
            main.DOMAIN_STORE = DomainStore(str(Path(temp_dir) / "domain.db"))
            main.CANVAS_DIR = str(Path(temp_dir) / "canvases")
            Path(main.CANVAS_DIR).mkdir(parents=True, exist_ok=True)
            project = main.DOMAIN_STORE.create_project("核心工具验收", "CORE")
            other_project = main.DOMAIN_STORE.create_project("越权项目", "OTHER")
            canvas = main.new_canvas("核心工具画布", kind="smart", project_id=project["id"])
            canvas["nodes"] = [{
                "id": "node_result",
                "type": "smart-image",
                "title": "生成结果",
                "images": [{"url": "/assets/output/core-result.png", "filename": "core-result.png"}],
            }]
            main.save_canvas(canvas)

            import_calls = []
            wiki_calls = []

            def fake_import(**kwargs):
                import_calls.append(kwargs)
                return {
                    "count": 1,
                    "imported": [{"id": "img_saved", "url": kwargs["urls"][0]}],
                    "skipped": [],
                    "source_id": "smart-canvas",
                }

            async def fake_tag(req):
                return {"results": [{"id": req.image_ids[0], "ok": True, "category": "办公", "tags": ["日景"]}]}

            def fake_wiki(context, output_type, title, content, related_ids=None):
                wiki_calls.append({"context": context, "output_type": output_type, "title": title, "content": content, "related_ids": related_ids or []})
                return {"id": f"{output_type}_core", "title": title, "content": content, "related_ids": related_ids or []}

            main.import_urls_into_library = fake_import
            main.library_ai_tag = fake_tag
            main.agent_create_wiki_output_page = fake_wiki
            main.load_library_images = lambda: [{
                "id": "img_project",
                "project_id": project["id"],
                "scope": "project",
                "filename": "project.png",
                "url": "/api/library/file/test/project.png",
            }]

            registry = main.build_design_agent_tool_registry()
            manifests = {item["name"]: item for item in registry.manifest()}
            for tool_id in [
                "read_smart_canvas",
                "save_canvas_node_images_to_library",
                "write_wiki_qa",
                "write_agent_report",
                "tag_library_images",
            ]:
                assert tool_id in manifests

            canvas_execution = ToolExecutionContext(
                goal="读取并回存当前画布结果",
                context={"project_id": project["id"], "canvas_id": canvas["id"], "selected_node_id": "node_result"},
                state={},
                history=[],
                step=1,
            )
            read_result = await registry.execute(
                "read_smart_canvas",
                {"canvas_id": canvas["id"], "project_id": project["id"], "selected_node_ids": ["node_result"]},
                canvas_execution,
                allow_writes=False,
                granted_permissions=["canvas:read", "project:read"],
                current_scope="smart-canvas",
            )
            assert read_result["node_count"] == 1
            assert read_result["selected_node_ids"] == ["node_result"]

            try:
                await registry.execute(
                    "read_smart_canvas",
                    {"canvas_id": canvas["id"], "project_id": other_project["id"]},
                    canvas_execution,
                    allow_writes=False,
                    granted_permissions=["canvas:read", "project:read"],
                    current_scope="smart-canvas",
                )
                raise AssertionError("cross-project canvas reads must be rejected")
            except HTTPException as exc:
                assert exc.status_code == 403

            save_result = await registry.execute(
                "save_canvas_node_images_to_library",
                {"canvas_id": canvas["id"], "project_id": project["id"]},
                canvas_execution,
                allow_writes=True,
                granted_permissions=["canvas:read", "library:write", "project:write"],
                current_scope="smart-canvas",
            )
            assert save_result["saved_count"] == 1
            assert import_calls[0]["project_id"] == project["id"]

            library_execution = ToolExecutionContext(
                goal="标注素材",
                context={"project_id": project["id"], "provider": "test", "model": "test-model"},
                state={},
                history=[],
                step=1,
            )
            tag_result = await registry.execute(
                "tag_library_images",
                {"image_ids": ["img_project"], "project_id": project["id"], "provider": "test", "model": "test-model"},
                library_execution,
                allow_writes=True,
                granted_permissions=["library:write", "generation:run"],
                current_scope="library",
            )
            assert tag_result["success_count"] == 1

            wiki_execution = ToolExecutionContext(
                goal="记录验收结论",
                context={"project_id": project["id"]},
                state={},
                history=[],
                step=1,
            )
            wiki_result = await registry.execute(
                "write_wiki_qa",
                {"question": "是否通过？", "answer": "核心工具已通过。", "related_ids": ["source_1"]},
                wiki_execution,
                allow_writes=True,
                granted_permissions=["wiki:write"],
                current_scope="home",
            )
            assert wiki_result["wiki_page_id"] == "qa_core"
            assert wiki_execution.state["wiki_page"]["id"] == "qa_core"

            report_execution = ToolExecutionContext(
                goal="研究并总结当前项目",
                context={"project_id": project["id"], "mode": "research"},
                state={"project_id": project["id"], "wiki_matches": [{"item": {"id": "source_2"}}]},
                history=[],
                step=1,
            )
            report_result = await registry.execute(
                "write_agent_report",
                {"title": "核心研究报告", "content": "## 结论\n知识模式已进入 Kernel。", "related_ids": [project["id"]]},
                report_execution,
                allow_writes=True,
                granted_permissions=["wiki:write"],
                current_scope="home",
            )
            assert report_result["wiki_page_id"] == "report_core"
            assert report_result["project_id"] == project["id"]
            assert report_result["related_ids"] == ["source_2"]
            assert f"项目 ID：`{project['id']}`" in wiki_calls[-1]["content"]

            assert main.agent_history_outputs({"result": {
                "runtime": "tool_calling_v1",
                "executed_tools": ["read_smart_canvas"],
                "canvas_id": canvas["id"],
            }}) == [{"type": "canvas_summary", "label": "画布摘要"}]
            assert main.agent_history_outputs({"result": {
                "runtime": "tool_calling_v1",
                "executed_tools": ["read_smart_canvas", "save_canvas_node_images_to_library"],
                "canvas_id": canvas["id"],
                "imported_count": 1,
            }}) == [{"type": "library_images", "label": "资源库图片"}]
        finally:
            main.DOMAIN_STORE = original_store
            main.CANVAS_DIR = original_canvas_dir
            main.import_urls_into_library = original_import
            main.library_ai_tag = original_tag
            main.agent_create_wiki_output_page = original_wiki
            main.load_library_images = original_library_loader


if __name__ == "__main__":
    asyncio.run(exercise_core_tools())
    print("agent core tool checks passed")
