import asyncio
import importlib
import os
import re
import subprocess
import sys
import tempfile
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image


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
    assert "CANVAS_ACTION_REGISTRY" in html and "'ask-agent':{label:'问 Agent'" in html, "context toolbar should source Agent actions from the shared action registry"
    assert "render:{label:'渲染设计'" in html and "populate:{label:'添加人物'" in html, "registered canvas actions should be localized for demo-ready workflows"
    assert "canvasActionIdsForNode" in html and "canvasActionSpec(id)" in html, "node toolbars should be generated from the shared action registry"
    assert "return handleContextAction(action, {templateKey, source:'agent'})" in html, "Agent and context-toolbar actions should share one execution route"
    assert "setCanvasTaskState('preflight'" in html and "setCanvasTaskState('running'" in html and "setCanvasTaskState('success'" in html and "setCanvasTaskState('error'" in html, "canvas tasks should expose the full execution state machine"
    assert "surface !== 'action' && surface !== 'material'" in html and "surface !== 'material' && surface !== 'agent'" in html, "material catalogue should coexist with the active action or Agent panel"
    assert '.quick-action-dock { position:absolute;' in html and 'data-dock-action="history"' in html and 'data-dock-action="upload"' in html, "the recording-style bottom dock should expose history, global creation, and zoom controls"
    assert "orderedIds = normalImage ? ['render','swap','edit','populate','style','video','enhance','download'" in html and "visibleActions = normalImage ? actions.slice(0, 8)" in html, "the selected-object toolbar should preserve the recording action order and disclose secondary layer commands"
    assert 'class="canvas-topbar"' in html and 'class="plan-badge">' not in html and 'data-upgrade-plan' not in html, "the quiet top bar should stay product-focused without plan or upgrade promotions"
    assert 'id="uploadImageBtn"' in html and "上传图片" in html, "smart canvas should expose a direct, discoverable image upload action"
    assert 'role="status" aria-live="polite"' in html and "setUploadStatus" in html, "upload progress and errors should be announced instead of silently disappearing"
    assert "UPLOAD_MAX_FILES" in html and "UPLOAD_MAX_BYTES" in html and "uploadResponseMessage" in html, "upload should validate limits and surface structured server errors"
    assert "canvasDropOverlay" in html and "松开即可上传图片" in html, "canvas drag/drop should visibly confirm the drop target"
    assert "handleFiles(e.dataTransfer.files, '', {dropPoint:p})" in html, "a failed canvas drop must not create an orphan empty node before validation succeeds"
    assert '<button class="node-drop"' in html and 'aria-label="上传图片到当前节点"' in html, "empty-node upload should be keyboard reachable and named"
    assert "uploadCreateNewNode = true" in html and "const targetId = uploadCreateNewNode ? ''" in html, "the global upload action should create a new node instead of silently modifying the current selection"
    assert "world.insertAdjacentHTML('beforeend', renderObjectContextInspector());" not in html, "selecting an image should not add a second text-heavy context card below the image toolbar"
    assert "contextInputNodesFor" in html and "contextOutputNodesFor" in html, "object context should summarize both input references and downstream versions"
    assert "contextVersionChainFor" in html and "primaryVersionInputFor" in html and "版本链" in html, "object context should expose a clickable upstream version chain"
    assert "versionHistoryRail" in html and "项目历史" in html and "generationHistoryEntries" in html, "smart canvas should expose a persistent project generation history rail"
    assert "recordGenerationHistory" in html and "canvas.generationHistory" in html and "generationHistory:canvas.generationHistory" in html, "project history must persist independently from the visible node graph"
    assert "data-history-entry" in html and "data-history-add" in html and "addHistoryEntryToCanvas" in html, "project history should preview, focus, and restore a result to the canvas"
    assert "const items = allItems;" in html and "data-upgrade-history" not in html and "解锁无限历史" not in html, "project history should expose saved results without plan or unlock promotions"
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
    assert "替换材质：${targetSurface.label}" in html, "swap result label should include the replacement target"
    assert "smartAgentPanel" in html, "smart canvas should include an in-canvas Agent panel"
    assert "SMART_AGENT_LOG_TYPE = 'smart-agent'" in html, "Agent records should use smart-agent canvas logs"
    assert 'data-agent-conversation="current"' in html and 'data-agent-conversation="new"' in html, "a selected image toolbar should let users continue the active Agent conversation or start a new one"
    assert "composerExpanded = false" in html and "composerTargetId === node.id" in html, "selecting an image must keep the API composer collapsed until an action requests it"
    assert "输入渲染要求并确认参数" in html and "openActionTemplatePanel('render', node)" in html and "data-action-prompt-run" in html, "Render Design should reveal its dedicated preflight panel instead of running immediately"
    assert "openSmartAgentConversation" in html and "immediateSmartAgentAdvice" in html, "opening an Agent conversation from an image must reveal the panel and produce executable advice immediately"
    assert "conversationId:activeSmartAgentConversationId()" in html and "log.conversationId || 'main'" in html, "new Agent conversations must keep their message history separate while preserving legacy logs"
    assert "pointerOverSmartAgent" in html and "addDroppedImageToSmartAgent(attachment)" in html, "dropping a moved canvas image on the Agent panel must use the same attachment path as the toolbar"
    assert 'draggable="false"' in html and "agent-drop-ready" in html, "canvas images must preserve node movement while the Agent panel exposes a visible drop affordance"
    assert "[data-agent-draggable]" not in html, "Agent attachment support must not exclude image elements from the canvas node drag gesture"
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
    assert "smartAgentPrimaryAction" in html and "data-agent-primary-action" in html, "the Agent's top recommendation should expose a visible one-click action"
    assert "workflowNeedsChoice" in html and "workflowSuggestions" in html and "baseSuggestions.filter" in html, "guided material workflow choices must take priority over generic contextual suggestions"
    assert "decision?.primary_action" in html and "alternative_actions" in html and "needs_input" in html, "Agent UI should consume structured decisions with questions and executable actions"
    assert "button.dataset.agentPrimaryAction || button.dataset.agentAction" in html and "runSmartAgentSuggestion(" in html, "Agent primary and suggested actions must share a delegated real action router"
    assert "建议动作" in html and "替换材质" in html and "生成视频" in html, "Agent suggested actions should be displayed in Chinese"
    assert "contextualSmartAgentSuggestions" in html and "dedupeSmartAgentSuggestions" in html, "Agent suggestions should adapt to the selected canvas object"
    assert "hasMaterialInput ? '再次替换材质' : '替换材质'" in html and "isSelectionNode(node)" in html and "继续版本链" in html, "Agent suggestions should prioritize material, selection, and version-chain workflows"
    assert "retryResultNode" in html and "重试 / 再生成" in html and "retry:{label:'重试'" in html, "operation result nodes should expose the registered retry action"
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
    assert "POPULATE_TEMPLATES" in html and "场景人物" in html and "人物数量、位置和服装关键词" in html, "Populate should expose lightweight parameters"
    assert "POPULATE_PRESET_PARAMS" in html and 'data-populate-param="density"' in html and 'data-populate-param="placement"' in html, "Populate should expose editable density and placement controls"
    assert 'data-populate-param="ageGroups"' in html and 'data-populate-param="identity"' in html and 'data-populate-param="ethnicity"' in html, "Populate should expose age, identity, and population controls"
    assert 'data-populate-density' in html and all(value in html for value in ["lite:'极少", "packed:'密集"]), "Populate density should match the demonstrated five-level Lite-to-Packed control"
    assert 'data-populate-age="${value}"' in html and "['teenagers','young-adults','adults','seniors']" in html and "至少保留一个年龄组" in html, "Populate age groups should be independently selectable without allowing an invalid empty set"
    assert 'data-populate-param="outfit"' in html and 'data-populate-param="extra"' in html and "populateParamsPrompt" in html, "Populate should persist outfit keywords and adversarial constraints in the generation prompt"
    assert "actionPreflight.actionParams" in html, "custom action parameters should be preserved in version provenance"
    assert "STYLE_TEMPLATES" in html and all(label in html for label in ["Flat Shade", "Heavy Marker", "Loose Sketch", "Physical Model", "Pink Blue", "Post Digital", "Precise Sketch", "Urban Marker", "Vintage Japanese"]), "Style should expose the demonstrated nine visual-media presets"
    assert all(f"/static/style-previews/{name}.png" in html for name in ["flat-shade", "heavy-marker", "loose-sketch", "physical-model", "pink-blue", "post-digital", "precise-sketch", "urban-marker", "vintage-japanese"]), "style choices should use real visual assets captured from the reference recording"
    assert "candidateSession:true" in html and "renderCandidateSessionRail" in html and "candidateSessionForOwner" in html, "generated alternatives should stay in a hidden candidate session attached to the source"
    assert "owner.candidatePreview" in html and "data-candidate-preview" in html and "addCandidateToCanvas" in html, "single-click preview and explicit promotion should use separate state transitions"
    assert "dblclick" in html and "data-candidate-add" in html and "draggable=\"true\"" in html, "candidate promotion should work by double-click, plus, or drag"
    assert "return Boolean(node && node.candidateSession === true)" in html, "only explicit candidate sessions may be folded into a source rail"
    assert "function markPromotedCandidate" in html and "delete node.candidateSession" in html and "delete node.sessionOwnerId" in html, "promoted results must clear every candidate-session identity field"
    node_events = re.search(r"function bindNodeEvents\(\)[\s\S]*?function rectOverlapNode", html)
    assert node_events and "candidateStack" not in node_events.group(0), "clicking or double-clicking an independent canvas object must never route back through candidate behavior"
    assert "const candidateSource = isCandidateSession(source)" in html and "markPromotedCandidate(newNode, source, candidateOwner)" in html, "candidate drag promotion must use explicit session identity and produce an independent object"
    assert "n.id === draggedId || isCandidateSession(n)" in html, "hidden candidate sessions must never participate in visible-object overlap or merge detection"
    assert "moved:false" in html and "if(!dragState.moved && !dragState.thumbDetached)" in html, "a click without real pointer movement must not run drag-drop merge logic"
    assert "pendingNode.generationJob = {status:'error'" in html and "pushSmartActionFailureLog" in html, "failed generations should preserve a recoverable candidate session"
    assert "openSwapMaterialPanel" in html and "data-swap-select-area" in html and "data-swap-material" in html and "data-swap-description" in html, "Swap Material should expose surface, material reference, and description inputs"
    assert "materialCatalogueState.detailId" in html and "用作材质参考" in html and "申请免费样品" in html, "material cards should open the demonstrated detail actions"
    assert "action === 'download'" in html and "downloadSelectedCanvasImage(node)" in html and "openDownloadRightsPanel" not in html, "download should export directly without commercial or upgrade prompts"
    assert "data-enhance-run" in html and "一键增强" in html, "Enhance should remain a deliberate one-click action without an unnecessary prompt"
    populate_run = re.search(r"querySelector\('\[data-populate-run\]'\)[\s\S]*?querySelectorAll\('\[data-style-select\]'\)", html)
    assert populate_run and populate_run.group(0).count("runImageNodeAction('populate'") == 1, "Populate must start exactly one generation per click"
    assert "VIDEO_TEMPLATES" in html and "Walk forward" in html and "Timelapse" in html, "Video should expose template prompts"
    assert "Video is available on the Studio plan" not in html and "settings.plan" not in html, "video creation should not be blocked by a plan gate"
    assert "Gendo 助手" not in html and "<span>智能助手</span>" in html, "the assistant surface should use a product-generic name"
    assert "Camera move" in html and "Cinematic edit" in html and "Custom transition" in html, "Video should cover the controlled and creative camera choices demonstrated in the recording"
    assert 'id="gendoOnboarding"' in html and "ONBOARDING_PROFILE_KEY" in html and all(f'data-onboarding-step="{step}"' in html for step in range(1, 5)), "first-time users should receive the demonstrated four-step role, company, discovery, and workspace onboarding"
    assert "workspace_profile:workspaceProfile()" in html, "onboarding answers should affect Agent context instead of being decorative survey data"
    assert 'id="customCutoutBtn"' in html and "openCustomCutoutPanel" in html and "runCustomCutoutGeneration" in html, "the canvas should expose a real Custom Cutout workflow"
    assert "customCutoutPrompt" in html and "透明背景或纯净可抠图背景" in html and "assetKind:'cutout'" in html, "Custom Cutout should generate a reusable full-body reference node rather than a generic image"
    assert 'id="addStickyNoteBtn"' in html and "createStickyNote" in html and "sticky-note-text" in html, "the demonstrated sticky-note canvas tool should persist editable notes"
    assert "startAgentCanvasSelection" in html and "toggleAgentCanvasNodeSelection" in html and 'data-agent-select-canvas="1"' in html, "Agent attachments should support the demonstrated visible multi-select-on-canvas workflow"
    assert "actionPreflightSummaryHtml" in html and "动作预检" in html and "调用配置" in html, "template actions should show a preflight summary before execution"
    assert "function closeActionTemplatePanel(event=null)" in html and "event?.stopPropagation?.()" in html, "closing an action panel should not clear the selected canvas object through click bubbling"
    assert "actionPreflightSnapshot" in html and "actionPreflight:actionPreflightSnapshot" in html, "action results should persist a preflight snapshot in runSettings"
    assert "materialNodeId:materialNode.id" in html and "materialTargetLabel:targetSurface.label" in html, "swap material preflight should persist material and target surface metadata"
    assert "pushSmartActionResultLog" in html and "候选结果已附着在源图右侧" in html, "completed actions should write a smart-agent result log with explicit candidate-promotion guidance"
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


def check_canvas_agent_decision_contract():
    sys.path.insert(0, str(ROOT))
    app_main = importlib.import_module("main")
    decision = app_main.canvas_decision_from_text("当前最适合先执行 render-design 整体渲染深化。")
    assert decision["needs_input"] is False
    assert decision["primary_action"]["action"] == "render"
    assert decision["primary_action"]["params"] == {}
    question = app_main.canvas_decision_from_text('请明确材质目标后执行 swap-material')
    assert question["needs_input"] is True
    assert question["primary_action"] is None
    advice = app_main.canvas_decision_from_text("这张图的空间层次已经比较清楚，可以继续深化细节。")
    assert advice["primary_action"] is None, "advice-only replies must not become render jobs without explicit action intent"


def check_canvas_llm_local_images_are_inlined():
    sys.path.insert(0, str(ROOT))
    app_main = importlib.import_module("main")
    captured = {}

    class FakeResponse:
        content = b'{"choices":[{"message":{"content":"analysis only"}}]}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "analysis only"}}]}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, headers=None, json=None):
            captured["request"] = json
            return FakeResponse()

    local_urls = {
        "/api/library/file/source/material.png",
        "/api/archlib/file/case/material.png",
    }
    original_client = app_main.httpx.AsyncClient
    original_resolver = app_main.resolve_chat_provider
    original_local_resolver = app_main.local_asset_path_from_url
    with tempfile.NamedTemporaryFile(suffix=".png") as image_file:
        Image.new("RGB", (2, 2), (128, 96, 64)).save(image_file, format="PNG")
        image_file.flush()
        try:
            app_main.httpx.AsyncClient = FakeAsyncClient
            app_main.resolve_chat_provider = lambda *args: ("https://upstream.invalid", {}, "test-model")
            app_main.local_asset_path_from_url = lambda url: image_file.name if url in local_urls else None
            payload = app_main.CanvasLLMRequest(
                message="分析附件",
                provider="modelscope",
                images=[*local_urls, "/api/unknown/file/missing.png"],
            )
            asyncio.run(app_main.canvas_llm(payload))
        finally:
            app_main.httpx.AsyncClient = original_client
            app_main.resolve_chat_provider = original_resolver
            app_main.local_asset_path_from_url = original_local_resolver

    user_content = captured["request"]["messages"][-1]["content"]
    image_urls = [part["image_url"]["url"] for part in user_content if part.get("type") == "image_url"]
    assert len(image_urls) == 2
    assert all(url.startswith("data:image/") for url in image_urls), "upstream vision APIs cannot resolve local library or ArchLib relative URLs"


def check_builtin_material_pack():
    sys.path.insert(0, str(ROOT))
    app_main = importlib.import_module("main")
    material_dir = Path(app_main.BUILTIN_MATERIAL_DIR)
    files = sorted(material_dir.glob("*.jpg"))
    assert len(files) == 12, "built-in architectural material pack should contain 12 diffuse textures"
    assert set(app_main.BUILTIN_MATERIAL_METADATA) == {re.sub(r"_(?:diff|diffuse)_1k$", "", path.stem) for path in files}
    for path in files:
        with Image.open(path) as image:
            assert image.size == (1024, 1024), f"{path.name} should be a 1K texture"


def check_ai_reference_upload_validation():
    sys.path.insert(0, str(ROOT))
    app_main = importlib.import_module("main")
    client = TestClient(app_main.app)
    buffer = BytesIO()
    Image.new("RGB", (3, 2), (34, 139, 230)).save(buffer, format="PNG")
    valid_png = buffer.getvalue()
    created_paths = []
    try:
        mixed = client.post("/api/ai/upload", files=[
            ("files", ("clipboard-image", valid_png, "application/octet-stream")),
            ("files", ("broken.png", b"this is not an image", "image/png")),
        ])
        assert mixed.status_code == 200, mixed.text
        payload = mixed.json()
        assert len(payload.get("files") or []) == 1, "valid images should survive a mixed multi-file upload"
        assert len(payload.get("errors") or []) == 1, "invalid images should be reported by filename instead of silently ignored"
        uploaded = payload["files"][0]
        assert uploaded["width"] == 3 and uploaded["height"] == 2
        assert uploaded["content_type"] == "image/png"
        uploaded_path = app_main.output_path_for(os.path.basename(uploaded["url"]), "input")
        created_paths.append(uploaded_path)
        with Image.open(uploaded_path) as stored:
            assert stored.size == (3, 2), "stored uploads should remain decodable images"

        invalid = client.post("/api/ai/upload", files={
            "files": ("fake.heic", b"not-heic", "image/heic"),
        })
        assert invalid.status_code == 415, invalid.text
        detail = invalid.json().get("detail") or {}
        errors = detail.get("errors") or [{}]
        assert "HEIC/HEIF" in errors[0].get("message", ""), "unsupported HEIC should receive a recoverable conversion instruction"

        too_many = client.post("/api/ai/upload", files=[
            ("files", (f"{index}.png", valid_png, "image/png"))
            for index in range(app_main.AI_REFERENCE_MAX_FILES + 1)
        ])
        assert too_many.status_code == 413, too_many.text
    finally:
        for path in created_paths:
            try:
                os.unlink(path)
            except OSError:
                pass


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
        cutout_node = {
            "id": "cutout-node",
            "type": "smart-image",
            "x": 720,
            "y": 420,
            "title": "Custom Cutout",
            "asset_kind": "cutout",
            "operation": "custom-cutout",
            "cutoutParams": {"age": "adult", "ethnicity": "diverse", "identity": "female", "clothing": "dark coat"},
            "images": [{"url": "/output/person-cutout.png", "name": "person-cutout.png", "asset_kind": "cutout"}],
        }
        note_node = {
            "id": "note-node",
            "type": "sticky-note",
            "x": 80,
            "y": 460,
            "w": 220,
            "h": 156,
            "title": "便签",
            "text": "保持入口和主要立面不变",
            "color": "#fde68a",
            "images": [],
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
        generation_history = [{
            "id": "history-result-node-1760000000000",
            "nodeId": "result-node",
            "status": "success",
            "operation": "swap-material",
            "label": "Swap Material: 屋面 / 绿化屋顶 · 石材",
            "image": {"url": "/output/material-result.png", "name": "material-result.png"},
            "createdAt": 1760000000000,
        }]
        saved = client.put(f"/api/canvases/{canvas_id}", json={
            "title": "材质替换持久化验证",
            "icon": "sparkles",
            "nodes": [target_node, material_node, selection_node, cutout_node, note_node, result_node],
            "connections": connections,
            "viewport": {"x": 10, "y": -20, "scale": 0.85},
            "logs": logs,
            "generationHistory": generation_history,
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
        assert nodes_by_id["cutout-node"]["asset_kind"] == "cutout"
        assert nodes_by_id["cutout-node"]["cutoutParams"]["identity"] == "female"
        assert nodes_by_id["note-node"]["type"] == "sticky-note"
        assert nodes_by_id["note-node"]["text"] == "保持入口和主要立面不变"
        assert nodes_by_id["result-node"]["operation"] == "swap-material"
        assert nodes_by_id["result-node"]["operationLabel"].endswith("· 石材")
        assert nodes_by_id["result-node"]["inputNodeIds"] == ["target-node", "material-node", "selection-node"]
        assert nodes_by_id["result-node"]["retryOfNodeId"] == "previous-result-node"
        assert nodes_by_id["result-node"]["runSettings"]["actionPreflight"]["action"] == "swap-material"
        assert loaded_canvas.get("logs", [])[0]["type"] == "smart-agent"
        assert loaded_canvas.get("logs", [])[0]["attachments"][0]["role"] == "target_image"
        assert loaded_canvas.get("generationHistory") == generation_history
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
    check_canvas_agent_decision_contract()
    check_canvas_llm_local_images_are_inlined()
    check_builtin_material_pack()
    check_archlib_material_chain()
    check_ai_reference_upload_validation()
    check_canvas_material_persistence()
    print("smart canvas material checks passed")
