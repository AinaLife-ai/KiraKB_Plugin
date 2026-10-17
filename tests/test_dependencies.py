"""Dependency detection tests (no real pip installs)."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stub_core

stub_core.install()
import importlib

deps = importlib.import_module("kbp.dependencies")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


async def main():
    print("\nD1  模块检测")
    check("已安装模块可识别", deps.has_module("json") is True)
    check("缺失模块可识别", deps.has_module("kirakb_missing_module_xyz") is False)
    missing = deps.missing([("json", "json"), ("kirakb_missing_module_xyz", "x")])
    check("missing 只返回缺失项", missing == [("kirakb_missing_module_xyz", "x")], str(missing))

    print("\nD2  报告结构")
    report = deps.report()
    check("报告包含 core/default", set(report) == {"core", "default"}, str(report.keys()))

    print("\nD3  自动安装开关")
    deps.configure(False)
    check("关闭后不安装", await deps.ensure_optional("kirakb_missing_module_xyz", "x") is False)
    check("已存在模块不受影响", await deps.ensure_optional("json", "json") is True)
    deps.configure(True)

    print("\nD4  依赖清单")
    plugin = Path(__file__).resolve().parent.parent
    req = (plugin / "requirements.txt").read_text(encoding="utf-8")
    for pkg in ("faiss-cpu", "numpy", "aiofiles", "pypdf", "rank-bm25", "jieba"):
        check(f"requirements 包含 {pkg}", pkg in req)
    for pkg in ("markitdown", "openpyxl", "pdf2image"):
        check(f"requirements 包含默认运行时依赖 {pkg}", pkg in req)
    check("可选依赖文件已合并", not (plugin / "requirements-optional.txt").exists())

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print("\n" + "=" * 60)
    print(f"TOTAL: {passed}/{len(RESULTS)} passed")
    for name, detail in failed:
        print(f"  FAIL {name}  {detail}")
    print("=" * 60)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
