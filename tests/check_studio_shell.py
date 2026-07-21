from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def check_unified_shell_contract():
    shell_js = (STATIC / "studio-shell.js").read_text(encoding="utf-8")
    shell_css = (STATIC / "studio-shell.css").read_text(encoding="utf-8")

    assert "Ctrl+1" in shell_js
    assert "Ctrl+2" in shell_js
    assert "Ctrl+3" in shell_js
    assert "Ctrl+4" in shell_js
    assert "工时" not in shell_js
    assert "studio_active_project_id" in shell_js
    assert "studio_feedback_v1" in shell_js
    assert "studio-project-change" in shell_js
    assert "#studio-global-shell" in shell_css
    assert "studio-shell-module-canvas" in shell_css

    for filename in (
        "index.html",
        "library.html",
        "smart-canvas.html",
        "project-workbench.html",
    ):
        page = (STATIC / filename).read_text(encoding="utf-8")
        assert "/static/studio-shell.css" in page, f"{filename} should load the shared shell styles"
        assert "/static/studio-shell.js" in page, f"{filename} should load the shared shell behavior"

    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="agentDrawerContent"' in index
    assert 'id="workbenchDrawerContent"' in index
    assert 'id="agentConversationSearch"' in index
    assert 'id="workbenchToolList"' in index
    assert 'data-nav-page="workbench"' not in index
    assert 'data-nav-page="library"' not in index
    assert "drawer-context-full" in index
    assert "async function sendAgentHomeRequest" in index
    assert "currentAgentModeConfig().taskType !== 'design_task'" in index
    assert "await createAgentPlan()" in index
    assert "runtime: 'tool_calling_v1'" in index
    assert 'id="agentCapabilityStatus"' in index
    assert "async function loadAgentCapabilities" in index
    assert "'/api/agent/capabilities'" in index
    assert "MCP ·" in index and "Skill ·" in index
    assert "project_id: localStorage.getItem('studio_active_project_id')" in index
    assert "async function hydrateAgentHistory" in index
    assert "confirmation_token: currentAgentTask.confirmation_token" in index
    assert "function setActiveFrame" in index
    assert 'hidden inert aria-hidden="true"' in index

    project_workbench = (STATIC / "project-workbench.html").read_text(encoding="utf-8")
    smart_canvas = (STATIC / "smart-canvas.html").read_text(encoding="utf-8")
    assert "fit=1" in project_workbench
    assert "function fitCanvasToContent" in smart_canvas


if __name__ == "__main__":
    check_unified_shell_contract()
    print("studio shell checks passed")
