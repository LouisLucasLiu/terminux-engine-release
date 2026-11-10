# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Terminux 可攜式安裝與移除；不修改使用者 shell 設定。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile

MARKER = "# Terminux portable launcher"


def _run(args: list[str | Path], **kwargs: object) -> None:
    subprocess.run(list(map(str, args)), check=True, **kwargs)


def _confirm(message: str, yes: bool) -> None:
    if not yes and input(message + " [yes/no]：").strip() != "yes":
        raise RuntimeError("已取消。")


def _home() -> Path:
    return Path(os.environ["HOME"]).absolute()


def _daemon_guard() -> None:
    homes = {_home(), Path(os.environ.get("TERMINUX_HOME_DIR", str(_home()))).absolute()}
    for home in homes:
        lock = home / ".terminux/run/owner.lock"
        if not os.path.lexists(lock):
            continue
        try:
            owner = json.loads((lock / "owner.json").read_text())
            node, pid = owner["nodename"], owner["pid"]
            if not isinstance(node, str) or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                raise ValueError("owner 欄位無效")
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise RuntimeError(f"無法確認 daemon 狀態：{lock}；請先人工確認 owner。") from error
        if node != (os.environ.get("TERMINUX_NODENAME") or socket.gethostname()):
            raise RuntimeError(f"owner 位於其他主機 {node}；請先到該主機以 Ctrl+C 停止 daemon，"
                               "若由服務管理，請用原服務管理方式停止；不要直接刪除活動 owner。")
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass
        raise RuntimeError(f"daemon 仍在執行（pid={pid}）；請先用原啟動終端的 Ctrl+C 或服務管理方式停止。")


def _prefix(raw: str) -> Path:
    prefix = Path(raw).expanduser().absolute()
    if prefix in {Path("/"), _home(), _home() / ".local", _home() / ".terminux"}:
        raise ValueError("安裝 prefix 不可是系統根目錄、HOME 或資料目錄。")
    return prefix.resolve()


def _version(raw: str) -> str:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[.-][A-Za-z0-9]+)*", raw):
        raise ValueError("版本格式無效。")
    return raw


def _preflight(prefix: Path, bundle: Path, force: bool, yes: bool) -> None:
    info = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            info[key] = shlex.split(value)[0] if value else ""
    distro, version = info.get("ID"), info.get("VERSION_ID", "")
    supported = ((distro == "ubuntu" and version == "24.04") or
                 (distro in {"rhel", "rocky", "almalinux", "centos"} and version.split(".")[0] == "9"))
    if not supported:
        message = f"不支援的平台：{info.get('PRETTY_NAME', distro)}。"
        if not force:
            raise RuntimeError(message + " 如需自行承擔相容性限制，請加 --force。")
        print("警告：" + message, flush=True)
    if platform.machine() != "x86_64":
        raise RuntimeError("只支援 x86_64。")
    libc = os.confstr("CS_GNU_LIBC_VERSION").split()[-1]
    if tuple(map(int, libc.split("."))) < (2, 34):
        raise RuntimeError(f"glibc {libc} 低於必要的 2.34。")
    ldconfig = shutil.which("ldconfig") or "/sbin/ldconfig"
    output = subprocess.run([ldconfig, "-p"], check=True, text=True, capture_output=True).stdout
    candidates = [line.split("=>")[-1].strip() for line in output.splitlines()
                  if "libstdc++.so.6 " in line and "x86-64" in line]
    if not candidates or not any(b"GLIBCXX_3.4.29\0" in Path(p).read_bytes() for p in candidates):
        raise RuntimeError("系統 libstdc++ 未提供 GLIBCXX_3.4.29；請洽系統管理員。")
    parent = prefix
    while not parent.exists():
        parent = parent.parent
    with tarfile.open(bundle / "python/cpython.tar.gz") as archive:
        required = sum(m.size for m in archive.getmembers())
    required += sum(p.stat().st_size for p in bundle.rglob("*") if p.is_file()) * 3 + 512 * 1024**2
    available = shutil.disk_usage(parent).free
    if available < required:
        raise RuntimeError(f"空間不足：需要 {required} bytes，可用 {available} bytes。")
    if os.geteuid() == 0:
        print("警告：不需要 root，建議使用一般使用者帳號。", flush=True)
        _confirm("仍以 root 安裝？", yes)
    print(f"預先檢查通過：{info.get('PRETTY_NAME')} x86_64 glibc={libc} "
          f"GLIBCXX_3.4.29 可用；可用空間 {available} bytes。", flush=True)


def _verify_bundle(bundle: Path) -> None:
    for line in (bundle / "SHA256SUMS").read_text().splitlines():
        digest, relative = line.split("  ", 1)
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("SHA256SUMS 路徑不安全。")
        with (bundle / path).open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest:
            raise RuntimeError(f"安裝包檔案 SHA-256 不符：{relative}")
    print("安裝包 SHA256SUMS 通過。", flush=True)


def _wrapper(prefix: Path, command: str) -> str:
    target = prefix / "current/venv/bin" / command
    return f'#!/bin/sh\n{MARKER}\nexec {shlex.quote(str(target))} "$@"\n'


def _bin_links(prefix: Path, *, remove: bool = False) -> None:
    directory = _home() / ".local/bin"
    if not remove:
        directory.mkdir(parents=True, exist_ok=True)
    for name, command in (("terminux", "terminux-launch"), ("terminux-qt-check", "terminux-qt-check")):
        path = directory / name
        text = _wrapper(prefix, command)
        if os.path.lexists(path):
            if path.is_symlink() or not path.is_file() or path.read_text(errors="replace") != text:
                print(f"警告：保留非本安裝建立的檔案 {path}。", flush=True)
                continue
            if remove:
                path.unlink()
            continue
        if not remove:
            # 獨占建立，既有使用者檔案永不被覆寫。
            with path.open("x") as stream:
                stream.write(text)
            path.chmod(0o755)


def _owned(directory: Path, prefix: Path) -> bool:
    if directory.is_symlink() or not directory.is_dir():
        return False
    try:
        marker = json.loads((directory / "share/portable-install.json").read_text())
        return (marker == {"prefix": str(prefix), "version": directory.name, "uid": os.geteuid()})
    except (OSError, ValueError):
        return False


def _current(prefix: Path) -> Path | None:
    current = prefix / "current"
    if not os.path.lexists(current):
        return None
    if not current.is_symlink() or current.resolve().parent != prefix:
        raise RuntimeError(f"current 不是本 prefix 的版本 symlink：{current}")
    return current.resolve()


def _relocate_venv(partial: Path, final: Path) -> None:
    # venv 的 console shebang、啟用腳本與 pyvenv.cfg 都可能含建立時的絕對位置。
    paths = list((partial / "venv/bin").iterdir()) + [partial / "venv/pyvenv.cfg"]
    for path in paths:
        if path.is_symlink():
            target = os.readlink(path)
            if str(partial) in target:
                path.unlink()
                path.symlink_to(target.replace(str(partial), str(final)))
        elif path.is_file():
            content = path.read_bytes()
            if b"\0" not in content and str(partial).encode() in content:
                path.write_bytes(content.replace(str(partial).encode(), str(final).encode()))


def _install(bundle: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="install.sh")
    parser.add_argument("--qt-dir")
    parser.add_argument("--prefix", default=str(_home() / ".local/opt/terminux"))
    parser.add_argument("--no-bin-link", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--reinstall", action="store_true")
    args = parser.parse_args(argv)
    prefix = _prefix(args.prefix)
    version = _version((bundle / "VERSION").read_text().strip())
    final, partial = prefix / version, prefix / (version + ".partial")
    _daemon_guard()
    previous = _current(prefix)
    if os.path.lexists(final):
        if not args.reinstall:
            raise RuntimeError(f"同版本已安裝：{final}；如需重裝，請加 --reinstall。")
        if not _owned(final, prefix):
            raise RuntimeError(f"拒絕取代非本安裝擁有的目錄：{final}")
    if os.path.lexists(partial):
        raise RuntimeError(f"partial 已存在，請先人工確認：{partial}")
    qt = Path(args.qt_dir).expanduser().absolute() if args.qt_dir else None
    if qt is None and previous and (previous / "qt").is_symlink():
        qt = (previous / "qt").resolve()
    if qt is None:
        raise ValueError("必須提供 --qt-dir，或已有可沿用的 current/qt symlink。")
    _preflight(prefix, bundle, args.force, args.yes)
    _verify_bundle(bundle)
    _confirm(f"安裝 Terminux {version} 至 {final}？", args.yes)
    prefix.mkdir(parents=True, exist_ok=True)
    partial.mkdir(mode=0o700)
    backup = prefix / (version + f".replaced-{os.getpid()}")
    try:
        with tarfile.open(bundle / "python/cpython.tar.gz") as archive:
            archive.extractall(partial, filter="data")
        python = partial / "python/bin/python3.12"
        _run([python, "-I", "-m", "venv", partial / "venv"])
        vpython = partial / "venv/bin/python"
        # 相對 symlink 在 staging 與最終目錄均成立。
        for path in (partial / "venv/bin").iterdir():
            if path.is_symlink() and str(partial) in os.readlink(path):
                target = Path(os.readlink(path))
                path.unlink()
                path.symlink_to(os.path.relpath(target, path.parent))
        wheelhouse = bundle / "wheelhouse"
        _run([vpython, "-I", "-m", "pip", "install", "--no-index", "--require-hashes",
              "--find-links", wheelhouse, "-r", wheelhouse / "requirements.lock"])
        engine = list(wheelhouse.glob("terminux_engine-*.whl"))
        if len(engine) != 1:
            raise RuntimeError("engine wheel 數目必須為一。")
        _run([vpython, "-I", "-m", "pip", "install", "--no-index", "--no-deps", engine[0]])
        shutil.copytree(bundle / "gui/bin", partial / "bin")
        (partial / "qt").symlink_to(qt, target_is_directory=True)
        share = partial / "share"
        share.mkdir()
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "VERSION", "README-INSTALL.md"):
            shutil.copy2(bundle / name, share / name)
        shutil.copytree(bundle / "third_party", share / "third_party")
        shutil.copy2(bundle / "portable_installer.py", share / "portable_installer.py")
        shutil.copy2(bundle / "uninstall.sh", partial / "uninstall.sh")
        (share / "portable-install.json").write_text(json.dumps(
            {"prefix": str(prefix), "version": version, "uid": os.geteuid()}))
        # 檢查器保留原始診斷輸出，且由 Qt 自我檢查確認真正可載入。
        _run([partial / "venv/bin/terminux-qt-check", "--qt-dir", partial / "qt", "--prefix", partial])
        _relocate_venv(partial, final)
        if os.path.lexists(final):
            if os.path.lexists(backup):
                raise RuntimeError(f"備份位置已存在：{backup}")
            final.rename(backup)
        try:
            partial.rename(final)
            link = prefix / f".current-{os.getpid()}"
            link.symlink_to(version, target_is_directory=True)
            os.replace(link, prefix / "current")
        except BaseException:
            if final.exists() and backup.exists():
                shutil.rmtree(final)
            if backup.exists():
                backup.rename(final)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if partial.exists():
            shutil.rmtree(partial)
            print(f"已清除 partial：{partial}；current 未變動。", flush=True)
    if not args.no_bin_link:
        _bin_links(prefix)
    print(f"安裝成功：{final}\ncurrent → {version}\n"
          'bash PATH：export PATH="$HOME/.local/bin:$PATH"\n'
          'tcsh PATH：setenv PATH "$HOME/.local/bin:$PATH"\n'
          'tcsh hook：if ( $?TERMINUX_ACTIVE ) source ~/.terminux/tcsh_hooks.csh\n'
          '有 ~/.tcshrc 就加在 ~/.tcshrc，沒有才加在 ~/.cshrc；GUI bash 自動整合。\n'
          '啟動：~/.local/bin/terminux\n'
          'LLM 設定為選用；請見 README-INSTALL.md 的 First Launch。\n'
          f'移除：{shlex.quote(str(final / "uninstall.sh"))} --version {version}\n'
          '多主機：每台主機的 Qt 目錄與安裝路徑必須相同。', flush=True)
    return 0


def _uninstall(root: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="uninstall.sh")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--version")
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--purge-data", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    installed = (root / "share/portable-install.json").is_file()
    prefix = _prefix(str(root.parent) if installed else str(_home() / ".local/opt/terminux"))
    _daemon_guard()
    current = _current(prefix)
    if args.version:
        targets = [prefix / _version(args.version)]
        if not _owned(targets[0], prefix):
            raise RuntimeError(f"版本不存在或不是本安裝擁有：{targets[0]}")
    else:
        targets = sorted(p for p in prefix.iterdir() if _owned(p, prefix)) if prefix.exists() else []
        if current and current not in targets:
            raise RuntimeError("current 未指向可安全移除的安裝，請先人工確認。")
    data = _home() / ".terminux"
    if args.purge_data:
        if data.is_symlink():
            raise RuntimeError("資料目錄是 symlink，拒絕清除。")
        # --yes 不得省略資料清除的完整路徑確認。
        if input(f"將永久刪除資料；請輸入完整路徑 {data}：").strip() != str(data):
            raise RuntimeError("路徑確認不符，未移除版本或資料。")
    _confirm("移除版本：" + ", ".join(p.name for p in targets) + "？", args.yes)
    for target in targets:
        # 以各版本 wheel 的原始腳本執行保守移除，不啟動 daemon。
        code = ("import pathlib,subprocess,terminux_engine; "
                "p=pathlib.Path(terminux_engine.__file__).parent/'resources/scripts/terminux-setup-wrappers'; "
                "subprocess.run(['/bin/bash',str(p),'--remove'],check=True)")
        _run([target / "venv/bin/python", "-I", "-c", code])
    if current in targets:
        (prefix / "current").unlink()
        _bin_links(prefix, remove=True)
    for target in targets:
        shutil.rmtree(target)
        print(f"已移除版本：{target}", flush=True)
    if args.purge_data and data.exists():
        shutil.rmtree(data)
        print(f"已清除資料：{data}", flush=True)
    else:
        print(f"保留資料、設定與 log：{data}", flush=True)
    print('請自行從 rc 移除不再使用的 Terminux PATH／hook 行：\n'
          'source ~/.terminux/bash_hooks.sh\n'
          'if ( $?TERMINUX_ACTIVE ) source ~/.terminux/tcsh_hooks.csh\n'
          '以及自行加入的 ~/.terminux/bin、BASH_ENV 設定。\n'
          '有 ~/.tcshrc 就檢查 ~/.tcshrc，沒有才檢查 ~/.cshrc；未修改任何 rc 檔。', flush=True)
    return 0


def main() -> int:
    mode, root, *argv = sys.argv[1:]
    if mode == "install":
        return _install(Path(root), argv)
    return _uninstall(Path(root), argv)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, EOFError) as error:
        print(f"Terminux：{error}", file=sys.stderr)
        raise SystemExit(1)
