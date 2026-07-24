import asyncio
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check_generation_retry_and_cancel_controls():
    import main
    from domain_store import DomainStore

    original_store = main.DOMAIN_STORE
    original_runner = main.run_canvas_image_task

    async def no_op_runner(task_id, payload):
        return None

    with tempfile.TemporaryDirectory() as temp_dir:
        store = DomainStore(os.path.join(temp_dir, "domain.db"))
        project = store.ensure_default_project()
        failed = store.create_generation_task(
            project["id"],
            canvas_id="canvas_controls",
            source_node_id="source_node",
            provider_id="test",
            model="test-model",
            prompt="retry this generation",
            parameters={
                "prompt": "retry this generation",
                "provider_id": "test",
                "model": "test-model",
                "project_id": project["id"],
                "canvas_id": "canvas_controls",
                "source_node_id": "source_node",
            },
        )
        store.update_generation_task(failed["id"], "failed", "temporary failure")
        try:
            main.DOMAIN_STORE = store
            main.run_canvas_image_task = no_op_runner
            retried = asyncio.run(main.retry_canvas_image_task(failed["id"]))
            assert retried["status"] == "queued"
            new_task_id = retried["task_id"]
            assert store.get_generation_task(new_task_id)["status"] == "queued"

            cancelled = asyncio.run(main.cancel_canvas_image_task(new_task_id))
            assert cancelled["status"] == "cancelled"
            assert store.get_generation_task(new_task_id)["status"] == "cancelled"
        finally:
            main.DOMAIN_STORE = original_store
            main.run_canvas_image_task = original_runner
            main.CANVAS_TASKS.pop(locals().get("new_task_id", ""), None)
            main.CANVAS_ASYNC_TASKS.pop(locals().get("new_task_id", ""), None)


if __name__ == "__main__":
    check_generation_retry_and_cancel_controls()
    print("generation task control checks passed")
