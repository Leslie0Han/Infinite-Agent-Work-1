import json
import tempfile
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

import main
from domain_store import DomainStore


def check_project_and_shared_library_boundaries():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        original_images_file = main.LIBRARY_IMAGES_FILE
        original_sources_file = main.LIBRARY_SOURCES_FILE
        original_domain_store = main.DOMAIN_STORE
        main.LIBRARY_IMAGES_FILE = str(root / "library" / "images.json")
        main.LIBRARY_SOURCES_FILE = str(root / "library.json")
        main.DOMAIN_STORE = DomainStore(str(root / "domain.db"))
        project_a = main.DOMAIN_STORE.create_project("项目 A", "A")
        project_b = main.DOMAIN_STORE.create_project("项目 B", "B")
        Path(main.LIBRARY_IMAGES_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(main.LIBRARY_IMAGES_FILE).write_text(json.dumps([
            {"id": "shared_legacy", "filename": "shared.png", "url": "/shared.png"},
            {"id": "project_a", "project_id": project_a["id"], "filename": "a.png", "url": "/a.png"},
            {"id": "project_b", "project_id": project_b["id"], "filename": "b.png", "url": "/b.png"},
        ], ensure_ascii=False), encoding="utf-8")
        try:
            client = TestClient(main.app)
            project_response = client.get("/api/library/images", params={
                "scope": "project", "project_id": project_a["id"], "page_size": 50,
            })
            assert project_response.status_code == 200
            assert [item["id"] for item in project_response.json()["images"]] == ["project_a"]

            shared_response = client.get("/api/library/images", params={"scope": "shared", "page_size": 50})
            assert [item["id"] for item in shared_response.json()["images"]] == ["shared_legacy"]

            available_response = client.get("/api/library/images", params={
                "scope": "available", "project_id": project_a["id"], "page_size": 50,
            })
            assert {item["id"] for item in available_response.json()["images"]} == {"shared_legacy", "project_a"}

            agent_matches = main.resolve_library_agent_matches({"project_id": project_a["id"]}, limit=20)
            assert {item["id"] for item in agent_matches["items"]} == {"shared_legacy", "project_a"}
            assert "project_b" not in {item["id"] for item in agent_matches["items"]}

            promoted = client.post("/api/library/images/project_a/copy", json={"target_scope": "shared"})
            assert promoted.status_code == 200
            promoted_image = promoted.json()["image"]
            assert promoted_image["scope"] == "shared"
            assert promoted_image["copied_from_image_id"] == "project_a"

            copied = client.post("/api/library/images/shared_legacy/copy", json={
                "target_scope": "project", "project_id": project_b["id"],
            })
            assert copied.status_code == 200
            copied_image = copied.json()["image"]
            assert copied_image["scope"] == "project"
            assert copied_image["project_id"] == project_b["id"]
            assert main.DOMAIN_STORE.asset_by_url("/shared.png", project_b["id"])["id"] == copied_image["asset_id"]

            original_asset = main.DOMAIN_STORE.register_asset(
                project_a["id"], "/cross-project.png", asset_id="asset_original",
            )
            copied_asset = main.DOMAIN_STORE.register_asset(
                project_b["id"], "/cross-project.png", asset_id=original_asset["id"],
            )
            assert copied_asset["id"] != original_asset["id"]
            assert main.DOMAIN_STORE.asset_by_url("/cross-project.png", project_a["id"])["id"] == original_asset["id"]

            persisted = json.loads(Path(main.LIBRARY_IMAGES_FILE).read_text(encoding="utf-8"))
            legacy = next(item for item in persisted if item["id"] == "shared_legacy")
            assert legacy["scope"] == "shared" and legacy["project_id"] == ""
            assert any(item.get("copied_from_image_id") == "project_a" for item in persisted)
        finally:
            main.LIBRARY_IMAGES_FILE = original_images_file
            main.LIBRARY_SOURCES_FILE = original_sources_file
            main.DOMAIN_STORE = original_domain_store


if __name__ == "__main__":
    check_project_and_shared_library_boundaries()
    print("library project scope checks passed")
