from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from aiohttp import web

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_NAME = "astrbot_plugin_persona_sublimation"
MAX_DB_BYTES = 50 * 1024 * 1024
PRUNE_SIZE_BATCH_ROWS = 50
PRUNE_SIZE_MAX_BATCHES = 20
PRUNE_MAX_SECONDS = 0.75


@register(
    PLUGIN_NAME,
    "SGSxingchen",
    "人格升华：捕获实际发往 LLM 的 ProviderRequest 快照（第一阶段只读 Hook）。",
    "0.1.0",
    "",
)
class PersonaSublimationPlugin(Star):
    """Read-only LLM request capture plugin."""

    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.enabled = bool(self._cfg("enabled", True))
        self.max_records = max(1, int(self._cfg("max_records", 500) or 500))
        self.capture_full_contexts = bool(self._cfg("capture_full_contexts", True))
        self.bind_host = str(self._cfg("bind_host", "127.0.0.1") or "127.0.0.1")
        self.port = int(self._cfg("port", 7833) or 7833)
        if self.bind_host in {"0.0.0.0", "::"}:
            logger.warning(
                "PersonaSublimation HTTP page is bound to %s without built-in auth. "
                "It exposes full prompts/contexts; use a reverse proxy, ACL, and auth.",
                self.bind_host,
            )
        self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "captures.sqlite3"
        self._lock = RLock()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._server_task: asyncio.Task | None = None
        self._init_db()
        self._start_http_server_background()
        logger.info(
            "PersonaSublimation initialized. Page: http://%s:%s/ DB: %s",
            self.bind_host,
            self.port,
            self.db_path,
        )

    def _cfg(self, key: str, default: Any = None) -> Any:
        if hasattr(self.config, "get"):
            return self.config.get(key, default)
        return default

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_request_captures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    timestamp_iso TEXT NOT NULL,
                    session_id TEXT,
                    platform TEXT,
                    unified_msg_origin TEXT,
                    sender_id TEXT,
                    sender_name TEXT,
                    persona_id TEXT,
                    model TEXT,
                    prompt TEXT,
                    system_prompt TEXT,
                    contexts_json TEXT NOT NULL,
                    image_urls_json TEXT NOT NULL,
                    audio_urls_json TEXT NOT NULL,
                    extra_user_content_parts_json TEXT NOT NULL DEFAULT '[]',
                    tools_json TEXT NOT NULL,
                    tool_calls_result_json TEXT NOT NULL DEFAULT 'null',
                    metadata_json TEXT NOT NULL
                )
                """
            )
            self._ensure_column(
                conn,
                "llm_request_captures",
                "extra_user_content_parts_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column(
                conn,
                "llm_request_captures",
                "tool_calls_result_json",
                "TEXT NOT NULL DEFAULT 'null'",
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ps_capture_session_time "
                "ON llm_request_captures(session_id, timestamp DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ps_capture_time "
                "ON llm_request_captures(timestamp DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS persona_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    timestamp_iso TEXT NOT NULL,
                    persona_id TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    interpretation TEXT NOT NULL DEFAULT '',
                    emotion TEXT NOT NULL DEFAULT '',
                    capture_id INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS persona_patches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patch_id TEXT NOT NULL UNIQUE,
                    timestamp REAL NOT NULL,
                    timestamp_iso TEXT NOT NULL,
                    persona_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    trigger TEXT NOT NULL DEFAULT '',
                    changes_json TEXT NOT NULL DEFAULT '[]',
                    core_preserved_json TEXT NOT NULL DEFAULT '[]',
                    proposed_prompt TEXT,
                    base_prompt TEXT,
                    diff TEXT NOT NULL DEFAULT '',
                    approved_by TEXT,
                    applied_at TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            self._ensure_column(conn, "persona_patches", "proposed_prompt", "TEXT")
            self._ensure_column(conn, "persona_patches", "base_prompt", "TEXT")
            self._ensure_column(
                conn, "persona_patches", "diff", "TEXT NOT NULL DEFAULT ''"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ps_observation_persona_time "
                "ON persona_observations(persona_id, timestamp DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ps_patch_persona_time "
                "ON persona_patches(persona_id, timestamp DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS persona_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    template_id TEXT NOT NULL UNIQUE,
                    timestamp REAL NOT NULL,
                    timestamp_iso TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    variables_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS persona_profiles (
                    persona_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL DEFAULT '',
                    archetype TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    template_id TEXT,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS persona_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id TEXT NOT NULL UNIQUE,
                    timestamp REAL NOT NULL,
                    timestamp_iso TEXT NOT NULL,
                    persona_id TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    source_path TEXT NOT NULL DEFAULT '',
                    content_sha256 TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(persona_id, source, source_path, content_sha256)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ps_snapshot_persona_time "
                "ON persona_snapshots(persona_id, timestamp DESC)"
            )
            conn.commit()

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        column_name: str,
        column_type: str,
    ) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}
        if column_name not in columns:
            conn.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
            )

    @filter.on_llm_request(priority=-100)
    async def capture_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """Capture the final-ish ProviderRequest state without mutating it."""
        if not self.enabled:
            return
        try:
            snapshot = self._build_snapshot(event, req)
            await asyncio.to_thread(self._insert_snapshot, snapshot)
        except Exception as exc:
            logger.warning("PersonaSublimation capture failed: %s", exc, exc_info=True)

    def _build_snapshot(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        contexts = list(req.contexts or [])
        stored_contexts = (
            contexts
            if self.capture_full_contexts
            else self._summarize_contexts(contexts)
        )
        session_id = str(
            getattr(req, "session_id", None) or getattr(event, "session_id", "") or ""
        )
        conversation = getattr(req, "conversation", None)
        persona_id = getattr(conversation, "persona_id", None)
        if not persona_id and hasattr(event, "get_extra"):
            try:
                persona_id = event.get_extra("persona_id")
            except Exception:
                persona_id = None

        return {
            "timestamp": now.timestamp(),
            "timestamp_iso": now.isoformat(),
            "session_id": session_id,
            "platform": self._safe_call(event, "get_platform_name")
            or getattr(getattr(event, "platform_meta", None), "name", ""),
            "unified_msg_origin": str(getattr(event, "unified_msg_origin", "") or ""),
            "sender_id": self._safe_call(event, "get_sender_id"),
            "sender_name": self._safe_call(event, "get_sender_name"),
            "persona_id": str(persona_id) if persona_id is not None else "",
            "model": str(getattr(req, "model", "") or ""),
            "prompt": getattr(req, "prompt", None) or "",
            "system_prompt": getattr(req, "system_prompt", "") or "",
            "contexts": stored_contexts,
            "image_urls": list(getattr(req, "image_urls", None) or []),
            "audio_urls": list(getattr(req, "audio_urls", None) or []),
            "extra_user_content_parts": self._to_jsonable(
                list(getattr(req, "extra_user_content_parts", None) or [])
            ),
            "tools": self._tool_names(getattr(req, "func_tool", None)),
            "tool_calls_result": self._to_jsonable(
                getattr(req, "tool_calls_result", None)
            ),
            "metadata": {
                "capture_full_contexts": self.capture_full_contexts,
                "context_count": len(contexts),
                "image_count": len(getattr(req, "image_urls", None) or []),
                "audio_count": len(getattr(req, "audio_urls", None) or []),
            },
        }

    def _safe_call(self, obj: Any, method_name: str) -> str:
        method = getattr(obj, method_name, None)
        if not callable(method):
            return ""
        try:
            value = method()
        except Exception:
            return ""
        return str(value or "")

    def _summarize_contexts(self, contexts: list[dict]) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for idx, item in enumerate(contexts):
            content = item.get("content", "") if isinstance(item, dict) else str(item)
            content_text = self._content_to_text(content)
            summaries.append(
                {
                    "index": idx,
                    "role": item.get("role", "") if isinstance(item, dict) else "",
                    "content_length": len(content_text),
                    "content_preview": content_text[:240],
                    "summary_only": True,
                }
            )
        return summaries

    def _content_to_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        try:
            return json.dumps(content, ensure_ascii=False)
        except TypeError:
            return str(content)

    def _to_jsonable(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, list | tuple):
            return [self._to_jsonable(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self._to_jsonable(item) for key, item in value.items()}
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return self._to_jsonable(model_dump())
            except Exception:
                pass
        if hasattr(value, "__dict__"):
            try:
                return self._to_jsonable(vars(value))
            except Exception:
                pass
        return str(value)

    def _tool_names(self, func_tool: Any) -> list[str]:
        if not func_tool:
            return []
        tools = getattr(func_tool, "tools", None) or getattr(
            func_tool, "func_list", None
        )
        if tools is not None:
            names = []
            for tool in tools:
                if getattr(tool, "active", True):
                    name = getattr(tool, "name", None)
                    if name:
                        names.append(str(name))
            return names
        schema_method = getattr(func_tool, "openai_schema", None) or getattr(
            func_tool, "get_func_desc_openai_style", None
        )
        if callable(schema_method):
            try:
                schema = schema_method()
                return [
                    str(item.get("function", {}).get("name"))
                    for item in schema
                    if isinstance(item, dict) and item.get("function", {}).get("name")
                ]
            except Exception:
                return []
        return []

    def _json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    def _loads(self, value: str | None, default: Any) -> Any:
        try:
            return json.loads(value or "")
        except (TypeError, json.JSONDecodeError):
            return default

    def _sha256_text(self, value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _safe_id_part(self, value: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value)
        return safe.strip("-") or "default"

    def _snapshot_row_to_item(
        self, row: sqlite3.Row, include_content: bool = False
    ) -> dict[str, Any]:
        item = dict(row)
        item["metadata"] = self._loads(item.pop("metadata_json"), {})
        item["content_len"] = len(item.get("content") or "")
        if not include_content:
            item.pop("content", None)
        return item

    def _insert_snapshot_record(
        self,
        conn: sqlite3.Connection,
        *,
        persona_id: str,
        content: str,
        label: str,
        source: str,
        source_path: str = "",
        metadata: dict[str, Any] | None = None,
        snapshot_id: str | None = None,
    ) -> str:
        ts, iso = self._now()
        content_sha = self._sha256_text(content)
        snapshot_id = snapshot_id or (
            f"snap-{self._safe_id_part(persona_id)}-{content_sha[:12]}-{int(ts)}"
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO persona_snapshots
            (snapshot_id, timestamp, timestamp_iso, persona_id, label, content,
             source, source_path, content_sha256, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                ts,
                iso,
                persona_id,
                label,
                content,
                source,
                source_path,
                content_sha,
                self._json(metadata or {}),
            ),
        )
        return snapshot_id

    def _insert_snapshot(self, snapshot: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO llm_request_captures (
                    timestamp, timestamp_iso, session_id, platform,
                    unified_msg_origin, sender_id, sender_name, persona_id,
                    model, prompt, system_prompt, contexts_json,
                    image_urls_json, audio_urls_json, extra_user_content_parts_json,
                    tools_json, tool_calls_result_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot["timestamp"],
                    snapshot["timestamp_iso"],
                    snapshot["session_id"],
                    snapshot["platform"],
                    snapshot["unified_msg_origin"],
                    snapshot["sender_id"],
                    snapshot["sender_name"],
                    snapshot["persona_id"],
                    snapshot["model"],
                    snapshot["prompt"],
                    snapshot["system_prompt"],
                    self._json(snapshot["contexts"]),
                    self._json(snapshot["image_urls"]),
                    self._json(snapshot["audio_urls"]),
                    self._json(snapshot["extra_user_content_parts"]),
                    self._json(snapshot["tools"]),
                    self._json(snapshot["tool_calls_result"]),
                    self._json(snapshot["metadata"]),
                ),
            )
            self._prune_locked(conn)
            conn.commit()

    def _prune_locked(self, conn: sqlite3.Connection) -> None:
        start = time.monotonic()

        def over_time_budget() -> bool:
            return (time.monotonic() - start) >= PRUNE_MAX_SECONDS

        try:
            conn.execute(
                "DELETE FROM llm_request_captures WHERE id NOT IN "
                "(SELECT id FROM llm_request_captures ORDER BY timestamp DESC, id DESC LIMIT ?)",
                (self.max_records,),
            )
            conn.commit()
            try:
                size = self.db_path.stat().st_size
            except OSError:
                size = 0

            batches = 0
            while size > MAX_DB_BYTES and batches < PRUNE_SIZE_MAX_BATCHES:
                if over_time_budget():
                    break
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM llm_request_captures"
                ).fetchone()
                remaining = int(row["c"] if row else 0)
                if remaining <= 1:
                    break
                delete_count = min(PRUNE_SIZE_BATCH_ROWS, remaining - 1)
                cur = conn.execute(
                    "DELETE FROM llm_request_captures WHERE id IN "
                    "(SELECT id FROM llm_request_captures ORDER BY timestamp ASC, id ASC LIMIT ?)",
                    (delete_count,),
                )
                conn.commit()
                batches += 1
                if cur.rowcount <= 0:
                    break
                try:
                    size = self.db_path.stat().st_size
                except OSError:
                    break

            if size > MAX_DB_BYTES and batches >= PRUNE_SIZE_MAX_BATCHES:
                logger.warning(
                    "PersonaSublimation prune reached batch limit (%s); DB may remain large: %s",
                    PRUNE_SIZE_MAX_BATCHES,
                    self.db_path,
                )

            if size > 0 and not over_time_budget():
                try:
                    conn.execute("VACUUM")
                except sqlite3.DatabaseError as exc:
                    logger.warning(
                        "PersonaSublimation VACUUM skipped/failed for %s: %s",
                        self.db_path,
                        exc,
                    )
        except Exception as exc:
            logger.warning("PersonaSublimation prune failed: %s", exc, exc_info=True)

    def _start_http_server_background(self) -> None:
        try:
            self._server_task = asyncio.create_task(self._start_http_server())
        except RuntimeError as exc:
            logger.warning(
                "PersonaSublimation HTTP server was not started: %s", exc, exc_info=True
            )
            self._server_task = None

    async def _start_http_server(self) -> None:
        app = web.Application()
        app.router.add_get("/", self.serve_index)
        app.router.add_get("/api/captures", self.api_list_captures)
        app.router.add_get(r"/api/captures/{capture_id:\d+}", self.api_get_capture)
        app.router.add_get("/api/sessions", self.api_list_sessions)
        app.router.add_get("/api/personas", self.api_list_personas)
        app.router.add_get("/api/personas/{persona_id}", self.api_get_persona)
        app.router.add_get("/api/observations", self.api_list_observations)
        app.router.add_post("/api/observations", self.api_create_observation)
        app.router.add_get("/api/patches", self.api_list_patches)
        app.router.add_post("/api/patches", self.api_create_patch)
        app.router.add_post("/api/patches/{patch_id}/approve", self.api_approve_patch)
        app.router.add_post("/api/patches/{patch_id}/apply", self.api_apply_patch)
        app.router.add_post("/api/migrate-skill", self.api_migrate_skill_files)
        app.router.add_get("/api/templates", self.api_list_templates)
        app.router.add_post("/api/templates", self.api_create_template)
        app.router.add_get("/api/snapshots", self.api_list_snapshots)
        app.router.add_post("/api/snapshots", self.api_create_snapshot)
        app.router.add_get("/api/snapshots/{snapshot_id}", self.api_get_snapshot)
        app.router.add_get("/api/profiles", self.api_list_profiles)
        app.router.add_post("/api/profiles/{persona_id}", self.api_upsert_profile)

        runner = web.AppRunner(app)
        try:
            await runner.setup()
            site = web.TCPSite(runner, self.bind_host, self.port)
            await site.start()
        except Exception as exc:
            with contextlib.suppress(Exception):
                await runner.cleanup()
            logger.warning(
                "PersonaSublimation HTTP server failed to start at http://%s:%s/: %s",
                self.bind_host,
                self.port,
                exc,
                exc_info=True,
            )
            return

        self._runner = runner
        self._site = site
        logger.info(
            "PersonaSublimation HTTP server started at http://%s:%s/",
            self.bind_host,
            self.port,
        )

    async def terminate(self) -> None:
        await self._stop_http_server()

    async def _stop_http_server(self) -> None:
        if self._site:
            with contextlib.suppress(Exception):
                await self._site.stop()
            self._site = None
        if self._runner:
            with contextlib.suppress(Exception):
                await self._runner.cleanup()
            self._runner = None
        if self._server_task and not self._server_task.done():
            self._server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._server_task
        self._server_task = None
        logger.info("PersonaSublimation HTTP server stopped")

    async def serve_index(self, _request: web.Request) -> web.Response:
        html_path = Path(__file__).parent / "pages" / "index.html"
        return web.Response(
            text=html_path.read_text(encoding="utf-8"),
            content_type="text/html",
            charset="utf-8",
        )

    async def api_list_captures(self, req: web.Request) -> web.Response:
        session_id = str(req.query.get("session_id", "")).strip()
        try:
            limit = min(200, max(1, int(req.query.get("limit", 50))))
            offset = max(0, int(req.query.get("offset", 0)))
        except (TypeError, ValueError):
            return web.json_response(
                {"ok": False, "error": "invalid pagination"}, status=400
            )

        where = "WHERE session_id = ?" if session_id else ""
        params: list[Any] = [session_id] if session_id else []
        with self._lock, self._connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS total FROM llm_request_captures {where}",
                params,
            ).fetchone()
            rows = conn.execute(
                f"""
                SELECT id, timestamp_iso, session_id, platform, unified_msg_origin,
                       sender_id, sender_name, persona_id, model, prompt
                FROM llm_request_captures
                {where}
                ORDER BY timestamp DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
        return web.json_response(
            {
                "ok": True,
                "total": int(total_row["total"] if total_row else 0),
                "items": [dict(row) for row in rows],
            }
        )

    async def api_get_capture(self, req: web.Request) -> web.Response:
        try:
            capture_id = int(req.match_info["capture_id"])
        except (KeyError, TypeError, ValueError):
            return web.json_response({"ok": False, "error": "invalid id"}, status=400)

        item = self._get_capture_item(capture_id)
        if not item:
            return web.json_response({"ok": False, "error": "not found"}, status=404)
        return web.json_response({"ok": True, "item": item})

    async def api_list_sessions(self, _request: web.Request) -> web.Response:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT session_id,
                       MAX(timestamp) AS last_timestamp,
                       MAX(timestamp_iso) AS last_timestamp_iso,
                       COUNT(*) AS count
                FROM llm_request_captures
                WHERE COALESCE(session_id, '') != ''
                GROUP BY session_id
                ORDER BY last_timestamp DESC
                LIMIT 500
                """
            ).fetchall()
        return web.json_response(
            {
                "ok": True,
                "items": [dict(row) for row in rows],
            }
        )

    def _now(self) -> tuple[float, str]:
        now = datetime.now(timezone.utc)
        return now.timestamp(), now.isoformat()

    def _parse_json_body(self, request: web.Request) -> Any:
        # placeholder kept for type locality; aiohttp json parsing is async at call sites.
        return None

    async def api_list_personas(self, _request: web.Request) -> web.Response:
        try:
            personas = await self.context.persona_manager.get_all_personas()
            with self._lock, self._connect() as conn:
                obs_counts = {
                    row["persona_id"]: int(row["c"])
                    for row in conn.execute(
                        "SELECT persona_id, COUNT(*) AS c FROM persona_observations GROUP BY persona_id"
                    )
                }
                patch_counts = {
                    row["persona_id"]: int(row["c"])
                    for row in conn.execute(
                        "SELECT persona_id, COUNT(*) AS c FROM persona_patches GROUP BY persona_id"
                    )
                }
            items = [
                {
                    "persona_id": p.persona_id,
                    "system_prompt_len": len(p.system_prompt or ""),
                    "updated_at": str(getattr(p, "updated_at", "") or ""),
                    "observation_count": obs_counts.get(p.persona_id, 0),
                    "patch_count": patch_counts.get(p.persona_id, 0),
                }
                for p in personas
            ]
            return web.json_response({"ok": True, "items": items})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    async def api_get_persona(self, request: web.Request) -> web.Response:
        persona_id = request.match_info.get("persona_id", "").strip()
        try:
            persona = await self.context.persona_manager.get_persona(persona_id)
            return web.json_response(
                {
                    "ok": True,
                    "item": {
                        "persona_id": persona.persona_id,
                        "system_prompt": persona.system_prompt or "",
                        "begin_dialogs": persona.begin_dialogs or [],
                        "tools": persona.tools or [],
                        "skills": getattr(persona, "skills", None) or [],
                    },
                }
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=404)

    async def api_list_observations(self, request: web.Request) -> web.Response:
        persona_id = request.query.get("persona_id", "").strip()
        limit = min(200, max(1, int(request.query.get("limit", 80))))
        where = "WHERE persona_id = ?" if persona_id else ""
        params: list[Any] = [persona_id] if persona_id else []
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM persona_observations {where} ORDER BY timestamp DESC, id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["metadata"] = self._loads(item.pop("metadata_json"), {})
            items.append(item)
        return web.json_response({"ok": True, "items": items})

    async def api_create_observation(self, request: web.Request) -> web.Response:
        data = await request.json()
        persona_id = str(data.get("persona_id", "")).strip()
        content = str(data.get("content", "")).strip()
        if not persona_id or not content:
            return web.json_response(
                {"ok": False, "error": "persona_id 和 content 必填"}, status=400
            )
        ts, iso = self._now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO persona_observations
                (timestamp, timestamp_iso, persona_id, source, content, interpretation, emotion, capture_id, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    iso,
                    persona_id,
                    str(data.get("source", "") or ""),
                    content,
                    str(data.get("interpretation", "") or ""),
                    str(data.get("emotion", "") or ""),
                    data.get("capture_id"),
                    self._json(data.get("metadata", {}) or {}),
                ),
            )
            conn.commit()
            new_id = cur.lastrowid
        return web.json_response({"ok": True, "id": new_id})

    async def api_list_patches(self, request: web.Request) -> web.Response:
        persona_id = request.query.get("persona_id", "").strip()
        limit = min(200, max(1, int(request.query.get("limit", 80))))
        where = "WHERE persona_id = ?" if persona_id else ""
        params: list[Any] = [persona_id] if persona_id else []
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM persona_patches {where} ORDER BY timestamp DESC, id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return web.json_response(
            {"ok": True, "items": [self._patch_row_to_item(row) for row in rows]}
        )

    def _patch_row_to_item(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for key in ("changes_json", "core_preserved_json", "metadata_json"):
            target = key.removesuffix("_json")
            item[target] = self._loads(item.pop(key), None)
        return item

    async def api_create_patch(self, request: web.Request) -> web.Response:
        data = await request.json()
        persona_id = str(data.get("persona_id", "")).strip()
        proposed_prompt = data.get("proposed_prompt")
        if not persona_id:
            return web.json_response(
                {"ok": False, "error": "persona_id 必填"}, status=400
            )
        try:
            persona = await self.context.persona_manager.get_persona(persona_id)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=404)
        base_prompt = data.get("base_prompt")
        if base_prompt is None:
            base_prompt = persona.system_prompt or ""
        if proposed_prompt is None:
            proposed_prompt = base_prompt
        patch_id = str(data.get("patch_id") or self._next_patch_id(persona_id))
        diff = "\n".join(
            difflib.unified_diff(
                str(base_prompt).splitlines(),
                str(proposed_prompt).splitlines(),
                fromfile=f"{persona_id}:current",
                tofile=f"{persona_id}:proposed",
                lineterm="",
            )
        )
        ts, iso = self._now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO persona_patches
                (patch_id, timestamp, timestamp_iso, persona_id, status, trigger, changes_json,
                 core_preserved_json, proposed_prompt, base_prompt, diff, approved_by, metadata_json)
                VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    patch_id,
                    ts,
                    iso,
                    persona_id,
                    str(data.get("trigger", "") or ""),
                    self._json(data.get("changes", []) or []),
                    self._json(data.get("core_preserved", []) or []),
                    str(proposed_prompt),
                    str(base_prompt),
                    diff,
                    data.get("approved_by"),
                    self._json(data.get("metadata", {}) or {}),
                ),
            )
            conn.commit()
        return web.json_response({"ok": True, "patch_id": patch_id, "diff": diff})

    def _next_patch_id(self, persona_id: str) -> str:
        prefix = "P" + "".join(ch for ch in persona_id if ch.isalnum())[:8]
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM persona_patches WHERE persona_id = ?",
                (persona_id,),
            ).fetchone()
        return f"{prefix}-{int(row['c'] if row else 0) + 1:03d}"

    async def api_approve_patch(self, request: web.Request) -> web.Response:
        patch_id = request.match_info.get("patch_id", "").strip()
        data = await request.json() if request.can_read_body else {}
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE persona_patches SET status = 'approved', approved_by = ? WHERE patch_id = ? AND status = 'pending'",
                (str(data.get("approved_by", "") or ""), patch_id),
            )
            conn.commit()
        if cur.rowcount <= 0:
            return web.json_response(
                {"ok": False, "error": "补丁不存在或状态不是 pending"}, status=404
            )
        return web.json_response({"ok": True})

    async def api_apply_patch(self, request: web.Request) -> web.Response:
        patch_id = request.match_info.get("patch_id", "").strip()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM persona_patches WHERE patch_id = ?", (patch_id,)
            ).fetchone()
        if not row:
            return web.json_response({"ok": False, "error": "补丁不存在"}, status=404)
        item = self._patch_row_to_item(row)
        if item["status"] != "approved":
            return web.json_response(
                {"ok": False, "error": "补丁必须先 approve"}, status=400
            )
        proposed = item.get("proposed_prompt")
        if not proposed:
            return web.json_response(
                {"ok": False, "error": "补丁没有 proposed_prompt"}, status=400
            )
        try:
            persona = await self.context.persona_manager.get_persona(item["persona_id"])
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=404)
        current_prompt = persona.system_prompt or ""
        base_prompt = item.get("base_prompt")
        if base_prompt is not None and str(base_prompt) != current_prompt:
            return web.json_response(
                {
                    "ok": False,
                    "error": "当前人格 system_prompt 已变化，请基于最新内容重新创建补丁",
                },
                status=409,
            )
        await self.context.persona_manager.update_persona(
            persona_id=item["persona_id"],
            system_prompt=proposed,
        )
        _, iso = self._now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE persona_patches SET status = 'applied', applied_at = ? WHERE patch_id = ?",
                (iso, patch_id),
            )
            conn.commit()
        return web.json_response({"ok": True, "applied_at": iso})

    async def api_migrate_skill_files(self, request: web.Request) -> web.Response:
        data = await request.json() if request.can_read_body else {}
        skill_dir = Path(
            str(data.get("skill_dir") or "/root/AstrBot/data/skills/persona-evolution")
        )
        migrated: list[str] = []
        skipped: list[str] = []
        if not skill_dir.exists():
            return web.json_response(
                {"ok": False, "error": f"目录不存在: {skill_dir}"}, status=404
            )
        for obs in skill_dir.glob("observation_notes_*.md"):
            persona_id = obs.stem.removeprefix("observation_notes_")
            content = obs.read_text(encoding="utf-8")
            if content.strip():
                ts, iso = self._now()
                with self._lock, self._connect() as conn:
                    exists = conn.execute(
                        """
                        SELECT 1 FROM persona_observations
                        WHERE persona_id = ? AND source = 'skill-migration' AND content = ?
                        LIMIT 1
                        """,
                        (persona_id, content),
                    ).fetchone()
                    if exists:
                        skipped.append(str(obs))
                        continue
                    conn.execute(
                        """
                        INSERT INTO persona_observations
                        (timestamp, timestamp_iso, persona_id, source, content, interpretation, emotion, metadata_json)
                        VALUES (?, ?, ?, 'skill-migration', ?, '从旧 persona-evolution skill 迁移', '', ?)
                        """,
                        (
                            ts,
                            iso,
                            persona_id,
                            content,
                            self._json({"source_file": str(obs)}),
                        ),
                    )
                    conn.commit()
                migrated.append(str(obs))
        for patches in skill_dir.glob("persona_patches_*.json"):
            persona_id = patches.stem.removeprefix("persona_patches_")
            try:
                payload = json.loads(patches.read_text(encoding="utf-8") or "{}")
            except json.JSONDecodeError:
                continue
            for patch in (
                payload.get("patches", []) if isinstance(payload, dict) else []
            ):
                patch_id = str(patch.get("patch_id") or self._next_patch_id(persona_id))
                ts, iso = self._now()
                with self._lock, self._connect() as conn:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO persona_patches
                        (patch_id, timestamp, timestamp_iso, persona_id, status, trigger, changes_json,
                         core_preserved_json, approved_by, applied_at, metadata_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            patch_id,
                            ts,
                            iso,
                            persona_id,
                            str(patch.get("status") or "pending"),
                            str(patch.get("trigger") or ""),
                            self._json(patch.get("changes", []) or []),
                            self._json(patch.get("core_preserved", []) or []),
                            patch.get("approved_by"),
                            patch.get("applied_at"),
                            self._json(
                                {"source_file": str(patches), "legacy_patch": patch}
                            ),
                        ),
                    )
                    conn.commit()
            migrated.append(str(patches))

        for snapshot_file in skill_dir.glob("persona_current_*.md"):
            persona_id = snapshot_file.stem.removeprefix("persona_current_")
            content = snapshot_file.read_text(encoding="utf-8")
            if not content.strip():
                continue
            content_sha = self._sha256_text(content)
            snapshot_id = (
                f"skill-current-{self._safe_id_part(persona_id)}-{content_sha[:12]}"
            )
            with self._lock, self._connect() as conn:
                before = conn.total_changes
                self._insert_snapshot_record(
                    conn,
                    persona_id=persona_id,
                    content=content,
                    label=f"Skill persona_current baseline for {persona_id}",
                    source="skill-migration",
                    source_path=str(snapshot_file),
                    metadata={
                        "source_file": str(snapshot_file),
                        "kind": "persona_current",
                    },
                    snapshot_id=snapshot_id,
                )
                conn.commit()
                if conn.total_changes == before:
                    skipped.append(str(snapshot_file))
                else:
                    migrated.append(str(snapshot_file))

        asset_files = [
            "persona_lingjiu-2.md",
            "persona_intimate_lingjiu-2.md",
            "pending_ops_module_lingjiu-2.md",
            "meta_preamble.md",
            "system_rules.md",
            "nsfw_module.md",
            "roleplay_module.md",
        ]
        for name in asset_files:
            asset = skill_dir / name
            if not asset.exists():
                continue
            content = asset.read_text(encoding="utf-8")
            if not content.strip():
                continue
            stem = asset.stem
            template_id = f"skill-{self._safe_id_part(stem)}"
            ts, iso = self._now()
            metadata = {
                "source": "skill-migration",
                "source_file": str(asset),
                "content_sha256": self._sha256_text(content),
                "kind": "module"
                if stem.endswith("_module") or stem in {"meta_preamble", "system_rules"}
                else "persona_template",
                "deprecated_skill": "persona-evolution",
            }
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO persona_templates
                    (template_id, timestamp, timestamp_iso, name, description, content, variables_json, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(template_id) DO UPDATE SET
                        timestamp=excluded.timestamp,
                        timestamp_iso=excluded.timestamp_iso,
                        name=excluded.name,
                        description=excluded.description,
                        content=excluded.content,
                        variables_json=excluded.variables_json,
                        metadata_json=excluded.metadata_json
                    """,
                    (
                        template_id,
                        ts,
                        iso,
                        stem,
                        "Migrated from deprecated persona-evolution skill.",
                        content,
                        self._json([]),
                        self._json(metadata),
                    ),
                )
                conn.commit()
            migrated.append(str(asset))

        return web.json_response({"ok": True, "migrated": migrated, "skipped": skipped})

    async def api_list_snapshots(self, request: web.Request) -> web.Response:
        persona_id = request.query.get("persona_id", "").strip()
        limit = min(200, max(1, int(request.query.get("limit", 80))))
        where = "WHERE persona_id = ?" if persona_id else ""
        params: list[Any] = [persona_id] if persona_id else []
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM persona_snapshots
                {where}
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return web.json_response(
            {
                "ok": True,
                "items": [
                    self._snapshot_row_to_item(row, include_content=False)
                    for row in rows
                ],
            }
        )

    async def api_get_snapshot(self, request: web.Request) -> web.Response:
        snapshot_id = request.match_info.get("snapshot_id", "").strip()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM persona_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        if not row:
            return web.json_response({"ok": False, "error": "快照不存在"}, status=404)
        return web.json_response(
            {"ok": True, "item": self._snapshot_row_to_item(row, include_content=True)}
        )

    async def api_create_snapshot(self, request: web.Request) -> web.Response:
        data = await request.json()
        persona_id = str(data.get("persona_id", "")).strip()
        if not persona_id:
            return web.json_response(
                {"ok": False, "error": "persona_id 必填"}, status=400
            )
        content = data.get("content")
        source = str(data.get("source", "") or "manual")
        label = str(data.get("label", "") or "")
        source_path = str(data.get("source_path", "") or "")
        if content is None:
            try:
                persona = await self.context.persona_manager.get_persona(persona_id)
            except Exception as exc:
                return web.json_response({"ok": False, "error": str(exc)}, status=404)
            content = persona.system_prompt or ""
            source = "current-persona"
            label = label or f"Current system_prompt snapshot for {persona_id}"
        content = str(content)
        if not content.strip():
            return web.json_response({"ok": False, "error": "快照内容为空"}, status=400)
        with self._lock, self._connect() as conn:
            snapshot_id = self._insert_snapshot_record(
                conn,
                persona_id=persona_id,
                content=content,
                label=label,
                source=source,
                source_path=source_path,
                metadata=data.get("metadata", {}) or {},
                snapshot_id=str(data.get("snapshot_id") or "").strip() or None,
            )
            conn.commit()
        return web.json_response({"ok": True, "snapshot_id": snapshot_id})

    async def api_list_templates(self, _request: web.Request) -> web.Response:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM persona_templates ORDER BY timestamp DESC, id DESC"
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            for key in ("variables_json", "metadata_json"):
                item[key.removesuffix("_json")] = self._loads(item.pop(key), None)
            items.append(item)
        return web.json_response({"ok": True, "items": items})

    async def api_create_template(self, request: web.Request) -> web.Response:
        data = await request.json()
        template_id = (
            str(data.get("template_id", "")).strip() or self._next_template_id()
        )
        name = str(data.get("name", "")).strip()
        content = str(data.get("content", ""))
        if not name or not content.strip():
            return web.json_response(
                {"ok": False, "error": "name 和 content 必填"}, status=400
            )
        ts, iso = self._now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO persona_templates
                (template_id, timestamp, timestamp_iso, name, description, content, variables_json, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    template_id,
                    ts,
                    iso,
                    name,
                    str(data.get("description", "") or ""),
                    content,
                    self._json(data.get("variables", []) or []),
                    self._json(data.get("metadata", {}) or {}),
                ),
            )
            conn.commit()
        return web.json_response({"ok": True, "template_id": template_id})

    def _next_template_id(self) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return f"tpl-{ts}"

    async def api_list_profiles(self, _request: web.Request) -> web.Response:
        personas = await self.context.persona_manager.get_all_personas()
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM persona_profiles").fetchall()
            profiles = {row["persona_id"]: dict(row) for row in rows}
            obs_counts = {
                row["persona_id"]: row["c"]
                for row in conn.execute(
                    "SELECT persona_id, COUNT(*) AS c FROM persona_observations GROUP BY persona_id"
                )
            }
            patch_counts = {
                row["persona_id"]: row["c"]
                for row in conn.execute(
                    "SELECT persona_id, COUNT(*) AS c FROM persona_patches GROUP BY persona_id"
                )
            }
        items = []
        for p in personas:
            profile = profiles.get(p.persona_id, {})
            if profile.get("metadata_json"):
                profile["metadata"] = self._loads(profile.pop("metadata_json"), {})
            items.append(
                {
                    "persona_id": p.persona_id,
                    "system_prompt_len": len(p.system_prompt or ""),
                    "profile": profile,
                    "observation_count": int(obs_counts.get(p.persona_id, 0)),
                    "patch_count": int(patch_counts.get(p.persona_id, 0)),
                }
            )
        return web.json_response({"ok": True, "items": items})

    async def api_upsert_profile(self, request: web.Request) -> web.Response:
        persona_id = request.match_info.get("persona_id", "").strip()
        data = await request.json()
        if not persona_id:
            return web.json_response(
                {"ok": False, "error": "persona_id 必填"}, status=400
            )
        _, iso = self._now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO persona_profiles
                (persona_id, display_name, archetype, notes, template_id, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(persona_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    archetype=excluded.archetype,
                    notes=excluded.notes,
                    template_id=excluded.template_id,
                    updated_at=excluded.updated_at,
                    metadata_json=excluded.metadata_json
                """,
                (
                    persona_id,
                    str(data.get("display_name", "") or ""),
                    str(data.get("archetype", "") or ""),
                    str(data.get("notes", "") or ""),
                    data.get("template_id"),
                    iso,
                    self._json(data.get("metadata", {}) or {}),
                ),
            )
            conn.commit()
        return web.json_response({"ok": True, "persona_id": persona_id})

    def _get_capture_item(self, capture_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM llm_request_captures WHERE id = ?", (capture_id,)
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        for key in (
            "contexts_json",
            "image_urls_json",
            "audio_urls_json",
            "extra_user_content_parts_json",
            "tools_json",
            "tool_calls_result_json",
            "metadata_json",
        ):
            target = "func_tool" if key == "tools_json" else key.removesuffix("_json")
            try:
                item[target] = self._loads(item.pop(key), None)
            except KeyError:
                item[target] = None
        return item
