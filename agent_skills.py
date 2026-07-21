import json
import os
from typing import Any, Dict, List, Optional


def _frontmatter_value(raw: str) -> Any:
    value = str(raw or "").strip()
    if not value:
        return ""
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.startswith("[") or value.startswith("{"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value.strip('"\'')


def parse_skill_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    metadata: Dict[str, Any] = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            header = text[4:end]
            body = text[end + 5:].strip()
            for line in header.splitlines():
                if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                metadata[key.strip()] = _frontmatter_value(value)
    skill_id = str(metadata.get("id") or os.path.basename(os.path.dirname(path))).strip()
    return {
        "id": skill_id,
        "name": str(metadata.get("name") or skill_id).strip(),
        "description": str(metadata.get("description") or "").strip(),
        "version": str(metadata.get("version") or "1.0.0").strip(),
        "enabled": bool(metadata.get("enabled", True)),
        "task_types": list(metadata.get("task_types") or []),
        "scopes": list(metadata.get("scopes") or []),
        "allowed_tools": list(metadata.get("allowed_tools") or []),
        "mcp_servers": list(metadata.get("mcp_servers") or []),
        "instructions": body,
        "path": os.path.abspath(path),
    }


class SkillRegistry:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    def list(self, include_disabled: bool = False) -> List[Dict[str, Any]]:
        if not os.path.isdir(self.root):
            return []
        skills = []
        for directory in sorted(os.listdir(self.root)):
            path = os.path.join(self.root, directory, "SKILL.md")
            if not os.path.isfile(path):
                continue
            try:
                skill = parse_skill_file(path)
            except Exception as exc:
                skill = {
                    "id": directory,
                    "name": directory,
                    "description": "",
                    "version": "",
                    "enabled": False,
                    "task_types": [],
                    "scopes": [],
                    "allowed_tools": [],
                    "mcp_servers": [],
                    "instructions": "",
                    "path": os.path.abspath(path),
                    "error": str(exc),
                }
            if include_disabled or skill.get("enabled"):
                skills.append(skill)
        return skills

    def get(self, skill_id: str) -> Optional[Dict[str, Any]]:
        wanted = str(skill_id or "").strip()
        return next((skill for skill in self.list(include_disabled=True) if skill.get("id") == wanted), None)

    def resolve(
        self,
        *,
        task_type: str = "",
        scope: str = "",
        requested_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        requested = {str(item).strip() for item in (requested_ids or []) if str(item).strip()}
        resolved = []
        for skill in self.list():
            if requested and skill.get("id") not in requested:
                continue
            task_types = set(skill.get("task_types") or [])
            scopes = set(skill.get("scopes") or [])
            if not requested and task_types and task_type not in task_types:
                continue
            if scope and scopes and scope not in scopes:
                continue
            resolved.append(skill)
        return resolved

    @staticmethod
    def public_record(skill: Dict[str, Any], include_instructions: bool = False) -> Dict[str, Any]:
        record = {
            key: skill.get(key)
            for key in (
                "id", "name", "description", "version", "enabled", "task_types",
                "scopes", "allowed_tools", "mcp_servers", "error",
            )
            if key in skill
        }
        if include_instructions:
            record["instructions"] = skill.get("instructions") or ""
        return record
