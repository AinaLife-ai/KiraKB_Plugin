"""Dependency detection and auto-install.

KiraAI installs ``requirements.txt`` automatically when a plugin import fails.
This module adds startup detection plus background installation for the
runtime packages used by document parsing / OCR. Core dependencies are never
installed here - if they are missing the plugin cannot be imported in the first
place; KiraAI's loader handles that.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from typing import Dict, Iterable, List, Optional, Tuple

CORE: Tuple[Tuple[str, str], ...] = (
    ("faiss", "faiss-cpu"),
    ("numpy", "numpy"),
    ("aiofiles", "aiofiles"),
    ("pypdf", "pypdf"),
    ("rank_bm25", "rank-bm25"),
    ("jieba", "jieba"),
)

DEFAULT: Tuple[Tuple[str, str], ...] = (
    ("markitdown", "markitdown>=0.0.1a2"),
    ("openpyxl", "openpyxl>=3.1.0"),
    ("pdf2image", "pdf2image>=1.16.0"),
)

_AUTO_INSTALL = True
_MIRROR: Optional[str] = None
_LOCKS: Dict[str, asyncio.Lock] = {}
_INSTALL_TIMEOUT = 240


def configure(auto_install: bool = True, mirror: Optional[str] = None) -> None:
    global _AUTO_INSTALL, _MIRROR
    _AUTO_INSTALL = bool(auto_install)
    _MIRROR = mirror or None


def has_module(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:
        return False


def missing(packages: Iterable[Tuple[str, str]] = CORE) -> List[Tuple[str, str]]:
    return [(module, package) for module, package in packages if not has_module(module)]


def report() -> Dict[str, List[Tuple[str, str]]]:
    return {"core": missing(CORE), "default": missing(DEFAULT)}


async def _pip_install(packages: List[str]) -> Tuple[bool, str]:
    if not packages:
        return True, ""
    cmd = [sys.executable, "-m", "pip", "install", *packages]
    if _MIRROR:
        cmd.extend(["-i", _MIRROR])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_INSTALL_TIMEOUT)
        except asyncio.CancelledError:
            if proc.returncode is None:
                try:
                    proc.terminate()
                    await proc.wait()
                except Exception:
                    pass
            raise
        except asyncio.TimeoutError:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.communicate()
            return False, f"pip install timed out after {_INSTALL_TIMEOUT}s"
    except Exception as exc:  # noqa: BLE001
        return False, f"pip failed: {type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        detail = (stderr or b"").decode("utf-8", errors="replace").strip()
        return False, detail[-500:] or f"pip exited with {proc.returncode}"
    return True, ""


async def ensure_optional(module: str, package: Optional[str] = None,
                          timeout: int = _INSTALL_TIMEOUT) -> bool:
    """Ensure a runtime module is importable, installing it if allowed."""
    if has_module(module):
        return True
    if not _AUTO_INSTALL:
        return False
    spec = dict(DEFAULT).get(module) or package or module
    lock = _LOCKS.setdefault(module, asyncio.Lock())
    async with lock:
        if has_module(module):
            return True
        ok, detail = await asyncio.wait_for(
            _pip_install([spec]), timeout=max(10, timeout + 10)
        )
        if not ok:
            return False
        return has_module(module)


async def preflight(logger=None, auto_install: Optional[bool] = None,
                    install_missing: bool = False) -> Dict[str, object]:
    """Detect runtime dependencies; optionally install missing ones.

    Core packages are handled by KiraAI's requirements installer. MarkItDown /
    openpyxl / pdf2image are listed in requirements.txt and are also installed
    on first use (Office / OCR) by ``ensure_optional``. Startup only reports by
    default so a slow/blocked PyPI can never delay plugin load.
    """
    if auto_install is not None:
        configure(auto_install, _MIRROR)
    state = report()
    core_missing = state["core"]
    opt_missing = state["default"]
    if logger:
        if core_missing:
            logger.error(
                "[kiraKB] missing core dependencies: "
                + ", ".join(pkg for _, pkg in core_missing)
            )
        if opt_missing:
            logger.info(
                "[kiraKB] runtime dependencies missing: "
                + ", ".join(pkg for _, pkg in opt_missing)
                + (" (will install on first use)" if _AUTO_INSTALL else "")
            )
    installed: List[str] = []
    failed: List[Dict[str, str]] = []
    if install_missing and _AUTO_INSTALL and opt_missing:
        for module, package in opt_missing:
            ok = await ensure_optional(module, package)
            if ok:
                installed.append(package)
                if logger:
                    logger.info(f"[kiraKB] runtime dependency installed: {package}")
            else:
                failed.append({"module": module, "package": package})
                if logger:
                    logger.warning(
                        f"[kiraKB] runtime dependency not installed: {package} "
                        f"(Office/OCR features stay disabled until it is available)"
                    )
    return {
        "core_missing": [pkg for _, pkg in core_missing],
        "default_missing": [pkg for _, pkg in opt_missing],
        "installed": installed,
        "failed": failed,
    }
