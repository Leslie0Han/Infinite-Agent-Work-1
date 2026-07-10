import importlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


def check_smart_canvas_script():
    html = (ROOT / "static" / "smart-canvas.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", html)
    assert scripts, "smart-canvas.html should contain an inline application script"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".js", delete=False) as tmp:
        tmp.write(scripts[-1])
        tmp_path = tmp.name
    try:
        subprocess.run(["node", "--check", tmp_path], check=True, cwd=ROOT)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    assert "materialCatalogueBtn" in html, "smart canvas should expose a material catalogue entry"
    assert "material-catalogue-modal" in html, "material catalogue should use a non-blocking canvas side panel"
    assert ".material-catalogue-modal { align-items:flex-start; justify-content:flex-start; background:transparent;" in html, "material catalogue panel should stay docked over the canvas"
    assert "materialCatalogueLayout:null" in html and "beginMaterialCatalogueLayoutDrag" in html and "settings.materialCatalogueLayout" in html, "material catalogue should be draggable/resizable and persist its layout"
    assert 'data-material-resize="n"' in html and 'data-material-resize="e"' in html and 'data-material-resize="s"' in html and 'data-material-resize="w"' in html, "material catalogue should expose resize handles on all four edges"
    assert ".material-catalogue-panel::after" in html and '<div class="library-picker-head" title="按住标题栏可拖动，拖边缘或右下角可缩放">' in html, "material catalogue drag/resize affordances should be visible and discoverable"
    assert 'title="拖动缩放材质库"' in html, "material catalogue resize handles should disclose their interaction"
    assert "/api/archlib/materials" in html, "material catalogue should merge ArchLib materials"
    assert "async function runImageNodeAction" in html, "context toolbar image actions should run as one-click actions"
    assert "async function runVideoNodeAction" in html, "context toolbar video action should run as a one-click action"
    assert "['ask-agent', 'bot', '问 Agent'" in html and "围绕当前选中的画布对象" in html, "context toolbar should let users ask the Agent about the selected object"
    assert "['render', 'wand-sparkles', '渲染设计'" in html and "['populate', 'users', '添加人物'" in html, "context toolbar actions should be localized for demo-ready canvas workflows"
    assert "quickActionDock" in html and "renderQuickActionDock" in html and "data-quick-action" in html, "smart canvas should expose a persistent bottom quick action dock"
    assert "AI Populate" in html and "Video" in html and "选择一张图后执行动作" in html, "quick action dock should mirror Gendo-style Render/Swap/Populate/Style/Video actions"
    assert "handleContextAction(btn.dataset.quickAction" in html, "quick action dock should reuse the same action routing as node toolbar actions"
    assert "renderObjectContextInspector" in html and "object-context-card" in html, "selected canvas objects should expose a compact context/lineage card"
    assert "contextInputNodesFor" in html and "contextOutputNodesFor" in html, "object context should summarize both input references and downstream versions"
    assert "contextVersionChainFor" in html and "primaryVersionInputFor" in html and "版本链" in html, "object context should expose a clickable upstream version chain"
    assert "versionHistoryRail" in html and "Project History" in html and "projectHistoryNodes" in html, "smart canvas should expose a Gendo-style project history rail"
    assert ".filter(node => !isMaterialNode(node) && !isSelectionNode(node))" in html, "project history should contain design versions, not material or selection references"
    assert "data-history-node" in html and "focusCanvasNode(btn.dataset.historyNode" in html, "project history rail should jump back to canvas versions"
    assert "data-history-new" in html and "新建画布节点" in html, "project history rail should support creating a new canvas node"
    assert "object-context-chip version" in html and "contextVersionLabel" in html, "version chain chips should show readable operation labels"
    assert "data-context-node" in html and "输入来源" in html, "object context input chips should be clickable and visible in Chinese"
    assert "后续版本" in html and "object-context-chip output" in html, "object context should expose clickable downstream version chips"
    assert "materialTargetSurface" in html, "material catalogue should expose a replacement target selector"
    assert "MATERIAL_TARGETS" in html, "material replacement should have explicit target surface options"
    assert "materialTargetSpec" in html, "material replacement prompt should use a target surface helper"
    assert "fromNode || current || 'auto'" in html, "material node target should take precedence over global target state"
    assert "scan-search" in html, "material nodes should display a target surface badge when set"
    assert "连接到当前选中目标并替换：" in html, "material swap buttons should disclose the selected target surface"
    assert "未指定的区域必须保持原材质和原设计" in html, "swap prompt should preserve non-target materials"
    assert "Swap Material: ${targetSurface.label}" in html, "swap result label should include the replacement target"
    assert "smartAgentPanel" in html, "smart canvas should include an in-canvas Agent panel"
    assert "SMART_AGENT_LOG_TYPE = 'smart-agent'" in html, "Agent records should use smart-agent canvas logs"
    assert "smartAgentHistory" in html and "smartAgentRunHistoryNodes" in html and "data-agent-history-node" in html, "Agent panel should expose recent action history with jump targets"
    assert "function focusCanvasNode" in html and "点击定位结果" in html, "recent actions and lineage chips should focus canvas result nodes"
    assert "fetch('/api/canvas-llm'" in html, "Agent panel should call the existing canvas LLM endpoint"
    assert "canvasAgentInventory" in html and "recentHistory:history" in html and "supportingReferences:supporting" in html, "Agent should send a whole-canvas inventory, not only the selected node"
    assert "selectedInputs" in html and "selectedOutputs" in html and "canvasInventory" in html, "Agent context should include selected node lineage and project history"
    assert "请先综合 canvasInventory、selectedInputs、selectedOutputs 和附件" in html, "Agent prompt should instruct the model to reason over the whole canvas"
    assert "function chatProviders()" in html, "Agent should resolve a chat-capable provider instead of reusing image provider settings"
    assert "smartAgentApiConfig" in html and "Agent API" in html, "Agent panel should expose visible API/provider configuration"
    assert "data-smart-agent-provider" in html and "data-smart-agent-model" in html, "Agent API config should let users choose provider and model"
    assert "本次调用：" in html and "Key 已配置" in html, "Agent API config should disclose the actual provider/model and key status"
    assert "grid-template-columns:auto minmax(88px" in html and ".smart-agent-api-field label { display:none; }" in html, "Agent API config should render as a compact one-line row"
    assert "smartAgentLayout:null" in html and "beginSmartAgentLayoutDrag" in html and "settings.smartAgentLayout" in html, "Agent panel layout should be draggable and persist on the canvas"
    assert 'data-agent-resize="n"' in html and 'data-agent-resize="e"' in html and 'data-agent-resize="s"' in html and 'data-agent-resize="w"' in html, "Agent panel should expose resize handles on all four edges"
    assert "smartAgentSuggestionsToggleBtn" in html and "smartAgentSuggestionsCollapsed" in html, "Agent suggested actions should be collapsible"
    assert "建议动作" in html and "替换材质" in html and "生成视频" in html, "Agent suggested actions should be displayed in Chinese"
    assert "contextualSmartAgentSuggestions" in html and "dedupeSmartAgentSuggestions" in html, "Agent suggestions should adapt to the selected canvas object"
    assert "hasMaterialInput ? '替换材质' : '打开材质库'" in html and "isSelectionNode(node)" in html and "继续版本链" in html, "Agent suggestions should prioritize material, selection, and version-chain workflows"
    assert "retryResultNode" in html and "重试 / 再生成" in html and "['retry', 'rotate-cw', '重试'" in html, "operation result nodes should expose retry/regenerate actions"
    assert "retryReferenceImagesForResult" in html and "selection_reference" in html and "material_reference" in html, "retry should rebuild semantic image references for material and selection workflows"
    assert "regenerate:'retry'" in html and "rerun:'retry'" in html, "Agent action aliases should normalize regenerate/rerun into retry"
    assert "retryOfNodeId:node.id" in html and "retryOfNodeId" in html, "retry outputs should preserve the source result node id"
    assert "function smartAgentDisplayLabel" in html and "'Swap Material':'替换材质'" in html and "'Populate':'添加人物'" in html, "Agent suggested action labels should be localized even when saved logs contain English labels"
    assert "escapeHtml(smartAgentDisplayLabel(item))" in html, "Agent suggestions should render through the localization helper"
    assert "resolveSmartAgentChatTarget" in html and "provider:chatTarget.provider" in html, "Agent requests should use the resolved chat provider/model"
    assert "smartAgentSupportsVision" in html and "images:chatTarget.images" in html, "Agent should only send image payloads to vision-capable chat models"
    assert "画布 LLM 调用失败：" in html, "Agent failures should disclose the actual upstream/config reason"
    assert "pushSmartAgentLog" in html, "Agent messages should be written back into canvas.logs"
    assert "token.dataset.role" in html and "nodeId:att.nodeId" in html, "attachment tokens should preserve node/image/url/role metadata"
    assert "smartAgentPinnedAttachments:[]" in html and "pinSelectedAttachmentToAgent" in html, "users should be able to explicitly pin canvas objects as persistent Agent attachments"
    assert "data-context-action=\"pin-agent\"" in html and "固定给 Agent" in html, "object context should expose a pin-to-Agent attachment action"
    assert "data-agent-unpin" in html and "unpinSmartAgentAttachment" in html, "pinned Agent attachments should be removable from the Agent panel"
    assert "mentionSelectedObjectInPrompt" in html and "data-context-action=\"mention-prompt\"" in html and "引用到 Prompt" in html, "selected canvas objects should be insertable as prompt mention tokens"
    assert "selectedMentionImage" in html and "insertMentionToken(mention)" in html, "object prompt references should reuse mention tokens with node/image/role metadata"
    assert "createSelectionReferenceNode" in html, "local-edit workflow should expose selection reference creation"
    selection_entry = re.search(r"function createSelectionReferenceNode[\s\S]*?function closeActionTemplatePanel", html)
    assert selection_entry and "openImageEditor(target.id, imageIndex)" in selection_entry.group(0) and "setImageEditMode('crop', true)" in selection_entry.group(0), "selection action should require an explicit user crop instead of silently using a fixed center region"
    assert "imageEditSelectionBtn" in html and "createSelectionReferenceFromEditor" in html, "image editor should create selection references from a user-defined crop box"
    assert "normalizedCropSelection" in html and "_selection.png" in html, "selection references should persist normalized crop regions and a selection thumbnail"
    assert "asset_kind:'selection'" in html, "selection nodes should use the normalized asset_kind field"
    assert "const selection = normalizedCropSelection()" in html, "selection nodes should persist the user-defined normalized region"
    assert "operation:'local-edit'" in html, "local edit should be traceable as an operation"
    assert "请先选择材质目标面，或创建局部 selection" in html, "ambiguous material swaps should ask for a target surface or selection"
    assert "openMaterialTargetPromptPanel" in html and "选择材质替换目标面" in html and "data-material-target-run" in html, "ambiguous material swaps should show an inline target-surface chooser"
    assert "data-material-target-selection" in html and "runSwapMaterial(targetNode, material)" in html, "target-surface chooser should continue swap or create a local selection"
    assert "handleMaterialDroppedOnTarget" in html and "已把材质“" in html, "dropping a material onto a target should start the swap guidance loop"
    assert "handleMaterialDroppedOnTarget(draggedNode, target)" in html, "material drag/drop should invoke target-surface guidance after connecting inputs"
    assert "applyMaterialTargetToCurrentNodes" in html, "material target selector should update current material links"
    assert "selected_library_import_items" in html and "items:importItems" in html, "library save should send material swap metadata items"
    assert "applyLibraryImportResultToNode" in html and "library_imported_ids" in html, "library save should mark generated result nodes as imported"
    assert "material_node_id" in html and "selection_node_id" in html and "material_target_label" in html, "swap results should preserve material target provenance"
    assert "POPULATE_TEMPLATES" in html and "Scene Population" in html and "人物数量/位置/服装关键词" in html, "Populate should expose lightweight parameters"
    assert "POPULATE_PRESET_PARAMS" in html and 'data-populate-param="density"' in html and 'data-populate-param="placement"' in html, "Populate should expose editable density and placement controls"
    assert 'data-populate-param="ageGroups"' in html and 'data-populate-param="identity"' in html and 'data-populate-param="ethnicity"' in html, "Populate should expose age, identity, and population controls"
    assert 'data-populate-param="outfit"' in html and 'data-populate-param="extra"' in html and "populateParamsPrompt" in html, "Populate should persist outfit keywords and adversarial constraints in the generation prompt"
    assert "actionPreflight.actionParams" in html, "custom action parameters should be preserved in version provenance"
    assert "STYLE_TEMPLATES" in html and "时间、季节、氛围" in html, "Style should expose time/season/mood parameters"
    assert "VIDEO_TEMPLATES" in html and "Walk forward" in html and "Timelapse" in html, "Video should expose template prompts"
    assert "actionPreflightSummaryHtml" in html and "动作预检" in html and "调用配置" in html, "template actions should show a preflight summary before execution"
    assert "function closeActionTemplatePanel(event=null)" in html and "event?.stopPropagation?.()" in html, "closing an action panel should not clear the selected canvas object through click bubbling"
    assert "actionPreflightSnapshot" in html and "actionPreflight:actionPreflightSnapshot" in html, "action results should persist a preflight snapshot in runSettings"
    assert "materialNodeId:materialNode.id" in html and "materialTargetLabel:targetSurface.label" in html, "swap material preflight should persist material and target surface metadata"
    assert "pushSmartActionResultLog" in html and "新结果节点已加入画布" in html, "completed actions should write a smart-agent result log"
    assert "smartActionFollowupSuggestions" in html and "pushSmartActionResultLog(pendingNode, label, 'swap-material')" in html, "completed action logs should include follow-up suggestions"
    assert "pushSmartActionResultLog(pendingNode, spec.label, spec.operation)" in html and "pushSmartActionResultLog(pendingNode, label, 'video')" in html, "image and video actions should log completion back to the Agent"
    assert "pushSmartActionFailureLog" in html and "未完成：" in html and "按建议动作修复后继续" in html, "failed actions should write recoverable smart-agent logs"
    assert "缺少材质参考" in html and "缺少可生成视频的目标图" in html and "Run generation" in html, "common failure paths should explain missing inputs or API failures"
    assert "normalizeSmartAgentAction" in html and "'swap-material':'swap'" in html and "'generate-style':'style'" in html, "Agent suggested actions should normalize operation-name aliases"
    assert "action === 'ask-agent'" in html and "action === 'catalogue'" in html and "action === 'pin-agent'" in html, "Agent suggestion cards should route to ask Agent, material catalogue, and pin attachment actions"
    assert "{label:'问 Agent', action:'ask-agent'" in html and "{label:'材质库', action:'catalogue'" in html and "{label:'固定附件', action:'pin-agent'" in html, "recoverable suggestions should use precise action ids"
    assert "action === 'mention-prompt'" in html and "'mention-prompt':'at-sign'" in html and "Mention prompt" in html, "Agent suggestion cards should route object references into the prompt"
    video_body = re.search(r"async function runVideoNodeAction[\s\S]*?async function runSwapMaterial", html)
    assert video_body, "runVideoNodeAction body should be present"
    assert "settings.apiKind = 'video'" in video_body.group(0), "video action should switch to video mode before running"
    assert "renderDynamicParams();" in video_body.group(0), "video action should initialize default video provider/model"


def check_archlib_material_chain():
    sys.path.insert(0, str(ROOT))
    app_main = importlib.import_module("main")
    if not os.path.isdir(app_main.ARCHLIB_CASE_DIR):
        print("ArchLib case directory not found; skipped ArchLib material check.")
        return
    items = app_main.build_archlib_material_index(force=True)
    assert items, "ArchLib material index should expose material candidates"
    sample = items[0]
    assert sample.get("material_label"), "ArchLib material item should have a material label"
    local_path = app_main.local_asset_path_from_url(sample["url"])
    assert local_path and os.path.isfile(local_path), "ArchLib material URL should resolve to a local image"
    data_url = app_main.reference_to_data_url({"url": sample["url"]}, max_size=32)
    assert data_url.startswith("data:image/"), "ArchLib material image should convert to a data URL for generation"
    client = TestClient(app_main.app)
    list_res = client.get("/api/archlib/materials", params={"page_size": 1})
    assert list_res.status_code == 200, list_res.text
    api_items = list_res.json().get("materials") or []
    assert api_items, "ArchLib material API should return at least one material"
    file_res = client.get(api_items[0]["url"])
    assert file_res.status_code == 200, file_res.text
    assert file_res.headers.get("content-type", "").startswith("image/"), "ArchLib file API should serve an image"
    print(f"ArchLib materials: {len(items)}; sample: {sample['material_label']}")


def check_canvas_material_persistence():
    sys.path.insert(0, str(ROOT))
    app_main = importlib.import_module("main")
    client = TestClient(app_main.app)
    created = client.post("/api/canvases", json={
        "title": "__codex_material_persistence__",
        "icon": "sparkles",
        "kind": "smart",
    })
    assert created.status_code == 200, created.text
    canvas = created.json()["canvas"]
    canvas_id = canvas["id"]
    try:
        material_url = "/api/archlib/file/material-persistence-sample.jpg"
        if os.path.isdir(app_main.ARCHLIB_CASE_DIR):
            items = app_main.build_archlib_material_index()
            if items:
                material_url = items[0]["url"]
        target_node = {
            "id": "target-node",
            "type": "image",
            "x": 120,
            "y": 160,
            "title": "目标空间",
            "images": [{"url": "/output/target-space.png", "name": "target-space.png"}],
        }
        material_node = {
            "id": "material-node",
            "type": "image",
            "x": 460,
            "y": 160,
            "title": "石材",
            "asset_kind": "material",
            "material_name": "石材",
            "material_family": "stone",
            "material_target": "roof",
            "images": [{
                "url": material_url,
                "name": "stone.jpg",
                "material_label": "石材",
                "material_target": "roof",
                "source_name": "ArchLib 案例库",
            }],
        }
        selection_node = {
            "id": "selection-node",
            "type": "smart-image",
            "x": 620,
            "y": 240,
            "title": "Selection reference",
            "asset_kind": "selection",
            "sourceNodeId": "target-node",
            "operation": "local-edit",
            "operationLabel": "Selection reference",
            "inputNodeIds": ["target-node"],
            "selection": {"kind": "region", "x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5, "unit": "normalized"},
            "images": [{
                "url": "/output/target-space.png",
                "name": "Selection · target-space.png",
                "asset_kind": "selection",
                "sourceNodeId": "target-node",
                "selection": {"kind": "region", "x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5, "unit": "normalized"},
            }],
        }
        result_node = {
            "id": "result-node",
            "type": "image",
            "x": 800,
            "y": 160,
            "title": "Swap Material: 屋面 / 绿化屋顶 · 石材",
            "operation": "swap-material",
            "operationLabel": "Swap Material: 屋面 / 绿化屋顶 · 石材",
            "inputNodeIds": ["target-node", "material-node", "selection-node"],
            "retryOfNodeId": "previous-result-node",
            "materialTarget": "roof",
            "runPrompt": "只替换目标图中的屋面，未指定区域必须保持原材质和原设计。",
            "runSettings": {"engine": "api", "apiKind": "image", "actionPreflight": {"action": "swap-material"}},
            "images": [],
        }
        connections = [
            {"from": "target-node", "to": "result-node", "kind": "input"},
            {"from": "material-node", "to": "result-node", "kind": "input"},
            {"from": "selection-node", "to": "result-node", "kind": "input"},
        ]
        logs = [{
            "type": "smart-agent",
            "role": "assistant",
            "text": "请选择目标面或创建局部 selection。",
            "attachments": [{
                "nodeId": "target-node",
                "imageIndex": 0,
                "url": "/output/target-space.png",
                "role": "target_image",
                "name": "target-space.png",
            }],
            "action": "swap-material",
            "suggestions": [{"label": "Create selection", "action": "selection"}],
            "created_at": 1760000000000,
        }]
        saved = client.put(f"/api/canvases/{canvas_id}", json={
            "title": "材质替换持久化验证",
            "icon": "sparkles",
            "nodes": [target_node, material_node, selection_node, result_node],
            "connections": connections,
            "viewport": {"x": 10, "y": -20, "scale": 0.85},
            "logs": logs,
            "settings": {"engine": "api", "apiKind": "image"},
            "base_updated_at": canvas.get("updated_at", 0),
        })
        assert saved.status_code == 200, saved.text
        loaded = client.get(f"/api/canvases/{canvas_id}")
        assert loaded.status_code == 200, loaded.text
        loaded_canvas = loaded.json()["canvas"]
        nodes_by_id = {node["id"]: node for node in loaded_canvas.get("nodes", [])}
        assert nodes_by_id["material-node"]["asset_kind"] == "material"
        assert nodes_by_id["material-node"]["material_target"] == "roof"
        assert nodes_by_id["material-node"]["images"][0]["material_target"] == "roof"
        assert nodes_by_id["selection-node"]["asset_kind"] == "selection"
        assert nodes_by_id["selection-node"]["sourceNodeId"] == "target-node"
        assert nodes_by_id["selection-node"]["selection"]["unit"] == "normalized"
        assert nodes_by_id["selection-node"]["images"][0]["asset_kind"] == "selection"
        assert nodes_by_id["result-node"]["operation"] == "swap-material"
        assert nodes_by_id["result-node"]["operationLabel"].endswith("· 石材")
        assert nodes_by_id["result-node"]["inputNodeIds"] == ["target-node", "material-node", "selection-node"]
        assert nodes_by_id["result-node"]["retryOfNodeId"] == "previous-result-node"
        assert nodes_by_id["result-node"]["runSettings"]["actionPreflight"]["action"] == "swap-material"
        assert loaded_canvas.get("logs", [])[0]["type"] == "smart-agent"
        assert loaded_canvas.get("logs", [])[0]["attachments"][0]["role"] == "target_image"
        assert loaded_canvas.get("connections") == connections
    finally:
        try:
            os.unlink(app_main.canvas_path(canvas_id))
        except OSError:
            pass

    old_sources = app_main.load_library_sources()
    old_images = app_main.load_library_images()
    sample_name = "__codex_material_swap_result.png"
    sample_path = app_main.output_path_for(sample_name, "output")
    sample_url = app_main.output_url_for(sample_name, "output")
    source_name = "codex_material_import_test"
    source_id = "smart-codex_material_import_test"
    try:
        os.makedirs(os.path.dirname(sample_path), exist_ok=True)
        app_main.Image.new("RGB", (48, 32), (180, 180, 170)).save(sample_path)
        imported = client.post("/api/library/import", json={
            "urls": [sample_url],
            "items": [{
                "url": sample_url,
                "node_id": "result-node",
                "node_title": "Swap Material: 屋面 / 绿化屋顶 · 石材",
                "operation": "swap-material",
                "operation_label": "Swap Material: 屋面 / 绿化屋顶 · 石材",
                "source_node_id": "target-node",
                "target_node_id": "target-node",
                "input_node_ids": ["target-node", "material-node", "selection-node"],
                "material_name": "石材",
                "material_target": "roof",
                "material_target_label": "屋面 / 绿化屋顶",
                "material_node_id": "material-node",
                "selection_node_id": "selection-node",
                "selection": {"kind": "region", "x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5, "unit": "normalized"},
                "run_prompt": "只替换目标图中的屋面，未指定区域必须保持原材质和原设计。",
            }],
            "source_name": source_name,
            "canvas_id": canvas_id,
            "canvas_title": "材质替换持久化验证",
            "node_id": "result-node",
            "categories": ["材质替换"],
            "manual_tags": ["智能画布"],
        })
        assert imported.status_code == 200, imported.text
        imported_items = imported.json().get("imported") or []
        assert len(imported_items) == 1
        record = imported_items[0]
        assert record["source_operation"] == "swap-material"
        assert record["material_name"] == "石材"
        assert record["material_target"] == "roof"
        assert record["source_material_node_id"] == "material-node"
        assert record["source_selection_node_id"] == "selection-node"
        assert record["source_input_node_ids"] == ["target-node", "material-node", "selection-node"]
        assert record["source_selection"]["unit"] == "normalized"
        assert "材质替换" in record["categories"]
        assert "石材" in record["manual_tags"]
        assert "未指定区域必须保持原材质和原设计" in record["notes"]
    finally:
        app_main.save_library_sources(old_sources)
        app_main.save_library_images(old_images)
        try:
            os.unlink(sample_path)
        except OSError:
            pass
        try:
            import shutil
            shutil.rmtree(os.path.join(app_main.LIBRARY_DIR, source_id), ignore_errors=True)
        except OSError:
            pass

    legacy = client.post("/api/canvases", json={
        "title": "__codex_legacy_canvas__",
        "icon": "sparkles",
        "kind": "smart",
    })
    assert legacy.status_code == 200, legacy.text
    legacy_canvas = legacy.json()["canvas"]
    legacy_id = legacy_canvas["id"]
    try:
        saved_legacy = client.put(f"/api/canvases/{legacy_id}", json={
            "title": "旧画布兼容验证",
            "icon": "sparkles",
            "nodes": [{"id": "legacy-node", "type": "smart-image", "x": 0, "y": 0, "title": "Legacy", "images": []}],
            "connections": [],
            "viewport": {},
            "logs": [],
            "settings": {},
            "base_updated_at": legacy_canvas.get("updated_at", 0),
        })
        assert saved_legacy.status_code == 200, saved_legacy.text
        loaded_legacy = client.get(f"/api/canvases/{legacy_id}")
        assert loaded_legacy.status_code == 200, loaded_legacy.text
        legacy_node = loaded_legacy.json()["canvas"]["nodes"][0]
        assert "asset_kind" not in legacy_node
        assert loaded_legacy.json()["canvas"].get("logs") == []
    finally:
        try:
            os.unlink(app_main.canvas_path(legacy_id))
        except OSError:
            pass


if __name__ == "__main__":
    check_smart_canvas_script()
    check_archlib_material_chain()
    check_canvas_material_persistence()
    print("smart canvas material checks passed")
