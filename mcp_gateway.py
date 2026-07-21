import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from agent_kernel import ToolRegistry, ToolSpec


class MCPGatewayError(RuntimeError):
    pass


@dataclass(frozen=True)
class MCPServerConfig:
    id: str
    name: str
    command: str
    args: List[str] = field(default_factory=list)
    cwd: str = ""
    env: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    read_only: bool = True
    timeout_seconds: float = 8.0

    def public_record(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "transport": "stdio",
            "enabled": self.enabled,
            "read_only": self.read_only,
        }


class MCPGateway:
    def __init__(self, servers: Optional[List[MCPServerConfig]] = None):
        self._servers = {server.id: server for server in (servers or [])}

    def list_servers(self) -> List[Dict[str, Any]]:
        return [server.public_record() for server in self._servers.values()]

    def get_server(self, server_id: str) -> MCPServerConfig:
        server = self._servers.get(str(server_id or "").strip())
        if not server:
            raise MCPGatewayError(f"未知 MCP Server：{server_id}")
        if not server.enabled:
            raise MCPGatewayError(f"MCP Server 已停用：{server_id}")
        return server

    @staticmethod
    def namespaced_tool_name(server_id: str, tool_name: str) -> str:
        namespace = str(server_id or "").replace("-", "_")
        return f"mcp.{namespace}.{tool_name}"

    async def _session_request(
        self,
        server: MCPServerConfig,
        method: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        inherited_names = ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "WINDIR")
        env = {name: os.environ[name] for name in inherited_names if os.environ.get(name)}
        env.update(server.env or {})
        process = await asyncio.create_subprocess_exec(
            server.command,
            *server.args,
            cwd=server.cwd or None,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        request_id = 0

        async def send(payload: Dict[str, Any]) -> None:
            if not process.stdin:
                raise MCPGatewayError(f"MCP Server 无法写入：{server.id}")
            process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await process.stdin.drain()

        async def request(request_method: str, request_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            nonlocal request_id
            request_id += 1
            current_id = request_id
            await send({
                "jsonrpc": "2.0",
                "id": current_id,
                "method": request_method,
                "params": request_params or {},
            })
            while True:
                if not process.stdout:
                    raise MCPGatewayError(f"MCP Server 无响应：{server.id}")
                line = await asyncio.wait_for(process.stdout.readline(), timeout=server.timeout_seconds)
                if not line:
                    error_text = ""
                    if process.stderr:
                        error_text = (await process.stderr.read()).decode("utf-8", errors="replace")[:600]
                    raise MCPGatewayError(f"MCP Server 提前退出：{server.id} {error_text}".strip())
                response = json.loads(line.decode("utf-8"))
                if response.get("id") != current_id:
                    continue
                if response.get("error"):
                    error = response["error"]
                    raise MCPGatewayError(str(error.get("message") or error))
                return response.get("result") or {}

        try:
            await request("initialize", {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "Infinite Agent Work", "version": "1.0.0"},
            })
            await send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            return await request(method, params)
        except asyncio.TimeoutError as exc:
            raise MCPGatewayError(f"MCP Server 超时：{server.id}") from exc
        except json.JSONDecodeError as exc:
            raise MCPGatewayError(f"MCP Server 返回了无效 JSON：{server.id}") from exc
        finally:
            if process.stdin:
                process.stdin.close()
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

    async def discover_tools(self, server_ids: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        wanted = set(server_ids) if server_ids is not None else None
        tools: List[Dict[str, Any]] = []
        for server in self._servers.values():
            if not server.enabled or (wanted is not None and server.id not in wanted):
                continue
            result = await self._session_request(server, "tools/list")
            for tool in result.get("tools") or []:
                if not isinstance(tool, dict) or not tool.get("name"):
                    continue
                tools.append({
                    "name": self.namespaced_tool_name(server.id, str(tool["name"])),
                    "server_id": server.id,
                    "server_name": server.name,
                    "remote_name": str(tool["name"]),
                    "description": str(tool.get("description") or ""),
                    "input_schema": tool.get("inputSchema") or {"type": "object", "properties": {}},
                    "writes": not server.read_only,
                    "permissions": [f"mcp:{server.id}:{'write' if not server.read_only else 'read'}"],
                    "scopes": ["home", "library", "smart-canvas", "wiki"],
                })
        return tools

    async def call_tool(self, server_id: str, tool_name: str, arguments: Dict[str, Any]) -> Any:
        server = self.get_server(server_id)
        result = await self._session_request(server, "tools/call", {
            "name": tool_name,
            "arguments": arguments or {},
        })
        if result.get("isError"):
            messages = [item.get("text", "") for item in (result.get("content") or []) if isinstance(item, dict)]
            raise MCPGatewayError("\n".join(messages).strip() or f"MCP 工具失败：{tool_name}")
        if "structuredContent" in result:
            return result["structuredContent"]
        texts = [item.get("text", "") for item in (result.get("content") or []) if isinstance(item, dict) and item.get("type") == "text"]
        text = "\n".join(texts).strip()
        try:
            return json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return {"text": text}

    async def register_tools(
        self,
        registry: ToolRegistry,
        server_ids: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        manifests = await self.discover_tools(server_ids)
        for manifest in manifests:
            server_id = manifest["server_id"]
            remote_name = manifest["remote_name"]

            async def handler(arguments, execution, sid=server_id, remote=remote_name):
                return await self.call_tool(sid, remote, arguments)

            registry.register(ToolSpec(
                name=manifest["name"],
                description=manifest["description"],
                input_schema=manifest["input_schema"],
                handler=handler,
                writes=bool(manifest["writes"]),
                permissions=list(manifest["permissions"]),
                scopes=list(manifest["scopes"]),
            ))
        return manifests

    async def health(self, server_id: str) -> Dict[str, Any]:
        server = self.get_server(server_id)
        try:
            tools = await self.discover_tools([server_id])
            return {
                **server.public_record(),
                "status": "connected",
                "tool_count": len(tools),
                "tools": [tool["name"] for tool in tools],
            }
        except Exception as exc:
            return {
                **server.public_record(),
                "status": "error",
                "tool_count": 0,
                "tools": [],
                "error": str(exc),
            }
