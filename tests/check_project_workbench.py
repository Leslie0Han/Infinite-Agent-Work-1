import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def check_project_workbench_contract():
    page = (ROOT / "static" / "project-workbench.html").read_text(encoding="utf-8")
    assert "/api/projects/${encodeURIComponent(activeProjectId)}/workspace" in page
    assert "/api/assets/${encodeURIComponent(assetId)}/lineage" in page
    assert 'id="canvasGrid"' in page
    assert 'id="taskList"' in page
    assert 'id="assetGrid"' in page
    assert 'id="lineageDialog"' in page
    assert "counts.feedback_events" in page and "counts.adopted_assets" in page, "workbench should surface material feedback progress"
    assert "/api/assets/${encodeURIComponent(assetId)}/feedback" in page, "workbench should persist final-adoption feedback"
    assert "final_unadopted" in page and "final_adopted" in page, "final adoption should be a reversible project decision"
    assert "feedback.score" in page and "feedback.event_count" in page, "asset lineage should expose accumulated feedback quality"
    scripts = re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", page, re.IGNORECASE)
    assert scripts, "project workbench should include application script"


if __name__ == "__main__":
    check_project_workbench_contract()
    print("project workbench checks passed")
