import hashlib
import json
import os
import sqlite3
import time
import uuid
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class DomainStore:
    """Durable project, asset, task, snapshot, and lineage storage."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    code TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    settings_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    kind TEXT NOT NULL DEFAULT 'image',
                    title TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    current_version_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS asset_versions (
                    id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL REFERENCES assets(id),
                    storage_url TEXT NOT NULL,
                    sha256 TEXT NOT NULL DEFAULT '',
                    mime_type TEXT NOT NULL DEFAULT '',
                    width INTEGER NOT NULL DEFAULT 0,
                    height INTEGER NOT NULL DEFAULT 0,
                    byte_size INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    UNIQUE(asset_id, storage_url)
                );
                CREATE INDEX IF NOT EXISTS idx_asset_versions_url ON asset_versions(storage_url);
                CREATE TABLE IF NOT EXISTS canvases (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'smart',
                    current_snapshot_id TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS canvas_snapshots (
                    id TEXT PRIMARY KEY,
                    canvas_id TEXT NOT NULL REFERENCES canvases(id),
                    version INTEGER NOT NULL,
                    scene_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(canvas_id, version)
                );
                CREATE TABLE IF NOT EXISTS generation_tasks (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    canvas_id TEXT,
                    source_node_id TEXT NOT NULL DEFAULT '',
                    provider_id TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    prompt TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    parameters_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    cost REAL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generation_inputs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES generation_tasks(id),
                    asset_id TEXT REFERENCES assets(id),
                    input_role TEXT NOT NULL DEFAULT 'reference',
                    source_url TEXT NOT NULL DEFAULT '',
                    region_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generation_outputs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES generation_tasks(id),
                    asset_id TEXT NOT NULL REFERENCES assets(id),
                    output_index INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'succeeded',
                    created_at INTEGER NOT NULL,
                    UNIQUE(task_id, output_index)
                );
                CREATE TABLE IF NOT EXISTS lineage_edges (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    from_asset_id TEXT NOT NULL REFERENCES assets(id),
                    to_asset_id TEXT NOT NULL REFERENCES assets(id),
                    generation_task_id TEXT REFERENCES generation_tasks(id),
                    relation_type TEXT NOT NULL DEFAULT 'generated_from',
                    created_at INTEGER NOT NULL,
                    UNIQUE(from_asset_id, to_asset_id, generation_task_id, relation_type)
                );
                CREATE TABLE IF NOT EXISTS preference_events (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    asset_id TEXT NOT NULL REFERENCES assets(id),
                    event_type TEXT NOT NULL,
                    context_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL
                );
                """
            )

    @staticmethod
    def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        return dict(row) if row else None

    def ensure_default_project(self) -> Dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM projects ORDER BY created_at LIMIT 1").fetchone()
            if row:
                return dict(row)
        return self.create_project("默认项目", "DEFAULT", project_id="project_default")

    def create_project(self, name: str, code: str = "", project_id: str = "") -> Dict[str, Any]:
        timestamp = now_ms()
        project_id = project_id or new_id("project")
        with self.connect() as db:
            db.execute(
                "INSERT INTO projects(id,name,code,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (project_id, name.strip()[:120] or "未命名项目", code.strip()[:40], "active", timestamp, timestamp),
            )
            return dict(db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())

    def list_projects(self) -> List[Dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM projects ORDER BY updated_at DESC")]

    def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as db:
            return self._row(db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())

    def upsert_canvas(self, canvas: Dict[str, Any], project_id: str = "") -> Dict[str, Any]:
        project_id = project_id or canvas.get("project_id") or self.ensure_default_project()["id"]
        timestamp = int(canvas.get("updated_at") or now_ms())
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO canvases(id,project_id,title,kind,created_at,updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    project_id=excluded.project_id,title=excluded.title,kind=excluded.kind,updated_at=excluded.updated_at
                """,
                (
                    canvas["id"], project_id, canvas.get("title") or "未命名画布",
                    canvas.get("kind") or "smart", int(canvas.get("created_at") or timestamp), timestamp,
                ),
            )
            return dict(db.execute("SELECT * FROM canvases WHERE id=?", (canvas["id"],)).fetchone())

    def get_canvas(self, canvas_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as db:
            return self._row(db.execute("SELECT * FROM canvases WHERE id=?", (canvas_id,)).fetchone())

    def list_canvas_snapshots(self, canvas_id: str, limit: int = 30) -> List[Dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                """SELECT id,canvas_id,version,created_at FROM canvas_snapshots
                   WHERE canvas_id=? ORDER BY version DESC LIMIT ?""",
                (canvas_id, max(1, min(200, limit))),
            )]

    def save_canvas_snapshot(self, canvas: Dict[str, Any], project_id: str = "") -> Dict[str, Any]:
        self.upsert_canvas(canvas, project_id)
        scene_json = json.dumps(canvas, ensure_ascii=False)
        with self.connect() as db:
            latest = db.execute(
                "SELECT * FROM canvas_snapshots WHERE canvas_id=? ORDER BY version DESC LIMIT 1",
                (canvas["id"],),
            ).fetchone()
            if latest and now_ms() - int(latest["created_at"]) < 5000:
                db.execute(
                    "UPDATE canvas_snapshots SET scene_json=?,created_at=? WHERE id=?",
                    (scene_json, now_ms(), latest["id"]),
                )
                return {"id": latest["id"], "canvas_id": canvas["id"], "version": latest["version"]}
            version = int(db.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM canvas_snapshots WHERE canvas_id=?",
                (canvas["id"],),
            ).fetchone()[0])
            snapshot_id = new_id("snapshot")
            db.execute(
                "INSERT INTO canvas_snapshots(id,canvas_id,version,scene_json,created_at) VALUES(?,?,?,?,?)",
                (snapshot_id, canvas["id"], version, scene_json, now_ms()),
            )
            db.execute(
                "UPDATE canvases SET current_snapshot_id=?,updated_at=? WHERE id=?",
                (snapshot_id, int(canvas.get("updated_at") or now_ms()), canvas["id"]),
            )
            return {"id": snapshot_id, "canvas_id": canvas["id"], "version": version}

    def asset_by_url(self, storage_url: str, project_id: str = "") -> Optional[Dict[str, Any]]:
        with self.connect() as db:
            query = """SELECT a.* FROM assets a JOIN asset_versions v ON v.asset_id=a.id
                       WHERE v.storage_url=?"""
            values = [storage_url]
            if project_id:
                query += " AND a.project_id=?"
                values.append(project_id)
            query += " ORDER BY v.created_at DESC LIMIT 1"
            row = db.execute(query, values).fetchone()
            return self._row(row)

    def register_asset(
        self,
        project_id: str,
        storage_url: str,
        *,
        asset_id: str = "",
        title: str = "",
        source: str = "",
        width: int = 0,
        height: int = 0,
        byte_size: int = 0,
        mime_type: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        existing = self.asset_by_url(storage_url, project_id)
        if existing and not asset_id:
            return existing
        if asset_id:
            with self.connect() as db:
                same_id = db.execute("SELECT project_id FROM assets WHERE id=?", (asset_id,)).fetchone()
            if same_id and same_id["project_id"] != project_id:
                metadata = {**(metadata or {}), "copied_from_asset_id": asset_id}
                asset_id = new_id("asset")
        asset_id = asset_id or new_id("asset")
        timestamp = now_ms()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        version_id = new_id("assetver")
        digest = hashlib.sha256(storage_url.encode("utf-8")).hexdigest()
        with self.connect() as db:
            db.execute(
                """INSERT INTO assets(id,project_id,kind,title,source,metadata_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     project_id=excluded.project_id,title=excluded.title,source=excluded.source,
                     metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
                (asset_id, project_id, "image", title[:240], source, metadata_json, timestamp, timestamp),
            )
            db.execute(
                """INSERT OR IGNORE INTO asset_versions
                   (id,asset_id,storage_url,sha256,mime_type,width,height,byte_size,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (version_id, asset_id, storage_url, digest, mime_type, width, height, byte_size, metadata_json, timestamp),
            )
            version = db.execute(
                "SELECT id FROM asset_versions WHERE asset_id=? AND storage_url=?",
                (asset_id, storage_url),
            ).fetchone()
            db.execute(
                "UPDATE assets SET current_version_id=?,updated_at=? WHERE id=?",
                (version["id"], timestamp, asset_id),
            )
            return dict(db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone())

    def create_generation_task(
        self,
        project_id: str,
        *,
        task_id: str = "",
        canvas_id: str = "",
        source_node_id: str = "",
        provider_id: str = "",
        model: str = "",
        prompt: str = "",
        parameters: Optional[Dict[str, Any]] = None,
        inputs: Iterable[Dict[str, Any]] = (),
    ) -> Dict[str, Any]:
        task_id = task_id or new_id("generation")
        timestamp = now_ms()
        with self.connect() as db:
            db.execute(
                """INSERT INTO generation_tasks
                   (id,project_id,canvas_id,source_node_id,provider_id,model,prompt,status,parameters_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id, project_id, canvas_id or None, source_node_id, provider_id, model,
                    prompt, "queued", json.dumps(parameters or {}, ensure_ascii=False), timestamp, timestamp,
                ),
            )
        for raw in inputs:
            source_url = str(raw.get("url") or "")
            asset = self.register_asset(
                project_id, source_url, asset_id=str(raw.get("asset_id") or ""),
                title=str(raw.get("name") or ""), source="generation_input",
            ) if source_url else None
            with self.connect() as db:
                db.execute(
                    """INSERT INTO generation_inputs
                       (id,task_id,asset_id,input_role,source_url,region_json,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        new_id("genin"), task_id, (asset or {}).get("id"),
                        str(raw.get("role") or "reference"), source_url,
                        json.dumps(raw.get("region") or {}, ensure_ascii=False), timestamp,
                    ),
                )
            if asset:
                self.record_preference_event(
                    project_id,
                    asset["id"],
                    "generation_reference",
                    {"generation_task_id": task_id, "input_role": str(raw.get("role") or "reference")},
                )
        return self.get_generation_task(task_id) or {}

    def update_generation_task(self, task_id: str, status: str, error: str = "") -> Dict[str, Any]:
        with self.connect() as db:
            db.execute(
                "UPDATE generation_tasks SET status=?,error=?,updated_at=? WHERE id=?",
                (status, error, now_ms(), task_id),
            )
        return self.get_generation_task(task_id) or {}

    def fail_interrupted_generation_tasks(self) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """UPDATE generation_tasks SET status='failed',
                   error='服务重启导致任务中断，请重新执行。',updated_at=?
                   WHERE status IN ('queued','running')""",
                (now_ms(),),
            )
            return int(cursor.rowcount)

    def get_generation_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM generation_tasks WHERE id=?", (task_id,)).fetchone()
            result = self._row(row)
            if not result:
                return None
            result["parameters"] = json.loads(result.pop("parameters_json") or "{}")
            result["inputs"] = [dict(item) for item in db.execute(
                "SELECT * FROM generation_inputs WHERE task_id=? ORDER BY created_at", (task_id,)
            )]
            result["outputs"] = [dict(item) for item in db.execute(
                """SELECT o.*,v.storage_url FROM generation_outputs o
                   JOIN assets a ON a.id=o.asset_id
                   JOIN asset_versions v ON v.id=a.current_version_id
                   WHERE o.task_id=? ORDER BY o.output_index""",
                (task_id,),
            )]
            return result

    def list_generation_tasks(
        self,
        *,
        project_id: str = "",
        canvas_id: str = "",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        clauses = []
        values: List[Any] = []
        if project_id:
            clauses.append("t.project_id=?")
            values.append(project_id)
        if canvas_id:
            clauses.append("t.canvas_id=?")
            values.append(canvas_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, min(200, int(limit or 50))))
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT t.*,
                       COUNT(DISTINCT i.id) AS input_count,
                       COUNT(DISTINCT o.id) AS output_count
                FROM generation_tasks t
                LEFT JOIN generation_inputs i ON i.task_id=t.id
                LEFT JOIN generation_outputs o ON o.task_id=t.id
                {where}
                GROUP BY t.id
                ORDER BY t.updated_at DESC
                LIMIT ?
                """,
                values,
            )
            result = []
            for row in rows:
                item = dict(row)
                item["parameters"] = json.loads(item.pop("parameters_json") or "{}")
                result.append(item)
            return result

    def complete_generation_task(self, task_id: str, output_urls: List[str]) -> Dict[str, Any]:
        task = self.get_generation_task(task_id)
        if not task:
            raise KeyError(task_id)
        input_asset_ids = [item["asset_id"] for item in task["inputs"] if item.get("asset_id")]
        outputs = [
            self.register_asset(
                task["project_id"], url, title=f"生成结果 {index + 1}",
                source="generation_output", metadata={"task_id": task_id, "output_index": index},
            )
            for index, url in enumerate(output_urls)
        ]
        with self.connect() as db:
            for index, asset in enumerate(outputs):
                db.execute(
                    """INSERT OR REPLACE INTO generation_outputs
                       (id,task_id,asset_id,output_index,status,created_at) VALUES(?,?,?,?,?,?)""",
                    (new_id("genout"), task_id, asset["id"], index, "succeeded", now_ms()),
                )
                for input_asset_id in input_asset_ids:
                    db.execute(
                        """INSERT OR IGNORE INTO lineage_edges
                           (id,project_id,from_asset_id,to_asset_id,generation_task_id,relation_type,created_at)
                           VALUES(?,?,?,?,?,?,?)""",
                        (
                            new_id("lineage"), task["project_id"], input_asset_id, asset["id"],
                            task_id, "generated_from", now_ms(),
                        ),
                    )
        for input_asset_id in input_asset_ids:
            self.record_preference_event(
                task["project_id"], input_asset_id, "variant_generated",
                {"generation_task_id": task_id, "output_asset_ids": [item["id"] for item in outputs]},
            )
        for output in outputs:
            self.record_preference_event(
                task["project_id"], output["id"], "generated_output",
                {"generation_task_id": task_id, "input_asset_ids": input_asset_ids},
            )
        return self.update_generation_task(task_id, "succeeded")

    @staticmethod
    def _feedback_summary(events: List[Dict[str, Any]]) -> Dict[str, Any]:
        counts = Counter(str(item.get("event_type") or "") for item in events)
        favorite_state = False
        adopted_state = False
        for item in sorted(events, key=lambda row: int(row.get("created_at") or 0)):
            event_type = str(item.get("event_type") or "")
            if event_type == "favorite":
                favorite_state = True
            elif event_type == "unfavorite":
                favorite_state = False
            elif event_type == "final_adopted":
                adopted_state = True
            elif event_type == "final_unadopted":
                adopted_state = False
        score = (
            (3 if favorite_state else 0)
            + (8 if adopted_state else 0)
            + min(10, counts["used_in_canvas"] * 2)
            + min(20, counts["generation_reference"] * 4)
            + min(10, counts["variant_generated"] * 2)
            + min(5, counts["saved_to_library"])
        )
        latest = max(events, key=lambda row: int(row.get("created_at") or 0), default={})
        return {
            "score": score,
            "favorited": favorite_state,
            "adopted": adopted_state,
            "event_count": len(events),
            "counts": dict(counts),
            "last_event_type": latest.get("event_type") or "",
            "last_event_at": int(latest.get("created_at") or 0),
        }

    def record_preference_event(
        self,
        project_id: str,
        asset_id: str,
        event_type: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        allowed = {
            "favorite", "unfavorite", "used_in_canvas", "generation_reference",
            "variant_generated", "generated_output", "saved_to_library",
            "final_adopted", "final_unadopted",
        }
        event_type = str(event_type or "").strip()
        if event_type not in allowed:
            raise ValueError(f"unsupported preference event: {event_type}")
        with self.connect() as db:
            asset = db.execute("SELECT project_id FROM assets WHERE id=?", (asset_id,)).fetchone()
            if not asset or str(asset["project_id"]) != str(project_id):
                raise ValueError("asset does not belong to project")
            db.execute(
                """INSERT INTO preference_events(id,project_id,asset_id,event_type,context_json,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (
                    new_id("pref"), project_id, asset_id, event_type,
                    json.dumps(context or {}, ensure_ascii=False), now_ms(),
                ),
            )
        return self.feedback_for_asset(asset_id, project_id)

    def feedback_for_asset(self, asset_id: str, project_id: str = "") -> Dict[str, Any]:
        with self.connect() as db:
            query = "SELECT * FROM preference_events WHERE asset_id=?"
            values: List[Any] = [asset_id]
            if project_id:
                query += " AND project_id=?"
                values.append(project_id)
            rows = [dict(row) for row in db.execute(query, values)]
        return self._feedback_summary(rows)

    def project_feedback_summary(self, project_id: str, limit: int = 8) -> Dict[str, Any]:
        with self.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM preference_events WHERE project_id=? ORDER BY created_at DESC",
                (project_id,),
            )]
            asset_rows = {
                row["id"]: dict(row)
                for row in db.execute(
                    """SELECT a.*,v.storage_url FROM assets a
                       LEFT JOIN asset_versions v ON v.id=a.current_version_id
                       WHERE a.project_id=?""",
                    (project_id,),
                )
            }
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row["asset_id"]), []).append(row)
        ranked = []
        for asset_id, events in grouped.items():
            asset = asset_rows.get(asset_id)
            if not asset:
                continue
            ranked.append({**asset, "feedback": self._feedback_summary(events)})
        ranked.sort(
            key=lambda item: (
                int((item.get("feedback") or {}).get("score") or 0),
                int((item.get("feedback") or {}).get("last_event_at") or 0),
            ),
            reverse=True,
        )
        return {
            "event_count": len(rows),
            "asset_count": len(grouped),
            "favorited_assets": sum(1 for item in ranked if item["feedback"]["favorited"]),
            "adopted_assets": sum(1 for item in ranked if item["feedback"]["adopted"]),
            "top_assets": ranked[:max(1, min(50, int(limit or 8)))],
        }

    def lineage_for_asset(self, asset_id: str) -> Dict[str, Any]:
        with self.connect() as db:
            asset = self._row(db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone())
            versions = [dict(row) for row in db.execute(
                """SELECT id,asset_id,storage_url,mime_type,width,height,byte_size,created_at
                   FROM asset_versions WHERE asset_id=? ORDER BY created_at DESC""",
                (asset_id,),
            )]
            upstream = [dict(row) for row in db.execute(
                """SELECT l.*,a.title,a.current_version_id,v.storage_url
                   FROM lineage_edges l
                   JOIN assets a ON a.id=l.from_asset_id
                   LEFT JOIN asset_versions v ON v.id=a.current_version_id
                   WHERE l.to_asset_id=?""",
                (asset_id,),
            )]
            downstream = [dict(row) for row in db.execute(
                """SELECT l.*,a.title,a.current_version_id,v.storage_url
                   FROM lineage_edges l
                   JOIN assets a ON a.id=l.to_asset_id
                   LEFT JOIN asset_versions v ON v.id=a.current_version_id
                   WHERE l.from_asset_id=?""",
                (asset_id,),
            )]
            feedback = self.feedback_for_asset(asset_id, str((asset or {}).get("project_id") or "")) if asset else self._feedback_summary([])
            return {"asset": asset, "versions": versions, "upstream": upstream, "downstream": downstream, "feedback": feedback}

    def project_overview(self, project_id: str) -> Dict[str, Any]:
        project = self.get_project(project_id)
        if not project:
            return {}
        with self.connect() as db:
            counts = {}
            for table in ("assets", "canvases", "generation_tasks", "lineage_edges", "preference_events"):
                counts[table] = int(db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE project_id=?", (project_id,)
                ).fetchone()[0])
            return {"project": project, "counts": counts}

    def project_workspace(self, project_id: str, limit: int = 24) -> Dict[str, Any]:
        project = self.get_project(project_id)
        if not project:
            return {}
        limit = max(1, min(100, int(limit or 24)))
        with self.connect() as db:
            canvases = [dict(row) for row in db.execute(
                """
                SELECT c.*,
                       (SELECT COUNT(*) FROM canvas_snapshots s WHERE s.canvas_id=c.id) AS snapshot_count,
                       (SELECT COUNT(*) FROM generation_tasks t WHERE t.canvas_id=c.id) AS task_count,
                       (SELECT COUNT(*) FROM generation_tasks t WHERE t.canvas_id=c.id AND t.status='succeeded') AS succeeded_count
                FROM canvases c
                WHERE c.project_id=?
                ORDER BY c.updated_at DESC
                """,
                (project_id,),
            )]
            assets = [dict(row) for row in db.execute(
                """
                SELECT a.*,v.storage_url,v.width,v.height,v.mime_type,v.byte_size,
                       (SELECT COUNT(*) FROM lineage_edges l WHERE l.to_asset_id=a.id) AS upstream_count,
                       (SELECT COUNT(*) FROM lineage_edges l WHERE l.from_asset_id=a.id) AS downstream_count
                FROM assets a
                LEFT JOIN asset_versions v ON v.id=a.current_version_id
                WHERE a.project_id=?
                ORDER BY a.updated_at DESC
                LIMIT ?
                """,
                (project_id, limit),
            )]
            status_rows = db.execute(
                """SELECT status,COUNT(*) AS count FROM generation_tasks
                   WHERE project_id=? GROUP BY status""",
                (project_id,),
            )
            task_status = {row["status"]: int(row["count"]) for row in status_rows}
            counts = {
                "canvases": len(canvases),
                "assets": int(db.execute(
                    "SELECT COUNT(*) FROM assets WHERE project_id=?", (project_id,)
                ).fetchone()[0]),
                "generation_tasks": sum(task_status.values()),
                "lineage_edges": int(db.execute(
                    "SELECT COUNT(*) FROM lineage_edges WHERE project_id=?", (project_id,)
                ).fetchone()[0]),
                "active_tasks": task_status.get("queued", 0) + task_status.get("running", 0),
                "succeeded_tasks": task_status.get("succeeded", 0),
                "attention_tasks": task_status.get("failed", 0) + task_status.get("cancelled", 0),
            }
        feedback_summary = self.project_feedback_summary(project_id, limit=8)
        feedback_by_asset = {
            item["id"]: item.get("feedback") or self._feedback_summary([])
            for item in feedback_summary.get("top_assets") or []
        }
        for asset in assets:
            asset["feedback"] = feedback_by_asset.get(asset["id"]) or self.feedback_for_asset(asset["id"], project_id)
        counts["feedback_events"] = feedback_summary["event_count"]
        counts["adopted_assets"] = feedback_summary["adopted_assets"]
        counts["favorited_assets"] = feedback_summary["favorited_assets"]
        tasks = self.list_generation_tasks(project_id=project_id, limit=limit)
        canvas_titles = {item["id"]: item["title"] for item in canvases}
        for task in tasks:
            task["canvas_title"] = canvas_titles.get(task.get("canvas_id"), "")
        return {
            "project": project,
            "counts": counts,
            "canvases": canvases,
            "recent_tasks": tasks,
            "recent_assets": assets,
            "feedback_summary": feedback_summary,
        }
