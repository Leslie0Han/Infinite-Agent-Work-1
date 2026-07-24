"""Project-scoped PPT image and text editing helpers.

The workbench edits OOXML parts in place.  It never rebuilds slides, so source
geometry, typography, crops, rotations, masters and page order stay intact.
"""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from PIL import Image, ImageOps, ImageStat


PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
RELATIONSHIP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_RELATIONSHIP_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
NS = {"p": PRESENTATION_NS, "a": DRAWING_NS, "r": RELATIONSHIP_NS}

MAX_TEMPLATE_BYTES = 160 * 1024 * 1024
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_PPTX_UNCOMPRESSED_BYTES = 700 * 1024 * 1024
SCHEMA_VERSION = 3
ANALYSIS_REVISION = "20260724-all-images-text-v2"
FONT_SUBSTITUTIONS = {
    "微软雅黑": "Hiragino Sans GB",
    "黑体": "STHeiti",
    "宋体": "Songti SC",
}


class PptWorkbenchError(ValueError):
    pass


def new_job_id() -> str:
    return f"pptjob_{uuid.uuid4().hex}"


def _safe_component(value: str, *, prefix: str = "item") -> str:
    text = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "")).strip("_")
    if not text:
        raise PptWorkbenchError(f"无效的{prefix}标识")
    return text[:120]


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(item or "") for item in parts)
    return f"{prefix}_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:18]}"


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)


def project_dir(root: str, project_id: str) -> Path:
    return Path(root) / _safe_component(project_id, prefix="项目")


def job_dir(root: str, project_id: str, job_id: str) -> Path:
    return project_dir(root, project_id) / _safe_component(job_id, prefix="PPT任务")


def manifest_path(root: str, project_id: str, job_id: str) -> Path:
    return job_dir(root, project_id, job_id) / "manifest.json"


def _validate_pptx(path: Path) -> int:
    if path.stat().st_size > MAX_TEMPLATE_BYTES:
        raise PptWorkbenchError("PPTX 超过 160MB，当前版本暂不处理")
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or "ppt/presentation.xml" not in names:
                raise PptWorkbenchError("文件不是有效的 PPTX")
            total = sum(int(item.file_size or 0) for item in archive.infolist())
            if total > MAX_PPTX_UNCOMPRESSED_BYTES:
                raise PptWorkbenchError("PPTX 解压体积过大，已拒绝处理")
            return len([name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)])
    except zipfile.BadZipFile as exc:
        raise PptWorkbenchError("文件不是有效的 PPTX") from exc


def _rels_path(part_path: str) -> str:
    return posixpath.join(posixpath.dirname(part_path), "_rels", posixpath.basename(part_path) + ".rels")


def _relationships(archive: zipfile.ZipFile, part_path: str) -> Dict[str, Dict[str, str]]:
    rels_name = _rels_path(part_path)
    try:
        root = ET.fromstring(archive.read(rels_name))
    except KeyError:
        return {}
    result: Dict[str, Dict[str, str]] = {}
    base = posixpath.dirname(part_path)
    for item in root.findall(f"{{{PACKAGE_RELATIONSHIP_NS}}}Relationship"):
        rel_id = str(item.get("Id") or "")
        target = str(item.get("Target") or "")
        external = str(item.get("TargetMode") or "").lower() == "external" or target.startswith(("http://", "https://"))
        result[rel_id] = {
            "target": target if external else posixpath.normpath(posixpath.join(base, target)),
            "type": str(item.get("Type") or "").rsplit("/", 1)[-1],
            "external": "1" if external else "",
        }
    return result


def _int_attr(node: Optional[ET.Element], key: str, default: int = 0) -> int:
    try:
        return int(node.get(key) or default) if node is not None else default
    except (TypeError, ValueError):
        return default


def _identity_transform() -> Dict[str, float]:
    return {"sx": 1.0, "sy": 1.0, "tx": 0.0, "ty": 0.0, "rotation": 0.0}


def _group_transform(group: ET.Element, parent: Dict[str, float]) -> Dict[str, float]:
    xfrm = group.find("./p:grpSpPr/a:xfrm", NS)
    if xfrm is None:
        return dict(parent)
    off, ext = xfrm.find("./a:off", NS), xfrm.find("./a:ext", NS)
    child_off, child_ext = xfrm.find("./a:chOff", NS), xfrm.find("./a:chExt", NS)
    child_w, child_h = max(_int_attr(child_ext, "cx", 1), 1), max(_int_attr(child_ext, "cy", 1), 1)
    local_sx, local_sy = _int_attr(ext, "cx", child_w) / child_w, _int_attr(ext, "cy", child_h) / child_h
    local_tx = _int_attr(off, "x") - _int_attr(child_off, "x") * local_sx
    local_ty = _int_attr(off, "y") - _int_attr(child_off, "y") * local_sy
    return {
        "sx": parent["sx"] * local_sx,
        "sy": parent["sy"] * local_sy,
        "tx": parent["tx"] + local_tx * parent["sx"],
        "ty": parent["ty"] + local_ty * parent["sy"],
        "rotation": parent["rotation"] + _int_attr(xfrm, "rot") / 60000,
    }


def _frame_from_xfrm(xfrm: Optional[ET.Element], transform: Dict[str, float], slide_width: int, slide_height: int) -> Dict[str, Any]:
    off = xfrm.find("./a:off", NS) if xfrm is not None else None
    ext = xfrm.find("./a:ext", NS) if xfrm is not None else None
    x = transform["tx"] + _int_attr(off, "x") * transform["sx"]
    y = transform["ty"] + _int_attr(off, "y") * transform["sy"]
    width = _int_attr(ext, "cx") * transform["sx"]
    height = _int_attr(ext, "cy") * transform["sy"]
    return {
        "x": round(x), "y": round(y), "width": round(width), "height": round(height),
        "x_percent": round(x / max(slide_width, 1) * 100, 5),
        "y_percent": round(y / max(slide_height, 1) * 100, 5),
        "width_percent": round(width / max(slide_width, 1) * 100, 5),
        "height_percent": round(height / max(slide_height, 1) * 100, 5),
    }


def _image_phash(image: Image.Image) -> str:
    sample = ImageOps.fit(ImageOps.grayscale(image), (32, 32), method=Image.Resampling.LANCZOS)
    pixels = list(sample.getdata())
    cosine = [[math.cos(math.pi * (2 * x + 1) * u / 64) for x in range(32)] for u in range(8)]
    values: List[float] = []
    for u in range(8):
        for v in range(8):
            total = 0.0
            for y in range(32):
                row = y * 32
                cy = cosine[v][y]
                total += sum(pixels[row + x] * cosine[u][x] * cy for x in range(32))
            values.append(total)
    median = sorted(values[1:])[len(values[1:]) // 2]
    bits = "".join("1" if value >= median else "0" for value in values)
    return f"{int(bits, 2):016x}"


def fingerprint_image_bytes(content: bytes) -> Dict[str, Any]:
    result = {"sha256": hashlib.sha256(content).hexdigest(), "normalized_sha256": "", "phash": "", "width": 0, "height": 0, "mime_type": ""}
    try:
        with Image.open(BytesIO(content)) as probe:
            image = ImageOps.exif_transpose(probe)
            image.load()
            result["width"], result["height"] = image.size
            result["mime_type"] = Image.MIME.get((probe.format or "").upper(), "")
            rgb = image.convert("RGB")
            if rgb.width * rgb.height <= 50_000_000:
                digest = hashlib.sha256()
                digest.update(f"{rgb.width}x{rgb.height}|RGB|".encode("ascii"))
                digest.update(rgb.tobytes())
                result["normalized_sha256"] = digest.hexdigest()
            result["phash"] = _image_phash(rgb)
    except Exception:
        pass
    return result


def fingerprint_image_path(path: str) -> Dict[str, Any]:
    return fingerprint_image_bytes(Path(path).read_bytes())


def _hamming(left: str, right: str) -> int:
    if not left or not right:
        return 999
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        return 999


def _visual_similarity(left_path: str, right_content: bytes) -> Tuple[float, float]:
    try:
        with Image.open(left_path) as left_probe, Image.open(BytesIO(right_content)) as right_probe:
            left = ImageOps.fit(ImageOps.grayscale(ImageOps.exif_transpose(left_probe)), (128, 128), method=Image.Resampling.LANCZOS)
            right = ImageOps.fit(ImageOps.grayscale(ImageOps.exif_transpose(right_probe)), (128, 128), method=Image.Resampling.LANCZOS)
            a, b = list(left.getdata()), list(right.getdata())
        mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
        var_a = sum((x - mean_a) ** 2 for x in a) / len(a)
        var_b = sum((x - mean_b) ** 2 for x in b) / len(b)
        covariance = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b)) / len(a)
        c1, c2 = 6.5025, 58.5225
        ssim = max(0.0, min(1.0, ((2 * mean_a * mean_b + c1) * (2 * covariance + c2)) / ((mean_a ** 2 + mean_b ** 2 + c1) * (var_a + var_b + c2))))
        mask_a = [value < 235 for value in a]
        mask_b = [value < 235 for value in b]
        intersection = sum(left and right for left, right in zip(mask_a, mask_b))
        union = sum(left or right for left, right in zip(mask_a, mask_b))
        return ssim, (intersection / union if union else 1.0)
    except Exception:
        return 0.0, 0.0


def _media_fingerprint(archive: zipfile.ZipFile, media_path: str) -> Dict[str, Any]:
    try:
        return fingerprint_image_bytes(archive.read(media_path))
    except KeyError:
        return {"sha256": "", "normalized_sha256": "", "phash": "", "width": 0, "height": 0, "mime_type": ""}


def _role_from_context(text: str, shape_name: str, index: int) -> Tuple[str, str, List[str]]:
    context = f"{text} {shape_name}".lower()
    rules = [
        ("户型", ["户型", "标准层", "平面图"]),
        ("鸟瞰", ["鸟瞰", "航拍", "全景"]),
        ("体块", ["体块", "模型"]),
        ("剖面", ["剖面", "竖向"]),
        ("区位", ["区位", "交通", "区位图"]),
        ("实景", ["实景", "现状", "四至"]),
        ("彩总", ["彩总", "总平", "总图", "规划"]),
    ]
    for role, keywords in rules:
        if any(keyword.lower() in context for keyword in keywords):
            return role, f"{role}图 {index}", keywords
    return "图片", f"图片 {index}", []


def _shape_props(node: ET.Element, kind: str) -> Tuple[str, str]:
    path = {"pic": "./p:nvPicPr/p:cNvPr", "sp": "./p:nvSpPr/p:cNvPr", "graphicFrame": "./p:nvGraphicFramePr/p:cNvPr"}.get(kind, "")
    props = node.find(path, NS) if path else None
    return (str(props.get("id") or "") if props is not None else "", str(props.get("name") or "") if props is not None else "")


def _scan_part(
    archive: zipfile.ZipFile,
    part_path: str,
    *,
    slide_width: int,
    slide_height: int,
    slide_numbers: Sequence[int],
    scope: str,
    context_text: str = "",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    try:
        root = ET.fromstring(archive.read(part_path))
    except KeyError:
        return [], []
    relationships = _relationships(archive, part_path)
    images: List[Dict[str, Any]] = []
    texts: List[Dict[str, Any]] = []
    image_index = 0
    table_index = 0

    def add_image(node: ET.Element, kind: str, transform: Dict[str, float]) -> None:
        nonlocal image_index
        image_index += 1
        shape_id, shape_name = _shape_props(node, kind)
        blip = node.find(".//a:blip", NS)
        rel_id = str(blip.get(f"{{{RELATIONSHIP_NS}}}embed") or "") if blip is not None else ""
        rel = relationships.get(rel_id) or {}
        xfrm = node.find("./p:spPr/a:xfrm", NS)
        frame = _frame_from_xfrm(xfrm, transform, slide_width, slide_height)
        rotation = transform["rotation"] + (_int_attr(xfrm, "rot") / 60000 if xfrm is not None else 0)
        src_rect = node.find(".//a:srcRect", NS)
        role, label, keywords = _role_from_context(context_text, shape_name, image_index)
        area_ratio = (frame["width"] * frame["height"]) / max(slide_width * slide_height, 1)
        object_class = "layout" if scope != "slide" else ("decorative" if area_ratio < 0.012 else "content")
        media_path = str(rel.get("target") or "")
        supported = bool(media_path) and not rel.get("external")
        object_id = _stable_id("imgobj", part_path, shape_id, rel_id)
        images.append({
            "id": object_id,
            "part_path": part_path,
            "relationship_path": _rels_path(part_path),
            "relationship_id": rel_id,
            "media_path": media_path,
            "shape_id": shape_id,
            "shape_name": shape_name,
            "slide_number": int(slide_numbers[0]) if scope == "slide" and slide_numbers else 0,
            "slide_numbers": list(slide_numbers),
            "scope": scope,
            "object_class": object_class if supported else "unsupported",
            "protected": scope != "slide" or object_class == "decorative",
            "supported": supported,
            "label": label if scope == "slide" else ("版式图片" if scope == "layout" else "母版图片"),
            "role": role,
            "keywords": keywords,
            "rotation_degrees": round(rotation, 4),
            "crop": dict(src_rect.attrib) if src_rect is not None else {},
            "frame": frame,
            "source_fingerprint": _media_fingerprint(archive, media_path) if media_path else {},
            "assignment": None,
            "keep_original": False,
            "match_status": "protected" if scope != "slide" or object_class == "decorative" else "unreviewed",
            "recommendations": [],
        })

    def add_text_shape(node: ET.Element, transform: Dict[str, float]) -> None:
        shape_id, shape_name = _shape_props(node, "sp")
        value = "\n".join("".join(t.text or "" for t in paragraph.findall(".//a:t", NS)) for paragraph in node.findall(".//a:p", NS)).strip()
        if not value:
            return
        frame = _frame_from_xfrm(node.find("./p:spPr/a:xfrm", NS), transform, slide_width, slide_height)
        texts.append({
            "id": _stable_id("txtobj", part_path, shape_id, "shape"),
            "part_path": part_path, "shape_id": shape_id, "shape_name": shape_name,
            "slide_number": int(slide_numbers[0]) if scope == "slide" and slide_numbers else 0,
            "slide_numbers": list(slide_numbers), "scope": scope, "kind": "shape",
            "protected": scope != "slide", "frame": frame,
            "original_text": value, "draft_text": value, "changed": False, "revision": 1,
        })

    def walk(container: ET.Element, transform: Dict[str, float]) -> None:
        nonlocal table_index
        for node in list(container):
            local = node.tag.rsplit("}", 1)[-1]
            if local == "grpSp":
                walk(node, _group_transform(node, transform))
            elif local == "pic":
                add_image(node, "pic", transform)
            elif local == "sp":
                if node.find(".//a:blip", NS) is not None:
                    add_image(node, "sp", transform)
                add_text_shape(node, transform)
            elif local == "graphicFrame":
                table_index += 1
                shape_id, shape_name = _shape_props(node, "graphicFrame")
                frame = _frame_from_xfrm(node.find("./p:xfrm", NS), transform, slide_width, slide_height)
                table = node.find(".//a:tbl", NS)
                if table is not None:
                    for row_index, row in enumerate(table.findall("./a:tr", NS)):
                        for column_index, cell in enumerate(row.findall("./a:tc", NS)):
                            value = "\n".join("".join(t.text or "" for t in paragraph.findall(".//a:t", NS)) for paragraph in cell.findall(".//a:p", NS)).strip()
                            if not value:
                                continue
                            texts.append({
                                "id": _stable_id("txtobj", part_path, shape_id, "table", table_index, row_index, column_index),
                                "part_path": part_path, "shape_id": shape_id, "shape_name": shape_name,
                                "slide_number": int(slide_numbers[0]) if scope == "slide" and slide_numbers else 0,
                                "slide_numbers": list(slide_numbers), "scope": scope, "kind": "table_cell",
                                "table_index": 1, "row_index": row_index, "column_index": column_index,
                                "protected": scope != "slide", "frame": frame,
                                "original_text": value, "draft_text": value, "changed": False, "revision": 1,
                            })

    tree = root.find(".//p:spTree", NS)
    if tree is not None:
        walk(tree, _identity_transform())
    return images, texts


def _used_layouts(archive: zipfile.ZipFile, slide_count: int) -> Tuple[Dict[str, List[int]], Dict[str, List[int]]]:
    layouts: Dict[str, List[int]] = {}
    masters: Dict[str, List[int]] = {}
    for slide_number in range(1, slide_count + 1):
        slide_path = f"ppt/slides/slide{slide_number}.xml"
        layout = next((rel["target"] for rel in _relationships(archive, slide_path).values() if rel.get("type") == "slideLayout"), "")
        if not layout:
            continue
        layouts.setdefault(layout, []).append(slide_number)
        master = next((rel["target"] for rel in _relationships(archive, layout).values() if rel.get("type") == "slideMaster"), "")
        if master:
            masters.setdefault(master, []).append(slide_number)
    return layouts, masters


def analyze_template(path: str) -> Dict[str, Any]:
    source = Path(path)
    slide_count = _validate_pptx(source)
    with zipfile.ZipFile(source) as archive:
        presentation = ET.fromstring(archive.read("ppt/presentation.xml"))
        size = presentation.find("./p:sldSz", NS)
        slide_width = _int_attr(size, "cx", 12192000)
        slide_height = _int_attr(size, "cy", 6858000)
        image_objects: List[Dict[str, Any]] = []
        text_objects: List[Dict[str, Any]] = []
        for slide_number in range(1, slide_count + 1):
            part_path = f"ppt/slides/slide{slide_number}.xml"
            root = ET.fromstring(archive.read(part_path))
            context = " ".join(t.text or "" for t in root.findall(".//a:t", NS))
            images, texts = _scan_part(
                archive, part_path, slide_width=slide_width, slide_height=slide_height,
                slide_numbers=[slide_number], scope="slide", context_text=context,
            )
            image_objects.extend(images)
            text_objects.extend(texts)
        layouts, masters = _used_layouts(archive, slide_count)
        for part_path, slide_numbers in layouts.items():
            images, texts = _scan_part(archive, part_path, slide_width=slide_width, slide_height=slide_height, slide_numbers=slide_numbers, scope="layout")
            image_objects.extend(images)
            text_objects.extend(texts)
        for part_path, slide_numbers in masters.items():
            images, texts = _scan_part(archive, part_path, slide_width=slide_width, slide_height=slide_height, slide_numbers=sorted(set(slide_numbers)), scope="master")
            image_objects.extend(images)
            text_objects.extend(texts)

        font_names: Dict[str, int] = {}
        for name in archive.namelist():
            if not name.endswith(".xml") or not name.startswith(("ppt/slides/", "ppt/slideLayouts/", "ppt/slideMasters/", "ppt/theme/")):
                continue
            try:
                root = ET.fromstring(archive.read(name))
            except ET.ParseError:
                continue
            for tag in ("latin", "ea", "cs"):
                for font in root.findall(f".//a:{tag}", NS):
                    typeface = str(font.get("typeface") or "").strip()
                    if typeface and not typeface.startswith("+"):
                        font_names[typeface] = font_names.get(typeface, 0) + 1
        font_report = {
            "fonts": [{"name": name, "usage_count": count, "preview_substitute": FONT_SUBSTITUTIONS.get(name, "")} for name, count in sorted(font_names.items())],
            "substitution_count": sum(count for name, count in font_names.items() if name in FONT_SUBSTITUTIONS),
            "missing_fonts": [name for name in font_names if name in FONT_SUBSTITUTIONS],
        }

    groups: Dict[str, Dict[str, Any]] = {}
    for item in image_objects:
        key = str(item.get("media_path") or item["id"])
        group = groups.setdefault(key, {"id": _stable_id("imggroup", key), "media_path": item.get("media_path") or "", "object_ids": [], "slide_numbers": []})
        group["object_ids"].append(item["id"])
        group["slide_numbers"] = sorted(set(group["slide_numbers"] + list(item.get("slide_numbers") or [])))
        item["group_id"] = group["id"]
    return {
        "schema_version": SCHEMA_VERSION,
        "analysis_revision": ANALYSIS_REVISION,
        "source_slide_count": slide_count,
        "slide_size": {"width": slide_width, "height": slide_height},
        "image_objects": image_objects,
        "image_groups": list(groups.values()),
        "text_objects": text_objects,
        "font_report": font_report,
        "recommendation_summary": {"unchanged": 0, "updates": 0, "recommended": 0, "unreviewed": len([item for item in image_objects if item.get("object_class") == "content"])},
    }


def _migrate_manifest(payload: Dict[str, Any], template: Path) -> Dict[str, Any]:
    analysis = analyze_template(str(template))
    old_slots = payload.get("slots") or []
    old_images = {str(item.get("id") or ""): item for item in payload.get("image_objects") or []}
    old_texts = {str(item.get("id") or ""): item for item in payload.get("text_objects") or []}
    assignments: Dict[Tuple[int, str], Dict[str, Any]] = {}
    duplicate_assets: Dict[str, int] = {}
    for slot in old_slots:
        assignment = slot.get("assignment") or {}
        asset_id = str(assignment.get("asset_id") or assignment.get("storage_url") or "")
        if assignment and asset_id:
            duplicate_assets[asset_id] = duplicate_assets.get(asset_id, 0) + 1
        assignments[(int(slot.get("slide_number") or 0), str(slot.get("shape_id") or ""))] = assignment
    seen_assets: set[str] = set()
    for item in analysis["image_objects"]:
        previous = old_images.get(str(item.get("id") or "")) or {}
        assignment = previous.get("assignment") or assignments.get((int(item.get("slide_number") or 0), str(item.get("shape_id") or ""))) or None
        asset_id = str((assignment or {}).get("asset_id") or (assignment or {}).get("storage_url") or "")
        if assignment and asset_id and duplicate_assets.get(asset_id, 0) > 1 and asset_id in seen_assets:
            assignment = None
            item["migration_note"] = "已移除旧版工作台跨户型重复指派，请重新确认。"
        if assignment and asset_id:
            seen_assets.add(asset_id)
        item["assignment"] = assignment
        item["keep_original"] = bool(previous.get("keep_original"))
        if previous.get("migration_note"):
            item["migration_note"] = previous["migration_note"]
        if assignment:
            item["match_status"] = "replaced"
    for item in analysis["text_objects"]:
        previous = old_texts.get(str(item.get("id") or "")) or {}
        if previous:
            item["draft_text"] = str(previous.get("draft_text") or item.get("original_text") or "")
            item["changed"] = item["draft_text"] != str(item.get("original_text") or "")
            item["revision"] = int(previous.get("revision") or 1)
            item["overflow_warning"] = bool(previous.get("overflow_warning"))
    payload.update(analysis)
    payload["schema_version"] = SCHEMA_VERSION
    payload["legacy_slots"] = old_slots
    payload.pop("slots", None)
    return payload


def load_manifest(root: str, project_id: str, job_id: str) -> Dict[str, Any]:
    path = manifest_path(root, project_id, job_id)
    if not path.is_file():
        raise FileNotFoundError(job_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("project_id") != project_id or payload.get("id") != job_id:
        raise PptWorkbenchError("PPT任务归属校验失败")
    template = job_dir(root, project_id, job_id) / str(payload.get("template_filename") or "template.pptx")
    if template.is_file() and (int(payload.get("schema_version") or 1) < SCHEMA_VERSION or payload.get("analysis_revision") != ANALYSIS_REVISION):
        payload = _migrate_manifest(payload, template)
        _extract_object_previews(payload, template, job_dir(root, project_id, job_id))
        _atomic_json(path, payload)
    return payload


def save_manifest(root: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    manifest["updated_at"] = int(time.time() * 1000)
    _atomic_json(manifest_path(root, manifest["project_id"], manifest["id"]), manifest)
    return manifest


def list_manifests(root: str, project_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    base = project_dir(root, project_id)
    if not base.is_dir():
        return []
    items: List[Dict[str, Any]] = []
    for path in base.glob("pptjob_*/manifest.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            item = load_manifest(root, project_id, str(raw.get("id") or path.parent.name))
        except (OSError, ValueError):
            continue
        if item.get("project_id") == project_id:
            items.append(item)
    items.sort(key=lambda item: int(item.get("updated_at") or 0), reverse=True)
    return items[: max(1, min(100, int(limit or 20)))]


def _extract_object_previews(manifest: Dict[str, Any], source: Path, destination: Path) -> None:
    preview_dir = destination / "object-previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        for item in manifest.get("image_objects") or []:
            media_path = str(item.get("media_path") or "")
            if not media_path:
                continue
            try:
                content = archive.read(media_path)
                with Image.open(BytesIO(content)) as probe:
                    image = ImageOps.exif_transpose(probe)
                    image.load()
                    image.thumbnail((1200, 1200))
                    target = preview_dir / f"{_safe_component(item['id'])}.png"
                    image.save(target, format="PNG", optimize=True)
                    item["source_preview_rel"] = str(target.relative_to(destination))
            except Exception:
                item["source_preview_rel"] = ""


def create_manifest(root: str, project_id: str, template_path: str, *, original_name: str, template_asset_id: str = "") -> Dict[str, Any]:
    analysis = analyze_template(template_path)
    job_id = new_job_id()
    destination = job_dir(root, project_id, job_id)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / "template.pptx"
    shutil.move(template_path, target)
    now = int(time.time() * 1000)
    manifest = {
        "id": job_id, "project_id": project_id,
        "name": Path(original_name).stem[:160] or "建筑强排模板",
        "template_filename": target.name, "original_filename": Path(original_name).name,
        "template_asset_id": template_asset_id, "status": "draft",
        "created_at": now, "updated_at": now, "exports": [], **analysis,
    }
    _extract_object_previews(manifest, target, destination)
    return save_manifest(root, manifest)


def add_preview_urls(manifest: Dict[str, Any], root: str, assets_root: str) -> Dict[str, Any]:
    job = job_dir(root, manifest["project_id"], manifest["id"])
    preview_dir = job / "previews"
    manifest["pages"] = []
    for slide_number in range(1, int(manifest.get("source_slide_count") or 0) + 1):
        preview = preview_dir / f"slide-{slide_number}.png"
        objects = [item for item in manifest.get("image_objects") or [] if slide_number in (item.get("slide_numbers") or [])]
        text_count = len([item for item in manifest.get("text_objects") or [] if slide_number in (item.get("slide_numbers") or []) and item.get("scope") == "slide"])
        manifest["pages"].append({
            "slide_number": slide_number,
            "preview_url": _asset_url(preview, assets_root) if preview.is_file() else "",
            "image_count": len([item for item in objects if item.get("scope") == "slide"]),
            "protected_count": len([item for item in objects if item.get("protected")]),
            "text_count": text_count,
            "statuses": sorted(set(str(item.get("match_status") or "unreviewed") for item in objects)),
        })
    for item in manifest.get("image_objects") or []:
        relative = str(item.get("source_preview_rel") or "")
        path = job / relative if relative else None
        item["source_preview_url"] = _asset_url(path, assets_root) if path and path.is_file() else ""
    return manifest


def _asset_url(path: Path, assets_root: str) -> str:
    relative = path.resolve().relative_to(Path(assets_root).resolve())
    return "/assets/" + "/".join(relative.parts)


def _preview_copy(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(source, "r") as src, zipfile.ZipFile(destination, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename.endswith(".xml") and item.filename.startswith("ppt/"):
                text = data.decode("utf-8", errors="ignore")
                for old, new in FONT_SUBSTITUTIONS.items():
                    text = text.replace(f'typeface="{old}"', f'typeface="{new}"')
                data = text.encode("utf-8")
            dst.writestr(item, data)


def _text_draft_copy(source: Path, destination: Path, manifest: Dict[str, Any]) -> None:
    """Create a preview-only PPTX with persisted text drafts applied in place."""
    changed_by_part: Dict[str, List[Dict[str, Any]]] = {}
    for item in manifest.get("text_objects") or []:
        if item.get("changed") and item.get("scope") == "slide":
            changed_by_part.setdefault(str(item.get("part_path") or ""), []).append(item)
    if not changed_by_part:
        shutil.copy2(source, destination)
        return
    with zipfile.ZipFile(source, "r") as src, zipfile.ZipFile(destination, "w") as dst:
        for archive_item in src.infolist():
            data = src.read(archive_item.filename)
            text_items = changed_by_part.get(archive_item.filename)
            if text_items:
                root_xml = ET.fromstring(data)
                for text_item in text_items:
                    _apply_text_edit(root_xml, text_item)
                data = ET.tostring(root_xml, encoding="utf-8", xml_declaration=True)
            dst.writestr(archive_item, data)


def render_template_previews(manifest: Dict[str, Any], root: str) -> List[int]:
    job = job_dir(root, manifest["project_id"], manifest["id"])
    source = job / manifest["template_filename"]
    preview_dir = job / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    soffice_candidates = [path for path in (shutil.which("soffice"), "/Applications/LibreOffice.app/Contents/MacOS/soffice") if path and Path(path).is_file()]
    pdftoppm = shutil.which("pdftoppm")
    if not soffice_candidates or not pdftoppm:
        return []
    with tempfile.TemporaryDirectory(prefix="ppt-preview-") as temp_dir:
        draft = Path(temp_dir) / f"draft-{source.name}"
        _text_draft_copy(source, draft, manifest)
        compatible = Path(temp_dir) / source.name
        _preview_copy(draft, compatible)
        pdf: Optional[Path] = None
        for index, soffice in enumerate(soffice_candidates):
            profile = Path(temp_dir) / f"libreoffice-profile-{index}"
            profile.mkdir(parents=True, exist_ok=True)
            try:
                subprocess.run([soffice, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--convert-to", "pdf", "--outdir", temp_dir, str(compatible)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
                pdfs = list(Path(temp_dir).glob("*.pdf"))
                if pdfs:
                    pdf = pdfs[0]
                    break
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                continue
        if not pdf:
            return []
        prefix = Path(temp_dir) / "slide"
        subprocess.run([pdftoppm, "-png", "-r", "110", str(pdf), str(prefix)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        rendered: List[int] = []
        for source_png in sorted(Path(temp_dir).glob("slide-*.png")):
            match = re.search(r"-(\d+)\.png$", source_png.name)
            if match:
                slide_number = int(match.group(1))
                shutil.copy2(source_png, preview_dir / f"slide-{slide_number}.png")
                rendered.append(slide_number)
        return rendered


def save_replacement_image(root: str, project_id: str, job_id: str, slot_id: str, content: bytes, original_name: str) -> Dict[str, Any]:
    if len(content) > MAX_IMAGE_BYTES:
        raise PptWorkbenchError("替换图片超过 30MB")
    fingerprint = fingerprint_image_bytes(content)
    if not fingerprint.get("width"):
        raise PptWorkbenchError("无法识别替换图片")
    extension = mimetypes.guess_extension(str(fingerprint.get("mime_type") or "")) or ".png"
    if extension == ".jpe":
        extension = ".jpg"
    images_dir = job_dir(root, project_id, job_id) / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    target = images_dir / f"{_safe_component(slot_id, prefix='图片对象')}-{uuid.uuid4().hex[:10]}{extension}"
    target.write_bytes(content)
    return {"path": str(target), "original_name": Path(original_name).name, "byte_size": len(content), **fingerprint}


def _object_by_id(manifest: Dict[str, Any], object_id: str) -> Optional[Dict[str, Any]]:
    return next((item for item in manifest.get("image_objects") or [] if item.get("id") == object_id), None)


def assign_image_object(root: str, project_id: str, job_id: str, object_id: str, assignment: Dict[str, Any], apply_scope: str = "object") -> Dict[str, Any]:
    manifest = load_manifest(root, project_id, job_id)
    target = _object_by_id(manifest, object_id)
    if not target:
        raise PptWorkbenchError("PPT 图片对象不存在")
    if target.get("protected") and apply_scope not in {"source_group", "layout_group"}:
        raise PptWorkbenchError("该图片属于受保护的版式或装饰对象，需要确认应用范围")
    targets = [target]
    if apply_scope in {"source_group", "layout_group"}:
        targets = [item for item in manifest.get("image_objects") or [] if item.get("group_id") == target.get("group_id")]
    for item in targets:
        item["assignment"] = dict(assignment)
        item["keep_original"] = False
        item["match_status"] = "replaced"
    manifest["status"] = "ready"
    return save_manifest(root, manifest)


def assign_slot(root: str, project_id: str, job_id: str, slot_id: str, assignment: Dict[str, Any]) -> Dict[str, Any]:
    return assign_image_object(root, project_id, job_id, slot_id, assignment)


def clear_image_assignment(root: str, project_id: str, job_id: str, object_id: str, *, keep_original: bool = True) -> Dict[str, Any]:
    manifest = load_manifest(root, project_id, job_id)
    target = _object_by_id(manifest, object_id)
    if not target:
        raise PptWorkbenchError("PPT 图片对象不存在")
    target["assignment"] = None
    target["keep_original"] = bool(keep_original)
    target["match_status"] = "kept" if keep_original else ("protected" if target.get("protected") else "unreviewed")
    return save_manifest(root, manifest)


def update_text_object(root: str, project_id: str, job_id: str, object_id: str, text: str, revision: int = 0) -> Dict[str, Any]:
    manifest = load_manifest(root, project_id, job_id)
    target = next((item for item in manifest.get("text_objects") or [] if item.get("id") == object_id), None)
    if not target:
        raise PptWorkbenchError("PPT 文字对象不存在")
    if target.get("protected"):
        raise PptWorkbenchError("版式或母版文字默认受保护")
    if revision and revision != int(target.get("revision") or 1):
        raise PptWorkbenchError("文字已在其他页面更新，请刷新后重试")
    value = str(text or "")[:20000]
    target["draft_text"] = value
    target["changed"] = value != str(target.get("original_text") or "")
    target["revision"] = int(target.get("revision") or 1) + 1
    original_length = max(len(str(target.get("original_text") or "")), 1)
    target["overflow_warning"] = len(value) > original_length * 1.35 or value.count("\n") > str(target.get("original_text") or "").count("\n") + 1
    return save_manifest(root, manifest)


def _semantic_score(item: Dict[str, Any], asset: Dict[str, Any]) -> Tuple[float, List[str]]:
    source = item.get("source_fingerprint") or {}
    width, height = int(asset.get("width") or 0), int(asset.get("height") or 0)
    source_ratio = float(source.get("width") or 1) / max(float(source.get("height") or 1), 1)
    asset_ratio = float(width or 1) / max(float(height or 1), 1)
    ratio_delta = abs(math.log(max(source_ratio, 0.001) / max(asset_ratio, 0.001)))
    text = " ".join([str(asset.get("title") or ""), " ".join(asset.get("categories") or []), " ".join(asset.get("tags") or [])]).lower()
    matched = [keyword for keyword in item.get("keywords") or [] if keyword.lower() in text]
    score = max(0.0, 0.35 - min(ratio_delta, 0.35)) + min(0.45, len(matched) * 0.18)
    reasons: List[str] = []
    if ratio_delta <= 0.08:
        reasons.append("图片比例接近")
        score += 0.15
    if matched:
        reasons.append(f"匹配标签：{' / '.join(matched[:3])}")
    if min(width, height) >= 1600:
        reasons.append("分辨率适合演示")
        score += 0.08
    if asset.get("scope") == "project":
        reasons.append("当前项目素材")
        score += 0.08
    return min(score, 0.89), reasons


def scan_recommendations(manifest: Dict[str, Any], template_path: str, assets: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    prepared: List[Dict[str, Any]] = []
    for asset in assets:
        path = str(asset.get("local_path") or "")
        if not path or not os.path.isfile(path):
            continue
        fingerprint = asset.get("fingerprint") or fingerprint_image_path(path)
        prepared.append({**asset, "fingerprint": fingerprint})
    prepared_by_id = {str(item.get("id") or item.get("asset_id") or item.get("library_image_id") or ""): item for item in prepared}
    source_owner: Dict[str, List[str]] = {}
    for source_item in manifest.get("image_objects") or []:
        source_fp = source_item.get("source_fingerprint") or {}
        for key in (str(source_fp.get("sha256") or ""), str(source_fp.get("normalized_sha256") or "")):
            if key:
                source_owner.setdefault(key, []).append(str(source_item.get("id") or ""))
    with zipfile.ZipFile(template_path) as archive:
        for item in manifest.get("image_objects") or []:
            item["recommendations"] = []
            if item.get("object_class") in {"unsupported", "layout"} or not item.get("media_path"):
                item["match_status"] = "protected" if item.get("protected") else "unsupported"
                continue
            source_fp = item.get("source_fingerprint") or _media_fingerprint(archive, str(item.get("media_path") or ""))
            item["source_fingerprint"] = source_fp
            try:
                source_content = archive.read(str(item.get("media_path") or ""))
            except KeyError:
                source_content = b""
            recs: List[Dict[str, Any]] = []
            for asset in prepared:
                fp = asset["fingerprint"]
                exact = bool(source_fp.get("sha256") and source_fp.get("sha256") == fp.get("sha256"))
                pixel_exact = bool(source_fp.get("normalized_sha256") and source_fp.get("normalized_sha256") == fp.get("normalized_sha256"))
                recommendation = {
                    "asset_id": asset.get("id") or asset.get("asset_id") or "",
                    "library_image_id": asset.get("library_image_id") or "",
                    "title": asset.get("title") or "未命名素材",
                    "storage_url": asset.get("storage_url") or "",
                    "width": int(fp.get("width") or asset.get("width") or 0),
                    "height": int(fp.get("height") or asset.get("height") or 0),
                    "scope": asset.get("scope") or "project",
                    "match_type": "semantic", "score": 0.0, "reasons": [],
                }
                if exact or pixel_exact:
                    recommendation.update({"match_type": "exact", "score": 1.0, "reasons": ["与 PPT 当前图片完全一致"]})
                else:
                    source_ratio = float(source_fp.get("width") or 1) / max(float(source_fp.get("height") or 1), 1)
                    asset_ratio = float(fp.get("width") or 1) / max(float(fp.get("height") or 1), 1)
                    ratio_delta = abs(source_ratio / max(asset_ratio, 0.001) - 1)
                    distance = _hamming(str(source_fp.get("phash") or ""), str(fp.get("phash") or ""))
                    similarity, foreground_iou = _visual_similarity(str(asset.get("local_path") or ""), source_content) if source_content and ratio_delta <= 0.08 and distance <= 8 else (0.0, 0.0)
                    if ratio_delta <= 0.08 and distance <= 8 and similarity >= 0.92 and foreground_iou >= 0.85:
                        recommendation.update({
                            "match_type": "update", "score": round(similarity, 4),
                            "reasons": ["与原图高度相似但存在局部变化", f"视觉相似度 {round(similarity * 100)}%"],
                            "difference_percent": round((1 - similarity) * 100, 2),
                        })
                    else:
                        score, reasons = _semantic_score(item, asset)
                        recommendation.update({"score": round(score, 4), "reasons": reasons or ["可作为备选素材"]})
                recs.append(recommendation)
            recs.sort(key=lambda rec: ({"exact": 3, "update": 2, "semantic": 1}.get(rec["match_type"], 0), rec["score"]), reverse=True)
            item["recommendations"] = [rec for rec in recs if rec["match_type"] != "semantic" or rec["score"] >= 0.35][:12]
            exact_rec = next((rec for rec in recs if rec["match_type"] == "exact"), None)
            update_rec = next((rec for rec in recs if rec["match_type"] == "update"), None)
            assignment = item.get("assignment") or {}
            assigned_asset = prepared_by_id.get(str(assignment.get("asset_id") or "")) if assignment else None
            assigned_fp = (assigned_asset or {}).get("fingerprint") or {}
            assigned_matches_elsewhere = any(
                owner_id != item.get("id")
                for fingerprint_key in (str(assigned_fp.get("sha256") or ""), str(assigned_fp.get("normalized_sha256") or ""))
                for owner_id in source_owner.get(fingerprint_key, [])
                if fingerprint_key
            )
            assigned_matches_here = bool(
                (source_fp.get("sha256") and source_fp.get("sha256") == assigned_fp.get("sha256"))
                or (source_fp.get("normalized_sha256") and source_fp.get("normalized_sha256") == assigned_fp.get("normalized_sha256"))
            )
            if assignment and assigned_matches_elsewhere and not assigned_matches_here:
                item["assignment"] = None
                assignment = {}
                item["migration_note"] = "已移除与其他 PPT 图片完全一致的跨对象误指派，请重新确认。"
            if exact_rec:
                item["exact_asset_id"] = exact_rec["asset_id"]
                if assignment and str(assignment.get("asset_id") or "") == str(exact_rec["asset_id"] or ""):
                    item["assignment"] = None
                    item["auto_cleared_noop"] = True
                item["match_status"] = "unchanged" if not item.get("assignment") else "replaced"
            elif assignment:
                item["match_status"] = "replaced"
            elif item.get("keep_original"):
                item["match_status"] = "kept"
            elif update_rec:
                item["match_status"] = "update"
            elif item.get("protected"):
                item["match_status"] = "protected"
            else:
                item["match_status"] = "recommended" if item["recommendations"] else "unreviewed"
    statuses = [str(item.get("match_status") or "") for item in manifest.get("image_objects") or []]
    manifest["recommendation_summary"] = {
        "unchanged": statuses.count("unchanged"), "updates": statuses.count("update"),
        "recommended": statuses.count("recommended"), "replaced": statuses.count("replaced"),
        "protected": statuses.count("protected"), "unreviewed": statuses.count("unreviewed"),
    }
    manifest["last_scanned_at"] = int(time.time() * 1000)
    return manifest


def quality_report(manifest: Dict[str, Any]) -> Dict[str, Any]:
    issues: List[Dict[str, Any]] = []
    image_objects = manifest.get("image_objects") or []
    for item in image_objects:
        assignment = item.get("assignment") or {}
        if assignment:
            width, height = int(assignment.get("width") or 0), int(assignment.get("height") or 0)
            if width and height and min(width, height) < 1200:
                issues.append({"object_id": item["id"], "slide_number": item.get("slide_number") or 0, "severity": "warning", "title": "图片分辨率偏低", "detail": f"{item['label']} 当前为 {width}×{height}"})
            frame = item.get("frame") or {}
            frame_ratio = float(frame.get("width") or 1) / max(float(frame.get("height") or 1), 1)
            image_ratio = float(width or 1) / max(float(height or 1), 1)
            if round(float(item.get("rotation_degrees") or 0)) % 180:
                image_ratio = 1 / max(image_ratio, 0.0001)
            if width and height and max(frame_ratio / image_ratio, image_ratio / frame_ratio) > 1.65:
                issues.append({"object_id": item["id"], "slide_number": item.get("slide_number") or 0, "severity": "warning", "title": "图片比例差异较大", "detail": f"{item['label']} 导出时会按原图框裁切"})
        if item.get("match_status") == "update":
            issues.append({"object_id": item["id"], "slide_number": item.get("slide_number") or 0, "severity": "warning", "title": "发现疑似更新版", "detail": f"{item['label']} 有高相似度候选图，建议对比确认"})
    for item in manifest.get("text_objects") or []:
        if item.get("changed") and item.get("overflow_warning"):
            issues.append({"text_object_id": item["id"], "slide_number": item.get("slide_number") or 0, "severity": "warning", "title": "文字可能溢出", "detail": str(item.get("draft_text") or "")[:60]})
    for font in (manifest.get("font_report") or {}).get("missing_fonts") or []:
        issues.append({"severity": "info", "title": "预览字体替代", "detail": f"{font} 在预览中使用本机中文字体替代，导出仍保留模板字体"})
    error_count = sum(item["severity"] == "error" for item in issues)
    warning_count = sum(item["severity"] == "warning" for item in issues)
    summary = manifest.get("recommendation_summary") or {}
    changed_text = sum(bool(item.get("changed")) for item in manifest.get("text_objects") or [])
    score = max(0, 100 - warning_count * 4 - error_count * 25)
    return {
        "score": score, "status": "passed" if error_count == 0 else "failed",
        "assigned": sum(bool(item.get("assignment")) for item in image_objects),
        "total": len([item for item in image_objects if item.get("scope") == "slide"]),
        "unchanged": int(summary.get("unchanged") or 0), "updates": int(summary.get("updates") or 0),
        "changed_text": changed_text, "error_count": error_count, "warning_count": warning_count,
        "info_count": sum(item["severity"] == "info" for item in issues), "issues": issues,
    }


def _image_for_media(
    path: str,
    rotation_degrees: float = 0,
    source_fingerprint: Optional[Dict[str, Any]] = None,
) -> Tuple[bytes, str]:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source)
        rotation = round(float(rotation_degrees or 0)) % 360
        source_width = int((source_fingerprint or {}).get("width") or 0)
        source_height = int((source_fingerprint or {}).get("height") or 0)
        source_is_landscape = source_width >= source_height if source_width and source_height else None
        replacement_is_landscape = image.width >= image.height
        # Some templates store a portrait media file inside a 90/270-degree
        # rotated frame.  Rotate only when the new file orientation differs
        # from the original media orientation; otherwise preserving the OOXML
        # transform alone is correct and avoids the historical double-rotation.
        if rotation in {90, 270} and source_is_landscape is not None and source_is_landscape != replacement_is_landscape:
            source_rotation = (360 - rotation) % 360
            image = image.rotate(-source_rotation, expand=True)
        output = BytesIO()
        has_alpha = image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info)
        if has_alpha:
            image.save(output, format="PNG", optimize=True)
            return output.getvalue(), ".png"
        if image.mode != "RGB":
            image = image.convert("RGB")
        image.save(output, format="JPEG", quality=94, optimize=True)
        return output.getvalue(), ".jpg"


def _updated_relationship_xml(source_xml: bytes, relationship_id: str, media_filename: str) -> bytes:
    root = ET.fromstring(source_xml)
    relationship = next((item for item in root.findall(f"{{{PACKAGE_RELATIONSHIP_NS}}}Relationship") if str(item.get("Id") or "") == relationship_id), None)
    if relationship is None:
        raise PptWorkbenchError(f"模板图片关系 {relationship_id} 不存在")
    relationship.set("Target", f"../media/{media_filename}")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _spread_text(paragraph: ET.Element, value: str) -> None:
    nodes = paragraph.findall(".//a:t", NS)
    if not nodes:
        run = ET.SubElement(paragraph, f"{{{DRAWING_NS}}}r")
        ET.SubElement(run, f"{{{DRAWING_NS}}}t").text = value
        return
    original_lengths = [len(node.text or "") for node in nodes]
    total = max(sum(original_lengths), 1)
    cursor = 0
    for index, node in enumerate(nodes):
        if index == len(nodes) - 1:
            node.text = value[cursor:]
        else:
            take = round(len(value) * original_lengths[index] / total)
            node.text = value[cursor:cursor + take]
            cursor += take


def _apply_text_edit(root: ET.Element, item: Dict[str, Any]) -> None:
    shape_id = str(item.get("shape_id") or "")
    target = None
    for candidate in root.findall(".//p:sp", NS) + root.findall(".//p:graphicFrame", NS):
        props = candidate.find("./p:nvSpPr/p:cNvPr", NS)
        if props is None:
            props = candidate.find("./p:nvGraphicFramePr/p:cNvPr", NS)
        if props is not None and str(props.get("id") or "") == shape_id:
            target = candidate
            break
    if target is None:
        raise PptWorkbenchError(f"文字对象 {item.get('id')} 在模板中不存在")
    if item.get("kind") == "table_cell":
        tables = target.findall(".//a:tbl", NS)
        table_index = max(int(item.get("table_index") or 1) - 1, 0)
        if table_index >= len(tables):
            raise PptWorkbenchError("表格对象定位失败")
        rows = tables[table_index].findall("./a:tr", NS)
        row_index, column_index = int(item.get("row_index") or 0), int(item.get("column_index") or 0)
        if row_index >= len(rows) or column_index >= len(rows[row_index].findall("./a:tc", NS)):
            raise PptWorkbenchError("表格单元格定位失败")
        target = rows[row_index].findall("./a:tc", NS)[column_index]
    paragraphs = target.findall(".//a:p", NS)
    lines = str(item.get("draft_text") or "").split("\n")
    if not paragraphs:
        return
    for index, paragraph in enumerate(paragraphs):
        value = lines[index] if index < len(lines) else ""
        if index == len(paragraphs) - 1 and len(lines) > len(paragraphs):
            value = "\n".join(lines[index:])
        _spread_text(paragraph, value)


def _ensure_content_types(source_xml: bytes, extensions: Iterable[str]) -> bytes:
    root = ET.fromstring(source_xml)
    existing = {str(item.get("Extension") or "").lower() for item in root.findall(f"{{{CONTENT_TYPES_NS}}}Default")}
    for extension in extensions:
        ext = extension.lstrip(".").lower()
        if ext in existing:
            continue
        content_type = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext)
        if content_type:
            ET.SubElement(root, f"{{{CONTENT_TYPES_NS}}}Default", {"Extension": ext, "ContentType": content_type})
            existing.add(ext)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def export_presentation(root: str, project_id: str, job_id: str, *, resolve_assignment_path) -> Dict[str, Any]:
    manifest = load_manifest(root, project_id, job_id)
    report = quality_report(manifest)
    if report["error_count"]:
        raise PptWorkbenchError("存在阻断问题，暂不能导出")
    job = job_dir(root, project_id, job_id)
    source = job / manifest["template_filename"]
    export_dir = job / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    output_name = f"{manifest['name']}-建筑强排-{timestamp}-{uuid.uuid4().hex[:6]}.pptx"
    output_path = export_dir / re.sub(r'[\\/:*?"<>|]+', "_", output_name)
    with zipfile.ZipFile(source, "r") as source_archive, zipfile.ZipFile(output_path, "w") as output_archive:
        updates: Dict[str, bytes] = {}
        new_media: Dict[str, bytes] = {}
        new_extensions: set[str] = set()
        for item in manifest.get("image_objects") or []:
            assignment = item.get("assignment") or {}
            if not assignment:
                continue
            image_path = resolve_assignment_path(assignment)
            if not image_path or not os.path.isfile(image_path):
                raise PptWorkbenchError(f"{item['label']} 的素材文件不存在")
            data, extension = _image_for_media(
                image_path,
                float(item.get("rotation_degrees") or 0),
                item.get("source_fingerprint") or {},
            )
            media_filename = f"iaw_{_safe_component(str(item['id']), prefix='图片对象')}_{uuid.uuid4().hex[:10]}{extension}"
            new_media[f"ppt/media/{media_filename}"] = data
            new_extensions.add(extension)
            relationships_path = str(item.get("relationship_path") or _rels_path(str(item.get("part_path") or "")))
            relationship_source = updates.get(relationships_path)
            if relationship_source is None:
                try:
                    relationship_source = source_archive.read(relationships_path)
                except KeyError as exc:
                    raise PptWorkbenchError(f"图片关系文件 {relationships_path} 不存在") from exc
            updates[relationships_path] = _updated_relationship_xml(relationship_source, str(item.get("relationship_id") or ""), media_filename)
        by_part: Dict[str, List[Dict[str, Any]]] = {}
        for item in manifest.get("text_objects") or []:
            if item.get("changed"):
                by_part.setdefault(str(item.get("part_path") or ""), []).append(item)
        for part_path, text_items in by_part.items():
            root_xml = ET.fromstring(source_archive.read(part_path))
            for item in text_items:
                _apply_text_edit(root_xml, item)
            updates[part_path] = ET.tostring(root_xml, encoding="utf-8", xml_declaration=True)
        if new_extensions:
            updates["[Content_Types].xml"] = _ensure_content_types(source_archive.read("[Content_Types].xml"), new_extensions)
        for archive_item in source_archive.infolist():
            output_archive.writestr(archive_item, updates.get(archive_item.filename, source_archive.read(archive_item.filename)))
        for media_path, data in new_media.items():
            output_archive.writestr(media_path, data, compress_type=zipfile.ZIP_DEFLATED)
    if _validate_pptx(output_path) != int(manifest.get("source_slide_count") or 0):
        output_path.unlink(missing_ok=True)
        raise PptWorkbenchError("导出页数与原始母版不一致，已停止交付")
    export_id = f"export_{uuid.uuid4().hex}"
    record = {"id": export_id, "filename": output_path.name, "relative_path": str(output_path.relative_to(job)), "created_at": int(time.time() * 1000), "quality": report}
    manifest.setdefault("exports", []).insert(0, record)
    manifest["status"] = "exported"
    save_manifest(root, manifest)
    return {"manifest": manifest, "export": record, "path": str(output_path), "quality": report}


def export_path(root: str, project_id: str, job_id: str, export_id: str) -> Path:
    manifest = load_manifest(root, project_id, job_id)
    record = next((item for item in manifest.get("exports") or [] if item.get("id") == export_id), None)
    if not record:
        raise FileNotFoundError(export_id)
    path = job_dir(root, project_id, job_id) / str(record["relative_path"])
    if not path.is_file():
        raise FileNotFoundError(export_id)
    return path
