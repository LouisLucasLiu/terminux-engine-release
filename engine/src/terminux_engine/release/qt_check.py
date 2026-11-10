# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""讀取外部 Qt 安裝內容，並執行不連線的 GUI 自我檢查。"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import subprocess
from importlib.resources import files
from pathlib import Path


def _environment() -> dict[str, str]:
    return {
        key: value for key, value in os.environ.items()
        if not key.startswith(("QT", "QML")) and key not in {"LD_LIBRARY_PATH", "LD_PRELOAD"}
    } | {"LC_ALL": "C.UTF-8"}


def _run(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(command, env=env, capture_output=True, text=True,
                              errors="replace", timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"[失敗] 無法完成 {command[0]}：{error}")
        return None


def _version(qt_dir: Path, minimum: str, major: int) -> bool:
    directory = qt_dir / "lib/cmake/Qt6"
    for name in ("Qt6ConfigVersion.cmake", "Qt6ConfigVersionImpl.cmake"):
        path = directory / name
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            print(f"[失敗] 無法讀取 Qt 版本：{path}：{error}")
            return False
        match = re.search(r'set\(\s*PACKAGE_VERSION\s+"(\d+\.\d+\.\d+)"\s*\)', content)
        if match:
            found = tuple(int(part) for part in match[1].split("."))
            required = tuple(int(part) for part in minimum.split("."))
            if found[0] == major and found >= required:
                print(f"[通過] Qt {match[1]}（需求 Qt {major}.x ≥ {minimum}）")
                return True
            print(f"[失敗] Qt {match[1]} 不符合 Qt {major}.x ≥ {minimum}")
            return False
    print(f"[失敗] 找不到可判讀的 Qt 版本：{directory}")
    return False


def _files_exist(qt_dir: Path, paths: list[str], label: str) -> bool:
    missing = [path for path in paths if not (qt_dir / path).is_file()]
    for path in missing:
        print(f"[失敗] 缺少 {label}：{qt_dir / path}")
    if not missing:
        print(f"[通過] {label}（{len(paths)} 項）")
    return not missing


def _xcb(plugin: Path, hints: dict[str, dict[str, str]], env: dict[str, str]) -> bool:
    if not plugin.is_file():
        return False
    result = _run(["ldd", str(plugin)], env | {"LC_ALL": "C"})
    if result is None:
        return False
    output = result.stdout + result.stderr
    missing = sorted(set(re.findall(r"^\s*(\S+)\s+=>\s+not found", output, re.MULTILINE)))
    for soname in missing:
        packages = hints.get(soname)
        if packages:
            print(f"[失敗] 缺少系統函式庫 {soname}；EL9：{packages['el9']}；"
                  f"Ubuntu 24.04：{packages['ubuntu_24_04']}。請系統管理員安裝符合此 SONAME 的套件。")
        elif soname.startswith("libQt6"):
            print(f"[失敗] xcb plugin 缺少 Qt 函式庫 {soname}，請確認 Qt 安裝完整且版本一致。")
        else:
            print(f"[失敗] 缺少函式庫 {soname}；尚無 EL9／Ubuntu 24.04 套件對照，"
                  "請系統管理員查詢提供相同 SONAME 的套件或確認 Qt SDK 的附帶函式庫。")
    if result.returncode or "not found" in output:
        if not missing or result.stderr:
            print(output.strip())
        return False
    print("[通過] xcb plugin 的 ldd 無缺少函式庫")
    return True


def _prefix(prefix: Path, qt_dir: Path, env: dict[str, str]) -> bool:
    link = prefix / "qt"
    if not link.is_symlink() or link.resolve() != qt_dir.resolve():
        print(f"[失敗] {link} 必須是指向 {qt_dir} 的有效 symlink")
        return False
    conf = configparser.ConfigParser(interpolation=None)
    try:
        with (prefix / "bin/qt.conf").open(encoding="utf-8") as stream:
            conf.read_file(stream)
        configured = prefix / "bin" / conf.get("Paths", "Prefix")
        if configured.resolve() != qt_dir.resolve():
            raise ValueError("Prefix 未指向指定 Qt 目錄")
        for key, default in (("Plugins", "plugins"), ("QmlImports", "qml")):
            if conf.get("Paths", key, fallback=default) != default:
                raise ValueError(f"{key} 必須使用相對路徑 {default}")
        if conf.has_option("Paths", "Qml2Imports"):
            raise ValueError("請使用 QmlImports 或省略此 key，避免舊版 fallback 路徑")
        if (prefix / "bin/qt6.conf").exists():
            raise ValueError("qt6.conf 會覆蓋 qt.conf，請確認安裝配置")
    except (OSError, configparser.Error, ValueError) as error:
        print(f"[失敗] qt.conf 配置：{error}")
        return False
    print("[通過] qt symlink 與 qt.conf")
    ok = True
    for name in ("terminux", "dqe_viewer"):
        result = _run([str(prefix / "bin" / name), "--self-check"],
                      env | {"QT_QPA_PLATFORM": "offscreen"})
        if result is None:
            ok = False
            continue
        output = (result.stdout + result.stderr).strip()
        print(f"[{'通過' if result.returncode == 0 else '失敗'}] {name} --self-check "
              f"exit={result.returncode}")
        if output:
            print(output)
        ok = result.returncode == 0 and ok
    return ok


def main(argv: list[str] | None = None) -> int:
    """檢查外部 Qt 的版本、資源與安裝 prefix；不安裝或修改系統。"""
    parser = argparse.ArgumentParser(prog="terminux-qt-check", description=__doc__)
    parser.add_argument("--qt-dir", type=Path, required=True)
    parser.add_argument("--prefix", type=Path)
    args = parser.parse_args(argv)
    qt_dir = args.qt_dir.expanduser().absolute()
    if not qt_dir.is_dir():
        print(f"[失敗] Qt 目錄不存在或 symlink 目標無效：{qt_dir}")
        return 1
    try:
        manifest = json.loads(files("terminux_engine.resources").joinpath("qt_runtime.json")
                              .read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"[失敗] 無法讀取 Qt 需求清單：{error}")
        return 1
    env = _environment()
    ok = _version(qt_dir, manifest["minimum_qt_version"], manifest["qt_major_version"])
    checks = [
        (["lib/" + name for name in manifest["libraries"]], "Qt 函式庫"),
        ([item["path"] + "/qmldir" for item in manifest["qml_imports"]], "QML import"),
        ([item["plugin"] for item in manifest["qml_imports"] if "plugin" in item], "QML plugin"),
        (list(manifest["platform_plugins"].values()), "平台 plugin"),
    ]
    for paths, label in checks:
        ok = _files_exist(qt_dir, paths, label) and ok
    ok = _xcb(qt_dir / manifest["platform_plugins"]["xcb"],
              manifest["system_package_hints"], env) and ok
    if args.prefix is not None:
        ok = _prefix(args.prefix.expanduser().absolute(), qt_dir, env) and ok
    print("Qt 安裝檢查通過。" if ok else "Qt 安裝檢查失敗；請修正上述項目後重試。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
