import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from domain_store import DomainStore


def check_vertical_project_asset_canvas_generation_lineage_loop():
    with tempfile.TemporaryDirectory() as temp_dir:
        store = DomainStore(os.path.join(temp_dir, "domain.db"))
        project = store.create_project("滨水展馆", "WATERFRONT")
        canvas = {
            "id": "canvas_vertical",
            "project_id": project["id"],
            "title": "概念方案",
            "kind": "smart",
            "nodes": [],
            "connections": [],
            "viewport": {"x": 0, "y": 0, "scale": 1},
            "created_at": 1000,
            "updated_at": 1000,
        }
        store.save_canvas_snapshot(canvas, project["id"])
        source = store.register_asset(
            project["id"],
            "/assets/input/site.jpg",
            title="场地照片",
            source="library",
        )
        task = store.create_generation_task(
            project["id"],
            canvas_id=canvas["id"],
            source_node_id="source_node",
            provider_id="test-provider",
            model="test-image-model",
            prompt="生成两个保留滨水轴线的候选",
            parameters={"n": 2},
            inputs=[{"url": "/assets/input/site.jpg", "asset_id": source["id"], "role": "source"}],
        )
        store.update_generation_task(task["id"], "running")
        completed = store.complete_generation_task(
            task["id"],
            ["/assets/output/candidate-a.png", "/assets/output/candidate-b.png"],
        )

        assert completed["status"] == "succeeded"
        assert len(completed["outputs"]) == 2
        listed = store.list_generation_tasks(project_id=project["id"], canvas_id=canvas["id"])
        assert len(listed) == 1
        assert listed[0]["output_count"] == 2
        selected = completed["outputs"][0]
        lineage = store.lineage_for_asset(selected["asset_id"])
        assert lineage["upstream"][0]["from_asset_id"] == source["id"]
        assert lineage["versions"][0]["storage_url"] == "/assets/output/candidate-a.png"
        library_asset = store.register_asset(
            project["id"],
            "/api/library/file/smart-canvas/candidate-a.png",
            asset_id=selected["asset_id"],
            title="采用候选 A",
            source="library",
        )
        assert library_asset["id"] == selected["asset_id"], "回存资源库必须保留生成结果的资产身份"
        overview = store.project_overview(project["id"])
        assert overview["counts"] == {
            "assets": 3,
            "canvases": 1,
            "generation_tasks": 1,
            "lineage_edges": 2,
            "preference_events": 4,
        }
        workspace = store.project_workspace(project["id"])
        assert workspace["counts"]["succeeded_tasks"] == 1
        assert workspace["counts"]["active_tasks"] == 0
        assert workspace["canvases"][0]["task_count"] == 1
        assert workspace["canvases"][0]["succeeded_count"] == 1
        assert workspace["recent_tasks"][0]["canvas_title"] == "概念方案"
        assert workspace["recent_assets"][0]["storage_url"]
        assert workspace["counts"]["feedback_events"] == 4
        assert workspace["feedback_summary"]["top_assets"], "生成参考和候选结果必须进入项目反馈闭环"
        source_feedback = store.feedback_for_asset(source["id"], project["id"])
        assert source_feedback["counts"] == {"generation_reference": 1, "variant_generated": 1}
        assert source_feedback["score"] == 6
        assert store.list_canvas_snapshots(canvas["id"])[0]["version"] == 1


if __name__ == "__main__":
    check_vertical_project_asset_canvas_generation_lineage_loop()
    print("unified domain foundation checks passed")
