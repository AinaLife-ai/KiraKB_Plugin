"""Shared API handlers for KiraKB.

Used by both the sidebar mode (@register.api in main.py) and the standalone
WebUI mode (Starlette routes in web_server.py).
"""

import asyncio
import tempfile
from pathlib import Path
from typing import Optional

from core.logging_manager import get_logger

from . import permissions as perms
from .task_manager import get_task_manager
from .document_parser import DocumentParser
from .kb_manager import EmbeddingDimensionError, content_hash

logger = get_logger("kirakb_api", "cyan")

# Keep references to background tasks so they are not garbage collected
_bg_tasks: set = set()


def _spawn(coro):
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


def _resolve_names(kb) -> dict:
    """doc_id -> current display name (entries may be renamed)."""
    return {doc["doc_id"]: doc["name"] for doc in kb.list_documents(include_deleted=True)}


def _decorate_results(kb, results):
    names = _resolve_names(kb)
    out = []
    for item in results:
        doc_id = str((item.get("metadata") or {}).get("doc_id") or "")
        row = dict(item)
        row["doc_id"] = doc_id
        row["doc_name"] = names.get(doc_id) or item.get("doc_name") or doc_id
        out.append(row)
    return out


def _human_write_allowed(mgr, kb, entry, op: str):
    """The WebUI is the human operator, but a frozen policy blocks it too."""
    defaults = getattr(mgr, "default_policies", None) or perms.DEFAULT_POLICIES
    return perms.decide(
        op=op,
        actor=perms.ACTOR_HUMAN,
        entry_perms=(entry or {}).get("permissions") if entry else None,
        kb_perms=kb.get_permissions(),
        defaults=defaults,
    )


# ========== Runtime settings (hot apply) ==========

def get_settings(mgr):
    getter = getattr(mgr, "settings_getter", None)
    if not callable(getter):
        return {"error": "settings unavailable"}, 503
    return getter(), 200


def update_settings(mgr, body: dict):
    setter = getattr(mgr, "settings_setter", None)
    if not callable(setter):
        return {"error": "settings unavailable"}, 503
    try:
        return setter(body or {}), 200
    except ValueError as exc:
        return {"error": str(exc)}, 400


# ========== Knowledge base management ==========

def list_kbs(mgr):
    kbs = []
    for kb_id, kb in mgr.kbs.items():
        kbs.append({
            "kb_id": kb_id,
            "display_name": kb.display_name,
            "description": kb.description,
            "version_count": len(kb._versions),
            "entry_count": len(kb.list_documents(include_deleted=False)),
            "active_version": kb._current_version_id,
            "permissions": kb.get_permissions(),
            "recall": kb.get_recall_settings(),
            "history_keep": kb.get_history_keep(),
        })
    return kbs, 200


async def create_kb(mgr, kb_id: str):
    if not kb_id:
        return {"error": "kb_id required"}, 400
    try:
        await mgr.create_kb(kb_id)
        return {"ok": True}, 200
    except Exception as e:
        logger.exception("Create KB failed")
        return {"error": str(e)}, 500


async def delete_kb(mgr, kb_id: str):
    try:
        await mgr.delete_kb(kb_id)
        return {"ok": True}, 200
    except Exception as e:
        logger.exception("Delete KB failed")
        return {"error": str(e)}, 500


async def get_kb_info(mgr, kb_id: str):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    return {
        "kb_id": kb_id,
        "display_name": kb.display_name,
        "description": kb.description,
        "active_version": kb._current_version_id,
        "entry_count": len(kb.list_documents(include_deleted=False)),
        "permissions": kb.get_permissions(),
        "recall": kb.get_recall_settings(),
        "history_keep": kb.get_history_keep(),
    }, 200


async def update_kb_info(mgr, kb_id: str, body: dict):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    if "display_name" in body:
        kb.info["display_name"] = body["display_name"]
    if "description" in body:
        kb.info["description"] = body["description"]
    if "history_keep" in body:
        try:
            await kb.set_history_keep(body.get("history_keep"))
        except ValueError as exc:
            return {"error": str(exc)}, 400
    kb._save_info()
    return {"ok": True}, 200


# ========== Permissions ==========

async def get_kb_permissions(mgr, kb_id: str):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    return {
        "permissions": kb.get_permissions(),
        "defaults": perms.DEFAULT_POLICIES,
        "policies": list(perms.SCOPE_POLICIES),
    }, 200


async def update_kb_permissions(mgr, kb_id: str, body: dict):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    saved = await kb.set_permissions(body or {})
    return {"ok": True, "permissions": saved}, 200


async def get_document_permissions(mgr, kb_id: str, doc_id: str):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    if not (kb.raw_docs_dir / f"{doc_id}.txt").exists():
        return {"error": "Document not found"}, 404
    meta = kb.get_document_settings(doc_id)
    return {
        "doc_id": doc_id,
        "permissions": meta["permissions"],
        "recall": meta["recall"],
        "policies": list(perms.ENTRY_POLICIES),
    }, 200


async def update_document_permissions(mgr, kb_id: str, doc_id: str, body: dict):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    if not (kb.raw_docs_dir / f"{doc_id}.txt").exists():
        return {"error": "Document not found"}, 404
    saved = await kb.update_document_settings(doc_id, {"permissions": body or {}})
    return {"ok": True, **saved}, 200


async def update_documents_permissions(mgr, kb_id: str, body: dict):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    doc_ids = body.get("doc_ids") or []
    patch = body.get("permissions") or {}
    saved = 0
    for doc_id in doc_ids:
        if (kb.raw_docs_dir / f"{doc_id}.txt").exists():
            await kb.update_document_settings(str(doc_id), {"permissions": patch})
            saved += 1
    return {"ok": True, "updated": saved}, 200


# ========== Passive recall settings ==========

def get_recall_global(mgr):
    """Read-only view of the plugin-level master switch for the WebUI."""
    cfg = getattr(mgr, "recall_global", None) or {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "mode": cfg.get("mode", "hybrid"),
        "top_k": cfg.get("top_k", 3),
        "min_score": cfg.get("min_score", 0.35),
        "max_chars": cfg.get("max_chars", 1200),
    }, 200


async def get_kb_recall(mgr, kb_id: str):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    return {"recall": kb.get_recall_settings()}, 200


async def update_kb_recall(mgr, kb_id: str, body: dict):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    saved = await kb.set_recall_settings(body or {})
    return {"ok": True, "recall": saved}, 200


async def get_document_recall(mgr, kb_id: str, doc_id: str):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    meta = kb.get_document_settings(doc_id)
    return {"doc_id": doc_id, "recall": meta["recall"]}, 200


async def update_document_recall(mgr, kb_id: str, doc_id: str, body: dict):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    saved = await kb.update_document_settings(doc_id, {"recall": body or {}})
    return {"ok": True, **saved}, 200


# ========== Version management ==========

async def list_versions(mgr, kb_id: str):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    versions = []
    for ver_id, ver in kb._versions.items():
        stale = kb.version_staleness(ver_id)
        versions.append({
            "version_id": ver_id,
            "model_name": ver.model_name,
            "model_uuid": ver.model_uuid,
            "dimension": ver.dimension,
            "created_at": ver.created_at,
            "status": ver.status,
            "is_active": ver_id == kb._current_version_id,
            "outdated": len(stale["missing"]) + len(stale["stale"]),
        })
    return versions, 200


async def activate_version(mgr, kb_id: str, version_id: str):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    ver = kb._versions.get(version_id)
    if ver is None:
        return {"error": "Version not found"}, 404
    if ver.status != "ready":
        return {"error": "该版本不完整（建库时失败），无法激活。请删除后重建。"}, 400
    success = await kb.set_active_version(version_id)
    if not success:
        return {"error": "Version not found"}, 404
    return {"ok": True}, 200


async def delete_version(mgr, kb_id: str, version_id: str):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    success = await kb.delete_version(version_id)
    if not success:
        return {"error": "Cannot delete active version or version not found"}, 400
    return {"ok": True}, 200


async def create_version(mgr, kb_id: str, body: dict):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    model_name = body.get("model_name")
    dimension = body.get("dimension")
    doc_ids = body.get("doc_ids")
    model_uuid = body.get("model_uuid") or None
    if not model_name:
        return {"error": "model_name required"}, 400
    try:
        dimension = int(dimension)
    except (TypeError, ValueError):
        return {"error": "维度必须是正整数。可点击“探测维度”自动获取。"}, 400
    if dimension <= 0:
        return {"error": "维度必须大于 0。可点击“探测维度”自动获取。"}, 400

    task_mgr = get_task_manager()
    total = len(doc_ids) if doc_ids else len(kb.list_documents(include_deleted=False))
    label = f"创建版本 {model_name}"
    task_id = task_mgr.create_task(kb_id, label, total_steps=total)

    async def run_with_progress(progress_callback):
        version_id = await kb.create_version(
            model_name, dimension, doc_ids, progress_callback, model_uuid
        )
        return {"version_id": version_id}

    _spawn(task_mgr.run_task(task_id, run_with_progress))
    return {"task_id": task_id}, 200


async def version_freshness(mgr, kb_id: str, version_id: str):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    if version_id not in kb._versions:
        return {"error": "Version not found"}, 404
    return kb.version_staleness(version_id), 200


async def sync_version(mgr, kb_id: str, version_id: str, body: dict):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    if version_id not in kb._versions:
        return {"error": "Version not found"}, 404
    doc_ids = body.get("doc_ids") if isinstance(body, dict) else None
    task_mgr = get_task_manager()
    freshness = kb.version_staleness(version_id)
    total = len(doc_ids) if doc_ids else len(freshness["missing"]) + len(freshness["stale"])
    task_id = task_mgr.create_task(kb_id, f"同步版本 {version_id}", total_steps=max(1, total))

    async def run_with_progress(progress_callback):
        return await kb.sync_version(version_id, doc_ids, progress_callback)

    _spawn(task_mgr.run_task(task_id, run_with_progress))
    return {"task_id": task_id}, 200


# ========== Document management ==========

async def list_documents(mgr, kb_id: str):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    return kb.list_documents(include_deleted=False), 200


async def list_deleted_documents(mgr, kb_id: str):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    return kb.get_deleted_documents(), 200


async def restore_document(mgr, kb_id: str, doc_id: str):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    doc = kb.find_document(doc_id, include_deleted=True)
    if not doc:
        return {"error": "Document not found"}, 404
    allowed, reason, _ = _human_write_allowed(mgr, kb, doc, "delete")
    if not allowed:
        return {"error": reason}, 403
    ok, detail = await kb.restore_document(doc_id)
    if not ok:
        return {"error": detail.get("error") or "Restore failed", "detail": detail}, 500
    return {"ok": True, "detail": detail}, 200


async def get_document(mgr, kb_id: str, doc_id: str):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    content = await kb.get_raw_document(doc_id)
    if content is None:
        return {"error": "Document not found"}, 404
    meta = kb.get_document_settings(doc_id)
    doc = kb.find_document(doc_id, include_deleted=True) or {}
    return {
        "doc_id": doc_id,
        "name": doc.get("name") or doc_id,
        "content": content,
        **meta,
    }, 200


async def update_document(mgr, kb_id: str, doc_id: str, body: dict):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    new_content = body.get("content")
    if new_content is None:
        return {"error": "content required"}, 400
    new_name = body.get("name")
    if new_name is not None:
        new_name = str(new_name).strip()
        if not new_name:
            return {"error": "name cannot be empty"}, 400
    doc = kb.find_document(doc_id, include_deleted=True)
    if doc and doc.get("deleted"):
        return {"error": "条目已删除，请先恢复后再编辑。"}, 400
    allowed, reason, _ = _human_write_allowed(mgr, kb, doc, "edit")
    if not allowed:
        return {"error": reason}, 403
    success = await kb.update_raw_document(doc_id, new_content, original_name=new_name)
    if not success:
        return {"error": "Document not found"}, 404

    active_ver = await kb.get_active_version()
    if active_ver:
        await active_ver.delete_document(doc_id)
        await kb._vectorize_document(active_ver, doc_id, new_content)
    return {"ok": True, "message": "文档已更新并重新向量化"}, 200


async def delete_document(mgr, kb_id: str, doc_id: str):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    doc = kb.find_document(doc_id, include_deleted=True)
    if not doc:
        return {"error": "Document not found"}, 404
    allowed, reason, _ = _human_write_allowed(mgr, kb, doc, "delete")
    if not allowed:
        return {"error": reason}, 403
    success = await kb.delete_raw_document(doc_id, soft=True)
    if not success:
        return {"error": "Document not found"}, 404
    return {"ok": True}, 200


async def upload_document(mgr, kb_id: str, filename: str, content: bytes):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404

    allowed, reason, _ = _human_write_allowed(mgr, kb, None, "add")
    if not allowed:
        return {"error": reason}, 403

    filename = filename or "upload.txt"
    suffix = Path(filename).suffix.lower()
    text = None
    if suffix in (".txt", ".md", ".markdown"):
        text = content.decode("utf-8", errors="replace")
    else:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            text, _ = await DocumentParser.parse(tmp_path, kb.vlm_client)
        except Exception as e:
            logger.error(f"Parse failed for {filename}: {e}")
            return {"error": f"文件解析失败: {str(e)}"}, 400
        finally:
            try:
                Path(tmp_path).unlink()
            except Exception:
                pass
    if not text or not text.strip():
        return {"error": "文件内容为空"}, 400

    digest = content_hash(text)
    for doc in kb.list_documents(include_deleted=False):
        if doc.get("content_hash") == digest and doc.get("name") == filename:
            return {"ok": True, "doc_id": doc["doc_id"], "duplicate": True}, 200

    doc_id = await kb.add_raw_document(text, filename)
    active_ver = await kb.get_active_version()
    if active_ver:
        await kb._vectorize_document(active_ver, doc_id, text)
    return {"ok": True, "doc_id": doc_id}, 200


# ========== Revision history ==========

async def list_history(mgr, kb_id: str, doc_id: str):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    doc = kb.find_document(doc_id, include_deleted=True)
    if not doc:
        return {"error": "Document not found"}, 404
    return {
        "doc_id": doc_id,
        "name": doc.get("name"),
        "keep": kb.get_history_keep(),
        "revisions": kb.list_history(doc_id),
    }, 200


async def get_history_revision(mgr, kb_id: str, doc_id: str, rev_id: str):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    record = kb.get_revision(doc_id, rev_id)
    if not record:
        return {"error": "Revision not found"}, 404
    return record, 200


async def restore_history_revision(mgr, kb_id: str, doc_id: str, rev_id: str):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    doc = kb.find_document(doc_id, include_deleted=True)
    if not doc or doc.get("deleted"):
        return {"error": "条目不存在或已删除，请先恢复。"}, 400
    allowed, reason, _ = _human_write_allowed(mgr, kb, doc, "edit")
    if not allowed:
        return {"error": reason}, 403
    content = await kb.restore_revision(doc_id, rev_id, actor="human")
    if content is None:
        return {"error": "Revision not found"}, 404
    active = kb.active_version
    if active:
        await active.delete_document(doc_id)
        await kb._vectorize_document(active, doc_id, content)
    return {"ok": True, "rev_id": rev_id}, 200


# ========== Search ==========

async def search(mgr, kb_id: str, body: dict):
    kb = await mgr.get_kb(kb_id)
    if not kb:
        return {"error": "KB not found"}, 404
    query = body.get("query")
    if not query:
        return {"error": "query required"}, 400
    active_ver = await kb.get_active_version()
    if not active_ver:
        if any(v.status != "ready" for v in kb._versions.values()):
            return {"error": "当前没有可用的激活版本：已存在的版本都是不完整的建库残留，"
                             "请在版本管理中删除它们后重新创建版本。"}, 400
        return {"error": "该知识库还没有激活版本，请先创建并激活一个版本。"}, 400
    if active_ver.status != "ready":
        return {"error": "当前激活版本不完整（可能是一次失败的建库残留），请删除并重建该版本。"}, 400
    try:
        top_k = max(1, min(50, int(body.get("top_k", 5))))
    except (TypeError, ValueError):
        top_k = 5
    try:
        results = await active_ver.search(
            query,
            top_k=top_k,
            enable_hybrid=bool(body.get("enable_hybrid", mgr.enable_hybrid)),
        )
    except EmbeddingDimensionError as e:
        return {"error": str(e)}, 400
    except Exception as e:
        detail = str(e).strip() or type(e).__name__
        logger.error(f"Search failed for KB {kb_id}: {detail}", exc_info=True)
        return {"error": f"检索失败：{detail}"}, 500
    return _decorate_results(kb, results), 200


# ========== Tasks ==========

def get_task(task_id: str):
    task_mgr = get_task_manager()
    task = task_mgr.get_task(task_id)
    if not task:
        return {"error": "Task not found"}, 404
    return task.to_dict(), 200


def list_tasks(kb_id: Optional[str] = None):
    task_mgr = get_task_manager()
    if kb_id:
        tasks = task_mgr.get_tasks_for_kb(kb_id)
    else:
        tasks = [t.to_dict() for t in task_mgr.tasks.values()]
    return tasks[-200:], 200


# ========== Embedding models / dimension probing ==========

def embedding_models(mgr, default_uuid: Optional[str] = None) -> dict:
    lister = getattr(mgr, "model_lister", None)
    models = []
    if lister:
        try:
            models = lister() or []
        except Exception as e:
            logger.warning(f"Failed to list embedding models: {e}")
    default_available = False
    resolver = getattr(mgr, "client_resolver", None)
    if resolver:
        try:
            default_available = resolver(None) is not None
        except Exception:
            default_available = False
    return {
        "models": models,
        "default_uuid": default_uuid,
        "default_available": default_available,
    }


async def probe_embedding(mgr, model_uuid: Optional[str] = None) -> dict:
    resolver = getattr(mgr, "client_resolver", None)
    client = None
    if resolver:
        try:
            client = resolver(model_uuid or None)
        except Exception as e:
            return {"error": f"无法加载该嵌入模型：{e}"}
    if client is None:
        return {"error": "没有可用的嵌入模型。请先在 KiraAI 主系统中配置 default_embedding。"}
    try:
        vectors = await client.embed(["dimension probe"])
    except Exception as e:
        return {"error": f"调用嵌入模型失败：{type(e).__name__}: {e}"}
    if not vectors or not vectors[0]:
        return {"error": "嵌入模型返回了空向量，请检查该模型配置。"}
    return {"dimension": len(vectors[0])}
