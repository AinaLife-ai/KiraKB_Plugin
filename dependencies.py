"""Dependency detection and optional auto-install.

KiraAI installs ``requirements.txt`` automatically when a plugin import fails.
This module adds the missing half: optional capabilities (Office parsing, PDF
OCR) are detected at runtime and installed in the background when they are
missing. Core dependencies are never installed here - if they are missing the
plugin cannot be imported in the first place; KiraAI's loader handles that.
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

OPTIONAL: Tuple[Tuple[str, str], ...] = (
    ("markitdown", "markitdown"),
    ("openpyxl", "openpyxl"),
    ("pdf2image", "pdf2image"),
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
    return {"core": missing(CORE), "optional": missing(OPTIONAL)}


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
    """Ensure an optional module is importable, installing it if allowed."""
    if has_module(module):
        return True
    if not _AUTO_INSTALL:
        return False
    package = package or module
    lock = _LOCKS.setdefault(module, asyncio.Lock())
    async with lock:
        if has_module(module):
            return True
        ok, detail = await asyncio.wait_for(
            _pip_install([package]), timeout=max(10, timeout + 10)
        )
        if not ok:
            return False
        return has_module(module)


async def preflight(logger=None, auto_install: Optional[bool] = None,
                    install_missing: bool = False) -> Dict[str, object]:
    """Log a dependency report; optionally install missing optional packages.

    Never installs core packages: the plugin is already running, so they are
    present; if they are not, KiraAI's loader reports the import failure.
    Optional packages are installed on first use (Office / PDF OCR) by
    ``ensure_optional``; startup only reports them to avoid blocking on pip.
    """
    if auto_install is not None:
        configure(auto_install, _MIRROR)
    state = report()
    core_missing = state["core"]
    opt_missing = state["optional"]
    if logger:
        if core_missing:
            logger.error(
                "[kiraKB] missing core dependencies: "
                + ", ".join(pkg for _, pkg in core_missing)
            )
        if opt_missing:
            logger.info(
                "[kiraKB] optional dependencies missing: "
                + ", ".join(pkg for _, pkg in opt_missing)
                + (" (auto-install enabled)" if _AUTO_INSTALL else "")
            )
    installed: List[str] = []
    failed: List[Dict[str, str]] = []
    if install_missing and _AUTO_INSTALL and opt_missing:
        for module, package in opt_missing:
            ok = await ensure_optional(module, package)
            if ok:
                installed.append(package)
                if logger:
                    logger.info(f"[kiraKB] optional dependency installed: {package}")
            else:
                failed.append({"module": module, "package": package})
                if logger:
                    logger.warning(
                        f"[kiraKB] optional dependency not installed: {package} "
                        f"(Office/OCR features stay disabled until it is available)"
                    )
    return {
        "core_missing": [pkg for _, pkg in core_missing],
        "optional_missing": [pkg for _, pkg in opt_missing],
        "installed": installed,
        "failed": failed,
    }
