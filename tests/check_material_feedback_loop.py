import json
import tempfile
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

import main
from domain_store import DomainStore


def check_material_feedback_loop():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        original_images_file = main.LIBRARY_IMAGES_FILE
        original_sources_file = main.LIBRARY_SOURCES_FILE
        original_domain_store = main.DOMAIN_STORE
        main.LIBRARY_IMAGES_FILE = str(root / "library" / "images.json")
        main.LIBRARY_SOURCES_FILE = str(root / "library.json")
        main.DOMAIN_STORE = DomainStore(str(root / "domain.db"))
        project_a = main.DOMAIN_STORE.create_project("反馈验收 A", "FEEDBACK-A")
        project_b = main.DOMAIN_STORE.create_project("反馈验收 B", "FEEDBACK-B")
        Path(main.LIBRARY_IMAGES_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(main.LIBRARY_IMAGES_FILE).write_text(json.dumps([
            {"id": "shared_material", "scope": "shared", "filename": "stone.png", "url": "/stone.png"},
        ], ensure_ascii=False), encoding="utf-8")
        try:
            client = TestClient(main.app)
            favorite = client.post("/api/library/images/shared_material/feedback", json={
                "project_id": project_a["id"], "event_type": "favorite", "context": {"source": "test"},
            })
            assert favorite.status_code == 200
            assert favorite.json()["feedback"]["favorited"] is True
            assert favorite.json()["feedback"]["score"] == 3

            adopted = client.post("/api/library/images/shared_material/feedback", json={
                "project_id": project_a["id"], "event_type": "final_adopted", "context": {"source": "test"},
            })
            assert adopted.status_code == 200
            assert adopted.json()["image"]["adopted"] is True
            assert adopted.json()["image"]["feedback"]["score"] == 11

            project_a_list = client.get("/api/library/images", params={
                "scope": "available", "project_id": project_a["id"], "page_size": 20,
            }).json()["images"]
            project_b_list = client.get("/api/library/images", params={
                "scope": "available", "project_id": project_b["id"], "page_size": 20,
            }).json()["images"]
            assert project_a_list[0]["favorited"] is True and project_a_list[0]["adopted"] is True
            assert project_b_list[0]["favorited"] is False and project_b_list[0]["adopted"] is False

            unfavorite = client.post("/api/library/images/shared_material/feedback", json={
                "project_id": project_a["id"], "event_type": "unfavorite", "context": {},
            })
            assert unfavorite.json()["feedback"]["favorited"] is False
            assert unfavorite.json()["feedback"]["score"] == 8

            summary = client.get(f"/api/projects/{project_a['id']}/feedback").json()
            assert summary["event_count"] == 3
            assert summary["adopted_assets"] == 1
            assert summary["favorited_assets"] == 0
            assert summary["top_assets"][0]["feedback"]["adopted"] is True
        finally:
            main.LIBRARY_IMAGES_FILE = original_images_file
            main.LIBRARY_SOURCES_FILE = original_sources_file
            main.DOMAIN_STORE = original_domain_store


if __name__ == "__main__":
    check_material_feedback_loop()
    print("material feedback loop checks passed")
