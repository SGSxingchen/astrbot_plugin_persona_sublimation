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
    "0.5.0",
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

    @web.middleware
    async def _http_middleware(
        self, request: web.Request, handler: Any
    ) -> web.StreamResponse:
        if request.method == "OPTIONS":
            response = web.Response(status=204)
        else:
            try:
                response = await handler(request)
            except web.HTTPException as exc:
                response = web.json_response(
                    {"ok": False, "error": exc.reason or "HTTP error"},
                    status=exc.status,
                )
            except Exception as exc:
                logger.warning(
                    "PersonaSublimation API error at %s %s: %s",
                    request.method,
                    request.path,
                    exc,
                    exc_info=True,
                )
                response = web.json_response(
                    {"ok": False, "error": "internal server error"}, status=500
                )
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = (
            "GET, POST, PATCH, PUT, DELETE, OPTIONS"
        )
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS persona_module_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    persona_id TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    order_index INTEGER NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(persona_id, template_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ps_module_link_persona_order "
                "ON persona_module_links(persona_id, enabled DESC, order_index ASC, id ASC)"
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

    def _table_exists(self, conn: sqlite3.Connection, table_name: str) -> bool:
        return bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()
        )

    def _infer_module_role(self, template_id: str, name: str = "") -> str:
        value = f"{template_id} {name}".lower()
        if "meta_preamble" in value or "preamble" in value:
            return "meta"
        if "system_rules" in value or "system-rule" in value:
            return "system"
        if "nsfw" in value:
            return "nsfw"
        if "roleplay" in value:
            return "roleplay"
        if "pending_ops" in value or "_ops" in value or "ops" in value:
            return "ops"
        if "persona_" in value or "persona-" in value:
            return "persona"
        return "custom"

    def _normalize_template_metadata(
        self,
        *,
        template_id: str,
        name: str,
        content: str,
        metadata: Any,
    ) -> dict[str, Any]:
        """Normalize persona_templates as module assets while keeping table compatibility."""
        normalized = dict(metadata) if isinstance(metadata, dict) else {}
        previous_kind = str(normalized.get("kind") or "").strip()
        if previous_kind and previous_kind != "module":
            normalized.setdefault("legacy_kind", previous_kind)
        normalized["kind"] = "module"
        normalized.setdefault("source", "manual")
        normalized.setdefault("module_id", template_id)
        normalized.setdefault("role", self._infer_module_role(template_id, name))
        normalized["content_sha256"] = self._sha256_text(content)
        return normalized

    def _legacy_patch_stable_id(self, persona_id: str, patch: dict[str, Any]) -> str:
        explicit = str(patch.get("patch_id") or "").strip()
        if explicit:
            return explicit
        payload = self._json(
            {
                "persona_id": persona_id,
                "status": patch.get("status") or "pending",
                "trigger": patch.get("trigger") or "",
                "changes": patch.get("changes", []) or [],
                "core_preserved": patch.get("core_preserved", []) or [],
                "approved_by": patch.get("approved_by"),
                "applied_at": patch.get("applied_at"),
            }
        )
        return f"skill-patch-{self._safe_id_part(persona_id)}-{self._sha256_text(payload)[:12]}"

    def _default_lingjiu_module_specs(self) -> list[tuple[str, str, int]]:
        return [
            ("skill-persona_lingjiu-2", "persona", 10),
            ("skill-meta_preamble", "meta", 20),
            ("skill-system_rules", "system", 30),
            ("skill-nsfw_module", "nsfw", 40),
            ("skill-roleplay_module", "roleplay", 50),
            ("skill-pending_ops_module_lingjiu-2", "ops", 60),
            ("skill-persona_intimate_lingjiu-2", "persona", 70),
        ]

    def _build_data_summary(self) -> dict[str, Any]:
        """Return schema and data health summary without prompt/content bodies."""
        target_tables = [
            "llm_request_captures",
            "captures",
            "persona_observations",
            "persona_patches",
            "persona_templates",
            "persona_snapshots",
            "persona_profiles",
            "persona_module_links",
        ]
        summary: dict[str, Any] = {"db_path": str(self.db_path), "tables": {}}
        with self._lock, self._connect() as conn:
            for table in target_tables:
                if not self._table_exists(conn, table):
                    summary["tables"][table] = {"exists": False}
                    continue
                columns = [
                    {
                        "name": row["name"],
                        "type": row["type"],
                        "notnull": bool(row["notnull"]),
                    }
                    for row in conn.execute(f"PRAGMA table_info({table})")
                ]
                item: dict[str, Any] = {
                    "exists": True,
                    "columns": columns,
                    "count": int(
                        conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()[
                            "c"
                        ]
                    ),
                }
                column_names = {column["name"] for column in columns}
                if "persona_id" in column_names:
                    item["persona_distribution"] = [
                        dict(row)
                        for row in conn.execute(
                            f"""
                            SELECT COALESCE(NULLIF(persona_id, ''), '(empty)') AS persona_id,
                                   COUNT(*) AS count
                            FROM {table}
                            GROUP BY COALESCE(NULLIF(persona_id, ''), '(empty)')
                            ORDER BY count DESC, persona_id
                            LIMIT 100
                            """
                        )
                    ]
                summary["tables"][table] = item

            if self._table_exists(conn, "persona_templates"):
                templates = []
                material_legacy_count = 0
                for row in conn.execute(
                    """
                    SELECT template_id, name, description, content, metadata_json
                    FROM persona_templates
                    ORDER BY template_id
                    """
                ):
                    metadata = self._loads(row["metadata_json"], {})
                    haystack = self._json(metadata) + row["name"] + row["description"]
                    if "素材" in haystack or "material" in haystack.lower():
                        material_legacy_count += 1
                    content = row["content"] or ""
                    templates.append(
                        {
                            "template_id": row["template_id"],
                            "name": row["name"],
                            "content_len": len(content),
                            "content_sha256_prefix": self._sha256_text(content)[:12],
                            "metadata_kind": metadata.get("kind"),
                            "metadata_source": metadata.get("source"),
                            "metadata_role": metadata.get("role"),
                            "module_id": metadata.get("module_id"),
                        }
                    )
                summary["templates"] = templates
                summary["legacy_material_mentions"] = material_legacy_count

            summary["duplicates"] = self._find_duplicate_groups(conn)
        return summary

    def _find_duplicate_groups(
        self, conn: sqlite3.Connection
    ) -> dict[str, list[dict[str, Any]]]:
        duplicates: dict[str, list[dict[str, Any]]] = {
            "observations": [],
            "snapshots": [],
            "patch_candidates": [],
        }
        if self._table_exists(conn, "persona_observations"):
            groups: dict[tuple[str, str, str], list[int]] = {}
            for row in conn.execute(
                "SELECT id, persona_id, source, content FROM persona_observations"
            ):
                sha = self._sha256_text(row["content"] or "")
                key = (row["persona_id"] or "", row["source"] or "", sha)
                groups.setdefault(key, []).append(int(row["id"]))
            duplicates["observations"] = [
                {
                    "persona_id": persona_id,
                    "source": source,
                    "content_sha256_prefix": sha[:12],
                    "count": len(ids),
                    "ids": ids,
                }
                for (persona_id, source, sha), ids in groups.items()
                if len(ids) > 1
            ]

        if self._table_exists(conn, "persona_snapshots"):
            groups: dict[tuple[str, str, str, str], list[int]] = {}
            for row in conn.execute(
                "SELECT id, persona_id, source, source_path, content, content_sha256 FROM persona_snapshots"
            ):
                sha = row["content_sha256"] or self._sha256_text(row["content"] or "")
                key = (
                    row["persona_id"] or "",
                    row["source"] or "",
                    row["source_path"] or "",
                    sha,
                )
                groups.setdefault(key, []).append(int(row["id"]))
            duplicates["snapshots"] = [
                {
                    "persona_id": persona_id,
                    "source": source,
                    "source_path_sha256_prefix": self._sha256_text(source_path)[:12],
                    "content_sha256_prefix": sha[:12],
                    "count": len(ids),
                    "ids": ids,
                }
                for (persona_id, source, source_path, sha), ids in groups.items()
                if len(ids) > 1
            ]

        if self._table_exists(conn, "persona_patches"):
            groups: dict[str, list[str]] = {}
            for row in conn.execute(
                """
                SELECT patch_id, persona_id, trigger, changes_json, core_preserved_json,
                       proposed_prompt, base_prompt
                FROM persona_patches
                """
            ):
                payload = self._json(
                    [
                        row["persona_id"],
                        row["trigger"],
                        row["changes_json"],
                        row["core_preserved_json"],
                        row["proposed_prompt"],
                        row["base_prompt"],
                    ]
                )
                groups.setdefault(self._sha256_text(payload), []).append(
                    row["patch_id"]
                )
            duplicates["patch_candidates"] = [
                {
                    "fingerprint_prefix": sha[:12],
                    "count": len(patch_ids),
                    "patch_ids": patch_ids,
                    "note": "仅提示疑似重复；cleanup 不删除 patch 历史",
                }
                for sha, patch_ids in groups.items()
                if len(patch_ids) > 1
            ]
        return duplicates

    def _cleanup_data_model(self, *, dry_run: bool = True) -> dict[str, Any]:
        """Idempotently normalize data-model metadata and safe duplicates."""
        result: dict[str, Any] = {
            "dry_run": dry_run,
            "normalized_templates": [],
            "inserted_module_links": [],
            "updated_module_link_roles": [],
            "deleted_observation_ids": [],
            "deleted_snapshot_ids": [],
            "patch_history_deleted": 0,
        }
        with self._lock, self._connect() as conn:
            if self._table_exists(conn, "persona_templates"):
                for row in conn.execute(
                    "SELECT template_id, name, content, metadata_json FROM persona_templates"
                ).fetchall():
                    metadata = self._loads(row["metadata_json"], {})
                    normalized = self._normalize_template_metadata(
                        template_id=row["template_id"],
                        name=row["name"],
                        content=row["content"] or "",
                        metadata=metadata,
                    )
                    if normalized != metadata:
                        result["normalized_templates"].append(row["template_id"])
                        if not dry_run:
                            conn.execute(
                                """
                                UPDATE persona_templates
                                SET metadata_json = ?
                                WHERE template_id = ?
                                """,
                                (self._json(normalized), row["template_id"]),
                            )

            if self._table_exists(conn, "persona_module_links") and self._table_exists(
                conn, "persona_templates"
            ):
                _, iso = self._now()
                for (
                    template_id,
                    role,
                    order_index,
                ) in self._default_lingjiu_module_specs():
                    exists = conn.execute(
                        "SELECT 1 FROM persona_templates WHERE template_id = ?",
                        (template_id,),
                    ).fetchone()
                    if not exists:
                        continue
                    link = conn.execute(
                        """
                        SELECT id, role FROM persona_module_links
                        WHERE persona_id = 'lingjiu-2' AND template_id = ?
                        """,
                        (template_id,),
                    ).fetchone()
                    if not link:
                        result["inserted_module_links"].append(template_id)
                        if not dry_run:
                            conn.execute(
                                """
                                INSERT INTO persona_module_links
                                (persona_id, template_id, role, enabled, order_index, notes,
                                 created_at, updated_at, metadata_json)
                                VALUES ('lingjiu-2', ?, ?, 1, ?,
                                        '默认模块装配关系；不会自动写入 AstrBot persona',
                                        ?, ?, ?)
                                """,
                                (
                                    template_id,
                                    role,
                                    order_index,
                                    iso,
                                    iso,
                                    self._json({"source": "data-cleanup"}),
                                ),
                            )
                    elif not str(link["role"] or "").strip():
                        result["updated_module_link_roles"].append(int(link["id"]))
                        if not dry_run:
                            conn.execute(
                                """
                                UPDATE persona_module_links
                                SET role = ?, updated_at = ?
                                WHERE id = ?
                                """,
                                (role, iso, int(link["id"])),
                            )

            duplicate_groups = self._find_duplicate_groups(conn)
            for group in duplicate_groups["observations"]:
                delete_ids = sorted(group["ids"])[1:]
                result["deleted_observation_ids"].extend(delete_ids)
                if delete_ids and not dry_run:
                    conn.executemany(
                        "DELETE FROM persona_observations WHERE id = ?",
                        [(item_id,) for item_id in delete_ids],
                    )
            for group in duplicate_groups["snapshots"]:
                delete_ids = sorted(group["ids"])[1:]
                result["deleted_snapshot_ids"].extend(delete_ids)
                if delete_ids and not dry_run:
                    conn.executemany(
                        "DELETE FROM persona_snapshots WHERE id = ?",
                        [(item_id,) for item_id in delete_ids],
                    )

            if not dry_run:
                conn.commit()
        return result

    async def _read_json(self, request: web.Request) -> dict[str, Any]:
        if not request.can_read_body:
            return {}
        try:
            data = await request.json()
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(reason="invalid JSON body") from exc
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(reason="JSON body must be an object")
        return data

    def _explicit_true(self, data: dict[str, Any], *keys: str) -> bool:
        """Return True only when one of the named JSON fields is explicitly true."""
        for key in keys:
            if key not in data:
                continue
            value = data[key]
            if value is True:
                return True
            if isinstance(value, str) and value.strip().lower() in {
                "true",
                "1",
                "yes",
                "on",
            }:
                return True
            if isinstance(value, int | float) and value == 1:
                return True
        return False

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

    def _clip_text(self, value: Any, limit: int = 240) -> str:
        text = str(value or "").replace("\r\n", "\n").strip()
        return text if len(text) <= limit else text[:limit] + "…"

    def _patch_safe_summary(
        self, item: dict[str, Any], include_content: bool = False
    ) -> dict[str, Any]:
        diff = str(item.get("diff") or "")
        summary = {
            "patch_id": item.get("patch_id"),
            "persona_id": item.get("persona_id"),
            "status": item.get("status"),
            "trigger": item.get("trigger") or "",
            "timestamp_iso": item.get("timestamp_iso"),
            "approved_by": item.get("approved_by"),
            "applied_at": item.get("applied_at"),
            "changes": item.get("changes") or [],
            "core_preserved": item.get("core_preserved") or [],
            "metadata": item.get("metadata") or {},
            "base_prompt_len": len(item.get("base_prompt") or ""),
            "proposed_prompt_len": len(item.get("proposed_prompt") or ""),
            "diff_len": len(diff),
            "diff_preview": self._clip_text(diff, 1200),
        }
        if include_content:
            summary["base_prompt"] = item.get("base_prompt") or ""
            summary["proposed_prompt"] = item.get("proposed_prompt") or ""
            summary["diff"] = diff
        return summary

    def _template_is_sensitive(self, item: dict[str, Any]) -> bool:
        metadata = item.get("metadata") or {}
        kind = str(metadata.get("kind") or "").lower()
        haystack = " ".join(
            [
                str(item.get("template_id") or ""),
                str(item.get("name") or ""),
                str(item.get("description") or ""),
                kind,
            ]
        ).lower()
        return any(
            marker in haystack
            for marker in ("ops", "operation", "nsfw", "secret", "sensitive", "rule")
        )

    def _template_safe_summary(
        self, item: dict[str, Any], include_content: bool = False
    ) -> dict[str, Any]:
        content = str(item.get("content") or "")
        sensitive = self._template_is_sensitive(item)
        summary = {
            "template_id": item.get("template_id"),
            "module_id": item.get("template_id"),
            "name": item.get("name") or "",
            "description": item.get("description") or "",
            "timestamp_iso": item.get("timestamp_iso"),
            "variables": item.get("variables") or [],
            "metadata": item.get("metadata") or {},
            "content_len": len(content),
            "content_preview": self._clip_text(content, 300)
            if include_content and not sensitive
            else "",
            "sensitive": sensitive,
            "content_hidden": not include_content or sensitive,
        }
        if include_content and not sensitive:
            summary["content"] = content
        elif include_content and sensitive:
            summary["message"] = (
                "该模块被判定为敏感模块，LLM 工具默认不展开正文；请在前端由人类确认查看。"
            )
        return summary

    def _module_link_row_to_item(
        self, row: sqlite3.Row, include_content: bool = False
    ) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item.get("enabled"))
        item["metadata"] = self._loads(item.pop("metadata_json", None), {})
        template = {
            "template_id": item.pop("template_id"),
            "name": item.pop("template_name", "") or "",
            "description": item.pop("template_description", "") or "",
            "timestamp_iso": item.pop("template_timestamp_iso", "") or "",
            "content": item.pop("template_content", "") or "",
            "variables": self._loads(item.pop("template_variables_json", None), []),
            "metadata": self._loads(item.pop("template_metadata_json", None), {}),
        }
        item["template_id"] = template["template_id"]
        item["module_id"] = template["template_id"]
        item["template"] = self._template_safe_summary(template, include_content)
        if include_content:
            item["template"]["_raw_content_for_patch"] = template["content"]
        return item

    def _list_module_links(
        self, persona_id: str, include_content: bool = False, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        where = "WHERE l.persona_id = ?"
        params: list[Any] = [persona_id]
        if enabled_only:
            where += " AND l.enabled = 1"
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT l.*, t.name AS template_name, t.description AS template_description,
                       t.timestamp_iso AS template_timestamp_iso,
                       t.content AS template_content,
                       t.variables_json AS template_variables_json,
                       t.metadata_json AS template_metadata_json
                FROM persona_module_links l
                JOIN persona_templates t ON t.template_id = l.template_id
                {where}
                ORDER BY l.enabled DESC, l.order_index ASC, l.id ASC
                """,
                params,
            ).fetchall()
        return [self._module_link_row_to_item(row, include_content) for row in rows]

    async def _create_patch_from_module_links(
        self, persona_id: str, notes: str = "", trigger: str = ""
    ) -> dict[str, Any]:
        persona_id = str(persona_id or "").strip()
        if not persona_id:
            raise web.HTTPBadRequest(reason="persona_id 必填")
        links = self._list_module_links(
            persona_id, include_content=True, enabled_only=True
        )
        if not links:
            raise web.HTTPBadRequest(reason="当前 persona 没有关联且启用的模块")
        parts = []
        module_refs = []
        for link in links:
            template = link["template"]
            content = str(
                template.get("_raw_content_for_patch") or template.get("content") or ""
            )
            if not content.strip():
                continue
            parts.append(
                "\n".join(
                    [
                        f"<!-- module:{template['template_id']} role:{link.get('role') or 'custom'} -->",
                        content,
                    ]
                )
            )
            module_refs.append(
                {
                    "link_id": link["id"],
                    "template_id": template["template_id"],
                    "role": link.get("role") or "",
                    "order_index": link.get("order_index", 0),
                }
            )
        if not parts:
            raise web.HTTPBadRequest(reason="启用模块没有可组合的正文")
        return await self._create_patch_record_from_data(
            {
                "persona_id": persona_id,
                "proposed_prompt": "\n\n".join(parts).strip(),
                "trigger": trigger or "由当前模块清单起草调整",
                "changes": [
                    {
                        "aspect": "module_links",
                        "after": [ref["template_id"] for ref in module_refs],
                        "reason": "由 persona 关联模块清单组合生成，等待人类审核",
                    }
                ],
                "notes": notes,
                "metadata": {"source": "module_links", "module_links": module_refs},
            }
        )

    def _make_prompt_diff(
        self, persona_id: str, base_prompt: str, proposed_prompt: str
    ) -> str:
        return "\n".join(
            difflib.unified_diff(
                str(base_prompt).splitlines(),
                str(proposed_prompt).splitlines(),
                fromfile=f"{persona_id}:current",
                tofile=f"{persona_id}:proposed",
                lineterm="",
            )
        )

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
        app = web.Application(middlewares=[self._http_middleware])
        app.router.add_get("/", self.serve_index)
        app.router.add_get("/api/captures", self.api_list_captures)
        app.router.add_get(r"/api/captures/{capture_id:\d+}", self.api_get_capture)
        app.router.add_get("/api/sessions", self.api_list_sessions)
        app.router.add_get("/api/debug/data-summary", self.api_debug_data_summary)
        app.router.add_post("/api/debug/cleanup", self.api_debug_cleanup)
        app.router.add_get("/api/personas", self.api_list_personas)
        app.router.add_get("/api/personas/{persona_id}", self.api_get_persona)
        app.router.add_get(
            "/api/personas/{persona_id}/modules", self.api_list_persona_modules
        )
        app.router.add_post(
            "/api/personas/{persona_id}/modules", self.api_link_persona_module
        )
        app.router.add_patch(
            r"/api/personas/{persona_id}/modules/{link_id:\d+}",
            self.api_update_persona_module_link,
        )
        app.router.add_delete(
            r"/api/personas/{persona_id}/modules/{link_id:\d+}",
            self.api_unlink_persona_module,
        )
        app.router.add_post(
            "/api/personas/{persona_id}/modules/patch",
            self.api_create_patch_from_persona_modules,
        )
        app.router.add_get("/api/observations", self.api_list_observations)
        app.router.add_post("/api/observations", self.api_create_observation)
        app.router.add_get(
            r"/api/observations/{observation_id:\d+}", self.api_get_observation
        )
        app.router.add_patch(
            r"/api/observations/{observation_id:\d+}", self.api_update_observation
        )
        app.router.add_post(
            r"/api/observations/{observation_id:\d+}", self.api_update_observation
        )
        app.router.add_delete(
            r"/api/observations/{observation_id:\d+}", self.api_delete_observation
        )
        app.router.add_get("/api/patches", self.api_list_patches)
        app.router.add_post("/api/patches", self.api_create_patch)
        app.router.add_get("/api/patches/{patch_id}", self.api_get_patch)
        app.router.add_post("/api/patches/{patch_id}", self.api_update_patch)
        app.router.add_patch("/api/patches/{patch_id}", self.api_update_patch)
        app.router.add_delete("/api/patches/{patch_id}", self.api_delete_patch)
        app.router.add_post("/api/patches/{patch_id}/approve", self.api_approve_patch)
        app.router.add_post("/api/patches/{patch_id}/apply", self.api_apply_patch)
        app.router.add_post("/api/migrate-skill", self.api_migrate_skill_files)
        app.router.add_get("/api/templates", self.api_list_templates)
        app.router.add_post("/api/templates", self.api_create_template)
        app.router.add_get("/api/templates/{template_id}", self.api_get_template)
        app.router.add_patch("/api/templates/{template_id}", self.api_update_template)
        app.router.add_post("/api/templates/{template_id}", self.api_update_template)
        app.router.add_delete("/api/templates/{template_id}", self.api_delete_template)
        app.router.add_post(
            "/api/templates/{template_id}/patch", self.api_create_patch_from_template
        )
        app.router.add_get("/api/snapshots", self.api_list_snapshots)
        app.router.add_post("/api/snapshots", self.api_create_snapshot)
        app.router.add_get("/api/snapshots/{snapshot_id}", self.api_get_snapshot)
        app.router.add_patch("/api/snapshots/{snapshot_id}", self.api_update_snapshot)
        app.router.add_post("/api/snapshots/{snapshot_id}", self.api_update_snapshot)
        app.router.add_delete("/api/snapshots/{snapshot_id}", self.api_delete_snapshot)
        app.router.add_post(
            "/api/snapshots/{snapshot_id}/patch", self.api_create_patch_from_snapshot
        )
        app.router.add_get("/api/profiles", self.api_list_profiles)
        app.router.add_get("/api/profiles/{persona_id}", self.api_get_profile)
        app.router.add_post("/api/profiles/{persona_id}", self.api_upsert_profile)
        app.router.add_patch("/api/profiles/{persona_id}", self.api_upsert_profile)
        app.router.add_delete("/api/profiles/{persona_id}", self.api_delete_profile)
        app.router.add_route("OPTIONS", "/{tail:.*}", self.api_options)

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

    async def api_options(self, _request: web.Request) -> web.Response:
        return web.Response(status=204)

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

    async def api_debug_data_summary(self, _request: web.Request) -> web.Response:
        """Safe data summary: counts, ids, lengths and sha prefixes only."""
        return web.json_response({"ok": True, "summary": self._build_data_summary()})

    async def api_debug_cleanup(self, request: web.Request) -> web.Response:
        data = await self._read_json(request)
        dry_run = not self._explicit_true(data, "apply", "commit", "write")
        return web.json_response(
            {"ok": True, "result": self._cleanup_data_model(dry_run=dry_run)}
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

    async def api_list_persona_modules(self, request: web.Request) -> web.Response:
        persona_id = request.match_info.get("persona_id", "").strip()
        if not persona_id:
            return web.json_response(
                {"ok": False, "error": "persona_id 必填"}, status=400
            )
        return web.json_response(
            {"ok": True, "items": self._list_module_links(persona_id)}
        )

    async def api_link_persona_module(self, request: web.Request) -> web.Response:
        persona_id = request.match_info.get("persona_id", "").strip()
        data = await self._read_json(request)
        template_id = str(
            data.get("module_id") or data.get("template_id") or ""
        ).strip()
        if not persona_id or not template_id:
            return web.json_response(
                {"ok": False, "error": "persona_id 和 module_id/template_id 必填"},
                status=400,
            )
        role = str(data.get("role") or data.get("kind") or "custom").strip()
        enabled = 1 if bool(data.get("enabled", True)) else 0
        order_index = int(data.get("order_index", 0) or 0)
        notes = str(data.get("notes", "") or "")
        _, iso = self._now()
        with self._lock, self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM persona_templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()
            if not exists:
                return web.json_response(
                    {"ok": False, "error": "模块不存在"}, status=404
                )
            conn.execute(
                """
                INSERT INTO persona_module_links
                (persona_id, template_id, role, enabled, order_index, notes, created_at, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(persona_id, template_id) DO UPDATE SET
                    role=excluded.role,
                    enabled=excluded.enabled,
                    order_index=excluded.order_index,
                    notes=excluded.notes,
                    updated_at=excluded.updated_at
                """,
                (
                    persona_id,
                    template_id,
                    role,
                    enabled,
                    order_index,
                    notes,
                    iso,
                    iso,
                    self._json(data.get("metadata", {}) or {}),
                ),
            )
            conn.commit()
            row = conn.execute(
                """
                SELECT l.*, t.name AS template_name, t.description AS template_description,
                       t.timestamp_iso AS template_timestamp_iso, t.content AS template_content,
                       t.variables_json AS template_variables_json, t.metadata_json AS template_metadata_json
                FROM persona_module_links l
                JOIN persona_templates t ON t.template_id = l.template_id
                WHERE l.persona_id = ? AND l.template_id = ?
                """,
                (persona_id, template_id),
            ).fetchone()
        return web.json_response(
            {"ok": True, "item": self._module_link_row_to_item(row)}
        )

    async def api_update_persona_module_link(
        self, request: web.Request
    ) -> web.Response:
        persona_id = request.match_info.get("persona_id", "").strip()
        link_id = int(request.match_info["link_id"])
        data = await self._read_json(request)
        _, iso = self._now()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM persona_module_links WHERE id = ? AND persona_id = ?",
                (link_id, persona_id),
            ).fetchone()
            if not row:
                return web.json_response(
                    {"ok": False, "error": "模块关联不存在"}, status=404
                )
            conn.execute(
                """
                UPDATE persona_module_links
                SET role = ?, enabled = ?, order_index = ?, notes = ?, updated_at = ?
                WHERE id = ? AND persona_id = ?
                """,
                (
                    str(data.get("role", row["role"]) or "custom"),
                    1 if bool(data.get("enabled", bool(row["enabled"]))) else 0,
                    int(data.get("order_index", row["order_index"]) or 0),
                    str(data.get("notes", row["notes"]) or ""),
                    iso,
                    link_id,
                    persona_id,
                ),
            )
            conn.commit()
        return web.json_response(
            {"ok": True, "items": self._list_module_links(persona_id)}
        )

    async def api_unlink_persona_module(self, request: web.Request) -> web.Response:
        persona_id = request.match_info.get("persona_id", "").strip()
        link_id = int(request.match_info["link_id"])
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM persona_module_links WHERE id = ? AND persona_id = ?",
                (link_id, persona_id),
            )
            conn.commit()
        if cur.rowcount <= 0:
            return web.json_response(
                {"ok": False, "error": "模块关联不存在"}, status=404
            )
        return web.json_response({"ok": True, "deleted_link_id": link_id})

    async def api_create_patch_from_persona_modules(
        self, request: web.Request
    ) -> web.Response:
        persona_id = request.match_info.get("persona_id", "").strip()
        data = await self._read_json(request)
        try:
            result = await self._create_patch_from_module_links(
                persona_id,
                notes=str(data.get("notes", "") or ""),
                trigger=str(data.get("trigger", "") or ""),
            )
        except web.HTTPException as exc:
            return web.json_response(
                {"ok": False, "error": exc.reason}, status=exc.status
            )
        return web.json_response({"ok": True, **result})

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

    def _observation_row_to_item(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["metadata"] = self._loads(item.pop("metadata_json"), {})
        return item

    async def api_create_observation(self, request: web.Request) -> web.Response:
        data = await self._read_json(request)
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

    async def api_get_observation(self, request: web.Request) -> web.Response:
        observation_id = int(request.match_info["observation_id"])
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM persona_observations WHERE id = ?",
                (observation_id,),
            ).fetchone()
        if not row:
            return web.json_response(
                {"ok": False, "error": "观察记录不存在"}, status=404
            )
        return web.json_response(
            {"ok": True, "item": self._observation_row_to_item(row)}
        )

    async def api_update_observation(self, request: web.Request) -> web.Response:
        observation_id = int(request.match_info["observation_id"])
        data = await self._read_json(request)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM persona_observations WHERE id = ?",
                (observation_id,),
            ).fetchone()
            if not row:
                return web.json_response(
                    {"ok": False, "error": "观察记录不存在"}, status=404
                )
            item = self._observation_row_to_item(row)
            expected_persona_id = str(data.get("persona_id", "")).strip()
            if expected_persona_id and expected_persona_id != item["persona_id"]:
                return web.json_response(
                    {"ok": False, "error": "persona_id 与观察记录不匹配"},
                    status=409,
                )
            metadata = (
                item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            )
            incoming_metadata = data.get("metadata")
            if isinstance(incoming_metadata, dict):
                metadata.update(incoming_metadata)
            conn.execute(
                """
                UPDATE persona_observations
                SET source = ?, content = ?, interpretation = ?, emotion = ?,
                    capture_id = ?, metadata_json = ?
                WHERE id = ?
                """,
                (
                    str(data.get("source", item.get("source") or "") or ""),
                    str(data.get("content", item.get("content") or "") or ""),
                    str(
                        data.get("interpretation", item.get("interpretation") or "")
                        or ""
                    ),
                    str(data.get("emotion", item.get("emotion") or "") or ""),
                    data.get("capture_id", item.get("capture_id")),
                    self._json(metadata),
                    observation_id,
                ),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM persona_observations WHERE id = ?",
                (observation_id,),
            ).fetchone()
        return web.json_response(
            {"ok": True, "item": self._observation_row_to_item(updated)}
        )

    async def api_delete_observation(self, request: web.Request) -> web.Response:
        observation_id = int(request.match_info["observation_id"])
        persona_id = request.query.get("persona_id", "").strip()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT persona_id FROM persona_observations WHERE id = ?",
                (observation_id,),
            ).fetchone()
            if not row:
                return web.json_response(
                    {"ok": False, "error": "观察记录不存在"}, status=404
                )
            if persona_id and persona_id != row["persona_id"]:
                return web.json_response(
                    {"ok": False, "error": "persona_id 与观察记录不匹配"},
                    status=409,
                )
            conn.execute(
                "DELETE FROM persona_observations WHERE id = ?",
                (observation_id,),
            )
            conn.commit()
        return web.json_response(
            {"ok": True, "deleted_id": observation_id, "persona_id": row["persona_id"]}
        )

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
        data = await self._read_json(request)
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
        diff = self._make_prompt_diff(
            persona_id, str(base_prompt), str(proposed_prompt)
        )
        metadata = data.get("metadata", {}) or {}
        if not isinstance(metadata, dict):
            metadata = {"raw_metadata": metadata}
        notes = str(data.get("notes", "") or "").strip()
        if notes:
            metadata["notes"] = notes
        mode = (
            "prompt" if data.get("proposed_prompt") is not None else "structured-draft"
        )
        metadata.setdefault("mode", mode)
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
                    self._json(metadata),
                ),
            )
            conn.commit()
        return web.json_response({"ok": True, "patch_id": patch_id, "diff": diff})

    async def _create_patch_record_from_data(
        self, data: dict[str, Any]
    ) -> dict[str, Any]:
        persona_id = str(data.get("persona_id", "")).strip()
        proposed_prompt = data.get("proposed_prompt")
        if not persona_id:
            raise web.HTTPBadRequest(reason="persona_id 必填")
        try:
            persona = await self.context.persona_manager.get_persona(persona_id)
        except Exception as exc:
            raise web.HTTPNotFound(reason=str(exc)) from exc
        base_prompt = data.get("base_prompt")
        if base_prompt is None:
            base_prompt = persona.system_prompt or ""
        if proposed_prompt is None:
            proposed_prompt = base_prompt
        patch_id = str(data.get("patch_id") or self._next_patch_id(persona_id))
        diff = self._make_prompt_diff(
            persona_id, str(base_prompt), str(proposed_prompt)
        )
        metadata = data.get("metadata", {}) or {}
        if not isinstance(metadata, dict):
            metadata = {"raw_metadata": metadata}
        notes = str(data.get("notes", "") or "").strip()
        if notes:
            metadata["notes"] = notes
        mode = (
            "prompt" if data.get("proposed_prompt") is not None else "structured-draft"
        )
        metadata.setdefault("mode", mode)
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
                    self._json(metadata),
                ),
            )
            conn.commit()
        return {"patch_id": patch_id, "diff": diff}

    async def api_get_patch(self, request: web.Request) -> web.Response:
        patch_id = request.match_info.get("patch_id", "").strip()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM persona_patches WHERE patch_id = ?", (patch_id,)
            ).fetchone()
        if not row:
            return web.json_response({"ok": False, "error": "补丁不存在"}, status=404)
        return web.json_response({"ok": True, "item": self._patch_row_to_item(row)})

    async def api_update_patch(self, request: web.Request) -> web.Response:
        patch_id = request.match_info.get("patch_id", "").strip()
        data = await self._read_json(request)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM persona_patches WHERE patch_id = ?", (patch_id,)
            ).fetchone()
            if not row:
                return web.json_response(
                    {"ok": False, "error": "补丁不存在"}, status=404
                )
            item = self._patch_row_to_item(row)
            if item["status"] != "pending":
                return web.json_response(
                    {"ok": False, "error": "只能修改 pending 状态的补丁草案"},
                    status=400,
                )

            trigger = (
                str(data["trigger"])
                if "trigger" in data
                else str(item.get("trigger", "") or "")
            )
            changes = data.get("changes", item.get("changes") or [])
            core_preserved = data.get(
                "core_preserved", item.get("core_preserved") or []
            )
            base_prompt = str(data.get("base_prompt", item.get("base_prompt") or ""))
            proposed_prompt = str(
                data.get("proposed_prompt", item.get("proposed_prompt") or "")
            )
            metadata = (
                item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            )
            incoming_metadata = data.get("metadata")
            if isinstance(incoming_metadata, dict):
                metadata.update(incoming_metadata)
            notes = str(data.get("notes", "") or "").strip()
            if notes:
                metadata["notes"] = notes
            diff = self._make_prompt_diff(
                str(item["persona_id"]), base_prompt, proposed_prompt
            )
            conn.execute(
                """
                UPDATE persona_patches
                SET trigger = ?, changes_json = ?, core_preserved_json = ?,
                    proposed_prompt = ?, base_prompt = ?, diff = ?, metadata_json = ?
                WHERE patch_id = ? AND status = 'pending'
                """,
                (
                    trigger,
                    self._json(changes or []),
                    self._json(core_preserved or []),
                    proposed_prompt,
                    base_prompt,
                    diff,
                    self._json(metadata),
                    patch_id,
                ),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM persona_patches WHERE patch_id = ?", (patch_id,)
            ).fetchone()
        return web.json_response(
            {"ok": True, "item": self._patch_row_to_item(updated), "diff": diff}
        )

    async def api_delete_patch(self, request: web.Request) -> web.Response:
        patch_id = request.match_info.get("patch_id", "").strip()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT patch_id, persona_id, status FROM persona_patches WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
            if not row:
                return web.json_response(
                    {"ok": False, "error": "补丁不存在"}, status=404
                )
            if row["status"] != "pending":
                return web.json_response(
                    {
                        "ok": False,
                        "error": "只允许删除 pending 补丁；已审批/已应用补丁不会被删除",
                    },
                    status=400,
                )
            conn.execute("DELETE FROM persona_patches WHERE patch_id = ?", (patch_id,))
            conn.commit()
        return web.json_response(
            {"ok": True, "deleted_patch_id": patch_id, "persona_id": row["persona_id"]}
        )

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
        data = await self._read_json(request)
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
        data = await self._read_json(request)
        auto_approve = self._explicit_true(data, "auto_approve", "approve")
        approved_by = str(data.get("approved_by", "") or "frontend-human")
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM persona_patches WHERE patch_id = ?", (patch_id,)
            ).fetchone()
        if not row:
            return web.json_response({"ok": False, "error": "补丁不存在"}, status=404)
        item = self._patch_row_to_item(row)
        if item["status"] == "pending" and auto_approve:
            item["status"] = "approved"
            item["approved_by"] = approved_by
        elif item["status"] != "approved":
            return web.json_response(
                {
                    "ok": False,
                    "error": "补丁必须先 approve，或在 apply 请求中显式传 auto_approve=true",
                },
                status=400,
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
                """
                UPDATE persona_patches
                SET status = 'applied',
                    approved_by = COALESCE(NULLIF(approved_by, ''), ?),
                    applied_at = ?
                WHERE patch_id = ?
                """,
                (approved_by, iso, patch_id),
            )
            conn.commit()
        return web.json_response(
            {
                "ok": True,
                "applied_at": iso,
                "auto_approved": auto_approve and row["status"] == "pending",
                "approved_by": approved_by,
            }
        )

    async def api_migrate_skill_files(self, request: web.Request) -> web.Response:
        data = await self._read_json(request)
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
                if not isinstance(patch, dict):
                    continue
                patch_id = self._legacy_patch_stable_id(persona_id, patch)
                ts, iso = self._now()
                with self._lock, self._connect() as conn:
                    before = conn.total_changes
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
                    if conn.total_changes == before:
                        skipped.append(f"{patches}#{patch_id}")
                    else:
                        migrated.append(f"{patches}#{patch_id}")

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
            metadata = self._normalize_template_metadata(
                template_id=template_id,
                name=stem,
                content=content,
                metadata={
                    "source": "skill-migration",
                    "source_file": str(asset),
                    "content_sha256": self._sha256_text(content),
                    "role": self._infer_module_role(template_id, stem),
                    "deprecated_skill": "persona-evolution",
                },
            )
            with self._lock, self._connect() as conn:
                before = conn.total_changes
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
                default_roles = {
                    "persona_lingjiu-2.md": ("persona", 10),
                    "meta_preamble.md": ("meta", 20),
                    "system_rules.md": ("system", 30),
                    "nsfw_module.md": ("nsfw", 40),
                    "roleplay_module.md": ("roleplay", 50),
                    "pending_ops_module_lingjiu-2.md": ("ops", 60),
                    "persona_intimate_lingjiu-2.md": ("persona", 70),
                }
                if name in default_roles:
                    role, order_index = default_roles[name]
                    conn.execute(
                        """
                        INSERT INTO persona_module_links
                        (persona_id, template_id, role, enabled, order_index, notes, created_at, updated_at, metadata_json)
                        VALUES ('lingjiu-2', ?, ?, 1, ?, '由旧 skill 迁移时建立的默认模块关联；不会自动写入 persona', ?, ?, ?)
                        ON CONFLICT(persona_id, template_id) DO NOTHING
                        """,
                        (
                            template_id,
                            role,
                            order_index,
                            iso,
                            iso,
                            self._json({"source": "skill-migration"}),
                        ),
                    )
                conn.commit()
                if conn.total_changes == before:
                    skipped.append(str(asset))
                else:
                    migrated.append(str(asset))

        cleanup = self._cleanup_data_model(dry_run=False)
        return web.json_response(
            {"ok": True, "migrated": migrated, "skipped": skipped, "cleanup": cleanup}
        )

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
        data = await self._read_json(request)
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

    async def api_update_snapshot(self, request: web.Request) -> web.Response:
        snapshot_id = request.match_info.get("snapshot_id", "").strip()
        data = await self._read_json(request)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM persona_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if not row:
                return web.json_response(
                    {"ok": False, "error": "快照不存在"}, status=404
                )
            item = self._snapshot_row_to_item(row, include_content=True)
            expected_persona_id = str(data.get("persona_id", "")).strip()
            if expected_persona_id and expected_persona_id != item["persona_id"]:
                return web.json_response(
                    {"ok": False, "error": "persona_id 与快照不匹配"}, status=409
                )
            metadata = (
                item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            )
            incoming_metadata = data.get("metadata")
            if isinstance(incoming_metadata, dict):
                metadata.update(incoming_metadata)
            conn.execute(
                """
                UPDATE persona_snapshots
                SET label = ?, metadata_json = ?
                WHERE snapshot_id = ?
                """,
                (
                    str(data.get("label", item.get("label") or "") or ""),
                    self._json(metadata),
                    snapshot_id,
                ),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM persona_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        return web.json_response(
            {"ok": True, "item": self._snapshot_row_to_item(updated, True)}
        )

    async def api_delete_snapshot(self, request: web.Request) -> web.Response:
        snapshot_id = request.match_info.get("snapshot_id", "").strip()
        persona_id = request.query.get("persona_id", "").strip()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT persona_id FROM persona_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if not row:
                return web.json_response(
                    {"ok": False, "error": "快照不存在"}, status=404
                )
            if persona_id and persona_id != row["persona_id"]:
                return web.json_response(
                    {"ok": False, "error": "persona_id 与快照不匹配"}, status=409
                )
            conn.execute(
                "DELETE FROM persona_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            )
            conn.commit()
        return web.json_response(
            {
                "ok": True,
                "deleted_snapshot_id": snapshot_id,
                "persona_id": row["persona_id"],
            }
        )

    async def api_create_patch_from_snapshot(
        self, request: web.Request
    ) -> web.Response:
        snapshot_id = request.match_info.get("snapshot_id", "").strip()
        data = await self._read_json(request)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM persona_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        if not row:
            return web.json_response({"ok": False, "error": "快照不存在"}, status=404)
        snapshot = self._snapshot_row_to_item(row, include_content=True)
        persona_id = str(data.get("persona_id") or snapshot["persona_id"]).strip()
        result = await self._create_patch_record_from_data(
            {
                "persona_id": persona_id,
                "proposed_prompt": snapshot.get("content") or "",
                "trigger": data.get(
                    "trigger",
                    f"从快照生成补丁草案：{snapshot.get('label') or snapshot_id}",
                ),
                "changes": data.get(
                    "changes",
                    [
                        {
                            "aspect": "snapshot",
                            "after": snapshot_id,
                            "reason": "人工从快照生成补丁草案",
                        }
                    ],
                ),
                "metadata": {
                    "source": "snapshot",
                    "snapshot_id": snapshot_id,
                    **(
                        data.get("metadata", {})
                        if isinstance(data.get("metadata"), dict)
                        else {}
                    ),
                },
            }
        )
        return web.json_response({"ok": True, **result})

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

    async def api_get_template(self, request: web.Request) -> web.Response:
        template_id = request.match_info.get("template_id", "").strip()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM persona_templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()
        if not row:
            return web.json_response({"ok": False, "error": "模块不存在"}, status=404)
        item = dict(row)
        for key in ("variables_json", "metadata_json"):
            item[key.removesuffix("_json")] = self._loads(item.pop(key), None)
        return web.json_response({"ok": True, "item": item})

    async def api_create_template(self, request: web.Request) -> web.Response:
        data = await self._read_json(request)
        template_id = (
            str(data.get("module_id") or data.get("template_id") or "").strip()
            or self._next_template_id()
        )
        name = str(data.get("name", "")).strip()
        content = str(data.get("content", ""))
        if not name or not content.strip():
            return web.json_response(
                {"ok": False, "error": "name 和 content 必填"}, status=400
            )
        ts, iso = self._now()
        metadata = self._normalize_template_metadata(
            template_id=template_id,
            name=name,
            content=content,
            metadata=data.get("metadata", {}) or {},
        )
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
                    self._json(metadata),
                ),
            )
            conn.commit()
        return web.json_response(
            {"ok": True, "template_id": template_id, "module_id": template_id}
        )

    async def api_update_template(self, request: web.Request) -> web.Response:
        template_id = request.match_info.get("template_id", "").strip()
        data = await self._read_json(request)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM persona_templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()
            if not row:
                return web.json_response(
                    {"ok": False, "error": "模块不存在"}, status=404
                )
            item = dict(row)
            item["variables"] = self._loads(item.pop("variables_json"), [])
            item["metadata"] = self._loads(item.pop("metadata_json"), {})
            metadata = (
                item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            )
            incoming_metadata = data.get("metadata")
            if isinstance(incoming_metadata, dict):
                metadata.update(incoming_metadata)
            variables = data.get("variables", item.get("variables") or [])
            name = str(data.get("name", item.get("name") or "") or "")
            content = str(data.get("content", item.get("content") or "") or "")
            metadata = self._normalize_template_metadata(
                template_id=template_id,
                name=name,
                content=content,
                metadata=metadata,
            )
            _, iso = self._now()
            conn.execute(
                """
                UPDATE persona_templates
                SET timestamp_iso = ?, name = ?, description = ?, content = ?,
                    variables_json = ?, metadata_json = ?
                WHERE template_id = ?
                """,
                (
                    iso,
                    name,
                    str(data.get("description", item.get("description") or "") or ""),
                    content,
                    self._json(variables or []),
                    self._json(metadata),
                    template_id,
                ),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM persona_templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()
        item = dict(updated)
        for key in ("variables_json", "metadata_json"):
            item[key.removesuffix("_json")] = self._loads(item.pop(key), None)
        return web.json_response({"ok": True, "item": item})

    async def api_delete_template(self, request: web.Request) -> web.Response:
        template_id = request.match_info.get("template_id", "").strip()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT template_id FROM persona_templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()
            if not row:
                return web.json_response(
                    {"ok": False, "error": "模块不存在"}, status=404
                )
            conn.execute(
                "DELETE FROM persona_templates WHERE template_id = ?",
                (template_id,),
            )
            conn.commit()
        return web.json_response(
            {
                "ok": True,
                "deleted_template_id": template_id,
                "deleted_module_id": template_id,
            }
        )

    async def api_create_patch_from_template(
        self, request: web.Request
    ) -> web.Response:
        template_id = request.match_info.get("template_id", "").strip()
        data = await self._read_json(request)
        persona_id = str(data.get("persona_id", "")).strip()
        if not persona_id:
            return web.json_response(
                {"ok": False, "error": "persona_id 必填"}, status=400
            )
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM persona_templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()
        if not row:
            return web.json_response({"ok": False, "error": "模块不存在"}, status=404)
        template = dict(row)
        template["metadata"] = self._loads(template.pop("metadata_json"), {})
        template["variables"] = self._loads(template.pop("variables_json"), [])
        proposed_prompt = str(
            data.get("proposed_prompt") or template.get("content") or ""
        )
        result = await self._create_patch_record_from_data(
            {
                "persona_id": persona_id,
                "proposed_prompt": proposed_prompt,
                "trigger": data.get(
                    "trigger",
                    f"从模块生成补丁草案：{template.get('name') or template_id}",
                ),
                "changes": data.get(
                    "changes",
                    [
                        {
                            "aspect": "module",
                            "after": template_id,
                            "reason": "人工从模块生成补丁草案",
                        }
                    ],
                ),
                "metadata": {
                    "source": "module",
                    "module_id": template_id,
                    "template_id": template_id,
                    **(
                        data.get("metadata", {})
                        if isinstance(data.get("metadata"), dict)
                        else {}
                    ),
                },
            }
        )
        return web.json_response({"ok": True, **result})

    def _next_template_id(self) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return f"mod-{ts}"

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

    async def api_get_profile(self, request: web.Request) -> web.Response:
        persona_id = request.match_info.get("persona_id", "").strip()
        if not persona_id:
            return web.json_response(
                {"ok": False, "error": "persona_id 必填"}, status=400
            )
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM persona_profiles WHERE persona_id = ?",
                (persona_id,),
            ).fetchone()
        if not row:
            return web.json_response(
                {
                    "ok": True,
                    "item": {
                        "persona_id": persona_id,
                        "display_name": "",
                        "archetype": "",
                        "notes": "",
                        "template_id": None,
                        "metadata": {},
                    },
                }
            )
        item = dict(row)
        item["metadata"] = self._loads(item.pop("metadata_json"), {})
        return web.json_response({"ok": True, "item": item})

    async def api_upsert_profile(self, request: web.Request) -> web.Response:
        persona_id = request.match_info.get("persona_id", "").strip()
        data = await self._read_json(request)
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

    async def api_delete_profile(self, request: web.Request) -> web.Response:
        persona_id = request.match_info.get("persona_id", "").strip()
        if not persona_id:
            return web.json_response(
                {"ok": False, "error": "persona_id 必填"}, status=400
            )
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM persona_profiles WHERE persona_id = ?",
                (persona_id,),
            )
            conn.commit()
        return web.json_response({"ok": True, "persona_id": persona_id})

    @filter.llm_tool(name="persona_sublimation_list_personas")
    async def tool_list_personas(self, event: AstrMessageEvent) -> dict[str, Any]:
        """列出可维护的人格状态摘要，不返回完整 system_prompt。

        Args:
        """
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
                snapshot_counts = {
                    row["persona_id"]: int(row["c"])
                    for row in conn.execute(
                        "SELECT persona_id, COUNT(*) AS c FROM persona_snapshots GROUP BY persona_id"
                    )
                }
            return {
                "ok": True,
                "items": [
                    {
                        "persona_id": p.persona_id,
                        "system_prompt_len": len(p.system_prompt or ""),
                        "updated_at": str(getattr(p, "updated_at", "") or ""),
                        "observation_count": obs_counts.get(p.persona_id, 0),
                        "patch_count": patch_counts.get(p.persona_id, 0),
                        "snapshot_count": snapshot_counts.get(p.persona_id, 0),
                    }
                    for p in personas
                ],
            }
        except Exception as exc:
            return {"ok": False, "error": f"列出人格失败：{exc}"}

    @filter.llm_tool(name="persona_sublimation_add_observation")
    async def tool_add_observation(
        self,
        event: AstrMessageEvent,
        persona_id: str,
        content: str,
        source: str = "",
        interpretation: str = "",
        emotion: str = "",
    ) -> dict[str, Any]:
        """为指定 persona 记录一条观察，不修改 persona。

        Args:
            persona_id(string): 要记录观察的 persona_id，必须明确指定。
            content(string): 观察内容。
            source(string): 来源，可为空，例如 owner-feedback/session/capture。
            interpretation(string): 对观察的理解，可为空。
            emotion(string): 情绪标记，可为空。
        """
        persona_id = str(persona_id or "").strip()
        content = str(content or "").strip()
        if not persona_id or not content:
            return {"ok": False, "error": "persona_id 和 content 必填"}
        ts, iso = self._now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO persona_observations
                (timestamp, timestamp_iso, persona_id, source, content, interpretation, emotion, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    iso,
                    persona_id,
                    str(source or ""),
                    content,
                    str(interpretation or ""),
                    str(emotion or ""),
                    self._json({"created_by": "llm_tool"}),
                ),
            )
            conn.commit()
            observation_id = cur.lastrowid
        return {
            "ok": True,
            "id": observation_id,
            "persona_id": persona_id,
            "message": "观察已记录，未修改 persona。",
        }

    @filter.llm_tool(name="persona_sublimation_list_observations")
    async def tool_list_observations(
        self, event: AstrMessageEvent, persona_id: str, limit: int = 10
    ) -> dict[str, Any]:
        """列出指定 persona 的观察摘要。

        Args:
            persona_id(string): 要查询的 persona_id。
            limit(number): 返回数量，默认 10，最多 50。
        """
        persona_id = str(persona_id or "").strip()
        if not persona_id:
            return {"ok": False, "error": "persona_id 必填"}
        limit = min(50, max(1, int(limit or 10)))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM persona_observations
                WHERE persona_id = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (persona_id, limit),
            ).fetchall()
        return {
            "ok": True,
            "items": [
                {
                    "id": row["id"],
                    "persona_id": row["persona_id"],
                    "timestamp_iso": row["timestamp_iso"],
                    "source": row["source"],
                    "emotion": row["emotion"],
                    "content_preview": self._clip_text(row["content"], 300),
                    "interpretation_preview": self._clip_text(
                        row["interpretation"], 200
                    ),
                }
                for row in rows
            ],
        }

    @filter.llm_tool(name="persona_sublimation_create_patch_draft")
    async def tool_create_patch_draft(
        self,
        event: AstrMessageEvent,
        persona_id: str,
        trigger: str,
        proposed_prompt: str = "",
        changes_json: str = "",
        notes: str = "",
        base_prompt: str = "",
    ) -> dict[str, Any]:
        """为指定 persona 起草 pending 调整，不审批也不应用。

        Args:
            persona_id(string): 要起草调整的 persona_id，必须明确指定。
            trigger(string): 起草原因。
            proposed_prompt(string): 拟议 system_prompt；可为空，空则只记录结构化草案。
            changes_json(string): 结构化调整项 JSON 字符串，例如 [{"aspect":"tone","after":"更短"}]；可为空。
            notes(string): 给人类审核者看的备注。
            base_prompt(string): 安全检查基线；可为空，空则取当前 persona prompt 作为基线。
        """
        try:
            data = {
                "persona_id": str(persona_id or "").strip(),
                "trigger": str(trigger or ""),
                "changes": self._loads(str(changes_json or ""), [])
                if str(changes_json or "").strip()
                else [],
                "notes": str(notes or ""),
                "metadata": {"created_by": "llm_tool"},
            }
            if proposed_prompt:
                data["proposed_prompt"] = str(proposed_prompt)
            if base_prompt:
                data["base_prompt"] = str(base_prompt)
            result = await self._create_patch_record_from_data(data)
            return {
                "ok": True,
                **result,
                "status": "pending",
                "message": "已起草 pending 调整；LLM 工具不会应用补丁，请到前端由人类审批/应用。",
            }
        except web.HTTPException as exc:
            return {"ok": False, "error": exc.reason}
        except Exception as exc:
            return {"ok": False, "error": f"起草调整失败：{exc}"}

    @filter.llm_tool(name="persona_sublimation_list_patches")
    async def tool_list_patches(
        self,
        event: AstrMessageEvent,
        persona_id: str,
        status: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        """列出指定 persona 的调整草案/补丁摘要，不返回完整 prompt。

        Args:
            persona_id(string): 要查询的 persona_id。
            status(string): 可选状态过滤：pending/approved/applied。
            limit(number): 返回数量，默认 10，最多 50。
        """
        persona_id = str(persona_id or "").strip()
        status = str(status or "").strip()
        if not persona_id:
            return {"ok": False, "error": "persona_id 必填"}
        limit = min(50, max(1, int(limit or 10)))
        where = "WHERE persona_id = ?"
        params: list[Any] = [persona_id]
        if status:
            where += " AND status = ?"
            params.append(status)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM persona_patches
                {where}
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return {
            "ok": True,
            "items": [
                self._patch_safe_summary(self._patch_row_to_item(row), False)
                for row in rows
            ],
        }

    @filter.llm_tool(name="persona_sublimation_get_patch")
    async def tool_get_patch(
        self, event: AstrMessageEvent, patch_id: str, include_content: bool = False
    ) -> dict[str, Any]:
        """查看某个调整草案/补丁；默认不返回完整 base/proposed prompt。

        Args:
            patch_id(string): patch_id。
            include_content(boolean): 是否显式返回完整 base_prompt/proposed_prompt/diff，默认 false。
        """
        patch_id = str(patch_id or "").strip()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM persona_patches WHERE patch_id = ?", (patch_id,)
            ).fetchone()
        if not row:
            return {"ok": False, "error": "补丁不存在"}
        return {
            "ok": True,
            "item": self._patch_safe_summary(
                self._patch_row_to_item(row), bool(include_content)
            ),
            "message": "该工具只查看/摘要补丁，不会审批或应用。",
        }

    @filter.llm_tool(name="persona_sublimation_create_snapshot")
    async def tool_create_snapshot(
        self,
        event: AstrMessageEvent,
        persona_id: str,
        label: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        """留存指定 persona 的当前版本快照，不修改 persona。

        Args:
            persona_id(string): 要留存版本的 persona_id，必须明确指定。
            label(string): 快照标签，可为空。
            description(string): 快照说明，可为空。
        """
        persona_id = str(persona_id or "").strip()
        if not persona_id:
            return {"ok": False, "error": "persona_id 必填"}
        try:
            persona = await self.context.persona_manager.get_persona(persona_id)
        except Exception as exc:
            return {"ok": False, "error": f"读取 persona 失败：{exc}"}
        content = persona.system_prompt or ""
        if not content.strip():
            return {"ok": False, "error": "当前 persona system_prompt 为空，无法留存"}
        with self._lock, self._connect() as conn:
            snapshot_id = self._insert_snapshot_record(
                conn,
                persona_id=persona_id,
                content=content,
                label=label or f"LLM 留存当前版本：{persona_id}",
                source="llm-tool",
                metadata={
                    "description": description,
                    "created_by": "llm_tool",
                },
            )
            conn.commit()
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "persona_id": persona_id,
            "content_len": len(content),
            "message": "当前版本已留存；未修改 persona。",
        }

    @filter.llm_tool(name="persona_sublimation_list_snapshots")
    async def tool_list_snapshots(
        self, event: AstrMessageEvent, persona_id: str, limit: int = 10
    ) -> dict[str, Any]:
        """列出指定 persona 的版本快照摘要，不返回正文。

        Args:
            persona_id(string): 要查询的 persona_id。
            limit(number): 返回数量，默认 10，最多 50。
        """
        persona_id = str(persona_id or "").strip()
        if not persona_id:
            return {"ok": False, "error": "persona_id 必填"}
        limit = min(50, max(1, int(limit or 10)))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM persona_snapshots
                WHERE persona_id = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (persona_id, limit),
            ).fetchall()
        return {
            "ok": True,
            "items": [
                self._snapshot_row_to_item(row, include_content=False) for row in rows
            ],
        }

    @filter.llm_tool(name="persona_sublimation_list_modules")
    async def tool_list_modules(
        self, event: AstrMessageEvent, limit: int = 20
    ) -> dict[str, Any]:
        """列出模块摘要，不展开正文。

        Args:
            limit(number): 返回数量，默认 20，最多 100。
        """
        limit = min(100, max(1, int(limit or 20)))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM persona_templates
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            for key in ("variables_json", "metadata_json"):
                item[key.removesuffix("_json")] = self._loads(item.pop(key), None)
            items.append(self._template_safe_summary(item, include_content=False))
        return {"ok": True, "items": items}

    @filter.llm_tool(name="persona_sublimation_get_module")
    async def tool_get_module(
        self,
        event: AstrMessageEvent,
        module_id: str,
        include_content: bool = False,
    ) -> dict[str, Any]:
        """查看模块；默认不展开正文，敏感模块即使请求展开也会隐藏。

        Args:
            module_id(string): 模块 ID（兼容存储字段为 template_id）。
            include_content(boolean): 是否显式请求返回正文，默认 false；敏感模块仍不会展开。
        """
        module_id = str(module_id or "").strip()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM persona_templates WHERE template_id = ?",
                (module_id,),
            ).fetchone()
        if not row:
            return {"ok": False, "error": "模块不存在"}
        item = dict(row)
        for key in ("variables_json", "metadata_json"):
            item[key.removesuffix("_json")] = self._loads(item.pop(key), None)
        return {
            "ok": True,
            "item": self._template_safe_summary(item, bool(include_content)),
        }

    @filter.llm_tool(name="persona_sublimation_generate_patch_from_snapshot")
    async def tool_generate_patch_from_snapshot(
        self,
        event: AstrMessageEvent,
        persona_id: str,
        snapshot_id: str,
        trigger: str = "",
    ) -> dict[str, Any]:
        """由某个快照起草 pending 调整，不审批也不应用。

        Args:
            persona_id(string): 要生成调整草案的目标 persona_id。
            snapshot_id(string): 快照 ID。
            trigger(string): 起草原因，可为空。
        """
        persona_id = str(persona_id or "").strip()
        snapshot_id = str(snapshot_id or "").strip()
        if not persona_id or not snapshot_id:
            return {"ok": False, "error": "persona_id 和 snapshot_id 必填"}
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM persona_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        if not row:
            return {"ok": False, "error": "快照不存在"}
        snapshot = self._snapshot_row_to_item(row, include_content=True)
        try:
            result = await self._create_patch_record_from_data(
                {
                    "persona_id": persona_id,
                    "proposed_prompt": snapshot.get("content") or "",
                    "trigger": trigger
                    or f"由快照起草调整：{snapshot.get('label') or snapshot_id}",
                    "changes": [
                        {
                            "aspect": "snapshot",
                            "after": snapshot_id,
                            "reason": "LLM 工具由快照起草，等待人类审核",
                        }
                    ],
                    "metadata": {
                        "source": "llm_tool_snapshot",
                        "snapshot_id": snapshot_id,
                        "created_by": "llm_tool",
                    },
                }
            )
            return {
                "ok": True,
                **result,
                "status": "pending",
                "message": "已由快照起草 pending 调整；请到前端由人类审批/应用。",
            }
        except Exception as exc:
            return {"ok": False, "error": f"由快照起草失败：{exc}"}

    @filter.llm_tool(name="persona_sublimation_create_patch_from_module")
    async def tool_create_patch_from_module(
        self,
        event: AstrMessageEvent,
        persona_id: str,
        module_id: str,
        trigger: str = "",
    ) -> dict[str, Any]:
        """由某个模块起草 pending 调整，不审批也不应用。

        Args:
            persona_id(string): 要生成调整草案的目标 persona_id。
            module_id(string): 模块 ID（兼容存储字段为 template_id）。
            trigger(string): 起草原因，可为空。
        """
        persona_id = str(persona_id or "").strip()
        module_id = str(module_id or "").strip()
        if not persona_id or not module_id:
            return {"ok": False, "error": "persona_id 和 module_id 必填"}
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM persona_templates WHERE template_id = ?",
                (module_id,),
            ).fetchone()
        if not row:
            return {"ok": False, "error": "模块不存在"}
        template = dict(row)
        for key in ("variables_json", "metadata_json"):
            template[key.removesuffix("_json")] = self._loads(template.pop(key), None)
        try:
            result = await self._create_patch_record_from_data(
                {
                    "persona_id": persona_id,
                    "proposed_prompt": str(template.get("content") or ""),
                    "trigger": trigger
                    or f"由模块起草调整：{template.get('name') or module_id}",
                    "changes": [
                        {
                            "aspect": "module",
                            "after": module_id,
                            "reason": "LLM 工具由模块起草，等待人类审核",
                        }
                    ],
                    "metadata": {
                        "source": "llm_tool_template",
                        "module_id": module_id,
                        "template_id": module_id,
                        "created_by": "llm_tool",
                    },
                }
            )
            return {
                "ok": True,
                **result,
                "status": "pending",
                "message": "已由模块起草 pending 调整；请到前端由人类审批/应用。",
            }
        except Exception as exc:
            return {"ok": False, "error": f"由模块起草失败：{exc}"}

    @filter.llm_tool(name="persona_sublimation_list_persona_modules")
    async def tool_list_persona_modules(
        self, event: AstrMessageEvent, persona_id: str
    ) -> dict[str, Any]:
        """列出某个 persona 已关联的模块清单，不返回模块正文。

        Args:
            persona_id(string): 要查询的 persona_id。
        """
        persona_id = str(persona_id or "").strip()
        if not persona_id:
            return {"ok": False, "error": "persona_id 必填"}
        return {"ok": True, "items": self._list_module_links(persona_id)}

    @filter.llm_tool(name="persona_sublimation_link_module")
    async def tool_link_module(
        self,
        event: AstrMessageEvent,
        persona_id: str,
        module_id: str,
        role: str = "",
        enabled: bool = True,
        order_index: int = 0,
        notes: str = "",
    ) -> dict[str, Any]:
        """把模块资产关联到某个 persona；只记录装配关系，不修改 persona。

        Args:
            persona_id(string): 目标 persona_id。
            module_id(string): 要关联的模块 ID（兼容存储字段为 template_id）。
            role(string): 模块角色，例如 meta/persona/system/nsfw/roleplay/ops/custom。
            enabled(boolean): 是否启用该关联。
            order_index(number): 模块顺序。
            notes(string): 备注。
        """
        persona_id = str(persona_id or "").strip()
        module_id = str(module_id or "").strip()
        if not persona_id or not module_id:
            return {"ok": False, "error": "persona_id 和 module_id 必填"}
        _, iso = self._now()
        with self._lock, self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM persona_templates WHERE template_id = ?",
                (module_id,),
            ).fetchone()
            if not exists:
                return {"ok": False, "error": "模块不存在"}
            conn.execute(
                """
                INSERT INTO persona_module_links
                (persona_id, template_id, role, enabled, order_index, notes, created_at, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(persona_id, template_id) DO UPDATE SET
                    role=excluded.role,
                    enabled=excluded.enabled,
                    order_index=excluded.order_index,
                    notes=excluded.notes,
                    updated_at=excluded.updated_at
                """,
                (
                    persona_id,
                    module_id,
                    role or "custom",
                    1 if enabled else 0,
                    int(order_index or 0),
                    notes or "",
                    iso,
                    iso,
                    self._json({"created_by": "llm_tool"}),
                ),
            )
            conn.commit()
        return {
            "ok": True,
            "items": self._list_module_links(persona_id),
            "message": "模块已关联到 persona；未修改原版 persona。",
        }

    @filter.llm_tool(name="persona_sublimation_unlink_module")
    async def tool_unlink_module(
        self, event: AstrMessageEvent, persona_id: str, link_id: int
    ) -> dict[str, Any]:
        """解除 persona 与模块的关联；不删除模块本体，不修改 persona。

        Args:
            persona_id(string): 目标 persona_id。
            link_id(number): 模块关联 ID。
        """
        persona_id = str(persona_id or "").strip()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM persona_module_links WHERE id = ? AND persona_id = ?",
                (int(link_id), persona_id),
            )
            conn.commit()
        if cur.rowcount <= 0:
            return {"ok": False, "error": "模块关联不存在"}
        return {"ok": True, "deleted_link_id": int(link_id)}

    @filter.llm_tool(name="persona_sublimation_create_patch_from_modules")
    async def tool_create_patch_from_modules(
        self, event: AstrMessageEvent, persona_id: str, notes: str = ""
    ) -> dict[str, Any]:
        """由当前 persona 已启用模块清单起草 pending 调整；不审批也不应用。

        Args:
            persona_id(string): 目标 persona_id。
            notes(string): 给人类审核者看的备注。
        """
        try:
            result = await self._create_patch_from_module_links(persona_id, notes)
            return {
                "ok": True,
                **result,
                "status": "pending",
                "message": "已由模块清单起草 pending 调整；请到前端由人类审批/应用。",
            }
        except web.HTTPException as exc:
            return {"ok": False, "error": exc.reason}
        except Exception as exc:
            return {"ok": False, "error": f"由模块清单起草失败：{exc}"}

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
