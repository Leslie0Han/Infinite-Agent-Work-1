import json
import os
import sys
from collections import Counter
from typing import Any, Dict


SERVER_INFO = {"name": "Infinite Agent Work Project Reader", "version": "1.0.0"}
SKIP_DIRECTORIES = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}


def workspace_summary(arguments: Dict[str, Any]) -> Dict[str, Any]:
    root = os.path.abspath(os.environ.get("IAW_MCP_WORKSPACE") or os.getcwd())
    max_files = max(1, min(100, int(arguments.get("max_files") or 40)))
    extensions = Counter()
    top_level = []
    sample_files = []
    total_files = 0
    total_directories = 0

    try:
        top_level = sorted(name for name in os.listdir(root) if name not in SKIP_DIRECTORIES)[:80]
    except OSError:
        top_level = []

    for current, directories, files in os.walk(root):
        directories[:] = sorted(name for name in directories if name not in SKIP_DIRECTORIES and not name.startswith(".audit-"))
        total_directories += len(directories)
        for filename in sorted(files):
            if filename.startswith(".codex-"):
                continue
            total_files += 1
            suffix = os.path.splitext(filename)[1].lower() or "[no-extension]"
            extensions[suffix] += 1
            if len(sample_files) < max_files:
                sample_files.append(os.path.relpath(os.path.join(current, filename), root))
        if total_files >= 20000:
            break

    return {
        "workspace": os.path.basename(root),
        "root": root,
        "read_only": True,
        "total_files": total_files,
        "total_directories": total_directories,
        "top_level": top_level,
        "extension_counts": dict(extensions.most_common(20)),
        "sample_files": sample_files,
        "truncated": total_files >= 20000 or total_files > len(sample_files),
    }


TOOLS = [{
    "name": "workspace_summary",
    "description": "Read a safe structural summary of the current Infinite Agent Work workspace without reading file contents or writing anything.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "max_files": {"type": "integer", "minimum": 1, "maximum": 100}
        },
        "additionalProperties": False,
    },
}]


def result_for(request: Dict[str, Any]) -> Dict[str, Any]:
    method = request.get("method")
    params = request.get("params") or {}
    if method == "initialize":
        return {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        if params.get("name") != "workspace_summary":
            raise ValueError(f"Unknown tool: {params.get('name')}")
        summary = workspace_summary(params.get("arguments") or {})
        return {
            "content": [{"type": "text", "text": json.dumps(summary, ensure_ascii=False)}],
            "structuredContent": summary,
            "isError": False,
        }
    raise ValueError(f"Unsupported method: {method}")


def main() -> None:
    for line in sys.stdin:
        request: Dict[str, Any] = {}
        try:
            request = json.loads(line)
            if "id" not in request:
                continue
            response = {"jsonrpc": "2.0", "id": request["id"], "result": result_for(request)}
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32603, "message": str(exc)},
            }
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
