import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import Request
from fastapi.responses import JSONResponse

from core.plugin import BasePlugin, logger, on, register, PluginPage, PageMenu
from core.chat.message_utils import KiraMessageBatchEvent
from core.provider import LLMRequest
from core.prompt_manager import Prompt
from core.utils.path_utils import get_data_path, get_config_path

from .kb_manager import KnowledgeBaseManager, EmbeddingDimensionError
from . import api_handlers as api
from . import permissions as perms
from .recall import (RecallEngine, message_text, batch_user_ids, render_block,
                     clamp_int, clamp_float)


def _json(data, status: int = 200):
    return JSONResponse(data, status_code=status)


# Read-only helpers are registered at runtime so the ``extra_tools_enabled``
# switch can actually change what the model sees (a decorator would always
# register them at import time).
_EXTRA_TOOLS = (
    {
        "name": "knowledge_list_entries",
        "description": (
            "列出知识库中的条目（名称、大小、更新时间）。"
            "用法：knowledge_list_entries(kb_id=\"知识库ID，可选\", limit=50)"
        ),
        "params": {
            "type": "object",
            "properties": {
                "kb_id": {"type": "string", "description": "知识库ID（可选）"},
                "limit": {"type": "integer", "description": "最多返回多少条", "default": 50}
            },
            "required": []
        },
        "method": "knowledge_list_entries",
    },
    {
        "name": "knowledge_get_entry",
        "description": (
            "读取指定条目的完整内容。"
            "用法：knowledge_get_entry(title=\"标题\", kb_id=\"知识库ID，可选\")"
        ),
        "params": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "条目标题"},
                "kb_id": {"type": "string", "description": "知识库ID（可选）"}
            },
            "required": ["title"]
        },
        "method": "knowledge_get_entry",
    },
)


class KiraKBPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        self.data_dir = get_data_path()
        self.config_path = get_config_path() / "plugins" / "kiraKB.json"

        # Migrate legacy flat config to nested sections (one-time).
        self._migrate_config(cfg)

        sec_basic = cfg.get("section_basic", {})
        sec_webui = cfg.get("section_webui", {})
        sec_perm = cfg.get("section_permission", {})
        sec_recall = cfg.get("section_recall", {})

        self.kb_base_dir = sec_basic.get("knowledge_base_dir") or str(self.data_dir / "knowledge_base")
        self.chunk_size = int(sec_basic.get("chunk_size", 500))
        self.chunk_overlap = int(sec_basic.get("chunk_overlap", 100))
        if self.chunk_overlap >= self.chunk_size:
            self.chunk_overlap = max(0, self.chunk_size // 5)
        self.default_top_k = int(sec_basic.get("default_top_k", 5))
        self.enable_hybrid = bool(sec_basic.get("enable_hybrid_search", False))
        self.enable_stopwords = bool(sec_basic.get("enable_stopwords", False))
        self.enable_rerank = bool(sec_basic.get("enable_rerank", False))
        self.entry_title_max_chars = max(4, min(60, int(sec_basic.get("entry_title_max_chars", 20))))
        self.extra_tools_enabled = bool(sec_basic.get("extra_tools_enabled", True))
        self.auto_sync_versions = bool(sec_basic.get("auto_sync_versions", False))
        self.history_keep = max(1, min(200, int(sec_basic.get("entry_history_keep", 5))))

        self.enable_webui = bool(sec_webui.get("enable_webui", False))
        self.webui_port = int(sec_webui.get("webui_port", 19122))
        self.webui_host = sec_webui.get("webui_host", "127.0.0.1")
        self.webui_token = sec_webui.get("webui_token", "")

        self.owner_whitelist: List[str] = perms.normalise_users(sec_perm.get("owner_whitelist", []))
        self.default_policies: Dict[str, str] = {
            "add": perms.normalise_policy(sec_perm.get("default_add_policy"), default="open") or "open",
            "edit": perms.normalise_policy(sec_perm.get("default_edit_policy"), default="open") or "open",
            "delete": perms.normalise_policy(sec_perm.get("default_delete_policy"), default="whitelist") or "whitelist",
        }

        self.recall_config: Dict[str, Any] = {
            "enabled": bool(sec_recall.get("recall_enabled", False)),
            "mode": sec_recall.get("recall_mode", "hybrid"),
            "top_k": sec_recall.get("recall_top_k", 3),
            "min_score": sec_recall.get("recall_min_score", 0.35),
            "strong_score": sec_recall.get("recall_strong_score", 0.60),
            "max_chars": sec_recall.get("recall_max_chars", 1200),
            "timeout_ms": sec_recall.get("recall_timeout_ms", 1500),
            "dedupe_turns": sec_recall.get("recall_dedupe_turns", 2),
            "max_kbs": sec_recall.get("recall_max_kbs", 3),
            "min_query_chars": sec_recall.get("recall_min_query_chars", 2),
        }

        self.kb_manager: Optional[KnowledgeBaseManager] = None
        self.recall_engine: Optional[RecallEngine] = None
        self._webui_server = None
        self._extra_tools_registered = False
        self._bg_tasks: set = set()

    # ------------------------------------------------------------------
    # Config migration: flat (v1.0.0) -> nested sections (v1.1.0),
    # plus chunk_overlap 50 (old default) -> 100 (new default).
    # ------------------------------------------------------------------
    def _migrate_config(self, cfg: dict):
        if "section_basic" not in cfg:
            flat_keys = [
                "knowledge_base_dir", "chunk_size", "chunk_overlap",
                "default_top_k", "enable_hybrid_search", "enable_rerank",
                "webui_port", "webui_host", "webui_token",
            ]
            if not any(k in cfg for k in flat_keys):
                return  # nothing to migrate
            logger.info("[kiraKB] Migrating legacy flat config to nested sections")
            old_overlap = cfg.get("chunk_overlap", 50)
            new_cfg = {
                "section_basic": {
                    "knowledge_base_dir": cfg.get("knowledge_base_dir"),
                    "chunk_size": cfg.get("chunk_size", 500),
                    "chunk_overlap": 100 if old_overlap == 50 else old_overlap,
                    "default_top_k": cfg.get("default_top_k", 5),
                    "enable_hybrid_search": cfg.get("enable_hybrid_search", False),
                    "enable_stopwords": cfg.get("enable_stopwords", False),
                    "enable_rerank": cfg.get("enable_rerank", False),
                },
                "section_webui": {
                    "enable_webui": False,
                    "webui_port": cfg.get("webui_port", 19122),
                    "webui_host": cfg.get("webui_host", "127.0.0.1"),
                    "webui_token": cfg.get("webui_token", ""),
                },
                "section_permission": {
                    "owner_whitelist": [],
                },
            }
            try:
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                self.config_path.write_text(
                    json.dumps(new_cfg, indent=4, ensure_ascii=False), encoding="utf-8"
                )
                cfg.clear()
                cfg.update(new_cfg)
                logger.info("[kiraKB] Config migrated to nested sections")
            except Exception as e:
                logger.error(f"[kiraKB] Config migration failed: {e}")
            return

        sec_basic = cfg.get("section_basic", {})
        changed = False
        if sec_basic.get("chunk_overlap") == 50:
            sec_basic["chunk_overlap"] = 100
            changed = True
        if sec_basic.get("enable_hybrid_search") is True:
            sec_basic["enable_hybrid_search"] = False
            changed = True
        if changed:
            try:
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                self.config_path.write_text(
                    json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8"
                )
                logger.info("[kiraKB] Migrated old defaults: chunk_overlap 50->100, enable_hybrid_search true->false")
            except Exception as e:
                logger.error(f"[kiraKB] Config migration failed: {e}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _config_value(self, key: str, default=None):
        """Read a global framework config value across both generations."""
        config = getattr(self.ctx, "config", None)
        if config is None:
            config = getattr(self.ctx, "kira_config", None)
        if config is None:
            return default
        try:
            return config.get_config(key, default)
        except Exception:
            return default

    async def initialize(self):
        embedding_client = None
        try:
            embedding_client = self.ctx.get_default_embedding_client()
            if embedding_client:
                logger.info("[kiraKB] Using default embedding model")
        except Exception as e:
            logger.warning(f"[kiraKB] Failed to get embedding client: {e}")

        if not embedding_client:
            embedding_client = DummyEmbeddingClient()
            logger.warning("[kiraKB] No embedding model configured, using dummy. Search will not work.")

        vlm_client = None
        try:
            vlm_client = self.ctx.provider_mgr.get_default_vlm()
            if vlm_client:
                logger.info("[kiraKB] Using default VLM model for OCR")
        except Exception as e:
            logger.warning(f"[kiraKB] No VLM model available for OCR: {e}")

        rerank_client = None
        if self.enable_rerank:
            try:
                rerank_client = self.ctx.provider_mgr.get_default_rerank()
                if rerank_client:
                    logger.info("[kiraKB] Using default rerank model")
            except Exception as e:
                logger.warning(f"[kiraKB] No rerank model available: {e}")

        async def get_embedding_client():
            return embedding_client

        def list_embedding_models() -> List[dict]:
            models = []
            try:
                for provider_id, provider in self.ctx.provider_mgr.get_all_providers().items():
                    try:
                        infos = self.ctx.provider_mgr.get_model_infos(provider_id)
                    except Exception:
                        continue
                    for info in infos:
                        if getattr(info.model_type, "value", None) != "embedding":
                            continue
                        models.append({
                            "uuid": f"{provider_id}:{info.model_id}",
                            "model_id": info.model_id,
                            "provider_id": provider_id,
                            "provider_name": info.provider_name,
                            "label": f"{info.provider_name} / {getattr(info, 'model_name', None) or info.model_id}",
                        })
            except Exception as e:
                logger.warning(f"[kiraKB] Failed to list embedding models: {e}")
            models.sort(key=lambda m: m["label"].lower())
            return models

        def resolve_embedding_client(model_uuid: Optional[str]):
            if model_uuid:
                try:
                    client = self.ctx.get_embedding_client(model_uuid)
                    if client is not None:
                        return client
                except Exception as e:
                    logger.warning(f"[kiraKB] Failed to resolve embedding model '{model_uuid}': {e}")
            client = embedding_client
            if isinstance(client, DummyEmbeddingClient):
                return None
            return client

        stopwords_path = self.data_dir / "stopwords.txt"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not stopwords_path.exists():
            stopwords_path.touch()
        default_stopwords_path = Path(__file__).parent / "stopwords_default.txt"

        sw_path = str(stopwords_path) if (self.enable_stopwords and stopwords_path.exists()) else None
        dsw_path = str(default_stopwords_path) if (self.enable_stopwords and default_stopwords_path.exists()) else None

        self.kb_manager = KnowledgeBaseManager(
            base_dir=self.kb_base_dir,
            embedding_client_getter=get_embedding_client,
            stopwords_path=sw_path,
            default_stopwords_path=dsw_path,
            vlm_client=vlm_client,
            rerank_client=rerank_client,
            enable_rerank=self.enable_rerank,
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            client_resolver=resolve_embedding_client,
            model_lister=list_embedding_models,
            enable_hybrid=self.enable_hybrid,
            history_keep=self.history_keep,
        )

        await self.kb_manager.load_existing_kbs()

        self.recall_engine = RecallEngine(
            config_provider=lambda: self.recall_config,
            kb_provider=lambda: self.kb_manager.kbs if self.kb_manager else {},
            logger=logger,
        )
        # Expose read-only runtime settings to the WebUI (sidebar and standalone).
        self.kb_manager.recall_global = dict(self.recall_config)
        self.kb_manager.default_policies = dict(self.default_policies)
        self.kb_manager.settings_getter = self._settings_snapshot
        self.kb_manager.settings_setter = self._apply_settings

        self._register_extra_tools()

        if self.enable_webui and self.webui_port > 0 and self.kb_manager:
            from .web_server import WebUIServer
            self._webui_server = WebUIServer(
                kb_manager=self.kb_manager,
                host=self.webui_host,
                port=self.webui_port,
                token=self.webui_token,
                default_embedding_uuid=self._config_value("models.default_embedding"),
            )
            await self._webui_server.start()

        logger.info("[kiraKB] KiraKB plugin initialized")

    async def terminate(self):
        if self._webui_server:
            await self._webui_server.stop()
            self._webui_server = None
        self._unregister_extra_tools()
        if self.kb_manager:
            await self.kb_manager.close_all()
        for t in list(self._bg_tasks):
            if not t.done():
                t.cancel()
        self._bg_tasks.clear()

    def _spawn_task(self, coro):
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _register_extra_tools(self) -> None:
        if not self.extra_tools_enabled:
            return
        tool_mgr = getattr(self.ctx, "tool_mgr", None)
        if tool_mgr is None:
            return
        for spec in _EXTRA_TOOLS:
            try:
                tool_mgr.register_tool(
                    name=spec["name"],
                    description=spec["description"],
                    parameters=spec["params"],
                    func=getattr(self, spec["method"]),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[kiraKB] Failed to register tool {spec['name']}: {exc}")
        self._extra_tools_registered = True

    def _unregister_extra_tools(self) -> None:
        if not self._extra_tools_registered:
            return
        tool_mgr = getattr(self.ctx, "tool_mgr", None)
        if tool_mgr is not None:
            for spec in _EXTRA_TOOLS:
                try:
                    tool_mgr.unregister_tool(spec["name"])
                except Exception:
                    pass
        self._extra_tools_registered = False

    def _sync_extra_tools(self) -> None:
        if self.extra_tools_enabled:
            self._register_extra_tools()
        else:
            self._unregister_extra_tools()

    # ------------------------------------------------------------------
    # Runtime settings (hot apply, no plugin reload)
    # ------------------------------------------------------------------
    def _settings_snapshot(self) -> dict:
        return {
            "search": {
                "enable_hybrid_search": self.enable_hybrid,
                "default_top_k": self.default_top_k,
                "chunk_size": self.chunk_size,
                "chunk_overlap": self.chunk_overlap,
                "enable_stopwords": self.enable_stopwords,
                "enable_rerank": self.enable_rerank,
            },
            "entries": {
                "entry_title_max_chars": self.entry_title_max_chars,
                "extra_tools_enabled": self.extra_tools_enabled,
                "auto_sync_versions": self.auto_sync_versions,
                "entry_history_keep": self.history_keep,
            },
            "permission": {
                "owner_whitelist": list(self.owner_whitelist),
                "default_add_policy": self.default_policies["add"],
                "default_edit_policy": self.default_policies["edit"],
                "default_delete_policy": self.default_policies["delete"],
            },
            "recall": dict(self.recall_config),
            "restart": {
                "knowledge_base_dir": self.kb_base_dir,
                "enable_webui": self.enable_webui,
                "webui_host": self.webui_host,
                "webui_port": self.webui_port,
            },
        }

    def _apply_settings(self, payload: dict) -> dict:
        """Apply WebUI settings to memory and the plugin config file.

        Only non-structural settings are hot. Stopwords/rerank and the storage
        or standalone-server options need a plugin reload, and are reported as
        warnings instead of being silently ignored.
        """
        if not isinstance(payload, dict):
            raise ValueError("settings payload must be an object")
        warnings = []
        search = payload.get("search") or {}
        entries = payload.get("entries") or {}
        permission = payload.get("permission") or {}
        recall = payload.get("recall") or {}

        if "enable_hybrid_search" in search:
            self.enable_hybrid = bool(search.get("enable_hybrid_search"))
            if self.kb_manager:
                self.kb_manager.enable_hybrid = self.enable_hybrid
        if "default_top_k" in search:
            self.default_top_k = clamp_int(search.get("default_top_k"), self.default_top_k, 1, 20)
        chunk_size = clamp_int(search.get("chunk_size"), self.chunk_size, 100, 8000)
        chunk_overlap = clamp_int(search.get("chunk_overlap"), self.chunk_overlap, 0, max(0, chunk_size - 1))
        if chunk_overlap >= chunk_size:
            chunk_overlap = max(0, chunk_size // 5)
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        if self.kb_manager:
            self.kb_manager.chunk_size = chunk_size
            self.kb_manager.chunk_overlap = chunk_overlap
            for kb in self.kb_manager.kbs.values():
                kb.chunk_size = chunk_size
                kb.chunk_overlap = chunk_overlap
        if "enable_stopwords" in search:
            desired = bool(search.get("enable_stopwords"))
            if desired != self.enable_stopwords:
                self.enable_stopwords = desired
                if self.kb_manager:
                    sw_path = self.data_dir / "stopwords.txt"
                    dsw_path = Path(__file__).parent / "stopwords_default.txt"
                    self.kb_manager.refresh_stopwords(
                        str(sw_path) if desired and sw_path.exists() else None,
                        str(dsw_path) if desired and dsw_path.exists() else None,
                    )
        if "enable_rerank" in search:
            desired = bool(search.get("enable_rerank"))
            if desired != self.enable_rerank:
                client = None
                if desired:
                    try:
                        client = self.ctx.provider_mgr.get_default_rerank()
                    except Exception:
                        client = None
                if desired and client is None:
                    warnings.append("未配置重排序模型，未能启用重排序。")
                else:
                    self.enable_rerank = desired
                    if self.kb_manager:
                        self.kb_manager.set_rerank(client, desired)

        if "entry_title_max_chars" in entries:
            self.entry_title_max_chars = clamp_int(
                entries.get("entry_title_max_chars"), self.entry_title_max_chars, 4, 60
            )
        if "auto_sync_versions" in entries:
            self.auto_sync_versions = bool(entries.get("auto_sync_versions"))
        if "entry_history_keep" in entries:
            self.history_keep = clamp_int(
                entries.get("entry_history_keep"), self.history_keep, 1, 200
            )
            if self.kb_manager:
                self.kb_manager.history_keep = self.history_keep
                for kb in self.kb_manager.kbs.values():
                    kb.history_keep = self.history_keep
        if "extra_tools_enabled" in entries:
            enabled = bool(entries.get("extra_tools_enabled"))
            if enabled != self.extra_tools_enabled:
                self.extra_tools_enabled = enabled
                self._sync_extra_tools()

        if "owner_whitelist" in permission:
            self.owner_whitelist = perms.normalise_users(permission.get("owner_whitelist"))
        for key, op in (("default_add_policy", "add"),
                        ("default_edit_policy", "edit"),
                        ("default_delete_policy", "delete")):
            if key in permission:
                value = perms.normalise_policy(permission.get(key), default=None)
                if value:
                    self.default_policies[op] = value
        if self.kb_manager:
            self.kb_manager.default_policies = dict(self.default_policies)

        mode = str(recall.get("recall_mode", self.recall_config.get("mode", "hybrid"))).lower()
        if mode not in ("mention", "semantic", "hybrid"):
            mode = "hybrid"
        self.recall_config.update({
            "enabled": bool(recall.get("recall_enabled", self.recall_config.get("enabled", False))),
            "mode": mode,
            "top_k": clamp_int(recall.get("recall_top_k"), self.recall_config.get("top_k", 3), 1, 10),
            "min_score": clamp_float(recall.get("recall_min_score"), self.recall_config.get("min_score", 0.35)),
            "strong_score": clamp_float(recall.get("recall_strong_score"), self.recall_config.get("strong_score", 0.60)),
            "max_chars": clamp_int(recall.get("recall_max_chars"), self.recall_config.get("max_chars", 1200), 100, 8000),
            "timeout_ms": clamp_int(recall.get("recall_timeout_ms"), self.recall_config.get("timeout_ms", 1500), 200, 10000),
            "dedupe_turns": clamp_int(recall.get("recall_dedupe_turns"), self.recall_config.get("dedupe_turns", 2), 0, 20),
            "max_kbs": clamp_int(recall.get("recall_max_kbs"), self.recall_config.get("max_kbs", 3), 1, 20),
            "min_query_chars": clamp_int(recall.get("recall_min_query_chars"), self.recall_config.get("min_query_chars", 2), 1, 32),
        })
        if self.kb_manager:
            self.kb_manager.recall_global = dict(self.recall_config)

        self._persist_settings()
        return {"ok": True, "settings": self._settings_snapshot(), "warnings": warnings}

    def _persist_settings(self) -> None:
        try:
            cfg = {}
            if self.config_path.exists():
                cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(cfg, dict):
                cfg = {}
            basic = cfg.setdefault("section_basic", {})
            basic.update({
                "enable_hybrid_search": self.enable_hybrid,
                "default_top_k": self.default_top_k,
                "chunk_size": self.chunk_size,
                "chunk_overlap": self.chunk_overlap,
                "enable_stopwords": self.enable_stopwords,
                "enable_rerank": self.enable_rerank,
                "entry_title_max_chars": self.entry_title_max_chars,
                "extra_tools_enabled": self.extra_tools_enabled,
                "auto_sync_versions": self.auto_sync_versions,
                "entry_history_keep": self.history_keep,
            })
            perm = cfg.setdefault("section_permission", {})
            perm.update({
                "owner_whitelist": list(self.owner_whitelist),
                "default_add_policy": self.default_policies["add"],
                "default_edit_policy": self.default_policies["edit"],
                "default_delete_policy": self.default_policies["delete"],
            })
            rec = cfg.setdefault("section_recall", {})
            rec.update({
                "recall_enabled": self.recall_config.get("enabled", False),
                "recall_mode": self.recall_config.get("mode", "hybrid"),
                "recall_top_k": self.recall_config.get("top_k", 3),
                "recall_min_score": self.recall_config.get("min_score", 0.35),
                "recall_strong_score": self.recall_config.get("strong_score", 0.60),
                "recall_max_chars": self.recall_config.get("max_chars", 1200),
                "recall_timeout_ms": self.recall_config.get("timeout_ms", 1500),
                "recall_dedupe_turns": self.recall_config.get("dedupe_turns", 2),
                "recall_max_kbs": self.recall_config.get("max_kbs", 3),
                "recall_min_query_chars": self.recall_config.get("min_query_chars", 2),
            })
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.config_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=4), encoding="utf-8")
            tmp.replace(self.config_path)
            plugin_mgr = getattr(self.ctx, "plugin_mgr", None)
            if plugin_mgr is not None and hasattr(plugin_mgr, "plugin_configs"):
                plugin_mgr.plugin_configs[self.config_path.stem] = cfg
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[kiraKB] Failed to persist settings: {exc}")

    # ------------------------------------------------------------------
    # Permission helpers
    # ------------------------------------------------------------------
    def _get_sender_id(self, event: KiraMessageBatchEvent) -> str:
        try:
            if event.messages and event.messages[-1].sender:
                return str(event.messages[-1].sender.user_id)
        except Exception:
            pass
        try:
            sid = event.sid or ""
            return sid.split(":")[-1]
        except Exception:
            return ""

    def _check_write(self, event, op: str, kb, entry: Optional[dict] = None):
        kb_perms = kb.get_permissions()
        entry_perms = entry.get("permissions") if entry else None
        return perms.decide(
            op=op,
            actor=perms.ACTOR_AI,
            user_ids=batch_user_ids(event) or [self._get_sender_id(event)],
            entry_perms=entry_perms,
            kb_perms=kb_perms,
            defaults=self.default_policies,
            entry_users=(entry_perms or {}).get("allow_users") if entry_perms else None,
            kb_users=(kb_perms or {}).get("allow_users") if kb_perms else None,
            global_whitelist=self.owner_whitelist,
        )

    def _deny_message(self, op: str, kb, entry: Optional[dict], reason: str) -> str:
        scope = f"条目「{entry.get('name')}」" if entry else f"知识库「{kb.display_name}」"
        action = {"add": "新增", "edit": "编辑", "delete": "删除"}.get(op, op)
        return f"{scope}的{action}权限不足：{reason}"

    # ------------------------------------------------------------------
    # Passive recall hook
    # ------------------------------------------------------------------
    @on.llm_request(priority=-50)
    async def inject_passive_recall(self, event: KiraMessageBatchEvent, req: LLMRequest, *_):
        if self.recall_engine is None:
            return
        cfg = self.recall_engine.config()
        if not cfg.get("enabled"):
            return
        query = message_text(event)
        if not self.recall_engine.should_run(query, cfg):
            return
        try:
            hits = await asyncio.wait_for(
                self.recall_engine.collect(
                    event=event, query=query, sid=event.sid,
                    turn_key=getattr(event, "event_id", "") or f"{event.sid}:{query[:32]}",
                ),
                timeout=max(0.2, cfg["timeout_ms"] / 1000.0),
            )
        except asyncio.TimeoutError:
            logger.debug("[kiraKB] passive recall timed out; skipping this turn")
            return
        except Exception as exc:  # noqa: BLE001 - never break a chat turn
            logger.debug(f"[kiraKB] passive recall skipped: {type(exc).__name__}: {exc}")
            return
        if not hits:
            return
        block = render_block(hits, cfg["max_chars"])
        if not block:
            return
        req.user_prompt.insert(
            0,
            Prompt(
                block,
                name="kirakb_recall",
                source="system",
                persist=False,
                render_template=False,
            ),
        )

    # ------------------------------------------------------------------
    # Tool helpers
    # ------------------------------------------------------------------
    async def _resolve_kb(self, kb_id: Optional[str]):
        if not self.kb_manager:
            return None, "知识库管理器未初始化"
        if not self.kb_manager.kbs:
            return None, "没有可用的知识库。请先在 WebUI 中创建。"
        if not kb_id:
            kb_id = list(self.kb_manager.kbs.keys())[0]
        kb = await self.kb_manager.get_kb(kb_id)
        if not kb:
            available = ", ".join(self.kb_manager.kbs.keys())
            return None, f"知识库 '{kb_id}' 不存在。可用的: {available}"
        return kb, ""

    def _sanitise_title(self, raw: str, content: str) -> str:
        text = (raw or "").strip()
        if not text:
            for line in (content or "").splitlines():
                if line.strip():
                    text = line.strip()
                    break
        text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text).strip()
        text = re.sub(r"\s+", " ", text)
        if not text:
            digest = hashlib.md5((content or "").encode("utf-8", errors="replace")).hexdigest()[:4]
            text = "条目-" + digest
        return text[: self.entry_title_max_chars].strip()

    def _decorate_hits(self, kb, hits):
        names = {d["doc_id"]: d["name"] for d in kb.list_documents(include_deleted=True)}
        out = []
        for hit in hits:
            doc_id = str((hit.get("metadata") or {}).get("doc_id") or "")
            row = dict(hit)
            row["doc_id"] = doc_id
            row["doc_name"] = names.get(doc_id) or hit.get("doc_name") or doc_id
            out.append(row)
        return out

    # ------------------------------------------------------------------
    # Tool 1: list knowledge bases
    # ------------------------------------------------------------------
    @register.tool(
        name="list_knowledge_bases",
        description="列出所有知识库、条目数量与当前激活版本。不确定用哪个知识库时先调用。用法：list_knowledge_bases()",
        params={"type": "object", "properties": {}, "required": []}
    )
    async def list_knowledge_bases(self, event: KiraMessageBatchEvent) -> str:
        if not self.kb_manager:
            return "知识库管理器未初始化"
        lines = []
        for kb_id, kb in self.kb_manager.kbs.items():
            active_ver = kb.active_version
            ver_info = f"激活版本: {active_ver.model_name if active_ver else '无'}"
            count = len(kb.list_documents(include_deleted=False))
            lines.append(
                f"- **{kb.display_name}** (ID: `{kb_id}`): {kb.description or '无描述'} | "
                f"{count} 个条目 | {ver_info}"
            )
        if not lines:
            return "没有可用的知识库。请先在 WebUI 中创建。"
        return "可用的知识库列表：\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # Tool 2: search
    # ------------------------------------------------------------------
    @register.tool(
        name="knowledge_search",
        description=(
            "检索知识库获取信息。用户问题可能涉及知识库内容时调用。"
            "用法：knowledge_search(query=\"用户问题\", kb_id=\"知识库ID，可选；省略表示检索全部\", top_k=5)"
        ),
        params={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "用户的问题"},
                "kb_id": {"type": "string", "description": "知识库ID（可选，省略表示检索全部知识库）"},
                "top_k": {"type": "integer", "description": "返回结果数量（1-20）", "default": 5}
            },
            "required": ["query"]
        }
    )
    async def knowledge_search(self, event: KiraMessageBatchEvent, query: str,
                               kb_id: str = None, top_k: int = None):
        if not self.kb_manager:
            return "知识库管理器未初始化"
        if not query:
            return "请提供查询内容"
        if not self.kb_manager.kbs:
            return "没有可用的知识库"
        try:
            top_k = max(1, min(20, int(top_k or self.default_top_k)))
        except (TypeError, ValueError):
            top_k = self.default_top_k

        if kb_id and kb_id not in ("all", "*", "__all__"):
            kb = await self.kb_manager.get_kb(kb_id)
            if not kb:
                available = ", ".join(self.kb_manager.kbs.keys())
                return f"知识库 '{kb_id}' 不存在。可用的: {available}"
            targets = [(kb_id, kb)]
            per_kb = top_k
        else:
            targets = list(self.kb_manager.kbs.items())
            per_kb = top_k

        collected = []
        errors = []
        for target_id, kb in targets:
            active_ver = kb.active_version
            if not active_ver:
                if any(v.status != "ready" for v in kb._versions.values()):
                    errors.append(f"{target_id}: 没有可用版本（存在不完整的建库残留，请删除后重建）")
                else:
                    errors.append(f"{target_id}: 没有激活版本")
                continue
            if active_ver.status != "ready":
                errors.append(f"{target_id}: 激活版本不完整，请删除并重建")
                continue
            try:
                hits = await active_ver.search(
                    query, top_k=per_kb, enable_hybrid=self.enable_hybrid
                )
            except EmbeddingDimensionError as exc:
                errors.append(f"{target_id}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                detail = str(exc).strip() or type(exc).__name__
                errors.append(f"{target_id}: {detail}")
                continue
            hits = self._decorate_hits(kb, hits)
            for hit in hits:
                hit["kb_id"] = target_id
                hit["kb_name"] = kb.display_name
            if hits and self.recall_engine is not None:
                self.recall_engine.record_tool_hits(event.sid, target_id, hits)
            collected.append(hits)

        merged = []
        index = 0
        while len(merged) < top_k:
            added = False
            for group in collected:
                if index < len(group):
                    merged.append(group[index])
                    added = True
                    if len(merged) >= top_k:
                        break
            if not added:
                break
            index += 1

        if not merged:
            if errors:
                return "检索失败：\n" + "\n".join(f"- {line}" for line in errors)
            return "未找到相关信息"

        output = []
        show_kb = len(targets) > 1
        for i, hit in enumerate(merged):
            source = f"{hit.get('kb_name')} / " if show_kb else ""
            output.append(
                f"【结果 {i + 1}】来自 {source}{hit.get('doc_name', 'unknown')} "
                f"(相关度: {float(hit.get('score', 0)):.2f})\n{hit.get('content', '')}\n"
            )
        if errors:
            output.append("部分知识库检索失败：\n" + "\n".join(f"- {line}" for line in errors))
        return "\n".join(output)

    # ------------------------------------------------------------------
    # Tool 3: add / update entry
    # ------------------------------------------------------------------
    @register.tool(
        name="knowledge_update_entry",
        description=(
            "新增或更新知识条目。用户提供有长期价值的信息时调用。"
            "用法：knowledge_update_entry(content=\"内容\", title=\"标题，可选\", kb_id=\"知识库ID，可选\")"
        ),
        params={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要存储的知识内容"},
                "title": {"type": "string", "description": "条目标题（可选，由你根据内容生成）"},
                "kb_id": {"type": "string", "description": "知识库ID（可选，默认第一个）"}
            },
            "required": ["content"]
        }
    )
    async def knowledge_update_entry(self, event: KiraMessageBatchEvent, content: str,
                                     title: str = None, kb_id: str = None):
        if not self.kb_manager:
            return "知识库管理器未初始化"
        if not content or not content.strip():
            return "内容不能为空"
        kb, error = await self._resolve_kb(kb_id)
        if not kb:
            return error

        name = self._sanitise_title(title, content)
        matches = kb.find_documents(name)
        live = [m for m in matches if not m.get("deleted")]
        dead = [m for m in matches if m.get("deleted")]
        target = live[0] if live else (dead[0] if dead else None)

        if target:
            allowed, reason, _ = self._check_write(event, "edit", kb, target)
            if not allowed:
                return self._deny_message("edit", kb, target, reason)
            doc_id = target["doc_id"]
            await kb.update_raw_document(doc_id, content, actor="ai")
            if target.get("deleted"):
                ok, detail = await kb.restore_document(doc_id)
                if not ok:
                    return f"条目 '{name}' 已更新，但恢复失败：{detail}"
                return f"已恢复并更新知识条目 '{name}'，并同步到所有可用版本。"
            active_ver = kb.active_version
            if active_ver:
                await active_ver.delete_document(doc_id)
                await kb._vectorize_document(active_ver, doc_id, content)
                others = [
                    ver for ver in kb._versions.values()
                    if ver.version_id != active_ver.version_id and ver.status == "ready"
                ]
                synced = 0
                if self.auto_sync_versions:
                    for ver in others:
                        try:
                            await kb.sync_version(ver.version_id, doc_ids=[doc_id])
                            synced += 1
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(f"[kiraKB] auto sync {ver.version_id} failed: {exc}")
                if not others:
                    return f"已更新知识条目 '{name}'，并同步更新了当前激活版本中的向量。"
                if self.auto_sync_versions and synced == len(others):
                    return f"已更新知识条目 '{name}'，并同步到全部 {len(others) + 1} 个可用版本。"
                return (
                    f"已更新知识条目 '{name}'，并同步更新了当前激活版本中的向量。"
                    f"另有 {len(others)} 个版本待同步，可在面板一键同步。"
                )
            return "文档已保存，但知识库没有激活版本，无法向量化。请先在 WebUI 创建版本。"

        allowed, reason, _ = self._check_write(event, "add", kb, None)
        if not allowed:
            return self._deny_message("add", kb, None, reason)

        doc_id = await kb.add_raw_document(content, name, actor="ai")
        active_ver = kb.active_version
        if not active_ver:
            return "文档已保存，但知识库没有激活版本，无法向量化。请先在 WebUI 创建版本。"
        try:
            await kb._vectorize_document(active_ver, doc_id, content)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[kiraKB] Vectorization failed: {exc}", exc_info=True)
            return f"文档已保存，但向量化失败: {exc}。请检查嵌入模型配置。"
        return f"已新增知识条目 '{name}'，并同步更新了当前激活版本中的向量。"

    # ------------------------------------------------------------------
    # Tool 4: delete entry
    # ------------------------------------------------------------------
    @register.tool(
        name="knowledge_delete_entry",
        description=(
            "删除指定的知识条目（软删除，可在 WebUI 恢复）。"
            "用法：knowledge_delete_entry(title=\"标题\", kb_id=\"知识库ID，可选\")"
        ),
        params={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "要删除的条目标题"},
                "kb_id": {"type": "string", "description": "知识库ID（可选，默认第一个）"}
            },
            "required": ["title"]
        }
    )
    async def knowledge_delete_entry(self, event: KiraMessageBatchEvent, title: str,
                                     kb_id: str = None):
        if not self.kb_manager:
            return "知识库管理器未初始化"
        kb, error = await self._resolve_kb(kb_id)
        if not kb:
            return error
        doc = kb.find_document(title, include_deleted=False)
        if not doc:
            return f"未找到名为 '{title}' 的知识条目（可能已删除）"
        allowed, reason, _ = self._check_write(event, "delete", kb, doc)
        if not allowed:
            return self._deny_message("delete", kb, doc, reason)
        await kb.delete_raw_document(doc["doc_id"], soft=True)
        return f"已删除知识条目 '{doc['name']}'（软删除），可从 WebUI 恢复。"

    # ------------------------------------------------------------------
    # Tool 5: restore entry
    # ------------------------------------------------------------------
    @register.tool(
        name="knowledge_restore_entry",
        description=(
            "恢复被软删除的知识条目。"
            "用法：knowledge_restore_entry(title=\"标题\", kb_id=\"知识库ID，可选\")"
        ),
        params={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "要恢复的条目标题"},
                "kb_id": {"type": "string", "description": "知识库ID（可选，默认第一个）"}
            },
            "required": ["title"]
        }
    )
    async def knowledge_restore_entry(self, event: KiraMessageBatchEvent, title: str,
                                      kb_id: str = None):
        if not self.kb_manager:
            return "知识库管理器未初始化"
        kb, error = await self._resolve_kb(kb_id)
        if not kb:
            return error
        doc = kb.find_document(title, include_deleted=True)
        if not doc or not doc.get("deleted"):
            return f"未找到已删除的条目 '{title}'"
        allowed, reason, _ = self._check_write(event, "delete", kb, doc)
        if not allowed:
            return self._deny_message("delete", kb, doc, reason)
        ok, detail = await kb.restore_document(doc["doc_id"])
        if not ok:
            return f"恢复失败：{detail}"
        failed = [v for v in detail.get("versions", []) if not v.get("ok")]
        if failed:
            return f"条目 '{doc['name']}' 已恢复，但部分版本同步失败：{failed}"
        return f"已恢复知识条目 '{doc['name']}'，并同步到所有可用版本。"

    # ------------------------------------------------------------------
    # Tool 6: list entries (registered conditionally in initialize())
    # ------------------------------------------------------------------
    async def knowledge_list_entries(self, event: KiraMessageBatchEvent,
                                     kb_id: str = None, limit: int = 50):
        if not self.kb_manager:
            return "知识库管理器未初始化"
        kb, error = await self._resolve_kb(kb_id)
        if not kb:
            return error
        try:
            limit = max(1, min(200, int(limit or 50)))
        except (TypeError, ValueError):
            limit = 50
        docs = kb.list_documents(include_deleted=False)[:limit]
        if not docs:
            return f"知识库 '{kb.display_name}' 还没有条目。"
        lines = [f"知识库「{kb.display_name}」共有 {len(kb.list_documents())} 个条目："]
        for doc in docs:
            updated = ""
            if doc.get("updated_at"):
                updated = " | 更新于 " + time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(float(doc["updated_at"]))
                )
            lines.append(f"- {doc['name']} ({max(1, doc.get('size', 0) // 1024)} KB{updated})")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Tool 7: read entry (registered conditionally in initialize())
    # ------------------------------------------------------------------
    async def knowledge_get_entry(self, event: KiraMessageBatchEvent, title: str,
                                  kb_id: str = None):
        if not self.kb_manager:
            return "知识库管理器未初始化"
        kb, error = await self._resolve_kb(kb_id)
        if not kb:
            return error
        doc = kb.find_document(title, include_deleted=False)
        if not doc:
            return f"未找到条目 '{title}'"
        content = await kb.get_raw_document(doc["doc_id"])
        if content is None:
            return f"条目 '{doc['name']}' 的内容缺失"
        return f"【{doc['name']}】\n{content}"

    # ==================== Model selection / dimension probing ====================

    @register.api(method="GET", path="/settings", auth=True, summary="Get runtime settings")
    async def api_get_settings(self):
        data, status = api.get_settings(self.kb_manager)
        return _json(data, status)

    @register.api(method="PUT", path="/settings", auth=True, summary="Update runtime settings")
    async def api_update_settings(self, request: Request):
        body = await request.json()
        data, status = api.update_settings(self.kb_manager, body)
        return _json(data, status)

    @register.api(method="GET", path="/embedding-models", auth=True, summary="List embedding models")
    async def api_embedding_models(self):
        default_uuid = self._config_value("models.default_embedding")
        return api.embedding_models(self.kb_manager, default_uuid)

    @register.api(method="POST", path="/embedding-models/probe", auth=True, summary="Probe embedding dimension")
    async def api_probe_embedding(self, request: Request):
        body = await request.json()
        return await api.probe_embedding(self.kb_manager, (body or {}).get("model_uuid"))

    # ==================== Sidebar WebUI (page + APIs) ====================

    @register.page("/index", menu=PageMenu(label={"zh": "知识库", "en": "Knowledge Base"}, icon="Collection", order=100))
    def kb_page(self):
        return PluginPage.from_folder("./web")

    # ---- Knowledge base ----

    @register.api(method="GET", path="/kbs", auth=True, summary="List knowledge bases")
    async def api_list_kbs(self):
        data, status = api.list_kbs(self.kb_manager)
        return _json(data, status)

    @register.api(method="POST", path="/kbs", auth=True, summary="Create knowledge base")
    async def api_create_kb(self, request: Request):
        body = await request.json()
        data, status = await api.create_kb(self.kb_manager, body.get("kb_id", "").strip())
        return _json(data, status)

    @register.api(method="DELETE", path="/kbs/{kb_id}", auth=True, summary="Delete knowledge base")
    async def api_delete_kb(self, kb_id: str):
        data, status = await api.delete_kb(self.kb_manager, kb_id)
        return _json(data, status)

    @register.api(method="GET", path="/kbs/{kb_id}/info", auth=True, summary="Get KB info")
    async def api_get_kb_info(self, kb_id: str):
        data, status = await api.get_kb_info(self.kb_manager, kb_id)
        return _json(data, status)

    @register.api(method="PUT", path="/kbs/{kb_id}/info", auth=True, summary="Update KB info")
    async def api_update_kb_info(self, kb_id: str, request: Request):
        body = await request.json()
        data, status = await api.update_kb_info(self.kb_manager, kb_id, body)
        return _json(data, status)

    # ---- Permissions ----

    @register.api(method="GET", path="/kbs/{kb_id}/permissions", auth=True, summary="Get KB permissions")
    async def api_get_kb_permissions(self, kb_id: str):
        data, status = await api.get_kb_permissions(self.kb_manager, kb_id)
        return _json(data, status)

    @register.api(method="PUT", path="/kbs/{kb_id}/permissions", auth=True, summary="Update KB permissions")
    async def api_update_kb_permissions(self, kb_id: str, request: Request):
        body = await request.json()
        data, status = await api.update_kb_permissions(self.kb_manager, kb_id, body)
        return _json(data, status)

    @register.api(method="PUT", path="/kbs/{kb_id}/documents/permissions", auth=True, summary="Bulk update entry permissions")
    async def api_bulk_document_permissions(self, kb_id: str, request: Request):
        body = await request.json()
        data, status = await api.update_documents_permissions(self.kb_manager, kb_id, body)
        return _json(data, status)

    @register.api(method="GET", path="/kbs/{kb_id}/documents/{doc_id}/permissions", auth=True, summary="Get entry permissions")
    async def api_get_document_permissions(self, kb_id: str, doc_id: str):
        data, status = await api.get_document_permissions(self.kb_manager, kb_id, doc_id)
        return _json(data, status)

    @register.api(method="PUT", path="/kbs/{kb_id}/documents/{doc_id}/permissions", auth=True, summary="Update entry permissions")
    async def api_update_document_permissions(self, kb_id: str, doc_id: str, request: Request):
        body = await request.json()
        data, status = await api.update_document_permissions(self.kb_manager, kb_id, doc_id, body)
        return _json(data, status)

    # ---- Passive recall settings ----

    @register.api(method="GET", path="/recall/global", auth=True, summary="Get global recall settings")
    async def api_get_recall_global(self):
        data, status = api.get_recall_global(self.kb_manager)
        return _json(data, status)

    @register.api(method="GET", path="/kbs/{kb_id}/recall", auth=True, summary="Get KB recall settings")
    async def api_get_kb_recall(self, kb_id: str):
        data, status = await api.get_kb_recall(self.kb_manager, kb_id)
        return _json(data, status)

    @register.api(method="PUT", path="/kbs/{kb_id}/recall", auth=True, summary="Update KB recall settings")
    async def api_update_kb_recall(self, kb_id: str, request: Request):
        body = await request.json()
        data, status = await api.update_kb_recall(self.kb_manager, kb_id, body)
        return _json(data, status)

    @register.api(method="GET", path="/kbs/{kb_id}/documents/{doc_id}/recall", auth=True, summary="Get entry recall settings")
    async def api_get_document_recall(self, kb_id: str, doc_id: str):
        data, status = await api.get_document_recall(self.kb_manager, kb_id, doc_id)
        return _json(data, status)

    @register.api(method="PUT", path="/kbs/{kb_id}/documents/{doc_id}/recall", auth=True, summary="Update entry recall settings")
    async def api_update_document_recall(self, kb_id: str, doc_id: str, request: Request):
        body = await request.json()
        data, status = await api.update_document_recall(self.kb_manager, kb_id, doc_id, body)
        return _json(data, status)

    # ---- Versions ----

    @register.api(method="GET", path="/kbs/{kb_id}/versions", auth=True, summary="List versions")
    async def api_list_versions(self, kb_id: str):
        data, status = await api.list_versions(self.kb_manager, kb_id)
        return _json(data, status)

    @register.api(method="POST", path="/kbs/{kb_id}/versions", auth=True, summary="Create version")
    async def api_create_version(self, kb_id: str, request: Request):
        body = await request.json()
        data, status = await api.create_version(self.kb_manager, kb_id, body)
        return _json(data, status)

    @register.api(method="POST", path="/kbs/{kb_id}/versions/{version_id}/activate", auth=True, summary="Activate version")
    async def api_activate_version(self, kb_id: str, version_id: str):
        data, status = await api.activate_version(self.kb_manager, kb_id, version_id)
        return _json(data, status)

    @register.api(method="DELETE", path="/kbs/{kb_id}/versions/{version_id}", auth=True, summary="Delete version")
    async def api_delete_version(self, kb_id: str, version_id: str):
        data, status = await api.delete_version(self.kb_manager, kb_id, version_id)
        return _json(data, status)

    @register.api(method="GET", path="/kbs/{kb_id}/versions/{version_id}/freshness", auth=True, summary="Version freshness")
    async def api_version_freshness(self, kb_id: str, version_id: str):
        data, status = await api.version_freshness(self.kb_manager, kb_id, version_id)
        return _json(data, status)

    @register.api(method="POST", path="/kbs/{kb_id}/versions/{version_id}/sync", auth=True, summary="Sync version")
    async def api_sync_version(self, kb_id: str, version_id: str, request: Request):
        body = await request.json()
        data, status = await api.sync_version(self.kb_manager, kb_id, version_id, body)
        return _json(data, status)

    # ---- Documents ----

    @register.api(method="GET", path="/kbs/{kb_id}/documents", auth=True, summary="List documents")
    async def api_list_documents(self, kb_id: str):
        data, status = await api.list_documents(self.kb_manager, kb_id)
        return _json(data, status)

    @register.api(method="POST", path="/kbs/{kb_id}/documents", auth=True, summary="Upload document")
    async def api_upload_document(self, kb_id: str, request: Request):
        form = await request.form()
        file = form.get("file")
        if not file:
            return {"error": "No file"}
        content = await file.read()
        data, status = await api.upload_document(self.kb_manager, kb_id, file.filename, content)
        return _json(data, status)

    @register.api(method="GET", path="/kbs/{kb_id}/documents/deleted", auth=True, summary="List deleted documents")
    async def api_list_deleted_documents(self, kb_id: str):
        data, status = await api.list_deleted_documents(self.kb_manager, kb_id)
        return _json(data, status)

    @register.api(method="GET", path="/kbs/{kb_id}/documents/{doc_id}", auth=True, summary="Get document")
    async def api_get_document(self, kb_id: str, doc_id: str):
        data, status = await api.get_document(self.kb_manager, kb_id, doc_id)
        return _json(data, status)

    @register.api(method="PUT", path="/kbs/{kb_id}/documents/{doc_id}", auth=True, summary="Update document")
    async def api_update_document(self, kb_id: str, doc_id: str, request: Request):
        body = await request.json()
        data, status = await api.update_document(self.kb_manager, kb_id, doc_id, body)
        return _json(data, status)

    @register.api(method="DELETE", path="/kbs/{kb_id}/documents/{doc_id}", auth=True, summary="Delete document")
    async def api_delete_document(self, kb_id: str, doc_id: str):
        data, status = await api.delete_document(self.kb_manager, kb_id, doc_id)
        return _json(data, status)

    @register.api(method="POST", path="/kbs/{kb_id}/documents/{doc_id}/restore", auth=True, summary="Restore document")
    async def api_restore_document(self, kb_id: str, doc_id: str):
        data, status = await api.restore_document(self.kb_manager, kb_id, doc_id)
        return _json(data, status)

    # ---- Revision history ----

    @register.api(method="GET", path="/kbs/{kb_id}/documents/{doc_id}/history", auth=True, summary="List entry revisions")
    async def api_list_history(self, kb_id: str, doc_id: str):
        data, status = await api.list_history(self.kb_manager, kb_id, doc_id)
        return _json(data, status)

    @register.api(method="GET", path="/kbs/{kb_id}/documents/{doc_id}/history/{rev_id}", auth=True, summary="Read entry revision")
    async def api_get_history_revision(self, kb_id: str, doc_id: str, rev_id: str):
        data, status = await api.get_history_revision(self.kb_manager, kb_id, doc_id, rev_id)
        return _json(data, status)

    @register.api(method="POST", path="/kbs/{kb_id}/documents/{doc_id}/history/{rev_id}/restore", auth=True, summary="Restore entry revision")
    async def api_restore_history_revision(self, kb_id: str, doc_id: str, rev_id: str):
        data, status = await api.restore_history_revision(self.kb_manager, kb_id, doc_id, rev_id)
        return _json(data, status)

    # ---- Search ----

    @register.api(method="POST", path="/kbs/{kb_id}/search", auth=True, summary="Search KB")
    async def api_search(self, kb_id: str, request: Request):
        body = await request.json()
        data, status = await api.search(self.kb_manager, kb_id, body)
        return _json(data, status)

    # ---- Tasks ----

    @register.api(method="GET", path="/tasks", auth=True, summary="List tasks")
    async def api_list_tasks(self):
        data, status = api.list_tasks()
        return _json(data, status)

    @register.api(method="GET", path="/tasks/{task_id}", auth=True, summary="Get task")
    async def api_get_task(self, task_id: str):
        data, status = api.get_task(task_id)
        return _json(data, status)

    @register.api(method="GET", path="/kbs/{kb_id}/tasks", auth=True, summary="List tasks for KB")
    async def api_list_kb_tasks(self, kb_id: str):
        data, status = api.list_tasks(kb_id)
        return _json(data, status)


class DummyEmbeddingClient:
    """Fallback client used when no embedding model is configured."""

    is_dummy = True

    async def embed(self, texts: List[str]) -> List[List[float]]:
        import numpy as np
        return [np.random.rand(384).tolist() for _ in texts]
