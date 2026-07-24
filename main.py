import json
import uuid
import base64
import urllib.request
import urllib.parse
import urllib.error
import os
import sys
import re
import random
import time
import shutil
import subprocess
import asyncio
import logging
import zipfile
import requests
import hashlib
import tempfile
import math
import xml.etree.ElementTree as ET
from collections import Counter
from typing import List, Dict, Any, Optional
from threading import Lock
import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from io import BytesIO
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Header, Request, BackgroundTasks, Form
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from agent_runtime import (
    cancel_task as agent_cancel_task,
    confirm_task as agent_confirm_task,
    create_plan_task,
    fail_task as agent_fail_task,
    list_agent_tools,
    list_tasks as list_agent_tasks,
    load_task as load_agent_task,
    start_task as agent_start_task,
    task_is_cancelled as agent_task_is_cancelled,
    update_task_plan as agent_update_task_plan,
    update_task_progress as agent_update_task_progress,
    update_task_partial as agent_update_task_partial,
    update_task_result as agent_update_task_result,
)
from agent_kernel import AgentKernelError, AgentLoop, ToolRegistry, ToolSpec
from agent_skills import SkillRegistry
from domain_store import DomainStore
from mcp_gateway import MCPGateway, MCPServerConfig
from ppt_workbench import (
    PptWorkbenchError,
    add_preview_urls,
    assign_image_object as assign_ppt_image_object,
    assign_slot as assign_ppt_slot,
    clear_image_assignment as clear_ppt_image_assignment,
    create_manifest as create_ppt_manifest,
    export_path as ppt_export_path,
    export_presentation as export_ppt_presentation,
    fingerprint_image_path,
    job_dir as ppt_job_dir,
    list_manifests as list_ppt_manifests,
    load_manifest as load_ppt_manifest,
    quality_report as ppt_quality_report,
    render_template_previews,
    save_replacement_image,
    save_manifest as save_ppt_manifest,
    scan_recommendations as scan_ppt_recommendations,
    update_text_object as update_ppt_text_object,
)

QUIET_ACCESS_PATHS = {
    "/api/queue_status",
    "/api/canvases",
    "/api/canvases/trash",
}
QUIET_ACCESS_PREFIXES = (
    "/api/canvases/",
)

class QuietAccessLogFilter(logging.Filter):
    def filter(self, record):
        args = record.args if isinstance(record.args, tuple) else ()
        if len(args) >= 3:
            path = str(args[2]).split("?", 1)[0]
            status = int(args[4]) if len(args) >= 5 and str(args[4]).isdigit() else 0
            quiet_dynamic = any(path.startswith(prefix) and path.endswith("/meta") for prefix in QUIET_ACCESS_PREFIXES)
            if (path in QUIET_ACCESS_PATHS or quiet_dynamic) and status < 400:
                return False
        message = record.getMessage()
        if any(f'"GET {path}' in message and '" 200' in message for path in QUIET_ACCESS_PATHS):
            return False
        if 'GET /api/canvases/' in message and '/meta' in message and '" 200' in message:
            return False
        return True

logging.getLogger("uvicorn.access").addFilter(QuietAccessLogFilter())

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- WebSocket 状态管理器 ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.user_connections: Dict[str, WebSocket] = {}
        self.connection_clients: Dict[WebSocket, str] = {}

    async def connect(self, websocket: WebSocket, client_id: str = None):
        await websocket.accept()
        self.active_connections.append(websocket)
        self.connection_clients[websocket] = client_id or f"anon-{id(websocket)}"
        if client_id:
            self.user_connections[client_id] = websocket
        print(f"WS Connected. Total: {len(self.active_connections)}, Online: {self.online_count()}")
        await self.broadcast_count()

    async def disconnect(self, websocket: WebSocket, client_id: str = None):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        self.connection_clients.pop(websocket, None)
        if client_id and self.user_connections.get(client_id) is websocket:
            del self.user_connections[client_id]
        print(f"WS Disconnected. Total: {len(self.active_connections)}, Online: {self.online_count()}")
        await self.broadcast_count()

    def online_count(self):
        visible_clients = {
            client_id for client_id in self.connection_clients.values()
            if client_id and not str(client_id).startswith("canvas_")
        }
        return len(visible_clients)

    async def broadcast_count(self):
        count = self.online_count()
        data = json.dumps({"type": "stats", "online_count": count})
        for connection in self.active_connections[:]:
            try:
                await connection.send_text(data)
            except Exception as e:
                print(f"Broadcast error: {e}")
                self.active_connections.remove(connection)

    async def broadcast_new_image(self, image_data: dict):
        data = json.dumps({"type": "new_image", "data": image_data})
        for connection in self.active_connections[:]:
            try:
                await connection.send_text(data)
            except Exception as e:
                print(f"Broadcast image error: {e}")
                self.active_connections.remove(connection)

    async def broadcast_canvas_updated(self, canvas_id: str, updated_at: int, client_id: str = ""):
        data = json.dumps({
            "type": "canvas_updated",
            "canvas_id": canvas_id,
            "updated_at": updated_at,
            "client_id": client_id or "",
        })
        for connection in self.active_connections[:]:
            try:
                await connection.send_text(data)
            except Exception as e:
                print(f"Broadcast canvas error: {e}")
                self.active_connections.remove(connection)

    async def send_personal_message(self, message: dict, client_id: str):
        ws = self.user_connections.get(client_id)
        if ws:
            try:
                await ws.send_text(json.dumps(message))
            except Exception as e:
                print(f"Personal message error for {client_id}: {e}")

manager = ConnectionManager()
GLOBAL_LOOP = None

@app.on_event("startup")
async def startup_event():
    global GLOBAL_LOOP
    GLOBAL_LOOP = asyncio.get_running_loop()
    DOMAIN_STORE.fail_interrupted_generation_tasks()
    sync_legacy_domain_records()
    ensure_builtin_material_library()

@app.websocket("/ws/stats")
async def websocket_endpoint(websocket: WebSocket, client_id: str = None):
    await manager.connect(websocket, client_id)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        await manager.disconnect(websocket, client_id)
    except Exception as e:
        print(f"WS Error: {e}")
        await manager.disconnect(websocket, client_id)

# --- 配置区域 ---

CLIENT_ID = str(uuid.uuid4())

# PyInstaller 打包模式适配
if getattr(sys, 'frozen', False):
    BUNDLE_DIR = sys._MEIPASS
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    BASE_DIR = BUNDLE_DIR

WORKFLOW_DIR = os.path.join(BUNDLE_DIR, "workflows")
WORKFLOW_PATH = os.path.join(WORKFLOW_DIR, "Z-Image.json")
STATIC_DIR = os.path.join(BUNDLE_DIR, "static")
STATIC_RUNNINGHUB_DIR = os.path.join(STATIC_DIR, "runninghub")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
PPT_WORKBENCH_DIR = os.path.join(ASSETS_DIR, "ppt_workbench")
OUTPUT_INPUT_DIR = os.path.join(ASSETS_DIR, "input")
OUTPUT_OUTPUT_DIR = os.path.join(ASSETS_DIR, "output")
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")
API_ENV_FILE = os.path.join(BASE_DIR, "API", ".env")
DATA_DIR = os.path.join(BASE_DIR, "data")
DOMAIN_DATABASE_PATH = os.path.abspath(
    os.environ.get("IAW_DATABASE_PATH") or os.path.join(DATA_DIR, "infinite_agent_work.db")
)
DOMAIN_STORE = DomainStore(DOMAIN_DATABASE_PATH)
CONVERSATION_DIR = os.path.join(DATA_DIR, "conversations")
CANVAS_DIR = os.path.join(DATA_DIR, "canvases")
AGENT_TASK_DIR = os.path.join(DATA_DIR, "agent_tasks")
AGENT_SKILL_DIR = os.path.join(BUNDLE_DIR, "agent_skills")
AGENT_SKILLS = SkillRegistry(AGENT_SKILL_DIR)
MCP_GATEWAY = MCPGateway([
    MCPServerConfig(
        id="project-reader",
        name="Project Reader",
        command=sys.executable,
        args=[os.path.join(BUNDLE_DIR, "mcp_servers", "project_reader.py")],
        cwd=BUNDLE_DIR,
        env={"IAW_MCP_WORKSPACE": BUNDLE_DIR},
        enabled=True,
        read_only=True,
    ),
])
API_PROVIDERS_FILE = os.path.join(DATA_DIR, "api_providers.json")
GLOBAL_CONFIG_FILE = os.path.join(BASE_DIR, "global_config.json")
CANVAS_TRASH_RETENTION_MS = 30 * 24 * 60 * 60 * 1000
AGENT_SMART_CANVAS_LIMIT = 12
AGENT_LIBRARY_TAG_LIMIT = 20
AGENT_WIKI_CONTEXT_LIMIT = 8

QUEUE = []
QUEUE_LOCK = Lock()
HISTORY_LOCK = Lock()
GLOBAL_CONFIG_LOCK = Lock()
CONVERSATION_LOCK = Lock()
CANVAS_LOCK = Lock()
LIBRARY_LOCK = Lock()
LOAD_LOCK = Lock()
AGENT_TASK_LOCK = Lock()
WIKI_LOCK = Lock()
AGENT_RUN_TASKS: Dict[str, asyncio.Task] = {}

LIBRARY_DIR = os.path.join(DATA_DIR, "library")
LIBRARY_SOURCES_FILE = os.path.join(DATA_DIR, "library.json")
LIBRARY_IMAGES_FILE = os.path.join(LIBRARY_DIR, "images.json")
LIBRARY_CATEGORIES_FILE = os.path.join(LIBRARY_DIR, "categories.json")
BUILTIN_MATERIAL_DIR = os.path.join(BASE_DIR, "static", "materials", "polyhaven-cc0")
BUILTIN_MATERIAL_SOURCE_ID = "builtin_polyhaven_cc0"
BUILTIN_MATERIAL_METADATA = {
    "concrete_panels": ("清水混凝土挂板", ["建筑材质", "混凝土"], ["材质", "清水混凝土", "挂板", "外立面"]),
    "concrete_slab_wall": ("混凝土墙板", ["建筑材质", "混凝土"], ["材质", "混凝土", "墙面", "清水混凝土"]),
    "brick_wall_001": ("红砖墙", ["建筑材质", "砖"], ["材质", "红砖", "砖墙", "外立面"]),
    "rectangular_facade_tiles": ("深色立面砖", ["建筑材质", "砖"], ["材质", "立面砖", "深色", "外立面"]),
    "granite_tile_03": ("花岗岩墙砖", ["建筑材质", "石材"], ["材质", "花岗岩", "石材", "墙面"]),
    "marble_01": ("大理石", ["建筑材质", "石材"], ["材质", "大理石", "石材", "室内"]),
    "brown_planks_05": ("深色木饰面", ["建筑材质", "木材"], ["材质", "木材", "木饰面", "深色"]),
    "wood_floor": ("木地板", ["建筑材质", "木材"], ["材质", "木材", "木地板", "室内"]),
    "metal_plate_02": ("金属板", ["建筑材质", "金属"], ["材质", "金属", "金属板", "立面"]),
    "corrugated_iron_02": ("波纹金属板", ["建筑材质", "金属"], ["材质", "金属", "波纹板", "屋面"]),
    "clay_roof_tiles_02": ("陶瓦屋面", ["建筑材质", "屋面"], ["材质", "陶瓦", "屋面", "瓦片"]),
    "concrete_pavers_03": ("混凝土铺装", ["建筑材质", "铺装"], ["材质", "混凝土", "铺装", "室外"]),
}
PROMPT_LIBRARY_PATH = os.path.join(DATA_DIR, "prompt_libraries.json")
WIKI_DIR = os.path.join(DATA_DIR, "wiki")
WIKI_SOURCES_FILE = os.path.join(WIKI_DIR, "sources.json")
WIKI_PAGES_FILE = os.path.join(WIKI_DIR, "pages.json")
WIKI_GRAPH_FILE = os.path.join(WIKI_DIR, "graph.json")
WIKI_RELATIONS_FILE = os.path.join(WIKI_DIR, "relations.json")
WIKI_SUBDIRS = {
    "source": "sources",
    "summary": "summaries",
    "concept": "concepts",
    "project": "projects",
    "qa": os.path.join("outputs", "qa"),
    "report": os.path.join("outputs", "reports"),
    "design": os.path.join("outputs", "design"),
    "health": "health",
}
LOCAL_WIKI_SCHEMA_VERSION = 1
LOCAL_WIKI_DIRS = [
    "00_收件箱",
    os.path.join("00_收件箱", "文章"),
    os.path.join("00_收件箱", "录音笔记"),
    os.path.join("00_收件箱", "视频"),
    os.path.join("00_收件箱", "论文"),
    os.path.join("00_收件箱", "项目资料"),
    os.path.join("00_收件箱", "上传文件"),
    "10_日记",
    "20_项目",
    os.path.join("20_项目", "_模板"),
    "30_研究",
    "40_知识库",
    os.path.join("40_知识库", "摘要"),
    os.path.join("40_知识库", "概念"),
    os.path.join("40_知识库", "索引"),
    "50_资源",
    os.path.join("50_资源", "问答档案"),
    os.path.join("50_资源", "报告"),
    os.path.join("50_资源", "健康检查"),
    "90_计划",
    "99_System",
    os.path.join("99_System", "Templates"),
    os.path.join("99_System", "Prompts"),
    ".agentwiki",
]
EXTERNAL_DIR = os.path.join(DATA_DIR, "external")
AWESOME_GPT_IMAGE_2_REPO = "https://github.com/Leslie0Han/awesome-gpt-image-2.git"
AWESOME_GPT_IMAGE_2_WEB = "https://github.com/Leslie0Han/awesome-gpt-image-2"
AWESOME_GPT_IMAGE_2_DIR = os.path.join(EXTERNAL_DIR, "awesome-gpt-image-2")
AWESOME_GPT_IMAGE_2_LIBRARY_ID = "awesome_gpt_image_2"
STATIC_GPT_IMAGE_2_LIBRARY_ID = "gpt_image2_industrial_templates"
ARCHLIB_DIR = os.path.abspath(os.environ.get("ARCHLIB_DIR") or os.path.join(BASE_DIR, "..", "ArchLib"))
ARCHLIB_CASE_DIR = os.path.join(ARCHLIB_DIR, "案例库")
ARCHLIB_MATERIAL_CACHE: Dict[str, Any] = {"built_at": 0, "items": []}
os.makedirs(LIBRARY_DIR, exist_ok=True)
os.makedirs(EXTERNAL_DIR, exist_ok=True)
os.makedirs(WIKI_DIR, exist_ok=True)
NEXT_TASK_ID = 1

PROVIDER_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{2,40}$")
SUPPORTED_PROVIDER_PROTOCOLS = {"openai", "apimart", "volcengine", "runninghub", "jimeng"}
RUNNINGHUB_DEFAULT_BASE_URL = "https://www.runninghub.cn"
RUNNINGHUB_DEFAULT_IMAGE_MODELS = [
    "seedream-v5-lite/text-to-image",
    "seedream-v5-lite/image-to-image",
]
VOLCENGINE_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
VOLCENGINE_DEFAULT_PROJECT_NAME = "default"
VOLCENGINE_DEFAULT_REGION = "cn-beijing"
VOLCENGINE_DEFAULT_VIDEO_MODELS = [
    "doubao-seedance-2-0-260128",
    "doubao-seedance-2-0-fast-260128",
    "doubao-seedance-1-5-pro-251215",
    "doubao-seedance-1-0-pro-250528",
    "doubao-seedance-1-0-lite-t2v-250428",
    "doubao-seedance-1-0-lite-i2v-250428",
]
JIMENG_DEFAULT_IMAGE_MODELS = ["5.0", "4.6", "4.5", "4.1", "4.0", "3.1", "3.0"]
JIMENG_DEFAULT_VIDEO_MODELS = [
    "seedance2.0_vip",
    "seedance2.0fast_vip",
    "seedance2.0",
    "seedance2.0fast",
    "3.5pro",
    "3.0pro",
    "3.0",
    "3.0fast",
]

def load_env_file():
    if not os.path.exists(API_ENV_FILE):
        return
    try:
        with open(API_ENV_FILE, 'r', encoding='utf-8-sig') as f:
            for raw_line in f.read().splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
    except Exception as e:
        print(f"加载 API/.env 失败: {e}")

load_env_file()

COMFYUI_INSTANCES = [s.strip() for s in os.getenv("COMFYUI_INSTANCES", "127.0.0.1:8188").split(",") if s.strip()]
COMFYUI_ADDRESS = COMFYUI_INSTANCES[0]

AI_BASE_URL = os.getenv("COMFLY_BASE_URL", "https://ai.comfly.chat").rstrip("/")
AI_API_KEY = os.getenv("COMFLY_API_KEY", "")
MODELSCOPE_API_KEY = os.getenv("MODELSCOPE_API_KEY", "")
MODELSCOPE_CHAT_BASE_URL = "https://api-inference.modelscope.cn/v1"
MODELSCOPE_DEFAULT_IMAGE_MODELS = [
    "Tongyi-MAI/Z-Image-Turbo",
    "Qwen/Qwen-Image-2512",
    "Qwen/Qwen-Image-Edit-2511",
    "black-forest-labs/FLUX.2-klein-9B",
]
MODELSCOPE_DEFAULT_CHAT_MODELS = [
    "Qwen/Qwen3-235B-A22B",
    "Qwen/Qwen3-VL-235B-A22B-Instruct",
    "MiniMax/MiniMax-M2.7:MiniMax",
]
_MODELSCOPE_CONFIGURED_CHAT_MODELS = [m.strip() for m in os.getenv("MODELSCOPE_CHAT_MODELS", "").split(",") if m.strip()]
MODELSCOPE_CHAT_MODELS = list(dict.fromkeys([m for m in [*MODELSCOPE_DEFAULT_CHAT_MODELS, *_MODELSCOPE_CONFIGURED_CHAT_MODELS] if m]))
MODELSCOPE_DEFAULT_IMAGE_MODEL = MODELSCOPE_DEFAULT_IMAGE_MODELS[0]
MODELSCOPE_DEFAULT_CHAT_MODEL = "Qwen/Qwen3-235B-A22B"
MODELSCOPE_DEFAULT_LORAS = [
    {
        "id": "Daniel8152/film",
        "name": "Z-Image Film",
        "target_model": "Tongyi-MAI/Z-Image-Turbo",
        "strength": 0.8,
        "enabled": True,
        "note": "",
    },
    {
        "id": "Daniel8152/Qwen-Image-2512-Film",
        "name": "Qwen Image 2512 Film",
        "target_model": "Qwen/Qwen-Image-2512",
        "strength": 0.8,
        "enabled": True,
        "note": "",
    },
    {
        "id": "Daniel8152/Klein-enhance",
        "name": "Klein enhance",
        "target_model": "black-forest-labs/FLUX.2-klein-9B",
        "strength": 0.8,
        "enabled": True,
        "note": "",
    },
]
MODELSCOPE_DEFAULTS_VERSION = 3
CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4o-mini")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-2")
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "30"))
AI_REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "120"))
IMAGE_POLL_INTERVAL = float(os.getenv("IMAGE_POLL_INTERVAL", "2"))
IMAGE_TASK_TIMEOUT = float(os.getenv("IMAGE_TASK_TIMEOUT", str(AI_REQUEST_TIMEOUT)))
COMFYUI_HISTORY_TIMEOUT = int(float(os.getenv("COMFYUI_HISTORY_TIMEOUT", "1800")))
APIMART_IMAGE_TASK_TIMEOUT = float(os.getenv("APIMART_IMAGE_TASK_TIMEOUT", "1800"))
APIMART_IMAGE_POLL_INTERVAL = float(os.getenv("APIMART_IMAGE_POLL_INTERVAL", "5"))
APIMART_IMAGE_INITIAL_POLL_DELAY = float(os.getenv("APIMART_IMAGE_INITIAL_POLL_DELAY", "10"))
VIDEO_POLL_TIMEOUT = float(os.getenv("VIDEO_POLL_TIMEOUT", "1800"))
ONLINE_IMAGE_PROMPT_MAX_LENGTH = int(os.getenv("ONLINE_IMAGE_PROMPT_MAX_LENGTH", "20000"))
VIDEO_PROMPT_MAX_LENGTH = int(os.getenv("VIDEO_PROMPT_MAX_LENGTH", "4000"))
LLM_MESSAGE_MAX_LENGTH = int(os.getenv("LLM_MESSAGE_MAX_LENGTH", "20000"))

FIELD_LABELS = {
    "prompt": "提示词",
    "message": "文本",
    "system_prompt": "系统提示词",
}

def friendly_validation_error(errors):
    parts = []
    for err in errors or []:
        loc = [str(item) for item in err.get("loc", []) if item != "body"]
        field = loc[-1] if loc else ""
        label = FIELD_LABELS.get(field, field or "请求参数")
        ctx = err.get("ctx") or {}
        limit = ctx.get("limit_value") or ctx.get("max_length") or ctx.get("min_length")
        err_type = str(err.get("type") or "")
        msg = str(err.get("msg") or "")
        if "max_length" in err_type or "at most" in msg:
            parts.append(f"{label}过长：当前内容超过后端上限 {limit} 个字符。请拆分为多个提示词节点，或先用 LLM 节点压缩后再生成。")
        elif "min_length" in err_type:
            parts.append(f"{label}不能为空。")
        else:
            parts.append(f"{label}格式不正确：{msg}")
    return "\n".join(parts) or "请求参数不正确。"

@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": friendly_validation_error(exc.errors()), "errors": exc.errors()},
    )

def model_list(env_name, primary, defaults):
    configured = os.getenv(env_name, "")
    configured_values = [item.strip() for item in configured.split(",") if item.strip()]
    values = configured_values or [primary, *defaults]
    deduped = []
    for value in values:
        if value and value not in deduped:
            deduped.append(value)
    return deduped

def reload_env_globals():
    """保存 API 设置后，将 os.environ 里最新的值同步回模块级全局变量，
    避免保存后需要重启才能生效。"""
    global MODELSCOPE_API_KEY, AI_API_KEY, AI_BASE_URL
    global IMAGE_MODELS, CHAT_MODELS, VIDEO_MODELS, MODELSCOPE_CHAT_MODELS
    MODELSCOPE_API_KEY = os.getenv("MODELSCOPE_API_KEY", "")
    AI_API_KEY = os.getenv("COMFLY_API_KEY", "")
    AI_BASE_URL = os.getenv("COMFLY_BASE_URL", "https://ai.comfly.chat").rstrip("/")
    IMAGE_MODELS = model_list("IMAGE_MODELS", os.getenv("IMAGE_MODEL", IMAGE_MODEL), ["nano-banana-pro"])
    CHAT_MODELS = model_list("CHAT_MODELS", os.getenv("CHAT_MODEL", CHAT_MODEL), ["gpt-4o-mini", "gemini-3.1-flash-image-preview-2k"])
    VIDEO_MODELS = model_list("VIDEO_MODELS", "veo3-fast", [
        "veo2", "veo2-fast", "veo2-pro",
        "veo3", "veo3-fast", "veo3-pro",
        "veo3.1", "veo3.1-fast", "veo3.1-pro",
        "sora-2", "sora-2-pro",
        "wan2.6-t2v", "wan2.6-i2v",
        "wan2.5-t2v-preview", "wan2.5-i2v-preview",
        "wan2.2-t2v-plus", "wan2.2-i2v-plus", "wan2.2-i2v-flash",
        "doubao-seedance-2-0-260128",
        "doubao-seedance-2-0-fast-260128",
        "doubao-seedance-1-5-pro-251215",
        "doubao-seedance-1-0-pro-250528",
        "doubao-seedance-1-0-lite-t2v-250428",
        "doubao-seedance-1-0-lite-i2v-250428",
    ])
    _configured = [m.strip() for m in os.getenv("MODELSCOPE_CHAT_MODELS", "").split(",") if m.strip()]
    MODELSCOPE_CHAT_MODELS = list(dict.fromkeys([m for m in [*MODELSCOPE_DEFAULT_CHAT_MODELS, *_configured] if m]))

CHAT_MODELS = model_list("CHAT_MODELS", CHAT_MODEL, ["gpt-4o-mini", "gemini-3.1-flash-image-preview-2k"])
IMAGE_MODELS = model_list("IMAGE_MODELS", IMAGE_MODEL, ["nano-banana-pro"])
VIDEO_MODELS = model_list("VIDEO_MODELS", "veo3-fast", [
    # —— Veo 系列 ——
    "veo2", "veo2-fast", "veo2-pro",
    "veo3", "veo3-fast", "veo3-pro",
    "veo3.1", "veo3.1-fast", "veo3.1-pro",
    # —— Sora ——
    "sora-2", "sora-2-pro",
    # —— 阿里 通义万相 ——
    "wan2.6-t2v", "wan2.6-i2v",
    "wan2.5-t2v-preview", "wan2.5-i2v-preview",
    "wan2.2-t2v-plus", "wan2.2-i2v-plus", "wan2.2-i2v-flash",
    # —— 火山 豆包 Seedance ——
    "doubao-seedance-2-0-260128",
    "doubao-seedance-2-0-fast-260128",
    "doubao-seedance-1-5-pro-251215",
    "doubao-seedance-1-0-pro-250528",
    "doubao-seedance-1-0-lite-t2v-250428",
    "doubao-seedance-1-0-lite-i2v-250428",
])

def provider_key_env(provider_id):
    if provider_id == "comfly":
        return "COMFLY_API_KEY"
    if provider_id == "modelscope":
        return "MODELSCOPE_API_KEY"
    if provider_id == "runninghub":
        return "RUNNINGHUB_API_KEY"
    if provider_id == "volcengine":
        return "ARK_API_KEY"
    return f"API_PROVIDER_{re.sub(r'[^A-Za-z0-9]', '_', provider_id).upper()}_KEY"

def read_api_env_value(key: str) -> str:
    key = str(key or "").strip()
    if not key or not os.path.exists(API_ENV_FILE):
        return ""
    try:
        with open(API_ENV_FILE, "r", encoding="utf-8-sig") as f:
            for raw_line in f.read().splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                env_key, value = line.split("=", 1)
                if env_key.strip() == key:
                    return value.strip().strip('"').strip("'")
    except Exception:
        return ""
    return ""

def provider_env_key_value(provider_id: str) -> str:
    env_key = provider_key_env(str(provider_id or "").strip().lower())
    return os.getenv(env_key, "") or read_api_env_value(env_key)

def mask_secret(value):
    if not value:
        return ""
    tail = value[-4:] if len(value) > 4 else value
    return f"••••••••{tail}"

def default_api_providers():
    # 独立入口平台强制保留，其他平台均可自定义增删
    return [
        {
            "id": "modelscope",
            "name": "ModelScope",
            "base_url": MODELSCOPE_CHAT_BASE_URL,
            "protocol": "openai",
            "enabled": True,
            "primary": False,
            "image_models": MODELSCOPE_DEFAULT_IMAGE_MODELS,
            "chat_models": MODELSCOPE_CHAT_MODELS,
            "video_models": [],
            "ms_loras": MODELSCOPE_DEFAULT_LORAS,
            "ms_defaults_version": MODELSCOPE_DEFAULTS_VERSION,
        },
        {
            "id": "runninghub",
            "name": "RunningHub",
            "base_url": RUNNINGHUB_DEFAULT_BASE_URL,
            "protocol": "runninghub",
            "enabled": True,
            "primary": False,
            "image_models": RUNNINGHUB_DEFAULT_IMAGE_MODELS,
            "chat_models": [],
            "video_models": [],
            "ms_loras": [],
            "ms_defaults_version": 0,
        },
        {
            "id": "volcengine",
            "name": "火山引擎",
            "base_url": VOLCENGINE_DEFAULT_BASE_URL,
            "protocol": "volcengine",
            "enabled": True,
            "primary": False,
            "image_models": [],
            "chat_models": [],
            "video_models": VOLCENGINE_DEFAULT_VIDEO_MODELS,
            "ms_loras": [],
            "ms_defaults_version": 0,
            "volcengine_project_name": VOLCENGINE_DEFAULT_PROJECT_NAME,
            "volcengine_region": VOLCENGINE_DEFAULT_REGION,
        },
    ]

def merge_default_api_providers(providers):
    merged = [dict(item) for item in providers]
    # 只强制保留独立入口平台（不再强制 comfly）
    ms_default = next((d for d in default_api_providers() if d["id"] == "modelscope"), None)
    if ms_default:
        current = next((item for item in merged if item.get("id") == "modelscope"), None)
        if not current:
            merged.append(ms_default)
        else:
            if not current.get("base_url"):
                current["base_url"] = ms_default["base_url"]
            seeded_version = int(current.get("ms_defaults_version") or 0)
            if seeded_version < MODELSCOPE_DEFAULTS_VERSION:
                image_models = model_list_from_values([*MODELSCOPE_DEFAULT_IMAGE_MODELS, *(current.get("image_models") or [])])
                chat_models = model_list_from_values([*MODELSCOPE_DEFAULT_CHAT_MODELS, *(current.get("chat_models") or [])])
                loras = normalize_ms_loras([*MODELSCOPE_DEFAULT_LORAS, *(current.get("ms_loras") or [])])
                current["image_models"] = image_models
                current["chat_models"] = chat_models
                current["ms_loras"] = loras
                current["ms_defaults_version"] = MODELSCOPE_DEFAULTS_VERSION
    rh_default = next((d for d in default_api_providers() if d["id"] == "runninghub"), None)
    if rh_default:
        current = next((item for item in merged if item.get("id") == "runninghub"), None)
        if not current:
            merged.append(rh_default)
        else:
            current["protocol"] = "runninghub"
            if not current.get("base_url"):
                current["base_url"] = rh_default["base_url"]
            current["image_models"] = model_list_from_values([*(current.get("image_models") or []), *rh_default["image_models"]])
    volc_default = next((d for d in default_api_providers() if d["id"] == "volcengine"), None)
    if volc_default:
        current = next((item for item in merged if item.get("id") == "volcengine"), None)
        if not current:
            merged.append(volc_default)
        else:
            current["protocol"] = "volcengine"
            if not current.get("base_url"):
                current["base_url"] = volc_default["base_url"]
            current["video_models"] = model_list_from_values([*(current.get("video_models") or []), *volc_default["video_models"]])
            current["volcengine_project_name"] = str(current.get("volcengine_project_name") or VOLCENGINE_DEFAULT_PROJECT_NAME).strip() or VOLCENGINE_DEFAULT_PROJECT_NAME
            current["volcengine_region"] = str(current.get("volcengine_region") or VOLCENGINE_DEFAULT_REGION).strip() or VOLCENGINE_DEFAULT_REGION
    for current in merged:
        if str(current.get("protocol") or "").lower() == "jimeng" or str(current.get("id") or "").lower() == "jimeng":
            current["protocol"] = "jimeng"
            current["base_url"] = ""
            current["image_models"] = model_list_from_values([*(current.get("image_models") or []), *JIMENG_DEFAULT_IMAGE_MODELS])
            current["video_models"] = model_list_from_values([*(current.get("video_models") or []), *JIMENG_DEFAULT_VIDEO_MODELS])
    return merged

def normalize_model_list(values):
    return model_list_from_values(values)

def model_list_from_values(values):
    deduped = []
    for value in values or []:
        item = str(value or "").strip()
        if item and item not in deduped:
            selected_model(item, item)
            deduped.append(item)
    return deduped

def normalize_ms_loras(values):
    normalized = []
    seen = set()
    for raw in values or []:
        if not isinstance(raw, dict):
            continue
        lora_id = str(raw.get("id") or "").strip()
        if not lora_id:
            continue
        target_model = str(raw.get("target_model") or raw.get("model") or "").strip()
        if not target_model:
            continue
        key = (target_model, lora_id)
        if key in seen:
            continue
        seen.add(key)
        try:
            strength = float(raw.get("strength", raw.get("default_strength", 0.8)))
        except Exception:
            strength = 0.8
        strength = max(0.0, min(2.0, strength))
        name = re.sub(r"\s+", " ", str(raw.get("name") or "").strip())[:80]
        normalized.append({
            "id": lora_id[:180],
            "name": name or lora_id,
            "target_model": target_model[:180],
            "strength": strength,
            "enabled": bool(raw.get("enabled", True)),
            "note": str(raw.get("note") or "").strip()[:300],
        })
    return normalized

def normalize_provider(item):
    provider_id = str(item.get("id") or "").strip().lower()
    if not PROVIDER_ID_RE.fullmatch(provider_id):
        raise HTTPException(status_code=400, detail=f"API 平台 ID 不合法：{provider_id or '(empty)'}")
    name = re.sub(r"\s+", " ", str(item.get("name") or provider_id).strip())[:60] or provider_id
    base_url = str(item.get("base_url") or "").strip().rstrip("/")
    if base_url and not re.match(r"^https?://", base_url):
        raise HTTPException(status_code=400, detail=f"{name} 的 Base URL 需要以 http:// 或 https:// 开头")
    protocol = str(item.get("protocol") or "openai").strip().lower()
    if protocol not in SUPPORTED_PROVIDER_PROTOCOLS:
        protocol = "openai"
    if provider_id == "runninghub":
        protocol = "runninghub"
        base_url = base_url or RUNNINGHUB_DEFAULT_BASE_URL
    if provider_id == "volcengine":
        protocol = "volcengine"
        base_url = base_url or VOLCENGINE_DEFAULT_BASE_URL
    if provider_id == "jimeng":
        protocol = "jimeng"
        base_url = ""
    volc_project = re.sub(r"\s+", " ", str(item.get("volcengine_project_name") or "").strip())[:80]
    volc_region = re.sub(r"\s+", " ", str(item.get("volcengine_region") or "").strip())[:40]
    return {
        "id": provider_id,
        "name": name,
        "base_url": base_url,
        "protocol": protocol,
        "enabled": bool(item.get("enabled", True)),
        "primary": bool(item.get("primary", False)),
        "image_models": model_list_from_values(item.get("image_models") or []),
        "chat_models": model_list_from_values(item.get("chat_models") or []),
        "video_models": model_list_from_values(item.get("video_models") or []),
        "ms_loras": normalize_ms_loras(item.get("ms_loras") or []),
        "ms_defaults_version": int(item.get("ms_defaults_version") or 0),
        "volcengine_project_name": volc_project or (VOLCENGINE_DEFAULT_PROJECT_NAME if provider_id == "volcengine" else ""),
        "volcengine_region": volc_region or (VOLCENGINE_DEFAULT_REGION if provider_id == "volcengine" else ""),
    }

def load_api_providers():
    defaults = default_api_providers()
    if not os.path.exists(API_PROVIDERS_FILE):
        return defaults
    try:
        with open(API_PROVIDERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        providers = [normalize_provider(item) for item in raw if isinstance(item, dict)]
        return merge_default_api_providers(providers or defaults)
    except Exception as e:
        print(f"加载 API 平台配置失败: {e}")
        return defaults

def save_api_providers(providers):
    os.makedirs(DATA_DIR, exist_ok=True)
    with GLOBAL_CONFIG_LOCK:
        with open(API_PROVIDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(providers, f, ensure_ascii=False, indent=2)

# --- 资源库辅助函数 ---

PRESET_CATEGORIES = [
    "住宅", "商业", "办公", "文化建筑", "酒店", "教育", "医疗", "综合体",
    "塔楼", "裙房", "展示区", "大堂/门厅", "样板间", "会所",
    "景观", "广场/节点", "水景",
    "鸟瞰", "人视", "夜景", "施工过程",
]

# AI 标签优先使用的预设词汇库（按类别分组，用于 prompt 和校验）
PRESET_TAG_VOCAB = {
    "建筑风格": ["现代风格", "新中式", "极简主义", "Art Deco", "后现代", "参数化", "工业风", "日式", "东南亚风格", "欧式", "美式", "有机建筑"],
    "建筑形态": ["高层", "超高层", "多层", "低层", "独栋", "联排", "板楼", "点式", "弧形", "L型", "U型", "围合式", "退台", "悬挑", "架空"],
    "外立面材质": ["玻璃幕墙", "石材", "涂料", "金属板", "铝板", "陶板", "真石漆", "GRC", "清水混凝土", "木材", "砖", "百叶", "格栅", "穿孔板"],
    "景观元素": ["绿化带", "草坪", "乔木", "灌木", "花卉", "铺装", "步道", "水景", "喷泉", "雕塑", "座椅", "路灯", "景墙", "廊架", "亭子", "栈道"],
    "室内空间": ["大堂", "电梯厅", "走廊", "楼梯", "卫生间", "厨房", "客厅", "卧室", "书房", "阳台", "飘窗", "中庭", "天井"],
    "环境氛围": ["白天", "黄昏", "傍晚", "清晨", "晴天", "阴天", "雨天", "雪景", "绿意盎然", "秋色", "城市天际线", "山景", "湖景", "江景", "海景"],
    "功能设施": ["停车场", "游泳池", "健身房", "儿童游乐", "商业街", "底商", "办公大堂", "会议室", "屋顶花园", "地下车库", "消防通道", "无障碍设施"],
    "拍摄手法": ["特写", "细节", "全景", "局部", "仰视", "俯视", "对称构图", "透视", "广角", "长焦", "航拍"],
}

def _library_read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _library_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with LIBRARY_LOCK:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

def load_library_sources():
    return _library_read_json(LIBRARY_SOURCES_FILE, [])

def save_library_sources(sources):
    _library_write_json(LIBRARY_SOURCES_FILE, sources)

def normalize_library_image_scope(image: Dict[str, Any]) -> Dict[str, Any]:
    record = dict(image or {})
    project_id = str(record.get("project_id") or "").strip()
    requested_scope = str(record.get("scope") or "").strip().lower()
    scope = "project" if project_id and requested_scope != "shared" else "shared"
    record["scope"] = scope
    record["project_id"] = project_id if scope == "project" else ""
    return record

def load_library_images():
    raw = _library_read_json(LIBRARY_IMAGES_FILE, [])
    if not isinstance(raw, list):
        return []
    images = [normalize_library_image_scope(item) for item in raw if isinstance(item, dict)]
    if images != raw:
        _library_write_json(LIBRARY_IMAGES_FILE, images)
    return images

def save_library_images(images):
    _library_write_json(
        LIBRARY_IMAGES_FILE,
        [normalize_library_image_scope(item) for item in images if isinstance(item, dict)],
    )

def filter_library_images_by_scope(images, scope: str = "all", project_id: str = ""):
    normalized_scope = str(scope or "all").strip().lower()
    if normalized_scope not in {"all", "available", "project", "shared"}:
        normalized_scope = "all"
    current_project_id = str(project_id or "").strip()
    if normalized_scope == "shared":
        return [item for item in images if item.get("scope") == "shared"]
    if normalized_scope == "project":
        return [item for item in images if item.get("scope") == "project" and item.get("project_id") == current_project_id]
    if normalized_scope == "available":
        return [
            item for item in images
            if item.get("scope") == "shared" or (
                item.get("scope") == "project" and item.get("project_id") == current_project_id
            )
        ]
    return list(images)

def load_library_categories():
    data = _library_read_json(LIBRARY_CATEGORIES_FILE, None)
    if data is None:
        return {"presets": PRESET_CATEGORIES[:], "custom": []}
    if "presets" not in data:
        data["presets"] = PRESET_CATEGORIES[:]
    return data

def save_library_categories(cats):
    _library_write_json(LIBRARY_CATEGORIES_FILE, cats)

PROMPT_BUILTIN_CATEGORY_IDS = {"view", "storyboard", "character", "product", "lighting", "custom"}

def sanitize_prompt_text(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback

def sanitize_prompt_name(value: Any, fallback: str = "提示词") -> str:
    return sanitize_prompt_text(value, fallback)[:120]

def prompt_item_category(value: Any) -> str:
    category = str(value or "").strip()
    return category[:80] if category else "custom"

def default_prompt_categories():
    return [
        {"id": "view", "name": "视角"},
        {"id": "storyboard", "name": "分镜"},
        {"id": "character", "name": "角色"},
        {"id": "product", "name": "产品"},
        {"id": "lighting", "name": "光影"},
        {"id": "custom", "name": "我的"},
    ]

def normalize_prompt_library_item(item: Dict[str, Any]):
    item_id = sanitize_prompt_text(item.get("id"), f"prompt_{uuid.uuid4().hex[:12]}")
    created_at = int(item.get("created_at") or item.get("createdAt") or now_ms())
    updated_at = int(item.get("updated_at") or item.get("updatedAt") or created_at)
    return {
        "id": item_id,
        "name": sanitize_prompt_name(item.get("name"), "提示词"),
        "category": prompt_item_category(item.get("category")),
        "positive": sanitize_prompt_text(item.get("positive")),
        "negative": sanitize_prompt_text(item.get("negative")),
        "scene": sanitize_prompt_text(item.get("scene")),
        "created_at": created_at,
        "updated_at": updated_at,
    }

def seed_prompt_library():
    now = now_ms()
    return {
        "id": "system",
        "name": "常用提示词",
        "type": "prompt",
        "readonly": False,
        "categories": default_prompt_categories(),
        "items": [
            {
                "id": "prompt_view_closeup",
                "name": "产品近景细节",
                "category": "view",
                "positive": "close-up product detail, clean composition, premium material texture, soft studio lighting",
                "negative": "blurry, low quality, cluttered background, distorted shape",
                "scene": "适合产品局部、材质、结构细节图。",
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": "prompt_storyboard_opening",
                "name": "开场分镜",
                "category": "storyboard",
                "positive": "wide establishing shot, clear subject placement, cinematic framing, natural depth",
                "negative": "messy layout, duplicated subject, unreadable composition",
                "scene": "适合视频或图组的第一张氛围建立图。",
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": "prompt_character_reference",
                "name": "角色设定参考",
                "category": "character",
                "positive": "character design sheet, consistent outfit, front view, clean background, detailed facial features",
                "negative": "extra limbs, inconsistent face, text, watermark",
                "scene": "适合沉淀角色外观与服装设定。",
                "created_at": now,
                "updated_at": now,
            },
        ],
    }

def default_prompt_libraries():
    return [seed_prompt_library()]

def normalize_prompt_categories(categories: Any):
    defaults = default_prompt_categories()
    seen = set()
    result = []
    for cat in categories if isinstance(categories, list) else []:
        name = sanitize_prompt_text(cat.get("name") if isinstance(cat, dict) else cat, "")
        raw_id = sanitize_prompt_text(cat.get("id") if isinstance(cat, dict) else "", "")
        cat_id = raw_id or f"pcat_{uuid.uuid4().hex[:8]}"
        if not name:
            name = cat_id
        if cat_id in seen:
            continue
        result.append({"id": cat_id[:80], "name": name[:80]})
        seen.add(cat_id)
    for cat in defaults:
        if cat["id"] not in seen:
            result.append(cat)
            seen.add(cat["id"])
    return result

def normalize_prompt_libraries(data: Any):
    raw_libraries = data.get("libraries") if isinstance(data, dict) else data
    libraries = []
    for lib in raw_libraries if isinstance(raw_libraries, list) else []:
        if not isinstance(lib, dict):
            continue
        lib_id = sanitize_prompt_text(lib.get("id"), f"plib_{uuid.uuid4().hex[:10]}")
        categories = normalize_prompt_categories(lib.get("categories"))
        category_ids = {c["id"] for c in categories}
        items = [normalize_prompt_library_item(item) for item in (lib.get("items") or []) if isinstance(item, dict)]
        for item in items:
            if item["category"] not in category_ids:
                categories.append({"id": item["category"], "name": item["category"]})
                category_ids.add(item["category"])
        libraries.append({
            "id": lib_id,
            "name": sanitize_prompt_name(lib.get("name"), "提示词库"),
            "type": "prompt",
            "readonly": bool(lib.get("readonly", False)),
            "categories": categories,
            "items": items,
            "created_at": int(lib.get("created_at") or lib.get("createdAt") or now_ms()),
            "updated_at": int(lib.get("updated_at") or lib.get("updatedAt") or now_ms()),
        })
    if not libraries:
        libraries = default_prompt_libraries()
    if not any(lib["id"] == "system" for lib in libraries):
        libraries.insert(0, seed_prompt_library())
    return {"libraries": libraries}

def load_prompt_libraries():
    data = _library_read_json(PROMPT_LIBRARY_PATH, None)
    return normalize_prompt_libraries(data)

def save_prompt_libraries(data):
    normalized = normalize_prompt_libraries(data)
    _library_write_json(PROMPT_LIBRARY_PATH, normalized)
    return normalized

# --- LLM Wiki 辅助函数 ---

def ensure_wiki_dirs():
    os.makedirs(WIKI_DIR, exist_ok=True)
    for rel in WIKI_SUBDIRS.values():
        os.makedirs(os.path.join(WIKI_DIR, rel), exist_ok=True)

def _wiki_read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _wiki_write_json(path, data):
    ensure_wiki_dirs()
    with WIKI_LOCK:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

def wiki_slug(value: str, fallback: str = "wiki") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-_")
    return (text[:60] or fallback)

def wiki_now_iso():
    return time.strftime("%Y-%m-%d", time.localtime())

def wiki_plain_text(value: str) -> str:
    text = re.sub(r"```.*?```", " ", str(value or ""), flags=re.S)
    text = re.sub(r"^\s*---.*?---", " ", text, flags=re.S)
    text = re.sub(r"\[[^\]]+\]\([^)]+\)", " ", text)
    text = re.sub(r"[\[\]#>*_`|]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def wiki_excerpt(value: str, limit: int = 260) -> str:
    text = wiki_plain_text(value)
    return text[:limit] + ("..." if len(text) > limit else "")

def wiki_path_for(page_type: str, page_id: str) -> str:
    rel = WIKI_SUBDIRS.get(page_type, "summaries")
    return os.path.join(WIKI_DIR, rel, f"{page_id}.md")

def wiki_rel_path(path: str) -> str:
    try:
        return os.path.relpath(path, WIKI_DIR).replace("\\", "/")
    except Exception:
        return path

def write_wiki_markdown(page_type: str, page_id: str, content: str) -> str:
    ensure_wiki_dirs()
    path = wiki_path_for(page_type, page_id)
    with WIKI_LOCK:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content.rstrip() + "\n")
    return wiki_rel_path(path)

def read_wiki_markdown(path: str) -> str:
    full = os.path.join(WIKI_DIR, path) if not os.path.isabs(path) else path
    try:
        root = os.path.abspath(WIKI_DIR)
        full_abs = os.path.abspath(full)
        if os.path.commonpath([root, full_abs]) != root:
            return ""
        with open(full_abs, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""

def load_wiki_sources():
    return _wiki_read_json(WIKI_SOURCES_FILE, [])

def save_wiki_sources(sources):
    _wiki_write_json(WIKI_SOURCES_FILE, sources)

def load_wiki_pages():
    return _wiki_read_json(WIKI_PAGES_FILE, [])

def save_wiki_pages(pages):
    _wiki_write_json(WIKI_PAGES_FILE, pages)

def load_wiki_relations():
    return _wiki_read_json(WIKI_RELATIONS_FILE, [])

def save_wiki_relations(relations):
    _wiki_write_json(WIKI_RELATIONS_FILE, relations)

def upsert_wiki_page(page: Dict[str, Any]):
    pages = load_wiki_pages()
    page["updated_at"] = now_ms()
    existing = next((item for item in pages if item.get("id") == page.get("id")), None)
    if existing:
        existing.update(page)
    else:
        page.setdefault("created_at", now_ms())
        pages.insert(0, page)
    save_wiki_pages(pages)
    return page

def upsert_wiki_relation(source_id: str, relation_type: str, target_id: str, evidence: str = ""):
    relations = load_wiki_relations()
    key = (source_id, relation_type, target_id)
    existing = next((item for item in relations if (item.get("source"), item.get("type"), item.get("target")) == key), None)
    payload = {
        "source": source_id,
        "type": relation_type,
        "target": target_id,
        "evidence": evidence[:240],
        "updated_at": now_ms(),
    }
    if existing:
        existing.update(payload)
    else:
        payload["id"] = f"rel_{uuid.uuid4().hex[:12]}"
        payload["created_at"] = now_ms()
        relations.append(payload)
    save_wiki_relations(relations)
    return payload

def source_to_node(source: Dict[str, Any]):
    return {
        "id": source.get("id", ""),
        "label": source.get("title") or source.get("name") or "来源",
        "type": "source",
        "summary": source.get("excerpt", ""),
    }

def page_to_node(page: Dict[str, Any]):
    return {
        "id": page.get("id", ""),
        "label": page.get("title") or page.get("id", ""),
        "type": page.get("type") or "page",
        "summary": page.get("excerpt", ""),
    }

def rebuild_wiki_graph():
    sources = load_wiki_sources()
    pages = load_wiki_pages()
    relations = load_wiki_relations()
    nodes = []
    seen = set()
    for source in sources:
        if source.get("id") and source.get("id") not in seen:
            nodes.append(source_to_node(source))
            seen.add(source.get("id"))
    for page in pages:
        if page.get("id") and page.get("id") not in seen:
            nodes.append(page_to_node(page))
            seen.add(page.get("id"))
    graph = {
        "nodes": nodes,
        "edges": [
            {
                "id": item.get("id") or f"{item.get('source')}-{item.get('type')}-{item.get('target')}",
                "source": item.get("source", ""),
                "target": item.get("target", ""),
                "type": item.get("type", "related"),
                "label": item.get("type", "related"),
                "evidence": item.get("evidence", ""),
            }
            for item in relations
            if item.get("source") and item.get("target")
        ],
        "updated_at": now_ms(),
    }
    _wiki_write_json(WIKI_GRAPH_FILE, graph)
    return graph

def wiki_index_markdown():
    sources = load_wiki_sources()
    pages = load_wiki_pages()
    concepts = [p for p in pages if p.get("type") == "concept"]
    summaries = [p for p in pages if p.get("type") == "summary"]
    outputs = [p for p in pages if p.get("type") in {"qa", "report", "design"}]
    lines = [
        "# LLM Wiki 索引",
        "",
        "## 来源",
        *[f"- [[{item.get('id')}]] {item.get('title') or '未命名来源'}" for item in sources[:50]],
        "",
        "## 摘要",
        *[f"- [[{item.get('id')}]] {item.get('title') or item.get('id')}" for item in summaries[:50]],
        "",
        "## 概念",
        *[f"- [[{item.get('id')}]] {item.get('title') or item.get('id')}" for item in concepts[:80]],
        "",
        "## 输出",
        *[f"- [[{item.get('id')}]] {item.get('title') or item.get('id')}" for item in outputs[:50]],
    ]
    return "\n".join(lines)

def wiki_overview_payload():
    sources = load_wiki_sources()
    pages = load_wiki_pages()
    graph = _wiki_read_json(WIKI_GRAPH_FILE, None) or rebuild_wiki_graph()
    by_type = {}
    for page in pages:
        by_type[page.get("type") or "page"] = by_type.get(page.get("type") or "page", 0) + 1
    return {
        "counts": {
            "sources": len(sources),
            "pages": len(pages),
            "relations": len(load_wiki_relations()),
            "graph_nodes": len(graph.get("nodes") or []),
            "graph_edges": len(graph.get("edges") or []),
            **by_type,
        },
        "recent_sources": sorted(sources, key=lambda item: item.get("updated_at", 0), reverse=True)[:8],
        "sources": sorted(sources, key=lambda item: item.get("updated_at", 0), reverse=True),
        "recent_pages": sorted(pages, key=lambda item: item.get("updated_at", 0), reverse=True)[:12],
        "index_markdown": wiki_index_markdown(),
        "updated_at": graph.get("updated_at", 0),
    }

def wiki_search_terms(query: str) -> List[str]:
    query = str(query or "").strip().lower()
    raw_terms = re.findall(r"[a-z0-9_\-]{2,}|[\u4e00-\u9fff]{2,}", query)
    stop_terms = {"应该", "怎么", "什么", "一下", "当前", "请问", "可以", "需要", "如何"}
    terms = []
    for term in raw_terms:
        if term in stop_terms:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]{4,}", term):
            terms.extend([term[i:i + 2] for i in range(0, len(term) - 1)])
        terms.append(term)
    return list(dict.fromkeys([term for term in terms if len(term) >= 2 and term not in stop_terms]))

def wiki_search_score(haystack: str, query: str, terms: List[str]) -> int:
    if not query and not terms:
        return 1
    score = haystack.count(query) * max(len(query), 1) if query else 0
    for term in terms:
        count = haystack.count(term)
        if count:
            score += count * max(len(term), 2)
    return score

def wiki_search_items(query: str, page_type: str = "", limit: int = 20):
    q = str(query or "").strip().lower()
    terms = wiki_search_terms(q)
    page_type = str(page_type or "").strip()
    candidates = []
    for source in load_wiki_sources():
        content = read_wiki_markdown(source.get("path", ""))
        haystack = " ".join([
            source.get("title", ""),
            source.get("source_type", ""),
            " ".join(source.get("tags") or []),
            source.get("excerpt", ""),
            content,
        ]).lower()
        score = wiki_search_score(haystack, q, terms)
        if score:
            candidates.append({"kind": "source", "score": score, "item": source})
    for page in load_wiki_pages():
        if page_type and page.get("type") != page_type:
            continue
        content = read_wiki_markdown(page.get("path", ""))
        haystack = " ".join([
            page.get("title", ""),
            page.get("type", ""),
            " ".join(page.get("tags") or []),
            page.get("excerpt", ""),
            content,
        ]).lower()
        score = wiki_search_score(haystack, q, terms)
        if score:
            candidates.append({"kind": "page", "score": score, "item": page})
    candidates.sort(key=lambda item: (item["score"], item["item"].get("updated_at", 0)), reverse=True)
    return candidates[:limit]

def extract_concept_titles(title: str, content: str, tags: List[str]):
    candidates = []
    candidates.extend([str(tag).strip() for tag in tags or []])
    body = re.sub(r"^\s*---.*?---", "", str(content or ""), flags=re.S)
    meta_keys = {"title", "type", "created", "updated", "source", "tags", "url"}
    for line in body.splitlines():
        clean = line.strip().strip("#").strip()
        if clean.lower().split(":", 1)[0].strip() in meta_keys:
            continue
        if 2 <= len(clean) <= 40 and (line.lstrip().startswith("#") or "：" in clean or ":" in clean):
            candidates.append(clean.split("：", 1)[0].split(":", 1)[0].strip())
    words = re.findall(r"[\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9_\- ]{1,18}", title or "")
    candidates.extend([w.strip() for w in words])
    stop = {"未命名", "source", "summary", "markdown", "the", "and", "for"}
    result = []
    for item in candidates:
        item = re.sub(r"\s+", " ", item).strip(" -_/，。,.")
        if len(item) < 2 or item.lower() in stop:
            continue
        if item not in result:
            result.append(item)
    return result[:8]

def create_wiki_source_record(title: str, content: str, source_type: str = "note", url: str = "", tags: Optional[List[str]] = None):
    ensure_wiki_dirs()
    now = now_ms()
    title = re.sub(r"\s+", " ", str(title or "").strip())[:120] or "未命名来源"
    content = str(content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="来源内容不能为空")
    source_type = re.sub(r"[^a-zA-Z0-9_-]", "", str(source_type or "note").strip().lower()) or "note"
    tags = [str(tag).strip()[:40] for tag in (tags or []) if str(tag).strip()][:12]
    source_id = f"src_{wiki_slug(title, 'source')}_{uuid.uuid4().hex[:6]}"
    body = "\n".join([
        "---",
        f'title: "{title}"',
        f"type: {source_type}",
        f"created: {wiki_now_iso()}",
        "---",
        "",
        f"# {title}",
        "",
        content,
    ])
    path = write_wiki_markdown("source", source_id, body)
    source = {
        "id": source_id,
        "title": title,
        "source_type": source_type,
        "url": str(url or "").strip()[:500],
        "tags": tags,
        "path": path,
        "excerpt": wiki_excerpt(content),
        "compiled": False,
        "created_at": now,
        "updated_at": now,
    }
    sources = load_wiki_sources()
    sources.insert(0, source)
    save_wiki_sources(sources)
    rebuild_wiki_graph()
    return source

def compile_wiki_source(source_id: str):
    sources = load_wiki_sources()
    source = next((item for item in sources if item.get("id") == source_id), None)
    if not source:
        raise HTTPException(status_code=404, detail="Wiki 来源不存在")
    content = read_wiki_markdown(source.get("path", ""))
    plain = wiki_plain_text(content)
    if not plain:
        raise HTTPException(status_code=400, detail="来源内容为空，无法编译")
    title = source.get("title") or "未命名来源"
    now = now_ms()
    summary_id = f"sum_{wiki_slug(title, 'summary')}_{source_id[-6:]}"
    key_points = []
    for sentence in re.split(r"[。！？.!?\n]+", plain):
        sentence = sentence.strip()
        if 12 <= len(sentence) <= 120 and sentence not in key_points:
            key_points.append(sentence)
        if len(key_points) >= 5:
            break
    if not key_points:
        key_points = [plain[:120]]
    summary_md = "\n".join([
        "---",
        f'title: "{title} 摘要"',
        "type: summary",
        f"source: {source_id}",
        f"updated: {wiki_now_iso()}",
        "---",
        "",
        f"# {title} 摘要",
        "",
        "## TL;DR",
        wiki_excerpt(plain, 420),
        "",
        "## 要点",
        *[f"- {point}" for point in key_points],
        "",
        "## 来源",
        f"- [[{source_id}]] {title}",
    ])
    summary_path = write_wiki_markdown("summary", summary_id, summary_md)
    summary_page = upsert_wiki_page({
        "id": summary_id,
        "type": "summary",
        "title": f"{title} 摘要",
        "source_id": source_id,
        "path": summary_path,
        "tags": source.get("tags") or [],
        "excerpt": wiki_excerpt(plain, 220),
        "created_at": now,
    })
    upsert_wiki_relation(source_id, "compiled_to", summary_id, title)

    concept_pages = []
    for concept_title in extract_concept_titles(title, content, source.get("tags") or []):
        concept_id = f"concept_{wiki_slug(concept_title, 'concept')}"
        concept_md = "\n".join([
            "---",
            f'title: "{concept_title}"',
            "type: concept",
            f"updated: {wiki_now_iso()}",
            "---",
            "",
            f"# {concept_title}",
            "",
            "## 定义",
            f"{concept_title} 是从《{title}》中提炼出的可复用知识点。",
            "",
            "## 当前理解",
            wiki_excerpt(plain, 360),
            "",
            "## 证据来源",
            f"- [[{source_id}]] {title}",
            f"- [[{summary_id}]] {title} 摘要",
        ])
        concept_path = write_wiki_markdown("concept", concept_id, concept_md)
        concept_page = upsert_wiki_page({
            "id": concept_id,
            "type": "concept",
            "title": concept_title,
            "source_id": source_id,
            "path": concept_path,
            "tags": list(dict.fromkeys([*(source.get("tags") or []), "concept"])),
            "excerpt": wiki_excerpt(plain, 180),
            "created_at": now,
        })
        concept_pages.append(concept_page)
        upsert_wiki_relation(summary_id, "extracts", concept_id, concept_title)
        upsert_wiki_relation(source_id, "evidence_for", concept_id, title)

    source["compiled"] = True
    source["summary_id"] = summary_id
    source["concept_ids"] = [item.get("id") for item in concept_pages]
    source["updated_at"] = now_ms()
    save_wiki_sources(sources)
    graph = rebuild_wiki_graph()
    return {
        "source": source,
        "summary": summary_page,
        "concepts": concept_pages,
        "graph": {"nodes": len(graph.get("nodes") or []), "edges": len(graph.get("edges") or [])},
    }

def create_wiki_output_page(output_type: str, title: str, content: str, related_ids: Optional[List[str]] = None):
    output_type = output_type if output_type in {"qa", "report", "design", "health"} else "report"
    title = re.sub(r"\s+", " ", str(title or "").strip())[:120] or "Wiki 输出"
    page_id = f"{output_type}_{wiki_slug(title, output_type)}_{uuid.uuid4().hex[:6]}"
    markdown = "\n".join([
        "---",
        f'title: "{title}"',
        f"type: {output_type}",
        f"updated: {wiki_now_iso()}",
        "---",
        "",
        f"# {title}",
        "",
        content.strip(),
    ])
    path = write_wiki_markdown(output_type, page_id, markdown)
    page = upsert_wiki_page({
        "id": page_id,
        "type": output_type,
        "title": title,
        "path": path,
        "tags": [output_type],
        "excerpt": wiki_excerpt(content),
        "created_at": now_ms(),
    })
    for related_id in related_ids or []:
        upsert_wiki_relation(page_id, "references", str(related_id), title)
    rebuild_wiki_graph()
    return page

def wiki_answer_from_context(question: str, matches: List[Dict[str, Any]]):
    snippets = []
    related_ids = []
    for match in matches[:6]:
        item = match.get("item") or {}
        content = read_wiki_markdown(item.get("path", ""))
        snippets.append({
            "id": item.get("id", ""),
            "title": item.get("title", ""),
            "type": item.get("type") or match.get("kind"),
            "excerpt": wiki_excerpt(content or item.get("excerpt", ""), 220),
        })
        if item.get("id"):
            related_ids.append(item.get("id"))
    if not snippets:
        answer = "当前知识库还没有命中资料。建议先导入来源并执行编译，再进行问答。"
    else:
        bullet_lines = [f"- {item['title']}：{item['excerpt']}" for item in snippets]
        answer = "\n".join([
            f"针对“{question}”，当前 Wiki 命中了 {len(snippets)} 条可用上下文。",
            "",
            "## 依据",
            *bullet_lines,
            "",
            "## 初步结论",
            "这些资料可以作为当前回答的本地依据；如果需要更高精度，下一步应继续补充原始来源或让 Agent 生成专项报告。",
        ])
    return answer, snippets, related_ids

def local_wiki_safe_name(value: str) -> str:
    name = re.sub(r"\s+", " ", str(value or "").strip())
    name = re.sub(r"[/:\\\0]+", "-", name)
    name = name.strip(" .-_")
    return (name or "AgentWiki")[:80]

def local_wiki_write_text(root: str, rel_path: str, content: str, created_files: List[str], overwrite: bool = False):
    path = os.path.join(root, rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path) and not overwrite:
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(content.rstrip() + "\n")
    created_files.append(rel_path.replace("\\", "/"))
    return True

def local_wiki_write_json(root: str, rel_path: str, payload: Any, created_files: List[str], overwrite: bool = False):
    path = os.path.join(root, rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path) and not overwrite:
        return False
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    created_files.append(rel_path.replace("\\", "/"))
    return True

def local_wiki_agents_md(name: str, local_only: bool) -> str:
    mode = "仅本地整理，不调用外部模型" if local_only else "允许按用户配置调用模型"
    return f"""# {name} - AI 读取规则

这是一个本地优先的 AI-first 知识库。AI 使用本文件恢复规则，避免迷路、误写或污染原始资料。

## 工作模式

- 默认模式：{mode}
- 原始资料只增不改，统一放在 `00_收件箱/`。
- 编译结果写入 `40_知识库/`，问答和报告写入 `50_资源/`。
- `.agentwiki/` 是机器索引目录，用于保存 JSON 元数据和图谱。

## 读取顺序

1. 先读 `README.md`。
2. 再读 `99_System/Memory.md`。
3. 再读 `40_知识库/索引/All-Sources.md` 和 `40_知识库/索引/All-Concepts.md`。
4. 需要项目经验时读 `20_项目/` 与 `40_知识库/索引/All-Projects.md`。

## 写入规则

1. 不直接修改 `00_收件箱/` 中的原始来源，除非用户明确要求。
2. 新上传或导入的文件先复制到 `00_收件箱/上传文件/` 或对应分类。
3. 每个来源编译后，在 `40_知识库/摘要/` 生成摘要。
4. 可复用知识点写入 `40_知识库/概念/`。
5. 对话问答写入 `50_资源/问答档案/`。
6. 研究、总结、方案报告写入 `50_资源/报告/`。
7. 健康检查写入 `50_资源/健康检查/`。
8. 每次新增摘要、概念、项目或输出后，同步更新 `.agentwiki/*.json` 和 `40_知识库/索引/*.md`。

## 回答规则

- 回答前先检索本地知识库。
- 引用来源时优先使用 Wiki 链接或 `.agentwiki/sources.json` 中的 source id。
- 不确定时标注 `confidence`，不要假装知道。
- 如果知识库没有命中，应说明缺口，并建议把资料放入 `00_收件箱/` 后再编译。
"""

def local_wiki_readme_md(name: str, local_only: bool) -> str:
    mode = "仅本地整理，不调用外部模型" if local_only else "可按用户设置调用模型"
    return f"""# {name}

这是 Infinite Agent Work 创建的本地 AI-first 知识库。

## 当前模式

{mode}

## 使用方法

1. 把原始资料放入 `00_收件箱/`，或在应用中上传文件，系统会复制到收件箱。
2. 运行编译，把来源整理为摘要、概念、索引和图谱。
3. 使用 Agent 进行问答、研究、总结和设计分析。
4. 产物会保存在本地文件夹中，不依赖云端数据库。

## 目录结构

```text
00_收件箱/          原始资料，只增不改
10_日记/           日志
20_项目/           主动项目和项目复盘
30_研究/           深度研究笔记
40_知识库/         LLM 编译后的摘要、概念、索引
50_资源/           问答档案、报告、健康检查
90_计划/           执行计划
99_System/         Memory、模板、提示词
.agentwiki/        机器索引、图谱和配置
```
"""

def local_wiki_memory_md(name: str, local_only: bool) -> str:
    mode = "local_only" if local_only else "model_enabled"
    return f"""# Memory - {name}

> 最后更新：{wiki_now_iso()}
> 这是 AI 恢复上下文时优先读取的记忆文件。

## 知识库状态

- 名称：{name}
- 模式：{mode}
- 初始化日期：{wiki_now_iso()}

## 结构原则

1. `00_收件箱/` 保存原始资料，只增不改。
2. `40_知识库/` 保存编译后的结构化知识。
3. `50_资源/` 保存问答、报告和健康检查。
4. `.agentwiki/` 保存机器可读索引。

## 待办

- [ ] 导入第一批原始资料到 `00_收件箱/`
- [ ] 运行首次编译
- [ ] 生成首份健康检查
"""

def local_wiki_index_md(title: str, description: str) -> str:
    return f"""---
title: "{title}"
updated: {wiki_now_iso()}
---

# {title}

> {description}

暂无内容。导入来源并编译后自动更新。
"""

def initialize_local_wiki(base_dir: str, name: str, local_only: bool = True, language: str = "zh-CN", allow_existing_initialized: bool = True):
    base_dir = os.path.abspath(os.path.expanduser(str(base_dir or "").strip()))
    if not os.path.isdir(base_dir):
        raise HTTPException(status_code=400, detail="保存位置不存在，请先选择一个有效的本地文件夹。")
    safe_name = local_wiki_safe_name(name)
    root = os.path.abspath(os.path.join(base_dir, safe_name))
    if os.path.commonpath([base_dir, root]) != base_dir:
        raise HTTPException(status_code=400, detail="知识库名称不安全。")

    marker = os.path.join(root, ".agentwiki", "config.json")
    if os.path.exists(root) and os.listdir(root) and not os.path.exists(marker):
        raise HTTPException(status_code=409, detail="目标文件夹已存在且不是已初始化的 AgentWiki。请换一个名称，或选择空文件夹。")
    if os.path.exists(marker) and not allow_existing_initialized:
        raise HTTPException(status_code=409, detail="这个知识库已经初始化。")

    created_dirs = []
    created_files = []
    os.makedirs(root, exist_ok=True)
    for rel in LOCAL_WIKI_DIRS:
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)
            created_dirs.append(rel.replace("\\", "/"))

    local_wiki_write_text(root, "AGENTS.md", local_wiki_agents_md(safe_name, local_only), created_files)
    local_wiki_write_text(root, "README.md", local_wiki_readme_md(safe_name, local_only), created_files)
    local_wiki_write_text(root, os.path.join("99_System", "Memory.md"), local_wiki_memory_md(safe_name, local_only), created_files)
    local_wiki_write_text(root, os.path.join("99_System", "Templates", "Wiki_Template.md"), """---
title: ""
type: concept
created: {{date}}
tags: []
confidence: draft
sources: []
---

# 概念名称

## 定义

## 要点

## 证据来源

## 相关概念
""", created_files)
    local_wiki_write_text(root, os.path.join("40_知识库", "索引", "All-Sources.md"), local_wiki_index_md("所有来源", "自动维护。记录所有原始资料和对应摘要。"), created_files)
    local_wiki_write_text(root, os.path.join("40_知识库", "索引", "All-Concepts.md"), local_wiki_index_md("所有概念", "自动维护。记录所有可复用知识点。"), created_files)
    local_wiki_write_text(root, os.path.join("40_知识库", "索引", "All-Projects.md"), local_wiki_index_md("所有项目", "自动维护。记录项目档案和复盘。"), created_files)
    local_wiki_write_text(root, os.path.join("40_知识库", "索引", "Architecture-Knowledge.md"), local_wiki_index_md("知识结构索引", "自动维护。按主题组织摘要、概念、项目和输出。"), created_files)

    now = now_ms()
    config = {
        "schema_version": LOCAL_WIKI_SCHEMA_VERSION,
        "name": safe_name,
        "root": root,
        "language": language or "zh-CN",
        "local_only": bool(local_only),
        "created_at": now,
        "updated_at": now,
        "inbox_dir": "00_收件箱",
        "machine_dir": ".agentwiki",
    }
    manifest = {
        "schema_version": LOCAL_WIKI_SCHEMA_VERSION,
        "name": safe_name,
        "root": root,
        "generated_at": now,
        "files": [],
        "directories": LOCAL_WIKI_DIRS,
    }
    local_wiki_write_json(root, os.path.join(".agentwiki", "config.json"), config, created_files, overwrite=True)
    local_wiki_write_json(root, os.path.join(".agentwiki", "manifest.json"), manifest, created_files, overwrite=True)
    local_wiki_write_json(root, os.path.join(".agentwiki", "sources.json"), [], created_files)
    local_wiki_write_json(root, os.path.join(".agentwiki", "pages.json"), [], created_files)
    local_wiki_write_json(root, os.path.join(".agentwiki", "relations.json"), [], created_files)
    local_wiki_write_json(root, os.path.join(".agentwiki", "graph.json"), {"nodes": [], "edges": [], "updated_at": now}, created_files)
    return {
        "name": safe_name,
        "root": root,
        "created_dirs": created_dirs,
        "created_files": created_files,
        "config": config,
        "next_steps": [
            "把原始资料放入 00_收件箱/，或在应用中上传文件。",
            "运行本地整理编译，生成摘要、概念和索引。",
            "用 Agent 基于本地知识库进行问答、总结和报告。",
        ],
    }

def local_wiki_validate_root(root: str) -> str:
    root = os.path.abspath(os.path.expanduser(str(root or "").strip()))
    marker = os.path.join(root, ".agentwiki", "config.json")
    if not os.path.isdir(root) or not os.path.exists(marker):
        raise HTTPException(status_code=400, detail="请先选择一个已初始化的本地知识库根目录。")
    return root

def local_wiki_category(value: str) -> str:
    category = str(value or "自动分类").strip()
    allowed = {"自动分类", "文章", "录音笔记", "视频", "论文", "项目资料", "上传文件"}
    return category if category in allowed else "上传文件"

def local_wiki_auto_category(filename: str) -> str:
    name = os.path.basename(str(filename or "")).lower()
    ext = os.path.splitext(name)[1].lstrip(".")
    if ext in {"mp3", "wav", "m4a", "aac", "flac", "ogg", "opus", "amr"}:
        return "录音笔记"
    if ext in {"mp4", "mov", "mkv", "avi", "webm", "m4v"}:
        return "视频"
    if ext in {"pdf"} or any(token in name for token in ["论文", "paper", "thesis", "journal"]):
        return "论文"
    if ext in {"doc", "docx", "ppt", "pptx", "xls", "xlsx", "key", "pages", "numbers"}:
        return "项目资料"
    if ext in {"md", "markdown", "txt", "html", "htm", "csv", "json"}:
        return "文章"
    return "上传文件"

def local_wiki_resolve_category(category: str, filename: str) -> str:
    selected = local_wiki_category(category)
    if selected == "自动分类" or selected == "上传文件":
        return local_wiki_auto_category(filename)
    return selected

def local_wiki_safe_filename(value: str) -> str:
    name = os.path.basename(str(value or "").strip())
    name = re.sub(r"[/:\\\0]+", "-", name).strip(" .-_")
    return name[:180] or f"source-{uuid.uuid4().hex[:8]}"

def local_wiki_unique_path(dest_dir: str, filename: str) -> str:
    base, ext = os.path.splitext(local_wiki_safe_filename(filename))
    candidate = os.path.join(dest_dir, f"{base}{ext}")
    index = 2
    while os.path.exists(candidate):
        candidate = os.path.join(dest_dir, f"{base}-{index}{ext}")
        index += 1
    return candidate

def local_wiki_file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def local_wiki_read_machine_json(root: str, name: str, default):
    path = os.path.join(root, ".agentwiki", name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def local_wiki_write_machine_json(root: str, name: str, payload: Any):
    path = os.path.join(root, ".agentwiki", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def local_wiki_record_import(root: str, dest_path: str, original_path: str = "", source_kind: str = "file"):
    now = now_ms()
    rel_path = os.path.relpath(dest_path, root).replace("\\", "/")
    size = os.path.getsize(dest_path)
    digest = local_wiki_file_sha256(dest_path)
    title = os.path.basename(dest_path)
    parts = rel_path.split("/")
    category = parts[1] if len(parts) > 2 and parts[0] == "00_收件箱" else ""
    source_id = f"src_{wiki_slug(os.path.splitext(title)[0], 'file')}_{digest[:8]}"
    record = {
        "id": source_id,
        "title": title,
        "source_type": os.path.splitext(title)[1].lstrip(".").lower() or source_kind,
        "category": category,
        "kind": source_kind,
        "path": rel_path,
        "original_path": original_path,
        "size": size,
        "sha256": digest,
        "compiled": False,
        "created_at": now,
        "updated_at": now,
    }

    sources = local_wiki_read_machine_json(root, "sources.json", [])
    existing = next((item for item in sources if item.get("sha256") == digest and item.get("path") == rel_path), None)
    if existing:
        existing.update(record)
    else:
        sources.insert(0, record)
    local_wiki_write_machine_json(root, "sources.json", sources)

    manifest = local_wiki_read_machine_json(root, "manifest.json", {})
    files = [item for item in manifest.get("files", []) if item.get("path") != rel_path]
    files.insert(0, {
        "path": rel_path,
        "source_id": source_id,
        "size": size,
        "sha256": digest,
        "original_path": original_path,
        "imported_at": now,
    })
    manifest["files"] = files
    manifest["generated_at"] = now
    local_wiki_write_machine_json(root, "manifest.json", manifest)

    config = local_wiki_read_machine_json(root, "config.json", {})
    config["updated_at"] = now
    local_wiki_write_machine_json(root, "config.json", config)
    return record

def local_wiki_copy_source_path(root: str, source_path: str, category: str = "上传文件", max_files: int = 200):
    root = local_wiki_validate_root(root)
    source_path = os.path.abspath(os.path.expanduser(str(source_path or "").strip()))
    if not os.path.exists(source_path):
        raise HTTPException(status_code=400, detail="来源路径不存在。")
    max_files = max(1, min(int(max_files or 200), 1000))
    imported = []

    if os.path.isfile(source_path):
        dest_dir = os.path.join(root, "00_收件箱", local_wiki_resolve_category(category, os.path.basename(source_path)))
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = local_wiki_unique_path(dest_dir, os.path.basename(source_path))
        shutil.copy2(source_path, dest_path)
        imported.append(local_wiki_record_import(root, dest_path, source_path, "file"))
        return imported

    copied = 0
    for current_root, dirs, files in os.walk(source_path):
        dirs[:] = [d for d in dirs if d not in {".git", ".obsidian", "node_modules", "__pycache__"}]
        rel_dir = os.path.relpath(current_root, source_path)
        for filename in files:
            if filename == ".DS_Store":
                continue
            if copied >= max_files:
                return imported
            src_file = os.path.join(current_root, filename)
            if not os.path.isfile(src_file):
                continue
            file_category = local_wiki_resolve_category(category, filename)
            category_dir = os.path.join(root, "00_收件箱", file_category)
            target_dir = category_dir if rel_dir == "." else os.path.join(category_dir, rel_dir)
            os.makedirs(target_dir, exist_ok=True)
            dest_path = local_wiki_unique_path(target_dir, filename)
            shutil.copy2(src_file, dest_path)
            imported.append(local_wiki_record_import(root, dest_path, src_file, "file"))
            copied += 1
    return imported

def local_wiki_abs_path(root: str, rel_path: str) -> str:
    root = local_wiki_validate_root(root)
    rel_path = str(rel_path or "").strip().lstrip("/\\")
    full = os.path.abspath(os.path.join(root, rel_path))
    if os.path.commonpath([root, full]) != root:
        raise HTTPException(status_code=400, detail="本地知识库路径不安全。")
    return full

def xml_text_from_bytes(data: bytes) -> str:
    try:
        root = ET.fromstring(data)
    except Exception:
        return ""
    parts = []
    for node in root.iter():
        if node.text and node.text.strip():
            parts.append(node.text.strip())
    return "\n".join(parts)

def local_wiki_extract_openxml_text(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            targets = []
            if ext == ".docx":
                targets = [name for name in names if name.startswith("word/") and name.endswith(".xml")]
            elif ext == ".pptx":
                targets = [name for name in names if name.startswith("ppt/slides/") and name.endswith(".xml")]
            elif ext == ".xlsx":
                targets = [name for name in names if (name.startswith("xl/worksheets/") or name == "xl/sharedStrings.xml") and name.endswith(".xml")]
            elif ext in {".pages", ".key", ".numbers"}:
                targets = [name for name in names if name.endswith((".xml", ".iwa")) and not name.startswith("__MACOSX/")]
            chunks = []
            for name in targets[:120]:
                if name.endswith(".iwa"):
                    continue
                text = xml_text_from_bytes(zf.read(name))
                if text:
                    chunks.append(text)
            return "\n\n".join(chunks)
    except Exception:
        return ""

def local_wiki_extract_with_textutil(path: str) -> str:
    if sys.platform != "darwin" or not shutil.which("textutil"):
        return ""
    try:
        result = subprocess.run(
            ["textutil", "-stdout", "-convert", "txt", path],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except Exception:
        return ""
    return ""

def local_wiki_extract_pdf_text(path: str) -> str:
    if shutil.which("pdftotext"):
        try:
            with tempfile.NamedTemporaryFile(suffix=".txt") as tmp:
                result = subprocess.run(["pdftotext", "-layout", path, tmp.name], capture_output=True, timeout=60)
                if result.returncode == 0:
                    with open(tmp.name, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                    if text.strip():
                        return text
        except Exception:
            pass
    try:
        with open(path, "rb") as f:
            raw = f.read(8 * 1024 * 1024)
        chunks = []
        for match in re.finditer(rb"\(([^()]*)\)", raw):
            bit = match.group(1)
            bit = bit.replace(rb"\\n", b"\n").replace(rb"\\r", b"\n").replace(rb"\\t", b" ")
            text = bit.decode("utf-8", errors="ignore") or bit.decode("latin-1", errors="ignore")
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) >= 2:
                chunks.append(text)
            if len(chunks) >= 5000:
                break
        return "\n".join(chunks)
    except Exception:
        return ""

def local_wiki_extract_document_text(path: str, limit: int = 240000) -> str:
    ext = os.path.splitext(path)[1].lower()
    extractors = []
    if ext in {".docx", ".pptx", ".xlsx", ".pages", ".key", ".numbers"}:
        extractors.append(local_wiki_extract_openxml_text)
    if ext == ".pdf":
        extractors.append(local_wiki_extract_pdf_text)
    extractors.append(local_wiki_extract_with_textutil)
    for extractor in extractors:
        text = extractor(path)
        text = wiki_plain_text(text)
        if text:
            return text[:limit]
    return ""

def local_wiki_read_text(root: str, rel_path: str, limit: int = 240000) -> str:
    path = local_wiki_abs_path(root, rel_path)
    if not os.path.isfile(path):
        return ""
    ext = os.path.splitext(path)[1].lower()
    if ext in {".doc", ".docx", ".rtf", ".ppt", ".pptx", ".xls", ".xlsx", ".pdf", ".pages", ".key", ".numbers"}:
        extracted = local_wiki_extract_document_text(path, limit=limit)
        if extracted:
            return extracted[:limit]
        return f"{os.path.basename(path)} 是 {ext.lstrip('.')} 文件；当前未能从文件中提取可编译文本。"
    if ext and ext not in {".md", ".markdown", ".txt", ".csv", ".json", ".html", ".htm", ".yaml", ".yml"}:
        return f"{os.path.basename(path)} 是 {ext.lstrip('.')} 文件；当前未能从文件中提取可编译文本。"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except Exception:
        return ""

def local_wiki_text_readable(rel_path: str) -> bool:
    ext = os.path.splitext(str(rel_path or "").lower())[1]
    return ext in {"", ".md", ".markdown", ".txt", ".csv", ".json", ".html", ".htm", ".yaml", ".yml", ".doc", ".docx", ".rtf", ".ppt", ".pptx", ".xls", ".xlsx", ".pdf", ".pages", ".key", ".numbers"}

def local_wiki_display_name(name: str) -> str:
    base = str(name or "")
    stem, ext = os.path.splitext(base)
    if ext.lower() in {".md", ".markdown"} and stem:
        return stem
    return base

def local_wiki_tree(root: str, max_files: int = 1200) -> Dict[str, Any]:
    root = local_wiki_validate_root(root)
    max_files = max(1, min(int(max_files or 1200), 3000))
    skip_names = {".DS_Store", "__pycache__"}
    skip_dirs = {".git", ".obsidian", "node_modules", "__pycache__", ".agentwiki"}
    preferred = [
        "00_收件箱",
        "10_日记",
        "20_项目",
        "30_研究",
        "40_知识库",
        "50_资源",
        "60_claude项目库",
        "90_计划",
        "99_System",
    ]
    count = 0

    def sort_key(name: str):
        if name in preferred:
            return (0, preferred.index(name), name)
        return (1, 0, name.lower())

    def walk_dir(abs_dir: str, rel_dir: str = "") -> List[Dict[str, Any]]:
        nonlocal count
        if count >= max_files:
            return []
        try:
            names = sorted(os.listdir(abs_dir), key=sort_key)
        except Exception:
            return []
        dirs = []
        files = []
        for name in names:
            if name in skip_names:
                continue
            abs_path = os.path.join(abs_dir, name)
            rel_path = os.path.join(rel_dir, name).replace("\\", "/") if rel_dir else name
            if os.path.isdir(abs_path):
                if name in skip_dirs:
                    continue
                dirs.append({
                    "type": "dir",
                    "name": name,
                    "display": local_wiki_display_name(name),
                    "path": rel_path,
                    "children": walk_dir(abs_path, rel_path),
                })
            elif os.path.isfile(abs_path):
                count += 1
                if count > max_files:
                    break
                stat = os.stat(abs_path)
                files.append({
                    "type": "file",
                    "name": name,
                    "display": local_wiki_display_name(name),
                    "path": rel_path,
                    "size": stat.st_size,
                    "updated_at": int(stat.st_mtime * 1000),
                    "readable": local_wiki_text_readable(rel_path),
                })
        return dirs + files

    children = walk_dir(root)
    return {
        "root": root,
        "name": os.path.basename(root.rstrip(os.sep)) or "AgentWiki",
        "children": children,
        "truncated": count >= max_files,
        "file_count": count,
    }

def local_wiki_file_payload(root: str, rel_path: str) -> Dict[str, Any]:
    root = local_wiki_validate_root(root)
    path = local_wiki_abs_path(root, rel_path)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="文件不存在。")
    rel = os.path.relpath(path, root).replace("\\", "/")
    stat = os.stat(path)
    readable = local_wiki_text_readable(rel)
    content = local_wiki_read_text(root, rel) if readable else ""
    return {
        "root": root,
        "path": rel,
        "name": os.path.basename(path),
        "display": local_wiki_display_name(os.path.basename(path)),
        "size": stat.st_size,
        "updated_at": int(stat.st_mtime * 1000),
        "readable": readable,
        "content": content,
    }

def local_wiki_write_text_file(root: str, rel_path: str, content: str) -> str:
    path = local_wiki_abs_path(root, rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content.rstrip() + "\n")
    return os.path.relpath(path, root).replace("\\", "/")

def local_wiki_upsert_page(root: str, page: Dict[str, Any]) -> Dict[str, Any]:
    pages = local_wiki_read_machine_json(root, "pages.json", [])
    page["updated_at"] = now_ms()
    existing = next((item for item in pages if item.get("id") == page.get("id")), None)
    if existing:
        existing.update(page)
    else:
        page.setdefault("created_at", now_ms())
        pages.insert(0, page)
    local_wiki_write_machine_json(root, "pages.json", pages)
    return page

def local_wiki_upsert_relation(root: str, source_id: str, relation_type: str, target_id: str, evidence: str = "") -> Dict[str, Any]:
    relations = local_wiki_read_machine_json(root, "relations.json", [])
    key = (source_id, relation_type, target_id)
    existing = next((item for item in relations if (item.get("source"), item.get("type"), item.get("target")) == key), None)
    payload = {
        "source": source_id,
        "type": relation_type,
        "target": target_id,
        "evidence": str(evidence or "")[:240],
        "updated_at": now_ms(),
    }
    if existing:
        existing.update(payload)
    else:
        payload["id"] = f"rel_{uuid.uuid4().hex[:12]}"
        payload["created_at"] = now_ms()
        relations.append(payload)
    local_wiki_write_machine_json(root, "relations.json", relations)
    return payload

def local_wiki_rebuild_graph(root: str) -> Dict[str, Any]:
    root = local_wiki_validate_root(root)
    sources = local_wiki_read_machine_json(root, "sources.json", [])
    pages = local_wiki_read_machine_json(root, "pages.json", [])
    relations = local_wiki_read_machine_json(root, "relations.json", [])
    nodes = []
    seen = set()
    for source in sources:
        if source.get("id") and source.get("id") not in seen:
            nodes.append({
                "id": source.get("id", ""),
                "label": source.get("title") or source.get("path") or "来源",
                "type": "source",
                "summary": source.get("excerpt", ""),
            })
            seen.add(source.get("id"))
    for page in pages:
        if page.get("id") and page.get("id") not in seen:
            nodes.append({
                "id": page.get("id", ""),
                "label": page.get("title") or page.get("id", ""),
                "type": page.get("type") or "page",
                "summary": page.get("excerpt", ""),
            })
            seen.add(page.get("id"))
    graph = {
        "nodes": nodes,
        "edges": [
            {
                "id": item.get("id") or f"{item.get('source')}-{item.get('type')}-{item.get('target')}",
                "source": item.get("source", ""),
                "target": item.get("target", ""),
                "type": item.get("type", "related"),
                "label": item.get("type", "related"),
                "evidence": item.get("evidence", ""),
            }
            for item in relations
            if item.get("source") and item.get("target")
        ],
        "updated_at": now_ms(),
    }
    local_wiki_write_machine_json(root, "graph.json", graph)
    return graph

def local_wiki_refresh_indexes(root: str) -> None:
    sources = local_wiki_read_machine_json(root, "sources.json", [])
    pages = local_wiki_read_machine_json(root, "pages.json", [])
    summaries = [p for p in pages if p.get("type") == "summary"]
    concepts = [p for p in pages if p.get("type") == "concept"]
    projects = [p for p in pages if p.get("type") == "project"]
    local_wiki_write_text_file(root, os.path.join("40_知识库", "索引", "All-Sources.md"), "\n".join([
        "# 所有来源",
        "",
        *[f"- [[{item.get('id')}]] {item.get('title') or item.get('path')}" for item in sources],
        "",
        "## 已生成摘要",
        *[f"- [[{item.get('id')}]] {item.get('title')}" for item in summaries],
    ]))
    local_wiki_write_text_file(root, os.path.join("40_知识库", "索引", "All-Concepts.md"), "\n".join([
        "# 所有概念",
        "",
        *[f"- [[{item.get('id')}]] {item.get('title')}" for item in concepts],
    ]))
    local_wiki_write_text_file(root, os.path.join("40_知识库", "索引", "All-Projects.md"), "\n".join([
        "# 所有项目",
        "",
        *[f"- [[{item.get('id')}]] {item.get('title')}" for item in projects],
    ]))
    graph = local_wiki_read_machine_json(root, "graph.json", {"nodes": [], "edges": []})
    local_wiki_write_text_file(root, os.path.join("40_知识库", "索引", "Architecture-Knowledge.md"), "\n".join([
        "# 知识结构索引",
        "",
        f"- 来源：{len(sources)}",
        f"- 摘要：{len(summaries)}",
        f"- 概念：{len(concepts)}",
        f"- 图谱节点：{len(graph.get('nodes') or [])}",
        f"- 图谱关系：{len(graph.get('edges') or [])}",
    ]))

def parse_json_object_from_text(text: str) -> Dict[str, Any]:
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{.*\}", raw, flags=re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}

async def local_wiki_api_compile_payload(root: str, source: Dict[str, Any], plain: str, provider: str = "", model: str = "") -> tuple[Dict[str, Any], Dict[str, Any]]:
    title = source.get("title") or source.get("path") or "未命名来源"
    prompt = "\n".join([
        "请把下面的来源资料编译为本地知识库条目，只返回 JSON，不要返回 Markdown。",
        "JSON 格式：",
        '{"summary":"300字以内摘要","key_points":["要点1","要点2"],"concepts":[{"title":"概念名","definition":"定义","evidence":"证据"}]}',
        "",
        f"来源标题：{title}",
        f"来源路径：{source.get('path') or ''}",
        "",
        "来源正文：",
        plain[:12000],
    ])
    result = await request_chat_completion(
        prompt,
        system_prompt="你是知识库编译助手。你需要把原始资料整理成摘要、要点、概念和证据。只输出合法 JSON。",
        provider=provider,
        model=model,
    )
    payload = parse_json_object_from_text(result.get("text", ""))
    meta = {"used_llm": bool(payload), "model": result.get("model", ""), "llm_error": ""}
    return payload, meta

def local_wiki_compile_source(root: str, source_id: str, force: bool = False, compile_payload: Optional[Dict[str, Any]] = None, compile_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    root = local_wiki_validate_root(root)
    sources = local_wiki_read_machine_json(root, "sources.json", [])
    source = next((item for item in sources if item.get("id") == source_id), None)
    if not source:
        raise HTTPException(status_code=404, detail="本地知识库来源不存在。")
    if source.get("compiled") and not force:
        return {"source": source, "skipped": True, "summary": None, "concepts": []}

    content = local_wiki_read_text(root, source.get("path", ""))
    plain = wiki_plain_text(content)
    if not plain:
        plain = f"{source.get('title') or source.get('path')} 暂无可读取正文。"
    title = re.sub(r"\s+", " ", str(source.get("title") or os.path.basename(source.get("path", "")) or "未命名来源").strip())[:120]
    now = now_ms()
    suffix = str(source_id)[-8:]
    summary_id = f"sum_{wiki_slug(title, 'summary')}_{suffix}"
    compile_payload = compile_payload or {}
    summary_text = str(compile_payload.get("summary") or "").strip() or wiki_excerpt(plain, 520)
    key_points = [str(item).strip() for item in (compile_payload.get("key_points") or []) if str(item).strip()]
    if not key_points:
        for sentence in re.split(r"[。！？.!?\n]+", plain):
            sentence = sentence.strip()
            if 12 <= len(sentence) <= 140 and sentence not in key_points:
                key_points.append(sentence)
            if len(key_points) >= 6:
                break
    if not key_points:
        key_points = [wiki_excerpt(plain, 140)]
    summary_rel = os.path.join("40_知识库", "摘要", f"{summary_id}.md")
    summary_md = "\n".join([
        "---",
        f'title: "{title} 摘要"',
        "type: summary",
        f"source: {source_id}",
        f"updated: {wiki_now_iso()}",
        "---",
        "",
        f"# {title} 摘要",
        "",
        "## TL;DR",
        summary_text,
        "",
        "## 要点",
        *[f"- {point}" for point in key_points],
        "",
        "## 来源",
        f"- [[{source_id}]] {title}",
    ])
    local_wiki_write_text_file(root, summary_rel, summary_md)
    summary_page = local_wiki_upsert_page(root, {
        "id": summary_id,
        "type": "summary",
        "title": f"{title} 摘要",
        "source_id": source_id,
        "path": summary_rel.replace("\\", "/"),
        "tags": source.get("tags") or [],
        "excerpt": wiki_excerpt(summary_text, 220),
        "created_at": now,
    })
    local_wiki_upsert_relation(root, source_id, "compiled_to", summary_id, title)

    concept_pages = []
    llm_concepts = compile_payload.get("concepts") if isinstance(compile_payload.get("concepts"), list) else []
    concept_inputs = []
    for item in llm_concepts:
        if isinstance(item, dict) and str(item.get("title") or "").strip():
            concept_inputs.append({
                "title": str(item.get("title")).strip(),
                "definition": str(item.get("definition") or "").strip(),
                "evidence": str(item.get("evidence") or "").strip(),
            })
    if not concept_inputs:
        concept_inputs = [{"title": item, "definition": "", "evidence": ""} for item in extract_concept_titles(title, content, source.get("tags") or [])]
    for concept_data in concept_inputs[:10]:
        concept_title = concept_data["title"]
        concept_id = f"concept_{wiki_slug(concept_title, 'concept')}"
        concept_rel = os.path.join("40_知识库", "概念", f"{concept_id}.md")
        definition = concept_data.get("definition") or f"{concept_title} 是从《{title}》中提炼出的可复用知识点。"
        evidence = concept_data.get("evidence") or wiki_excerpt(plain, 420)
        concept_md = "\n".join([
            "---",
            f'title: "{concept_title}"',
            "type: concept",
            f"updated: {wiki_now_iso()}",
            "---",
            "",
            f"# {concept_title}",
            "",
            "## 定义",
            definition,
            "",
            "## 当前理解",
            evidence,
            "",
            "## 证据来源",
            f"- [[{source_id}]] {title}",
            f"- [[{summary_id}]] {title} 摘要",
        ])
        local_wiki_write_text_file(root, concept_rel, concept_md)
        concept_page = local_wiki_upsert_page(root, {
            "id": concept_id,
            "type": "concept",
            "title": concept_title,
        "source_id": source_id,
        "path": concept_rel.replace("\\", "/"),
        "tags": list(dict.fromkeys([*(source.get("tags") or []), "concept"])),
        "excerpt": wiki_excerpt(definition or evidence, 180),
        "created_at": now,
        })
        concept_pages.append(concept_page)
        local_wiki_upsert_relation(root, summary_id, "extracts", concept_id, concept_title)
        local_wiki_upsert_relation(root, source_id, "evidence_for", concept_id, title)

    source["compiled"] = True
    source["summary_id"] = summary_id
    source["concept_ids"] = [item.get("id") for item in concept_pages]
    source["excerpt"] = wiki_excerpt(summary_text, 220)
    source["compile_meta"] = compile_meta or {"used_llm": False, "model": "", "llm_error": ""}
    source["updated_at"] = now_ms()
    local_wiki_write_machine_json(root, "sources.json", sources)
    graph = local_wiki_rebuild_graph(root)
    local_wiki_refresh_indexes(root)
    return {
        "source": source,
        "summary": summary_page,
        "concepts": concept_pages,
        "compile_meta": source["compile_meta"],
        "graph": {"nodes": len(graph.get("nodes") or []), "edges": len(graph.get("edges") or [])},
    }

async def local_wiki_compile(root: str, source_id: str = "", force: bool = False, use_llm: bool = True, provider: str = "", model: str = "") -> Dict[str, Any]:
    root = local_wiki_validate_root(root)
    sources = local_wiki_read_machine_json(root, "sources.json", [])
    targets = [item for item in sources if not source_id or item.get("id") == source_id]
    if source_id and not targets:
        raise HTTPException(status_code=404, detail="本地知识库来源不存在。")
    results = []
    for source in targets:
        if source.get("compiled") and not force:
            results.append({"source": source, "skipped": True, "summary": None, "concepts": []})
            continue
        compile_payload = {}
        compile_meta = {"used_llm": False, "model": "", "llm_error": ""}
        if use_llm:
            try:
                content = local_wiki_read_text(root, source.get("path", ""))
                plain = wiki_plain_text(content) or f"{source.get('title') or source.get('path')} 暂无可读取正文。"
                compile_payload, compile_meta = await local_wiki_api_compile_payload(root, source, plain, provider=provider, model=model)
                if not compile_payload:
                    compile_meta["llm_error"] = compile_meta.get("llm_error") or "模型返回内容无法解析为 JSON，已使用本地兜底编译。"
            except Exception as exc:
                compile_meta["llm_error"] = getattr(exc, "detail", None) or str(exc)
        results.append(local_wiki_compile_source(root, source.get("id", ""), force=force, compile_payload=compile_payload, compile_meta=compile_meta))
    graph = local_wiki_rebuild_graph(root)
    local_wiki_refresh_indexes(root)
    return {
        "compiled": len([item for item in results if not item.get("skipped")]),
        "skipped": len([item for item in results if item.get("skipped")]),
        "total": len(results),
        "results": results,
        "graph": {"nodes": len(graph.get("nodes") or []), "edges": len(graph.get("edges") or [])},
        "overview": local_wiki_overview_payload(root),
    }

async def local_wiki_compile_imported(root: str, imported: List[Dict[str, Any]], use_llm: bool = True) -> Dict[str, Any]:
    source_ids = [item.get("id") for item in imported if item.get("id")]
    results = []
    for source_id in source_ids:
        compiled = await local_wiki_compile(root, source_id=source_id, force=True, use_llm=use_llm)
        results.extend(compiled.get("results") or [])
    graph = local_wiki_rebuild_graph(root)
    local_wiki_refresh_indexes(root)
    return {
        "compiled": len([item for item in results if not item.get("skipped")]),
        "skipped": len([item for item in results if item.get("skipped")]),
        "total": len(results),
        "results": results,
        "graph": {"nodes": len(graph.get("nodes") or []), "edges": len(graph.get("edges") or [])},
        "overview": local_wiki_overview_payload(root),
    }

def local_wiki_refresh_imported_records(root: str, imported: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    source_map = {item.get("id"): item for item in local_wiki_read_machine_json(root, "sources.json", [])}
    return [source_map.get(item.get("id"), item) for item in imported]

def local_wiki_search_items(root: str, query: str, page_type: str = "", limit: int = 20) -> List[Dict[str, Any]]:
    root = local_wiki_validate_root(root)
    q = str(query or "").strip().lower()
    terms = wiki_search_terms(q)
    page_type = str(page_type or "").strip()
    candidates = []
    for source in local_wiki_read_machine_json(root, "sources.json", []):
        content = local_wiki_read_text(root, source.get("path", ""))
        haystack = " ".join([
            source.get("title", ""),
            source.get("source_type", ""),
            " ".join(source.get("tags") or []),
            source.get("excerpt", ""),
            source.get("path", ""),
            content,
        ]).lower()
        score = wiki_search_score(haystack, q, terms)
        if score:
            candidates.append({"kind": "source", "score": score, "item": source, "root": root})
    for page in local_wiki_read_machine_json(root, "pages.json", []):
        if page_type and page.get("type") != page_type:
            continue
        content = local_wiki_read_text(root, page.get("path", ""))
        haystack = " ".join([
            page.get("title", ""),
            page.get("type", ""),
            " ".join(page.get("tags") or []),
            page.get("excerpt", ""),
            page.get("path", ""),
            content,
        ]).lower()
        score = wiki_search_score(haystack, q, terms)
        if score:
            candidates.append({"kind": "page", "score": score, "item": page, "root": root})
    candidates.sort(key=lambda item: (item["score"], item["item"].get("updated_at", 0)), reverse=True)
    return candidates[:max(1, min(int(limit or 20), 100))]

def local_wiki_recent_context_items(root: str, limit: int = 24) -> List[Dict[str, Any]]:
    root = local_wiki_validate_root(root)
    items = []
    for source in local_wiki_read_machine_json(root, "sources.json", []):
        items.append({"kind": "source", "score": 1, "item": source, "root": root})
    for page in local_wiki_read_machine_json(root, "pages.json", []):
        items.append({"kind": "page", "score": 1, "item": page, "root": root})
    items.sort(key=lambda item: item["item"].get("updated_at", item["item"].get("created_at", 0)), reverse=True)
    return items[:max(1, min(int(limit or 24), 100))]

def local_wiki_should_use_recent_fallback(question: str) -> bool:
    compact = re.sub(r"[\s，。！？!?,.~～]+", "", str(question or "").strip().lower())
    if not compact:
        return False
    casual = {"你好", "您好", "嗨", "hi", "hello", "hey", "在吗", "谢谢", "thanks", "thankyou", "早", "早上好", "晚上好", "ok", "好的"}
    if compact in casual:
        return False
    if len(compact) <= 8:
        return False
    return True

def read_agent_wiki_match_content(match: Dict[str, Any]) -> str:
    item = match.get("item") or {}
    root = match.get("root") or ""
    if root:
        return local_wiki_read_text(root, item.get("path", ""))
    return read_wiki_markdown(item.get("path", ""))

def local_wiki_create_output_page(root: str, output_type: str, title: str, content: str, related_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    root = local_wiki_validate_root(root)
    output_type = output_type if output_type in {"qa", "report", "design", "health"} else "report"
    title = re.sub(r"\s+", " ", str(title or "").strip())[:120] or "本地知识库输出"
    page_id = f"{output_type}_{wiki_slug(title, output_type)}_{uuid.uuid4().hex[:6]}"
    folder = {
        "qa": os.path.join("50_资源", "问答档案"),
        "report": os.path.join("50_资源", "报告"),
        "design": os.path.join("50_资源", "报告"),
        "health": os.path.join("50_资源", "健康检查"),
    }.get(output_type, os.path.join("50_资源", "报告"))
    rel_path = os.path.join(folder, f"{page_id}.md")
    markdown = "\n".join([
        "---",
        f'title: "{title}"',
        f"type: {output_type}",
        f"updated: {wiki_now_iso()}",
        "---",
        "",
        f"# {title}",
        "",
        content.strip(),
    ])
    local_wiki_write_text_file(root, rel_path, markdown)
    page = local_wiki_upsert_page(root, {
        "id": page_id,
        "type": output_type,
        "title": title,
        "path": rel_path.replace("\\", "/"),
        "tags": [output_type],
        "excerpt": wiki_excerpt(content),
        "created_at": now_ms(),
    })
    for related_id in related_ids or []:
        local_wiki_upsert_relation(root, page_id, "references", str(related_id), title)
    local_wiki_rebuild_graph(root)
    local_wiki_refresh_indexes(root)
    return page

def wiki_answer_snippets_from_matches(question: str, matches: List[Dict[str, Any]]) -> tuple[str, List[Dict[str, Any]], List[str]]:
    snippets = []
    related_ids = []
    for match in matches[:18]:
        item = match.get("item") or {}
        content = read_agent_wiki_match_content(match)
        snippets.append({
            "id": item.get("id", ""),
            "title": item.get("title", ""),
            "type": item.get("type") or item.get("source_type") or match.get("kind"),
            "path": item.get("path", ""),
            "excerpt": wiki_excerpt(content or item.get("excerpt", ""), 420),
            "content": wiki_excerpt(content or item.get("excerpt", ""), 1800),
        })
        if item.get("id"):
            related_ids.append(item.get("id"))
    if not snippets:
        answer = "当前知识库还没有命中资料。建议先导入来源并执行编译，再进行问答。"
    else:
        bullet_lines = [f"- {item['title']}：{item['excerpt']}" for item in snippets]
        answer = "\n".join([
            f"针对“{question}”，当前知识库命中了 {len(snippets)} 条可用上下文。",
            "",
            "## 依据",
            *bullet_lines,
            "",
            "## 初步结论",
            "这些资料可以作为当前回答的本地依据；如果需要更高精度，可以允许 Agent 调用已配置的聊天模型生成综合回答。",
        ])
    return answer, snippets, related_ids

async def answer_from_wiki_context(
    question: str,
    matches: List[Dict[str, Any]],
    use_llm: bool = True,
    provider: str = "",
    model: str = "",
    local_only: bool = False,
) -> tuple[str, List[Dict[str, Any]], List[str], Dict[str, Any]]:
    fallback_answer, snippets, related_ids = wiki_answer_snippets_from_matches(question, matches)
    meta = {"used_llm": False, "model": "", "llm_error": ""}
    if not use_llm or local_only or not snippets:
        return fallback_answer, snippets, related_ids, meta
    context_lines = []
    for item in snippets:
        context_lines.append("\n".join([
            f"### {item.get('title')}",
            f"类型：{item.get('type') or ''}",
            f"路径：{item.get('path') or ''}",
            item.get("content") or item.get("excerpt") or "",
        ]))
    prompt = "\n\n".join([
        "请扫描下面的本地知识库内容，直接回答用户问题。",
        "如果回答过程中形成了可沉淀的结论、待办、方案或资料结构，请在回答中明确列出，系统会把本次回答保存成本地知识库资产。",
        f"用户问题：{question}",
        "本地知识库内容：",
        "\n\n".join(context_lines),
    ])
    try:
        result = await request_chat_completion(
            prompt,
            system_prompt="你是本地知识库问答引擎。阅读给定知识库内容后直接回答，并尽量给出可保存、可复用的知识资产。",
            provider=provider,
            model=model,
        )
        answer = result.get("text") or fallback_answer
        meta.update({"used_llm": bool(result.get("text")), "model": result.get("model") or ""})
        return answer, snippets, related_ids, meta
    except Exception as exc:
        meta["llm_error"] = getattr(exc, "detail", None) or str(exc)
        return fallback_answer, snippets, related_ids, meta

def local_wiki_overview_payload(root: str):
    root = local_wiki_validate_root(root)
    config = local_wiki_read_machine_json(root, "config.json", {})
    sources = local_wiki_read_machine_json(root, "sources.json", [])
    pages = local_wiki_read_machine_json(root, "pages.json", [])
    relations = local_wiki_read_machine_json(root, "relations.json", [])
    graph = local_wiki_read_machine_json(root, "graph.json", {"nodes": [], "edges": []})
    by_type = {}
    for page in pages:
        by_type[page.get("type") or "page"] = by_type.get(page.get("type") or "page", 0) + 1
    directories = []
    for rel in LOCAL_WIKI_DIRS:
        path = os.path.join(root, rel)
        file_count = 0
        if os.path.isdir(path):
            for _, _, files in os.walk(path):
                file_count += len([f for f in files if f != ".DS_Store"])
        directories.append({
            "path": rel.replace("\\", "/"),
            "exists": os.path.isdir(path),
            "file_count": file_count,
        })
    return {
        "root": root,
        "config": config,
        "counts": {
            "sources": len(sources),
            "pages": len(pages),
            "relations": len(relations),
            "graph_nodes": len(graph.get("nodes") or []),
            "graph_edges": len(graph.get("edges") or []),
            **by_type,
        },
        "recent_sources": sorted(sources, key=lambda item: item.get("updated_at", 0), reverse=True)[:12],
        "recent_pages": sorted(pages, key=lambda item: item.get("updated_at", 0), reverse=True)[:12],
        "directories": directories,
    }

def choose_local_folder_dialog():
    if sys.platform == "darwin":
        script = 'POSIX path of (choose folder with prompt "选择本地知识库保存位置")'
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                check=True,
                capture_output=True,
                text=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as exc:
            raise HTTPException(status_code=400, detail="已取消选择文件夹。") from exc
    raise HTTPException(status_code=400, detail="当前系统暂不支持原生文件夹选择，请手动输入路径。")

def prompt_library_without_static_external(data):
    libraries = [
        lib for lib in (data or {}).get("libraries", [])
        if lib.get("id") not in {STATIC_GPT_IMAGE_2_LIBRARY_ID, AWESOME_GPT_IMAGE_2_LIBRARY_ID}
    ]
    return {"libraries": libraries}

def awesome_gpt_image_2_paths():
    return {
        "root": AWESOME_GPT_IMAGE_2_DIR,
        "style": os.path.join(AWESOME_GPT_IMAGE_2_DIR, "data", "style-library.json"),
        "cases": os.path.join(AWESOME_GPT_IMAGE_2_DIR, "data", "cases.json"),
        "images": os.path.join(AWESOME_GPT_IMAGE_2_DIR, "data", "images"),
    }

def awesome_gpt_image_2_synced():
    paths = awesome_gpt_image_2_paths()
    return (
        os.path.isfile(paths["style"])
        and os.path.isfile(paths["cases"])
        and os.path.isdir(paths["images"])
    )

def awesome_gpt_image_2_status():
    paths = awesome_gpt_image_2_paths()
    style = _library_read_json(paths["style"], {}) if os.path.isfile(paths["style"]) else {}
    cases = _library_read_json(paths["cases"], {}) if os.path.isfile(paths["cases"]) else {}
    image_count = 0
    latest_mtime = 0
    for path in (paths["style"], paths["cases"], paths["images"]):
        if os.path.exists(path):
            latest_mtime = max(latest_mtime, int(os.path.getmtime(path) * 1000))
    if os.path.isdir(paths["images"]):
        image_count = len([
            name for name in os.listdir(paths["images"])
            if os.path.isfile(os.path.join(paths["images"], name))
        ])
    return {
        "id": "awesome-gpt-image-2",
        "name": "awesome-gpt-image-2",
        "repo": AWESOME_GPT_IMAGE_2_WEB,
        "synced": awesome_gpt_image_2_synced(),
        "template_count": len(style.get("templates") or []),
        "case_count": len(cases.get("cases") or []),
        "image_count": image_count,
        "updated_at": latest_mtime,
        "path": paths["root"],
    }

def awesome_safe_image_name(value: str):
    name = os.path.basename(str(value or "").strip())
    if not re.match(r"^[A-Za-z0-9_.-]+$", name):
        return ""
    return name

def awesome_image_url(value: str):
    name = awesome_safe_image_name(value)
    return f"/api/prompt-sources/awesome-gpt-image-2/images/{urllib.parse.quote(name)}" if name else ""

def awesome_category_id_map(style):
    return {
        str(cat.get("value") or ""): str(cat.get("id") or "cat-other")
        for cat in (style.get("categories") or [])
        if isinstance(cat, dict)
    }

def localized_value(value, fallback=""):
    if isinstance(value, dict):
        return str(value.get("zh") or value.get("en") or fallback)
    return str(value or fallback)

def awesome_lines(values):
    return "\n".join(f"- {str(item)}" for item in (values or []) if str(item).strip())

def awesome_template_source_url(template):
    anchor = str(template.get("anchor") or "").strip()
    suffix = f"#{anchor}" if anchor else ""
    return f"{AWESOME_GPT_IMAGE_2_WEB}/blob/main/docs/templates.md{suffix}"

def awesome_template_item(template, category_map, now):
    title = localized_value(template.get("title"), template.get("id"))
    desc = localized_value(template.get("description"))
    use_when = localized_value(template.get("useWhen"))
    guidance = (template.get("guidance") or {}).get("zh") or (template.get("guidance") or {}).get("en") or []
    pitfalls = (template.get("pitfalls") or {}).get("zh") or (template.get("pitfalls") or {}).get("en") or []
    styles = [str(x) for x in (template.get("styles") or []) if str(x).strip()]
    scenes = [str(x) for x in (template.get("scenes") or []) if str(x).strip()]
    tags = [str(x) for x in (template.get("tags") or []) if str(x).strip()]
    category = category_map.get(str(template.get("category") or ""), "cat-other")
    source_url = awesome_template_source_url(template)
    positive = (
        f"【模板】{title}\n"
        f"【用途】{desc}\n"
        f"【适用场景】{use_when}\n\n"
        "【填空结构】\n"
        f"1. 明确输出类型：{title}。\n"
        "2. 写清主体/产品/人物/空间/主题：[填写具体对象]。\n"
        "3. 设定构图和布局：[比例、视角、层级、主视觉位置、留白方式]。\n"
        f"4. 设定视觉风格：风格标签 {'、'.join(styles) or '按主题选择'}；场景标签 {'、'.join(scenes) or '按主题选择'}。\n"
        "5. 锁定文字与信息：[标题、短文案、标签、UI 文案；要求准确可读]。\n"
        "6. 输出规格：[1:1 / 4:5 / 9:16 / 16:9 / 其他]，高完成度成图。\n\n"
        f"【生成要求】\n{awesome_lines(guidance)}\n\n"
        f"【标签】{'、'.join(tags) or '无'}\n"
        f"【来源】{source_url}"
    ).strip()
    negative = "\n".join(dict.fromkeys([
        *[str(x) for x in pitfalls],
        "避免乱码文字、错别字和无意义占位符。",
        "避免主体不清、布局拥挤、信息层级混乱。",
    ]))
    return {
        "id": f"awesome_tpl_{str(template.get('id') or uuid.uuid4().hex)}",
        "name": f"GPT-Image2｜{title}",
        "category": category,
        "positive": positive,
        "negative": negative,
        "scene": f"工业模板。分类：{template.get('category') or 'Other'}。示例参考：{', '.join('case ' + str(x) for x in (template.get('exampleCases') or [])) or '见来源仓库'}。",
        "created_at": now,
        "updated_at": now,
        "readonly": True,
        "external": True,
        "source_kind": "template",
        "source_url": source_url,
        "github_url": source_url,
        "image_url": awesome_image_url(template.get("cover") or ""),
        "styles": styles,
        "scenes": scenes,
        "tags": tags,
    }

def awesome_case_item(case, category_map, now):
    case_id = str(case.get("id") or uuid.uuid4().hex)
    category = category_map.get(str(case.get("category") or ""), "cat-other")
    styles = [str(x) for x in (case.get("styles") or []) if str(x).strip()]
    scenes = [str(x) for x in (case.get("scenes") or []) if str(x).strip()]
    source_label = str(case.get("sourceLabel") or "").strip()
    source_url = str(case.get("sourceUrl") or "").strip()
    github_url = str(case.get("githubUrl") or "").strip() or f"{AWESOME_GPT_IMAGE_2_WEB}/blob/main/docs/gallery.md"
    meta = [
        "案例参考",
        f"分类：{case.get('category')}" if case.get("category") else "",
        f"风格：{'、'.join(styles)}" if styles else "",
        f"场景：{'、'.join(scenes)}" if scenes else "",
        f"来源：{source_label}" if source_label else "",
    ]
    return {
        "id": f"awesome_case_{case_id}",
        "name": f"案例 {case_id}｜{sanitize_prompt_name(case.get('title'), 'GPT-Image2 案例')}",
        "category": category,
        "positive": sanitize_prompt_text(case.get("prompt")),
        "negative": "",
        "scene": "。".join([x for x in meta if x]) + "。",
        "created_at": now,
        "updated_at": now,
        "readonly": True,
        "external": True,
        "source_kind": "case",
        "source_url": source_url,
        "github_url": github_url,
        "source_label": source_label,
        "image_url": awesome_image_url(case.get("image") or ""),
        "image_alt": str(case.get("imageAlt") or case.get("title") or ""),
        "styles": styles,
        "scenes": scenes,
        "tags": [],
    }

def build_awesome_gpt_image_2_library():
    if not awesome_gpt_image_2_synced():
        return None
    paths = awesome_gpt_image_2_paths()
    style = _library_read_json(paths["style"], {})
    cases = _library_read_json(paths["cases"], {})
    category_map = awesome_category_id_map(style)
    now = awesome_gpt_image_2_status().get("updated_at") or now_ms()
    categories = [
        {
            "id": str(cat.get("id") or "cat-other"),
            "name": localized_value(cat.get("title"), cat.get("value") or "分类"),
        }
        for cat in (style.get("categories") or [])
        if isinstance(cat, dict)
    ]
    category_ids = {cat["id"] for cat in categories}
    if "cat-other" not in category_ids:
        categories.append({"id": "cat-other", "name": "其他应用场景"})
    template_items = [awesome_template_item(t, category_map, now) for t in (style.get("templates") or []) if isinstance(t, dict)]
    case_items = [awesome_case_item(c, category_map, now) for c in (cases.get("cases") or []) if isinstance(c, dict)]
    styles = sorted({*(str(x.get("value") or "") for x in (style.get("styles") or []) if isinstance(x, dict)), *(s for item in case_items for s in item.get("styles", []))})
    scenes = sorted({*(str(x.get("value") or "") for x in (style.get("scenes") or []) if isinstance(x, dict)), *(s for item in case_items for s in item.get("scenes", []))})
    return {
        "id": AWESOME_GPT_IMAGE_2_LIBRARY_ID,
        "name": "GPT-Image2 案例库",
        "type": "prompt",
        "readonly": True,
        "external": True,
        "source_id": "awesome-gpt-image-2",
        "source_url": AWESOME_GPT_IMAGE_2_WEB,
        "license": "MIT",
        "categories": categories,
        "items": [*template_items, *case_items],
        "filters": {
            "kinds": [
                {"id": "template", "name": "工业模板"},
                {"id": "case", "name": "案例参考"},
            ],
            "styles": [x for x in styles if x],
            "scenes": [x for x in scenes if x],
        },
        "status": awesome_gpt_image_2_status(),
        "created_at": now,
        "updated_at": now,
    }

def public_prompt_libraries(data=None):
    local_data = prompt_library_without_static_external(data or load_prompt_libraries())
    external = build_awesome_gpt_image_2_library()
    if external:
        local_data["libraries"].append(external)
    return local_data

def find_prompt_library(data: Dict[str, Any], library_id: str):
    return next((lib for lib in data.get("libraries", []) if lib.get("id") == library_id), None)

def extract_library_canvas_refs(notes: str = "", image: Optional[Dict[str, Any]] = None):
    record = image or {}
    canvas_id = str(record.get("source_canvas_id") or "").strip()
    canvas_title = str(record.get("source_canvas_title") or "").strip()
    node_id = str(record.get("source_node_id") or "").strip()
    text = str(notes or record.get("notes") or "")
    if text:
        if not canvas_title:
            match = re.search(r"^来自智能画布：(.+)$", text, re.MULTILINE)
            if match:
                canvas_title = match.group(1).strip()
        if not canvas_id:
            match = re.search(r"^画布ID：([A-Za-z0-9_-]+)$", text, re.MULTILINE)
            if match:
                canvas_id = match.group(1).strip()
        if not node_id:
            match = re.search(r"^节点ID：([A-Za-z0-9_-]+)$", text, re.MULTILINE)
            if match:
                node_id = match.group(1).strip()
    return {
        "source_canvas_id": canvas_id,
        "source_canvas_title": canvas_title,
        "source_node_id": node_id,
    }

def library_asset_for_project(image: Dict[str, Any], project_id: str, create: bool = False):
    project_id = str(project_id or "").strip()
    if not project_id or not DOMAIN_STORE.get_project(project_id):
        return None
    image = normalize_library_image_scope(image)
    if image.get("scope") == "project" and image.get("project_id") != project_id:
        return None
    url = str(image.get("url") or "").strip()
    if not url:
        return None
    existing = DOMAIN_STORE.asset_by_url(url, project_id)
    if existing or not create:
        return existing
    requested_asset_id = str(image.get("asset_id") or "") if image.get("scope") == "project" else ""
    return DOMAIN_STORE.register_asset(
        project_id,
        url,
        asset_id=requested_asset_id,
        title=str(image.get("filename") or ""),
        source="project_library" if image.get("scope") == "project" else "shared_library_reference",
        width=int(image.get("width") or 0),
        height=int(image.get("height") or 0),
        byte_size=int(image.get("size_bytes") or 0),
        metadata={"library_image_id": image.get("id") or "", "library_scope": image.get("scope") or "shared"},
    )

def library_image_feedback(image: Dict[str, Any], project_id: str):
    asset = library_asset_for_project(image, project_id, create=False)
    if not asset:
        return DOMAIN_STORE._feedback_summary([]), None
    return DOMAIN_STORE.feedback_for_asset(asset["id"], project_id), asset

def build_project_preference_profile(project_id: str) -> Dict[str, Any]:
    project_id = str(project_id or "").strip()
    if not project_id or not DOMAIN_STORE.get_project(project_id):
        return {}
    summary = DOMAIN_STORE.project_feedback_summary(project_id, limit=50)
    library_by_id = {str(item.get("id") or ""): item for item in load_library_images()}
    positive_terms = Counter()
    avoided_terms = Counter()
    positive_assets = []
    avoided_assets = []
    explicit_signal_count = 0
    for asset in summary.get("top_assets") or []:
        feedback = asset.get("feedback") or {}
        try:
            metadata = json.loads(asset.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        library_item = library_by_id.get(str(metadata.get("library_image_id") or "")) or {}
        terms = [
            *(library_item.get("categories") or []),
            *(library_item.get("ai_tags") or []),
            *(library_item.get("manual_tags") or []),
            str(library_item.get("source_operation") or ""),
            str(library_item.get("material_name") or ""),
        ]
        generic_terms = {"设计Agent", "项目素材", "参考图", "生成结果"}
        terms = list(dict.fromkeys(
            str(term).strip()
            for term in terms
            if str(term).strip() and str(term).strip() not in generic_terms and len(str(term).strip()) <= 16
        ))
        rating = int(feedback.get("rating") or 0)
        is_positive = bool(feedback.get("adopted") or feedback.get("favorited") or rating >= 4) and not feedback.get("rejected")
        is_avoided = bool(feedback.get("rejected") or (rating and rating <= 2))
        explicit_signal_count += int(bool(feedback.get("adopted"))) + int(bool(feedback.get("favorited"))) + int(bool(rating)) + int(bool(feedback.get("rejected")))
        asset_record = {
            "asset_id": str(asset.get("id") or ""),
            "title": str(asset.get("title") or library_item.get("filename") or "未命名素材"),
            "storage_url": str(asset.get("storage_url") or library_item.get("url") or ""),
            "score": int(feedback.get("score") or 0),
            "rating": rating,
            "terms": terms[:12],
        }
        if is_positive:
            positive_assets.append(asset_record)
            positive_terms.update(terms)
        if is_avoided:
            avoided_assets.append(asset_record)
            avoided_terms.update(terms)
    preferred = [term for term, _ in positive_terms.most_common(12) if term not in avoided_terms]
    avoided = [term for term, _ in avoided_terms.most_common(12)]
    guidance = []
    if preferred:
        guidance.append("优先考虑：" + "、".join(preferred[:8]))
    if avoided:
        guidance.append("降低或避免：" + "、".join(avoided[:8]))
    if positive_assets:
        guidance.append("优先参考已采纳/高评分素材，不要仅按时间排序。")
    return {
        "project_id": project_id,
        "event_count": int(summary.get("event_count") or 0),
        "explicit_signal_count": explicit_signal_count,
        "adopted_assets": int(summary.get("adopted_assets") or 0),
        "favorited_assets": int(summary.get("favorited_assets") or 0),
        "rejected_assets": int(summary.get("rejected_assets") or 0),
        "rated_assets": int(summary.get("rated_assets") or 0),
        "average_rating": float(summary.get("average_rating") or 0),
        "preferred_terms": preferred,
        "avoided_terms": avoided,
        "positive_assets": positive_assets[:8],
        "avoided_assets": avoided_assets[:8],
        "guidance": guidance,
        "ready": explicit_signal_count >= 2,
    }

def refresh_project_preference_skill_candidate(project_id: str) -> Optional[Dict[str, Any]]:
    profile = build_project_preference_profile(project_id)
    if not profile.get("ready"):
        return None
    instructions = "\n".join([
        "# 项目素材偏好候选",
        "",
        "这是由项目反馈事件生成的候选规则，未发布到正式 Skill。",
        *(f"- {line}" for line in (profile.get("guidance") or ["当前信号尚少，仅作候选。"])),
        "- 使用偏好时仍需尊重当前任务的明确约束。",
    ])
    return DOMAIN_STORE.upsert_skill_candidate(
        project_id,
        "material-preference",
        "项目素材偏好候选",
        instructions,
        evidence=profile,
    )

def enrich_library_image_record(image, source_map=None, project_id: str = ""):
    if not isinstance(image, dict):
        return image
    if source_map is None:
        source_map = {str(s.get("id") or ""): s for s in load_library_sources()}
    record = dict(image)
    record = normalize_library_image_scope(record)
    feedback, project_asset = library_image_feedback(record, project_id)
    record["asset_id"] = (project_asset or {}).get("id") or record.get("asset_id") or (record.get("id") if record["scope"] == "project" else "") or ""
    record["feedback"] = feedback
    if project_id:
        record["favorited"] = feedback["favorited"]
        record["adopted"] = feedback["adopted"]
        record["rejected"] = feedback["rejected"]
        record["rating"] = feedback["rating"]
    source_id = str(record.get("source_id") or "")
    src = source_map.get(source_id) or {}
    if source_id:
        record["source_name"] = src.get("name") or record.get("source_name") or source_id
    refs = extract_library_canvas_refs(record.get("notes"), record)
    record.update(refs)
    return record

def _snap_to_preset(value, presets):
    """将 AI 返回的分类值校验到允许的预设列表中。精确匹配优先，否则前缀/包含匹配，最后回退到第一个。"""
    if not value:
        return presets[0] if presets else ""
    value = value.strip()
    # 精确匹配
    if value in presets:
        return value
    # 去掉斜杠后半部分再匹配（如 "大堂/门厅" -> "大堂"）
    base = value.split("/")[0].strip()
    if base in presets:
        return base
    # 前缀匹配
    for p in presets:
        if p.startswith(value) or value.startswith(p):
            return p
    # 包含匹配
    for p in presets:
        if value in p or p in value:
            return p
    # 回退
    return presets[0] if presets else value

def public_provider(provider):
    key = provider_env_key_value(provider["id"])
    return {
        **provider,
        "has_key": bool(key),
        "key_preview": mask_secret(key),
        "key_env": provider_key_env(provider["id"]),
    }

def get_primary_provider_id(providers=None):
    """返回当前首选 provider 的 id；优先 primary=True 的，否则取第一个非 modelscope 的，再次取第一个。"""
    providers = providers if providers is not None else load_api_providers()
    primary = next((p for p in providers if p.get("primary") and p.get("enabled", True)), None)
    if primary:
        return primary["id"]
    non_ms = next((p for p in providers if p["id"] != "modelscope" and p.get("enabled", True)), None)
    if non_ms:
        return non_ms["id"]
    return providers[0]["id"] if providers else "modelscope"

def get_api_provider(provider_id="comfly"):
    providers = load_api_providers()
    target = (provider_id or "").strip().lower()
    # 兼容旧的 "comfly" 硬编码：若 comfly 不存在或未指定，回退到首选 provider
    if not target or not any(p["id"] == target for p in providers):
        target = get_primary_provider_id(providers)
    provider = next((p for p in providers if p["id"] == target), None)
    if not provider:
        raise HTTPException(status_code=400, detail=f"未找到 API 平台：{target}")
    if not provider.get("enabled", True):
        raise HTTPException(status_code=400, detail=f"API 平台已禁用：{provider.get('name') or target}")
    return provider

def get_api_provider_exact(provider_id: str):
    providers = load_api_providers()
    target = (provider_id or "").strip().lower()
    provider = next((p for p in providers if p["id"] == target), None)
    if not provider:
        raise HTTPException(status_code=400, detail=f"未找到 API 平台：{target or '(empty)'}。新增平台未保存时请使用当前表单拉取模型。")
    if not provider.get("enabled", True):
        raise HTTPException(status_code=400, detail=f"API 平台已禁用：{provider.get('name') or target}")
    return provider

def env_quote(value):
    text = str(value or "")
    if not text or re.search(r"\s|#|['\"]", text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text

def update_env_values(updates):
    os.makedirs(os.path.dirname(API_ENV_FILE), exist_ok=True)
    lines = []
    if os.path.exists(API_ENV_FILE):
        with open(API_ENV_FILE, "r", encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    seen = set()
    next_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            next_lines.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            next_lines.append(f"{key}={env_quote(updates[key])}")
            os.environ[key] = str(updates[key] or "")
            seen.add(key)
        else:
            next_lines.append(line)
    for key, value in updates.items():
        if key not in seen:
            next_lines.append(f"{key}={env_quote(value)}")
            os.environ[key] = str(value or "")
    with open(API_ENV_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(next_lines).rstrip() + "\n")

BACKEND_LOCAL_LOAD = {addr: 0 for addr in COMFYUI_INSTANCES}

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(ASSETS_DIR, exist_ok=True)
os.makedirs(PPT_WORKBENCH_DIR, exist_ok=True)
os.makedirs(OUTPUT_INPUT_DIR, exist_ok=True)
os.makedirs(OUTPUT_OUTPUT_DIR, exist_ok=True)
if not getattr(sys, 'frozen', False):
    os.makedirs(STATIC_DIR, exist_ok=True)
    os.makedirs(WORKFLOW_DIR, exist_ok=True)
os.makedirs(CONVERSATION_DIR, exist_ok=True)
os.makedirs(CANVAS_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

# --- Pydantic 模型 ---

class GenerateRequest(BaseModel):
    prompt: str = ""
    width: int = 1024
    height: int = 1024
    workflow_json: str = "Z-Image.json"
    params: Dict[str, Any] = {}
    type: str = "zimage"
    client_id: str = ""
    convert_to_jpg: bool = False

class DeleteHistoryRequest(BaseModel):
    timestamp: float

class TokenRequest(BaseModel):
    token: str

class CloudGenRequest(BaseModel):
    prompt: str
    api_key: str = ""
    model: str = ""
    resolution: str = "1024x1024"
    type: str = "zimage"
    image_urls: List[str] = []
    loras: Optional[Any] = None
    client_id: Optional[str] = None

class CloudPollRequest(BaseModel):
    task_id: str
    api_key: str = ""
    client_id: Optional[str] = None

class AIReference(BaseModel):
    url: str = ""
    name: str = ""
    role: str = ""
    asset_id: str = ""
    node_id: str = ""
    region: Dict[str, Any] = Field(default_factory=dict)

class OnlineImageRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=ONLINE_IMAGE_PROMPT_MAX_LENGTH)
    provider_id: str = "comfly"
    model: str = ""
    size: str = "1024x1024"
    quality: str = "auto"
    n: int = Field(default=1, ge=1, le=4)
    reference_images: List[AIReference] = []
    preserve_canvas: bool = False
    source_width: int = Field(default=0, ge=0)
    source_height: int = Field(default=0, ge=0)
    project_id: str = ""
    canvas_id: str = ""
    source_node_id: str = ""
    quality_gate: bool = False
    quality_requirements: List[str] = []
    quality_pass_threshold: float = Field(default=75, ge=0, le=100)
    quality_max_retries: int = Field(default=0, ge=0, le=2)
    quality_attempt: int = Field(default=1, ge=1, le=3)
    quality_root_task_id: str = ""
    quality_parent_task_id: str = ""
    judge_provider: str = ""
    judge_model: str = ""
    context_compilation_id: str = ""
    compiled_constraints: List[str] = []

class QualityEvaluationRequest(BaseModel):
    judge_provider: str = ""
    judge_model: str = ""
    requirements: List[str] = []
    pass_threshold: float = Field(default=75, ge=0, le=100)

class QualityRetryRequest(BaseModel):
    judge_provider: str = ""
    judge_model: str = ""
    max_retries: int = Field(default=0, ge=0, le=2)

CANVAS_TASKS: Dict[str, Dict[str, Any]] = {}
CANVAS_ASYNC_TASKS: Dict[str, asyncio.Task] = {}
CANVAS_TASK_LOCK = Lock()

class CanvasVideoRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=VIDEO_PROMPT_MAX_LENGTH)
    provider_id: str = "comfly"
    model: str = "veo3-fast"
    duration: int = 5
    aspect_ratio: str = "16:9"
    resolution: str = ""
    size: str = ""
    images: List[AIReference] = []
    videos: List[str] = []
    enhance_prompt: bool = False
    enable_upsample: bool = False
    watermark: bool = False
    seed: Optional[int] = None
    camerafixed: bool = False
    return_last_frame: bool = False
    generate_audio: bool = False

class ApiProviderPayload(BaseModel):
    id: str = ""
    name: str = ""
    base_url: str = ""
    protocol: str = "openai"
    enabled: bool = True
    primary: bool = False
    image_models: List[str] = []
    chat_models: List[str] = []
    video_models: List[str] = []
    ms_loras: List[Dict[str, Any]] = []
    ms_defaults_version: int = 0
    volcengine_project_name: str = ""
    volcengine_region: str = ""
    api_key: Optional[str] = None

class ChatRequest(BaseModel):
    conversation_id: str = ""
    message: str = Field(min_length=1, max_length=LLM_MESSAGE_MAX_LENGTH)
    model: str = ""
    image_model: str = ""
    mode: str = "chat"
    size: str = "1024x1024"
    quality: str = "auto"
    reference_images: List[AIReference] = []
    provider: str = "comfly"
    ms_model: str = ""

class MsGenerateRequest(BaseModel):
    prompt: str
    api_key: str = ""
    model: str = "black-forest-labs/FLUX.2-klein-9B"
    image_urls: List[str] = []
    width: int = 0
    height: int = 0
    size: str = ""
    loras: Optional[Any] = None
    client_id: Optional[str] = None

class CanvasLLMRequest(BaseModel):
    message: str = Field(min_length=1, max_length=LLM_MESSAGE_MAX_LENGTH)
    system_prompt: str = "You are a helpful assistant."
    model: str = ""
    messages: List[Dict[str, Any]] = []
    provider: str = "comfly"
    ms_model: str = ""
    images: List[str] = []   # 可以是 /output/*.png、/assets/*.png 本地路径 或 http(s) URL 或 data URL

class ConversationCreateRequest(BaseModel):
    title: str = "新对话"

class CanvasCreateRequest(BaseModel):
    title: str = "未命名画布"
    icon: str = "🧩"
    kind: str = "classic"
    project_id: str = ""

class CanvasSaveRequest(BaseModel):
    title: str = "未命名画布"
    icon: str = "🧩"
    nodes: List[Dict[str, Any]] = []
    connections: List[Dict[str, Any]] = []
    viewport: Dict[str, Any] = {}
    logs: List[Dict[str, Any]] = []
    generationHistory: List[Dict[str, Any]] = []
    settings: Dict[str, Any] = {}
    client_id: str = ""
    base_updated_at: int = 0

class CanvasAssetCheckRequest(BaseModel):
    urls: List[str] = []

class CanvasAssetDownloadRequest(BaseModel):
    urls: List[str] = []
    filename: str = "canvas-output-assets.zip"

# --- 资源库模型 ---

class LibrarySourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    type: str = "local"
    path: str = ""
    url: str = ""
    api_key: str = ""

class LibrarySourceUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    path: Optional[str] = None
    url: Optional[str] = None
    api_key: Optional[str] = None

class LibraryImageUpdate(BaseModel):
    categories: Optional[List[str]] = None
    manual_tags: Optional[List[str]] = None
    favorited: Optional[bool] = None
    notes: Optional[str] = None

class LibraryImageCopyRequest(BaseModel):
    target_scope: str = "project"
    project_id: str = ""

class AssetFeedbackRequest(BaseModel):
    project_id: str
    event_type: str
    context: Dict[str, Any] = Field(default_factory=dict)

class SkillCandidateReviewRequest(BaseModel):
    status: str

class LibraryTagRequest(BaseModel):
    image_ids: List[str]
    provider: str = ""
    model: str = ""

class LibraryCategoryUpdate(BaseModel):
    custom: List[str]

class PromptLibraryRequest(BaseModel):
    name: str = Field(default="提示词库", min_length=1, max_length=120)

class PromptLibraryItemRequest(BaseModel):
    library_id: str = "system"
    item_id: str = ""
    name: str = Field(default="提示词", min_length=1, max_length=120)
    category: str = "custom"
    positive: str = ""
    negative: str = ""
    scene: str = ""

class PromptLibraryBatchDeleteRequest(BaseModel):
    ids: List[str] = []

class PromptLibraryCategoryRequest(BaseModel):
    name: str = Field(default="新分组", min_length=1, max_length=80)
    library_id: str = "system"

class WikiSourceRequest(BaseModel):
    title: str = Field(default="未命名来源", min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=80000)
    source_type: str = "note"
    url: str = ""
    tags: List[str] = []

class WikiCompileRequest(BaseModel):
    source_id: str = ""
    title: str = ""
    content: str = ""
    source_type: str = "note"
    url: str = ""
    tags: List[str] = []

class WikiQARequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    page_type: str = ""
    save: bool = True

class LocalWikiInitRequest(BaseModel):
    base_dir: str = Field(min_length=1, max_length=2000)
    name: str = Field(default="AgentWiki", min_length=1, max_length=120)
    local_only: bool = False
    language: str = "zh-CN"
    allow_existing_initialized: bool = True

class LocalWikiImportPathRequest(BaseModel):
    root: str = Field(min_length=1, max_length=2000)
    source_path: str = Field(min_length=1, max_length=2000)
    category: str = "上传文件"
    max_files: int = 200


class LocalWikiCompileRequest(BaseModel):
    root: str = Field(min_length=1, max_length=2000)
    source_id: str = ""
    force: bool = False
    use_llm: bool = True
    provider: str = ""
    model: str = ""


class LocalWikiQARequest(BaseModel):
    root: str = Field(min_length=1, max_length=2000)
    question: str = Field(min_length=1, max_length=8000)
    page_type: str = ""
    save: bool = True
    use_llm: bool = True
    provider: str = ""
    model: str = ""


class AgentPlanRequest(BaseModel):
    goal: str = ""
    page: str = "home"
    context: Dict[str, Any] = Field(default_factory=dict)


class AgentRunRequest(BaseModel):
    task_id: str
    context_overrides: Dict[str, Any] = Field(default_factory=dict)
    confirmation_token: str = ""

class LibraryImportRequest(BaseModel):
    urls: List[str] = []
    items: List[Dict[str, Any]] = []
    source_name: str = "智能画布"
    canvas_id: str = ""
    canvas_title: str = ""
    node_id: str = ""
    categories: List[str] = []
    manual_tags: List[str] = []
    project_id: str = ""


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    code: str = Field(default="", max_length=40)

class ProjectContextCompileRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    reference_images: List[AIReference] = []
    local_wiki_root: str = ""


class PptSlotAssignRequest(BaseModel):
    asset_id: str = Field(min_length=1, max_length=180)


class PptImageObjectAssignRequest(BaseModel):
    asset_id: str = Field(min_length=1, max_length=180)
    apply_scope: str = "object"


class PptTextObjectUpdateRequest(BaseModel):
    text: str = Field(default="", max_length=20000)
    revision: int = 0

# --- 负载均衡 ---

def check_images_exist(backend_addr, images):
    if not images: return True
    for img in images:
        try:
            url = f"http://{backend_addr}/view?filename={urllib.parse.quote(img)}&type=input"
            r = requests.get(url, stream=True, timeout=0.5)
            r.close()
            if r.status_code != 200: return False
        except: return False
    return True

def get_best_backend(required_images: List[str] = None):
    best_backend = COMFYUI_INSTANCES[0]
    min_queue_size = float('inf')
    candidates_with_images = []
    candidates_others = []
    backend_stats = {}

    for addr in COMFYUI_INSTANCES:
        try:
            with urllib.request.urlopen(f"http://{addr}/queue", timeout=1) as response:
                data = json.loads(response.read())
                remote_load = len(data.get('queue_running', [])) + len(data.get('queue_pending', []))
                with LOAD_LOCK:
                    local_load = BACKEND_LOCAL_LOAD.get(addr, 0)
                effective_load = max(remote_load, local_load)
                has_images = check_images_exist(addr, required_images)
                backend_stats[addr] = {"load": effective_load, "has_images": has_images}
                if has_images:
                    candidates_with_images.append(addr)
                else:
                    candidates_others.append(addr)
        except Exception as e:
            print(f"Backend {addr} unreachable: {e}")
            continue

    target_candidates = candidates_with_images if candidates_with_images else candidates_others
    if not target_candidates:
        if candidates_others:
            target_candidates = candidates_others
        else:
            return COMFYUI_INSTANCES[0]

    for addr in target_candidates:
        load = backend_stats[addr]["load"]
        if load < min_queue_size:
            min_queue_size = load
            best_backend = addr

    return best_backend

# --- 辅助工具 ---

def download_image(comfy_address, comfy_url_path, prefix="studio_"):
    filename = f"{prefix}{uuid.uuid4().hex[:10]}.png"
    local_path = output_path_for(filename, "output")
    full_url = f"http://{comfy_address}{comfy_url_path}"
    try:
        with urllib.request.urlopen(full_url) as response, open(local_path, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)
        return output_url_for(filename, "output")
    except Exception as e:
        print(f"下载图片失败: {e}")
        if comfy_url_path.startswith("/view"):
            return comfy_url_path.replace("/view", "/api/view", 1)
        return full_url

def comfy_output_extension(item):
    filename = str((item or {}).get("filename") or "")
    ext = os.path.splitext(filename)[1].lower()
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".mov", ".m4v", ".gif"}:
        return ext
    fmt = str((item or {}).get("format") or "").lower()
    if "webm" in fmt:
        return ".webm"
    if "quicktime" in fmt or "mov" in fmt:
        return ".mov"
    if "mp4" in fmt or "h264" in fmt or "video" in fmt:
        return ".mp4"
    return ".png"

def is_video_output_item(item):
    ext = comfy_output_extension(item)
    fmt = str((item or {}).get("format") or "").lower()
    return ext in {".mp4", ".webm", ".mov", ".m4v"} or "video" in fmt

def download_comfy_output(comfy_address, item, prefix="studio_"):
    ext = comfy_output_extension(item)
    filename = f"{prefix}{uuid.uuid4().hex[:10]}{ext}"
    local_path = output_path_for(filename, "output")
    subfolder = urllib.parse.quote(str(item.get("subfolder") or ""))
    file_type = urllib.parse.quote(str(item.get("type") or "output"))
    comfy_url_path = f"/view?filename={urllib.parse.quote(str(item['filename']))}&subfolder={subfolder}&type={file_type}"
    full_url = f"http://{comfy_address}{comfy_url_path}"
    try:
        with urllib.request.urlopen(full_url) as response, open(local_path, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)
        return output_url_for(filename, "output")
    except Exception as e:
        print(f"下载 ComfyUI 输出失败: {e}")
        if comfy_url_path.startswith("/view"):
            return comfy_url_path.replace("/view", "/api/view", 1)
        return full_url

def save_to_history(record):
    with HISTORY_LOCK:
        history = []
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                    history = json.load(f)
            except: pass
        if "timestamp" not in record:
            record["timestamp"] = time.time()
        history.insert(0, record)
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history[:5000], f, ensure_ascii=False, indent=4)

def get_comfy_history(comfy_address, prompt_id):
    try:
        with urllib.request.urlopen(f"http://{comfy_address}/history/{prompt_id}") as response:
            return json.loads(response.read())
    except Exception as e:
        return {}

def safe_user_id(user_id, request: Request):
    candidate = (user_id or "").strip()
    if not candidate and request.client:
        candidate = f"ip-{request.client.host}"
    if not candidate:
        candidate = "anonymous"
    candidate = re.sub(r"[^a-zA-Z0-9_.-]", "-", candidate)[:80].strip(".-")
    return candidate or "anonymous"

def user_dir(user_id):
    path = os.path.join(CONVERSATION_DIR, user_id)
    os.makedirs(path, exist_ok=True)
    return path

def conversation_path(user_id, conversation_id):
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", conversation_id or "")
    if not cleaned:
        raise HTTPException(status_code=400, detail="无效的对话 ID")
    return os.path.join(user_dir(user_id), f"{cleaned}.json")

def now_ms():
    return int(time.time() * 1000)

def save_conversation(user_id, conversation):
    with CONVERSATION_LOCK:
        path = conversation_path(user_id, conversation["id"])
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(conversation, f, ensure_ascii=False, indent=2)

def new_conversation(user_id, title="新对话"):
    timestamp = now_ms()
    conversation = {
        "id": uuid.uuid4().hex,
        "title": (title or "新对话")[:80],
        "created_at": timestamp,
        "updated_at": timestamp,
        "messages": [],
    }
    save_conversation(user_id, conversation)
    return conversation

def load_conversation(user_id, conversation_id):
    path = conversation_path(user_id, conversation_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="对话不存在")
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def list_conversations(user_id):
    records = []
    for filename in os.listdir(user_dir(user_id)):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(user_dir(user_id), filename)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue
        messages = data.get("messages", [])
        last_message = next((m for m in reversed(messages) if m.get("role") != "system"), None)
        records.append({
            "id": data.get("id"),
            "title": data.get("title", "新对话"),
            "created_at": data.get("created_at", 0),
            "updated_at": data.get("updated_at", 0),
            "last_message": (last_message or {}).get("content", ""),
        })
    return sorted(records, key=lambda item: item["updated_at"], reverse=True)

def canvas_path(canvas_id):
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", canvas_id or "")
    if not cleaned:
        raise HTTPException(status_code=400, detail="无效的画布 ID")
    return os.path.join(CANVAS_DIR, f"{cleaned}.json")

def save_canvas(canvas):
    canvas["updated_at"] = now_ms()
    with CANVAS_LOCK:
        with open(canvas_path(canvas["id"]), 'w', encoding='utf-8') as f:
            json.dump(canvas, f, ensure_ascii=False, indent=2)
    DOMAIN_STORE.upsert_canvas(canvas, canvas.get("project_id") or "")

def normalize_canvas_kind(kind="classic"):
    return "smart" if str(kind or "").strip().lower() == "smart" else "classic"

def new_canvas(title="未命名画布", icon="layers", kind="classic", project_id=""):
    timestamp = now_ms()
    canvas_kind = normalize_canvas_kind(kind)
    project = DOMAIN_STORE.get_project(project_id) if project_id else None
    project_id = (project or DOMAIN_STORE.ensure_default_project())["id"]
    canvas = {
        "id": uuid.uuid4().hex,
        "project_id": project_id,
        "title": (title or ("智能画布" if canvas_kind == "smart" else "未命名画布"))[:80],
        "icon": (icon or ("sparkles" if canvas_kind == "smart" else "🧩"))[:32],
        "kind": canvas_kind,
        "created_at": timestamp,
        "updated_at": timestamp,
        "nodes": [],
        "connections": [],
        "viewport": {"x": 0, "y": 0, "scale": 1},
        "settings": {},
    }
    save_canvas(canvas)
    DOMAIN_STORE.save_canvas_snapshot(canvas, project_id)
    return canvas


def sync_legacy_domain_records():
    project_id = DOMAIN_STORE.ensure_default_project()["id"]
    for filename in os.listdir(CANVAS_DIR):
        if not filename.endswith(".json"):
            continue
        try:
            with open(os.path.join(CANVAS_DIR, filename), "r", encoding="utf-8") as handle:
                canvas = json.load(handle)
            DOMAIN_STORE.upsert_canvas(canvas, canvas.get("project_id") or project_id)
        except Exception as exc:
            print(f"[domain] skipped legacy canvas {filename}: {exc}")
    for image in load_library_images():
        if image.get("scope") == "shared":
            continue
        url = str(image.get("url") or "")
        if not url:
            continue
        try:
            image_project_id = str(image.get("project_id") or project_id)
            if not DOMAIN_STORE.get_project(image_project_id):
                image_project_id = project_id
            DOMAIN_STORE.register_asset(
                image_project_id,
                url,
                asset_id=str(image.get("asset_id") or image.get("id") or ""),
                title=str(image.get("filename") or ""),
                source="legacy_library",
                width=int(image.get("width") or 0),
                height=int(image.get("height") or 0),
                byte_size=int(image.get("size_bytes") or 0),
                metadata={"library_image_id": image.get("id")},
            )
        except Exception as exc:
            print(f"[domain] skipped legacy asset {image.get('id')}: {exc}")


def smart_canvas_node_image_from_library_image(img):
    return {
        "url": img.get("url", ""),
        "asset_id": img.get("asset_id") or (img.get("id") if img.get("scope") == "project" else "") or "",
        "name": img.get("filename") or "library-image",
        "width": int(img.get("width") or 0),
        "height": int(img.get("height") or 0),
        "natural_w": int(img.get("width") or 0),
        "natural_h": int(img.get("height") or 0),
        "thumb_url": img.get("thumb_url") or "",
        "library_image_id": img.get("id") or "",
        "library_source_id": img.get("source_id") or "",
        "library_source_name": img.get("source_name") or img.get("source_id") or "",
        "library_categories": (img.get("categories") or [])[:8],
        "library_tags": (img.get("tags") or [])[:24],
    }


def create_smart_canvas_image_node(images: List[Dict[str, Any]], x: int = 120, y: int = 120):
    node_images = [smart_canvas_node_image_from_library_image(img) for img in images if img.get("url")]
    return {
        "id": f"smart_{uuid.uuid4().hex[:12]}",
        "type": "smart-image",
        "x": x,
        "y": y,
        "title": "Group" if len(node_images) > 1 else "Image",
        "images": node_images,
        "created_at": now_ms(),
    }


def library_agent_target_source_label(context: Dict[str, Any], source_mode: str) -> str:
    if source_mode == "selected":
        return "当前选中"
    if source_mode == "visible":
        return "当前可见"
    has_filter = any([
        str(context.get("source_id") or "").strip(),
        str(context.get("category") or "").strip(),
        str(context.get("query") or "").strip(),
        bool(context.get("filter_favorited")),
        bool(context.get("filter_untagged")),
    ])
    return "当前筛选" if has_filter else "全部资源库"


def project_domain_assets_for_library(project_id: str) -> List[Dict[str, Any]]:
    project_id = str(project_id or "").strip()
    if not project_id or not DOMAIN_STORE.get_project(project_id):
        return []
    workspace = DOMAIN_STORE.project_workspace(project_id, limit=100)
    items = []
    for asset in workspace.get("recent_assets") or []:
        url = str(asset.get("storage_url") or "").strip()
        if not url:
            continue
        try:
            metadata = json.loads(asset.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        source = str(asset.get("source") or "")
        if source == "generation_input":
            categories = ["项目素材", "参考图"]
        elif source == "generation_output":
            categories = ["项目素材", "生成结果"]
        else:
            categories = ["项目素材"]
        items.append({
            "id": str(metadata.get("library_image_id") or asset.get("id") or ""),
            "asset_id": str(asset.get("id") or ""),
            "project_id": project_id,
            "scope": "project",
            "source_id": "project-assets",
            "source_name": "项目资产",
            "filename": str(asset.get("title") or os.path.basename(urllib.parse.urlparse(url).path) or "project-asset"),
            "url": url,
            "thumb_url": url,
            "width": int(asset.get("width") or 0),
            "height": int(asset.get("height") or 0),
            "size_bytes": int(asset.get("byte_size") or 0),
            "categories": categories,
            "tags": [source] if source else [],
            "manual_tags": [],
            "notes": f"项目资产 · {source}" if source else "项目资产",
            "created_at": int(asset.get("created_at") or 0),
            "updated_at": int(asset.get("updated_at") or 0),
            "domain_only": not bool(metadata.get("library_image_id")),
        })
    return items


def resolve_library_agent_matches(context: Dict[str, Any], limit: int, enrich: bool = False):
    project_id = resolve_project_id(
        str(context.get("project_id") or ""),
        str(context.get("canvas_id") or ""),
    )
    source_id = str(context.get("source_id") or "").strip()
    category = str(context.get("category") or "").strip()
    q = str(context.get("query") or "").strip().lower()
    favorited = bool(context.get("filter_favorited"))
    untagged = bool(context.get("filter_untagged"))
    selected_image_ids = [str(x).strip() for x in (context.get("selected_image_ids") or []) if str(x).strip()]
    visible_image_ids = [str(x).strip() for x in (context.get("visible_image_ids") or []) if str(x).strip()]

    images = filter_library_images_by_scope(load_library_images(), "available", project_id)
    known_urls = {str(image.get("url") or "") for image in images}
    images.extend(
        image for image in project_domain_assets_for_library(project_id)
        if str(image.get("url") or "") not in known_urls
    )
    source_map = {str(s.get("id") or ""): s for s in load_library_sources()}
    result = images[:]
    source_mode = "filtered"
    if selected_image_ids:
        selected_set = set(selected_image_ids)
        result = [img for img in result if str(img.get("id") or "") in selected_set]
        source_mode = "selected"
    elif visible_image_ids:
        visible_set = set(visible_image_ids)
        result = [img for img in result if str(img.get("id") or "") in visible_set]
        source_mode = "visible"
    elif source_id:
        result = [img for img in result if img.get("source_id") == source_id]
    if category:
        result = [img for img in result if category in (img.get("categories") or [])]
    if q:
        def match_q(img):
            searchable = " ".join([
                img.get("filename", ""),
                " ".join(img.get("categories", [])),
                " ".join(img.get("tags", [])),
                " ".join(img.get("ai_tags", [])),
                " ".join(img.get("manual_tags", [])),
                img.get("notes", ""),
            ]).lower()
            query_tokens = [token for token in re.split(r"\s+", q) if token]
            return q in searchable or any(token in searchable for token in query_tokens)
        result = [img for img in result if match_q(img)]
    if favorited:
        result = [img for img in result if library_image_feedback(img, project_id)[0].get("favorited")]
    if untagged:
        result = [img for img in result if not img.get("ai_tagged")]
    if not selected_image_ids and not visible_image_ids:
        result.sort(
            key=lambda img: (
                int(library_image_feedback(img, project_id)[0].get("score") or 0),
                int(img.get("updated_at") or img.get("created_at") or 0),
            ),
            reverse=True,
        )
    limited = result[:limit]
    items = [enrich_library_image_record(img, source_map, project_id) for img in limited] if enrich else limited
    matched_total = len(result)
    return {
        "items": items,
        "matched_total": matched_total,
        "effective_count": len(limited),
        "limit": limit,
        "truncated": matched_total > limit,
        "target_source": source_mode,
        "target_source_label": library_agent_target_source_label(context, source_mode),
    }


def filter_library_images_for_agent(context: Dict[str, Any], limit: int = AGENT_SMART_CANVAS_LIMIT):
    resolved = resolve_library_agent_matches(context, limit=limit, enrich=True)
    return resolved["items"], resolved["matched_total"]


def library_tag_targets_for_agent(context: Dict[str, Any], limit: int = AGENT_LIBRARY_TAG_LIMIT):
    resolved = resolve_library_agent_matches(context, limit=max(limit * 3, 100), enrich=False)
    return [item for item in resolved["items"] if not item.get("domain_only")][:limit]


def build_agent_plan_preview(page_role: str, tool_ids: List[str], context: Dict[str, Any]) -> Dict[str, Any]:
    context = context or {}
    goal = str(context.get("goal") or context.get("query") or "").strip()
    if "code_agent_placeholder" in tool_ids:
        return {
            "target_source": "code",
            "target_source_label": "代码智能体入口",
            "matched_total": 0,
            "effective_count": 0,
            "limit": 0,
            "truncated": False,
            "write_target": "none",
            "write_target_label": "暂未接入",
            "can_run": False,
            "blockers": ["代码模式暂未接入 Pi Coding Agent；当前只保留入口和任务记录。"],
            "warnings": [],
        }

    if page_role == "library" and "create_smart_canvas" in tool_ids and "append_images_to_smart_canvas" in tool_ids:
        resolved = resolve_library_agent_matches(context, limit=AGENT_SMART_CANVAS_LIMIT, enrich=True)
        preview = {
            "target_source": resolved["target_source"],
            "target_source_label": resolved["target_source_label"],
            "matched_total": resolved["matched_total"],
            "effective_count": resolved["effective_count"],
            "limit": resolved["limit"],
            "truncated": resolved["truncated"],
            "write_target": "new_smart_canvas",
            "write_target_label": "新智能画布",
            "can_run": resolved["effective_count"] > 0,
            "blockers": [],
            "warnings": [],
        }
        if preview["effective_count"] <= 0:
            preview["blockers"].append("当前资源库上下文里没有可送入智能画布的图片。")
        if preview["truncated"]:
            preview["warnings"].append(f"命中 {preview['matched_total']} 张，只会送入前 {preview['limit']} 张。")
        return preview

    if page_role == "library" and "tag_library_images" in tool_ids:
        resolved = resolve_library_agent_matches(context, limit=AGENT_LIBRARY_TAG_LIMIT, enrich=False)
        provider = str(context.get("provider") or "").strip()
        model = str(context.get("model") or "").strip()
        preview = {
            "target_source": resolved["target_source"],
            "target_source_label": resolved["target_source_label"],
            "matched_total": resolved["matched_total"],
            "effective_count": resolved["effective_count"],
            "limit": resolved["limit"],
            "truncated": resolved["truncated"],
            "write_target": "library_tags",
            "write_target_label": "资源库标签与分类",
            "provider": provider,
            "model": model,
            "can_run": resolved["effective_count"] > 0 and bool(model),
            "blockers": [],
            "warnings": [],
        }
        if preview["effective_count"] <= 0:
            preview["blockers"].append("当前资源库上下文里没有可用于批量标注的图片。")
        if not model:
            preview["blockers"].append("请先选择用于批量标注的 Provider 和模型。")
        if preview["truncated"]:
            preview["warnings"].append(f"命中 {preview['matched_total']} 张，只会标注前 {preview['limit']} 张。")
        return preview

    if page_role == "smart-canvas" and "save_canvas_node_images_to_library" in tool_ids:
        selected_urls = [str(x).strip() for x in (context.get("selected_image_urls") or []) if str(x).strip()]
        count = len(selected_urls)
        preview = {
            "target_source": "selected",
            "target_source_label": "当前选中节点图片",
            "matched_total": count,
            "effective_count": count,
            "limit": count,
            "truncated": False,
            "write_target": "library",
            "write_target_label": "资源库",
            "can_run": count > 0,
            "blockers": [],
            "warnings": [],
        }
        if count <= 0:
            preview["blockers"].append("当前智能画布没有选中可入库的图片。")
        return preview

    if "search_wiki_context" in tool_ids:
        wiki_matches = agent_wiki_search_items(goal, context, limit=AGENT_WIKI_CONTEXT_LIMIT)
        local_root = agent_local_wiki_root(context)
        library_resolved = resolve_library_agent_matches(context, limit=AGENT_SMART_CANVAS_LIMIT, enrich=False) if "list_library_images" in tool_ids else None
        matched_total = len(wiki_matches) + int((library_resolved or {}).get("matched_total") or 0)
        effective_count = len(wiki_matches) + int((library_resolved or {}).get("effective_count") or 0)
        write_target = "wiki_outputs"
        write_label = "本地知识库输出" if local_root else "Wiki 输出"
        if "generate_design_image" in tool_ids:
            write_target = "design_outputs"
            write_label = "设计简报 / 资源库 / 智能画布"
        elif "write_wiki_qa" in tool_ids:
            write_target = "wiki_qa"
            write_label = "问答档案"
        elif "write_agent_report" in tool_ids:
            write_target = "wiki_report"
            write_label = "工作报告"
        return {
            "target_source": "local_wiki" if local_root else "wiki",
            "target_source_label": "本地知识库 + 当前上下文" if local_root else "LLM Wiki + 当前上下文",
            "matched_total": matched_total,
            "effective_count": effective_count,
            "limit": AGENT_WIKI_CONTEXT_LIMIT,
            "truncated": len(wiki_matches) >= AGENT_WIKI_CONTEXT_LIMIT,
            "write_target": write_target,
            "write_target_label": write_label,
            "wiki_match_count": len(wiki_matches),
            "library_match_count": int((library_resolved or {}).get("effective_count") or 0),
            "can_run": True,
            "blockers": [],
            "warnings": [] if wiki_matches else ["当前 Wiki 暂无命中，Agent 会以当前目标生成首版产物。"],
        }

    return {
        "target_source": "context",
        "target_source_label": "当前上下文",
        "matched_total": 0,
        "effective_count": 0,
        "limit": 0,
        "truncated": False,
        "write_target": "none",
        "write_target_label": "无写入",
        "can_run": True,
        "blockers": [],
        "warnings": [],
    }


def merge_agent_context(task: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(task.get("context") or {})
    for key, value in (overrides or {}).items():
        merged[key] = value
    return merged


def decorate_agent_task_preview(task_dir: str, task: Dict[str, Any]) -> Dict[str, Any]:
    plan = task.get("plan") or {}
    preview = build_agent_plan_preview(
        str(plan.get("page_role") or task.get("page_role") or ""),
        list(plan.get("tool_ids") or []),
        task.get("context") or {},
    )
    return agent_update_task_plan(
        task_dir,
        task["id"],
        preview=preview,
        blockers=preview.get("blockers") or [],
        can_run=bool(preview.get("can_run", True)),
    )


def import_urls_into_library(
    urls: List[str],
    source_name: str = "智能画布",
    canvas_id: str = "",
    canvas_title: str = "",
    node_id: str = "",
    manual_tags: Optional[List[str]] = None,
    categories: Optional[List[str]] = None,
    items: Optional[List[Dict[str, Any]]] = None,
    project_id: str = "",
):
    raw_items = [item for item in (items or []) if isinstance(item, dict)]
    import_items = []
    if raw_items:
        for item in raw_items[:200]:
            url = str(item.get("url") or "").strip()
            if url:
                import_items.append({**item, "url": url})
    else:
        import_items = [{"url": str(url or "").strip()} for url in urls[:200] if str(url or "").strip()]
    if not import_items:
        raise HTTPException(status_code=400, detail="没有可导入的素材")

    source_slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", (source_name or "smart-canvas").strip().lower()).strip("-") or "smart-canvas"
    source_id = f"smart-{source_slug}"[:48]
    source_name = (source_name or "智能画布").strip()[:80] or "智能画布"
    source_folder = os.path.join(LIBRARY_DIR, source_id, "files")
    os.makedirs(source_folder, exist_ok=True)
    os.makedirs(os.path.join(LIBRARY_DIR, source_id, "thumbs"), exist_ok=True)

    sources = load_library_sources()
    src = next((s for s in sources if s.get("id") == source_id), None)
    if not src:
        src = {
            "id": source_id,
            "name": source_name,
            "type": "local",
            "path": source_folder,
            "enabled": True,
            "created_at": now_ms(),
            "last_scan_at": 0,
            "managed_by": "smart-canvas",
        }
        sources.insert(0, src)
    else:
        src["name"] = source_name
        src["type"] = "local"
        src["path"] = source_folder
        src["enabled"] = True

    images = load_library_images()
    categories = [str(x).strip() for x in (categories or []) if str(x).strip()][:8]
    manual_tags = [str(x).strip() for x in (manual_tags or []) if str(x).strip()][:24]
    imported = []
    skipped = []
    domain_project_id = resolve_project_id(project_id, canvas_id)

    for index, item in enumerate(import_items, start=1):
        url = str(item.get("url") or "").strip()
        path = local_asset_path_from_url(url)
        if not path or not os.path.isfile(path):
            skipped.append({"url": url, "reason": "missing"})
            continue
        mime = content_type_for_path(path)
        if not mime.startswith("image/"):
            skipped.append({"url": url, "reason": "not_image"})
            continue
        base = os.path.basename(path) or f"image-{index}.png"
        stem, ext = os.path.splitext(base)
        ext = ext or ".png"
        archive_name = f"{re.sub(r'[^a-zA-Z0-9._-]+', '_', stem)[:80]}{ext.lower()}"
        target_path = os.path.join(source_folder, archive_name)
        suffix = 2
        while os.path.exists(target_path):
            target_path = os.path.join(source_folder, f"{re.sub(r'[^a-zA-Z0-9._-]+', '_', stem)[:72]}-{suffix}{ext.lower()}")
            suffix += 1
        shutil.copy2(path, target_path)
        thumb_name, w, h = create_library_thumb_from_path(target_path, source_id)
        filename = os.path.basename(target_path)
        operation = str(item.get("operation") or "").strip()
        operation_label = str(item.get("operation_label") or item.get("operationLabel") or operation).strip()
        material_name = str(item.get("material_name") or item.get("materialName") or "").strip()
        material_target = str(item.get("material_target") or item.get("materialTarget") or "").strip()
        material_target_label = str(item.get("material_target_label") or item.get("materialTargetLabel") or material_target).strip()
        selection_node_id = str(item.get("selection_node_id") or item.get("selectionNodeId") or "").strip()
        material_node_id = str(item.get("material_node_id") or item.get("materialNodeId") or "").strip()
        target_node_id = str(item.get("target_node_id") or item.get("targetNodeId") or item.get("source_node_id") or item.get("sourceNodeId") or "").strip()
        item_node_id = str(item.get("node_id") or item.get("nodeId") or node_id or "").strip()
        input_node_ids = [str(x).strip() for x in (item.get("input_node_ids") or item.get("inputNodeIds") or []) if str(x).strip()][:24]
        selection = item.get("selection") if isinstance(item.get("selection"), dict) else None
        run_prompt = str(item.get("run_prompt") or item.get("runPrompt") or "").strip()
        try:
            run_at = int(item.get("run_at") or item.get("runAt") or 0)
        except (TypeError, ValueError):
            run_at = 0
        item_tags = [
            *(manual_tags or []),
            operation_label if operation else "",
            "材质替换" if operation == "swap-material" else "",
            material_name,
            material_target_label,
            "局部选区" if selection_node_id or selection else "",
        ]
        item_tags = list(dict.fromkeys([str(x).strip() for x in item_tags if str(x).strip()]))[:24]
        item_categories = [
            *(categories or []),
            "材质替换" if operation == "swap-material" else "",
        ]
        item_categories = list(dict.fromkeys([str(x).strip() for x in item_categories if str(x).strip()]))[:8]
        note_lines = [
            f"来自智能画布：{canvas_title}" if canvas_title else "",
            f"画布ID：{canvas_id}" if canvas_id else "",
            f"节点ID：{item_node_id}" if item_node_id else "",
            f"操作：{operation_label or operation}" if operation else "",
            f"材质：{material_name}" if material_name else "",
            f"目标面：{material_target_label or material_target}" if material_target else "",
            f"材质节点：{material_node_id}" if material_node_id else "",
            f"目标节点：{target_node_id}" if target_node_id else "",
            f"Selection节点：{selection_node_id}" if selection_node_id else "",
            f"Selection：{json.dumps(selection, ensure_ascii=False)}" if selection else "",
            f"输入节点：{', '.join(input_node_ids)}" if input_node_ids else "",
            f"提示词：{run_prompt[:800]}" if run_prompt else "",
            f"原始路径：{url}",
        ]
        record = {
            "id": f"img_{uuid.uuid4().hex[:12]}",
            "source_id": source_id,
            "source_name": source_name,
            "source_canvas_id": canvas_id,
            "source_canvas_title": canvas_title,
            "source_node_id": item_node_id,
            "source_target_node_id": target_node_id,
            "source_material_node_id": material_node_id,
            "source_selection_node_id": selection_node_id,
            "source_input_node_ids": input_node_ids,
            "source_operation": operation,
            "source_operation_label": operation_label,
            "source_url": url,
            "source_run_prompt": run_prompt,
            "source_run_at": run_at,
            "source_selection": selection,
            "material_name": material_name,
            "material_target": material_target,
            "material_target_label": material_target_label,
            "filename": filename,
            "local_path": target_path,
            "url": f"/api/library/file/{source_id}/{urllib.parse.quote(filename)}",
            "thumb_url": f"/api/library/thumb/{source_id}/{thumb_name}",
            "width": w,
            "height": h,
            "size_bytes": os.path.getsize(target_path),
            "categories": item_categories,
            "tags": item_tags[:],
            "ai_tags": [],
            "ai_tagged": False,
            "ai_tag_model": "",
            "manual_tags": item_tags[:],
            "favorited": False,
            "scope": "project",
            "project_id": domain_project_id,
            "notes": "\n".join([line for line in note_lines if line]),
            "created_at": now_ms(),
            "updated_at": now_ms(),
        }
        existing_asset = DOMAIN_STORE.asset_by_url(url, domain_project_id)
        requested_asset_id = str(item.get("asset_id") or item.get("assetId") or "")
        if existing_asset:
            asset_id = str(existing_asset["id"])
        else:
            source_asset = DOMAIN_STORE.register_asset(
                domain_project_id,
                url,
                asset_id=requested_asset_id,
                title=filename,
                source="canvas_output" if canvas_id else "library_import",
                width=w,
                height=h,
                byte_size=os.path.getsize(target_path),
                metadata={"canvas_id": canvas_id, "node_id": item_node_id},
            )
            asset_id = str(source_asset["id"])
        DOMAIN_STORE.register_asset(
            domain_project_id,
            record["url"],
            asset_id=asset_id,
            title=filename,
            source="library",
            width=w,
            height=h,
            byte_size=record["size_bytes"],
            metadata={"library_image_id": record["id"], "canvas_id": canvas_id, "node_id": item_node_id},
        )
        record["asset_id"] = asset_id
        DOMAIN_STORE.record_preference_event(
            domain_project_id,
            asset_id,
            "saved_to_library",
            {"library_image_id": record["id"], "canvas_id": canvas_id, "node_id": item_node_id},
        )
        images.append(record)
        imported.append(record)

    src["last_scan_at"] = now_ms()
    save_library_sources(sources)
    save_library_images(images)
    return {"imported": imported, "count": len(imported), "skipped": skipped, "source_id": source_id}

def load_canvas(canvas_id):
    path = canvas_path(canvas_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="画布不存在")
    with open(path, 'r', encoding='utf-8') as f:
        canvas = json.load(f)
    canvas["kind"] = normalize_canvas_kind(canvas.get("kind"))
    canvas["settings"] = canvas.get("settings") or {}
    canvas["project_id"] = canvas.get("project_id") or resolve_project_id(canvas_id=canvas_id)
    if canvas.get("deleted_at"):
        raise HTTPException(status_code=404, detail="画布已在回收站")
    return canvas

def load_canvas_any(canvas_id):
    path = canvas_path(canvas_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="画布不存在")
    with open(path, 'r', encoding='utf-8') as f:
        canvas = json.load(f)
    canvas["kind"] = normalize_canvas_kind(canvas.get("kind"))
    canvas["settings"] = canvas.get("settings") or {}
    canvas["project_id"] = canvas.get("project_id") or resolve_project_id(canvas_id=canvas_id)
    return canvas

def canvas_record(data):
    return {
        "id": data.get("id"),
        "project_id": data.get("project_id") or resolve_project_id(canvas_id=str(data.get("id") or "")),
        "title": data.get("title", "未命名画布"),
        "icon": data.get("icon", "🧩"),
        "kind": normalize_canvas_kind(data.get("kind")),
        "created_at": data.get("created_at", 0),
        "updated_at": data.get("updated_at", 0),
        "deleted_at": data.get("deleted_at", 0),
        "node_count": len(data.get("nodes", [])),
    }

def cleanup_expired_canvas_trash():
    cutoff = now_ms() - CANVAS_TRASH_RETENTION_MS
    with CANVAS_LOCK:
        for filename in os.listdir(CANVAS_DIR):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(CANVAS_DIR, filename)
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                deleted_at = int(data.get("deleted_at") or 0)
                if deleted_at and deleted_at < cutoff:
                    os.remove(path)
            except Exception:
                continue

def iter_canvas_records(include_deleted=False):
    cleanup_expired_canvas_trash()
    records = []
    for filename in os.listdir(CANVAS_DIR):
        if not filename.endswith(".json"):
            continue
        try:
            with open(os.path.join(CANVAS_DIR, filename), 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue
        is_deleted = bool(data.get("deleted_at"))
        if include_deleted != is_deleted:
            continue
        records.append(canvas_record(data))
    return records

def list_canvases(project_id: str = ""):
    records = iter_canvas_records(include_deleted=False)
    if project_id:
        records = [item for item in records if item.get("project_id") == project_id]
    return sorted(records, key=lambda item: item["updated_at"], reverse=True)

def list_deleted_canvases(project_id: str = ""):
    records = iter_canvas_records(include_deleted=True)
    if project_id:
        records = [item for item in records if item.get("project_id") == project_id]
    return sorted(records, key=lambda item: item["deleted_at"], reverse=True)

def display_title(text):
    title = re.sub(r"\s+", " ", text or "").strip()
    return title[:24] or "新对话"

def resolve_chat_provider(provider: str, model: str, ms_model: str):
    if provider == "modelscope":
        if not MODELSCOPE_API_KEY:
            raise HTTPException(status_code=400, detail="未配置 MODELSCOPE_API_KEY，请在 API/.env 中填写。")
        base = MODELSCOPE_CHAT_BASE_URL
        hdrs = {"Authorization": f"Bearer {MODELSCOPE_API_KEY}", "Content-Type": "application/json"}
        mdl = selected_model(ms_model or model, MODELSCOPE_CHAT_MODELS[0] if MODELSCOPE_CHAT_MODELS else "MiniMax/MiniMax-M2.7")
        return base, hdrs, mdl
    api_provider = get_api_provider(provider or "")
    base_root = (api_provider.get("base_url") or AI_BASE_URL).rstrip("/")
    if not base_root:
        raise HTTPException(status_code=400, detail=f"{api_provider.get('name') or api_provider['id']} 未配置 Base URL")
    base = base_root if base_root.endswith("/v1") else base_root + "/v1"
    hdrs = api_headers(provider=api_provider)
    default_model = (api_provider.get("chat_models") or [CHAT_MODEL])[0]
    mdl = selected_model(model, default_model)
    return base, hdrs, mdl

def api_headers(json_body=True, provider=None):
    if provider:
        key_env = provider_key_env(provider["id"])
        api_key = os.getenv(key_env, "")
        provider_name = provider.get("name") or provider["id"]
        if not api_key:
            raise HTTPException(status_code=400, detail=f"未配置 {provider_name} 的 API Key，请在 API 平台管理中填写。")
    else:
        api_key = AI_API_KEY
        if not api_key:
            raise HTTPException(status_code=400, detail="未配置 COMFLY_API_KEY，请在 API/.env 中填写。")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers

def selected_model(requested, fallback):
    model = (requested or fallback).strip()
    if not model:
        raise HTTPException(status_code=400, detail="模型名称不能为空")
    if len(model) > 120 or not re.fullmatch(r"[a-zA-Z0-9_.:/+-]+", model):
        raise HTTPException(status_code=400, detail=f"模型名称不合法：{model}")
    return model

def modelscope_size(value, fallback="1024x1024"):
    size = str(value or fallback).strip().lower().replace("*", "x")
    if re.fullmatch(r"\d{2,5}x\d{2,5}", size):
        return size
    raise HTTPException(status_code=400, detail=f"ModelScope size 格式不正确：{value or fallback}，应为 WxH，例如 1024x1024")

def unwrap_apimart_response(raw):
    """APIMart 将标准 OpenAI 响应包在 {"code":200,"data":{...}} 里；如果检测到就解包。"""
    if isinstance(raw, dict) and "data" in raw and isinstance(raw.get("data"), dict) and "choices" not in raw:
        return raw["data"]
    return raw

def text_from_chat_response(data):
    data = unwrap_apimart_response(data)
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text") or item.get("content") or "")
        return "\n".join(part for part in parts if part)
    return str(content)

async def request_chat_completion(
    user_message: str,
    system_prompt: str = "",
    messages: Optional[List[Dict[str, Any]]] = None,
    provider: str = "",
    model: str = "",
    ms_model: str = "",
) -> Dict[str, Any]:
    chat_base, chat_hdrs, resolved_model = resolve_chat_provider(provider, model, ms_model)
    api_provider = get_api_provider(provider) if provider not in ("modelscope",) else {}
    is_apimart = is_apimart_provider(api_provider)
    upstream_messages = [{"role": "system", "content": system_prompt or SYSTEM_PROMPT}]
    for item in (messages or [])[-MAX_HISTORY_MESSAGES:]:
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "assistant"} and content:
            upstream_messages.append({"role": role, "content": content})
    upstream_messages.append({"role": "user", "content": user_message})
    async with httpx.AsyncClient(timeout=AI_REQUEST_TIMEOUT) as client:
        req_body = {"model": resolved_model, "messages": upstream_messages}
        if is_apimart:
            req_body["stream"] = False
        response = await client.post(
            f"{chat_base}/chat/completions",
            headers=chat_hdrs,
            json=req_body,
        )
        response.raise_for_status()
        raw = response.json()
    raw_data = unwrap_apimart_response(raw) if isinstance(raw, dict) else raw
    return {
        "text": text_from_chat_response(raw).strip() if isinstance(raw, dict) else "",
        "model": resolved_model,
        "raw_usage": raw_data.get("usage") if isinstance(raw_data, dict) else None,
        "raw": raw,
    }

def text_delta_from_chat_chunk(data):
    choices = data.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text") or item.get("content") or "")
        return "".join(parts)
    return str(content) if content else ""

def sse_event(data):
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

def extract_image(data):
    if isinstance(data.get("data"), dict) and isinstance(data["data"].get("result"), dict):
        data = data["data"]
    if isinstance(data.get("result"), dict):
        result_images = data["result"].get("images") or []
        if result_images:
            first = result_images[0]
            url = first.get("url")
            if isinstance(url, list) and url:
                return {"type": "url", "value": url[0]}
            if isinstance(url, str) and url:
                return {"type": "url", "value": url}
    if isinstance(data.get("data"), dict) and isinstance(data["data"].get("data"), dict):
        data = data["data"]["data"]
    images = data.get("data") or []
    if not images:
        raise HTTPException(status_code=502, detail="生图接口没有返回图片数据")
    first = images[0]
    if first.get("url"):
        return {"type": "url", "value": first["url"]}
    if first.get("b64_json"):
        return {"type": "b64", "value": first["b64_json"]}
    raise HTTPException(status_code=502, detail="无法识别生图接口返回格式")

def extract_task_id(data):
    if data.get("task_id"):
        return str(data["task_id"])
    if data.get("id") and str(data.get("id", "")).startswith("task"):
        return str(data["id"])
    nested = data.get("data")
    if isinstance(nested, list) and nested:
        first = nested[0]
        if isinstance(first, dict):
            return extract_task_id(first)
    if isinstance(nested, dict):
        return extract_task_id(nested)
    return None

def provider_protocol(provider):
    return str((provider or {}).get("protocol") or "openai").strip().lower()

def protocol_from_payload(payload):
    provider_id = str(getattr(payload, "provider_id", "") or "").strip().lower()
    if provider_id == "runninghub":
        return "runninghub"
    if provider_id == "volcengine":
        return "volcengine"
    if provider_id == "jimeng":
        return "jimeng"
    protocol = str(getattr(payload, "protocol", "") or "openai").strip().lower()
    return protocol if protocol in SUPPORTED_PROVIDER_PROTOCOLS else "openai"

def is_apimart_provider(provider):
    base_url = str((provider or {}).get("base_url") or "").lower()
    return provider_protocol(provider) == "apimart" or "apimart.ai" in base_url

def upstream_models_url(base_url: str, protocol: str):
    base_url = (base_url or "").strip().rstrip("/")
    if protocol == "volcengine":
        return f"{base_url}/models" if base_url.endswith("/api/v3") else f"{base_url}/api/v3/models"
    if protocol == "runninghub":
        return f"{base_url}/openapi/v2/models"
    return f"{base_url}/models" if base_url.endswith("/v1") else f"{base_url}/v1/models"

def upstream_model_headers(api_key: str, protocol: str):
    if protocol == "runninghub":
        return {"Authorization": api_key.strip(), "Accept": "application/json"}
    return {"Authorization": f"Bearer {api_key.strip()}", "Accept": "application/json"}

def classify_upstream_model(mid):
    lc = str(mid or "").lower()
    video_keys = ["veo", "sora", "wan2", "wanx", "doubao-seedance", "doubao-1", "kling", "hailuo", "video", "t2v-", "i2v-", "s2v", "seedance"]
    if any(k in lc for k in video_keys):
        return "video"
    image_keys = ["banana", "image", "dalle", "dall-e", "imagen", "flux", "stable", "sdxl", "midjourney", "ideogram", "fal-ai", "z-image", "qwen-image", "klein", "seedream", "text-to-image", "image-to-image"]
    if any(k in lc for k in image_keys):
        return "image"
    return "chat"

def parse_upstream_models(raw, protocol="openai"):
    items = raw.get("data") if isinstance(raw, dict) else None
    if not items and isinstance(raw, dict):
        items = raw.get("models") or raw.get("list") or raw.get("dataList") or []
    if isinstance(items, dict):
        items = items.get("list") or items.get("models") or []
    if not isinstance(items, list):
        items = []
    ids = []
    for item in items:
        if isinstance(item, str):
            mid = item
        elif isinstance(item, dict):
            mid = item.get("id") or item.get("name") or item.get("model") or item.get("modelId") or item.get("modelName")
        else:
            mid = ""
        if mid:
            ids.append(str(mid))
    ids = sorted(set(ids))
    if protocol == "volcengine" and not ids:
        ids = VOLCENGINE_DEFAULT_VIDEO_MODELS[:]
    if protocol == "runninghub" and not ids:
        ids = RUNNINGHUB_DEFAULT_IMAGE_MODELS[:]
    grouped = {"image": [], "chat": [], "video": []}
    for mid in ids:
        grouped[classify_upstream_model(mid)].append(mid)
    return grouped, ids

async def wait_for_image_task(client, task_id, provider=None):
    base_url = (provider.get("base_url") if provider else AI_BASE_URL).rstrip("/")
    is_apimart = is_apimart_provider(provider)
    if is_apimart:
        task_url = f"{base_url}/tasks/{task_id}" if base_url.endswith("/v1") else f"{base_url}/v1/tasks/{task_id}"
    else:
        task_url = f"{base_url}/images/tasks/{task_id}" if base_url.endswith("/v1") else f"{base_url}/v1/images/tasks/{task_id}"
    timeout = APIMART_IMAGE_TASK_TIMEOUT if is_apimart else IMAGE_TASK_TIMEOUT
    interval = APIMART_IMAGE_POLL_INTERVAL if is_apimart else IMAGE_POLL_INTERVAL
    initial_delay = APIMART_IMAGE_INITIAL_POLL_DELAY if is_apimart else 0
    deadline = time.monotonic() + timeout
    last_payload = {}
    while time.monotonic() < deadline:
        if initial_delay:
            await asyncio.sleep(min(initial_delay, max(0.0, deadline - time.monotonic())))
            initial_delay = 0
            if time.monotonic() >= deadline:
                break
        response = await client.get(task_url, headers=api_headers(provider=provider))
        response.raise_for_status()
        last_payload = response.json()
        task_data = last_payload.get("data") if isinstance(last_payload.get("data"), dict) else last_payload
        status = str(task_data.get("status", "")).upper()
        if status in {"SUCCESS", "COMPLETED"}:
            return last_payload
        if status in {"FAILURE", "FAILED", "ERROR"}:
            error = task_data.get("error") if isinstance(task_data.get("error"), dict) else {}
            reason = task_data.get("fail_reason") or error.get("message") or last_payload.get("message") or "生图任务失败"
            raise HTTPException(status_code=502, detail=f"生图任务失败：{reason}")
        await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
    raise HTTPException(status_code=504, detail=f"生图任务超时（已等待 {int(timeout)} 秒），task_id={task_id}")

def output_storage(category="output"):
    return (OUTPUT_INPUT_DIR, "input") if category == "input" else (OUTPUT_OUTPUT_DIR, "output")

def output_url_for(filename, category="output"):
    _, subdir = output_storage(category)
    return f"/assets/{subdir}/{filename}"

def output_path_for(filename, category="output"):
    folder, _ = output_storage(category)
    return os.path.join(folder, filename)

def output_file_from_url(url):
    if isinstance(url, dict):
        url = url.get("url", "")
    if not url or not (url.startswith("/output/") or url.startswith("/assets/")):
        return None
    clean = urllib.parse.unquote(url.split("?", 1)[0]).replace("\\", "/")
    if clean.startswith("/assets/"):
        root = ASSETS_DIR
        rel = clean[len("/assets/"):]
    else:
        root = OUTPUT_DIR
        rel = clean[len("/output/"):]
    rel = rel.lstrip("/")
    if not rel:
        return None
    path = os.path.abspath(os.path.join(root, rel))
    output_root = os.path.abspath(root)
    if os.path.commonpath([output_root, path]) != output_root or not os.path.exists(path):
        return None
    return path

def library_file_from_url(url):
    if isinstance(url, dict):
        url = url.get("url", "")
    text = str(url or "").strip()
    if not text.startswith("/api/library/file/"):
        return None
    clean = urllib.parse.unquote(text.split("?", 1)[0]).replace("\\", "/")
    prefix = "/api/library/file/"
    rest = clean[len(prefix):]
    if not rest or "/" not in rest:
        return None
    source_id, rel_path = rest.split("/", 1)
    if not source_id or not rel_path:
        return None
    sources = load_library_sources()
    src = next((s for s in sources if s.get("id") == source_id), None)
    folder = str((src or {}).get("path") or "").strip()
    if not src or not folder:
        return None
    folder_abs = os.path.abspath(folder)
    full = os.path.abspath(os.path.join(folder_abs, rel_path))
    try:
        if os.path.commonpath([folder_abs, full]) != folder_abs:
            return None
    except ValueError:
        return None
    if not os.path.isfile(full):
        return None
    return full

def archlib_file_from_url(url):
    if isinstance(url, dict):
        url = url.get("url", "")
    text = str(url or "").strip()
    if not text.startswith("/api/archlib/file/"):
        return None
    rel_path = urllib.parse.unquote(text.split("?", 1)[0][len("/api/archlib/file/"):]).replace("\\", "/")
    if not rel_path:
        return None
    root_abs = os.path.abspath(ARCHLIB_DIR)
    full = os.path.abspath(os.path.join(root_abs, rel_path))
    try:
        if os.path.commonpath([root_abs, full]) != root_abs:
            return None
    except ValueError:
        return None
    if not os.path.isfile(full):
        return None
    if os.path.splitext(full)[1].lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
        return None
    return full

def local_asset_path_from_url(url):
    return output_file_from_url(url) or library_file_from_url(url) or archlib_file_from_url(url)

def create_library_thumb_from_path(local_path, source_id):
    thumb_dir = os.path.join(LIBRARY_DIR, source_id, "thumbs")
    os.makedirs(thumb_dir, exist_ok=True)
    with Image.open(local_path) as img:
        img.load()
        w, h = img.size
        thumb = img.copy()
        thumb.thumbnail((256, 256))
        if thumb.mode not in ("RGB", "RGBA"):
            thumb = thumb.convert("RGB")
        thumb_name = f"thumb_{uuid.uuid4().hex[:8]}.jpg"
        thumb.save(os.path.join(thumb_dir, thumb_name), "JPEG", quality=80)
    return thumb_name, w, h

def content_type_for_path(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in [".mp4", ".m4v"]:
        return "video/mp4"
    if ext == ".webm":
        return "video/webm"
    if ext == ".mov":
        return "video/quicktime"
    if ext in [".jpg", ".jpeg"]:
        return "image/jpeg"
    if ext == ".webp":
        return "image/webp"
    return "image/png"

def convert_output_to_jpg(url, quality=88):
    path = output_file_from_url(url)
    if not path:
        return url
    root, ext = os.path.splitext(path)
    if ext.lower() in [".jpg", ".jpeg"]:
        return url
    jpg_path = f"{root}.jpg"
    try:
        with Image.open(path) as img:
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
                img = bg
            else:
                img = img.convert("RGB")
            img.save(jpg_path, "JPEG", quality=quality, optimize=True)
        try:
            root = ASSETS_DIR if os.path.commonpath([os.path.abspath(ASSETS_DIR), os.path.abspath(jpg_path)]) == os.path.abspath(ASSETS_DIR) else OUTPUT_DIR
        except ValueError:
            root = OUTPUT_DIR
        rel = os.path.relpath(jpg_path, root).replace("\\", "/")
        prefix = "/assets" if root == ASSETS_DIR else "/output"
        return f"{prefix}/{rel}"
    except Exception as e:
        print(f"转换 JPG 失败: {e}")
        return url

def reference_to_data_url(ref, max_size=None):
    """把本地输出文件转为 data URL（base64）。max_size 限制最长边像素，避免 payload 过大。"""
    path = local_asset_path_from_url(ref.get("url", ""))
    if not path:
        return ref.get("url", "")
    if max_size:
        try:
            with Image.open(path) as img:
                img.load()
                w, h = img.size
                if max(w, h) > max_size:
                    img.thumbnail((max_size, max_size), Image.LANCZOS)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                buf = BytesIO()
                fmt = "PNG" if img.mode == "RGBA" else "JPEG"
                img.save(buf, format=fmt, quality=88 if fmt == "JPEG" else None)
                encoded = base64.b64encode(buf.getvalue()).decode("ascii")
                mime = "image/png" if fmt == "PNG" else "image/jpeg"
                return f"data:{mime};base64,{encoded}"
        except Exception as e:
            print(f"reference resize failed, fallback to raw: {e}")
    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    return f"data:{content_type_for_path(path)};base64,{encoded}"

def compress_data_url_image(value, max_size=1536, jpeg_quality=88):
    if not isinstance(value, str) or not value.startswith("data:image/") or ";base64," not in value:
        return value
    header, encoded = value.split(";base64,", 1)
    try:
        raw = base64.b64decode(encoded)
        with Image.open(BytesIO(raw)) as img:
            img.load()
            if max_size and max(img.size) > max_size:
                img.thumbnail((max_size, max_size), Image.LANCZOS)
            has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
            if has_alpha:
                if img.mode != "RGBA":
                    img = img.convert("RGBA")
                fmt, mime = "PNG", "image/png"
            else:
                if img.mode != "RGB":
                    img = img.convert("RGB")
                fmt, mime = "JPEG", "image/jpeg"
            buf = BytesIO()
            if fmt == "JPEG":
                img.save(buf, format=fmt, quality=jpeg_quality, optimize=True)
            else:
                img.save(buf, format=fmt, optimize=True)
            return f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"
    except Exception as e:
        print(f"data url image compress failed, fallback to raw: {e}")
        return value

def modelscope_image_url(value, max_size=1536):
    if not value:
        return value
    if isinstance(value, str) and local_asset_path_from_url(value):
        return reference_to_data_url({"url": value}, max_size=max_size)
    if isinstance(value, str) and value.startswith("data:image/"):
        return compress_data_url_image(value, max_size=max_size)
    return value

def valid_video_image_input(value: str) -> bool:
    if not isinstance(value, str):
        return False
    value = value.strip()
    return (
        value.startswith("http://") or
        value.startswith("https://") or
        value.startswith("asset://") or
        (value.startswith("data:image/") and ";base64," in value)
    )

def valid_apimart_video_image_input(value: str) -> bool:
    if not isinstance(value, str):
        return False
    value = value.strip()
    return value.startswith("http://") or value.startswith("https://") or value.startswith("asset://")

def invalid_video_image_preview(value: str) -> str:
    text = str(value or "")
    if text.startswith("data:"):
        return text.split(";base64,", 1)[0] + ";base64,..."
    return text[:120]

def extract_apimart_asset_url(payload):
    if isinstance(payload, list):
        for item in payload:
            found = extract_apimart_asset_url(item)
            if found:
                return found
        return ""
    if not isinstance(payload, dict):
        return ""
    url_keys = ("url", "asset_url", "assetUrl", "uri", "file_url", "fileUrl")
    for key in url_keys:
        value = str(payload.get(key) or "").strip()
        if valid_apimart_video_image_input(value):
            return value
    id_keys = ("asset_id", "assetId", "file_id", "fileId", "id")
    for key in id_keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value if value.startswith("asset://") else f"asset://{value}"
    for key in ("data", "file", "asset", "result"):
        found = extract_apimart_asset_url(payload.get(key))
        if found:
            return found
    return ""

async def upload_image_for_apimart(client, provider, ref_url: str) -> str:
    """把本地图片转成上游可接受的输入。
    按 APIMart 文档上传到 /v1/uploads/images，拿到可用于生成接口的 http/https URL。
    绝不把 /output/* 或 /assets/* 这类本地路径直接传给上游。"""
    ref_url = str(ref_url or "").strip()
    if not ref_url:
        return ref_url
    # 已经是网络 URL 或 asset:// → 直接可用，无需上传
    if ref_url.startswith("http://") or ref_url.startswith("https://") or ref_url.startswith("asset://"):
        return ref_url
    # 当前 APIMart 视频接口只接受 http(s) 或 asset://，不接受 data:image。
    if ref_url.startswith("data:"):
        return ""
    path = local_asset_path_from_url(ref_url)
    if not path:
        return ""  # 无法解析成本地文件时，避免把无效本地路径传给上游
    try:
        ct = content_type_for_path(path)
        base_url = video_api_root(provider)
        upload_url = f"{base_url}/v1/uploads/images"
        with open(path, "rb") as fh:
            files = {"file": (os.path.basename(path), fh, ct)}
            resp = await client.post(upload_url, headers=api_headers(json_body=False, provider=provider), files=files, timeout=60)
        if resp.status_code in (200, 201):
            rj = resp.json()
            url = extract_apimart_asset_url(rj)
            if valid_apimart_video_image_input(url):
                return url
            print(f"APIMart 文件上传返回中未找到可用 asset/url: {str(rj)[:300]}")
        print(f"APIMart 文件上传失败 ({resp.status_code}): {resp.text[:300]}")
    except Exception as e:
        print(f"APIMart 文件上传异常: {e}")
    return ""

async def save_ai_image_to_output(image_data, prefix="online_", category="output"):
    filename = f"{prefix}{uuid.uuid4().hex[:10]}.png"
    path = output_path_for(filename, category)
    if image_data["type"] == "b64":
        with open(path, "wb") as f:
            f.write(base64.b64decode(image_data["value"]))
        return output_url_for(filename, category)
    value = image_data["value"]
    if value.startswith("/output/") or value.startswith("/assets/"):
        return value
    try:
        timeout = httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=20.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(value)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if "jpeg" in content_type or "jpg" in content_type:
                filename = filename[:-4] + ".jpg"
                path = output_path_for(filename, category)
            elif "webp" in content_type:
                filename = filename[:-4] + ".webp"
                path = output_path_for(filename, category)
            with open(path, "wb") as f:
                f.write(response.content)
            return output_url_for(filename, category)
    except Exception as e:
        print(f"保存上游图片失败: {e}")
        return value

def normalize_generated_canvas(url, source_width, source_height):
    """把生成结果校正回源图宽高比，不拉伸图像。
    上游已按源比例请求；这里只是防止个别模型忽略尺寸参数。"""
    width = max(0, int(source_width or 0))
    height = max(0, int(source_height or 0))
    path = local_asset_path_from_url(url)
    if not path or not width or not height:
        return url
    try:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            current_width, current_height = image.size
            if not current_width or not current_height:
                return url
            target_ratio = width / height
            current_ratio = current_width / current_height
            if abs(current_ratio - target_ratio) / target_ratio <= 0.001:
                return url
            if current_ratio > target_ratio:
                crop_width = max(1, round(current_height * target_ratio))
                left = max(0, (current_width - crop_width) // 2)
                image = image.crop((left, 0, left + crop_width, current_height))
            else:
                crop_height = max(1, round(current_width / target_ratio))
                top = max(0, (current_height - crop_height) // 2)
                image = image.crop((0, top, current_width, top + crop_height))
            suffix = os.path.splitext(path)[1].lower()
            if suffix in {".jpg", ".jpeg"} and image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            image.save(path)
        return url
    except Exception as exc:
        print(f"校正生成图画幅失败: {exc}")
        return url

async def save_remote_video_to_output(url, prefix="video_", category="output"):
    if not url:
        return ""
    if url.startswith("/output/") or url.startswith("/assets/"):
        return url
    filename = f"{prefix}{uuid.uuid4().hex[:10]}.mp4"
    path = output_path_for(filename, category)
    try:
        async with httpx.AsyncClient(timeout=VIDEO_POLL_TIMEOUT) as client:
            response = await client.get(url)
            response.raise_for_status()
            content_type = (response.headers.get("Content-Type") or "").lower()
            clean_path = urllib.parse.urlparse(url).path
            ext = os.path.splitext(clean_path)[1].lower()
            if ext in {".mp4", ".webm", ".mov"}:
                filename = filename[:-4] + ext
                path = output_path_for(filename, category)
            elif "webm" in content_type:
                filename = filename[:-4] + ".webm"
                path = output_path_for(filename, category)
            elif "quicktime" in content_type or "mov" in content_type:
                filename = filename[:-4] + ".mov"
                path = output_path_for(filename, category)
            with open(path, "wb") as f:
                f.write(response.content)
            return output_url_for(filename, category)
    except Exception as e:
        print(f"保存上游视频失败: {e}")
        return url

def parse_size_pair(size):
    match = re.fullmatch(r"\s*(\d+)\s*[xX*]\s*(\d+)\s*", str(size or ""))
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))

GPT_IMAGE2_MAX_EDGE = 3840
GPT_IMAGE2_MAX_PIXELS = 8_294_400
GPT_IMAGE2_MIN_PIXELS = 655_360

def is_gpt_image_2_model(model):
    return str(model or "").strip().lower() == "gpt-image-2"

def normalize_gpt_image_2_size(size):
    width, height = parse_size_pair(size)
    if not width or not height:
        return size or "auto"
    if width == height and (width > 2048 or width * height > 4_194_304):
        return "3840x2160"
    ratio = width / height
    if ratio > 3:
        width = height * 3
    elif ratio < 1 / 3:
        height = width * 3
    scale = min(
        1.0,
        GPT_IMAGE2_MAX_EDGE / max(width, height),
        (GPT_IMAGE2_MAX_PIXELS / max(1, width * height)) ** 0.5,
    )
    width = max(16, int((width * scale) // 16) * 16)
    height = max(16, int((height * scale) // 16) * 16)
    if width * height < GPT_IMAGE2_MIN_PIXELS:
        grow = (GPT_IMAGE2_MIN_PIXELS / max(1, width * height)) ** 0.5
        width = int((width * grow + 15) // 16) * 16
        height = int((height * grow + 15) // 16) * 16
    return f"{width}x{height}"

def apimart_size_resolution(size, preserve_canvas=False):
    width, height = parse_size_pair(size)
    if not width or not height:
        raw = str(size or "").strip().lower()
        if raw in {"1k", "2k", "4k"}:
            return "1:1", raw
        if re.fullmatch(r"(auto|\d+\s*:\s*\d+)", raw):
            return raw.replace(" ", ""), "1k"
        return "1:1", "1k"
    long_edge = max(width, height)
    pixels = width * height
    if long_edge >= 3000 or pixels > 4_500_000:
        resolution = "4k"
    elif long_edge >= 1800 or pixels > 1_800_000:
        resolution = "2k"
    else:
        resolution = "1k"
    if preserve_canvas:
        divisor = math.gcd(width, height)
        return f"{width // divisor}:{height // divisor}", resolution
    common = [
        (1, 1, "1:1"), (3, 2, "3:2"), (2, 3, "2:3"), (4, 3, "4:3"), (3, 4, "3:4"),
        (5, 4, "5:4"), (4, 5, "4:5"), (16, 9, "16:9"), (9, 16, "9:16"),
        (2, 1, "2:1"), (1, 2, "1:2"), (3, 1, "3:1"), (1, 3, "1:3"),
        (21, 9, "21:9"), (9, 21, "9:21"),
    ]
    ratio = width / height
    best = min(common, key=lambda item: abs(ratio - item[0] / item[1]))
    return best[2], resolution

async def generate_modelscope_provider_image(prompt, size, model, reference_images=None, provider=None):
    clean_token = MODELSCOPE_API_KEY.strip()
    if not clean_token:
        raise HTTPException(status_code=400, detail="未配置 ModelScope API Key，请在 API 设置中填写。")
    width, height = parse_size_pair(size)
    refs = []
    for ref in (reference_images or [])[:4]:
        if not ref.get("url"):
            continue
        # 把参考图压缩为 data URL，避免 base64 payload 过大导致 MS 内部任务失败
        refs.append(modelscope_image_url(ref.get("url", ""), max_size=1536))
    headers = {
        "Authorization": f"Bearer {clean_token}",
        "Content-Type": "application/json",
        "X-ModelScope-Async-Mode": "true",
    }
    payload = {
        "model": selected_model(model, "Tongyi-MAI/Z-Image-Turbo"),
        "prompt": prompt.strip(),
    }
    if width and height:
        payload["width"] = width
        payload["height"] = height
        payload["size"] = f"{width}x{height}"
    if refs:
        payload["image_url"] = refs

    base_root = ((provider or {}).get("base_url") or MODELSCOPE_CHAT_BASE_URL).rstrip("/")
    api_root = base_root if base_root.endswith("/v1") else f"{base_root}/v1"
    async with httpx.AsyncClient(timeout=AI_REQUEST_TIMEOUT) as client:
        submit_res = await client.post(f"{api_root}/images/generations", headers=headers, json=payload)
        submit_res.raise_for_status()
        raw = submit_res.json()
        task_id = raw.get("task_id")
        if not task_id:
            try:
                return extract_image(raw), raw
            except HTTPException:
                raise HTTPException(status_code=502, detail=f"ModelScope 未返回 task_id：{raw}")

        deadline = time.monotonic() + AI_REQUEST_TIMEOUT
        last_payload = raw
        while time.monotonic() < deadline:
            await asyncio.sleep(IMAGE_POLL_INTERVAL)
            result = await client.get(
                f"{api_root}/tasks/{task_id}",
                headers={**headers, "X-ModelScope-Task-Type": "image_generation"},
            )
            result.raise_for_status()
            data = result.json()
            last_payload = data
            status = str(data.get("task_status") or "").upper()
            if status == "SUCCEED":
                images = data.get("output_images") or []
                if not images:
                    raise HTTPException(status_code=502, detail=f"ModelScope 成功但没有返回图片：{data}")
                return {"type": "url", "value": images[0]}, data
            if status in {"FAILED", "FAIL", "ERROR", "CANCELED", "CANCELLED", "TIMEOUT", "REVOKED"}:
                detail = data.get("error_info") or data.get("message") or data.get("detail") or str(data)
                raise HTTPException(status_code=502, detail=f"ModelScope 任务失败：{detail}")
        raise HTTPException(status_code=504, detail=f"ModelScope 生图任务超时：{last_payload}")

async def generate_ai_image(prompt, size, quality, model, reference_images=None, provider_id="comfly", preserve_canvas=False):
    provider = get_api_provider(provider_id)
    if provider["id"] == "modelscope":
        return await generate_modelscope_provider_image(prompt, size, model, reference_images, provider)
    is_gpt2 = is_gpt_image_2_model(model)
    is_apimart = is_apimart_provider(provider)
    if is_gpt_image_2_model(model) and not is_apimart:
        size = normalize_gpt_image_2_size(size)
    base_url = (provider.get("base_url") or AI_BASE_URL).rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail=f"{provider.get('name') or provider['id']} 未配置 Base URL")
    gen_url = f"{base_url}/images/generations" if base_url.endswith("/v1") else f"{base_url}/v1/images/generations"
    edit_url = f"{base_url}/images/edits" if base_url.endswith("/v1") else f"{base_url}/v1/images/edits"
    refs = [ref for ref in (reference_images or []) if ref.get("url")]
    mask_refs = [ref for ref in refs if str(ref.get("role") or "").strip().lower() == "mask" or str(ref.get("name") or "").lower().endswith("_mask.png")]
    image_refs = [ref for ref in refs if ref not in mask_refs]
    request_timeout = httpx.Timeout(connect=20.0, read=600.0, write=120.0, pool=20.0) if (is_gpt2 or is_apimart) else AI_REQUEST_TIMEOUT
    async with httpx.AsyncClient(timeout=request_timeout) as client:
        response = None
        if is_apimart:
            apimart_size, resolution = apimart_size_resolution(size, preserve_canvas=preserve_canvas)
            body = {
                "model": model,
                "prompt": prompt,
                "n": 1,
                "size": apimart_size,
                "resolution": resolution.upper(),
                "official_fallback": False,
            }
            if image_refs:
                image_payload = []
                for ref in image_refs[:14]:
                    uploaded = await upload_image_for_apimart(client, provider, ref.get("url", ""))
                    if uploaded:
                        image_payload.append(uploaded)
                    else:
                        data_url = reference_to_data_url(ref, max_size=1536)
                        if data_url:
                            image_payload.append(data_url)
                body["image_urls"] = image_payload
            response = await client.post(gen_url, headers=api_headers(provider=provider), json=body)
        elif is_gpt2 and not mask_refs:
            body = {"model": model, "prompt": prompt, "size": size}
            if quality:
                body["quality"] = quality
            if image_refs:
                body["image"] = [reference_to_data_url(ref, max_size=1536) for ref in image_refs[:4]]
            response = await client.post(gen_url, headers=api_headers(provider=provider), json=body)
        elif image_refs:
            # 1) 先用 multipart 提交到 /images/edits（OpenAI / Comfly 风格）
            files = []
            opened = []
            edit_failed_status = None
            edit_failed_text = ""
            try:
                for ref in image_refs[:4]:
                    path = local_asset_path_from_url(ref.get("url", ""))
                    if not path:
                        continue
                    fh = open(path, "rb")
                    opened.append(fh)
                    files.append(("image", (os.path.basename(path), fh, content_type_for_path(path))))
                if mask_refs:
                    mask_path = local_asset_path_from_url(mask_refs[0].get("url", ""))
                    if mask_path:
                        fh = open(mask_path, "rb")
                        opened.append(fh)
                        files.append(("mask", (os.path.basename(mask_path), fh, content_type_for_path(mask_path))))
                data = {"model": model, "prompt": prompt, "size": size, "quality": quality, "response_format": "url", "n": "1"}
                try:
                    response = await client.post(edit_url, headers=api_headers(json_body=False, provider=provider), data=data, files=files)
                    if response.status_code >= 400:
                        edit_failed_status = response.status_code
                        edit_failed_text = response.text[:500]
                        response = None
                except httpx.HTTPError as e:
                    edit_failed_status = -1
                    edit_failed_text = str(e)
                    response = None
            finally:
                for fh in opened:
                    fh.close()
            # 2) edits 失败 → 回退到 /images/generations + JSON image:[urls/base64]（grsai 风格）
            if response is None:
                print(f"/images/edits failed ({edit_failed_status}): {edit_failed_text[:200]} → 回退到 /images/generations + image:[] JSON")
                image_payload = [reference_to_data_url(ref, max_size=1536) for ref in image_refs[:4]]
                body = {
                    "model": model, "prompt": prompt, "size": size,
                    "quality": quality, "response_format": "url", "n": 1,
                    "image": image_payload,
                }
                response = await client.post(gen_url, headers=api_headers(provider=provider), json=body)
        else:
            response = await client.post(
                gen_url,
                headers=api_headers(provider=provider),
                json={"model": model, "prompt": prompt, "size": size, "quality": quality, "response_format": "url", "n": 1},
            )
        response.raise_for_status()
        raw = response.json()
        try:
            return extract_image(raw), raw
        except HTTPException:
            task_id = extract_task_id(raw)
            if not task_id:
                raise
        task_result = await wait_for_image_task(client, task_id, provider)
        return extract_image(task_result), task_result

def upstream_message_from_record(item):
    role = item.get("role")
    if role not in {"user", "assistant"} or item.get("type") == "image":
        return None
    refs = item.get("attachments") or []
    if refs and role == "user":
        content = [{"type": "text", "text": item.get("content", "")}]
        for ref in refs[:4]:
            url = reference_to_data_url(ref)
            if url:
                content.append({"type": "image_url", "image_url": {"url": url}})
        return {"role": role, "content": content}
    return {"role": role, "content": item.get("content", "")}

# --- 路由接口 ---

@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

@app.get("/api/wiki/overview")
async def wiki_overview():
    ensure_wiki_dirs()
    return wiki_overview_payload()

@app.get("/api/wiki/pages")
async def wiki_pages(type: str = "", q: str = "", limit: int = 200):
    pages = load_wiki_pages()
    page_type = str(type or "").strip()
    query = str(q or "").strip().lower()
    if page_type:
        pages = [page for page in pages if page.get("type") == page_type]
    if query:
        matched_ids = {match["item"].get("id") for match in wiki_search_items(query, page_type=page_type, limit=max(1, min(limit, 500)))}
        pages = [page for page in pages if page.get("id") in matched_ids]
    pages = sorted(pages, key=lambda item: item.get("updated_at", 0), reverse=True)
    return {"pages": pages[:max(1, min(limit, 500))], "total": len(pages)}

@app.get("/api/wiki/pages/{page_id}")
async def wiki_page_detail(page_id: str):
    page_id = str(page_id or "").strip()
    source = next((item for item in load_wiki_sources() if item.get("id") == page_id), None)
    page = next((item for item in load_wiki_pages() if item.get("id") == page_id), None)
    item = source or page
    if not item:
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")
    return {
        "kind": "source" if source else "page",
        "page": item,
        "content": read_wiki_markdown(item.get("path", "")),
        "relations": [
            rel for rel in load_wiki_relations()
            if rel.get("source") == page_id or rel.get("target") == page_id
        ],
    }

@app.post("/api/wiki/sources")
async def wiki_create_source(req: WikiSourceRequest):
    source = create_wiki_source_record(
        title=req.title,
        content=req.content,
        source_type=req.source_type,
        url=req.url,
        tags=req.tags,
    )
    return {"source": source, "overview": wiki_overview_payload()}

@app.post("/api/wiki/compile")
async def wiki_compile(req: WikiCompileRequest):
    source_id = str(req.source_id or "").strip()
    if not source_id:
        if not str(req.content or "").strip():
            raise HTTPException(status_code=400, detail="请提供 source_id 或新的来源内容")
        source = create_wiki_source_record(
            title=req.title or "未命名来源",
            content=req.content,
            source_type=req.source_type,
            url=req.url,
            tags=req.tags,
        )
        source_id = source["id"]
    result = compile_wiki_source(source_id)
    return {**result, "overview": wiki_overview_payload()}

@app.get("/api/wiki/search")
async def wiki_search(q: str = "", type: str = "", limit: int = 20):
    matches = wiki_search_items(q, page_type=type, limit=max(1, min(limit, 100)))
    return {
        "query": q,
        "matches": [
            {
                "kind": match["kind"],
                "score": match["score"],
                "item": match["item"],
            }
            for match in matches
        ],
    }

@app.get("/api/wiki/graph")
async def wiki_graph():
    ensure_wiki_dirs()
    graph = _wiki_read_json(WIKI_GRAPH_FILE, None)
    if not graph:
        graph = rebuild_wiki_graph()
    return graph

@app.get("/api/local-wiki/inspect")
async def local_wiki_inspect(path: str = ""):
    target = os.path.abspath(os.path.expanduser(str(path or "").strip()))
    if not target:
        raise HTTPException(status_code=400, detail="请提供本地文件夹路径")
    exists = os.path.exists(target)
    is_dir = os.path.isdir(target)
    entries = []
    initialized = False
    if is_dir:
        try:
            entries = sorted(os.listdir(target))[:80]
            initialized = os.path.exists(os.path.join(target, ".agentwiki", "config.json"))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"无法读取文件夹：{exc}")
    return {
        "path": target,
        "exists": exists,
        "is_dir": is_dir,
        "initialized": initialized,
        "entry_count": len(entries),
        "entries": entries,
    }

@app.get("/api/local-wiki/choose-folder")
async def local_wiki_choose_folder():
    path = choose_local_folder_dialog()
    return {"path": path}

@app.get("/api/local-wiki/overview")
async def local_wiki_overview(root: str = ""):
    return local_wiki_overview_payload(root)

@app.get("/api/local-wiki/tree")
async def local_wiki_tree_api(root: str = "", max_files: int = 1200):
    return local_wiki_tree(root, max_files=max_files)

@app.get("/api/local-wiki/file")
async def local_wiki_file_api(root: str = "", path: str = ""):
    return local_wiki_file_payload(root, path)

@app.post("/api/local-wiki/init")
async def local_wiki_init(req: LocalWikiInitRequest):
    result = initialize_local_wiki(
        base_dir=req.base_dir,
        name=req.name,
        local_only=req.local_only,
        language=req.language,
        allow_existing_initialized=req.allow_existing_initialized,
    )
    return result

@app.post("/api/local-wiki/import-path")
async def local_wiki_import_path(req: LocalWikiImportPathRequest):
    imported = local_wiki_copy_source_path(
        root=req.root,
        source_path=req.source_path,
        category=req.category,
        max_files=req.max_files,
    )
    compile_result = await local_wiki_compile_imported(req.root, imported, use_llm=True)
    imported = local_wiki_refresh_imported_records(req.root, imported)
    return {"imported": imported, "count": len(imported), "compile": compile_result}

@app.post("/api/local-wiki/upload")
async def local_wiki_upload(
    root: str = Form(...),
    category: str = Form("上传文件"),
    files: List[UploadFile] = File(...),
):
    target_root = local_wiki_validate_root(root)
    imported = []
    for upload in files:
        filename = local_wiki_safe_filename(upload.filename)
        dest_dir = os.path.join(target_root, "00_收件箱", local_wiki_resolve_category(category, filename))
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = local_wiki_unique_path(dest_dir, filename)
        with open(dest_path, "wb") as f:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        imported.append(local_wiki_record_import(target_root, dest_path, upload.filename or filename, "upload"))
    compile_result = await local_wiki_compile_imported(target_root, imported, use_llm=True)
    imported = local_wiki_refresh_imported_records(target_root, imported)
    return {"imported": imported, "count": len(imported), "compile": compile_result}

@app.post("/api/local-wiki/compile")
async def local_wiki_compile_api(req: LocalWikiCompileRequest):
    return await local_wiki_compile(req.root, source_id=req.source_id, force=req.force, use_llm=req.use_llm, provider=req.provider, model=req.model)

@app.get("/api/local-wiki/search")
async def local_wiki_search_api(root: str = "", q: str = "", type: str = "", limit: int = 20):
    matches = local_wiki_search_items(root, q, page_type=type, limit=max(1, min(limit, 100)))
    return {
        "query": q,
        "matches": [
            {
                "kind": match["kind"],
                "score": match["score"],
                "item": match["item"],
            }
            for match in matches
        ],
    }

@app.post("/api/local-wiki/qa")
async def local_wiki_qa_api(req: LocalWikiQARequest):
    root = local_wiki_validate_root(req.root)
    matches = local_wiki_search_items(root, req.question, page_type=req.page_type, limit=24)
    if not matches and local_wiki_should_use_recent_fallback(req.question):
        matches = local_wiki_recent_context_items(root, limit=24)
    answer, snippets, related_ids, meta = await answer_from_wiki_context(
        req.question,
        matches,
        use_llm=req.use_llm,
        provider=req.provider,
        model=req.model,
        local_only=False,
    )
    page = None
    if req.save:
        source_lines = [f"- [[{item['id']}]] {item['title']}" for item in snippets if item.get("id")]
        saved_answer = answer
        if meta.get("llm_error"):
            saved_answer = f"API 调用失败，未生成模型回答。\n\n错误信息：{meta.get('llm_error')}"
        page = local_wiki_create_output_page(
            root,
            "qa",
            display_title(req.question),
            "\n".join([
                "## 问题",
                req.question,
                "",
                "## 回答",
                saved_answer,
                "",
                "## 引用",
                *(source_lines or ["- 当前没有可引用的本地来源。"]),
            ]),
            related_ids=related_ids,
        )
    return {
        "answer": answer,
        "snippets": snippets,
        "page": page,
        "meta": meta,
        "overview": local_wiki_overview_payload(root),
    }

@app.post("/api/wiki/qa")
async def wiki_qa(req: WikiQARequest):
    matches = wiki_search_items(req.question, page_type=req.page_type, limit=AGENT_WIKI_CONTEXT_LIMIT)
    answer, snippets, related_ids = wiki_answer_from_context(req.question, matches)
    page = None
    if req.save:
        source_lines = [f"- [[{item['id']}]] {item['title']}" for item in snippets if item.get("id")]
        page = create_wiki_output_page(
            "qa",
            display_title(req.question),
            "\n".join([
                "## 问题",
                req.question,
                "",
                "## 回答",
                answer,
                "",
                "## 引用",
                *(source_lines or ["- 当前没有可引用的本地来源。"]),
            ]),
            related_ids=related_ids,
        )
    return {"answer": answer, "matches": snippets, "page": page, "overview": wiki_overview_payload()}


def agent_task_cancelled(task_id: str) -> bool:
    with AGENT_TASK_LOCK:
        return agent_task_is_cancelled(AGENT_TASK_DIR, task_id)


def agent_task_progress(
    task_id: str,
    *,
    step_id: str = "",
    message: str = "",
    progress_current: Optional[int] = None,
    progress_total: Optional[int] = None,
    event_type: str = "progress",
):
    with AGENT_TASK_LOCK:
        return agent_update_task_progress(
            AGENT_TASK_DIR,
            task_id,
            step_id=step_id,
            message=message,
            progress_current=progress_current,
            progress_total=progress_total,
            event_type=event_type,
        )


async def run_library_to_smart_canvas_task(task_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
    project_id = resolve_project_id(str(context.get("project_id") or ""), str(context.get("canvas_id") or ""))
    agent_task_progress(
        task_id,
        step_id="list_library_images",
        message="正在解析资源库目标图片...",
        progress_current=0,
        progress_total=3,
        event_type="step",
    )
    if agent_task_cancelled(task_id):
        return load_agent_task(AGENT_TASK_DIR, task_id)

    resolved = resolve_library_agent_matches(context, limit=AGENT_SMART_CANVAS_LIMIT, enrich=True)
    images = resolved["items"]
    total = resolved["matched_total"]
    if not images:
        raise HTTPException(status_code=400, detail="当前资源库上下文里没有可用于创建智能画布的图片")

    agent_task_progress(
        task_id,
        step_id="create_smart_canvas",
        message="正在创建智能画布...",
        progress_current=1,
        progress_total=3,
        event_type="step",
    )
    if agent_task_cancelled(task_id):
        return load_agent_task(AGENT_TASK_DIR, task_id)

    source_name = str(context.get("source_name") or "").strip()
    category = str(context.get("category") or "").strip()
    query = str(context.get("query") or "").strip()
    title_bits = [bit for bit in [source_name, category, query] if bit]
    canvas_title = " · ".join(title_bits[:2]) if title_bits else "资源库智能画布草稿"
    canvas = new_canvas(title=canvas_title[:80], icon="sparkles", kind="smart", project_id=project_id)

    agent_task_progress(
        task_id,
        step_id="append_images_to_smart_canvas",
        message=f"正在插入 {len(images)} 张图片到智能画布...",
        progress_current=2,
        progress_total=3,
        event_type="step",
    )
    if agent_task_cancelled(task_id):
        return load_agent_task(AGENT_TASK_DIR, task_id)

    node = create_smart_canvas_image_node(images, x=120, y=120)
    canvas["nodes"] = [node]
    canvas["connections"] = []
    canvas["settings"] = canvas.get("settings") or {}
    save_canvas(canvas)
    for image in images:
        asset = library_asset_for_project(image, project_id, create=True)
        if asset:
            DOMAIN_STORE.record_preference_event(
                project_id,
                asset["id"],
                "used_in_canvas",
                {"canvas_id": canvas["id"], "node_id": node["id"], "source": "agent"},
            )

    agent_task_progress(
        task_id,
        step_id="append_images_to_smart_canvas",
        message="智能画布已创建，图片插入完成。",
        progress_current=3,
        progress_total=3,
    )
    if agent_task_cancelled(task_id):
        return load_agent_task(AGENT_TASK_DIR, task_id)

    result = {
        "kind": "smart_canvas_created",
        "canvas_id": canvas["id"],
        "canvas_title": canvas["title"],
        "open_url": f"/static/smart-canvas.html?id={urllib.parse.quote(canvas['id'])}&v=20260724-canvas-agent-history-fix",
        "inserted_image_count": len(node["images"]),
        "matched_total": total,
        "processed_count": len(node["images"]),
        "truncated": total > len(node["images"]),
        "target_source_label": resolved["target_source_label"],
        "node_id": node["id"],
    }
    with AGENT_TASK_LOCK:
        return agent_update_task_result(
            AGENT_TASK_DIR,
            task_id,
            result=result,
            message=f"已创建智能画布《{canvas['title']}》，并插入 {len(node['images'])} 张图片。",
        )


async def run_smart_canvas_to_library_task(task_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
    selected_urls = [str(x).strip() for x in (context.get("selected_image_urls") or []) if str(x).strip()]
    agent_task_progress(
        task_id,
        step_id="read_smart_canvas",
        message="正在读取当前智能画布选中结果...",
        progress_current=0,
        progress_total=2,
        event_type="step",
    )
    if not selected_urls:
        raise HTTPException(status_code=400, detail="当前智能画布上下文里没有可回存到资源库的图片")
    if agent_task_cancelled(task_id):
        return load_agent_task(AGENT_TASK_DIR, task_id)

    agent_task_progress(
        task_id,
        step_id="save_canvas_node_images_to_library",
        message=f"正在回存 {len(selected_urls)} 张图片到资源库...",
        progress_current=1,
        progress_total=2,
        event_type="step",
    )
    if agent_task_cancelled(task_id):
        return load_agent_task(AGENT_TASK_DIR, task_id)

    canvas_title = str(context.get("canvas_title") or "").strip()
    canvas_id = str(context.get("canvas_id") or "").strip()
    node_id = str(context.get("selected_node_id") or "").strip()
    import_result = import_urls_into_library(
        urls=selected_urls,
        items=context.get("selected_library_import_items") or context.get("selected_import_items") or [],
        source_name="智能画布",
        canvas_id=canvas_id,
        canvas_title=canvas_title,
        node_id=node_id,
        manual_tags=[canvas_title or "智能画布"],
    )
    count = int(import_result.get("count", 0) or 0)
    skipped = import_result.get("skipped") or []

    agent_task_progress(
        task_id,
        step_id="save_canvas_node_images_to_library",
        message="资源库回存已完成。",
        progress_current=2,
        progress_total=2,
    )
    if agent_task_cancelled(task_id):
        return load_agent_task(AGENT_TASK_DIR, task_id)

    result = {
        "kind": "library_imported",
        "count": count,
        "processed_count": count,
        "skipped_count": len(skipped),
        "source_id": import_result.get("source_id", ""),
        "imported_ids": [item.get("id") for item in import_result.get("imported", [])],
        "skipped": skipped,
        "canvas_id": canvas_id,
        "canvas_title": canvas_title,
        "node_id": node_id,
        "open_url": "/static/library.html?v=20260521-library-workflow",
    }
    with AGENT_TASK_LOCK:
        return agent_update_task_result(
            AGENT_TASK_DIR,
            task_id,
            result=result,
            message=f"已从智能画布回存 {count} 张图片到资源库。",
        )


async def run_library_batch_tag_task(task_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
    provider = str(context.get("provider") or "").strip()
    model = str(context.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="请先选择用于批量标注的 Provider 和模型")

    agent_task_progress(
        task_id,
        step_id="list_library_images",
        message="正在解析需要批量标注的资源库图片...",
        progress_current=0,
        progress_total=1,
        event_type="step",
    )
    targets = library_tag_targets_for_agent(context, limit=AGENT_LIBRARY_TAG_LIMIT)
    if not targets:
        raise HTTPException(status_code=400, detail="当前资源库上下文里没有可用于批量标注的图片")
    if agent_task_cancelled(task_id):
        return load_agent_task(AGENT_TASK_DIR, task_id)

    total = len(targets)
    agent_task_progress(
        task_id,
        step_id="tag_library_images",
        message=f"准备批量标注 {total} 张图片...",
        progress_current=0,
        progress_total=total,
        event_type="step",
    )

    results = []
    success_count = 0
    failed_count = 0
    for index, img in enumerate(targets, start=1):
        if agent_task_cancelled(task_id):
            return load_agent_task(AGENT_TASK_DIR, task_id)
        filename = str(img.get("filename") or img.get("id") or f"image-{index}")[:80]
        agent_task_progress(
            task_id,
            step_id="tag_library_images",
            message=f"正在标注 {index}/{total}：{filename}",
            progress_current=index - 1,
            progress_total=total,
            event_type="step",
        )
        tag_result = await library_ai_tag(LibraryTagRequest(
            image_ids=[img["id"]],
            provider=provider,
            model=model,
        ))
        item = (tag_result.get("results") or [{}])[0]
        results.append(item)
        if item.get("ok"):
            success_count += 1
        else:
            failed_count += 1
        agent_task_progress(
            task_id,
            step_id="tag_library_images",
            message=f"已完成 {index}/{total} 张批量标注。",
            progress_current=index,
            progress_total=total,
        )

    if agent_task_cancelled(task_id):
        return load_agent_task(AGENT_TASK_DIR, task_id)

    result = {
        "kind": "library_tagged",
        "count": total,
        "processed_count": total,
        "success_count": success_count,
        "failed_count": failed_count,
        "provider": provider,
        "model": model,
        "results": results,
        "open_url": "/static/library.html?v=20260521-library-workflow",
    }
    with AGENT_TASK_LOCK:
        return agent_update_task_result(
            AGENT_TASK_DIR,
            task_id,
            result=result,
            message=f"已完成 {success_count}/{total} 张资源库图片的批量标注。",
        )

def agent_related_ids_from_matches(matches: List[Dict[str, Any]]) -> List[str]:
    related = []
    for match in matches:
        item = match.get("item") or {}
        item_id = item.get("id")
        if item_id and item_id not in related:
            related.append(item_id)
    return related

def agent_local_wiki_root(context: Dict[str, Any]) -> str:
    for key in ("local_wiki_root", "wiki_root", "active_local_wiki_root"):
        raw = str((context or {}).get(key) or "").strip()
        if raw:
            try:
                return local_wiki_validate_root(raw)
            except Exception:
                return ""
    return ""

def agent_wiki_search_items(goal: str, context: Dict[str, Any], limit: int = AGENT_WIKI_CONTEXT_LIMIT) -> List[Dict[str, Any]]:
    root = agent_local_wiki_root(context)
    if root:
        return local_wiki_search_items(root, goal, limit=limit)
    return wiki_search_items(goal, limit=limit)

def agent_create_wiki_output_page(context: Dict[str, Any], output_type: str, title: str, content: str, related_ids: Optional[List[str]] = None):
    root = agent_local_wiki_root(context)
    if root:
        return local_wiki_create_output_page(root, output_type, title, content, related_ids=related_ids)
    return create_wiki_output_page(output_type, title, content, related_ids=related_ids)

def compile_project_design_context(
    project_id: str,
    goal: str,
    context: Optional[Dict[str, Any]] = None,
    *,
    persist: bool = True,
) -> Dict[str, Any]:
    project = DOMAIN_STORE.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    context = dict(context or {})
    goal = str(goal or "").strip()
    preference = build_project_preference_profile(project_id)
    wiki_matches = agent_wiki_search_items(goal, context, limit=4) if goal else []
    try:
        settings = json.loads(project.get("settings_json") or "{}")
    except (TypeError, ValueError):
        settings = {}

    constraints: List[Dict[str, Any]] = []
    sources: List[Dict[str, Any]] = []
    references: List[Dict[str, Any]] = []
    seen_constraints = set()
    seen_urls = set()

    def add_constraint(kind: str, polarity: str, text: str, source_type: str, source_id: str = "", priority: int = 50):
        normalized = " ".join(str(text or "").split()).strip(" ，,;；")
        key = (polarity, normalized.lower())
        if not normalized or key in seen_constraints:
            return
        seen_constraints.add(key)
        constraints.append({
            "id": f"constraint_{hashlib.sha256((polarity + normalized).encode('utf-8')).hexdigest()[:12]}",
            "kind": kind,
            "polarity": polarity,
            "text": normalized[:500],
            "source_type": source_type,
            "source_id": str(source_id or ""),
            "priority": int(priority),
        })

    def add_reference(raw: Dict[str, Any], reason: str, source_type: str):
        url = str(raw.get("url") or raw.get("storage_url") or "").strip()
        if not url or url in seen_urls:
            return
        seen_urls.add(url)
        reference = {
            "asset_id": str(raw.get("asset_id") or raw.get("id") or ""),
            "title": str(raw.get("title") or raw.get("name") or "项目参考图")[:160],
            "url": url,
            "reason": reason,
            "source_type": source_type,
        }
        references.append(reference)
        sources.append({"type": source_type, **reference})

    add_constraint("task", "positive", goal, "task", priority=100)
    positive_setting_keys = ("design_constraints", "style_rules", "positive_rules", "requirements")
    negative_setting_keys = ("negative_rules", "avoid_rules", "forbidden")
    for key in positive_setting_keys + negative_setting_keys:
        raw_values = settings.get(key) or []
        if isinstance(raw_values, str):
            raw_values = [raw_values]
        for value in raw_values if isinstance(raw_values, list) else []:
            add_constraint(
                "manual",
                "negative" if key in negative_setting_keys else "positive",
                str(value),
                "project_settings",
                key,
                90,
            )
    if any(settings.get(key) for key in positive_setting_keys + negative_setting_keys):
        sources.append({"type": "project_settings", "id": project_id, "title": "项目手工规则"})

    for line in preference.get("guidance") or []:
        polarity = "negative" if str(line).startswith("降低或避免") else "positive"
        add_constraint("preference", polarity, line, "feedback", project_id, 80)
    for asset in preference.get("positive_assets") or []:
        add_reference(asset, "已采纳、收藏或高评分素材", "positive_feedback_asset")
    for asset in preference.get("avoided_assets") or []:
        sources.append({
            "type": "negative_feedback_asset",
            "asset_id": str(asset.get("asset_id") or ""),
            "title": str(asset.get("title") or "已淘汰素材"),
            "url": str(asset.get("storage_url") or ""),
            "reason": "已淘汰或低评分，不作为视觉参考",
        })

    explicit_references = context.get("reference_images") or []
    for raw in explicit_references if isinstance(explicit_references, list) else []:
        if isinstance(raw, BaseModel):
            raw = raw.model_dump(mode="json")
        if isinstance(raw, dict):
            add_reference(raw, "本次任务明确指定", "task_reference")

    for match in wiki_matches:
        item = match.get("item") or {}
        item_id = str(item.get("id") or item.get("path") or "")
        excerpt = wiki_excerpt(read_agent_wiki_match_content(match) or item.get("excerpt", ""), 240)
        title = str(item.get("title") or "Wiki 依据")
        sources.append({"type": "wiki", "id": item_id, "title": title, "excerpt": excerpt})
        if excerpt:
            add_constraint("knowledge", "positive", f"参考知识《{title}》：{excerpt}", "wiki", item_id, 60)

    constraints.sort(key=lambda item: (-int(item.get("priority") or 0), item["id"]))
    references = references[:4]
    payload = {
        "project_id": project_id,
        "goal": goal,
        "constraints": constraints[:16],
        "sources": sources[:24],
        "reference_assets": references,
    }
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    if persist:
        compiled = DOMAIN_STORE.record_project_context_compilation(
            project_id,
            goal,
            digest=digest,
            constraints=payload["constraints"],
            sources=payload["sources"],
            reference_assets=payload["reference_assets"],
        )
    else:
        compiled = {"id": "", "digest": digest, **payload}
    compiled["positive_constraints"] = [
        item["text"] for item in payload["constraints"] if item.get("polarity") != "negative"
    ]
    compiled["negative_constraints"] = [
        item["text"] for item in payload["constraints"] if item.get("polarity") == "negative"
    ]
    compiled["wiki_matches"] = wiki_matches
    return compiled


def apply_compiled_context_to_prompt(prompt: str, compiled_context: Optional[Dict[str, Any]]) -> str:
    prompt = str(prompt or "").strip()
    compiled_context = compiled_context or {}
    if "项目约束（必须遵守）" in prompt:
        return prompt
    positive = [str(item).strip() for item in (compiled_context.get("positive_constraints") or []) if str(item).strip()]
    negative = [str(item).strip() for item in (compiled_context.get("negative_constraints") or []) if str(item).strip()]
    additions = []
    if positive:
        additions.append("项目约束（必须遵守）：" + "；".join(positive[:8]))
    if negative:
        additions.append("项目禁止项：" + "；".join(negative[:6]))
    return "\n".join([prompt, *additions]).strip()


def build_design_prompt(goal: str, wiki_matches: List[Dict[str, Any]], library_items: List[Dict[str, Any]], preference_profile: Optional[Dict[str, Any]] = None, compiled_context: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    wiki_bits = []
    for match in wiki_matches[:4]:
        item = match.get("item") or {}
        text = wiki_excerpt(read_agent_wiki_match_content(match) or item.get("excerpt", ""), 120)
        if item.get("title"):
            wiki_bits.append(f"{item.get('title')}：{text}")
    image_bits = []
    for img in library_items[:4]:
        tags = ", ".join([*(img.get("categories") or []), *(img.get("tags") or []), *(img.get("manual_tags") or [])][:6])
        if img.get("filename"):
            image_bits.append(f"{img.get('filename')}{f'（{tags}）' if tags else ''}")
    positive = "，".join([
        goal,
        "建筑空间概念设计",
        "清晰空间层次",
        "真实材质",
        "现代商业氛围",
        "自然人流尺度",
        "高质量建筑可视化",
        "cinematic architectural visualization",
    ])
    if wiki_bits:
        positive += "，参考知识：" + "；".join(wiki_bits[:2])
    preference_profile = preference_profile or {}
    preferred_terms = preference_profile.get("preferred_terms") or []
    avoided_terms = preference_profile.get("avoided_terms") or []
    if preferred_terms:
        positive += "，项目历史偏好：" + "、".join(preferred_terms[:8])
    negative = "低清晰度，模糊，畸形结构，杂乱构图，错误文字，过度拼接，不合理尺度，低质量渲染"
    if avoided_terms:
        negative += "，项目历史避免：" + "、".join(avoided_terms[:8])
    compiled_context = compiled_context or {}
    positive = apply_compiled_context_to_prompt(positive, compiled_context)
    compiled_negative = [str(item).strip() for item in (compiled_context.get("negative_constraints") or []) if str(item).strip()]
    if compiled_negative:
        negative += "，项目禁止项：" + "、".join(compiled_negative[:6])
    brief = "\n".join([
        "## 设计目标",
        goal,
        "",
        "## 本地知识依据",
        *(f"- {item}" for item in (wiki_bits or ["当前 Wiki 暂无命中，使用任务目标生成首版方向。"])),
        "",
        "## 资源库参考",
        *(f"- {item}" for item in (image_bits or ["当前资源库上下文暂无命中参考图。"])),
        "",
        "## 项目历史偏好",
        *(f"- {item}" for item in (preference_profile.get("guidance") or ["当前项目还没有足够的显式偏好信号。"])),
        "",
        "## 本次编译约束",
        *(f"- {item.get('text')}（{item.get('source_type')}）" for item in (compiled_context.get("constraints") or [])),
        "",
        "## 正向提示词",
        positive,
        "",
        "## 负向提示词",
        negative,
        "",
        "## 下一步",
        "- 如图片生成成功，结果会自动导入资源库并送入智能画布。",
        "- 如上游 API 未配置，保留本简报后可手动去在线生图页继续生成。",
    ])
    return {"positive": positive, "negative": negative, "brief": brief}


def parse_agent_kernel_decision(text: str) -> Dict[str, Any]:
    raw = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, re.IGNORECASE)
    if fenced:
        raw = fenced.group(1).strip()
    candidates = [raw]
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except Exception:
            continue
    raise AgentKernelError(f"Agent 规划器未返回有效 JSON：{raw[:240]}")


async def plan_agent_kernel_action(payload: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    provider = str(context.get("provider") or "").strip()
    model = str(context.get("model") or "").strip()
    system_prompt = """
你是建筑设计 AI 工作台的受控 Agent 执行器。你不直接声称已完成操作，只能根据当前状态选择一个工具，或在目标已达成/确实无法继续时结束。
只返回一个 JSON 对象，不要 Markdown，不要额外文字。
调用工具：{"action":"tool","tool":"工具名","arguments":{},"reason":"为什么调用"}
结束任务：{"action":"finish","answer":"给用户的简洁结果，说明产物和任何未完成项"}
规则：
1. 只能选择 tools 里的工具，参数必须符合 input_schema。
1.1 如果 context.active_skills 不为空，必须遵循其 instructions，但不能突破工具权限和用户确认边界。
2. 先读取项目上下文，再按需要搜索 Wiki/素材，不要重复已有结果。
3. 设计任务应先产生简报，再尝试生图，并将可用结果保存到项目、资源库和智能画布。
4. 研究、总结和学习任务必须使用 Wiki 检索结果生成内容，再调用已授权的 Wiki 写入工具持久化；无命中时要显式标注依据不足。
5. 某个工具失败后要根据 error 换路、修正参数或带着明确说明结束，不要无限重试。
6. 步数接近 max_steps 时优先保存已有产物并结束。
""".strip()
    response = await request_chat_completion(
        user_message=json.dumps(payload, ensure_ascii=False),
        system_prompt=system_prompt,
        provider=provider,
        model=model,
        ms_model=model if provider == "modelscope" else "",
    )
    try:
        return parse_agent_kernel_decision(response.get("text") or "")
    except AgentKernelError:
        repair = await request_chat_completion(
            user_message="上一次输出无法解析。请严格根据下面状态只返回一个 JSON 决策：\n" + json.dumps(payload, ensure_ascii=False),
            system_prompt=system_prompt,
            provider=provider,
            model=model,
            ms_model=model if provider == "modelscope" else "",
        )
        return parse_agent_kernel_decision(repair.get("text") or "")


def build_design_agent_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def get_project_context_tool(arguments, execution):
        project_id = resolve_project_id(
            str(arguments.get("project_id") or execution.context.get("project_id") or ""),
            str(execution.context.get("canvas_id") or ""),
        )
        workspace = DOMAIN_STORE.project_workspace(project_id, limit=12)
        preference_profile = build_project_preference_profile(project_id)
        compiled_context = compile_project_design_context(project_id, execution.goal, execution.context)
        execution.state["project_id"] = project_id
        execution.state["project"] = workspace.get("project") or {}
        execution.state["project_assets"] = workspace.get("recent_assets") or []
        execution.state["preference_profile"] = preference_profile
        execution.state["compiled_context"] = compiled_context
        execution.state["context_compilation_id"] = compiled_context.get("id", "")
        execution.state["wiki_matches"] = list(compiled_context.get("wiki_matches") or [])
        return {
            "project": workspace.get("project") or {},
            "counts": workspace.get("counts") or {},
            "canvases": (workspace.get("canvases") or [])[:8],
            "recent_tasks": (workspace.get("recent_tasks") or [])[:8],
            "recent_assets": (workspace.get("recent_assets") or [])[:8],
            "feedback_summary": workspace.get("feedback_summary") or {},
            "preference_profile": preference_profile,
            "compiled_context": {
                "id": compiled_context.get("id", ""),
                "digest": compiled_context.get("digest", ""),
                "constraints": compiled_context.get("constraints") or [],
                "sources": compiled_context.get("sources") or [],
                "reference_assets": compiled_context.get("reference_assets") or [],
            },
            "skill_candidates": workspace.get("skill_candidates") or [],
        }

    async def search_wiki_tool(arguments, execution):
        query = str(arguments.get("query") or execution.goal).strip()
        limit = max(1, min(AGENT_WIKI_CONTEXT_LIMIT, int(arguments.get("limit") or AGENT_WIKI_CONTEXT_LIMIT)))
        matches = agent_wiki_search_items(query, execution.context, limit=limit)
        merged_matches = list(execution.state.get("wiki_matches") or [])
        known_match_ids = {
            str((match.get("item") or {}).get("id") or (match.get("item") or {}).get("path") or "")
            for match in merged_matches
        }
        for match in matches:
            match_id = str((match.get("item") or {}).get("id") or (match.get("item") or {}).get("path") or "")
            if match_id and match_id not in known_match_ids:
                merged_matches.append(match)
                known_match_ids.add(match_id)
        execution.state["wiki_matches"] = merged_matches
        return {
            "query": query,
            "count": len(matches),
            "items": [
                {
                    "id": (match.get("item") or {}).get("id", ""),
                    "title": (match.get("item") or {}).get("title", ""),
                    "excerpt": wiki_excerpt(
                        read_agent_wiki_match_content(match) or (match.get("item") or {}).get("excerpt", ""),
                        360,
                    ),
                }
                for match in matches
            ],
        }

    def resolved_wiki_related_ids(arguments, execution):
        requested_ids = [str(item).strip() for item in (arguments.get("related_ids") or []) if str(item).strip()]
        if "wiki_matches" not in execution.state:
            return requested_ids
        available_ids = agent_related_ids_from_matches(execution.state.get("wiki_matches") or [])
        available_set = set(available_ids)
        valid_requested = [item_id for item_id in requested_ids if item_id in available_set]
        return valid_requested or available_ids

    async def search_library_tool(arguments, execution):
        merged = dict(execution.context)
        if arguments.get("query"):
            merged["query"] = str(arguments["query"]).strip()
        if arguments.get("project_id"):
            merged["project_id"] = str(arguments["project_id"]).strip()
        limit = max(1, min(AGENT_SMART_CANVAS_LIMIT, int(arguments.get("limit") or 8)))
        resolved = resolve_library_agent_matches(merged, limit=limit, enrich=True)
        images = resolved.get("items") or []
        execution.state["library_items"] = images
        return {
            "query": str(merged.get("query") or ""),
            "matched_total": resolved.get("matched_total", 0),
            "count": len(images),
            "items": [
                {
                    "id": image.get("id", ""),
                    "asset_id": image.get("asset_id") or "",
                    "filename": image.get("filename", ""),
                    "url": image.get("url", ""),
                    "categories": image.get("categories") or [],
                    "tags": image.get("tags") or image.get("manual_tags") or [],
                    "feedback": image.get("feedback") or {},
                }
                for image in images
            ],
        }

    async def tag_library_tool(arguments, execution):
        project_id = resolve_project_id(
            str(arguments.get("project_id") or execution.state.get("project_id") or execution.context.get("project_id") or ""),
            str(execution.context.get("canvas_id") or ""),
        )
        requested_ids = [str(item).strip() for item in (arguments.get("image_ids") or []) if str(item).strip()]
        if not requested_ids:
            requested_ids = [
                str(item.get("id") or "")
                for item in (execution.state.get("library_items") or [])
                if item.get("id") and not item.get("domain_only")
            ]
        available_ids = {
            str(item.get("id") or "")
            for item in filter_library_images_by_scope(load_library_images(), "available", project_id)
        }
        image_ids = list(dict.fromkeys(item for item in requested_ids if item in available_ids))[:AGENT_LIBRARY_TAG_LIMIT]
        if not image_ids:
            raise HTTPException(status_code=400, detail="当前项目范围内没有可标注的资源库图片")
        provider = str(arguments.get("provider") or execution.context.get("provider") or "").strip()
        model = str(arguments.get("model") or execution.context.get("model") or "").strip()
        if not model:
            raise HTTPException(status_code=400, detail="素材标注需要明确的模型")
        result = await library_ai_tag(LibraryTagRequest(image_ids=image_ids, provider=provider, model=model))
        items = result.get("results") or []
        execution.state["library_tag_result"] = result
        return {
            "project_id": project_id,
            "processed_count": len(items),
            "success_count": sum(1 for item in items if item.get("ok")),
            "failed_count": sum(1 for item in items if not item.get("ok")),
            "results": items,
        }

    async def write_wiki_qa_tool(arguments, execution):
        question = str(arguments.get("question") or execution.goal).strip()
        answer = str(arguments.get("answer") or "").strip()
        if not answer:
            raise HTTPException(status_code=400, detail="写入 Wiki 前必须提供回答内容")
        related_ids = resolved_wiki_related_ids(arguments, execution)
        title = str(arguments.get("title") or display_title(question)).strip()[:120]
        content = "\n".join([
            "## 问题",
            question,
            "",
            "## 回答",
            answer,
            "",
            "## 引用",
            *(f"- [[{item_id}]]" for item_id in related_ids),
        ]).rstrip()
        page = agent_create_wiki_output_page(
            execution.context,
            "qa",
            title,
            content,
            related_ids=related_ids,
        )
        execution.state["wiki_page"] = {"id": page.get("id", ""), "title": page.get("title", "")}
        return {
            "wiki_page_id": page.get("id", ""),
            "wiki_page_title": page.get("title", ""),
            "question": question,
            "related_ids": related_ids,
        }

    async def write_agent_report_tool(arguments, execution):
        title = str(arguments.get("title") or f"{display_title(execution.goal)} 工作报告").strip()[:120]
        content = str(arguments.get("content") or "").strip()
        if not content:
            raise HTTPException(status_code=400, detail="写入工作报告前必须提供报告内容")
        related_ids = resolved_wiki_related_ids(arguments, execution)
        project_id = resolve_project_id(
            str(execution.state.get("project_id") or execution.context.get("project_id") or ""),
            str(execution.context.get("canvas_id") or ""),
        )
        report_content = "\n".join([
            "## 项目上下文",
            f"- 项目 ID：`{project_id}`",
            f"- 任务模式：{str(execution.context.get('mode') or 'summary')}",
            "",
            content,
            "",
            "## 关联来源",
            *(f"- [[{item_id}]]" for item_id in related_ids),
        ]).rstrip()
        page = agent_create_wiki_output_page(
            execution.context,
            "report",
            title,
            report_content,
            related_ids=related_ids,
        )
        execution.state["project_id"] = project_id
        execution.state["wiki_page"] = {"id": page.get("id", ""), "title": page.get("title", "")}
        execution.state["wiki_related_ids"] = related_ids
        return {
            "project_id": project_id,
            "wiki_page_id": page.get("id", ""),
            "wiki_page_title": page.get("title", ""),
            "related_ids": related_ids,
        }

    async def generate_brief_tool(arguments, execution):
        focus = str(arguments.get("focus") or "").strip()
        brief_goal = execution.goal if not focus else f"{execution.goal}；设计重点：{focus}"
        prompt_pack = build_design_prompt(
            brief_goal,
            execution.state.get("wiki_matches") or [],
            execution.state.get("library_items") or [],
            execution.state.get("preference_profile") or {},
            execution.state.get("compiled_context") or {},
        )
        related_ids = agent_related_ids_from_matches(execution.state.get("wiki_matches") or [])
        page = agent_create_wiki_output_page(
            execution.context,
            "design",
            f"{display_title(execution.goal)} 设计简报",
            prompt_pack["brief"],
            related_ids=related_ids,
        )
        execution.state["prompt_pack"] = prompt_pack
        execution.state["wiki_page"] = {"id": page.get("id", ""), "title": page.get("title", "")}
        return {
            "brief": prompt_pack["brief"],
            "positive_prompt": prompt_pack["positive"],
            "negative_prompt": prompt_pack["negative"],
            "wiki_page_id": page.get("id", ""),
            "wiki_page_title": page.get("title", ""),
        }

    async def generate_image_tool(arguments, execution):
        prompt_pack = execution.state.get("prompt_pack") or build_design_prompt(
            execution.goal,
            execution.state.get("wiki_matches") or [],
            execution.state.get("library_items") or [],
            execution.state.get("preference_profile") or {},
            execution.state.get("compiled_context") or {},
        )
        compiled_context = execution.state.get("compiled_context") or {}
        prompt = apply_compiled_context_to_prompt(
            str(arguments.get("prompt") or prompt_pack["positive"]).strip(),
            compiled_context,
        )
        project_id = str(execution.state.get("project_id") or execution.context.get("project_id") or "")
        reference_images = [
            {
                "url": str(item.get("url") or ""),
                "name": str(item.get("title") or "项目参考图"),
                "role": "style_reference",
                "asset_id": str(item.get("asset_id") or ""),
            }
            for item in (compiled_context.get("reference_assets") or [])[:2]
            if item.get("url")
        ]
        use_project_reference = any(token in execution.goal for token in [
            "参考图", "已有素材", "现有素材", "项目素材", "基于当前项目", "基于项目",
        ])
        if use_project_reference and not reference_images:
            project_assets = list(execution.state.get("project_assets") or [])
            project_assets.sort(key=lambda item: 0 if item.get("source") == "generation_input" else 1)
            reference_asset = next((item for item in project_assets if item.get("storage_url")), None)
            if reference_asset:
                reference_images.append({
                    "url": str(reference_asset.get("storage_url") or ""),
                    "name": str(reference_asset.get("title") or "项目参考图"),
                    "role": "reference",
                    "asset_id": str(reference_asset.get("id") or ""),
                })
        compiled_quality_requirements = [
            str(item.get("text") or "")
            for item in (compiled_context.get("constraints") or [])
            if item.get("text") and item.get("kind") in {"task", "manual", "preference"}
        ][:6]
        payload = OnlineImageRequest(
            prompt=prompt,
            provider_id=str(arguments.get("provider_id") or execution.context.get("image_provider_id") or execution.context.get("provider_id") or "comfly"),
            model=str(arguments.get("model") or execution.context.get("image_model") or ""),
            size=str(arguments.get("size") or execution.context.get("image_size") or execution.context.get("size") or "1024x1024"),
            quality=str(execution.context.get("quality") or "auto"),
            n=max(1, min(4, int(arguments.get("n") or 1))),
            reference_images=reference_images,
            project_id=project_id,
            canvas_id=str(execution.context.get("canvas_id") or ""),
            quality_gate=True,
            quality_requirements=list(dict.fromkeys([
                *[str(item) for item in (execution.context.get("quality_requirements") or []) if str(item).strip()],
                *compiled_quality_requirements,
            ]))[:6],
            quality_pass_threshold=float(execution.context.get("quality_pass_threshold") or 75),
            quality_max_retries=max(0, min(2, int(execution.context.get("quality_max_retries") or 1))),
            judge_provider=str(execution.context.get("judge_provider") or execution.context.get("provider") or ""),
            judge_model=str(execution.context.get("judge_model") or execution.context.get("model") or ""),
            context_compilation_id=str(compiled_context.get("id") or ""),
            compiled_constraints=[str(item.get("text") or "") for item in (compiled_context.get("constraints") or []) if item.get("text")],
        )
        domain_task = DOMAIN_STORE.create_generation_task(
            project_id,
            canvas_id=payload.canvas_id,
            source_node_id=payload.source_node_id,
            provider_id=payload.provider_id,
            model=payload.model,
            prompt=payload.prompt,
            parameters=payload.model_dump(mode="json"),
            inputs=[item.model_dump(mode="json") for item in payload.reference_images],
            root_task_id=payload.quality_root_task_id,
            parent_task_id=payload.quality_parent_task_id,
            attempt=payload.quality_attempt,
            context_compilation_id=payload.context_compilation_id,
        )
        DOMAIN_STORE.update_generation_task(domain_task["id"], "running")
        try:
            result = await build_online_image_result(payload, domain_task["id"])
        except Exception as exc:
            DOMAIN_STORE.update_generation_task(domain_task["id"], "failed", str(getattr(exc, "detail", exc)))
            raise
        evaluation = await evaluate_generation_task_quality(
            domain_task["id"],
            judge_provider=payload.judge_provider,
            judge_model=payload.judge_model,
            requirements_override=payload.quality_requirements,
            pass_threshold=payload.quality_pass_threshold,
        )
        quality_retries = []
        current_task = domain_task
        current_payload = payload
        while evaluation.get("status") == "failed" and len(quality_retries) < payload.quality_max_retries:
            next_attempt = int(current_task.get("attempt") or 1) + 1
            next_payload = current_payload.model_copy(update={
                "prompt": quality_retry_prompt(current_task.get("prompt") or current_payload.prompt, evaluation),
                "quality_attempt": next_attempt,
                "quality_root_task_id": domain_task["id"],
                "quality_parent_task_id": current_task["id"],
                "quality_max_retries": 0,
            })
            next_task = DOMAIN_STORE.create_generation_task(
                project_id,
                canvas_id=next_payload.canvas_id,
                source_node_id=next_payload.source_node_id,
                provider_id=next_payload.provider_id,
                model=next_payload.model,
                prompt=next_payload.prompt,
                parameters=next_payload.model_dump(mode="json"),
                inputs=[item.model_dump(mode="json") for item in next_payload.reference_images],
                root_task_id=domain_task["id"],
                parent_task_id=current_task["id"],
                attempt=next_attempt,
                context_compilation_id=next_payload.context_compilation_id,
            )
            DOMAIN_STORE.link_generation_retry(
                project_id, current_task["id"], next_task["id"],
                str(evaluation.get("feedback") or evaluation.get("verdict") or "质量门未通过"),
            )
            DOMAIN_STORE.update_generation_task(next_task["id"], "running")
            try:
                result = await build_online_image_result(next_payload, next_task["id"])
            except Exception as exc:
                DOMAIN_STORE.update_generation_task(next_task["id"], "failed", str(getattr(exc, "detail", exc)))
                raise
            evaluation = await evaluate_generation_task_quality(
                next_task["id"], judge_provider=next_payload.judge_provider,
                judge_model=next_payload.judge_model,
                requirements_override=next_payload.quality_requirements,
                pass_threshold=next_payload.quality_pass_threshold,
            )
            quality_retries.append(next_task["id"])
            current_task = next_task
            current_payload = next_payload
        image_urls = [str(url).strip() for url in (result.get("images") or []) if str(url).strip()]
        result["quality_evaluation"] = evaluation
        result["quality_retry_task_ids"] = quality_retries
        execution.state["image_result"] = result
        execution.state["image_urls"] = image_urls
        execution.state["generation_task_id"] = current_task["id"]
        execution.state["quality_evaluation"] = evaluation
        execution.state["quality_replay_root_task_id"] = domain_task["id"]
        execution.state["context_compilation_id"] = payload.context_compilation_id
        return {
            "count": len(image_urls),
            "image_urls": image_urls,
            "provider_id": payload.provider_id,
            "model": payload.model,
            "generation_task_id": current_task["id"],
            "quality_replay_root_task_id": domain_task["id"],
            "quality_status": evaluation.get("status"),
            "quality_score": evaluation.get("overall_score"),
            "quality_retry_task_ids": quality_retries,
            "context_compilation_id": payload.context_compilation_id,
            "applied_constraint_count": len(payload.compiled_constraints),
            "output_asset_ids": [item.get("asset_id") for item in ((result.get("domain") or {}).get("outputs") or [])],
        }

    async def create_canvas_tool(arguments, execution):
        existing = execution.state.get("canvas") or {}
        if existing.get("id"):
            return {"canvas_id": existing["id"], "canvas_title": existing.get("title", ""), "reused": True}
        project_id = resolve_project_id(
            str(arguments.get("project_id") or execution.state.get("project_id") or execution.context.get("project_id") or ""),
            str(execution.context.get("canvas_id") or ""),
        )
        title = str(arguments.get("title") or f"{display_title(execution.goal)} · 设计方案").strip()[:80]
        canvas = new_canvas(title=title, icon="sparkles", kind="smart", project_id=project_id)
        execution.state["project_id"] = project_id
        execution.state["canvas"] = canvas
        return {"canvas_id": canvas["id"], "canvas_title": canvas["title"], "project_id": project_id, "reused": False}

    async def append_canvas_tool(arguments, execution):
        canvas_id = str(arguments.get("canvas_id") or (execution.state.get("canvas") or {}).get("id") or execution.context.get("canvas_id") or "")
        if canvas_id:
            canvas = load_canvas(canvas_id)
        else:
            project_id = resolve_project_id(str(execution.state.get("project_id") or execution.context.get("project_id") or ""))
            canvas = new_canvas(title=f"{display_title(execution.goal)} · 设计方案", icon="sparkles", kind="smart", project_id=project_id)
        image_urls = [str(url).strip() for url in (arguments.get("image_urls") or execution.state.get("image_urls") or []) if str(url).strip()]
        if not image_urls:
            image_urls = [str(item.get("url") or "").strip() for item in (execution.state.get("library_items") or []) if str(item.get("url") or "").strip()]
        if not image_urls:
            raise HTTPException(status_code=400, detail="当前没有可插入智能画布的图片")
        existing_urls = {
            str(image.get("url") or "")
            for node in (canvas.get("nodes") or [])
            for image in (node.get("images") or [])
        }
        new_urls = [url for url in image_urls if url not in existing_urls]
        if new_urls:
            item_by_url = {
                str(item.get("url") or ""): item
                for item in (arguments.get("items") or [])
                if isinstance(item, dict) and item.get("url")
            }
            node = create_smart_canvas_image_node(
                [
                    {
                        "url": url,
                        "filename": str((item_by_url.get(url) or {}).get("filename") or os.path.basename(urllib.parse.urlparse(url).path) or "agent-output"),
                        "asset_id": str((item_by_url.get(url) or {}).get("asset_id") or ""),
                    }
                    for url in new_urls
                ],
                x=120,
                y=120 + len(canvas.get("nodes") or []) * 80,
            )
            canvas.setdefault("nodes", []).append(node)
            canvas.setdefault("connections", [])
            save_canvas(canvas)
        execution.state["canvas"] = canvas
        return {"canvas_id": canvas["id"], "canvas_title": canvas["title"], "inserted_count": len(new_urls)}

    async def read_canvas_tool(arguments, execution):
        canvas_id = str(
            arguments.get("canvas_id")
            or (execution.state.get("canvas") or {}).get("id")
            or execution.context.get("canvas_id")
            or ""
        ).strip()
        if not canvas_id:
            raise HTTPException(status_code=400, detail="读取画布需要 canvas_id")
        canvas = load_canvas(canvas_id)
        project_id = resolve_project_id(
            str(arguments.get("project_id") or execution.context.get("project_id") or ""),
            canvas_id,
        )
        if str(canvas.get("project_id") or "") != project_id:
            raise HTTPException(status_code=403, detail="画布不属于当前项目")
        selected_ids = [str(item).strip() for item in (arguments.get("selected_node_ids") or []) if str(item).strip()]
        if not selected_ids and execution.context.get("selected_node_id"):
            selected_ids = [str(execution.context.get("selected_node_id"))]
        nodes = canvas.get("nodes") or []
        selected_set = set(selected_ids)
        selected_nodes = [node for node in nodes if str(node.get("id") or "") in selected_set]
        execution.state["project_id"] = project_id
        execution.state["canvas"] = canvas
        execution.state["selected_canvas_nodes"] = selected_nodes
        return {
            "project_id": project_id,
            "canvas_id": canvas["id"],
            "canvas_title": canvas.get("title", ""),
            "node_count": len(nodes),
            "connection_count": len(canvas.get("connections") or []),
            "selected_node_ids": [str(node.get("id") or "") for node in selected_nodes],
            "nodes": [
                {
                    "id": str(node.get("id") or ""),
                    "type": str(node.get("type") or ""),
                    "title": str(node.get("title") or ""),
                    "image_count": len(node.get("images") or []),
                    "image_urls": [str(image.get("url") or "") for image in (node.get("images") or []) if image.get("url")][:12],
                }
                for node in nodes[:40]
            ],
        }

    async def save_canvas_images_tool(arguments, execution):
        canvas_id = str(arguments.get("canvas_id") or (execution.state.get("canvas") or {}).get("id") or execution.context.get("canvas_id") or "").strip()
        if not canvas_id:
            raise HTTPException(status_code=400, detail="画布结果回存需要 canvas_id")
        canvas = load_canvas(canvas_id)
        project_id = resolve_project_id(
            str(arguments.get("project_id") or execution.state.get("project_id") or execution.context.get("project_id") or ""),
            canvas_id,
        )
        if str(canvas.get("project_id") or "") != project_id:
            raise HTTPException(status_code=403, detail="画布不属于当前项目")
        selected_ids = [str(item).strip() for item in (arguments.get("node_ids") or []) if str(item).strip()]
        if not selected_ids and execution.context.get("selected_node_id"):
            selected_ids = [str(execution.context.get("selected_node_id"))]
        selected_set = set(selected_ids)
        node_items = [node for node in (canvas.get("nodes") or []) if str(node.get("id") or "") in selected_set]
        canvas_images = [
            {**image, "node_id": str(node.get("id") or "")}
            for node in node_items
            for image in (node.get("images") or [])
            if image.get("url")
        ]
        requested_urls = [str(item).strip() for item in (arguments.get("image_urls") or []) if str(item).strip()]
        if requested_urls:
            requested_set = set(requested_urls)
            canvas_images = [image for image in canvas_images if str(image.get("url") or "") in requested_set]
        if not canvas_images:
            raise HTTPException(status_code=400, detail="当前选中画布节点没有可回存图片")
        urls = list(dict.fromkeys(str(image.get("url") or "") for image in canvas_images))
        imported = import_urls_into_library(
            urls=urls,
            items=canvas_images,
            source_name="智能画布",
            canvas_id=canvas_id,
            canvas_title=str(canvas.get("title") or ""),
            node_id=selected_ids[0] if len(selected_ids) == 1 else "",
            manual_tags=[str(canvas.get("title") or "智能画布")],
            project_id=project_id,
        )
        execution.state["project_id"] = project_id
        execution.state["canvas"] = canvas
        execution.state["import_result"] = imported
        return {
            "project_id": project_id,
            "canvas_id": canvas_id,
            "processed_count": len(urls),
            "saved_count": int(imported.get("count") or 0),
            "library_image_ids": [item.get("id") for item in (imported.get("imported") or [])],
            "skipped": imported.get("skipped") or [],
        }

    async def save_output_tool(arguments, execution):
        image_urls = [str(url).strip() for url in (arguments.get("image_urls") or execution.state.get("image_urls") or []) if str(url).strip()]
        project_id = resolve_project_id(
            str(arguments.get("project_id") or execution.state.get("project_id") or execution.context.get("project_id") or ""),
            str((execution.state.get("canvas") or {}).get("id") or execution.context.get("canvas_id") or ""),
        )
        imported = None
        if image_urls:
            imported = import_urls_into_library(
                urls=image_urls,
                source_name="设计 Agent",
                categories=["设计Agent"],
                manual_tags=[display_title(execution.goal), "设计Agent"],
                project_id=project_id,
                canvas_id=str((execution.state.get("canvas") or {}).get("id") or execution.context.get("canvas_id") or ""),
            )
            execution.state["import_result"] = imported
        canvas = execution.state.get("canvas") or {}
        context_canvas_id = str(execution.context.get("canvas_id") or "").strip()
        if image_urls and not canvas.get("id") and context_canvas_id:
            candidate = load_canvas(context_canvas_id)
            if str(candidate.get("project_id") or "") == project_id:
                canvas = candidate
                execution.state["canvas"] = canvas
        if image_urls and not canvas.get("id"):
            canvas = new_canvas(title=f"{display_title(execution.goal)} · 设计方案", icon="sparkles", kind="smart", project_id=project_id)
            execution.state["canvas"] = canvas
        if image_urls and canvas.get("id"):
            imported_by_source = {
                str(item.get("source_url") or ""): item
                for item in ((imported or {}).get("imported") or [])
                if item.get("source_url")
            }
            canvas_items = [
                {
                    "url": url,
                    "filename": str((imported_by_source.get(url) or {}).get("filename") or os.path.basename(urllib.parse.urlparse(url).path) or "agent-output"),
                    "asset_id": str((imported_by_source.get(url) or {}).get("asset_id") or ""),
                }
                for url in image_urls
            ]
            await append_canvas_tool({"canvas_id": canvas["id"], "image_urls": image_urls, "items": canvas_items}, execution)
            canvas = execution.state.get("canvas") or canvas
        return {
            "project_id": project_id,
            "saved_image_count": int((imported or {}).get("count") or 0),
            "library_image_ids": [item.get("id") for item in ((imported or {}).get("imported") or [])],
            "canvas_id": canvas.get("id", ""),
            "canvas_title": canvas.get("title", ""),
            "brief_saved": bool(execution.state.get("wiki_page")),
        }

    async def link_project_output_tool(arguments, execution):
        project_id = resolve_project_id(
            str(arguments.get("project_id") or execution.state.get("project_id") or execution.context.get("project_id") or ""),
            str((execution.state.get("canvas") or {}).get("id") or execution.context.get("canvas_id") or ""),
        )
        workspace = DOMAIN_STORE.project_workspace(project_id, limit=8)
        execution.state["project_id"] = project_id
        execution.state["project_output"] = {
            "counts": workspace.get("counts") or {},
            "canvas_id": (execution.state.get("canvas") or {}).get("id", ""),
            "wiki_page_id": (execution.state.get("wiki_page") or {}).get("id", ""),
            "image_urls": execution.state.get("image_urls") or [],
            "context_compilation_id": execution.state.get("context_compilation_id") or "",
        }
        return execution.state["project_output"]

    object_schema = lambda properties=None, required=None: {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }
    registry.register(ToolSpec(
        "get_project_context", "读取当前项目、画布、素材与生成任务摘要。",
        object_schema({"project_id": {"type": "string"}}), get_project_context_tool,
        permissions=["project:read"], scopes=["home", "library", "smart-canvas", "wiki"],
    ))
    registry.register(ToolSpec(
        "search_wiki_context", "检索当前 Wiki/本地知识库，结果会供后续简报工具使用。",
        object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]), search_wiki_tool,
        permissions=["wiki:read"], scopes=["home", "wiki", "library", "smart-canvas"],
    ))
    registry.register(ToolSpec(
        "write_wiki_qa", "将已经生成的问答内容写入当前 Wiki，并返回持久化页面标识。",
        object_schema({
            "question": {"type": "string"},
            "answer": {"type": "string"},
            "title": {"type": "string"},
            "related_ids": {"type": "array", "items": {"type": "string"}},
        }, ["question", "answer"]), write_wiki_qa_tool, writes=True,
        permissions=["wiki:write"], scopes=["home", "wiki"],
    ))
    registry.register(ToolSpec(
        "write_agent_report", "将研究、总结或学习结果写入当前 Wiki，并保留项目与来源关联。",
        object_schema({
            "title": {"type": "string"},
            "content": {"type": "string"},
            "related_ids": {"type": "array", "items": {"type": "string"}},
        }, ["title", "content"]), write_agent_report_tool, writes=True,
        permissions=["wiki:write"], scopes=["home", "wiki"],
    ))
    registry.register(ToolSpec(
        "list_library_images", "搜索资源库图片，结果可作为设计参考或画布输入。",
        object_schema({"query": {"type": "string"}, "project_id": {"type": "string"}, "limit": {"type": "integer"}}), search_library_tool,
        permissions=["library:read"], scopes=["home", "library", "smart-canvas"],
    ))
    registry.register(ToolSpec(
        "tag_library_images", "在当前项目可见范围内调用指定模型，为资源库图片写入分类和标签。",
        object_schema({
            "image_ids": {"type": "array", "items": {"type": "string"}},
            "project_id": {"type": "string"},
            "provider": {"type": "string"},
            "model": {"type": "string"},
        }, ["image_ids"]), tag_library_tool, writes=True,
        permissions=["library:write", "generation:run"], scopes=["library"],
    ))
    registry.register(ToolSpec(
        "generate_design_brief", "根据目标、Wiki 和素材结果生成设计简报/提示词并写入 Wiki。",
        object_schema({"focus": {"type": "string"}}), generate_brief_tool, writes=True,
        permissions=["wiki:write"], scopes=["home", "wiki", "library", "smart-canvas"],
    ))
    registry.register(ToolSpec(
        "generate_design_image", "使用设计简报或指定提示词调用现有生图能力。",
        object_schema({"prompt": {"type": "string"}, "provider_id": {"type": "string"}, "model": {"type": "string"}, "size": {"type": "string"}, "n": {"type": "integer"}}), generate_image_tool, writes=True,
        permissions=["generation:run"], scopes=["home", "library", "smart-canvas"],
    ))
    registry.register(ToolSpec(
        "create_smart_canvas", "为当前项目创建智能画布。",
        object_schema({"title": {"type": "string"}, "project_id": {"type": "string"}}), create_canvas_tool, writes=True,
        permissions=["canvas:write"], scopes=["home", "library", "smart-canvas"],
    ))
    registry.register(ToolSpec(
        "append_images_to_smart_canvas", "将生成图或资源库图片插入指定智能画布。",
        object_schema({"canvas_id": {"type": "string"}, "image_urls": {"type": "array", "items": {"type": "string"}}}), append_canvas_tool, writes=True,
        permissions=["canvas:write"], scopes=["home", "library", "smart-canvas"],
    ))
    registry.register(ToolSpec(
        "read_smart_canvas", "读取当前项目智能画布的节点、连接和选中节点摘要。",
        object_schema({
            "canvas_id": {"type": "string"},
            "project_id": {"type": "string"},
            "selected_node_ids": {"type": "array", "items": {"type": "string"}},
        }, ["canvas_id"]), read_canvas_tool,
        permissions=["canvas:read", "project:read"], scopes=["smart-canvas"],
    ))
    registry.register(ToolSpec(
        "save_canvas_node_images_to_library", "将当前项目画布中明确选中的节点图片回存资源库。",
        object_schema({
            "canvas_id": {"type": "string"},
            "project_id": {"type": "string"},
            "node_ids": {"type": "array", "items": {"type": "string"}},
            "image_urls": {"type": "array", "items": {"type": "string"}},
        }, ["canvas_id"]), save_canvas_images_tool, writes=True,
        permissions=["canvas:read", "library:write", "project:write"], scopes=["smart-canvas"],
    ))
    registry.register(ToolSpec(
        "save_design_output", "将已有简报和图片回存资源库、智能画布和当前项目。",
        object_schema({"project_id": {"type": "string"}, "image_urls": {"type": "array", "items": {"type": "string"}}}), save_output_tool, writes=True,
        permissions=["library:write", "canvas:write", "project:write"], scopes=["home", "library", "smart-canvas", "wiki"],
    ))
    registry.register(ToolSpec(
        "link_project_output", "核对并返回项目产物、画布和素材的关联摘要。",
        object_schema({"project_id": {"type": "string"}}), link_project_output_tool,
        permissions=["project:read"], scopes=["home", "library", "smart-canvas"],
    ))
    return registry


async def run_tool_calling_agent_task(task_id: str, task: Dict[str, Any]) -> Dict[str, Any]:
    context = dict(task.get("context") or {})
    goal = str(task.get("goal") or context.get("goal") or "设计出图任务").strip()
    plan = task.get("plan") or {}
    allowed_tools = list(plan.get("tool_ids") or [])
    max_steps = max(1, min(12, int(plan.get("max_steps") or 9)))
    registry = build_design_agent_tool_registry()
    skill_ids = list(plan.get("skill_ids") or [])
    active_skills = AGENT_SKILLS.resolve(
        task_type=str(task.get("task_type") or plan.get("task_type") or ""),
        scope=str(plan.get("page_role") or task.get("page_role") or ""),
        requested_ids=skill_ids,
    )
    if skill_ids and not active_skills:
        raise AgentKernelError("计划要求的 Skill 不可用，已按安全策略停止执行")
    context["active_skills"] = [AGENT_SKILLS.public_record(skill, include_instructions=True) for skill in active_skills]
    skill_allowed_tools = {
        tool_name
        for skill in active_skills
        for tool_name in (skill.get("allowed_tools") or [])
    }
    if skill_allowed_tools:
        allowed_tools = [tool_name for tool_name in allowed_tools if tool_name in skill_allowed_tools]
    if active_skills and not allowed_tools:
        raise AgentKernelError("Skill 授权后没有剩余可执行工具，已按安全策略停止执行")
    mcp_server_ids = sorted({
        server_id
        for skill in active_skills
        for server_id in (skill.get("mcp_servers") or [])
    })
    mcp_tools = []
    mcp_error = ""
    if mcp_server_ids:
        try:
            mcp_tools = await MCP_GATEWAY.register_tools(registry, mcp_server_ids)
        except Exception as exc:
            mcp_error = str(exc)
            agent_task_progress(
                task_id,
                step_id="mcp_gateway",
                message=f"MCP 暂不可用，Agent 将使用原生工具继续：{mcp_error}",
                progress_current=0,
                progress_total=max_steps,
                event_type="mcp_unavailable",
            )

    async def planner(payload):
        return await plan_agent_kernel_action(payload, context)

    def on_event(event):
        step = int(event.get("step") or 0)
        event_type = str(event.get("type") or "agent_event")
        tool_name = str(event.get("tool") or "")
        agent_task_progress(
            task_id,
            step_id=tool_name,
            message=str(event.get("message") or "Agent 正在执行..."),
            progress_current=min(step, max_steps),
            progress_total=max_steps,
            event_type=event_type,
        )

    loop = AgentLoop(registry, planner, max_steps=max_steps, max_consecutive_errors=3)
    confirmed = bool(task.get("confirmed_at"))
    manifests = registry.manifest(allowed_tools)
    granted_permissions = {
        permission
        for manifest in manifests
        if confirmed or not manifest.get("writes")
        for permission in (manifest.get("permissions") or [])
    }
    knowledge_task = str(task.get("task_type") or plan.get("task_type") or "") in {"wiki_task", "work_task"}
    completion_contract = [
        "读取当前项目上下文",
        "检索 Wiki 并保留真实来源",
        "将研究、总结、学习或问答结果写入 Wiki",
        "返回可追溯的项目和 Wiki 页面标识",
    ] if knowledge_task else [
        "读取当前项目上下文",
        "编译并采用当前项目的设计约束与参考素材",
        "根据任务按需搜索 Wiki 和素材库",
        "生成并保存设计简报",
        "尝试生图，失败时保留可操作的错误原因",
        "将可用图片保存到资源库和智能画布",
        "返回可追溯的项目产物摘要",
    ]
    loop_result = await loop.run(
        goal,
        context,
        allowed_tools=allowed_tools,
        allow_writes=confirmed or not bool(plan.get("requires_confirmation")),
        granted_permissions=granted_permissions,
        current_scope=str(plan.get("page_role") or task.get("page_role") or "home"),
        is_cancelled=lambda: agent_task_cancelled(task_id),
        on_event=on_event,
        completion_contract=completion_contract,
        required_tools=list(plan.get("required_tools") or []),
    )
    if loop_result.get("status") == "cancelled":
        return load_agent_task(AGENT_TASK_DIR, task_id)
    state = loop_result.get("state") or {}
    canvas = state.get("canvas") or {}
    wiki_page = state.get("wiki_page") or {}
    import_result = state.get("import_result") or {}
    result = {
        "kind": "tool_calling_agent_output",
        "runtime": "tool_calling_v1",
        "goal": goal,
        "answer": loop_result.get("answer") or "Agent 任务已完成。",
        "executed_tools": [item.get("tool") for item in (loop_result.get("history") or [])],
        "tool_history": loop_result.get("history") or [],
        "brief": (state.get("prompt_pack") or {}).get("brief", ""),
        "prompt": (state.get("prompt_pack") or {}).get("positive", ""),
        "preference_profile": state.get("preference_profile") or {},
        "context_compilation_id": state.get("context_compilation_id") or "",
        "applied_constraints": (state.get("compiled_context") or {}).get("constraints") or [],
        "context_sources": (state.get("compiled_context") or {}).get("sources") or [],
        "wiki_page_id": wiki_page.get("id", ""),
        "wiki_page_title": wiki_page.get("title", ""),
        "wiki_related_ids": state.get("wiki_related_ids") or agent_related_ids_from_matches(state.get("wiki_matches") or []),
        "image_urls": state.get("image_urls") or [],
        "generation_task_id": state.get("generation_task_id") or "",
        "output_asset_ids": [
            item.get("asset_id")
            for item in (((state.get("image_result") or {}).get("domain") or {}).get("outputs") or [])
            if item.get("asset_id")
        ],
        "imported_count": int(import_result.get("count") or 0),
        "canvas_id": canvas.get("id", ""),
        "canvas_title": canvas.get("title", ""),
        "canvas_open_url": f"/static/smart-canvas.html?id={urllib.parse.quote(canvas.get('id', ''))}" if canvas.get("id") else "",
        "library_open_url": "/static/library.html?v=20260720-agent-runtime-v1",
        "project_id": state.get("project_id") or context.get("project_id") or "",
        "project_open_url": f"/static/project-workbench.html?project_id={urllib.parse.quote(str(state.get('project_id') or context.get('project_id') or ''))}",
        "skill_ids": [skill.get("id") for skill in active_skills],
        "mcp_servers": mcp_server_ids,
        "mcp_tools": [tool.get("name") for tool in mcp_tools],
        "mcp_error": mcp_error,
        "completion": {
            "status": loop_result.get("status") or "succeeded",
            "missing_tools": loop_result.get("missing_tools") or [],
        },
    }
    with AGENT_TASK_LOCK:
        if loop_result.get("status") == "partial":
            return agent_update_task_partial(
                AGENT_TASK_DIR,
                task_id,
                result=result,
                message="Agent 已保留可用产物，并明确记录未完成项。",
            )
        return agent_update_task_result(
            AGENT_TASK_DIR,
            task_id,
            result=result,
            message="Agent Runtime v1 已完成动态工具调用任务。",
        )

async def run_design_agent_task(task_id: str, task: Dict[str, Any]) -> Dict[str, Any]:
    context = task.get("context") or {}
    goal = str(task.get("goal") or context.get("goal") or "设计出图任务").strip()
    agent_task_progress(
        task_id,
        step_id="search_wiki_context",
        message="正在检索 Wiki 与资源库上下文...",
        progress_current=0,
        progress_total=4,
        event_type="step",
    )
    if agent_task_cancelled(task_id):
        return load_agent_task(AGENT_TASK_DIR, task_id)

    wiki_matches = agent_wiki_search_items(goal, context, limit=AGENT_WIKI_CONTEXT_LIMIT)
    library_resolved = resolve_library_agent_matches(context, limit=8, enrich=True)
    library_items = library_resolved.get("items") or []

    agent_task_progress(
        task_id,
        step_id="generate_design_brief",
        message="正在生成设计简报和生图提示词...",
        progress_current=1,
        progress_total=4,
        event_type="step",
    )
    prompt_pack = build_design_prompt(goal, wiki_matches, library_items, build_project_preference_profile(str(context.get("project_id") or "")))
    related_ids = agent_related_ids_from_matches(wiki_matches)
    design_page = agent_create_wiki_output_page(
        context,
        "design",
        f"{display_title(goal)} 设计简报",
        prompt_pack["brief"],
        related_ids=related_ids,
    )
    if agent_task_cancelled(task_id):
        return load_agent_task(AGENT_TASK_DIR, task_id)

    agent_task_progress(
        task_id,
        step_id="generate_design_image",
        message="正在调用在线生图接口...",
        progress_current=2,
        progress_total=4,
        event_type="step",
    )
    image_result = None
    import_result = None
    canvas = None
    image_error = ""
    image_urls: List[str] = []
    try:
        image_payload = OnlineImageRequest(
            prompt=prompt_pack["positive"],
            provider_id=str(context.get("image_provider_id") or context.get("provider_id") or "").strip() or "comfly",
            model=str(context.get("image_model") or "").strip(),
            size=str(context.get("image_size") or context.get("size") or "1024x1024").strip() or "1024x1024",
            quality=str(context.get("quality") or "auto").strip() or "auto",
            reference_images=[],
        )
        image_result = await build_online_image_result(image_payload)
        image_urls = [str(url).strip() for url in (image_result.get("images") or []) if str(url).strip()]
    except Exception as exc:
        image_error = getattr(exc, "detail", None) or str(exc)

    agent_task_progress(
        task_id,
        step_id="save_design_output",
        message="正在保存设计产物...",
        progress_current=3,
        progress_total=4,
        event_type="step",
    )
    if image_urls:
        try:
            import_result = import_urls_into_library(
                urls=image_urls,
                source_name="设计 Agent",
                categories=["设计Agent"],
                manual_tags=[display_title(goal), "设计Agent"],
            )
            imported = import_result.get("imported") or []
            if imported:
                canvas = new_canvas(title=f"{display_title(goal)} · 设计方案", icon="sparkles", kind="smart")
                node = create_smart_canvas_image_node([enrich_library_image_record(img) for img in imported], x=120, y=120)
                canvas["nodes"] = [node]
                canvas["connections"] = []
                canvas["settings"] = canvas.get("settings") or {}
                save_canvas(canvas)
        except Exception as exc:
            image_error = image_error or (getattr(exc, "detail", None) or str(exc))

    agent_task_progress(
        task_id,
        step_id="save_design_output",
        message="设计 Agent 产物已保存。",
        progress_current=4,
        progress_total=4,
    )
    result = {
        "kind": "design_agent_output",
        "goal": goal,
        "prompt": prompt_pack["positive"],
        "negative_prompt": prompt_pack["negative"],
        "brief": prompt_pack["brief"],
        "wiki_page_id": design_page.get("id"),
        "wiki_page_title": design_page.get("title"),
        "wiki_match_count": len(wiki_matches),
        "library_match_count": len(library_items),
        "image_urls": image_urls,
        "image_error": image_error,
        "imported_count": int((import_result or {}).get("count") or 0),
        "library_open_url": "/static/library.html?v=20260603-prompt-library-shell",
        "canvas_id": (canvas or {}).get("id", ""),
        "canvas_title": (canvas or {}).get("title", ""),
        "canvas_open_url": f"/static/smart-canvas.html?id={urllib.parse.quote((canvas or {}).get('id', ''))}" if canvas else "",
        "online_open_url": "/static/online.html?v=20260512-i18n2",
    }
    with AGENT_TASK_LOCK:
        return agent_update_task_result(
            AGENT_TASK_DIR,
            task_id,
            result=result,
            message="设计 Agent 已完成：简报已写入 Wiki，图片结果按可用情况保存。",
        )

async def run_work_or_wiki_agent_task(task_id: str, task: Dict[str, Any]) -> Dict[str, Any]:
    context = task.get("context") or {}
    goal = str(task.get("goal") or context.get("goal") or "知识库任务").strip()
    task_type = str(task.get("task_type") or "")
    mode = str(task.get("mode") or context.get("mode") or "")
    local_root = agent_local_wiki_root(context)
    agent_task_progress(
        task_id,
        step_id="search_wiki_context",
        message="正在检索本地知识库..." if local_root else "正在检索 LLM Wiki...",
        progress_current=0,
        progress_total=2,
        event_type="step",
    )
    if agent_task_cancelled(task_id):
        return load_agent_task(AGENT_TASK_DIR, task_id)
    wiki_matches = agent_wiki_search_items(goal, context, limit=AGENT_WIKI_CONTEXT_LIMIT)
    local_config = local_wiki_read_machine_json(local_root, "config.json", {}) if local_root else {}
    answer, snippets, related_ids, answer_meta = await answer_from_wiki_context(
        goal,
        wiki_matches,
        use_llm=True,
        provider=str(context.get("provider") or ""),
        model=str(context.get("model") or ""),
        local_only=bool(local_config.get("local_only")) and not (context.get("provider") or context.get("model")),
    )

    if task_type == "wiki_task" or mode in {"research", "deep", "learn", "wiki"}:
        agent_task_progress(
            task_id,
            step_id="write_wiki_qa",
            message="正在生成知识库问答档案...",
            progress_current=1,
            progress_total=2,
            event_type="step",
        )
        source_lines = [f"- [[{item['id']}]] {item['title']}" for item in snippets if item.get("id")]
        page = agent_create_wiki_output_page(
            context,
            "qa",
            display_title(goal),
            "\n".join([
                "## 问题",
                goal,
                "",
                "## 回答",
                answer,
                "",
                "## 引用",
                *(source_lines or ["- 当前没有可引用的本地来源。"]),
            ]),
            related_ids=related_ids,
        )
        kind = "wiki_qa_created"
        message = "知识库问答档案已生成。"
    else:
        agent_task_progress(
            task_id,
            step_id="write_agent_report",
            message="正在生成工作报告...",
            progress_current=1,
            progress_total=2,
            event_type="step",
        )
        page = agent_create_wiki_output_page(
            context,
            "report",
            f"{display_title(goal)} 工作报告",
            "\n".join([
                "## 任务目标",
                goal,
                "",
                "## 本地知识依据",
                answer,
                "",
                "## 建议动作",
                "- 把相关来源继续补齐到 Wiki。",
                "- 根据报告结论拆分下一步可执行 Agent 任务。",
                "- 如涉及设计产物，可切换到设计模式继续生图或送入智能画布。",
            ]),
            related_ids=related_ids,
        )
        kind = "wiki_report_created"
        message = "工作报告已生成。"

    if agent_task_cancelled(task_id):
        return load_agent_task(AGENT_TASK_DIR, task_id)
    agent_task_progress(
        task_id,
        step_id="write_wiki_qa" if kind == "wiki_qa_created" else "write_agent_report",
        message=message,
        progress_current=2,
        progress_total=2,
    )
    result = {
        "kind": kind,
        "goal": goal,
        "answer": answer,
        "matches": snippets,
        "wiki_page_id": page.get("id"),
        "wiki_page_title": page.get("title"),
        "wiki_open_url": f"/static/wiki.html?page={urllib.parse.quote(page.get('id', ''))}&v=20260608-obsidian-vault",
        "local_wiki_root": local_root,
        "used_llm": bool(answer_meta.get("used_llm")),
        "llm_model": answer_meta.get("model", ""),
        "llm_error": answer_meta.get("llm_error", ""),
    }
    with AGENT_TASK_LOCK:
        return agent_update_task_result(
            AGENT_TASK_DIR,
            task_id,
            result=result,
            message=message,
        )


async def execute_agent_task(task_id: str):
    current_run = asyncio.current_task()
    if current_run:
        AGENT_RUN_TASKS[task_id] = current_run
    try:
        with AGENT_TASK_LOCK:
            task = load_agent_task(AGENT_TASK_DIR, task_id)
        plan = task.get("plan") or {}
        page_role = str(plan.get("page_role") or task.get("page_role") or "")
        context = task.get("context") or {}
        tool_ids = plan.get("tool_ids") or []
        task_type = str(task.get("task_type") or plan.get("task_type") or "")

        if plan.get("runtime") == "tool_calling_v1":
            await run_tool_calling_agent_task(task_id, task)
            return
        if page_role == "library" and "create_smart_canvas" in tool_ids and "append_images_to_smart_canvas" in tool_ids:
            await run_library_to_smart_canvas_task(task_id, context)
            return
        if page_role == "library" and "tag_library_images" in tool_ids:
            await run_library_batch_tag_task(task_id, context)
            return
        if page_role == "smart-canvas" and "save_canvas_node_images_to_library" in tool_ids:
            await run_smart_canvas_to_library_task(task_id, context)
            return
        if task_type == "design_task":
            await run_design_agent_task(task_id, task)
            return
        if task_type in {"work_task", "wiki_task"} and "search_wiki_context" in tool_ids:
            await run_work_or_wiki_agent_task(task_id, task)
            return

        raise HTTPException(status_code=400, detail="当前任务类型还没有接入执行能力")
    except asyncio.CancelledError:
        with AGENT_TASK_LOCK:
            try:
                if not agent_task_is_cancelled(AGENT_TASK_DIR, task_id):
                    agent_cancel_task(AGENT_TASK_DIR, task_id)
            except FileNotFoundError:
                pass
    except HTTPException as exc:
        with AGENT_TASK_LOCK:
            agent_fail_task(AGENT_TASK_DIR, task_id, str(exc.detail))
    except Exception as exc:
        with AGENT_TASK_LOCK:
            agent_fail_task(AGENT_TASK_DIR, task_id, f"执行失败：{exc}")
    finally:
        if AGENT_RUN_TASKS.get(task_id) is current_run:
            AGENT_RUN_TASKS.pop(task_id, None)

@app.get("/api/agent/tools")
async def agent_tools():
    registry = build_design_agent_tool_registry()
    mcp_tools = []
    mcp_error = ""
    try:
        mcp_tools = await MCP_GATEWAY.register_tools(registry)
    except Exception as exc:
        mcp_error = str(exc)
    kernel_manifests = {item["name"]: item for item in registry.manifest()}
    tools = []
    for tool in list_agent_tools():
        record = dict(tool)
        manifest = kernel_manifests.get(str(tool.get("id") or ""))
        if manifest:
            record.update({
                "input_schema": manifest.get("input_schema") or {},
                "permissions": manifest.get("permissions") or [],
                "callable": True,
                "runtime": "tool_calling_v1",
            })
        else:
            record["callable"] = False
            record["runtime"] = "legacy_workflow"
        tools.append(record)
    return {
        "runtime": "tool_calling_v1",
        "tools": tools,
        "mcp_tool_count": len(mcp_tools),
        "mcp_error": mcp_error,
    }


@app.get("/api/agent/skills")
async def agent_skills():
    return {
        "skills": [AGENT_SKILLS.public_record(skill, include_instructions=False) for skill in AGENT_SKILLS.list(include_disabled=True)]
    }


@app.get("/api/agent/mcp/servers")
async def agent_mcp_servers():
    statuses = []
    for server in MCP_GATEWAY.list_servers():
        statuses.append(await MCP_GATEWAY.health(server["id"]))
    return {"servers": statuses}


@app.post("/api/agent/mcp/servers/{server_id}/test")
async def agent_mcp_server_test(server_id: str):
    status = await MCP_GATEWAY.health(server_id)
    if status.get("status") != "connected":
        raise HTTPException(status_code=502, detail=status.get("error") or "MCP Server 连接失败")
    return status


@app.get("/api/agent/capabilities")
async def agent_capabilities():
    skills = [AGENT_SKILLS.public_record(skill, include_instructions=False) for skill in AGENT_SKILLS.list()]
    servers = []
    for server in MCP_GATEWAY.list_servers():
        servers.append(await MCP_GATEWAY.health(server["id"]))
    return {
        "runtime": "tool_calling_v1",
        "skills": skills,
        "mcp_servers": servers,
        "connected_mcp_servers": sum(1 for server in servers if server.get("status") == "connected"),
    }

@app.post("/api/agent/plan")
async def agent_plan(payload: AgentPlanRequest):
    agent_context = dict(payload.context or {})
    agent_context["goal"] = payload.goal
    agent_context["project_id"] = resolve_project_id(
        str(agent_context.get("project_id") or ""),
        str(agent_context.get("canvas_id") or ""),
    )
    with AGENT_TASK_LOCK:
        task = create_plan_task(
            AGENT_TASK_DIR,
            goal=payload.goal,
            page=payload.page,
            context=agent_context,
        )
        plan = task.get("plan") or {}
        active_skills = AGENT_SKILLS.resolve(
            task_type=str(task.get("task_type") or plan.get("task_type") or ""),
            scope=str(task.get("page_role") or plan.get("page_role") or ""),
            requested_ids=list(plan.get("skill_ids") or []),
        )
        agent_context["active_skills"] = [
            AGENT_SKILLS.public_record(skill, include_instructions=True)
            for skill in active_skills
        ]
        task = agent_update_task_plan(AGENT_TASK_DIR, task["id"], context=agent_context)
        task = decorate_agent_task_preview(AGENT_TASK_DIR, task)
    return task

@app.post("/api/agent/run")
async def agent_run(payload: AgentRunRequest, background_tasks: BackgroundTasks):
    try:
        with AGENT_TASK_LOCK:
            task = load_agent_task(AGENT_TASK_DIR, payload.task_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Agent 任务不存在")

    try:
        if task.get("status") == "running":
            raise HTTPException(status_code=409, detail="当前 Agent 任务正在执行中")
        if task.get("status") in {"succeeded", "partial", "failed", "cancelled"}:
            raise HTTPException(status_code=400, detail="当前 Agent 任务已结束，请重新生成计划")

        plan = task.get("plan") or {}
        if plan.get("requires_confirmation") and not task.get("confirmed_at"):
            expected_token = str(task.get("confirmation_token") or "")
            if not expected_token or str(payload.confirmation_token or "") != expected_token:
                raise HTTPException(status_code=403, detail="当前计划包含写入操作，需要从已展示的计划中显式确认后执行")
            with AGENT_TASK_LOCK:
                task = agent_confirm_task(AGENT_TASK_DIR, payload.task_id)

        merged_context = merge_agent_context(task, payload.context_overrides or {})
        task = agent_update_task_plan(
            AGENT_TASK_DIR,
            payload.task_id,
            context=merged_context,
        )
        task = decorate_agent_task_preview(AGENT_TASK_DIR, task)
        plan = task.get("plan") or {}
        if not plan.get("can_run", True):
            blockers = plan.get("blockers") or ["当前任务还不满足执行条件"]
            raise HTTPException(status_code=400, detail=blockers[0])
        task = agent_start_task(
            AGENT_TASK_DIR,
            payload.task_id,
            message="任务已进入执行队列。",
            progress_current=0,
            progress_total=0,
        )
        background_tasks.add_task(execute_agent_task, payload.task_id)
        return task
    except HTTPException as exc:
        raise exc


def agent_history_outputs(task: Dict[str, Any]) -> List[Dict[str, str]]:
    result = task.get("result") or {}
    if result.get("runtime") != "tool_calling_v1":
        return task.get("outputs") or []
    executed_tools = result.get("executed_tools") or []
    if executed_tools == ["read_smart_canvas"]:
        return [{"type": "canvas_summary", "label": "画布摘要"}]
    if executed_tools and set(executed_tools).issubset({
        "get_project_context",
        "mcp.project_reader.workspace_summary",
    }):
        return [{"type": "project_summary", "label": "项目摘要"}]
    outputs = []
    if result.get("wiki_page_id"):
        outputs.append({"type": "wiki", "label": "Wiki 页面"})
    if int(result.get("imported_count") or 0) > 0:
        outputs.append({"type": "library_images", "label": "资源库图片"})
    if result.get("canvas_id") and any(tool in executed_tools for tool in {
        "create_smart_canvas",
        "append_images_to_smart_canvas",
        "save_design_output",
    }):
        outputs.append({"type": "canvas", "label": "智能画布"})
    if "tag_library_images" in executed_tools:
        outputs.append({"type": "library_tags", "label": "资源库标签"})
    return outputs


@app.get("/api/agent/history")
async def agent_history(project_id: str = "", limit: int = 50):
    with AGENT_TASK_LOCK:
        tasks = list_agent_tasks(AGENT_TASK_DIR, project_id=project_id, limit=limit)
    return {
        "project_id": project_id,
        "tasks": [
            {
                "id": task.get("id", ""),
                "goal": task.get("goal", ""),
                "status": task.get("status", ""),
                "task_type": task.get("task_type", ""),
                "mode": task.get("mode", ""),
                "outputs": agent_history_outputs(task),
                "project_id": str((task.get("context") or {}).get("project_id") or (task.get("result") or {}).get("project_id") or ""),
                "created_at": task.get("created_at", 0),
                "updated_at": task.get("updated_at", 0),
            }
            for task in tasks
        ],
    }

@app.get("/api/agent/tasks/{task_id}")
async def agent_task_status(task_id: str):
    try:
        with AGENT_TASK_LOCK:
            task = load_agent_task(AGENT_TASK_DIR, task_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Agent 任务不存在")
    return task

@app.post("/api/agent/cancel/{task_id}")
async def agent_cancel(task_id: str):
    try:
        with AGENT_TASK_LOCK:
            task = agent_cancel_task(AGENT_TASK_DIR, task_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Agent 任务不存在")
    running_task = AGENT_RUN_TASKS.get(task_id)
    if running_task and not running_task.done():
        running_task.cancel()
    return task

@app.get("/api/view")
def view_image(filename: str, type: str = "input", subfolder: str = ""):
    # 先按原逻辑去各 ComfyUI 后端找
    for addr in COMFYUI_INSTANCES:
        try:
            url = f"http://{addr}/view"
            params = {"filename": filename, "type": type, "subfolder": subfolder}
            r = requests.get(url, params=params, timeout=1)
            if r.status_code == 200:
                return Response(content=r.content, media_type=r.headers.get('Content-Type'))
        except Exception:
            continue
    # 后端都拿不到时回退本地 assets/<input|output>/
    # 适用场景：画布通过 /api/ai/upload 把参考图直接落到本地 assets/input/，
    # 但 ComfyUI 的 input 可能因为重启/清理而丢失，导致 enhance/klein 等页面预览对比图 404
    if not subfolder and type in ("input", "output"):
        safe_name = os.path.basename(filename or "")
        if safe_name:
            local_path = output_path_for(safe_name, "input" if type == "input" else "output")
            if os.path.isfile(local_path):
                return FileResponse(local_path, media_type=content_type_for_path(local_path))
    raise HTTPException(status_code=404, detail="Image not found on any available backend")

@app.get("/api/download-output")
def download_output(url: str, name: str = ""):
    path = output_file_from_url(url)
    if not path:
        raise HTTPException(status_code=404, detail="文件不存在")
    filename = os.path.basename(name) if name else os.path.basename(path)
    return FileResponse(path, media_type=content_type_for_path(path), filename=filename)

@app.post("/api/upload")
async def upload_image(files: List[UploadFile] = File(...)):
    uploaded_files = []
    files_content = []
    for file in files:
        content = await file.read()
        files_content.append((file, content))

    for file, content in files_content:
        success_count = 0
        last_result = None
        for addr in COMFYUI_INSTANCES:
            try:
                files_data = {'image': (file.filename, content, file.content_type)}
                response = requests.post(f"http://{addr}/upload/image", files=files_data, timeout=5)
                if response.status_code == 200:
                    last_result = response.json()
                    success_count += 1
            except Exception as e:
                print(f"Upload error for {addr}: {e}")

        if success_count > 0 and last_result:
            uploaded_files.append({"comfy_name": last_result.get("name", file.filename)})
        else:
            raise HTTPException(status_code=500, detail="Failed to upload to any backend")

    return {"files": uploaded_files}

AI_REFERENCE_MAX_FILES = 20
AI_REFERENCE_MAX_BYTES = 30 * 1024 * 1024
AI_REFERENCE_MAX_PIXELS = 60_000_000
AI_REFERENCE_DIRECT_FORMATS = {
    "PNG": (".png", "image/png"),
    "JPEG": (".jpg", "image/jpeg"),
    "WEBP": (".webp", "image/webp"),
}


def normalize_ai_reference_image(content: bytes, original_name: str = ""):
    if not content:
        raise ValueError("文件为空")
    if len(content) > AI_REFERENCE_MAX_BYTES:
        raise ValueError("单张图片不能超过 30MB")
    try:
        with Image.open(BytesIO(content)) as probe:
            image_format = str(probe.format or "").upper()
            width, height = probe.size
            probe.verify()
        if width <= 0 or height <= 0 or width * height > AI_REFERENCE_MAX_PIXELS:
            raise ValueError("图片尺寸无效或像素总量超过 6000 万")
        if image_format in AI_REFERENCE_DIRECT_FORMATS:
            with Image.open(BytesIO(content)) as decoded:
                decoded.load()
            ext, media_type = AI_REFERENCE_DIRECT_FORMATS[image_format]
            return content, ext, media_type, width, height
        with Image.open(BytesIO(content)) as source:
            source.seek(0)
            normalized = ImageOps.exif_transpose(source)
            normalized.load()
            if normalized.mode not in ("RGB", "RGBA"):
                normalized = normalized.convert("RGBA" if "transparency" in normalized.info else "RGB")
            buffer = BytesIO()
            normalized.save(buffer, format="PNG", optimize=True)
            width, height = normalized.size
            return buffer.getvalue(), ".png", "image/png", width, height
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        ext = os.path.splitext(original_name or "")[1].lower()
        if ext in (".heic", ".heif"):
            raise ValueError("当前环境无法解析 HEIC/HEIF，请先导出为 JPG、PNG 或 WebP") from exc
        raise ValueError("文件内容不是可解析的图片，可能已经损坏") from exc


@app.post("/api/ai/upload")
async def upload_ai_reference(files: List[UploadFile] = File(...)):
    if len(files) > AI_REFERENCE_MAX_FILES:
        raise HTTPException(status_code=413, detail=f"一次最多上传 {AI_REFERENCE_MAX_FILES} 张图片")
    uploaded = []
    errors = []
    for file in files:
        display_name = os.path.basename(file.filename or "未命名图片")
        try:
            content = await file.read(AI_REFERENCE_MAX_BYTES + 1)
            normalized, ext, media_type, width, height = normalize_ai_reference_image(content, display_name)
            filename = f"ai_ref_{uuid.uuid4().hex[:12]}{ext}"
            path = output_path_for(filename, "input")
            with open(path, "wb") as target:
                target.write(normalized)
            uploaded.append({
                "url": output_url_for(filename, "input"),
                "name": display_name,
                "width": width,
                "height": height,
                "content_type": media_type,
            })
        except ValueError as exc:
            errors.append({"name": display_name, "message": str(exc)})
        except Exception:
            logging.exception("Failed to store smart-canvas upload: %s", display_name)
            errors.append({"name": display_name, "message": "服务器保存失败，请重试"})
        finally:
            await file.close()
    if not uploaded:
        raise HTTPException(status_code=415, detail={"message": "没有可用图片", "errors": errors})
    return {"files": uploaded, "errors": errors}

@app.get("/api/config")
async def ai_config():
    preferred_chat_model = next((m for m in CHAT_MODELS if m == "gpt-5.5"), CHAT_MODELS[0] if CHAT_MODELS else CHAT_MODEL)
    providers = [public_provider(p) for p in load_api_providers()]
    return {
        "base_url": AI_BASE_URL,
        "chat_model": preferred_chat_model,
        "image_model": IMAGE_MODEL,
        "chat_models": CHAT_MODELS,
        "image_models": IMAGE_MODELS,
        "video_models": VIDEO_MODELS,
        "api_providers": providers,
        "has_api_key": bool(AI_API_KEY),
        "ms_chat_models": MODELSCOPE_CHAT_MODELS,
        "has_ms_key": bool(MODELSCOPE_API_KEY),
    }

@app.get("/api/models")
async def ai_models():
    return {"chat_models": CHAT_MODELS, "image_models": IMAGE_MODELS, "video_models": VIDEO_MODELS}

@app.get("/api/providers")
async def api_providers():
    return {"providers": [public_provider(p) for p in load_api_providers()]}

@app.put("/api/providers")
async def save_providers(payload: List[ApiProviderPayload]):
    providers = []
    env_updates = {}
    # 收集每个 item 的 primary 字段
    raw_primary_flags = [bool(getattr(item, "primary", False)) for item in payload]
    for item in payload:
        provider = normalize_provider(item.dict(exclude={"api_key"}))
        if any(existing["id"] == provider["id"] for existing in providers):
            raise HTTPException(status_code=400, detail=f"API 平台 ID 重复：{provider['id']}")
        providers.append(provider)
        if item.api_key is not None:
            env_updates[provider_key_env(provider["id"])] = item.api_key.strip()
        if provider["id"] == "comfly":
            env_updates["COMFLY_BASE_URL"] = provider["base_url"]
            env_updates["IMAGE_MODELS"] = ",".join(provider["image_models"])
            env_updates["CHAT_MODELS"] = ",".join(provider["chat_models"])
            env_updates["VIDEO_MODELS"] = ",".join(provider.get("video_models") or [])
        if provider["id"] == "modelscope":
            env_updates["MODELSCOPE_CHAT_MODELS"] = ",".join(provider["chat_models"])
        if provider["id"] == "runninghub":
            env_updates["RUNNINGHUB_IMAGE_MODELS"] = ",".join(provider["image_models"])
        if provider["id"] == "volcengine":
            env_updates["VOLCENGINE_VIDEO_MODELS"] = ",".join(provider["video_models"])
            env_updates["VOLCENGINE_PROJECT_NAME"] = provider.get("volcengine_project_name") or VOLCENGINE_DEFAULT_PROJECT_NAME
            env_updates["VOLCENGINE_REGION"] = provider.get("volcengine_region") or VOLCENGINE_DEFAULT_REGION
    if not providers:
        raise HTTPException(status_code=400, detail="至少保留一个 API 平台")
    # 强制最多一个 primary（取最后被标记的；都没标记则保持原样不强制）
    primary_indices = [i for i, flag in enumerate(raw_primary_flags) if flag]
    if primary_indices:
        winner = primary_indices[-1]
        for i, p in enumerate(providers):
            p["primary"] = (i == winner)
    save_api_providers(providers)
    if env_updates:
        update_env_values(env_updates)
        reload_env_globals()   # 立即将最新 env 值同步回模块全局变量，无需重启
    return {"providers": [public_provider(p) for p in providers]}

# --- ModelScope Token (从 env 读取，不再支持通过 UI 修改) ---

@app.get("/api/config/token")
async def get_global_token():
    # 只返回配置状态。真实 Token 只在服务端使用，不再下发到浏览器。
    token = MODELSCOPE_API_KEY
    if os.path.exists(GLOBAL_CONFIG_FILE):
        try:
            with open(GLOBAL_CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                token = token or str(config.get("modelscope_token", "") or "")
        except:
            pass
    return {"token": "", "configured": bool(token), "key_preview": mask_secret(token)}

# --- 在线生图 (COMFLY) ---

class TestConnectionPayload(BaseModel):
    base_url: str = ""
    api_key: str = ""
    provider_id: str = ""
    protocol: str = "openai"

@app.post("/api/providers/test-connection")
async def test_provider_connection(payload: TestConnectionPayload):
    """测试请求地址是否可用：调上游 /v1/models。验证通过时同时把模型清单按类别返回，避免再调一次拉取接口。"""
    protocol = protocol_from_payload(payload)
    if protocol == "jimeng":
        installed = bool(shutil.which("dreamina"))
        return {
            "ok": installed,
            "status": 200 if installed else 0,
            "message": "已检测到即梦 dreamina CLI" if installed else "未检测到即梦 dreamina CLI，请先安装并登录。",
            "model_count": len(JIMENG_DEFAULT_IMAGE_MODELS) + len(JIMENG_DEFAULT_VIDEO_MODELS),
            "image_models": JIMENG_DEFAULT_IMAGE_MODELS,
            "chat_models": [],
            "video_models": JIMENG_DEFAULT_VIDEO_MODELS,
            "all": [*JIMENG_DEFAULT_IMAGE_MODELS, *JIMENG_DEFAULT_VIDEO_MODELS],
        }
    base_url = (payload.base_url or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail="请先填写请求地址")
    if not re.match(r"^https?://", base_url):
        raise HTTPException(status_code=400, detail="请求地址必须以 http:// 或 https:// 开头")
    api_key = (payload.api_key or "").strip()
    if not api_key and payload.provider_id:
        api_key = provider_env_key_value(payload.provider_id)
    if not api_key:
        raise HTTPException(status_code=400, detail="请先填写或保存 API Key")
    url = upstream_models_url(base_url, protocol)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=upstream_model_headers(api_key, protocol))
        if resp.status_code >= 400:
            return {"ok": False, "status": resp.status_code, "message": resp.text[:300]}
        data = resp.json() if resp.text else {}
        grouped, ids = parse_upstream_models(data, protocol)
        return {"ok": True, "protocol": protocol, "status": resp.status_code, "model_count": len(ids), "image_models": grouped["image"], "chat_models": grouped["chat"], "video_models": grouped["video"], "all": ids}
    except httpx.HTTPError as e:
        return {"ok": False, "status": 0, "message": str(e)[:300]}

@app.post("/api/providers/probe-async")
async def probe_async_endpoint(payload: TestConnectionPayload):
    """验证异步协议：用假 task_id 请求 GET /v1/tasks/{fake_id}。
    收到 400 Invalid task ID = 端点存在且 Key 有效；401/403 = Key 无效；404/连接失败 = 不支持异步端点。"""
    base_url = (payload.base_url or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail="请先填写请求地址")
    api_key = (payload.api_key or "").strip()
    if not api_key and payload.provider_id:
        api_key = provider_env_key_value(payload.provider_id)
    if not api_key:
        raise HTTPException(status_code=400, detail="请先填写或保存 API Key")
    tasks_base = base_url if base_url.endswith("/v1") else f"{base_url}/v1"
    probe_url = f"{tasks_base}/tasks/healthcheck_probe_do_not_submit"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(probe_url, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:500]
        sc = resp.status_code
        # 判断结果
        err_msg = ""
        if isinstance(body, dict):
            err = body.get("error") or {}
            if isinstance(err, dict):
                err_msg = str(err.get("message") or "").lower()
            else:
                err_msg = str(err).lower()
        # 400 + "invalid task id" → 端点存在，Key 有效
        if sc == 400 and "invalid task id" in err_msg:
            return {"ok": True, "status_code": sc, "message": "异步任务端点可用，API Key 已通过认证", "raw": body}
        # 401 / 403 → Key 无效
        if sc in (401, 403):
            return {"ok": False, "status_code": sc, "message": "API Key 无效或无权限", "raw": body}
        # 404 + 没有结构化错误 → 平台不支持此端点
        if sc == 404:
            return {"ok": False, "status_code": sc, "message": "平台不支持 /v1/tasks/ 端点，可能不是 APIMart 异步协议", "raw": body}
        # 其他 400 系 → 返回原始信息供参考
        if 400 <= sc < 500:
            return {"ok": None, "status_code": sc, "message": f"端点返回 {sc}，请查看原始响应判断", "raw": body}
        # 2xx → 意外成功（不太可能）
        if sc < 300:
            return {"ok": True, "status_code": sc, "message": f"端点返回 {sc}（意外成功）", "raw": body}
        return {"ok": False, "status_code": sc, "message": f"服务端错误 {sc}", "raw": body}
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=str(e)[:300])

async def fetch_models_from_upstream(base_url: str, api_key: str, protocol: str = "openai"):
    """从 OpenAI 兼容 /v1/models 端点拉取模型，并按名称做轻量分类。"""
    protocol = protocol if protocol in SUPPORTED_PROVIDER_PROTOCOLS else "openai"
    if protocol == "jimeng":
        models = [*JIMENG_DEFAULT_IMAGE_MODELS, *JIMENG_DEFAULT_VIDEO_MODELS]
        return {"total": len(models), "image_models": JIMENG_DEFAULT_IMAGE_MODELS, "chat_models": [], "video_models": JIMENG_DEFAULT_VIDEO_MODELS, "all": models}
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail="请先填写请求地址")
    if not re.match(r"^https?://", base_url):
        raise HTTPException(status_code=400, detail="请求地址必须以 http:// 或 https:// 开头")
    api_key = (api_key or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="请先填写或保存 API Key")
    url = upstream_models_url(base_url, protocol)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=upstream_model_headers(api_key, protocol))
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=f"上游 /v1/models 失败：{resp.text[:300]}")
            raw = resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"请求上游模型列表失败：{e}")
    grouped, ids = parse_upstream_models(raw, protocol)
    return {"total": len(ids), "image_models": grouped["image"], "chat_models": grouped["chat"], "video_models": grouped["video"], "all": ids}

@app.post("/api/providers/fetch-models")
async def fetch_upstream_models_from_payload(payload: TestConnectionPayload):
    """按页面当前表单值拉取模型，支持新增平台未保存时直接使用临时 Base URL / Key。"""
    api_key = (payload.api_key or "").strip()
    if not api_key and payload.provider_id:
        api_key = provider_env_key_value(payload.provider_id)
    return await fetch_models_from_upstream(payload.base_url, api_key, protocol_from_payload(payload))

@app.get("/api/providers/{provider_id}/fetch-models")
async def fetch_upstream_models(provider_id: str):
    """从已保存的上游 OpenAI 兼容接口拉取 /v1/models 列表，按名称智能分类为 image/chat/video。"""
    provider = get_api_provider_exact(provider_id)
    api_key = provider_env_key_value(provider["id"])
    if not api_key:
        raise HTTPException(status_code=400, detail=f"{provider.get('name') or provider_id} 未配置 API Key")
    return await fetch_models_from_upstream(provider.get("base_url") or "", api_key, provider_protocol(provider))

async def build_online_image_result(payload: OnlineImageRequest, domain_task_id: str = ""):
    provider = get_api_provider(payload.provider_id)
    default_model = (provider.get("image_models") or [IMAGE_MODEL])[0]
    model = selected_model(payload.model, default_model)
    refs = [ref.model_dump(mode="json") for ref in payload.reference_images if ref.url]
    try:
        generated = []
        for _ in range(payload.n):
            generated.append(
                await generate_ai_image(
                    payload.prompt,
                    payload.size,
                    payload.quality,
                    model,
                    refs,
                    provider["id"],
                    preserve_canvas=payload.preserve_canvas,
                )
            )
        local_urls = [
            await save_ai_image_to_output(image_data, prefix="online_")
            for image_data, _ in generated
        ]
        if payload.preserve_canvas and payload.source_width and payload.source_height:
            local_urls = [
                normalize_generated_canvas(url, payload.source_width, payload.source_height)
                for url in local_urls
            ]
    except httpx.HTTPStatusError as exc:
        text = exc.response.text or ''
        # 把上游英文错误转成中文友好提示
        friendly = None
        m = re.search(r"longest edge must be less than or equal to (\d+)", text)
        if m:
            limit = m.group(1)
            friendly = f"该模型不支持当前分辨率：最长边超过 {limit}px。请把图片分辨率调低（例如换到 2K 或更小），或更换支持高分辨率的模型。"
        elif "Invalid size" in text or "invalid_value" in text:
            friendly = f"该模型不支持当前尺寸：{payload.size}。请尝试更换分辨率或模型。"
        elif "rate limit" in text.lower() or "429" in text:
            friendly = "请求过于频繁，已被上游限流，请稍后再试。"
        elif "Unauthorized" in text or "401" in text:
            friendly = "API Key 无效或已过期，请到「API 设置」检查 Key。"
        elif "model_not_found" in text or "channel not found" in text:
            friendly = f"上游平台找不到模型「{model}」可用通道。可能该模型未在此账号开通，请换一个已开通的模型。"
        detail = friendly or f"上游生图接口错误：{text[:300]}"
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"请求上游生图接口失败：{exc}") from exc

    raw = generated[0][1]
    result = {
        "prompt": payload.prompt,
        "images": local_urls,
        "timestamp": time.time(),
        "type": "online",
        "model": model,
        "provider_id": provider["id"],
        "provider_name": provider.get("name") or provider["id"],
        "task_id": extract_task_id(raw) if isinstance(raw, dict) else None,
        "request_id": raw.get("id") if isinstance(raw, dict) else None,
        "params": {
            "provider_id": provider["id"],
            "model": model,
            "size": payload.size,
            "quality": payload.quality,
            "n": payload.n,
            "reference_images": refs,
            "preserve_canvas": payload.preserve_canvas,
            "source_width": payload.source_width,
            "source_height": payload.source_height,
        },
        "raw_usage": raw.get("usage") if isinstance(raw, dict) else None,
    }
    if domain_task_id:
        domain_task = DOMAIN_STORE.complete_generation_task(domain_task_id, local_urls)
        result["generation_task_id"] = domain_task_id
        result["domain"] = {
            "project_id": domain_task.get("project_id"),
            "outputs": [
                {
                    "asset_id": item.get("asset_id"),
                    "url": item.get("storage_url"),
                    "output_index": item.get("output_index"),
                }
                for item in domain_task.get("outputs") or []
            ],
        }
    save_to_history(result)
    if GLOBAL_LOOP:
        asyncio.run_coroutine_threadsafe(manager.broadcast_new_image(result), GLOBAL_LOOP)
    return result

def generation_quality_requirements(payload: OnlineImageRequest, overrides: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    supplied = [str(item).strip() for item in (overrides or payload.quality_requirements or []) if str(item).strip()]
    requirements = []
    if supplied:
        requirements.extend([
            {
                "id": f"requirement_{index}",
                "title": item[:80],
                "description": item[:500],
                "weight": 1,
                "threshold": 70,
            }
            for index, item in enumerate(supplied[:6], start=1)
        ])
    else:
        requirements.append({
            "id": "intent_fidelity",
            "title": "任务意图符合",
            "description": f"输出应准确回应任务目标：{payload.prompt[:500]}",
            "weight": 1.4,
            "threshold": 70,
        })
    requirements.extend([
        {
            "id": "visual_quality",
            "title": "视觉质量与清晰度",
            "description": "主体清晰，画面完成度足够，不存在明显模糊、破碎、错误文字或低质量拼接。",
            "weight": 1,
            "threshold": 70,
        },
        {
            "id": "composition_scale",
            "title": "构图与尺度",
            "description": "构图层级明确，空间尺度、透视和主体关系合理。",
            "weight": 1,
            "threshold": 70,
        },
        {
            "id": "geometry_plausibility",
            "title": "几何与物理合理性",
            "description": "建筑、物体、材质、光照和遮挡关系没有明显不合理或结构性错误。",
            "weight": 1,
            "threshold": 70,
        },
    ])
    if payload.preserve_canvas or payload.reference_images:
        requirements.append({
            "id": "reference_continuity",
            "title": "参考与画幅连续性",
            "description": "保留参考图中被要求锁定的构图、视角、主体和画幅关系，不发生无意漂移。",
            "weight": 1.3,
            "threshold": 75,
        })
    return requirements


def parse_quality_judge_result(text: str, requirements: List[Dict[str, Any]], pass_threshold: float) -> Dict[str, Any]:
    raw = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, re.IGNORECASE)
    candidate = fenced.group(1).strip() if fenced else raw
    start, end = candidate.find("{"), candidate.rfind("}")
    if start >= 0 and end > start:
        candidate = candidate[start:end + 1]
    parsed = json.loads(candidate)
    if not isinstance(parsed, dict):
        raise ValueError("AI Judge 未返回 JSON 对象")
    raw_scores = parsed.get("criteria") or parsed.get("scores") or []
    by_id = {
        str(item.get("id") or ""): item
        for item in raw_scores if isinstance(item, dict) and item.get("id")
    }
    normalized = []
    weighted_total = 0.0
    total_weight = 0.0
    for index, requirement in enumerate(requirements):
        raw_item = by_id.get(requirement["id"])
        if not raw_item and index < len(raw_scores) and isinstance(raw_scores[index], dict):
            raw_item = raw_scores[index]
        raw_item = raw_item or {}
        score = max(0.0, min(100.0, float(raw_item.get("score") or 0)))
        threshold = float(requirement.get("threshold") or 70)
        weight = float(requirement.get("weight") or 1)
        normalized.append({
            "requirement_id": requirement["id"],
            "title": requirement["title"],
            "score": round(score, 1),
            "passed": score >= threshold,
            "reason": str(raw_item.get("reason") or raw_item.get("feedback") or "")[:1000],
        })
        weighted_total += score * weight
        total_weight += weight
    calculated = weighted_total / max(1.0, total_weight)
    claimed = parsed.get("overall_score")
    overall_score = max(0.0, min(100.0, float(claimed if claimed is not None else calculated)))
    passed = overall_score >= float(pass_threshold) and all(item["passed"] for item in normalized)
    return {
        "status": "passed" if passed else "failed",
        "overall_score": round(overall_score, 1),
        "pass_threshold": float(pass_threshold),
        "verdict": str(parsed.get("verdict") or ("通过质量门" if passed else "未通过质量门"))[:1000],
        "feedback": str(parsed.get("feedback") or parsed.get("revision_advice") or "")[:4000],
        "scores": normalized,
    }


async def evaluate_generation_task_quality(
    task_id: str,
    *,
    judge_provider: str,
    judge_model: str,
    requirements_override: Optional[List[str]] = None,
    pass_threshold: float = 75,
) -> Dict[str, Any]:
    task = DOMAIN_STORE.get_generation_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="生成任务不存在")
    output_urls = [str(item.get("storage_url") or "") for item in (task.get("outputs") or []) if item.get("storage_url")]
    input_urls = [str(item.get("source_url") or "") for item in (task.get("inputs") or []) if item.get("source_url")]
    if not output_urls:
        raise HTTPException(status_code=409, detail="生成任务没有可评审的输出")
    try:
        payload = OnlineImageRequest(**(task.get("parameters") or {"prompt": task.get("prompt") or "评审生成结果"}))
    except Exception:
        payload = OnlineImageRequest(prompt=task.get("prompt") or "评审生成结果")
    requirements = generation_quality_requirements(payload, requirements_override)
    effective_provider = str(judge_provider or "").strip()
    effective_model = str(judge_model or "").strip()
    if effective_provider == "apimart" and "gemini" not in effective_model.lower():
        effective_model = "gemini-3.1-flash-image-preview"
    elif effective_provider == "modelscope" and "vl" not in effective_model.lower():
        effective_model = "Qwen/Qwen3-VL-235B-A22B-Instruct"
    requirement_text = "\n".join(
        f'- {item["id"]}｜{item["title"]}｜阈值 {item["threshold"]}：{item["description"]}'
        for item in requirements
    )
    system_prompt = """你是建筑设计生成结果的质量评审 Agent。你必须逐条核对要求，只输出合法 JSON，不要输出 Markdown。不能确认的要求应降低分数并说明证据不足。"""
    message = f"""评审本次生成结果。

原始任务：{task.get('prompt') or ''}
总通过阈值：{float(pass_threshold)}
图片顺序：前 {len(input_urls)} 张为输入/参考图，后 {len(output_urls)} 张为本轮输出图。若没有输入图，不评判参考连续性。

逐条要求：
{requirement_text}

只返回：
{{"overall_score":0-100,"verdict":"结论","feedback":"下一轮可直接执行的修改建议","criteria":[{{"id":"要求ID","score":0-100,"reason":"可核对理由"}}]}}
"""
    try:
        response = await canvas_llm(CanvasLLMRequest(
            message=message,
            system_prompt=system_prompt,
            provider=effective_provider,
            model=effective_model,
            images=[*input_urls, *output_urls],
        ))
        parsed = parse_quality_judge_result(response.get("text") or "", requirements, pass_threshold)
        response_text = str(response.get("text") or "")
        if parsed.get("scores") and all(float(item.get("score") or 0) == 0 for item in parsed["scores"]) and any(
            token in response_text for token in ("未提供生成结果", "无法评估", "没有图像", "未看到图像")
        ):
            parsed.update({
                "status": "error",
                "verdict": "当前 Judge 模型无法读取图像",
                "feedback": "请配置支持视觉输入的 Judge 模型后重新评审；本次不计入命中率。",
            })
        evaluator_model = response.get("model") or effective_model
    except Exception as exc:
        detail = str(getattr(exc, "detail", exc))[:1500]
        return DOMAIN_STORE.record_quality_evaluation(
            task["project_id"], task_id, status="error", overall_score=0,
            pass_threshold=pass_threshold, verdict="质量评审未完成", feedback=detail,
            requirements=requirements, scores=[], output_urls=output_urls,
            evaluator="ai_judge", model=effective_model,
        )
    return DOMAIN_STORE.record_quality_evaluation(
        task["project_id"], task_id, requirements=requirements, output_urls=output_urls,
        evaluator="ai_judge", model=evaluator_model, **parsed,
    )


def quality_retry_prompt(prompt: str, evaluation: Dict[str, Any]) -> str:
    failed = [item for item in (evaluation.get("scores") or []) if not item.get("passed")]
    reasons = "；".join(
        f"{item.get('title')}: {item.get('reason') or '未达到要求'}" for item in failed
    )
    feedback = str(evaluation.get("feedback") or "").strip()
    return "\n".join(filter(None, [
        str(prompt or "").strip(),
        "",
        "上一轮质量门反馈（必须修正，未提及的构图和有效内容保持不变）：",
        reasons or str(evaluation.get("verdict") or "未通过质量门"),
        feedback,
    ]))


def archive_quality_result(task_id: str) -> Dict[str, Any]:
    task = DOMAIN_STORE.get_generation_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="生成任务不存在")
    evaluations = task.get("quality_evaluations") or []
    if not evaluations:
        raise HTTPException(status_code=409, detail="当前任务还没有质量评审")
    evaluation = evaluations[-1]
    output_items = [
        {
            "url": str(item.get("storage_url") or ""),
            "asset_id": str(item.get("asset_id") or ""),
            "operation": "quality-gate",
            "operation_label": f"质量门第 {int(task.get('attempt') or 1)} 轮",
            "prompt": task.get("prompt") or "",
        }
        for item in (task.get("outputs") or []) if item.get("storage_url")
    ]
    if not output_items:
        raise HTTPException(status_code=409, detail="当前任务没有可归档的输出")
    canvas_id = str(task.get("canvas_id") or "")
    canvas = load_canvas(canvas_id) if canvas_id else None
    existing_by_asset = {
        str(item.get("asset_id") or ""): item
        for item in load_library_images()
        if item.get("asset_id") and item.get("project_id") == task["project_id"]
    }
    existing_records = [existing_by_asset[item["asset_id"]] for item in output_items if item["asset_id"] in existing_by_asset]
    pending_items = [item for item in output_items if item["asset_id"] not in existing_by_asset]
    imported = import_urls_into_library(
        urls=[item["url"] for item in pending_items],
        items=pending_items,
        source_name="质量门",
        canvas_id=canvas_id,
        canvas_title=str((canvas or {}).get("title") or ""),
        manual_tags=[
            "质量门",
            "质量门通过" if evaluation.get("status") == "passed" else "质量门待改进",
            f"第{int(task.get('attempt') or 1)}轮",
        ],
        categories=["设计Agent"],
        project_id=task["project_id"],
    ) if pending_items else {"count": 0, "imported": [], "skipped": [], "source_id": ""}
    archive_records = [*existing_records, *(imported.get("imported") or [])]
    node_id = ""
    if canvas and archive_records:
        already_exists = any(
            str(node.get("generation_task_id") or "") == task_id
            for node in (canvas.get("nodes") or []) if isinstance(node, dict)
        )
        if not already_exists:
            nodes = canvas.get("nodes") or []
            x = max([int(node.get("x") or 0) for node in nodes if isinstance(node, dict)] or [0]) + 460
            node = create_smart_canvas_image_node(archive_records, x=x, y=120)
            node.update({
                "title": f"质量门第 {int(task.get('attempt') or 1)} 轮 · {float(evaluation.get('overall_score') or 0):g} 分",
                "generation_task_id": task_id,
                "quality_evaluation_id": evaluation.get("id") or "",
                "quality_status": evaluation.get("status") or "",
                "quality_score": evaluation.get("overall_score") or 0,
                "quality_verdict": evaluation.get("verdict") or "",
            })
            canvas.setdefault("nodes", []).append(node)
            canvas.setdefault("generationHistory", []).append({
                "id": f"quality_archive_{uuid.uuid4().hex[:10]}",
                "task_id": task_id,
                "quality_evaluation_id": evaluation.get("id") or "",
                "status": evaluation.get("status") or "",
                "score": evaluation.get("overall_score") or 0,
                "created_at": now_ms(),
            })
            save_canvas(canvas)
            node_id = node["id"]
    return {
        "task_id": task_id,
        "canvas_id": canvas_id,
        "canvas_node_id": node_id,
        "imported_count": int(imported.get("count") or 0),
        "imported_asset_ids": [item.get("asset_id") for item in archive_records],
        "quality_status": evaluation.get("status"),
        "quality_score": evaluation.get("overall_score"),
    }


async def create_quality_retry_task(
    task_id: str,
    evaluation: Dict[str, Any],
    max_retries: int = 0,
    judge_provider: str = "",
    judge_model: str = "",
) -> Dict[str, Any]:
    durable = DOMAIN_STORE.get_generation_task(task_id)
    if not durable:
        raise HTTPException(status_code=404, detail="生成任务不存在")
    if evaluation.get("status") != "failed":
        raise HTTPException(status_code=409, detail="只有未通过质量门的任务可以创建修正轮次")
    attempt = int(durable.get("attempt") or 1) + 1
    if attempt > 3:
        raise HTTPException(status_code=409, detail="质量门最多允许两次自动修正")
    parameters = dict(durable.get("parameters") or {})
    parameters.update({
        "prompt": quality_retry_prompt(durable.get("prompt") or "", evaluation),
        "project_id": durable["project_id"],
        "canvas_id": durable.get("canvas_id") or "",
        "source_node_id": durable.get("source_node_id") or "",
        "quality_gate": True,
        "quality_attempt": attempt,
        "quality_root_task_id": durable.get("root_task_id") or durable["id"],
        "quality_parent_task_id": durable["id"],
        "quality_max_retries": max(0, min(2, int(max_retries or 0))),
    })
    if judge_provider:
        parameters["judge_provider"] = judge_provider
    if judge_model:
        parameters["judge_model"] = judge_model
    try:
        payload = OnlineImageRequest(**parameters)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"质量修正参数无法恢复：{exc}")
    result = await create_canvas_image_task(payload)
    DOMAIN_STORE.link_generation_retry(
        durable["project_id"], durable["id"], result["task_id"],
        str(evaluation.get("feedback") or evaluation.get("verdict") or "质量门未通过"),
    )
    return result


@app.post("/api/online-image")
async def online_image(payload: OnlineImageRequest):
    project_id = resolve_project_id(payload.project_id, payload.canvas_id)
    domain_task = DOMAIN_STORE.create_generation_task(
        project_id,
        canvas_id=payload.canvas_id,
        source_node_id=payload.source_node_id,
        provider_id=payload.provider_id,
        model=payload.model,
        prompt=payload.prompt,
        parameters=payload.model_dump(mode="json"),
        inputs=[item.model_dump(mode="json") for item in payload.reference_images],
        root_task_id=payload.quality_root_task_id,
        parent_task_id=payload.quality_parent_task_id,
        attempt=payload.quality_attempt,
        context_compilation_id=payload.context_compilation_id,
    )
    DOMAIN_STORE.update_generation_task(domain_task["id"], "running")
    try:
        result = await build_online_image_result(payload, domain_task["id"])
        if payload.quality_gate:
            evaluation = await evaluate_generation_task_quality(
                domain_task["id"], judge_provider=payload.judge_provider,
                judge_model=payload.judge_model,
                requirements_override=payload.quality_requirements,
                pass_threshold=payload.quality_pass_threshold,
            )
            result["quality_evaluation"] = evaluation
            if payload.quality_parent_task_id:
                result["quality_archive"] = archive_quality_result(domain_task["id"])
            if evaluation.get("status") == "failed" and payload.quality_max_retries > 0:
                retry_result = await create_quality_retry_task(
                    domain_task["id"], evaluation, max_retries=payload.quality_max_retries - 1,
                    judge_provider=payload.judge_provider, judge_model=payload.judge_model,
                )
                result["quality_retry_task_id"] = retry_result.get("task_id")
        return result
    except Exception as exc:
        DOMAIN_STORE.update_generation_task(domain_task["id"], "failed", str(getattr(exc, "detail", exc)))
        raise

async def run_canvas_image_task(task_id: str, payload: OnlineImageRequest):
    with CANVAS_TASK_LOCK:
        if task_id in CANVAS_TASKS:
            CANVAS_TASKS[task_id]["status"] = "running"
            CANVAS_TASKS[task_id]["updated_at"] = time.time()
    try:
        DOMAIN_STORE.update_generation_task(task_id, "running")
        result = await build_online_image_result(payload, task_id)
        evaluation = None
        quality_retry = None
        if payload.quality_gate:
            evaluation = await evaluate_generation_task_quality(
                task_id,
                judge_provider=payload.judge_provider,
                judge_model=payload.judge_model,
                requirements_override=payload.quality_requirements,
                pass_threshold=payload.quality_pass_threshold,
            )
            result["quality_evaluation"] = evaluation
            if evaluation.get("status") == "failed" and payload.quality_max_retries > 0:
                quality_retry = await create_quality_retry_task(
                    task_id, evaluation, max_retries=payload.quality_max_retries - 1,
                    judge_provider=payload.judge_provider, judge_model=payload.judge_model,
                )
                result["quality_retry_task_id"] = quality_retry.get("task_id")
        with CANVAS_TASK_LOCK:
            CANVAS_TASKS[task_id].update({
                "status": "succeeded",
                "result": result,
                "error": "",
                "updated_at": time.time(),
            })
    except asyncio.CancelledError:
        with CANVAS_TASK_LOCK:
            if task_id in CANVAS_TASKS:
                CANVAS_TASKS[task_id].update({
                    "status": "cancelled",
                    "error": "任务已取消",
                    "updated_at": time.time(),
                })
        DOMAIN_STORE.update_generation_task(task_id, "cancelled", "任务已取消")
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        status_code = getattr(exc, "status_code", 500)
        with CANVAS_TASK_LOCK:
            CANVAS_TASKS[task_id].update({
                "status": "failed",
                "error": str(detail),
                "status_code": status_code,
                "updated_at": time.time(),
            })
        DOMAIN_STORE.update_generation_task(task_id, "failed", str(detail))
    finally:
        with CANVAS_TASK_LOCK:
            CANVAS_ASYNC_TASKS.pop(task_id, None)

@app.post("/api/canvas-image-tasks")
async def create_canvas_image_task(payload: OnlineImageRequest):
    task_id = f"canvas_img_{uuid.uuid4().hex}"
    project_id = resolve_project_id(payload.project_id, payload.canvas_id)
    DOMAIN_STORE.create_generation_task(
        project_id,
        task_id=task_id,
        canvas_id=payload.canvas_id,
        source_node_id=payload.source_node_id,
        provider_id=payload.provider_id,
        model=payload.model,
        prompt=payload.prompt,
        parameters=payload.model_dump(mode="json"),
        inputs=[item.model_dump(mode="json") for item in payload.reference_images],
        root_task_id=payload.quality_root_task_id,
        parent_task_id=payload.quality_parent_task_id,
        attempt=payload.quality_attempt,
        context_compilation_id=payload.context_compilation_id,
    )
    with CANVAS_TASK_LOCK:
        CANVAS_TASKS[task_id] = {
            "id": task_id,
            "type": "online-image",
            "status": "queued",
            "created_at": time.time(),
            "updated_at": time.time(),
            "result": None,
            "error": "",
        }
    task_handle = asyncio.create_task(run_canvas_image_task(task_id, payload))
    with CANVAS_TASK_LOCK:
        CANVAS_ASYNC_TASKS[task_id] = task_handle
    return {"task_id": task_id, "status": "queued"}

@app.get("/api/canvas-image-tasks/{task_id}")
async def get_canvas_image_task(task_id: str):
    with CANVAS_TASK_LOCK:
        task = dict(CANVAS_TASKS.get(task_id) or {})
    if not task:
        durable = DOMAIN_STORE.get_generation_task(task_id)
        if durable:
            task = {
                "id": durable["id"],
                "status": durable["status"],
                "error": durable.get("error") or "",
                "result": {
                    "images": [item.get("storage_url") for item in durable.get("outputs") or []],
                    "generation_task_id": durable["id"],
                    "domain": {"project_id": durable["project_id"], "outputs": durable.get("outputs") or []},
                } if durable["status"] == "succeeded" else None,
                "durable": True,
            }
        else:
            raise HTTPException(status_code=404, detail="画布任务不存在")
    return task


@app.get("/api/generation-tasks")
async def list_generation_tasks(project_id: str = "", canvas_id: str = "", limit: int = 50):
    return {
        "tasks": DOMAIN_STORE.list_generation_tasks(
            project_id=project_id,
            canvas_id=canvas_id,
            limit=limit,
        )
    }


@app.get("/api/generation-tasks/{task_id}")
async def generation_task_detail(task_id: str):
    task = DOMAIN_STORE.get_generation_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="生成任务不存在")
    return {"task": task}


@app.post("/api/generation-tasks/{task_id}/evaluate")
async def evaluate_generation_task(task_id: str, payload: QualityEvaluationRequest):
    evaluation = await evaluate_generation_task_quality(
        task_id,
        judge_provider=payload.judge_provider,
        judge_model=payload.judge_model,
        requirements_override=payload.requirements,
        pass_threshold=payload.pass_threshold,
    )
    return {"evaluation": evaluation, "replay": DOMAIN_STORE.quality_replay(task_id)}


@app.get("/api/generation-tasks/{task_id}/replay")
async def generation_task_replay(task_id: str):
    replay = DOMAIN_STORE.quality_replay(task_id)
    if not replay:
        raise HTTPException(status_code=404, detail="生成任务不存在")
    return replay


@app.post("/api/generation-tasks/{task_id}/archive-quality-result")
async def archive_generation_quality_result(task_id: str):
    return {"archive": archive_quality_result(task_id), "replay": DOMAIN_STORE.quality_replay(task_id)}


@app.post("/api/generation-tasks/{task_id}/quality-retry")
async def retry_generation_from_quality(task_id: str, payload: QualityRetryRequest):
    evaluations = DOMAIN_STORE.quality_evaluations_for_task(task_id)
    if not evaluations:
        raise HTTPException(status_code=409, detail="当前任务还没有质量评审记录")
    evaluation = evaluations[-1]
    result = await create_quality_retry_task(
        task_id, evaluation, max_retries=payload.max_retries,
        judge_provider=payload.judge_provider, judge_model=payload.judge_model,
    )
    return {**result, "replay": DOMAIN_STORE.quality_replay(task_id)}


@app.post("/api/canvas-image-tasks/{task_id}/cancel")
async def cancel_canvas_image_task(task_id: str):
    durable = DOMAIN_STORE.get_generation_task(task_id)
    if not durable:
        raise HTTPException(status_code=404, detail="生成任务不存在")
    if durable["status"] in {"succeeded", "failed", "cancelled"}:
        return {"task_id": task_id, "status": durable["status"]}
    with CANVAS_TASK_LOCK:
        handle = CANVAS_ASYNC_TASKS.pop(task_id, None)
        if task_id in CANVAS_TASKS:
            CANVAS_TASKS[task_id].update({
                "status": "cancelled",
                "error": "任务已取消",
                "updated_at": time.time(),
            })
    DOMAIN_STORE.update_generation_task(task_id, "cancelled", "任务已取消")
    if handle and not handle.done():
        handle.cancel()
    return {"task_id": task_id, "status": "cancelled"}


@app.post("/api/canvas-image-tasks/{task_id}/retry")
async def retry_canvas_image_task(task_id: str):
    durable = DOMAIN_STORE.get_generation_task(task_id)
    if not durable:
        raise HTTPException(status_code=404, detail="生成任务不存在")
    if durable["status"] not in {"failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="只有失败或已取消的任务可以重试")
    try:
        payload = OnlineImageRequest(**(durable.get("parameters") or {}))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"任务参数无法恢复：{exc}")
    return await create_canvas_image_task(payload)

# --- Canvas Video ---

def video_output_urls(raw):
    data = raw.get("data") if isinstance(raw, dict) else {}
    if isinstance(data, list) and data:
        data = data[0] if isinstance(data[0], dict) else {}
    if not isinstance(data, dict):
        data = {}
    urls = []
    result = data.get("result") if isinstance(data.get("result"), dict) else raw.get("result") if isinstance(raw, dict) and isinstance(raw.get("result"), dict) else {}
    output = data.get("output") or raw.get("output")
    outputs = data.get("outputs") or raw.get("outputs") or []
    videos = result.get("videos") or data.get("videos") or raw.get("videos") or []
    if isinstance(output, str) and output:
        urls.append(output)
    if isinstance(outputs, list):
        for item in outputs:
            if isinstance(item, str) and item:
                urls.append(item)
            elif isinstance(item, dict):
                value = item.get("url") or item.get("output")
                if value:
                    urls.extend(value if isinstance(value, list) else [value])
    if isinstance(videos, list):
        for item in videos:
            if isinstance(item, str) and item:
                urls.append(item)
            elif isinstance(item, dict):
                value = item.get("url") or item.get("video_url") or item.get("output")
                if value:
                    urls.extend(value if isinstance(value, list) else [value])
    elif isinstance(videos, str) and videos:
        urls.append(videos)
    deduped = []
    for url in urls:
        if isinstance(url, str) and url and url not in deduped:
            deduped.append(url)
    return deduped

def video_api_root(provider):
    base_url = (provider.get("base_url") or AI_BASE_URL).rstrip("/")
    if base_url.endswith("/v1") or base_url.endswith("/v2"):
        base_url = base_url.rsplit("/", 1)[0]
    return base_url

async def wait_for_video_task(client, provider, task_id):
    base_url = video_api_root(provider)
    if not base_url:
        raise HTTPException(status_code=400, detail=f"{provider.get('name') or provider['id']} 未配置 Base URL")
    if is_apimart_provider(provider):
        task_path = f"{base_url}/tasks/{task_id}" if base_url.endswith("/v1") else f"{base_url}/v1/tasks/{task_id}"
        task_url = f"{task_path}?language=zh"
    else:
        task_url = f"{base_url}/v2/videos/generations/{task_id}"
    deadline = time.monotonic() + VIDEO_POLL_TIMEOUT
    delay = max(2.0, IMAGE_POLL_INTERVAL)
    last_payload = {}
    while time.monotonic() < deadline:
        await asyncio.sleep(delay)
        response = await client.get(task_url, headers=api_headers(provider=provider))
        response.raise_for_status()
        raw = response.json()
        last_payload = raw
        task_data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        status = str(task_data.get("status") or raw.get("status") or "").upper()
        if status in {"SUCCESS", "COMPLETED"}:
            return raw
        if status in {"FAILURE", "FAILED", "FAIL", "ERROR", "CANCELED", "CANCELLED", "TIMEOUT"}:
            error = task_data.get("error") if isinstance(task_data.get("error"), dict) else {}
            reason = task_data.get("fail_reason") or error.get("message") or raw.get("error") or raw.get("message") or str(raw)
            raise HTTPException(status_code=502, detail=f"视频生成任务失败：{reason}")
        delay = min(delay * 1.6, 12)
    raise HTTPException(status_code=504, detail=f"视频生成任务超时：{last_payload or task_id}")

def apimart_video_size(size):
    value = str(size or "16:9").strip()
    if value == "keep_ratio":
        return "adaptive"
    allowed = {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"}
    return value if value in allowed else "16:9"

@app.post("/api/canvas-video")
async def canvas_video(payload: CanvasVideoRequest):
    provider = get_api_provider(payload.provider_id)
    base_url = video_api_root(provider)
    if not base_url:
        raise HTTPException(status_code=400, detail=f"{provider.get('name') or provider['id']} 未配置 Base URL")
    api_key = os.getenv(provider_key_env(provider["id"]), "")
    if not api_key:
        raise HTTPException(status_code=400, detail=f"未配置 {provider.get('name') or provider['id']} 的 API Key，请在 API 设置中填写。")
    is_apimart = is_apimart_provider(provider)
    submit_url = f"{base_url}/videos/generations" if is_apimart and base_url.endswith("/v1") else f"{base_url}/v1/videos/generations" if is_apimart else f"{base_url}/v2/videos/generations"
    try:
        async with httpx.AsyncClient(timeout=VIDEO_POLL_TIMEOUT) as client:
            # --- 构造图片载荷 ---
            if is_apimart:
                # APIMart 只接受 http/https 或 asset:// URL，先上传本地图片取回网络 URL
                image_with_roles = []
                invalid_images = []
                for ref in payload.images[:9]:
                    if not ref.url:
                        continue
                    role = str(ref.role or "").strip()
                    if role in {"first_frame", "last_frame", "reference_image"}:
                        up_url = await upload_image_for_apimart(client, provider, ref.url)
                        if valid_apimart_video_image_input(up_url):
                            image_with_roles.append({"url": up_url, "role": role})
                        else:
                            invalid_images.append(ref.url)
                image_payload = []
                if not image_with_roles:
                    for ref in payload.images[:9]:
                        if not ref.url:
                            continue
                        up_url = await upload_image_for_apimart(client, provider, ref.url)
                        if valid_apimart_video_image_input(up_url):
                            image_payload.append(up_url)
                        else:
                            invalid_images.append(ref.url)
                if payload.images and not image_with_roles and not image_payload:
                    sample = invalid_video_image_preview(invalid_images[0] if invalid_images else "")
                    raise HTTPException(status_code=400, detail=f"输入图片无法转换为视频接口支持的格式：{sample}")
                # --- APIMart 请求体 ---
                body = {
                    "prompt": payload.prompt,
                    "model": selected_model(payload.model, "doubao-seedance-2.0"),
                    "duration": payload.duration,
                    "size": apimart_video_size(payload.aspect_ratio or payload.size),
                    "resolution": payload.resolution or "480p",
                }
                if image_with_roles:
                    body["image_with_roles"] = image_with_roles
                elif image_payload:
                    body["image_urls"] = image_payload[:9]
                if payload.videos:
                    body["video_urls"] = [v for v in payload.videos if v][:3]
                if payload.seed is not None:
                    body["seed"] = payload.seed
                if payload.return_last_frame:
                    body["return_last_frame"] = True
                if payload.generate_audio:
                    body["generate_audio"] = True
            else:
                # 非 APIMart：data URL 方式（OpenAI / ComflyAI 接口）
                image_payload = []
                for ref in payload.images[:4]:
                    if ref.url:
                        image_payload.append(reference_to_data_url(ref.model_dump(mode="json"), max_size=1536))
                body = {
                    "prompt": payload.prompt,
                    "model": selected_model(payload.model, "veo3-fast"),
                    "duration": payload.duration,
                    "watermark": payload.watermark,
                }
                if payload.aspect_ratio:
                    body["aspect_ratio"] = payload.aspect_ratio
                    body["ratio"] = payload.aspect_ratio
                if payload.size:
                    body["size"] = payload.size
                if payload.resolution:
                    body["resolution"] = payload.resolution
                if image_payload:
                    body["images"] = image_payload
                if payload.videos:
                    body["videos"] = [v for v in payload.videos if v]
                if payload.enhance_prompt:
                    body["enhance_prompt"] = True
                if payload.enable_upsample:
                    body["enable_upsample"] = True
                if payload.seed is not None:
                    body["seed"] = payload.seed
                if payload.camerafixed:
                    body["camerafixed"] = True
                if payload.return_last_frame:
                    body["return_last_frame"] = True
                if payload.generate_audio:
                    body["generate_audio"] = True
            # --- 发起视频生成请求 ---
            response = await client.post(submit_url, headers=api_headers(provider=provider), json=body)
            response.raise_for_status()
            try:
                raw = response.json()
            except Exception:
                # 上游返回了 HTML 错误页面或非 JSON 响应
                resp_text = response.text[:500]
                raise HTTPException(status_code=502, detail=f"上游视频接口返回非 JSON 响应（状态 {response.status_code}）：{resp_text}")
            task_id = extract_task_id(raw) or raw.get("task_id") or raw.get("id")
            result = raw
            if task_id and not video_output_urls(raw):
                result = await wait_for_video_task(client, provider, task_id)
            urls = video_output_urls(result)
            if not urls:
                raise HTTPException(status_code=502, detail=f"视频生成成功但没有返回视频：{result}")
            local_urls = [await save_remote_video_to_output(url) for url in urls]
            return {"videos": local_urls, "task_id": task_id, "raw": result}
    except httpx.HTTPStatusError as exc:
        text = exc.response.text
        try:
            requested_model = body.get("model", "") or payload.model or ""
        except NameError:
            requested_model = payload.model or ""
        provider_name = provider.get('name') or provider['id']
        # 1) 模型名不在上游支持范围 → 从错误信息里抽取合法列表展示
        valid_models_match = re.search(r"not in\s*\[([^\]]+)\]", text)
        if valid_models_match:
            valid_models = [m.strip() for m in valid_models_match.group(1).split(",") if m.strip()]
            sample = valid_models[:30]
            more = f"（共 {len(valid_models)} 个，仅显示前 {len(sample)} 个）" if len(valid_models) > len(sample) else ""
            hint = (
                f"上游「{provider_name}」不识别模型「{requested_model}」。\n\n"
                f"上游支持的视频模型清单{more}：\n  {', '.join(sample)}\n\n"
                f"请到「API 设置」里把视频模型改成上面列表中的一个。"
            )
            raise HTTPException(status_code=exc.response.status_code, detail=hint) from exc
        # 2) 模型名合法但账号没开通通道
        if "channel not found" in text or "model_not_found" in text:
            hint = (
                f"上游「{provider_name}」识别了模型「{requested_model}」，但你的 API Key 账号下**没有该模型的可用通道**。\n\n"
                f"原因：你的账号没开通这个模型的访问权限（付费/订阅相关）。\n\n"
                f"解决方法：\n"
                f"  1. 登录 {provider.get('base_url') or '上游平台'} 控制台，开通该模型 / 充值；\n"
                f"  2. 或在「API 设置」里把视频模型改成你账号已开通的型号（如 veo3-fast / veo2-fast / sora-2 等）。"
            )
            raise HTTPException(status_code=exc.response.status_code, detail=hint) from exc
        raise HTTPException(status_code=exc.response.status_code, detail=f"上游视频接口错误：{text}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"请求上游视频接口失败：{exc}") from exc

# --- Canvas LLM ---

def canvas_decision_from_text(text: str) -> dict:
    """Return an additive structured decision while preserving the original text response."""
    source = (text or "").strip()
    parsed = None
    match = re.search(r"\{[\s\S]*\}", source)
    if match:
        try:
            candidate = json.loads(match.group(0))
            parsed = candidate.get("decision", candidate) if isinstance(candidate, dict) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
    allowed = {
        "render": ("render", "立即执行渲染设计"),
        "render-design": ("render", "立即执行渲染设计"),
        "swap-material": ("swap", "立即替换材质"),
        "local-edit": ("local-edit", "立即局部编辑"),
        "populate": ("populate", "立即添加人物"),
        "generate-style": ("style", "立即生成风格"),
        "video": ("video", "立即生成视频"),
    }
    if isinstance(parsed, dict):
        primary = parsed.get("primary_action") if isinstance(parsed.get("primary_action"), dict) else {}
        action = str(primary.get("action") or "").strip().lower()
        if action in allowed:
            normalized, default_label = allowed[action]
            primary = {"action": normalized, "label": primary.get("label") or default_label, "params": primary.get("params") or {}}
        else:
            primary = None
        alternatives = [item for item in (parsed.get("alternative_actions") or []) if isinstance(item, dict)][:3]
        return {
            "summary": str(parsed.get("summary") or source[:220]).strip(),
            "needs_input": bool(parsed.get("needs_input")),
            "question": str(parsed.get("question") or "").strip(),
            "primary_action": primary,
            "alternative_actions": alternatives,
        }
    lowered = source.lower()
    ranked = [key for key in ("render-design", "swap-material", "local-edit", "populate", "generate-style", "video") if key in lowered]
    if not ranked:
        return {"summary": source[:220], "needs_input": False, "question": "", "primary_action": None, "alternative_actions": []}
    action, label = allowed[ranked[0]]
    needs_input = action == "swap" and any(word in source for word in ("请补充", "请明确", "需要追问"))
    return {"summary": source[:220], "needs_input": needs_input, "question": source[-160:] if needs_input else "", "primary_action": None if needs_input else {"action": action, "label": label, "params": {}}, "alternative_actions": []}

@app.post("/api/canvas-llm")
async def canvas_llm(payload: CanvasLLMRequest):
    chat_base, chat_hdrs, model = resolve_chat_provider(payload.provider, payload.model, payload.ms_model)
    # 判断协议：APIMart 异步 vs 标准 OpenAI
    _llm_provider = get_api_provider(payload.provider) if payload.provider not in ("modelscope",) else {}
    _is_apimart = is_apimart_provider(_llm_provider)
    upstream_messages = [{"role": "system", "content": payload.system_prompt or SYSTEM_PROMPT}]
    for item in payload.messages[-MAX_HISTORY_MESSAGES:]:
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "assistant"} and content:
            upstream_messages.append({"role": role, "content": content})
    # 构造用户消息：有图片时用 OpenAI vision 多模态格式
    if payload.images:
        content_parts = [{"type": "text", "text": payload.message}]
        ok_imgs = 0
        for img in payload.images[:8]:
            if not img or not isinstance(img, str):
                continue
            # 上游无法访问本站相对 URL；所有可解析的本地图片统一内联为 data URL。
            if local_asset_path_from_url(img):
                ref_url = reference_to_data_url({"url": img}, max_size=1024)
            elif img.startswith(("http://", "https://", "data:image/")):
                ref_url = img
            else:
                continue
            if not ref_url:
                continue
            content_parts.append({"type": "image_url", "image_url": {"url": ref_url}})
            ok_imgs += 1
        print(f"[canvas-llm] model={model} provider={payload.provider} text_len={len(payload.message)} images={ok_imgs}/{len(payload.images)}")
        upstream_messages.append({"role": "user", "content": content_parts})
    else:
        upstream_messages.append({"role": "user", "content": payload.message})
    raw = None
    try:
        async with httpx.AsyncClient(timeout=AI_REQUEST_TIMEOUT) as client:
            req_body = {"model": model, "messages": upstream_messages}
            if _is_apimart:
                req_body["stream"] = False   # APIMart 默认流式，强制关闭
            response = await client.post(
                f"{chat_base}/chat/completions",
                headers=chat_hdrs,
                json=req_body,
            )
            response.raise_for_status()
            if not response.content:
                raise HTTPException(status_code=502, detail="上游接口返回了空响应")
            raw = response.json()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text or ""
        raise HTTPException(status_code=exc.response.status_code, detail=f"上游接口错误：{body}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"请求上游接口失败：{exc}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"解析上游响应失败：{exc}") from exc
    try:
        text = text_from_chat_response(raw).strip() if isinstance(raw, dict) else ""
        text = text or "接口返回了空回复。"
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"解析回复内容失败：{exc}") from exc
    raw_data = unwrap_apimart_response(raw) if isinstance(raw, dict) else {}
    return {"text": text, "decision": canvas_decision_from_text(text), "model": model, "raw_usage": raw_data.get("usage")}

# --- 对话管理 ---

@app.get("/api/conversations")
async def conversations(request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    return {"user_id": user_id, "conversations": list_conversations(user_id)}

@app.post("/api/conversations")
async def create_conversation(payload: ConversationCreateRequest, request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    return {"conversation": new_conversation(user_id, payload.title)}

@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    return {"conversation": load_conversation(user_id, conversation_id)}

@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    path = conversation_path(user_id, conversation_id)
    if os.path.exists(path):
        os.remove(path)
    return {"ok": True}

# --- 画布管理 ---

def resolve_project_id(project_id: str = "", canvas_id: str = "") -> str:
    if project_id and DOMAIN_STORE.get_project(project_id):
        return project_id
    if canvas_id:
        canvas_record = DOMAIN_STORE.get_canvas(canvas_id)
        if canvas_record:
            return str(canvas_record["project_id"])
    return str(DOMAIN_STORE.ensure_default_project()["id"])


@app.get("/api/projects")
async def projects():
    return {"projects": DOMAIN_STORE.list_projects()}


@app.post("/api/projects")
async def create_project(payload: ProjectCreateRequest):
    return {"project": DOMAIN_STORE.create_project(payload.name, payload.code)}


@app.get("/api/projects/{project_id}")
async def project_overview(project_id: str):
    overview = DOMAIN_STORE.project_overview(project_id)
    if not overview:
        raise HTTPException(status_code=404, detail="项目不存在")
    return overview


@app.get("/api/projects/{project_id}/workspace")
async def project_workspace(project_id: str, limit: int = 24):
    workspace = DOMAIN_STORE.project_workspace(project_id, limit)
    if not workspace:
        raise HTTPException(status_code=404, detail="项目不存在")
    workspace["preference_profile"] = build_project_preference_profile(project_id)
    return workspace


def ppt_storage_url(path: str) -> str:
    absolute = os.path.abspath(path)
    try:
        relative = os.path.relpath(absolute, os.path.abspath(ASSETS_DIR))
    except ValueError as exc:
        raise PptWorkbenchError("PPT文件不在受管素材目录") from exc
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        raise PptWorkbenchError("PPT文件不在受管素材目录")
    return "/assets/" + relative.replace(os.sep, "/")


def ppt_manifest_payload(manifest: Dict[str, Any]) -> Dict[str, Any]:
    payload = json.loads(json.dumps(manifest, ensure_ascii=False))
    add_preview_urls(payload, PPT_WORKBENCH_DIR, ASSETS_DIR)
    payload["quality"] = ppt_quality_report(payload)
    for record in payload.get("exports") or []:
        record["download_url"] = (
            f"/api/projects/{urllib.parse.quote(payload['project_id'])}/ppt-workbench/"
            f"{urllib.parse.quote(payload['id'])}/exports/{urllib.parse.quote(record['id'])}/download"
        )
    return payload


async def save_limited_upload(upload: UploadFile, path: str, limit: int) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    total = 0
    with open(path, "wb") as target:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                target.close()
                try:
                    os.remove(path)
                except OSError:
                    pass
                raise PptWorkbenchError("上传文件超过当前版本限制")
            target.write(chunk)
    return total


def ppt_project_assets(project_id: str, *, include_shared: bool = True) -> List[Dict[str, Any]]:
    """Return locally comparable project assets plus optional shared-library candidates."""
    workspace = DOMAIN_STORE.project_workspace(project_id, limit=100)
    library_items = load_library_images()
    library_by_id = {str(item.get("id") or ""): item for item in library_items}
    result: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in workspace.get("recent_assets") or []:
        storage_url = str(item.get("storage_url") or "")
        if item.get("kind") != "image" or not storage_url:
            continue
        try:
            metadata = json.loads(item.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        library_item = library_by_id.get(str(metadata.get("library_image_id") or "")) or {}
        local_path = local_asset_path_from_url(storage_url) or ""
        fingerprint = {
            "sha256": str(item.get("sha256") or ""),
            "normalized_sha256": str(item.get("normalized_sha256") or ""),
            "phash": str(item.get("phash") or ""),
            "width": int(item.get("width") or 0),
            "height": int(item.get("height") or 0),
            "mime_type": str(item.get("mime_type") or ""),
        }
        if local_path and (not fingerprint["normalized_sha256"] or not fingerprint["phash"]):
            try:
                fingerprint = fingerprint_image_path(local_path)
                if item.get("version_id"):
                    DOMAIN_STORE.update_asset_fingerprint(
                        str(item["version_id"]),
                        **fingerprint,
                        byte_size=os.path.getsize(local_path),
                    )
            except (OSError, ValueError):
                pass
        result.append({
            "id": item.get("id"), "asset_id": item.get("id"),
            "library_image_id": metadata.get("library_image_id") or "",
            "title": item.get("title") or "未命名素材",
            "storage_url": storage_url, "local_path": local_path,
            "width": int(fingerprint.get("width") or item.get("width") or 0),
            "height": int(fingerprint.get("height") or item.get("height") or 0),
            "source": item.get("source") or "asset", "scope": "project",
            "categories": library_item.get("categories") or ["项目素材"],
            "tags": library_item.get("tags") or [str(item.get("source") or "")],
            "fingerprint": fingerprint,
        })
        seen_urls.add(storage_url)
    if include_shared:
        for item in library_items:
            storage_url = str(item.get("url") or "")
            if item.get("scope") != "shared" or not storage_url or storage_url in seen_urls:
                continue
            local_path = library_file_from_url(storage_url) or ""
            if not local_path:
                continue
            try:
                fingerprint = fingerprint_image_path(local_path)
            except (OSError, ValueError):
                continue
            result.append({
                "id": item.get("id"), "asset_id": "", "library_image_id": item.get("id") or "",
                "title": item.get("filename") or "共享素材", "storage_url": storage_url,
                "local_path": local_path, "width": int(fingerprint.get("width") or 0),
                "height": int(fingerprint.get("height") or 0), "source": "shared_library",
                "scope": "shared", "categories": item.get("categories") or [],
                "tags": item.get("tags") or [], "fingerprint": fingerprint,
            })
            seen_urls.add(storage_url)
    return result


def resolve_ppt_assignment_asset(project_id: str, requested_id: str) -> Dict[str, Any]:
    lineage = DOMAIN_STORE.lineage_for_asset(requested_id)
    asset = lineage.get("asset") or {}
    if not asset:
        library_item = next((item for item in load_library_images() if str(item.get("id") or "") == requested_id), None)
        if library_item:
            asset = library_asset_for_project(library_item, project_id, create=True) or {}
            lineage = DOMAIN_STORE.lineage_for_asset(str(asset.get("id") or "")) if asset else {}
    if not asset:
        raise HTTPException(status_code=404, detail="项目素材不存在")
    if asset.get("project_id") != project_id or asset.get("kind") != "image":
        raise HTTPException(status_code=400, detail="只能选择当前项目中的图片素材")
    version = (lineage.get("versions") or [{}])[0]
    storage_url = str(version.get("storage_url") or "")
    local_path = local_asset_path_from_url(storage_url)
    if not local_path:
        raise HTTPException(status_code=400, detail="当前素材没有可用于 PPT 导出的本地原文件")
    fingerprint = fingerprint_image_path(local_path)
    if version.get("id"):
        DOMAIN_STORE.update_asset_fingerprint(str(version["id"]), **fingerprint, byte_size=os.path.getsize(local_path))
    return {
        "asset_id": asset["id"], "storage_url": storage_url,
        "title": asset.get("title") or "项目素材",
        "width": int(fingerprint.get("width") or version.get("width") or 0),
        "height": int(fingerprint.get("height") or version.get("height") or 0),
        "source": "project_library",
    }


def ppt_public_assets(project_id: str) -> List[Dict[str, Any]]:
    return [
        {key: value for key, value in item.items() if key not in {"local_path", "fingerprint"}}
        for item in ppt_project_assets(project_id)
    ]


def scan_ppt_manifest_for_project(manifest: Dict[str, Any]) -> Dict[str, Any]:
    project_id = str(manifest.get("project_id") or "")
    template_path = os.path.join(
        str(ppt_job_dir(PPT_WORKBENCH_DIR, project_id, manifest["id"])),
        str(manifest.get("template_filename") or "template.pptx"),
    )
    candidates = ppt_project_assets(project_id)
    manifest = scan_ppt_recommendations(manifest, template_path, candidates)
    return save_ppt_manifest(PPT_WORKBENCH_DIR, manifest)


@app.get("/api/projects/{project_id}/ppt-workbench")
async def project_ppt_workbench(project_id: str, limit: int = 20):
    if not DOMAIN_STORE.get_project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    manifests = list_ppt_manifests(PPT_WORKBENCH_DIR, project_id, limit)
    if manifests and not manifests[0].get("last_scanned_at"):
        manifests[0] = await asyncio.to_thread(scan_ppt_manifest_for_project, manifests[0])
    jobs = [ppt_manifest_payload(item) for item in manifests]
    return {"project_id": project_id, "jobs": jobs, "active_job": jobs[0] if jobs else None, "assets": ppt_public_assets(project_id)}


@app.get("/api/projects/{project_id}/ppt-workbench/{job_id}")
async def project_ppt_workbench_job(project_id: str, job_id: str):
    try:
        manifest = load_ppt_manifest(PPT_WORKBENCH_DIR, project_id, job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="PPT任务不存在") from exc
    except PptWorkbenchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not manifest.get("last_scanned_at"):
        manifest = await asyncio.to_thread(scan_ppt_manifest_for_project, manifest)
    return {"job": ppt_manifest_payload(manifest), "assets": ppt_public_assets(project_id)}


@app.post("/api/projects/{project_id}/ppt-workbench/{job_id}/scan")
async def scan_project_ppt_workbench(project_id: str, job_id: str):
    try:
        manifest = load_ppt_manifest(PPT_WORKBENCH_DIR, project_id, job_id)
        manifest = await asyncio.to_thread(scan_ppt_manifest_for_project, manifest)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="PPT任务不存在") from exc
    except PptWorkbenchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job": ppt_manifest_payload(manifest), "assets": ppt_public_assets(project_id)}


@app.get("/api/projects/{project_id}/ppt-workbench/{job_id}/recommendations")
async def project_ppt_recommendations(project_id: str, job_id: str, object_id: str = ""):
    try:
        manifest = load_ppt_manifest(PPT_WORKBENCH_DIR, project_id, job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="PPT任务不存在") from exc
    objects = manifest.get("image_objects") or []
    if object_id:
        target = next((item for item in objects if item.get("id") == object_id), None)
        if not target:
            raise HTTPException(status_code=404, detail="PPT 图片对象不存在")
        return {"object_id": object_id, "recommendations": target.get("recommendations") or []}
    return {"summary": manifest.get("recommendation_summary") or {}, "objects": [{"id": item.get("id"), "match_status": item.get("match_status"), "recommendations": item.get("recommendations") or []} for item in objects]}


@app.post("/api/projects/{project_id}/ppt-workbench/templates")
async def upload_project_ppt_template(project_id: str, file: UploadFile = File(...)):
    if not DOMAIN_STORE.get_project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    original_name = os.path.basename(file.filename or "template.pptx")
    if not original_name.lower().endswith(".pptx"):
        raise HTTPException(status_code=400, detail="请上传 .pptx 模板")
    temp_path = os.path.join(PPT_WORKBENCH_DIR, f".upload-{uuid.uuid4().hex}.pptx")
    try:
        await save_limited_upload(file, temp_path, 160 * 1024 * 1024)
        manifest = await asyncio.to_thread(
            create_ppt_manifest,
            PPT_WORKBENCH_DIR,
            project_id,
            temp_path,
            original_name=original_name,
        )
        template_path = os.path.join(
            str(ppt_job_dir(PPT_WORKBENCH_DIR, project_id, manifest["id"])),
            manifest["template_filename"],
        )
        template_url = ppt_storage_url(template_path)
        asset = DOMAIN_STORE.register_asset(
            project_id,
            template_url,
            kind="presentation",
            title=f"PPT模板｜{manifest['name']}",
            source="ppt_template",
            byte_size=os.path.getsize(template_path),
            mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            metadata={"ppt_job_id": manifest["id"], "role": "template"},
        )
        manifest["template_asset_id"] = asset["id"]
        manifest["template_url"] = template_url
        manifest = await asyncio.to_thread(scan_ppt_manifest_for_project, manifest)
        try:
            manifest["rendered_slides"] = await asyncio.to_thread(render_template_previews, manifest, PPT_WORKBENCH_DIR)
            manifest.pop("preview_error", None)
        except Exception as exc:
            manifest["rendered_slides"] = []
            manifest["preview_error"] = f"模板缩略图生成失败：{exc}"
        save_ppt_manifest(PPT_WORKBENCH_DIR, manifest)
    except PptWorkbenchError as exc:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise
    return {"job": ppt_manifest_payload(manifest), "assets": ppt_public_assets(project_id)}


@app.post("/api/projects/{project_id}/ppt-workbench/{job_id}/images")
async def upload_project_ppt_image(
    project_id: str,
    job_id: str,
    slot_id: str = Form(...),
    file: UploadFile = File(...),
):
    try:
        load_ppt_manifest(PPT_WORKBENCH_DIR, project_id, job_id)
        content = await file.read(30 * 1024 * 1024 + 1)
        saved = save_replacement_image(
            PPT_WORKBENCH_DIR,
            project_id,
            job_id,
            slot_id,
            content,
            file.filename or "replacement.png",
        )
        storage_url = ppt_storage_url(saved["path"])
        asset = DOMAIN_STORE.register_asset(
            project_id,
            storage_url,
            kind="image",
            title=os.path.splitext(saved["original_name"])[0][:240],
            source="ppt_replacement_upload",
            width=saved["width"],
            height=saved["height"],
            byte_size=saved["byte_size"],
            mime_type=saved["mime_type"],
            metadata={"ppt_job_id": job_id, "slot_id": slot_id},
        )
        assignment = resolve_ppt_assignment_asset(project_id, str(asset["id"]))
        assignment["source"] = "upload"
        manifest = assign_ppt_image_object(PPT_WORKBENCH_DIR, project_id, job_id, slot_id, assignment)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="PPT任务不存在") from exc
    except PptWorkbenchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job": ppt_manifest_payload(manifest), "asset": assignment}


@app.put("/api/projects/{project_id}/ppt-workbench/{job_id}/slots/{slot_id}")
async def assign_project_asset_to_ppt_slot(project_id: str, job_id: str, slot_id: str, req: PptSlotAssignRequest):
    assignment = resolve_ppt_assignment_asset(project_id, req.asset_id)
    try:
        manifest = assign_ppt_slot(PPT_WORKBENCH_DIR, project_id, job_id, slot_id, assignment)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="PPT任务不存在") from exc
    except PptWorkbenchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job": ppt_manifest_payload(manifest), "asset": assignment}


@app.put("/api/projects/{project_id}/ppt-workbench/{job_id}/image-objects/{object_id}")
async def assign_project_asset_to_ppt_object(project_id: str, job_id: str, object_id: str, req: PptImageObjectAssignRequest):
    if req.apply_scope not in {"object", "source_group", "layout_group"}:
        raise HTTPException(status_code=400, detail="无效的应用范围")
    assignment = resolve_ppt_assignment_asset(project_id, req.asset_id)
    try:
        manifest = assign_ppt_image_object(PPT_WORKBENCH_DIR, project_id, job_id, object_id, assignment, req.apply_scope)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="PPT任务不存在") from exc
    except PptWorkbenchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job": ppt_manifest_payload(manifest), "asset": assignment}


@app.delete("/api/projects/{project_id}/ppt-workbench/{job_id}/image-objects/{object_id}/assignment")
async def clear_project_ppt_object(project_id: str, job_id: str, object_id: str, keep_original: bool = True):
    try:
        manifest = clear_ppt_image_assignment(PPT_WORKBENCH_DIR, project_id, job_id, object_id, keep_original=keep_original)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="PPT任务不存在") from exc
    except PptWorkbenchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job": ppt_manifest_payload(manifest)}


@app.put("/api/projects/{project_id}/ppt-workbench/{job_id}/text-objects/{object_id}")
async def edit_project_ppt_text(project_id: str, job_id: str, object_id: str, req: PptTextObjectUpdateRequest):
    try:
        manifest = update_ppt_text_object(PPT_WORKBENCH_DIR, project_id, job_id, object_id, req.text, req.revision)
        rendered = await asyncio.to_thread(render_template_previews, manifest, PPT_WORKBENCH_DIR)
        if rendered:
            manifest = save_ppt_manifest(
                PPT_WORKBENCH_DIR,
                add_preview_urls(manifest, PPT_WORKBENCH_DIR, ASSETS_DIR),
            )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="PPT任务不存在") from exc
    except PptWorkbenchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job": ppt_manifest_payload(manifest)}


@app.post("/api/projects/{project_id}/ppt-workbench/{job_id}/export")
async def export_project_ppt(project_id: str, job_id: str):
    try:
        manifest = load_ppt_manifest(PPT_WORKBENCH_DIR, project_id, job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="PPT任务不存在") from exc
    inputs = []
    if manifest.get("template_url"):
        inputs.append({"url": manifest["template_url"], "asset_id": manifest.get("template_asset_id"), "name": "PPT母版", "role": "template"})
    for image_object in manifest.get("image_objects") or []:
        assignment = image_object.get("assignment") or {}
        if assignment.get("storage_url"):
            inputs.append({
                "url": assignment["storage_url"],
                "asset_id": assignment.get("asset_id"),
                "name": image_object.get("label"),
                "role": image_object.get("id"),
            })
    task = DOMAIN_STORE.create_generation_task(
        project_id,
        provider_id="ppt-template",
        model="architectural-site-planning-v1",
        prompt="在真实母版中原位替换已确认图片、保留未修改内容并导出可编辑 PPTX",
        parameters={
            "ppt_job_id": job_id,
            "image_object_count": len(manifest.get("image_objects") or []),
            "assigned_image_count": sum(bool(item.get("assignment")) for item in manifest.get("image_objects") or []),
            "changed_text_count": sum(bool(item.get("changed")) for item in manifest.get("text_objects") or []),
        },
        inputs=inputs,
    )
    DOMAIN_STORE.update_generation_task(task["id"], "running")
    try:
        result = await asyncio.to_thread(
            export_ppt_presentation,
            PPT_WORKBENCH_DIR,
            project_id,
            job_id,
            resolve_assignment_path=lambda assignment: local_asset_path_from_url(str(assignment.get("storage_url") or "")),
        )
        output_url = ppt_storage_url(result["path"])
        output_asset = DOMAIN_STORE.register_asset(
            project_id,
            output_url,
            kind="presentation",
            title=os.path.splitext(result["export"]["filename"])[0],
            source="ppt_export",
            byte_size=os.path.getsize(result["path"]),
            mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            metadata={"ppt_job_id": job_id, "generation_task_id": task["id"], "quality": result["quality"]},
        )
        DOMAIN_STORE.complete_generation_task(task["id"], [output_url])
        result["export"]["asset_id"] = output_asset["id"]
        result["export"]["generation_task_id"] = task["id"]
        save_ppt_manifest(PPT_WORKBENCH_DIR, result["manifest"])
    except PptWorkbenchError as exc:
        DOMAIN_STORE.update_generation_task(task["id"], "failed", str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        DOMAIN_STORE.update_generation_task(task["id"], "failed", str(exc))
        raise
    payload = ppt_manifest_payload(result["manifest"])
    exported = next(item for item in payload["exports"] if item["id"] == result["export"]["id"])
    return {"job": payload, "export": exported, "generation_task_id": task["id"], "asset_id": output_asset["id"]}


@app.get("/api/projects/{project_id}/ppt-workbench/{job_id}/exports/{export_id}/download")
async def download_project_ppt_export(project_id: str, job_id: str, export_id: str):
    try:
        path = ppt_export_path(PPT_WORKBENCH_DIR, project_id, job_id, export_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="导出文件不存在") from exc
    return FileResponse(
        str(path),
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=path.name,
    )


@app.get("/api/projects/{project_id}/design-context")
async def project_design_context_history(project_id: str, limit: int = 12):
    if not DOMAIN_STORE.get_project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    return {
        "project_id": project_id,
        "compilations": DOMAIN_STORE.list_project_context_compilations(project_id, limit),
    }


@app.post("/api/projects/{project_id}/design-context/compile")
async def compile_project_design_context_endpoint(project_id: str, req: ProjectContextCompileRequest):
    context = {
        "reference_images": [item.model_dump(mode="json") for item in req.reference_images],
        "local_wiki_root": req.local_wiki_root,
    }
    compiled = compile_project_design_context(project_id, req.goal, context)
    compiled.pop("wiki_matches", None)
    return {"compilation": compiled}


@app.get("/api/project-context-compilations/{compilation_id}")
async def project_design_context_compilation(compilation_id: str):
    compilation = DOMAIN_STORE.get_project_context_compilation(compilation_id)
    if not compilation:
        raise HTTPException(status_code=404, detail="项目约束快照不存在")
    return {"compilation": compilation}


@app.get("/api/projects/{project_id}/feedback")
async def project_feedback(project_id: str, limit: int = 8):
    if not DOMAIN_STORE.get_project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    return DOMAIN_STORE.project_feedback_summary(project_id, limit)

@app.get("/api/projects/{project_id}/preferences")
async def project_preferences(project_id: str):
    if not DOMAIN_STORE.get_project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    return {
        "profile": build_project_preference_profile(project_id),
        "skill_candidates": DOMAIN_STORE.list_skill_candidates(project_id),
    }

@app.post("/api/projects/{project_id}/preferences/refresh")
async def refresh_project_preferences(project_id: str):
    if not DOMAIN_STORE.get_project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    candidate = refresh_project_preference_skill_candidate(project_id)
    return {
        "profile": build_project_preference_profile(project_id),
        "skill_candidate": candidate,
        "published": False,
    }

@app.post("/api/projects/{project_id}/skill-candidates/{candidate_id}/review")
async def review_project_skill_candidate(project_id: str, candidate_id: str, req: SkillCandidateReviewRequest):
    try:
        candidate = DOMAIN_STORE.review_skill_candidate(candidate_id, project_id, req.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"candidate": candidate, "published": False}


@app.get("/api/assets/{asset_id}/lineage")
async def asset_lineage(asset_id: str):
    result = DOMAIN_STORE.lineage_for_asset(asset_id)
    if not result.get("asset"):
        raise HTTPException(status_code=404, detail="素材不存在")
    return result


@app.post("/api/assets/{asset_id}/feedback")
async def asset_feedback(asset_id: str, req: AssetFeedbackRequest):
    try:
        feedback = DOMAIN_STORE.record_preference_event(req.project_id, asset_id, req.event_type, req.context)
        candidate = refresh_project_preference_skill_candidate(req.project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "asset_id": asset_id,
        "project_id": req.project_id,
        "feedback": feedback,
        "preference_profile": build_project_preference_profile(req.project_id),
        "skill_candidate": candidate,
    }


@app.get("/api/canvases")
async def canvases(project_id: str = ""):
    return {"canvases": list_canvases(project_id)}

@app.get("/api/canvases/trash")
async def trashed_canvases(project_id: str = ""):
    return {"canvases": list_deleted_canvases(project_id), "retention_days": 30}

@app.post("/api/canvases")
async def create_canvas(payload: CanvasCreateRequest):
    return {"canvas": new_canvas(payload.title, payload.icon, payload.kind, payload.project_id)}

@app.get("/api/canvases/{canvas_id}/meta")
async def get_canvas_meta(canvas_id: str):
    canvas = load_canvas(canvas_id)
    return {
        "id": canvas.get("id"),
        "updated_at": canvas.get("updated_at", 0),
        "title": canvas.get("title", "未命名画布"),
        "icon": canvas.get("icon", "layers"),
        "kind": normalize_canvas_kind(canvas.get("kind")),
    }

@app.get("/api/canvases/{canvas_id}")
async def get_canvas(canvas_id: str):
    return {"canvas": load_canvas(canvas_id)}


@app.get("/api/canvases/{canvas_id}/snapshots")
async def get_canvas_snapshots(canvas_id: str, limit: int = 30):
    load_canvas(canvas_id)
    return {"snapshots": DOMAIN_STORE.list_canvas_snapshots(canvas_id, limit)}

@app.post("/api/canvas-assets/check")
async def check_canvas_assets(payload: CanvasAssetCheckRequest):
    result = {}
    for url in payload.urls[:3000]:
        text = str(url or "").strip()
        if not text:
            continue
        if text.startswith("/output/") or text.startswith("/assets/") or text.startswith("/api/library/file/"):
            result[text] = bool(local_asset_path_from_url(text))
        else:
            result[text] = True
    return {"exists": result}

@app.post("/api/canvas-assets/download")
async def download_canvas_assets(payload: CanvasAssetDownloadRequest):
    buffer = BytesIO()
    used_names = set()
    count = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for url in payload.urls[:1000]:
            text = str(url or "").strip()
            if not text:
                continue
            path = local_asset_path_from_url(text)
            if not path or not os.path.isfile(path):
                continue
            base = os.path.basename(path) or f"asset-{count + 1}.bin"
            name, ext = os.path.splitext(base)
            archive_name = base
            suffix = 2
            while archive_name in used_names:
                archive_name = f"{name}-{suffix}{ext}"
                suffix += 1
            used_names.add(archive_name)
            zf.write(path, archive_name)
            count += 1
    if count <= 0:
        raise HTTPException(status_code=404, detail="没有可下载的本地素材")
    buffer.seek(0)
    filename = re.sub(r'[\\/:*?"<>|]+', "_", payload.filename or "canvas-output-assets.zip")
    if not filename.lower().endswith(".zip"):
        filename += ".zip"
    encoded = urllib.parse.quote(filename)
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"}
    return Response(buffer.getvalue(), media_type="application/zip", headers=headers)

@app.put("/api/canvases/{canvas_id}")
async def update_canvas(canvas_id: str, payload: CanvasSaveRequest):
    canvas = load_canvas(canvas_id)
    current_updated_at = int(canvas.get("updated_at") or 0)
    if payload.base_updated_at and current_updated_at and int(payload.base_updated_at) < current_updated_at:
        raise HTTPException(status_code=409, detail={
            "message": "画布已被其他页面更新，已拒绝旧版本覆盖。",
            "canvas": canvas,
            "updated_at": current_updated_at,
        })
    canvas["title"] = (payload.title or canvas.get("title") or "未命名画布")[:80]
    canvas["icon"] = (payload.icon or canvas.get("icon") or "layers")[:32]
    canvas["kind"] = normalize_canvas_kind(canvas.get("kind"))
    canvas["nodes"] = payload.nodes
    canvas["connections"] = payload.connections
    canvas["viewport"] = payload.viewport
    canvas["logs"] = payload.logs[-500:]
    canvas["generationHistory"] = payload.generationHistory[-100:]
    canvas["settings"] = payload.settings or {}
    save_canvas(canvas)
    snapshot = DOMAIN_STORE.save_canvas_snapshot(canvas, canvas.get("project_id") or "")
    await manager.broadcast_canvas_updated(canvas_id, int(canvas.get("updated_at") or now_ms()), payload.client_id)
    return {"canvas": canvas, "snapshot": snapshot}

@app.delete("/api/canvases/{canvas_id}")
async def delete_canvas(canvas_id: str):
    canvas = load_canvas_any(canvas_id)
    if not canvas.get("deleted_at"):
        canvas["deleted_at"] = now_ms()
        save_canvas(canvas)
    return {"ok": True}

@app.post("/api/canvases/{canvas_id}/restore")
async def restore_canvas(canvas_id: str):
    canvas = load_canvas_any(canvas_id)
    if canvas.get("deleted_at"):
        canvas.pop("deleted_at", None)
        save_canvas(canvas)
    return {"canvas": canvas}

@app.delete("/api/canvases/{canvas_id}/purge")
async def purge_canvas(canvas_id: str):
    path = canvas_path(canvas_id)
    if os.path.exists(path):
        os.remove(path)
    return {"ok": True}

# --- GPT 对话 ---

@app.post("/api/chat")
async def chat(payload: ChatRequest, request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    conversation = (
        load_conversation(user_id, payload.conversation_id)
        if payload.conversation_id
        else new_conversation(user_id, display_title(payload.message))
    )
    if not conversation.get("messages"):
        conversation["title"] = display_title(payload.message)

    refs = [ref.model_dump(mode="json") for ref in payload.reference_images if ref.url]
    user_message = {
        "id": uuid.uuid4().hex,
        "role": "user",
        "content": payload.message,
        "created_at": now_ms(),
        "attachments": refs,
        "mode": payload.mode,
    }
    conversation["messages"].append(user_message)
    conversation["updated_at"] = now_ms()
    save_conversation(user_id, conversation)

    if payload.mode == "image":
        image_provider_id = payload.provider if payload.provider not in {"modelscope"} else "comfly"
        provider = get_api_provider(image_provider_id)
        default_model = (provider.get("image_models") or [IMAGE_MODEL])[0]
        model = selected_model(payload.image_model or payload.model, default_model)
        try:
            image_data, raw = await generate_ai_image(payload.message, payload.size, payload.quality, model, refs, provider["id"])
            local_url = await save_ai_image_to_output(image_data, prefix="chat_")
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=exc.response.status_code, detail=f"上游生图接口错误：{exc.response.text}") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"请求上游生图接口失败：{exc}") from exc
        assistant_message = {
            "id": uuid.uuid4().hex,
            "role": "assistant",
            "type": "image",
            "content": payload.message,
            "image_url": local_url,
            "created_at": now_ms(),
            "model": model,
            "raw_usage": raw.get("usage") if isinstance(raw, dict) else None,
        }
    else:
        chat_base, chat_hdrs, model = resolve_chat_provider(payload.provider, payload.model, payload.ms_model)
        _conv_provider = get_api_provider(payload.provider) if payload.provider not in ("modelscope",) else {}
        _conv_is_apimart = is_apimart_provider(_conv_provider)
        history = conversation["messages"][-MAX_HISTORY_MESSAGES:]
        upstream_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for item in history:
            msg = upstream_message_from_record(item)
            if msg:
                upstream_messages.append(msg)
        try:
            async with httpx.AsyncClient(timeout=AI_REQUEST_TIMEOUT) as client:
                conv_req_body = {"model": model, "messages": upstream_messages}
                if _conv_is_apimart:
                    conv_req_body["stream"] = False
                response = await client.post(
                    f"{chat_base}/chat/completions",
                    headers=chat_hdrs,
                    json=conv_req_body,
                )
                response.raise_for_status()
                raw = response.json()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=exc.response.status_code, detail=f"上游接口错误：{exc.response.text}") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"请求上游接口失败：{exc}") from exc
        raw_data = unwrap_apimart_response(raw) if isinstance(raw, dict) else raw
        assistant_message = {
            "id": uuid.uuid4().hex,
            "role": "assistant",
            "content": text_from_chat_response(raw).strip() or "接口返回了空回复。",
            "created_at": now_ms(),
            "model": model,
            "raw_usage": raw_data.get("usage") if isinstance(raw_data, dict) else None,
        }

    conversation["messages"].append(assistant_message)
    conversation["updated_at"] = now_ms()
    save_conversation(user_id, conversation)
    return {"conversation": conversation, "message": assistant_message}

@app.post("/api/chat/stream")
async def chat_stream(payload: ChatRequest, request: Request, x_user_id: str = Header(default="")):
    if payload.mode == "image":
        raise HTTPException(status_code=400, detail="图片模式请使用 /api/chat")

    user_id = safe_user_id(x_user_id, request)
    conversation = (
        load_conversation(user_id, payload.conversation_id)
        if payload.conversation_id
        else new_conversation(user_id, display_title(payload.message))
    )
    if not conversation.get("messages"):
        conversation["title"] = display_title(payload.message)

    refs = [ref.model_dump(mode="json") for ref in payload.reference_images if ref.url]
    user_message = {
        "id": uuid.uuid4().hex,
        "role": "user",
        "content": payload.message,
        "created_at": now_ms(),
        "attachments": refs,
        "mode": payload.mode,
    }
    conversation["messages"].append(user_message)
    conversation["updated_at"] = now_ms()
    save_conversation(user_id, conversation)

    chat_base, chat_hdrs, model = resolve_chat_provider(payload.provider, payload.model, payload.ms_model)
    history = conversation["messages"][-MAX_HISTORY_MESSAGES:]
    upstream_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for item in history:
        msg = upstream_message_from_record(item)
        if msg:
            upstream_messages.append(msg)

    async def stream():
        content_parts = []
        raw_usage = None
        yield sse_event({"type": "meta", "conversation": conversation})
        try:
            async with httpx.AsyncClient(timeout=AI_REQUEST_TIMEOUT) as client:
                async with client.stream(
                    "POST",
                    f"{chat_base}/chat/completions",
                    headers=chat_hdrs,
                    json={"model": model, "messages": upstream_messages, "stream": True},
                ) as response:
                    if response.status_code >= 400:
                        detail = await response.aread()
                        yield sse_event({"type": "error", "detail": f"上游接口错误：{detail.decode('utf-8', errors='ignore')}"})
                        return
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("data:"):
                            line = line[5:].strip()
                        if line == "[DONE]":
                            break
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(chunk, dict) and chunk.get("usage"):
                            raw_usage = chunk.get("usage")
                        delta = text_delta_from_chat_chunk(chunk)
                        if delta:
                            content_parts.append(delta)
                            yield sse_event({"type": "delta", "delta": delta})
        except httpx.HTTPError as exc:
            yield sse_event({"type": "error", "detail": f"请求上游接口失败：{exc}"})
            return

        assistant_message = {
            "id": uuid.uuid4().hex,
            "role": "assistant",
            "content": "".join(content_parts).strip() or "接口返回了空回复。",
            "created_at": now_ms(),
            "model": model,
            "raw_usage": raw_usage,
        }
        conversation["messages"].append(assistant_message)
        conversation["updated_at"] = now_ms()
        save_conversation(user_id, conversation)
        yield sse_event({"type": "done", "conversation": conversation, "message": assistant_message})

    return StreamingResponse(stream(), media_type="text/event-stream")

# --- 历史记录 ---

@app.get("/api/history")
async def get_history_api(type: str = None):
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if type:
                    data = [item for item in data if item.get("type", "zimage") == type]
                data = [item for item in data if item.get("images") and len(item["images"]) > 0]

                def sort_key(item):
                    ts = item.get("timestamp", 0)
                    if isinstance(ts, (int, float)):
                        return float(ts)
                    return 0

                data.sort(key=sort_key, reverse=True)
                return data
        except Exception as e:
            print(f"读取历史文件失败: {e}")
            return []
    return []

@app.get("/api/queue_status")
async def get_queue_status(client_id: str):
    with QUEUE_LOCK:
        total = len(QUEUE)
        positions = [i + 1 for i, t in enumerate(QUEUE) if t["client_id"] == client_id]
        position = positions[0] if positions else 0
    return {"total": total, "position": position}

@app.post("/api/history/delete")
async def delete_history(req: DeleteHistoryRequest):
    if not os.path.exists(HISTORY_FILE):
        return {"success": False, "message": "History file not found"}
    try:
        with HISTORY_LOCK:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                history = json.load(f)
            target_record = None
            new_history = []
            for item in history:
                is_match = False
                item_ts = item.get("timestamp", 0)
                if isinstance(req.timestamp, (int, float)) and isinstance(item_ts, (int, float)):
                    if abs(float(item_ts) - float(req.timestamp)) < 0.001:
                        is_match = True
                elif str(item_ts) == str(req.timestamp):
                    is_match = True
                if is_match:
                    target_record = item
                else:
                    new_history.append(item)
            if target_record:
                with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                    json.dump(new_history, f, ensure_ascii=False, indent=4)

        if target_record:
            for img_url in target_record.get("images", []):
                file_path = output_file_from_url(img_url)
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception as e:
                        print(f"Failed to delete file {file_path}: {e}")
            return {"success": True}
        else:
            return {"success": False, "message": "Record not found"}
    except Exception as e:
        print(f"Delete history error: {e}")
        return {"success": False, "message": str(e)}

# --- ModelScope 角度控制 ---

@app.post("/api/angle/poll_status")
async def poll_angle_cloud(req: CloudPollRequest):
    base_url = 'https://api-inference.modelscope.cn/'
    clean_token = (req.api_key or MODELSCOPE_API_KEY).strip()
    if not clean_token:
        raise HTTPException(status_code=400, detail="未提供 ModelScope API Key")

    headers = {
        "Authorization": f"Bearer {clean_token}",
        "Content-Type": "application/json",
        "X-ModelScope-Async-Mode": "true"
    }
    task_id = req.task_id
    print(f"Resuming polling for Angle Task: {task_id}")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for i in range(300):
                await asyncio.sleep(2)
                try:
                    result = await client.get(
                        f"{base_url}v1/tasks/{task_id}",
                        headers={**headers, "X-ModelScope-Task-Type": "image_generation"},
                    )
                    data = result.json()
                    status = data.get("task_status")

                    if status == "SUCCEED":
                        img_url = data["output_images"][0]
                        local_path = ""
                        try:
                            async with httpx.AsyncClient() as dl_client:
                                img_res = await dl_client.get(img_url)
                                if img_res.status_code == 200:
                                    filename = f"cloud_angle_{int(time.time())}.png"
                                    file_path = output_path_for(filename, "output")
                                    with open(file_path, "wb") as f:
                                        f.write(img_res.content)
                                    local_path = output_url_for(filename, "output")
                                else:
                                    local_path = img_url
                        except Exception:
                            local_path = img_url

                        record = {"timestamp": time.time(), "prompt": f"Resumed {task_id}", "images": [local_path], "type": "angle"}
                        save_to_history(record)
                        if req.client_id:
                            await manager.send_personal_message({"type": "cloud_status", "status": "SUCCEED", "task_id": task_id}, req.client_id)
                        return {"url": local_path}

                    elif status == "FAILED":
                        if req.client_id:
                            await manager.send_personal_message({"type": "cloud_status", "status": "FAILED", "task_id": task_id}, req.client_id)
                        raise Exception(f"ModelScope task failed: {data}")

                    if i % 5 == 0 and req.client_id:
                        await manager.send_personal_message({
                            "type": "cloud_status", "status": f"{status} ({i}/300)",
                            "task_id": task_id, "progress": i, "total": 300
                        }, req.client_id)

                except Exception as loop_e:
                    print(f"Angle polling error: {loop_e}")
                    continue

            if req.client_id:
                await manager.send_personal_message({"type": "cloud_status", "status": "TIMEOUT", "task_id": task_id}, req.client_id)
            return {"status": "timeout", "task_id": task_id, "message": "Task still pending"}

    except Exception as e:
        print(f"Angle polling error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/angle/generate")
async def generate_angle_cloud(req: CloudGenRequest):
    base_url = 'https://api-inference.modelscope.cn/'
    clean_token = (req.api_key or MODELSCOPE_API_KEY).strip()
    if not clean_token:
        raise HTTPException(status_code=400, detail="未提供 ModelScope API Key")

    headers = {
        "Authorization": f"Bearer {clean_token}",
        "Content-Type": "application/json",
        "X-ModelScope-Async-Mode": "true"
    }
    model = selected_model(req.model, "Qwen/Qwen-Image-Edit-2511")
    payload = {
        "model": model,
        "prompt": req.prompt.strip(),
        "image_url": [modelscope_image_url(url, max_size=1536) for url in req.image_urls]
    }
    if req.resolution:
        payload["size"] = modelscope_size(req.resolution)
    if req.loras is not None:
        payload["loras"] = req.loras

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            submit_res = await client.post(f"{base_url}v1/images/generations", headers=headers, json=payload)
            if submit_res.status_code != 200:
                try:
                    detail = submit_res.json()
                except:
                    detail = submit_res.text
                raise HTTPException(status_code=submit_res.status_code, detail=detail)

            task_id = submit_res.json().get("task_id")
            print(f"Angle Task submitted, ID: {task_id}")

            for i in range(300):
                await asyncio.sleep(2)
                try:
                    result = await client.get(
                        f"{base_url}v1/tasks/{task_id}",
                        headers={**headers, "X-ModelScope-Task-Type": "image_generation"},
                    )
                    data = result.json()
                    status = data.get("task_status")

                    if status == "SUCCEED":
                        img_url = data["output_images"][0]
                        local_path = ""
                        try:
                            async with httpx.AsyncClient() as dl_client:
                                img_res = await dl_client.get(img_url)
                                if img_res.status_code == 200:
                                    filename = f"cloud_angle_{int(time.time())}.png"
                                    file_path = output_path_for(filename, "output")
                                    with open(file_path, "wb") as f:
                                        f.write(img_res.content)
                                    local_path = output_url_for(filename, "output")
                                else:
                                    local_path = img_url
                        except Exception:
                            local_path = img_url

                        record = {"timestamp": time.time(), "prompt": req.prompt, "images": [local_path], "type": "angle"}
                        save_to_history(record)
                        if req.client_id:
                            await manager.send_personal_message({"type": "cloud_status", "status": "SUCCEED", "task_id": task_id}, req.client_id)
                        if GLOBAL_LOOP:
                            asyncio.run_coroutine_threadsafe(manager.broadcast_new_image(record), GLOBAL_LOOP)
                        return {"url": local_path, "task_id": task_id}

                    elif status == "FAILED":
                        if req.client_id:
                            await manager.send_personal_message({"type": "cloud_status", "status": "FAILED", "task_id": task_id}, req.client_id)
                        raise Exception(f"ModelScope task failed: {data}")

                    if i % 5 == 0 and req.client_id:
                        await manager.send_personal_message({
                            "type": "cloud_status", "status": f"{status} ({i}/300)",
                            "task_id": task_id, "progress": i, "total": 300
                        }, req.client_id)

                except Exception as loop_e:
                    print(f"Angle polling error: {loop_e}")
                    continue

            if req.client_id:
                await manager.send_personal_message({"type": "cloud_status", "status": "TIMEOUT", "task_id": task_id}, req.client_id)
            return {"status": "timeout", "task_id": task_id, "message": "Task still pending"}

    except HTTPException:
        raise
    except Exception as e:
        print(f"Angle generation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

# --- ModelScope Z-Image 云端生图 ---

@app.post("/generate")
async def generate_cloud(req: CloudGenRequest):
    base_url = 'https://api-inference.modelscope.cn/'
    clean_token = (req.api_key or MODELSCOPE_API_KEY).strip()
    if not clean_token:
        raise HTTPException(status_code=400, detail="未提供 ModelScope API Key")

    headers = {
        "Authorization": f"Bearer {clean_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "Tongyi-MAI/Z-Image-Turbo",
        "prompt": req.prompt.strip(),
        "size": modelscope_size(req.resolution),
        "n": 1
    }
    if req.loras is not None:
        payload["loras"] = req.loras

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            submit_res = await client.post(
                f"{base_url}v1/images/generations",
                headers={**headers, "X-ModelScope-Async-Mode": "true"},
                json=payload
            )
            if submit_res.status_code != 200:
                try:
                    detail = submit_res.json()
                except:
                    detail = submit_res.text
                raise HTTPException(status_code=submit_res.status_code, detail=detail)

            task_id = submit_res.json().get("task_id")
            print(f"Z-Image Task submitted, ID: {task_id}")

            for i in range(200):
                await asyncio.sleep(3)
                try:
                    result = await client.get(
                        f"{base_url}v1/tasks/{task_id}",
                        headers={**headers, "X-ModelScope-Task-Type": "image_generation"},
                    )
                    data = result.json()
                    status = data.get("task_status")

                    if i % 5 == 0:
                        print(f"Task {task_id} status check {i}: {status}")

                    if status == "SUCCEED":
                        img_url = data["output_images"][0]
                        local_path = ""
                        try:
                            async with httpx.AsyncClient() as dl_client:
                                img_res = await dl_client.get(img_url)
                                if img_res.status_code == 200:
                                    filename = f"cloud_{int(time.time())}.png"
                                    file_path = output_path_for(filename, "output")
                                    with open(file_path, "wb") as f:
                                        f.write(img_res.content)
                                    local_path = output_url_for(filename, "output")
                                else:
                                    local_path = img_url
                        except Exception as dl_e:
                            print(f"Download error: {dl_e}")
                            local_path = img_url

                        record = {"timestamp": time.time(), "prompt": req.prompt, "images": [local_path], "type": "cloud"}
                        save_to_history(record)
                        try:
                            await manager.broadcast_new_image(record)
                        except Exception:
                            pass
                        return {"url": local_path}

                    elif status == "FAILED":
                        raise Exception(f"ModelScope task failed: {data}")

                except Exception as loop_e:
                    print(f"Polling error (retrying): {loop_e}")
                    continue

            raise Exception("Cloud generation timeout")

    except HTTPException:
        raise
    except Exception as e:
        print(f"Cloud generation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

# --- ModelScope 通用图片生成（支持图生图） ---

@app.post("/api/ms/generate")
async def ms_generate(req: MsGenerateRequest):
    base_url = 'https://api-inference.modelscope.cn/'
    clean_token = (req.api_key or MODELSCOPE_API_KEY).strip()
    if not clean_token:
        raise HTTPException(status_code=400, detail="未配置 ModelScope API Key，请在 API 设置中填写，或重新保存 ModelScope Token。")

    headers = {
        "Authorization": f"Bearer {clean_token}",
        "Content-Type": "application/json",
        "X-ModelScope-Async-Mode": "true"
    }
    payload = {
        "model": req.model,
        "prompt": req.prompt.strip(),
    }
    if req.width and req.height:
        payload["width"] = req.width
        payload["height"] = req.height
        payload["size"] = modelscope_size(req.size or f"{req.width}x{req.height}")
    elif req.size:
        payload["size"] = modelscope_size(req.size)
    if req.image_urls:
        payload["image_url"] = [modelscope_image_url(url, max_size=1536) for url in req.image_urls]
    if req.loras is not None:
        payload["loras"] = req.loras

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            submit_res = await client.post(
                f"{base_url}v1/images/generations",
                headers=headers,
                json=payload
            )
            if submit_res.status_code != 200:
                try:
                    detail = submit_res.json()
                except:
                    detail = submit_res.text
                raise HTTPException(status_code=submit_res.status_code, detail=detail)

            task_id = submit_res.json().get("task_id")
            print(f"MS Generate Task submitted ({req.model}), ID: {task_id}")

            TERMINAL_FAILED_STATUSES = {"FAILED", "FAIL", "ERROR", "CANCELED", "CANCELLED", "TIMEOUT", "REVOKED"}

            for i in range(300):
                await asyncio.sleep(2)
                try:
                    result = await client.get(
                        f"{base_url}v1/tasks/{task_id}",
                        headers={**headers, "X-ModelScope-Task-Type": "image_generation"},
                    )
                    data = result.json()
                    status = data.get("task_status")
                    print(f"MS Task {task_id} poll {i}: status={status}")

                    if status == "SUCCEED":
                        img_url = data["output_images"][0]
                        local_path = ""
                        try:
                            async with httpx.AsyncClient() as dl_client:
                                img_res = await dl_client.get(img_url)
                                if img_res.status_code == 200:
                                    filename = f"ms_{req.model.replace('/', '_').replace(':', '_')}_{int(time.time())}.png"
                                    file_path = output_path_for(filename, "output")
                                    with open(file_path, "wb") as f:
                                        f.write(img_res.content)
                                    local_path = output_url_for(filename, "output")
                                else:
                                    local_path = img_url
                        except Exception:
                            local_path = img_url

                        record = {
                            "timestamp": time.time(),
                            "prompt": req.prompt,
                            "images": [local_path],
                            "type": "klein",
                            "model": req.model,
                        }
                        save_to_history(record)
                        if GLOBAL_LOOP:
                            asyncio.run_coroutine_threadsafe(manager.broadcast_new_image(record), GLOBAL_LOOP)
                        return {"url": local_path, "task_id": task_id}

                    elif status in TERMINAL_FAILED_STATUSES:
                        error_info = data.get("error_info") or data.get("message") or data.get("detail") or str(data)
                        raise HTTPException(status_code=502, detail=f"MS task {status}: {error_info}")

                except HTTPException:
                    raise
                except Exception as loop_e:
                    print(f"MS polling error: {loop_e}")
                    continue

            raise HTTPException(status_code=504, detail="MS 生图超时")

    except HTTPException:
        raise
    except Exception as e:
        print(f"MS generate error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

# --- 本地 ComfyUI 生图 ---

@app.post("/api/generate")
def generate(req: GenerateRequest):
    global NEXT_TASK_ID
    current_task = None
    target_backend = None
    with QUEUE_LOCK:
        task_id = NEXT_TASK_ID
        NEXT_TASK_ID += 1
        current_task = {"task_id": task_id, "client_id": req.client_id}
        QUEUE.append(current_task)

    try:
        required_images = []
        for node_id, node_inputs in req.params.items():
            if isinstance(node_inputs, dict) and "image" in node_inputs:
                image_name = node_inputs["image"]
                if isinstance(image_name, str) and image_name:
                    required_images.append(image_name)

        target_backend = get_best_backend(required_images)
        with LOAD_LOCK:
            BACKEND_LOCAL_LOAD[target_backend] += 1

        for image_name in required_images:
            need_sync = False
            try:
                check_url = f"http://{target_backend}/view?filename={urllib.parse.quote(image_name)}&type=input"
                resp = requests.get(check_url, stream=True, timeout=0.5)
                resp.close()
                if resp.status_code != 200:
                    need_sync = True
            except:
                need_sync = True

            if need_sync:
                image_content = None
                image_type = "image/png"
                for addr in COMFYUI_INSTANCES:
                    if addr == target_backend: continue
                    try:
                        src_url = f"http://{addr}/view?filename={urllib.parse.quote(image_name)}&type=input"
                        r = requests.get(src_url, timeout=5)
                        if r.status_code == 200:
                            image_content = r.content
                            image_type = r.headers.get("Content-Type", "image/png")
                            break
                    except: continue

                if image_content:
                    try:
                        files = {'image': (image_name, image_content, image_type)}
                        requests.post(f"http://{target_backend}/upload/image", files=files, timeout=10)
                    except Exception as e:
                        print(f"Sync upload failed: {e}")

        workflow_path = os.path.join(WORKFLOW_DIR, req.workflow_json)
        if not os.path.exists(workflow_path) and req.workflow_json == "Z-Image.json":
            workflow_path = WORKFLOW_PATH
        if not os.path.exists(workflow_path):
            raise Exception(f"Workflow file not found: {req.workflow_json}")

        with open(workflow_path, 'r', encoding='utf-8') as f:
            workflow = json.load(f)

        seed = random.randint(1, 10**15)

        if "23" in workflow and req.prompt:
            workflow["23"]["inputs"]["text"] = req.prompt
        if "144" in workflow:
            workflow["144"]["inputs"]["width"] = req.width
            workflow["144"]["inputs"]["height"] = req.height
        if "22" in workflow:
            workflow["22"]["inputs"]["seed"] = seed
        if "158" in workflow:
            workflow["158"]["inputs"]["noise_seed"] = seed
        for node_id in ["146", "181"]:
            if node_id in workflow and "inputs" in workflow[node_id] and "seed" in workflow[node_id]["inputs"]:
                workflow[node_id]["inputs"]["seed"] = seed
        if "184" in workflow and "inputs" in workflow["184"] and "seed" in workflow["184"]["inputs"]:
            workflow["184"]["inputs"]["seed"] = seed
        if "172" in workflow and "inputs" in workflow["172"] and "seed" in workflow["172"]["inputs"]:
            workflow["172"]["inputs"]["seed"] = seed % 4294967295
        if "14" in workflow and "inputs" in workflow["14"] and "seed" in workflow["14"]["inputs"]:
            workflow["14"]["inputs"]["seed"] = seed

        for node_id, node_inputs in req.params.items():
            if node_id in workflow:
                if "inputs" not in workflow[node_id]:
                    workflow[node_id]["inputs"] = {}
                for input_name, value in node_inputs.items():
                    workflow[node_id]["inputs"][input_name] = value

        p = {"prompt": workflow, "client_id": CLIENT_ID}
        data = json.dumps(p).encode('utf-8')
        try:
            post_req = urllib.request.Request(f"http://{target_backend}/prompt", data=data)
            prompt_id = json.loads(urllib.request.urlopen(post_req, timeout=10).read())['prompt_id']
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            raise Exception(f"HTTP Error {e.code}: {error_body}")

        history_data = None
        for i in range(COMFYUI_HISTORY_TIMEOUT):
            try:
                res = get_comfy_history(target_backend, prompt_id)
                if prompt_id in res:
                    history_data = res[prompt_id]
                    break
            except Exception:
                pass
            time.sleep(1)

        if not history_data:
            raise Exception("ComfyUI 渲染超时")

        local_images = []
        local_videos = []
        local_urls = []
        current_timestamp = time.time()
        if 'outputs' in history_data:
            for node_id in history_data['outputs']:
                node_output = history_data['outputs'][node_id]
                if 'images' in node_output:
                    for img in node_output['images']:
                        prefix = f"{req.type}_{int(current_timestamp)}_"
                        local_path = download_comfy_output(target_backend, img, prefix=prefix)
                        if req.convert_to_jpg:
                            local_path = convert_output_to_jpg(local_path)
                        local_images.append(local_path)
                        local_urls.append(local_path)
                for output_key in ("videos", "gifs", "animated"):
                    for video in node_output.get(output_key, []) or []:
                        if not isinstance(video, dict) or not video.get("filename"):
                            continue
                        prefix = f"{req.type}_{int(current_timestamp)}_"
                        local_path = download_comfy_output(target_backend, video, prefix=prefix)
                        local_videos.append(local_path)
                        local_urls.append(local_path)

        result = {
            "prompt": req.prompt if req.prompt else "Detail Enhance",
            "images": local_images,
            "videos": local_videos,
            "outputs": local_urls,
            "seed": seed,
            "timestamp": current_timestamp,
            "type": req.type,
            "workflow_json": req.workflow_json,
            "task_id": task_id,
            "prompt_id": prompt_id,
            "backend": target_backend,
            "params": req.params
        }
        save_to_history(result)
        if GLOBAL_LOOP:
            asyncio.run_coroutine_threadsafe(manager.broadcast_new_image(result), GLOBAL_LOOP)
        return result

    except Exception as e:
        return {"images": [], "error": str(e)}
    finally:
        if target_backend:
            with LOAD_LOCK:
                if BACKEND_LOCAL_LOAD.get(target_backend, 0) > 0:
                    BACKEND_LOCAL_LOAD[target_backend] -= 1
        if current_task:
            with QUEUE_LOCK:
                if current_task in QUEUE:
                    QUEUE.remove(current_task)

# --- ComfyUI 工作流管理 ---

BUILTIN_WORKFLOWS = {"Z-Image.json", "Z-Image-Enhance.json", "2511.json", "klein-enhance.json", "Flux2-Klein.json", "upscale.json"}
CUSTOM_WORKFLOW_FOLDER = "custom"
LEGACY_CUSTOM_WORKFLOW_FOLDER = "自定义"
WORKFLOW_NAME_RE = re.compile(rf"^(?:(?:{CUSTOM_WORKFLOW_FOLDER}|{LEGACY_CUSTOM_WORKFLOW_FOLDER})/)?[a-zA-Z0-9_一-龥\.\-]+\.json$")

class WorkflowField(BaseModel):
    id: str
    node: str = ""
    input: str = ""
    name: str = ""
    type: str = "text"
    default: Any = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    options: List[str] = []

class WorkflowConfig(BaseModel):
    title: str = ""
    fields: List[WorkflowField] = []
    mini_cards: Dict[str, Any] = {}

class WorkflowUploadRequest(BaseModel):
    name: str
    workflow: Dict[str, Any]

class WorkflowRunRequest(BaseModel):
    fields: Dict[str, Any] = {}
    config: WorkflowConfig
    client_id: str = ""

def workflow_path_from_name(name: str) -> str:
    if not WORKFLOW_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid workflow name")
    path = os.path.abspath(os.path.join(WORKFLOW_DIR, *name.split("/")))
    workflow_root = os.path.abspath(WORKFLOW_DIR)
    if os.path.commonpath([workflow_root, path]) != workflow_root:
        raise HTTPException(status_code=400, detail="Invalid workflow name")
    return path

def workflow_config_path(name: str) -> str:
    return workflow_path_from_name(name).replace(".json", ".config.json")

def is_builtin_workflow(name: str) -> bool:
    return "/" not in name and os.path.basename(name) in BUILTIN_WORKFLOWS

class ComfyInstancesPayload(BaseModel):
    instances: List[str] = []

@app.get("/api/comfyui/instances")
def get_comfyui_instances():
    return {"instances": COMFYUI_INSTANCES}

@app.put("/api/comfyui/instances")
def save_comfyui_instances(payload: ComfyInstancesPayload):
    # 宽容校验：去前后空白、去 http(s):// 前缀、去尾部斜杠；要求形如 host:port
    cleaned = []
    for item in payload.instances:
        s = str(item or "").strip()
        if not s:
            continue
        s = re.sub(r"^https?://", "", s)
        s = s.rstrip("/")
        if ":" not in s:
            raise HTTPException(status_code=400, detail=f"地址缺少端口号：{item}（应为 host:port，例如 127.0.0.1:8188）")
        host, _, port = s.rpartition(":")
        if not host or not port.isdigit():
            raise HTTPException(status_code=400, detail=f"地址不合法：{item}（应为 host:port，例如 127.0.0.1:8188）")
        if s in cleaned:
            continue
        cleaned.append(s)
    if not cleaned:
        raise HTTPException(status_code=400, detail="至少保留一个 ComfyUI 后端地址")
    # 写入 env 文件
    try:
        update_env_values({"COMFYUI_INSTANCES": ",".join(cleaned)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入 env 失败：{e}")
    # 更新进程中的全局变量
    global COMFYUI_INSTANCES, COMFYUI_ADDRESS, BACKEND_LOCAL_LOAD
    COMFYUI_INSTANCES = cleaned
    COMFYUI_ADDRESS = cleaned[0]
    new_load = {addr: 0 for addr in cleaned}
    for addr, n in (BACKEND_LOCAL_LOAD or {}).items():
        if addr in new_load:
            new_load[addr] = n
    BACKEND_LOCAL_LOAD = new_load
    return {"instances": COMFYUI_INSTANCES}

@app.get("/api/workflows")
def list_workflows():
    if not os.path.isdir(WORKFLOW_DIR):
        return {"workflows": []}
    items = []
    for root, dirs, files in os.walk(WORKFLOW_DIR):
        if os.path.abspath(root) == os.path.abspath(WORKFLOW_DIR):
            dirs[:] = [d for d in dirs if d in {CUSTOM_WORKFLOW_FOLDER, LEGACY_CUSTOM_WORKFLOW_FOLDER}]
        for fn in sorted(files):
            if not fn.endswith(".json") or fn.endswith(".config.json"):
                continue
            rel = os.path.relpath(os.path.join(root, fn), WORKFLOW_DIR).replace("\\", "/")
            if is_builtin_workflow(rel):
                continue
            cfg = {}
            cfg_path = workflow_config_path(rel)
            if os.path.exists(cfg_path):
                try:
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        cfg = json.load(f) or {}
                except Exception:
                    cfg = {}
            items.append({
                "name": rel,
                "title": cfg.get("title") or fn.replace(".json", ""),
                "builtin": False,
                "field_count": len(cfg.get("fields") or []),
            })
    items.sort(key=lambda item: (0 if item["name"].startswith(f"{CUSTOM_WORKFLOW_FOLDER}/") else 1, item["title"]))
    return {"workflows": items}

@app.get("/api/workflows/{name:path}")
def get_workflow(name: str):
    if not WORKFLOW_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid workflow name")
    workflow_path = workflow_path_from_name(name)
    if not os.path.exists(workflow_path):
        raise HTTPException(status_code=404, detail="Workflow not found")
    with open(workflow_path, "r", encoding="utf-8") as f:
        workflow = json.load(f)
    cfg = {"title": name.replace(".json", ""), "fields": []}
    cfg_path = workflow_config_path(name)
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f) or cfg
        except Exception:
            pass
    return {"name": name, "workflow": workflow, "config": cfg, "builtin": is_builtin_workflow(name)}

@app.post("/api/workflows")
def upload_workflow(payload: WorkflowUploadRequest):
    name = os.path.basename(payload.name.strip())
    if not name.endswith(".json"):
        name = name + ".json"
    if not WORKFLOW_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="工作流名称不合法，请使用中文/英文/数字/_-.")
    if not isinstance(payload.workflow, dict) or not payload.workflow:
        raise HTTPException(status_code=400, detail="工作流 JSON 为空")
    # 简单校验：是 API 格式（节点 id 为 key，含 class_type）
    sample = next(iter(payload.workflow.values()), None)
    if not isinstance(sample, dict) or "class_type" not in sample:
        raise HTTPException(status_code=400, detail="不是有效的 ComfyUI API 工作流 JSON（需包含 class_type）")
    custom_dir = os.path.join(WORKFLOW_DIR, CUSTOM_WORKFLOW_FOLDER)
    os.makedirs(custom_dir, exist_ok=True)
    stored_name = f"{CUSTOM_WORKFLOW_FOLDER}/{name}"
    path = workflow_path_from_name(stored_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload.workflow, f, ensure_ascii=False, indent=2)
    return {"name": stored_name}

@app.put("/api/workflows/{name:path}/config")
def save_workflow_config(name: str, payload: WorkflowConfig):
    if not WORKFLOW_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid workflow name")
    workflow_path = workflow_path_from_name(name)
    if not os.path.exists(workflow_path):
        raise HTTPException(status_code=404, detail="Workflow not found")
    cfg_path = workflow_config_path(name)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(payload.dict(), f, ensure_ascii=False, indent=2)
    return {"config": payload.dict()}

@app.delete("/api/workflows/{name:path}")
def delete_workflow(name: str):
    if not WORKFLOW_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid workflow name")
    if is_builtin_workflow(name):
        raise HTTPException(status_code=400, detail="内置工作流不可删除")
    workflow_path = workflow_path_from_name(name)
    cfg_path = workflow_config_path(name)
    if not os.path.exists(workflow_path):
        raise HTTPException(status_code=404, detail="Workflow not found")
    os.remove(workflow_path)
    if os.path.exists(cfg_path):
        os.remove(cfg_path)
    return {"ok": True}

@app.post("/api/workflows/{name:path}/run")
def run_workflow(name: str, payload: WorkflowRunRequest):
    if not WORKFLOW_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid workflow name")
    if not os.path.exists(workflow_path_from_name(name)):
        raise HTTPException(status_code=404, detail="Workflow not found")
    # 根据 config 的字段把值映射成 params 节点覆盖
    params: Dict[str, Dict[str, Any]] = {}
    for field in payload.config.fields:
        if not field.node or not field.input:
            continue
        if field.id in payload.fields:
            value = payload.fields[field.id]
            # 类型转换
            if field.type in ("number", "slider"):
                try:
                    value = float(value) if (field.step and field.step < 1) else int(float(value))
                except Exception:
                    pass
            elif field.type == "boolean":
                value = bool(value)
            elif field.type == "dropdown":
                # 下拉值如果看起来是数字（如 "1024" / "2048" / "0.8"），自动转成 int/float
                if isinstance(value, str):
                    s = value.strip()
                    try:
                        if s and ('.' in s or 'e' in s.lower()):
                            value = float(s)
                        elif s and (s.lstrip('-').isdigit()):
                            value = int(s)
                    except (ValueError, TypeError):
                        pass
            params.setdefault(field.node, {})[field.input] = value
    req = GenerateRequest(
        prompt="",
        workflow_json=name,
        params=params,
        type="workflow-test",
        client_id=payload.client_id or str(uuid.uuid4()),
    )
    return generate(req)

# ============================================================
#  资源库 API
# ============================================================

@app.get("/api/library/sources")
def library_list_sources():
    sources = load_library_sources()
    images = load_library_images()
    count_map = {}
    for img in images:
        sid = img.get("source_id", "")
        count_map[sid] = count_map.get(sid, 0) + 1
    result = []
    for s in sources:
        result.append({**s, "image_count": count_map.get(s["id"], 0)})
    return {"sources": result}

@app.get("/api/library/browse")
def library_browse(path: str = ""):
    if not path:
        # 返回常用根目录
        home = os.path.expanduser("~")
        drives = []
        if os.name == "nt":
            import string
            for letter in string.ascii_uppercase:
                dp = f"{letter}:\\"
                if os.path.isdir(dp):
                    drives.append({"name": dp, "path": dp})
        return {"current": "", "parent": "", "entries": drives, "roots": drives}
    # Windows: "D:" -> "D:\\" 保证是磁盘根目录而非当前工作目录
    if os.name == "nt" and len(path) == 2 and path[1] == ":":
        path = path + "\\"
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise HTTPException(status_code=400, detail="路径不存在或不是文件夹")
    parent = os.path.dirname(path)
    if parent == path:
        parent = ""
    entries = []
    try:
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if os.path.isdir(full) and not name.startswith("."):
                entries.append({"name": name, "path": full})
    except PermissionError:
        pass
    return {"current": path, "parent": parent, "entries": entries}

@app.post("/api/library/sources")
def library_create_source(req: LibrarySourceCreate):
    sources = load_library_sources()
    src = {
        "id": f"src_{uuid.uuid4().hex[:10]}",
        "name": req.name,
        "type": req.type,
        "path": req.path,
        "url": req.url,
        "api_key": req.api_key,
        "enabled": True,
        "created_at": now_ms(),
        "updated_at": now_ms(),
        "last_scan_at": None,
    }
    sources.append(src)
    save_library_sources(sources)
    if src["type"] == "local" and src["path"]:
        os.makedirs(os.path.join(LIBRARY_DIR, src["id"], "thumbs"), exist_ok=True)
    return {"source": src}

@app.put("/api/library/sources/{source_id}")
def library_update_source(source_id: str, req: LibrarySourceUpdate):
    sources = load_library_sources()
    src = next((s for s in sources if s["id"] == source_id), None)
    if not src:
        raise HTTPException(status_code=404, detail="来源不存在")
    if req.name is not None:
        src["name"] = req.name
    if req.enabled is not None:
        src["enabled"] = req.enabled
    if req.path is not None:
        src["path"] = req.path
    if req.url is not None:
        src["url"] = req.url
    if req.api_key is not None:
        src["api_key"] = req.api_key
    src["updated_at"] = now_ms()
    save_library_sources(sources)
    return {"source": src}

@app.delete("/api/library/sources/{source_id}")
def library_delete_source(source_id: str):
    sources = load_library_sources()
    before = len(sources)
    sources = [s for s in sources if s["id"] != source_id]
    if len(sources) == before:
        raise HTTPException(status_code=404, detail="来源不存在")
    save_library_sources(sources)
    # 同时删除该来源下所有图片记录
    images = load_library_images()
    images = [img for img in images if img.get("source_id") != source_id]
    save_library_images(images)
    return {"ok": True}

@app.post("/api/library/sources/{source_id}/scan")
def library_scan_source(source_id: str):
    sources = load_library_sources()
    src = next((s for s in sources if s["id"] == source_id), None)
    if not src:
        raise HTTPException(status_code=404, detail="来源不存在")
    if src.get("type") != "local":
        raise HTTPException(status_code=400, detail="暂仅支持本地文件夹扫描")
    folder = src.get("path", "")
    if not folder or not os.path.isdir(folder):
        raise HTTPException(status_code=400, detail="文件夹路径不存在")
    supported = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
    images = load_library_images()
    existing_paths = {img.get("local_path") for img in images if img.get("source_id") == source_id}
    added = 0
    thumb_dir = os.path.join(LIBRARY_DIR, source_id, "thumbs")
    os.makedirs(thumb_dir, exist_ok=True)
    for root, dirs, files in os.walk(folder):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in supported:
                continue
            full_path = os.path.join(root, fname)
            if full_path in existing_paths:
                continue
            try:
                with Image.open(full_path) as img:
                    img.load()
                    w, h = img.size
                    thumb = img.copy()
                    thumb.thumbnail((256, 256))
                    if thumb.mode not in ("RGB", "RGBA"):
                        thumb = thumb.convert("RGB")
                    thumb_name = f"thumb_{uuid.uuid4().hex[:8]}.jpg"
                    thumb.save(os.path.join(thumb_dir, thumb_name), "JPEG", quality=80)
            except Exception:
                continue
            rel = os.path.relpath(full_path, folder).replace("\\", "/")
            asset_id = re.sub(r"_(?:diff|diffuse)_1k$", "", os.path.splitext(fname)[0], flags=re.I)
            builtin_meta = BUILTIN_MATERIAL_METADATA.get(asset_id) if os.path.abspath(folder) == os.path.abspath(BUILTIN_MATERIAL_DIR) else None
            material_label, material_categories, material_tags = builtin_meta or ("", [], [])
            image_record = {
                "id": f"img_{uuid.uuid4().hex[:12]}",
                "source_id": source_id,
                "filename": fname,
                "local_path": full_path,
                "url": f"/api/library/file/{source_id}/{rel}",
                "thumb_url": f"/api/library/thumb/{source_id}/{thumb_name}",
                "width": w,
                "height": h,
                "size_bytes": os.path.getsize(full_path),
                "categories": material_categories,
                "tags": material_tags,
                "ai_tags": [],
                "ai_tagged": False,
                "ai_tag_model": "",
                "manual_tags": material_tags,
                "favorited": False,
                "scope": "shared",
                "project_id": "",
                "notes": f"{material_label}；Poly Haven CC0；https://polyhaven.com/a/{asset_id}" if material_label else "",
                "created_at": now_ms(),
                "updated_at": now_ms(),
            }
            images.append(image_record)
            added += 1
    save_library_images(images)
    # 更新来源的 last_scan_at
    src["last_scan_at"] = now_ms()
    save_library_sources(sources)
    return {"added": added}

def ensure_builtin_material_library():
    if not os.path.isdir(BUILTIN_MATERIAL_DIR):
        return
    sources = load_library_sources()
    source = next((item for item in sources if item.get("id") == BUILTIN_MATERIAL_SOURCE_ID or os.path.abspath(str(item.get("path") or "")) == os.path.abspath(BUILTIN_MATERIAL_DIR)), None)
    if source is None:
        source = {
            "id": BUILTIN_MATERIAL_SOURCE_ID,
            "name": "常规建筑材质 · Poly Haven CC0",
            "type": "local",
            "path": BUILTIN_MATERIAL_DIR,
            "url": "https://polyhaven.com/textures",
            "api_key": "",
            "enabled": True,
            "created_at": now_ms(),
            "updated_at": now_ms(),
            "last_scan_at": None,
        }
        sources.append(source)
    else:
        source["path"] = BUILTIN_MATERIAL_DIR
        source["enabled"] = True
    save_library_sources(sources)
    try:
        library_scan_source(source["id"])
    except Exception as exc:
        print(f"[materials] built-in material scan skipped: {exc}")

@app.post("/api/library/import")
def library_import_images(req: LibraryImportRequest):
    return import_urls_into_library(
        urls=req.urls,
        items=req.items,
        source_name=req.source_name,
        canvas_id=req.canvas_id,
        canvas_title=req.canvas_title,
        node_id=req.node_id,
        manual_tags=req.manual_tags,
        categories=req.categories,
        project_id=req.project_id,
    )

def archlib_url_for_path(path: str) -> str:
    rel = os.path.relpath(path, ARCHLIB_DIR).replace("\\", "/")
    return f"/api/archlib/file/{urllib.parse.quote(rel, safe='/')}"

def archlib_page_image_for_metadata(metadata_path: str, data: Dict[str, Any]):
    case_root = os.path.dirname(os.path.dirname(metadata_path))
    page_number = data.get("page_number")
    if not page_number:
        match = re.search(r"page_(\d+)\.json$", os.path.basename(metadata_path), re.I)
        page_number = int(match.group(1)) if match else 0
    candidates = []
    if page_number:
        stem = f"page_{int(page_number):03d}"
        candidates.extend([
            os.path.join(case_root, "pages", f"{stem}.jpg"),
            os.path.join(case_root, "pages", f"{stem}.jpeg"),
            os.path.join(case_root, "pages", f"{stem}.png"),
            os.path.join(case_root, "pages", f"{stem}.webp"),
        ])
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None

def build_archlib_material_index(force: bool = False):
    now = time.time()
    if not force and ARCHLIB_MATERIAL_CACHE.get("items") and now - float(ARCHLIB_MATERIAL_CACHE.get("built_at") or 0) < 300:
        return ARCHLIB_MATERIAL_CACHE["items"]
    items = []
    if not os.path.isdir(ARCHLIB_CASE_DIR):
        ARCHLIB_MATERIAL_CACHE.update({"built_at": now, "items": []})
        return []
    for root, dirs, files in os.walk(ARCHLIB_CASE_DIR):
        if os.path.basename(root) != "metadata":
            continue
        for fname in files:
            if not fname.lower().endswith(".json"):
                continue
            metadata_path = os.path.join(root, fname)
            try:
                with open(metadata_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            materials = [str(x).strip() for x in (data.get("materials") or []) if str(x).strip()]
            if not materials:
                continue
            image_path = archlib_page_image_for_metadata(metadata_path, data)
            if not image_path:
                continue
            case_id = str(data.get("case_id") or os.path.basename(os.path.dirname(os.path.dirname(metadata_path))))
            page_number = int(data.get("page_number") or 0)
            common_tags = []
            for key in ("space_types", "elements", "style", "design_topics", "design_strategy"):
                common_tags.extend([str(x).strip() for x in (data.get(key) or []) if str(x).strip()])
            notes = str(data.get("reference_value") or data.get("text_summary") or "").strip()
            for material in materials:
                digest = hashlib.sha1(f"{image_path}|{material}".encode("utf-8", "ignore")).hexdigest()[:16]
                items.append({
                    "id": f"archlib_{digest}",
                    "filename": os.path.basename(image_path),
                    "url": archlib_url_for_path(image_path),
                    "thumb_url": archlib_url_for_path(image_path),
                    "width": 0,
                    "height": 0,
                    "source_id": "archlib",
                    "source_name": "ArchLib 案例库",
                    "categories": [material],
                    "tags": common_tags[:16],
                    "material_label": material,
                    "case_id": case_id,
                    "case_name": case_id,
                    "page_number": page_number,
                    "notes": notes,
                    "year": data.get("year"),
                    "location": data.get("location") or "",
                    "archlib": True,
                })
    items.sort(key=lambda x: (str(x.get("year") or ""), str(x.get("case_id") or ""), int(x.get("page_number") or 0)), reverse=True)
    ARCHLIB_MATERIAL_CACHE.update({"built_at": now, "items": items})
    return items

@app.get("/api/archlib/materials")
def archlib_list_materials(q: str = "", material: str = "", page: int = 1, page_size: int = 80):
    page_size = max(1, min(200, int(page_size or 80)))
    page = max(1, int(page or 1))
    result = build_archlib_material_index()
    if material:
        ml = material.lower()
        result = [item for item in result if ml in str(item.get("material_label") or "").lower()]
    if q:
        ql = q.lower()
        def match_item(item):
            text = " ".join([
                str(item.get("material_label") or ""),
                str(item.get("case_name") or ""),
                str(item.get("notes") or ""),
                " ".join(item.get("categories") or []),
                " ".join(item.get("tags") or []),
            ]).lower()
            return ql in text
        result = [item for item in result if match_item(item)]
    total = len(result)
    start = (page - 1) * page_size
    return {"materials": result[start:start + page_size], "total": total, "page": page, "page_size": page_size, "root_exists": os.path.isdir(ARCHLIB_CASE_DIR)}

@app.get("/api/archlib/file/{path:path}")
def archlib_serve_file(path: str):
    full = archlib_file_from_url(f"/api/archlib/file/{path}")
    if not full:
        raise HTTPException(status_code=404, detail="ArchLib 文件不存在")
    return FileResponse(full, media_type=content_type_for_path(full))

@app.get("/api/library/images")
def library_list_images(
    source_id: str = "",
    category: str = "",
    tag: str = "",
    q: str = "",
    favorited: Optional[str] = None,
    ai_tagged: Optional[str] = None,
    scope: str = "all",
    project_id: str = "",
    page: int = 1,
    page_size: int = 50,
):
    images = load_library_images()
    source_map = {str(s.get("id") or ""): s for s in load_library_sources()}
    result = [
        enrich_library_image_record(item, source_map, project_id)
        for item in filter_library_images_by_scope(images, scope, project_id)
    ]
    if source_id:
        result = [img for img in result if img.get("source_id") == source_id]
    if category:
        result = [img for img in result if category in (img.get("categories") or [])]
    if tag:
        result = [img for img in result if tag in (img.get("tags") or []) or tag in (img.get("ai_tags") or []) or tag in (img.get("manual_tags") or [])]
    if q:
        ql = q.lower()
        def match_q(img):
            searchable = " ".join([
                img.get("filename", ""),
                " ".join(img.get("categories", [])),
                " ".join(img.get("tags", [])),
                " ".join(img.get("ai_tags", [])),
                " ".join(img.get("manual_tags", [])),
                img.get("notes", ""),
            ]).lower()
            return ql in searchable
        result = [img for img in result if match_q(img)]
    if favorited == "true":
        result = [img for img in result if img.get("favorited")]
    if ai_tagged == "true":
        result = [img for img in result if img.get("ai_tagged")]
    elif ai_tagged == "false":
        result = [img for img in result if not img.get("ai_tagged")]
    total = len(result)
    start = max(0, (page - 1) * page_size)
    end = start + page_size
    return {"images": result[start:end], "total": total, "page": page, "page_size": page_size}

@app.get("/api/library/images/{image_id}")
def library_get_image(image_id: str, project_id: str = ""):
    images = load_library_images()
    img = next((i for i in images if i["id"] == image_id), None)
    if not img:
        raise HTTPException(status_code=404, detail="图片不存在")
    return {"image": enrich_library_image_record(img, project_id=project_id)}

@app.put("/api/library/images/{image_id}")
def library_update_image(image_id: str, req: LibraryImageUpdate, project_id: str = ""):
    images = load_library_images()
    img = next((i for i in images if i["id"] == image_id), None)
    if not img:
        raise HTTPException(status_code=404, detail="图片不存在")
    if req.categories is not None:
        img["categories"] = req.categories
    if req.manual_tags is not None:
        img["manual_tags"] = req.manual_tags
        img["tags"] = list(set((img.get("ai_tags") or []) + req.manual_tags))
    if req.favorited is not None:
        img["favorited"] = req.favorited
    if req.notes is not None:
        img["notes"] = req.notes
    img["updated_at"] = now_ms()
    save_library_images(images)
    return {"image": enrich_library_image_record(img, project_id=project_id)}

@app.post("/api/library/images/{image_id}/feedback")
def library_record_feedback(image_id: str, req: AssetFeedbackRequest):
    images = load_library_images()
    image = next((item for item in images if item.get("id") == image_id), None)
    if not image:
        raise HTTPException(status_code=404, detail="图片不存在")
    asset = library_asset_for_project(image, req.project_id, create=True)
    if not asset:
        raise HTTPException(status_code=400, detail="素材不可用于当前项目")
    try:
        feedback = DOMAIN_STORE.record_preference_event(req.project_id, asset["id"], req.event_type, req.context)
        candidate = refresh_project_preference_skill_candidate(req.project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "image": enrich_library_image_record(image, project_id=req.project_id),
        "asset_id": asset["id"],
        "feedback": feedback,
        "preference_profile": build_project_preference_profile(req.project_id),
        "skill_candidate": candidate,
    }

@app.post("/api/library/images/{image_id}/copy")
def library_copy_image(image_id: str, req: LibraryImageCopyRequest):
    images = load_library_images()
    source = next((item for item in images if item.get("id") == image_id), None)
    if not source:
        raise HTTPException(status_code=404, detail="图片不存在")
    target_scope = str(req.target_scope or "project").strip().lower()
    if target_scope not in {"project", "shared"}:
        raise HTTPException(status_code=400, detail="目标范围必须是 project 或 shared")
    target_project_id = ""
    if target_scope == "project":
        target_project_id = str(req.project_id or "").strip()
        if not target_project_id or not DOMAIN_STORE.get_project(target_project_id):
            raise HTTPException(status_code=400, detail="目标项目不存在")
    timestamp = now_ms()
    copied = dict(source)
    copied.update({
        "id": f"img_{uuid.uuid4().hex[:12]}",
        "scope": target_scope,
        "project_id": target_project_id,
        "copied_from_image_id": source.get("id") or "",
        "copied_from_project_id": source.get("project_id") or "",
        "created_at": timestamp,
        "updated_at": timestamp,
    })
    source_asset_id = str(source.get("asset_id") or "")
    if target_scope == "project":
        asset_id = f"asset_{uuid.uuid4().hex}"
        DOMAIN_STORE.register_asset(
            target_project_id,
            str(source.get("url") or ""),
            asset_id=asset_id,
            title=str(source.get("filename") or ""),
            source="shared_library_copy" if source.get("scope") == "shared" else "project_library_copy",
            width=int(source.get("width") or 0),
            height=int(source.get("height") or 0),
            byte_size=int(source.get("size_bytes") or 0),
            metadata={
                "library_image_id": copied["id"],
                "copied_from_library_image_id": source.get("id") or "",
                "source_asset_id": source_asset_id,
            },
        )
        copied["asset_id"] = asset_id
    else:
        copied["source_asset_id"] = source_asset_id
        copied["asset_id"] = ""
    images.append(copied)
    save_library_images(images)
    return {"image": enrich_library_image_record(copied, project_id=target_project_id), "source_image_id": image_id}

@app.delete("/api/library/images/{image_id}")
def library_delete_image(image_id: str):
    images = load_library_images()
    before = len(images)
    images = [i for i in images if i["id"] != image_id]
    if len(images) == before:
        raise HTTPException(status_code=404, detail="图片不存在")
    save_library_images(images)
    return {"ok": True}

@app.get("/api/library/categories")
def library_get_categories():
    return {"categories": load_library_categories()}

@app.put("/api/library/categories")
def library_update_categories(req: LibraryCategoryUpdate):
    cats = load_library_categories()
    cats["custom"] = req.custom
    save_library_categories(cats)
    return {"categories": cats}

@app.get("/api/library/stats")
def library_stats(scope: str = "all", project_id: str = ""):
    sources = load_library_sources()
    images = [
        enrich_library_image_record(item, project_id=project_id)
        for item in filter_library_images_by_scope(load_library_images(), scope, project_id)
    ]
    total = len(images)
    tagged = sum(1 for img in images if img.get("ai_tagged"))
    fav = sum(1 for img in images if img.get("favorited"))
    cat_counts = {}
    for img in images:
        for c in (img.get("categories") or []):
            cat_counts[c] = cat_counts.get(c, 0) + 1
    return {
        "total_images": total,
        "total_sources": len(sources),
        "tagged": tagged,
        "untagged": total - tagged,
        "favorited": fav,
        "category_counts": cat_counts,
    }

@app.get("/api/prompt-sources/awesome-gpt-image-2/status")
def awesome_gpt_image_2_status_api():
    return awesome_gpt_image_2_status()

@app.post("/api/prompt-sources/awesome-gpt-image-2/sync")
def awesome_gpt_image_2_sync_api():
    os.makedirs(EXTERNAL_DIR, exist_ok=True)
    tmp_dir = os.path.join(EXTERNAL_DIR, f".awesome-gpt-image-2-{uuid.uuid4().hex[:8]}")
    try:
        if os.path.isdir(os.path.join(AWESOME_GPT_IMAGE_2_DIR, ".git")):
            subprocess.run(
                ["git", "-C", AWESOME_GPT_IMAGE_2_DIR, "pull", "--ff-only"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=180,
            )
        else:
            if os.path.exists(AWESOME_GPT_IMAGE_2_DIR):
                shutil.rmtree(AWESOME_GPT_IMAGE_2_DIR)
            subprocess.run(
                ["git", "clone", "--depth", "1", AWESOME_GPT_IMAGE_2_REPO, tmp_dir],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,
            )
            os.replace(tmp_dir, AWESOME_GPT_IMAGE_2_DIR)
    except subprocess.CalledProcessError as e:
        if os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        detail = (e.stderr or e.stdout or str(e))[-800:]
        raise HTTPException(status_code=502, detail=f"同步提示词案例库失败：{detail}")
    except Exception as e:
        if os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"同步提示词案例库失败：{e}")
    if not awesome_gpt_image_2_synced():
        raise HTTPException(status_code=500, detail="同步完成但缺少 cases/style/images 数据")
    return {"ok": True, **awesome_gpt_image_2_status()}

@app.get("/api/prompt-sources/awesome-gpt-image-2/library")
def awesome_gpt_image_2_library_api():
    library = build_awesome_gpt_image_2_library()
    if not library:
        raise HTTPException(status_code=404, detail="尚未同步提示词案例库")
    return {"library": library}

@app.get("/api/prompt-sources/awesome-gpt-image-2/images/{filename}")
def awesome_gpt_image_2_image(filename: str):
    name = awesome_safe_image_name(filename)
    if not name:
        raise HTTPException(status_code=400, detail="图片文件名无效")
    images_dir = os.path.abspath(awesome_gpt_image_2_paths()["images"])
    full = os.path.abspath(os.path.join(images_dir, name))
    if os.path.commonpath([images_dir, full]) != images_dir:
        raise HTTPException(status_code=403, detail="路径越界")
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="示例图不存在")
    return FileResponse(full, media_type=content_type_for_path(full))

@app.get("/api/prompt-libraries")
def prompt_libraries_list():
    return public_prompt_libraries()

@app.post("/api/prompt-libraries")
def prompt_libraries_create(req: PromptLibraryRequest):
    data = load_prompt_libraries()
    now = now_ms()
    library = {
        "id": f"plib_{uuid.uuid4().hex[:10]}",
        "name": sanitize_prompt_name(req.name, "提示词库"),
        "type": "prompt",
        "readonly": False,
        "categories": default_prompt_categories(),
        "items": [],
        "created_at": now,
        "updated_at": now,
    }
    data["libraries"].append(library)
    save_prompt_libraries(data)
    return {"library": library, **public_prompt_libraries()}

@app.patch("/api/prompt-libraries/{library_id}")
def prompt_libraries_update(library_id: str, req: PromptLibraryRequest):
    data = load_prompt_libraries()
    library = find_prompt_library(data, library_id)
    if not library:
        raise HTTPException(status_code=404, detail="提示词库不存在")
    library["name"] = sanitize_prompt_name(req.name, library.get("name") or "提示词库")
    library["updated_at"] = now_ms()
    save_prompt_libraries(data)
    return {"library": library, **public_prompt_libraries()}

@app.delete("/api/prompt-libraries/{library_id}")
def prompt_libraries_delete(library_id: str):
    if library_id == "system":
        raise HTTPException(status_code=400, detail="常用提示词库不能删除")
    data = load_prompt_libraries()
    before = len(data["libraries"])
    data["libraries"] = [lib for lib in data["libraries"] if lib.get("id") != library_id]
    if len(data["libraries"]) == before:
        raise HTTPException(status_code=404, detail="提示词库不存在")
    save_prompt_libraries(data)
    return {"ok": True, **public_prompt_libraries()}

@app.post("/api/prompt-libraries/items")
def prompt_libraries_create_item(req: PromptLibraryItemRequest):
    data = load_prompt_libraries()
    library = find_prompt_library(data, req.library_id or "system")
    if not library:
        raise HTTPException(status_code=404, detail="提示词库不存在")
    now = now_ms()
    item = normalize_prompt_library_item({
        "id": f"prompt_{uuid.uuid4().hex[:12]}",
        "name": req.name,
        "category": req.category,
        "positive": req.positive,
        "negative": req.negative,
        "scene": req.scene,
        "created_at": now,
        "updated_at": now,
    })
    category_ids = {cat.get("id") for cat in library.get("categories", [])}
    if item["category"] not in category_ids:
        library.setdefault("categories", []).append({"id": item["category"], "name": item["category"]})
    library.setdefault("items", []).insert(0, item)
    library["updated_at"] = now
    save_prompt_libraries(data)
    return {"item": item, **public_prompt_libraries()}

@app.patch("/api/prompt-libraries/items/{item_id}")
def prompt_libraries_update_item(item_id: str, req: PromptLibraryItemRequest):
    data = load_prompt_libraries()
    target_library = None
    target_item = None
    for library in data["libraries"]:
        for item in library.get("items", []):
            if item.get("id") == item_id:
                target_library = library
                target_item = item
                break
        if target_item:
            break
    if not target_item or not target_library:
        raise HTTPException(status_code=404, detail="提示词不存在")
    target_item.update({
        "name": sanitize_prompt_name(req.name, target_item.get("name") or "提示词"),
        "category": prompt_item_category(req.category),
        "positive": sanitize_prompt_text(req.positive),
        "negative": sanitize_prompt_text(req.negative),
        "scene": sanitize_prompt_text(req.scene),
        "updated_at": now_ms(),
    })
    category_ids = {cat.get("id") for cat in target_library.get("categories", [])}
    if target_item["category"] not in category_ids:
        target_library.setdefault("categories", []).append({"id": target_item["category"], "name": target_item["category"]})
    target_library["updated_at"] = now_ms()
    save_prompt_libraries(data)
    return {"item": target_item, **public_prompt_libraries()}

@app.delete("/api/prompt-libraries/items/{item_id}")
def prompt_libraries_delete_item(item_id: str):
    data = load_prompt_libraries()
    removed = False
    for library in data["libraries"]:
        before = len(library.get("items", []))
        library["items"] = [item for item in library.get("items", []) if item.get("id") != item_id]
        if len(library["items"]) != before:
            library["updated_at"] = now_ms()
            removed = True
            break
    if not removed:
        raise HTTPException(status_code=404, detail="提示词不存在")
    save_prompt_libraries(data)
    return {"ok": True, **public_prompt_libraries()}

@app.post("/api/prompt-libraries/items/delete")
def prompt_libraries_batch_delete_items(req: PromptLibraryBatchDeleteRequest):
    ids = set(req.ids or [])
    if not ids:
        return {"ok": True, "deleted": 0, **public_prompt_libraries()}
    data = load_prompt_libraries()
    deleted = 0
    for library in data["libraries"]:
        before = len(library.get("items", []))
        library["items"] = [item for item in library.get("items", []) if item.get("id") not in ids]
        deleted += before - len(library["items"])
        if before != len(library["items"]):
            library["updated_at"] = now_ms()
    save_prompt_libraries(data)
    return {"ok": True, "deleted": deleted, **public_prompt_libraries()}

@app.post("/api/prompt-libraries/categories")
def prompt_libraries_create_category(req: PromptLibraryCategoryRequest):
    data = load_prompt_libraries()
    library = find_prompt_library(data, req.library_id or "system")
    if not library:
        raise HTTPException(status_code=404, detail="提示词库不存在")
    name = sanitize_prompt_text(req.name, "新分组")[:80]
    category = {"id": f"pcat_{uuid.uuid4().hex[:8]}", "name": name}
    library.setdefault("categories", []).append(category)
    library["updated_at"] = now_ms()
    save_prompt_libraries(data)
    return {"category": category, **public_prompt_libraries()}

@app.patch("/api/prompt-libraries/categories/{category_id}")
def prompt_libraries_update_category(category_id: str, req: PromptLibraryCategoryRequest):
    data = load_prompt_libraries()
    library = find_prompt_library(data, req.library_id or "system")
    if not library:
        raise HTTPException(status_code=404, detail="提示词库不存在")
    category = next((cat for cat in library.get("categories", []) if cat.get("id") == category_id), None)
    if not category:
        raise HTTPException(status_code=404, detail="分组不存在")
    category["name"] = sanitize_prompt_text(req.name, category.get("name") or "分组")[:80]
    library["updated_at"] = now_ms()
    save_prompt_libraries(data)
    return {"category": category, **public_prompt_libraries()}

@app.delete("/api/prompt-libraries/categories/{category_id}")
def prompt_libraries_delete_category(category_id: str, library_id: str = "system"):
    if category_id in PROMPT_BUILTIN_CATEGORY_IDS:
        raise HTTPException(status_code=400, detail="默认分组不能删除")
    data = load_prompt_libraries()
    library = find_prompt_library(data, library_id or "system")
    if not library:
        raise HTTPException(status_code=404, detail="提示词库不存在")
    before = len(library.get("categories", []))
    library["categories"] = [cat for cat in library.get("categories", []) if cat.get("id") != category_id]
    if len(library["categories"]) == before:
        raise HTTPException(status_code=404, detail="分组不存在")
    for item in library.get("items", []):
        if item.get("category") == category_id:
            item["category"] = "custom"
            item["updated_at"] = now_ms()
    library["updated_at"] = now_ms()
    save_prompt_libraries(data)
    return {"ok": True, **public_prompt_libraries()}

@app.get("/api/library/file/{source_id}/{path:path}")
def library_serve_file(source_id: str, path: str):
    sources = load_library_sources()
    src = next((s for s in sources if s["id"] == source_id), None)
    if not src:
        raise HTTPException(status_code=404, detail="来源不存在")
    folder = src.get("path", "")
    if not folder:
        raise HTTPException(status_code=400, detail="无本地路径")
    full = os.path.abspath(os.path.join(folder, path))
    folder_abs = os.path.abspath(folder)
    if os.path.commonpath([folder_abs, full]) != folder_abs:
        raise HTTPException(status_code=403, detail="路径越界")
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(full, media_type=content_type_for_path(full))

@app.get("/api/library/thumb/{source_id}/{filename}")
def library_serve_thumb(source_id: str, filename: str):
    thumb_dir = os.path.join(LIBRARY_DIR, source_id, "thumbs")
    full = os.path.join(thumb_dir, filename)
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="缩略图不存在")
    return FileResponse(full, media_type="image/jpeg")

@app.post("/api/library/tag")
async def library_ai_tag(req: LibraryTagRequest):
    images = load_library_images()
    id_set = set(req.image_ids)
    targets = [img for img in images if img["id"] in id_set]
    if not targets:
        raise HTTPException(status_code=400, detail="未找到指定图片")
    chat_base, chat_hdrs, resolved_model = resolve_chat_provider(req.provider, req.model, "")
    model = req.model or resolved_model
    # 构建标签词汇表字符串
    vocab_lines = []
    for group, words in PRESET_TAG_VOCAB.items():
        vocab_lines.append(f"  {group}：{' | '.join(words)}")
    vocab_text = "\n".join(vocab_lines)
    prompt_text = (
        "请分析这张建筑/设计类图片，严格按以下 JSON 格式返回。\n"
        "【重要规则】\n"
        "1. category、building_type、space、viewpoint、content_type 这5个字段的值只能从各自给定的选项中选择，禁止使用选项之外的任何词语。\n"
        "2. tags 字段应优先从下方「标签词汇库」中选择，如果词汇库中没有能准确描述的词才允许自由补充。\n\n"
        "{\n"
        '  "category": "只能从以下选一个：住宅 | 商业 | 办公 | 文化建筑 | 酒店 | 教育 | 医疗 | 综合体 | 塔楼 | 裙房 | 展示区 | 大堂/门厅 | 样板间 | 会所 | 景观 | 广场/节点 | 水景 | 鸟瞰 | 人视 | 夜景 | 施工过程",\n'
        '  "tags": ["标签1", "标签2", "标签3", "标签4", "标签5"],\n'
        '  "building_type": "只能选：住宅 | 商业 | 办公 | 文化建筑 | 酒店 | 教育 | 医疗 | 综合体 | 其他",\n'
        '  "space": "只能选：塔楼 | 裙房 | 展示区 | 大堂 | 样板间 | 会所 | 景观 | 广场 | 水景 | 其他",\n'
        '  "viewpoint": "只能选：鸟瞰 | 人视 | 半鸟瞰 | 夜景 | 日景 | 其他",\n'
        '  "content_type": "只能选：效果图 | 实景照片 | 施工照片 | 模型照片 | 平面图 | 剖面图 | 其他"\n'
        "}\n\n"
        "【标签词汇库】优先从以下词汇中选择 tags：\n"
        f"{vocab_text}\n\n"
        "tags 选 5-8 个，优先用词汇库中的词，不够再自由补充。只返回 JSON，不要其他文字。"
    )
    results = []
    for img in targets:
        local_path = img.get("local_path", "")
        if not local_path or not os.path.isfile(local_path):
            results.append({"id": img["id"], "ok": False, "error": "本地文件不存在"})
            continue
        try:
            with Image.open(local_path) as pil_img:
                pil_img.load()
                w, h = pil_img.size
                if max(w, h) > 1024:
                    pil_img.thumbnail((1024, 1024), Image.LANCZOS)
                if pil_img.mode not in ("RGB", "RGBA"):
                    pil_img = pil_img.convert("RGB")
                buf = BytesIO()
                fmt = "PNG" if pil_img.mode == "RGBA" else "JPEG"
                pil_img.save(buf, format=fmt, quality=85 if fmt == "JPEG" else None)
                encoded = base64.b64encode(buf.getvalue()).decode("ascii")
                mime = "image/png" if fmt == "PNG" else "image/jpeg"
                data_url = f"data:{mime};base64,{encoded}"
        except Exception as e:
            results.append({"id": img["id"], "ok": False, "error": str(e)})
            continue
        messages = [
            {"role": "system", "content": "你是图片分析助手。只返回 JSON。"},
            {"role": "user", "content": [
                {"type": "text", "text": prompt_text},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ]
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{chat_base}/chat/completions",
                    headers=chat_hdrs,
                    json={"model": model, "messages": messages},
                )
                resp.raise_for_status()
                raw = resp.json()
            content = text_from_chat_response(raw).strip()
            # 尝试匹配 JSON 对象（支持嵌套）
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                parsed = json.loads(json_match.group())
                cat = parsed.get("category", "")
                # 校验：主分类必须在预设列表中，否则强制匹配
                cat = _snap_to_preset(cat, PRESET_CATEGORIES)
                tags = parsed.get("tags", [])
                if isinstance(tags, list):
                    tags = [str(t).strip() for t in tags if t][:10]
                else:
                    tags = []
                # 校验 tags：优先匹配预设词汇库，匹配不上的保留原值
                all_vocab = []
                for words in PRESET_TAG_VOCAB.values():
                    all_vocab.extend(words)
                validated_tags = []
                for t in tags:
                    snapped = _snap_to_preset(t, all_vocab)
                    validated_tags.append(snapped)
                tags = list(dict.fromkeys(validated_tags))  # 去重保序
                # 校验各维度字段，只保留允许的值
                dim_map = {
                    "building_type": ["住宅","商业","办公","文化建筑","酒店","教育","医疗","综合体"],
                    "space": ["塔楼","裙房","展示区","大堂","样板间","会所","景观","广场","水景"],
                    "viewpoint": ["鸟瞰","人视","半鸟瞰","夜景","日景"],
                    "content_type": ["效果图","实景照片","施工照片","模型照片","平面图","剖面图"],
                }
                dims = []
                for key, allowed in dim_map.items():
                    val = str(parsed.get(key, "")).strip()
                    snapped = _snap_to_preset(val, allowed + ["其他"])
                    if snapped and snapped != "其他":
                        dims.append(snapped)
                all_ai_tags = list(dict.fromkeys(dims + tags))  # 去重保序
                img["categories"] = [cat]
                img["ai_tags"] = all_ai_tags
                img["tags"] = list(set(all_ai_tags + (img.get("manual_tags") or [])))
                img["ai_tagged"] = True
                img["ai_tag_model"] = model
                img["updated_at"] = now_ms()
                results.append({"id": img["id"], "ok": True, "category": cat, "tags": all_ai_tags})
            else:
                results.append({"id": img["id"], "ok": False, "error": "AI 返回格式异常"})
        except Exception as e:
            results.append({"id": img["id"], "ok": False, "error": str(e)})
    save_library_images(images)
    return {"results": results}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=3000)
