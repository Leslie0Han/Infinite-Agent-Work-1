import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
from domain_store import DomainStore


def check_quality_gate_replay():
    with tempfile.TemporaryDirectory() as temp_dir:
        original_store = main.DOMAIN_STORE
        original_canvas_llm = main.canvas_llm
        original_runner = main.run_canvas_image_task

        async def fake_canvas_llm(payload):
            return {
                "text": """{
                    "overall_score": 62,
                    "verdict": "构图可用，但几何连续性不足",
                    "feedback": "保持现有构图，修正拱券连接与人物尺度。",
                    "criteria": [
                        {"id":"intent_fidelity","score":82,"reason":"回应了未来感建筑目标"},
                        {"id":"visual_quality","score":76,"reason":"清晰度足够"},
                        {"id":"composition_scale","score":68,"reason":"人物尺度偏大"},
                        {"id":"geometry_plausibility","score":45,"reason":"拱券连接断裂"}
                    ]
                }""",
                "model": "judge-test-model",
            }

        async def no_op_runner(task_id, payload):
            return None

        try:
            store = DomainStore(os.path.join(temp_dir, "domain.db"))
            project = store.create_project("质量门验收", "QUALITY")
            task = store.create_generation_task(
                project["id"],
                canvas_id="canvas_quality",
                provider_id="test-image",
                model="image-model",
                prompt="生成克制的未来感拱廊建筑",
                parameters={
                    "prompt": "生成克制的未来感拱廊建筑",
                    "provider_id": "test-image",
                    "model": "image-model",
                    "project_id": project["id"],
                    "canvas_id": "canvas_quality",
                    "quality_gate": True,
                    "judge_provider": "test-chat",
                    "judge_model": "judge-test-model",
                },
            )
            store.complete_generation_task(task["id"], ["/assets/output/quality-first.png"])
            main.DOMAIN_STORE = store
            main.canvas_llm = fake_canvas_llm
            main.run_canvas_image_task = no_op_runner

            with TestClient(main.app) as client:
                evaluated = client.post(
                    f"/api/generation-tasks/{task['id']}/evaluate",
                    json={"judge_provider": "test-chat", "judge_model": "judge-test-model", "pass_threshold": 75},
                )
                assert evaluated.status_code == 200
                evaluation = evaluated.json()["evaluation"]
                assert evaluation["status"] == "failed"
                assert evaluation["overall_score"] == 62
                assert len(evaluation["scores"]) == 4
                assert evaluation["scores"][3]["passed"] is False

                retried = client.post(
                    f"/api/generation-tasks/{task['id']}/quality-retry",
                    json={"max_retries": 0},
                )
                assert retried.status_code == 200
                child_id = retried.json()["task_id"]
                child = store.get_generation_task(child_id)
                assert child["parent_task_id"] == task["id"]
                assert child["root_task_id"] == task["id"]
                assert child["attempt"] == 2
                assert "拱券连接与人物尺度" in child["prompt"]

                replay = client.get(f"/api/generation-tasks/{child_id}/replay")
                assert replay.status_code == 200
                replay_data = replay.json()
                assert replay_data["root_task_id"] == task["id"]
                assert [item["attempt"] for item in replay_data["attempts"]] == [1, 2]
                assert replay_data["attempts"][0]["quality_evaluations"][0]["status"] == "failed"
                assert replay_data["retry_links"][0]["child_task_id"] == child_id

                workspace = client.get(f"/api/projects/{project['id']}/workspace").json()
                assert workspace["quality_summary"]["evaluation_count"] == 1
                assert workspace["quality_summary"]["first_hit_rate"] == 0
                assert workspace["counts"]["quality_failed_runs"] == 1
        finally:
            main.DOMAIN_STORE = original_store
            main.canvas_llm = original_canvas_llm
            main.run_canvas_image_task = original_runner
            main.CANVAS_TASKS.clear()
            main.CANVAS_ASYNC_TASKS.clear()


if __name__ == "__main__":
    check_quality_gate_replay()
    print("quality gate replay checks passed")
