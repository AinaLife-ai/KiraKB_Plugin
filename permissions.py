"""Write-permission policy resolution for KiraKB.

The policy model is intentionally small and explicit:

* operations: ``add`` (create an entry), ``edit`` (change an existing one),
  ``delete`` (remove one)
* scopes: the knowledge base (defaults) and a single entry (override)
* policies: ``open``, ``whitelist``, ``admin``, ``off`` (entries may also
  ``inherit`` the knowledge base value)

The defaults reproduce the pre-1.2 behaviour exactly: adding and editing were
unrestricted, deleting was limited to ``owner_whitelist`` (an empty list meant
"no hard restriction").

This module is pure Python on purpose: every rule is unit-testable without a
running KiraAI instance.
"""

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

OP_ADD = "add"
OP_EDIT = "edit"
OP_DELETE = "delete"
OPS: Tuple[str, ...] = (OP_ADD, OP_EDIT, OP_DELETE)

POLICY_OPEN = "open"
POLICY_WHITELIST = "whitelist"
POLICY_ADMIN = "admin"
POLICY_OFF = "off"
POLICY_INHERIT = "inherit"

SCOPE_POLICIES: Tuple[str, ...] = (
    POLICY_OPEN,
    POLICY_WHITELIST,
    POLICY_ADMIN,
    POLICY_OFF,
)
ENTRY_POLICIES: Tuple[str, ...] = SCOPE_POLICIES + (POLICY_INHERIT,)

DEFAULT_POLICIES: Dict[str, str] = {
    OP_ADD: POLICY_OPEN,
    OP_EDIT: POLICY_OPEN,
    OP_DELETE: POLICY_WHITELIST,
}

ACTOR_AI = "ai"
ACTOR_HUMAN = "human"

REASON_TEXTS = {
    POLICY_ADMIN: "该操作被设置为仅限人工（WebUI）修改，AI 工具不能执行。",
    POLICY_OFF: "该操作已被冻结，需要先在前端解除冻结。",
    POLICY_WHITELIST: "当前用户不在允许名单内。",
}


def normalise_policy(value: Any, *, allow_inherit: bool = False,
                     default: Optional[str] = None) -> Optional[str]:
    """Return a canonical policy string, or ``default`` when invalid."""
    if not isinstance(value, str):
        return default
    value = value.strip().lower()
    valid = ENTRY_POLICIES if allow_inherit else SCOPE_POLICIES
    return value if value in valid else default


def normalise_users(value: Any) -> List[str]:
    """Normalise a user-id list: strings only, trimmed, de-duplicated."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    out: List[str] = []
    seen = set()
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def normalise_permissions(raw: Any, *, for_entry: bool = False) -> Optional[Dict[str, Any]]:
    """Normalise a stored ``permissions`` object.

    Returns ``None`` when the object is absent or not a dict, which callers use
    as the signal "this scope was never configured -> legacy behaviour".
    """
    if not isinstance(raw, dict):
        return None
    valid = ENTRY_POLICIES if for_entry else SCOPE_POLICIES
    out: Dict[str, Any] = {}
    for op in OPS:
        if op not in raw:
            continue
        policy = raw.get(op)
        if isinstance(policy, str) and policy.strip().lower() in valid:
            out[op] = policy.strip().lower()
    if "allow_users" in raw:
        out["allow_users"] = normalise_users(raw.get("allow_users"))
    return out


def permissions_configured(raw: Any) -> bool:
    """True when a scope carries an explicit policy object."""
    return isinstance(raw, dict) and any(op in raw for op in OPS)


def _scope_users(scope_perms: Optional[Dict[str, Any]]) -> List[str]:
    if not scope_perms:
        return []
    return normalise_users(scope_perms.get("allow_users"))


def resolve_policy(op: str, entry_perms: Optional[Dict[str, Any]],
                   kb_perms: Optional[Dict[str, Any]],
                   defaults: Optional[Dict[str, str]] = None) -> Tuple[str, str]:
    """Resolve the effective policy and where it came from.

    Returns ``(policy, source)`` where source is ``entry`` / ``kb`` / ``default``.
    """
    if entry_perms:
        value = entry_perms.get(op)
        if isinstance(value, str) and value in SCOPE_POLICIES:
            return value, "entry"
    if kb_perms:
        value = kb_perms.get(op)
        if isinstance(value, str) and value in SCOPE_POLICIES:
            return value, "kb"
    base = defaults or DEFAULT_POLICIES
    return normalise_policy(base.get(op), default=POLICY_OPEN) or POLICY_OPEN, "default"


def _user_allowed(policy: str, user_ids: Sequence[str],
                  scope_users: Sequence[str], global_whitelist: Sequence[str]) -> bool:
    if policy != POLICY_WHITELIST:
        return True
    # Scope list wins when present; the global list is only a fallback. This is
    # what makes a per-KB or per-entry list a real narrowing instrument.
    scoped = normalise_users(scope_users)
    candidates = scoped if scoped else normalise_users(global_whitelist)
    # Empty list == no hard restriction (pre-1.2 behaviour). The UI states this
    # explicitly next to the input field.
    if not candidates:
        return True
    wanted = set(candidates)
    return any(str(uid) in wanted for uid in user_ids if uid)


def decide(*, op: str, actor: str = ACTOR_AI,
           user_ids: Optional[Iterable[str]] = None,
           entry_perms: Optional[Dict[str, Any]] = None,
           kb_perms: Optional[Dict[str, Any]] = None,
           defaults: Optional[Dict[str, str]] = None,
           entry_users: Optional[Sequence[str]] = None,
           kb_users: Optional[Sequence[str]] = None,
           global_whitelist: Optional[Sequence[str]] = None) -> Tuple[bool, str, str]:
    """Evaluate one write operation.

    ``user_ids`` are every distinct non-bot sender in the current message
    batch: authorization succeeds when *any* of them is allowed (batch-level
    check, deliberately not restricted to the last speaker).

    Returns ``(allowed, reason, source)``.
    """
    op = (op or "").strip().lower()
    if op not in OPS:
        return False, "未知操作。", "unknown"
    policy, source = resolve_policy(op, entry_perms, kb_perms, defaults)

    if policy == POLICY_OPEN:
        return True, "", source

    if actor == ACTOR_HUMAN:
        if policy == POLICY_OFF:
            return False, REASON_TEXTS[POLICY_OFF], source
        return True, "", source

    if policy in (POLICY_ADMIN, POLICY_OFF):
        return False, REASON_TEXTS[policy], source

    users = list(user_ids or [])
    if entry_users is None:
        entry_users = _scope_users(entry_perms)
    if kb_users is None:
        kb_users = _scope_users(kb_perms)
    scoped = entry_users if source == "entry" else kb_users
    if not _user_allowed(policy, users, scoped or [], global_whitelist or []):
        return False, REASON_TEXTS[POLICY_WHITELIST], source
    return True, "", source


def describe(permissions: Optional[Dict[str, Any]], op: str,
             defaults: Optional[Dict[str, str]] = None) -> str:
    """Human-readable summary used by tools and the WebUI."""
    policy, source = resolve_policy(op, None, permissions, defaults)
    names = {
        POLICY_OPEN: "任何人",
        POLICY_WHITELIST: "名单用户",
        POLICY_ADMIN: "仅人工",
        POLICY_OFF: "已冻结",
    }
    label = names.get(policy, policy)
    if source == "default":
        return f"{label}（默认）"
    return label
