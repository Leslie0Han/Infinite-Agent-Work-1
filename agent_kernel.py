import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional


JsonDict = Dict[str, Any]
ToolHandler = Callable[[JsonDict, "ToolExecutionContext"], Any]
Planner = Callable[[JsonDict], Awaitable[JsonDict]]
EventHandler = Callable[[JsonDict], Any]
CancelCheck = Callable[[], bool]


class AgentKernelError(RuntimeError):
    pass


class ToolNotFoundError(AgentKernelError):
    pass


class ToolPermissionError(AgentKernelError):
    pass


class ToolInputError(AgentKernelError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: JsonDict
    handler: ToolHandler
    writes: bool = False
    permissions: List[str] = field(default_factory=list)
    scopes: List[str] = field(default_factory=list)

    def manifest(self) -> JsonDict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "writes": self.writes,
            "permissions": list(self.permissions),
            "scopes": list(self.scopes),
        }


@dataclass
class ToolExecutionContext:
    goal: str
    context: JsonDict
    state: JsonDict
    history: List[JsonDict]
    step: int


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        name = str(spec.name or "").strip()
        if not name:
            raise ValueError("工具名称不能为空")
        if name in self._tools:
            raise ValueError(f"工具已注册：{name}")
        self._tools[name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(f"未知工具：{name}") from exc

    def manifest(self, allowed: Optional[Iterable[str]] = None) -> List[JsonDict]:
        allowed_names = set(allowed) if allowed is not None else None
        return [
            spec.manifest()
            for name, spec in self._tools.items()
            if allowed_names is None or name in allowed_names
        ]

    async def execute(
        self,
        name: str,
        arguments: JsonDict,
        execution_context: ToolExecutionContext,
        *,
        allow_writes: bool,
        allowed: Optional[Iterable[str]] = None,
        granted_permissions: Optional[Iterable[str]] = None,
        current_scope: str = "",
    ) -> Any:
        if allowed is not None and name not in set(allowed):
            raise ToolPermissionError(f"当前任务未授权使用工具：{name}")
        spec = self.get(name)
        if current_scope and spec.scopes and current_scope not in spec.scopes:
            raise ToolPermissionError(f"工具 {name} 不允许在 {current_scope} 作用域执行")
        if granted_permissions is not None:
            granted = set(granted_permissions)
            missing_permissions = [permission for permission in spec.permissions if permission not in granted]
            if missing_permissions:
                raise ToolPermissionError(f"工具 {name} 缺少权限：{', '.join(missing_permissions)}")
        if spec.writes and not allow_writes:
            raise ToolPermissionError(f"工具 {name} 会写入数据，需要用户确认后才能执行")
        clean_arguments = arguments if isinstance(arguments, dict) else {}
        self._validate_arguments(spec.input_schema, clean_arguments)
        result = spec.handler(clean_arguments, execution_context)
        if inspect.isawaitable(result):
            result = await result
        return result

    @staticmethod
    def _validate_arguments(schema: JsonDict, arguments: JsonDict) -> None:
        required = schema.get("required") or []
        missing = [name for name in required if name not in arguments]
        if missing:
            raise ToolInputError(f"缺少工具参数：{', '.join(missing)}")
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            unexpected = [name for name in arguments if name not in properties]
            if unexpected:
                raise ToolInputError(f"工具包含未声明参数：{', '.join(unexpected)}")
        type_map = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        for name, value in arguments.items():
            expected_name = (properties.get(name) or {}).get("type")
            expected = type_map.get(expected_name)
            if expected_name in {"integer", "number"} and isinstance(value, bool):
                raise ToolInputError(f"工具参数 {name} 应为 {expected_name}")
            if expected and not isinstance(value, expected):
                raise ToolInputError(f"工具参数 {name} 应为 {expected_name}")
            property_schema = properties.get(name) or {}
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if property_schema.get("minimum") is not None and value < property_schema["minimum"]:
                    raise ToolInputError(f"工具参数 {name} 不能小于 {property_schema['minimum']}")
                if property_schema.get("maximum") is not None and value > property_schema["maximum"]:
                    raise ToolInputError(f"工具参数 {name} 不能大于 {property_schema['maximum']}")


class AgentLoop:
    def __init__(
        self,
        registry: ToolRegistry,
        planner: Planner,
        *,
        max_steps: int = 8,
        max_consecutive_errors: int = 3,
    ) -> None:
        self.registry = registry
        self.planner = planner
        self.max_steps = max(1, min(24, int(max_steps or 8)))
        self.max_consecutive_errors = max(1, min(8, int(max_consecutive_errors or 3)))

    async def run(
        self,
        goal: str,
        context: Optional[JsonDict] = None,
        *,
        allowed_tools: Optional[Iterable[str]] = None,
        allow_writes: bool = False,
        initial_state: Optional[JsonDict] = None,
        is_cancelled: Optional[CancelCheck] = None,
        on_event: Optional[EventHandler] = None,
        completion_contract: Optional[List[str]] = None,
        required_tools: Optional[List[str]] = None,
        granted_permissions: Optional[Iterable[str]] = None,
        current_scope: str = "",
    ) -> JsonDict:
        context = dict(context or {})
        state = dict(initial_state or {})
        history: List[JsonDict] = []
        allowed = list(allowed_tools) if allowed_tools is not None else None
        tools = self.registry.manifest(allowed)
        if not tools:
            raise AgentKernelError("当前任务没有可用工具")
        consecutive_errors = 0
        repeated_calls: Dict[str, int] = {}

        for step in range(1, self.max_steps + 1):
            if is_cancelled and is_cancelled():
                return {"status": "cancelled", "history": history, "state": state}

            planner_payload = {
                "goal": goal,
                "context": self._compact(context),
                "state": self._compact(state),
                "history": self._compact(history[-8:]),
                "tools": tools,
                "step": step,
                "max_steps": self.max_steps,
                "completion_contract": completion_contract or [],
                "required_tools": required_tools or [],
            }
            decision = await self.planner(planner_payload)
            if not isinstance(decision, dict):
                raise AgentKernelError("规划器没有返回结构化决策")
            action = str(decision.get("action") or "").strip().lower()

            if action == "finish":
                successful_tools = {item.get("tool") for item in history if item.get("ok") is True}
                missing_tools = [name for name in (required_tools or []) if name not in successful_tools]
                if missing_tools:
                    failed_required = {
                        item.get("tool")
                        for item in history
                        if item.get("ok") is False and item.get("tool") in missing_tools
                    }
                    if failed_required:
                        answer = str(decision.get("answer") or decision.get("reason") or "任务已完成可执行部分").strip()
                        event = {
                            "type": "agent_partial",
                            "step": step,
                            "missing_tools": missing_tools,
                            "message": answer,
                        }
                        await self._emit(on_event, event)
                        state["completion_contract"] = {"satisfied": False, "missing_tools": missing_tools}
                        return {
                            "status": "partial",
                            "answer": answer,
                            "missing_tools": missing_tools,
                            "history": history,
                            "state": state,
                            "steps": step - 1,
                        }
                    contract_event = {
                        "type": "completion_blocked",
                        "step": step,
                        "missing_tools": missing_tools,
                        "message": f"完成条件未满足：{', '.join(missing_tools)}",
                    }
                    history.append({
                        "step": step,
                        "type": "completion_contract",
                        "ok": False,
                        "missing_tools": missing_tools,
                    })
                    state["completion_contract"] = {"satisfied": False, "missing_tools": missing_tools}
                    await self._emit(on_event, contract_event)
                    continue
                answer = str(decision.get("answer") or decision.get("reason") or "任务已完成").strip()
                event = {"type": "agent_finished", "step": step, "message": answer}
                await self._emit(on_event, event)
                return {
                    "status": "succeeded",
                    "answer": answer,
                    "history": history,
                    "state": state,
                    "steps": step - 1,
                }

            if action != "tool":
                raise AgentKernelError("规划器决策必须是 tool 或 finish")

            tool_name = str(decision.get("tool") or "").strip()
            arguments = decision.get("arguments") if isinstance(decision.get("arguments"), dict) else {}
            signature = json.dumps([tool_name, arguments], ensure_ascii=False, sort_keys=True)
            repeated_calls[signature] = repeated_calls.get(signature, 0) + 1
            if repeated_calls[signature] > 2:
                raise AgentKernelError(f"工具调用重复超过安全限制：{tool_name}")

            await self._emit(on_event, {
                "type": "tool_started",
                "step": step,
                "tool": tool_name,
                "arguments": self._compact(arguments),
                "message": str(decision.get("reason") or f"正在调用 {tool_name}"),
            })
            execution_context = ToolExecutionContext(
                goal=goal,
                context=context,
                state=state,
                history=history,
                step=step,
            )
            try:
                result = await self.registry.execute(
                    tool_name,
                    arguments,
                    execution_context,
                    allow_writes=allow_writes,
                    allowed=allowed,
                    granted_permissions=granted_permissions,
                    current_scope=current_scope,
                )
                record = {
                    "step": step,
                    "tool": tool_name,
                    "arguments": self._compact(arguments),
                    "result": self._compact(result),
                    "ok": True,
                }
                history.append(record)
                state.setdefault("tool_results", {})[tool_name] = result
                consecutive_errors = 0
                await self._emit(on_event, {
                    "type": "tool_completed",
                    "step": step,
                    "tool": tool_name,
                    "result": self._compact(result),
                    "message": f"工具 {tool_name} 执行完成",
                })
            except Exception as exc:
                consecutive_errors += 1
                error = str(getattr(exc, "detail", None) or exc)
                history.append({
                    "step": step,
                    "tool": tool_name,
                    "arguments": self._compact(arguments),
                    "error": error,
                    "ok": False,
                })
                await self._emit(on_event, {
                    "type": "tool_failed",
                    "step": step,
                    "tool": tool_name,
                    "error": error,
                    "message": f"工具 {tool_name} 失败：{error}",
                })
                if consecutive_errors >= self.max_consecutive_errors:
                    raise AgentKernelError(f"连续 {consecutive_errors} 次工具执行失败，任务已停止") from exc

        raise AgentKernelError(f"Agent 达到最大执行步数 {self.max_steps}，仍未完成任务")

    @staticmethod
    async def _emit(handler: Optional[EventHandler], event: JsonDict) -> None:
        if not handler:
            return
        result = handler(event)
        if inspect.isawaitable(result):
            await result

    @classmethod
    def _compact(cls, value: Any, depth: int = 0) -> Any:
        if depth >= 5:
            return "[truncated]"
        if isinstance(value, dict):
            compacted = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 32:
                    compacted["..."] = "[truncated]"
                    break
                compacted[str(key)] = cls._compact(item, depth + 1)
            return compacted
        if isinstance(value, list):
            return [cls._compact(item, depth + 1) for item in value[:24]]
        if isinstance(value, str) and len(value) > 2000:
            return value[:2000] + "..."
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)
