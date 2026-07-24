import io
import posixpath
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from PIL import Image

import main
from domain_store import DomainStore
from ppt_workbench import _image_for_media


TARGET_SLIDES = {2, 7, 12, 13, 14, 15}
PACKAGE_RELATIONSHIP_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS = {"a": DRAWING_NS, "p": PRESENTATION_NS}


def jpeg_bytes(color=(214, 75, 66), size=(1800, 1200)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def minimal_template_bytes():
    buffer = io.BytesIO()
    presentation = (
        '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:sldSz cx="12192000" cy="6858000"/></p:presentation>'
    )
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="jpeg" ContentType="image/jpeg"/><Default Extension="xml" ContentType="application/xml"/></Types>')
        archive.writestr("ppt/presentation.xml", presentation)
        for slide_number in range(1, 16):
            text = ""
            if slide_number == 12:
                text = '<p:sp><p:nvSpPr><p:cNvPr id="900" name="户型标题"/></p:nvSpPr><p:spPr><a:xfrm><a:off x="200000" y="100000"/><a:ext cx="3000000" cy="500000"/></a:xfrm></p:spPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="zh-CN"><a:ea typeface="微软雅黑"/></a:rPr><a:t>洋房112+112户型</a:t></a:r></a:p></p:txBody></p:sp>'
            if slide_number in TARGET_SLIDES:
                rotation = ' rot="16200000"' if slide_number == 14 else ""
                picture = f'<p:pic><p:nvPicPr><p:cNvPr id="{slide_number}" name="Main {slide_number}"/></p:nvPicPr><p:blipFill><a:blip r:embed="rId1"/></p:blipFill><p:spPr><a:xfrm{rotation}><a:off x="900000" y="700000"/><a:ext cx="10000000" cy="5200000"/></a:xfrm></p:spPr></p:pic>'
                rels = f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image{slide_number}.jpeg"/></Relationships>'
                archive.writestr(f"ppt/slides/_rels/slide{slide_number}.xml.rels", rels)
                media_size = (180, 320) if slide_number == 14 else (320, 180)
                archive.writestr(f"ppt/media/image{slide_number}.jpeg", jpeg_bytes(color=(slide_number * 10, 90, 120), size=media_size))
            else:
                picture = ""
            slide_xml = f'<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:cSld><p:spTree>{picture}{text}</p:spTree></p:cSld></p:sld>'
            archive.writestr(f"ppt/slides/slide{slide_number}.xml", slide_xml)
    return buffer.getvalue()


def relationship_target(archive, slide_number):
    root = ET.fromstring(archive.read(f"ppt/slides/_rels/slide{slide_number}.xml.rels"))
    relationship = next(item for item in root.findall(f"{{{PACKAGE_RELATIONSHIP_NS}}}Relationship") if item.get("Id") == "rId1")
    return posixpath.normpath(posixpath.join("ppt/slides", relationship.get("Target")))


def check_frontend_contract():
    page = (ROOT / "static" / "ppt-workbench.html").read_text(encoding="utf-8")
    assert all(f'data-mode="{mode}"' in page for mode in ("images", "text", "qa"))
    assert "image-objects" in page and "text-objects" in page and "/scan" in page
    assert "完全相同无需替换" in page and "疑似更新版" in page
    assert "导出完整" in page and "保护" in page
    project_page = (ROOT / "static" / "project-workbench.html").read_text(encoding="utf-8")
    assert 'id="pptWorkbenchBtn"' in project_page


def check_real_project_export_flow():
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        original_store, original_assets, original_ppt_root, original_render = main.DOMAIN_STORE, main.ASSETS_DIR, main.PPT_WORKBENCH_DIR, main.render_template_previews
        try:
            main.ASSETS_DIR = str(temp / "assets")
            main.PPT_WORKBENCH_DIR = str(temp / "assets" / "ppt_workbench")
            Path(main.PPT_WORKBENCH_DIR).mkdir(parents=True, exist_ok=True)
            main.DOMAIN_STORE = DomainStore(str(temp / "domain.db"))
            main.render_template_previews = lambda manifest, root: []
            project = main.DOMAIN_STORE.create_project("PPT闭环验收", "PPT")
            with TestClient(main.app) as client:
                response = client.post(
                    f"/api/projects/{project['id']}/ppt-workbench/templates",
                    files={"file": ("建筑强排模板.pptx", minimal_template_bytes(), "application/vnd.openxmlformats-officedocument.presentationml.presentation")},
                )
                assert response.status_code == 200, response.text
                job = response.json()["job"]
                assert job["schema_version"] == 3
                assert len(job["image_objects"]) == 6
                assert len(job["pages"]) == 15
                assert len(job["text_objects"]) == 1

                first = next(item for item in job["image_objects"] if item["slide_number"] == 2)
                exact = jpeg_bytes(color=(20, 90, 120), size=(320, 180))
                response = client.post(
                    f"/api/projects/{project['id']}/ppt-workbench/{job['id']}/images",
                    data={"slot_id": first["id"]},
                    files={"file": ("same.jpg", exact, "image/jpeg")},
                )
                assert response.status_code == 200, response.text
                response = client.post(f"/api/projects/{project['id']}/ppt-workbench/{job['id']}/scan")
                assert response.status_code == 200, response.text
                job = response.json()["job"]
                first = next(item for item in job["image_objects"] if item["slide_number"] == 2)
                assert first["match_status"] == "unchanged" and not first.get("assignment"), "exact image must become a no-op"

                replacement = jpeg_bytes(color=(10, 170, 80), size=(1800, 1200))
                for slide_number in (7, 14):
                    target = next(item for item in job["image_objects"] if item["slide_number"] == slide_number)
                    response = client.post(
                        f"/api/projects/{project['id']}/ppt-workbench/{job['id']}/images",
                        data={"slot_id": target["id"]},
                        files={"file": (f"replace-{slide_number}.jpg", replacement, "image/jpeg")},
                    )
                    assert response.status_code == 200, response.text
                    job = response.json()["job"]

                text_object = job["text_objects"][0]
                response = client.put(
                    f"/api/projects/{project['id']}/ppt-workbench/{job['id']}/text-objects/{text_object['id']}",
                    json={"text": "洋房125+125户型", "revision": text_object["revision"]},
                )
                assert response.status_code == 200, response.text
                job = response.json()["job"]
                assert job["quality"]["status"] == "passed"

                response = client.post(f"/api/projects/{project['id']}/ppt-workbench/{job['id']}/export")
                assert response.status_code == 200, response.text
                payload = response.json()
                task = main.DOMAIN_STORE.get_generation_task(payload["generation_task_id"])
                assert task["status"] == "succeeded" and len(task["inputs"]) == 3
                download = client.get(payload["export"]["download_url"])
                assert download.status_code == 200
                with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
                    slides = [name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")]
                    assert len(slides) == 15
                    assert relationship_target(archive, 2) == "ppt/media/image2.jpeg", "exact no-op must not rewrite media"
                    assert relationship_target(archive, 7).startswith("ppt/media/iaw_")
                    rotated = relationship_target(archive, 14)
                    with Image.open(io.BytesIO(archive.read(rotated))) as image:
                        assert image.height > image.width, "rotated source frame needs pre-rotated replacement"
                    root = ET.fromstring(archive.read("ppt/slides/slide12.xml"))
                    assert "洋房125+125户型" in "".join(node.text or "" for node in root.findall(".//a:t", NS))

                refreshed = client.get(f"/api/projects/{project['id']}/ppt-workbench/{job['id']}").json()["job"]
                assert next(item for item in refreshed["text_objects"] if item["id"] == text_object["id"])["changed"]
                assert refreshed["exports"], "history must persist after refresh"
        finally:
            main.DOMAIN_STORE, main.ASSETS_DIR, main.PPT_WORKBENCH_DIR, main.render_template_previews = original_store, original_assets, original_ppt_root, original_render


def check_rotation_normalization():
    with tempfile.TemporaryDirectory() as temp_dir:
        portrait = Path(temp_dir) / "portrait.jpg"
        portrait.write_bytes(jpeg_bytes(size=(900, 1400)))
        output, _ = _image_for_media(
            str(portrait),
            270,
            {"width": 7017, "height": 9925},
        )
        with Image.open(io.BytesIO(output)) as image:
            assert image.height > image.width, "matching source orientation must not be rotated twice"


if __name__ == "__main__":
    check_frontend_contract()
    check_real_project_export_flow()
    check_rotation_normalization()
    print("ppt workbench checks passed")
