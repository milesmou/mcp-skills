#!/usr/bin/env python3
"""toolbox — 统一管理工程依赖的便携式 CLI 工具。

所有工具集中安装到同一个共享目录（TOOLBOX_HOME）：
用时先查找，缺失再从 GitHub Release 或直链下载到该目录，避免每个
工程各自往临时目录下载一份缓存。

只依赖 Python 3.8+ 标准库，Windows / macOS / Linux 通用。

用法示例：
    python toolbox.py list
    python toolbox.py ensure ripgrep
    python toolbox.py path ripgrep
    python toolbox.py get github:sharkdp/fd --asset "fd-v*-x86_64-pc-windows-msvc.zip"
    python toolbox.py path jq
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import platform as platform_module
import re
import shutil
import string
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA_VERSION = 1
TOOLBOX_VERSION = "0.1.0"
USER_AGENT = f"toolbox/{TOOLBOX_VERSION} (shared-tool-cache)"
CHUNK_SIZE = 256 * 1024
# Windows：优先把共享目录放在这些盘符根目录（仅当该盘存在且为本地固定盘时采用）。
# 首选 D 盘；D 盘不可用（不存在/可移动盘/光驱等）时退回“最后一个本地固定盘”。
PREFERRED_DRIVES = ("D",)
# 共享目录根部由 toolbox 自己占用的名字，不能当作工具目录
RESERVED_NAMES = {"bin", "downloads", "store", "index.json", "recipes.json", "toolbox.py"}


class ToolboxError(Exception):
    """面向用户的、可预期的错误。"""


# ---------------------------------------------------------------------------
# 路径与平台
# ---------------------------------------------------------------------------

def last_fixed_drive() -> Optional[str]:
    """Windows：返回本地固定硬盘中盘符最大的一个（如 'D'）。

    排除 U 盘（可移动盘）、光驱、网络盘，避免插拔设备改变工具目录。
    """
    if os.name != "nt":
        return None
    letters: list[str] = []
    try:
        import ctypes

        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for index, letter in enumerate(string.ascii_uppercase):
            if not (bitmask & (1 << index)):
                continue
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\")
            if drive_type == 3:  # DRIVE_FIXED
                letters.append(letter)
        if not letters:  # 没有固定盘时退化为所有已挂载盘符
            letters = [
                letter
                for index, letter in enumerate(string.ascii_uppercase)
                if bitmask & (1 << index)
            ]
    except Exception:  # ctypes 不可用时按路径存在性判断
        letters = [letter for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]
    return max(letters) if letters else None


def preferred_fixed_drive() -> Optional[str]:
    """Windows：按 PREFERRED_DRIVES 顺序返回首个“存在且为本地固定盘”的盘符。

    默认优先 D 盘；只有该盘存在且 GetDriveType 为 DRIVE_FIXED(3) 时才采用，
    否则返回 None，由调用方退回最后一个本地固定盘。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes

        for letter in PREFERRED_DRIVES:
            if ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\") == 3:  # DRIVE_FIXED
                return letter
    except Exception:  # ctypes 不可用时按路径存在性判断
        for letter in PREFERRED_DRIVES:
            if os.path.exists(f"{letter}:\\"):
                return letter
    return None


def default_home() -> Path:
    """默认共享目录：Windows 优先固定盘（默认 D:\\toolbox），
    首选盘不可用时退化为最后一个本地固定盘根目录下的 toolbox。"""
    if os.name == "nt":
        letter = preferred_fixed_drive() or last_fixed_drive()
        if letter:
            return Path(f"{letter}:\\toolbox")
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "toolbox"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "toolbox"


def toolbox_home(explicit: Optional[str] = None) -> Path:
    """返回共享目录根。优先级：--home > TOOLBOX_HOME > 最后一个固定盘的 toolbox。

    TOOLBOX_HOME 设为 `auto` 时忽略具体值，按当前机器的最后一个固定盘解析，
    换电脑后无需改环境变量。
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("TOOLBOX_HOME")
    if env and env.strip().lower() != "auto":
        return Path(env).expanduser().resolve()
    return default_home().resolve()


def os_family() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def arch_name() -> str:
    machine = platform_module.machine().lower()
    return {
        "amd64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
        "i386": "x86",
        "i686": "x86",
        "x86": "x86",
    }.get(machine, machine or "unknown")


def platform_key() -> str:
    return f"{os_family()}-{arch_name()}"


def ensure_layout(home: Path) -> None:
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ToolboxError(
            f"无法创建共享目录 {home}: {exc}\n"
            f"请用 --home 或环境变量 TOOLBOX_HOME 指定一个可写目录。"
        ) from exc


def rel_to_home(home: Path, path: Path) -> str:
    """把路径转成相对共享目录的 POSIX 风格字符串，便于迁移。"""
    try:
        return path.resolve().relative_to(home.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------

def load_index(home: Path) -> dict[str, Any]:
    path = home / "index.json"
    if not path.exists():
        return {"schema": SCHEMA_VERSION, "tools": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolboxError(f"无法解析索引 {path}: {exc}") from exc
    data.setdefault("schema", SCHEMA_VERSION)
    data.setdefault("tools", {})
    return data


def save_index(home: Path, index: dict[str, Any]) -> None:
    path = home / "index.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def version_sort_key(version: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", version or "")
    return tuple(int(p) for p in parts) if parts else (0,)


def choose_latest(versions: Iterable[str]) -> str:
    return max(versions, key=version_sort_key)


def find_entry(
    index: dict[str, Any], name: str, version: Optional[str] = None
) -> Optional[dict[str, Any]]:
    tool = index.get("tools", {}).get(name)
    if not tool:
        return None
    versions = tool.get("versions", {})
    if version is None:
        version = tool.get("latest")
    if not version:
        return None
    return versions.get(version)


# ---------------------------------------------------------------------------
# 网络与 GitHub
# ---------------------------------------------------------------------------

def github_token() -> Optional[str]:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def _request(url: str, token: Optional[str] = None) -> urllib.request.Request:
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if "api.github.com" in url:
        headers["Accept"] = "application/vnd.github+json"
    return urllib.request.Request(url, headers=headers)


def download(url: str, dest: Path, token: Optional[str] = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(_request(url, token), timeout=180) as resp, open(
            dest, "wb"
        ) as fh:
            shutil.copyfileobj(resp, fh, CHUNK_SIZE)
    except urllib.error.HTTPError as exc:
        raise ToolboxError(f"下载失败 HTTP {exc.code}: {url}") from exc
    except urllib.error.URLError as exc:
        raise ToolboxError(f"下载失败（网络或代理问题）: {url}\n{exc.reason}") from exc
    return dest


def fetch_text(url: str, token: Optional[str] = None, limit: int = 4_000_000) -> str:
    try:
        with urllib.request.urlopen(_request(url, token), timeout=45) as resp:
            return resp.read(limit).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise ToolboxError(f"请求失败 HTTP {exc.code}: {url}") from exc
    except urllib.error.URLError as exc:
        raise ToolboxError(f"请求失败（网络或代理问题）: {url}\n{exc.reason}") from exc


def normalize_repo(repo: str) -> str:
    repo = repo.strip().strip("/")
    if repo.startswith("https://github.com/"):
        repo = repo[len("https://github.com/") :]
    if repo.startswith("git@github.com:"):
        repo = repo[len("git@github.com:") :]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if repo.count("/") != 1:
        raise ToolboxError(f"无效的仓库标识: {repo}（应为 owner/repo）")
    return repo


def github_release(repo: str, tag: Optional[str] = None) -> dict[str, Any]:
    repo = normalize_repo(repo)
    if tag:
        url = f"https://api.github.com/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}"
    else:
        url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        return json.loads(fetch_text(url, github_token()))
    except urllib.error.HTTPError:  # fetch_text 已转换，这里兜底
        raise
    except json.JSONDecodeError as exc:
        raise ToolboxError(f"GitHub 返回了无法解析的数据: {url}") from exc


def pick_asset(assets: list[dict[str, Any]], pattern: str) -> dict[str, Any]:
    if not pattern:
        raise ToolboxError("缺少 release 资产匹配模式")
    names = [a.get("name", "") for a in assets]
    for asset in assets:
        if asset.get("name") == pattern:
            return asset
    matched = [
        a for a in assets if fnmatch.fnmatchcase(a.get("name", "").lower(), pattern.lower())
    ]
    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        listing = "\n  ".join(a.get("name", "") for a in matched)
        raise ToolboxError(f"资产模式匹配到多个文件，请写得更精确:\n  {listing}")
    listing = "\n  ".join(names[:60])
    raise ToolboxError(f"没有匹配的 release 资产: {pattern}\n可用资产:\n  {listing}")


def guess_asset(assets: list[dict[str, Any]]) -> dict[str, Any]:
    """在没有 --asset 时按当前平台猜一个资产。"""
    family = os_family()
    arch = arch_name()
    family_tokens = {
        "win32": ("win", "windows"),
        "darwin": ("darwin", "macos", "osx", "apple"),
        "linux": ("linux",),
    }[family]
    arch_tokens = {
        "x64": ("x64", "x86_64", "amd64"),
        "arm64": ("arm64", "aarch64"),
        "x86": ("x86", "i386", "i686", "386", "win32"),
    }.get(arch, (arch,))
    other_arches = {
        "x64": ("arm64", "aarch64", "i386", "i686", "386"),
        "arm64": ("x86_64", "amd64", "i386", "i686", "386"),
    }.get(arch, ())
    scored: list[tuple[int, dict[str, Any]]] = []
    for asset in assets:
        name = asset.get("name", "").lower()
        if name.endswith((".sha256", ".sha512", ".sig", ".asc", ".txt", ".json")):
            continue
        score = 0
        if any(t in name for t in family_tokens):
            score += 5
        elif any(t in name for t in ("win", "darwin", "macos", "linux", "osx")):
            score -= 5
        if any(t in name for t in arch_tokens):
            score += 4
        if any(t in name for t in other_arches):
            score -= 8
        if name.endswith((".zip", ".tar.gz", ".tgz", ".tar.xz")):
            score += 1
        scored.append((score, asset))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored or scored[0][0] <= 0:
        listing = "\n  ".join(a.get("name", "") for a in assets)
        raise ToolboxError(f"无法自动判断该用哪个资产，请用 --asset 指定:\n  {listing}")
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        listing = "\n  ".join(a.get("name", "") for a in assets)
        raise ToolboxError(f"有多个资产得分相同，请用 --asset 指定:\n  {listing}")
    return scored[0][1]


def sidecar_sha256(assets: list[dict[str, Any]], asset: dict[str, Any]) -> Optional[str]:
    """如果 release 提供 <asset>.sha256，返回其中的摘要。"""
    wanted = asset.get("name", "") + ".sha256"
    for candidate in assets:
        if candidate.get("name") == wanted:
            try:
                text = fetch_text(candidate["browser_download_url"], github_token(), 8192)
            except ToolboxError:
                return None
            match = re.search(r"\b([0-9a-fA-F]{64})\b", text)
            return match.group(1).lower() if match else None
    return None


# ---------------------------------------------------------------------------
# 校验、解压、定位
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_target(root: Path, member: str) -> Path:
    root_resolved = root.resolve()
    target = (root / member).resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ToolboxError(f"压缩包包含越界路径: {member}")
    return target


def extract_archive(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                _safe_target(dest, info.filename)
            zf.extractall(dest)
    elif name.endswith((".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".tar")):
        with tarfile.open(archive, "r:*") as tf:
            for member in tf.getmembers():
                _safe_target(dest, member.name)
            if hasattr(tarfile, "data_filter"):
                tf.extractall(dest, filter="data")
            else:
                tf.extractall(dest)
    else:
        shutil.copy2(archive, dest / archive.name)


def single_root(root: Path) -> Path:
    """归档解压后若只有一个顶层目录，返回该目录，去掉多余的一层包装。"""
    entries = list(root.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return root


def find_executable(root: Path, hint: str) -> Path:
    hint = hint.replace("\\", "/")
    if "/" in hint:
        candidate = root / hint
        if candidate.is_file():
            return candidate
    matches: list[tuple[int, int, Path]] = []
    for path in root.rglob("*"):
        if path.is_file() and (
            path.name == hint or fnmatch.fnmatchcase(path.name.lower(), hint.lower())
        ):
            rel = path.relative_to(root)
            matches.append((len(rel.parts), len(rel.as_posix()), path))
    if not matches:
        files = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
        listing = "\n  ".join(files[:40]) or "(空)"
        raise ToolboxError(f"解压后找不到可执行文件 `{hint}`。现有文件:\n  {listing}")
    matches.sort()
    return matches[0][2]


def smart_find_executable(root: Path, hints: list[str]) -> Path:
    for hint in hints:
        if not hint:
            continue
        try:
            return find_executable(root, hint)
        except ToolboxError:
            continue
    files = [p for p in root.rglob("*") if p.is_file()]
    if len(files) == 1:
        return files[0]
    candidates = [p for p in files if p.suffix.lower() in (".exe", "") and not p.name.startswith(".")]
    if len(candidates) == 1:
        return candidates[0]
    listing = "\n  ".join(p.relative_to(root).as_posix() for p in files[:40])
    raise ToolboxError(f"无法确定要暴露的可执行文件，请用 --exe 指定。文件:\n  {listing}")


# ---------------------------------------------------------------------------
# 目录扫描：索引缺失时直接从 store/ 目录发现工具
# ---------------------------------------------------------------------------

_PLATFORM_DIR_RE = re.compile(r"^(?:win32|linux|darwin)-[A-Za-z0-9_]+$")
_TEXT_EXTS = {
    ".txt", ".md", ".json", ".html", ".htm", ".xml", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".1", ".license", ".notice", ".sha256",
}
_NON_EXE_EXTS = {
    ".pdb", ".dll", ".lib", ".a", ".so", ".dylib", ".py", ".pyc",
    ".js", ".mjs", ".cjs", ".sh", ".ps1", ".cmd", ".bat", ".map",
}


def detect_store_exe(root: Path, name: str) -> Optional[Path]:
    """在一个工具目录里猜测可执行文件（用于索引缺失时的兜底）。"""
    files = [p for p in root.rglob("*") if p.is_file()]
    if not files:
        return None

    def plausible(path: Path) -> bool:
        suffix = path.suffix.lower()
        if suffix in _TEXT_EXTS:
            return False
        if os.name == "nt":
            return suffix in (".exe", ".bat", ".cmd")  # 手工工具常见 .bat 启动器
        if suffix in _NON_EXE_EXTS:
            return False
        return path.suffix == "" or os.access(path, os.X_OK)

    candidates = [p for p in files if plausible(p)]
    if not candidates:
        candidates = [p for p in files if p.suffix.lower() not in _TEXT_EXTS]
    if not candidates:
        return None

    def rank(path: Path) -> tuple[int, int, int]:
        return (len(path.relative_to(root).parts), -path.stat().st_size, len(path.as_posix()))

    # 目录名可能带版本，如 jadx-1.5.6 -> jadx，便于匹配 jadx.bat
    base = re.sub(r"[-_ ]?v?\d+(?:\.\d+)*$", "", name).lower() or name.lower()
    wanted = {name.lower(), base}
    exact = [p for p in candidates if p.stem.lower() in wanted]
    if exact:
        return min(exact, key=rank)
    prefixed = [p for p in candidates if any(p.stem.lower().startswith(w) for w in wanted)]
    if prefixed:
        return min(prefixed, key=rank)
    if len(candidates) == 1:
        return candidates[0]
    return None  # 多个候选且名称不匹配：宁可跳过，避免误认 SDK/大目录里的 exe


def _scan_nested_layout(tool_dir: Path, name: str) -> list[dict[str, Any]]:
    """兼容旧布局 store/<名称>/<版本>/<平台>/...。"""
    records: list[dict[str, Any]] = []
    version_dirs = sorted(p for p in tool_dir.iterdir() if p.is_dir())
    if not version_dirs:
        exe = detect_store_exe(tool_dir, name)
        if exe:
            records.append(
                {
                    "name": name,
                    "version": "local",
                    "platform": platform_key(),
                    "dir": tool_dir,
                    "exe": exe,
                    "indexed": False,
                }
            )
        return records
    for version_dir in version_dirs:
        plat_dirs = [
            p for p in version_dir.iterdir() if p.is_dir() and _PLATFORM_DIR_RE.match(p.name)
        ]
        if plat_dirs:
            for plat_dir in sorted(plat_dirs):
                exe = detect_store_exe(plat_dir, name)
                if exe:
                    records.append(
                        {
                            "name": name,
                            "version": version_dir.name,
                            "platform": plat_dir.name,
                            "dir": plat_dir,
                            "exe": exe,
                            "indexed": False,
                        }
                    )
        else:
            exe = detect_store_exe(version_dir, name)
            if exe:
                records.append(
                    {
                        "name": name,
                        "version": version_dir.name,
                        "platform": "unknown",
                        "dir": version_dir,
                        "exe": exe,
                        "indexed": False,
                    }
                )
    return records


def scan_tools(
    home: Path,
    only_name: Optional[str] = None,
    skip_names: frozenset[str] = frozenset(),
    skip_dirs: frozenset[Path] = frozenset(),
) -> list[dict[str, Any]]:
    """扫描目录，返回磁盘上实际存在的工具。

    主布局（每个工具一个目录）：
      <TOOLBOX_HOME>/<名称>/... 或带版本号的 <TOOLBOX_HOME>/<名称>-<版本>/...
    同时兼容旧布局：
      <TOOLBOX_HOME>/store/<名称>/<版本>/<平台>/...
    """
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(records: list[dict[str, Any]]) -> None:
        for record in records:
            name = record["name"]
            if name in skip_names or name in seen:
                continue
            try:
                record_dir = record["dir"].resolve()
            except OSError:
                record_dir = record["dir"]
            if record_dir in skip_dirs:  # 已被索引登记（含带版本号的目录），不重复发现
                continue
            seen.add(name)
            found.append(record)

    legacy = home / "store"
    if legacy.is_dir():
        names = [only_name] if only_name else sorted(p.name for p in legacy.iterdir() if p.is_dir())
        for name in names:
            tool_dir = legacy / name
            if tool_dir.is_dir():
                add(_scan_nested_layout(tool_dir, name))

    if only_name:
        flat_names = [only_name] if (home / only_name).is_dir() else []
    else:
        flat_names = sorted(p.name for p in home.iterdir() if p.is_dir())
    for name in flat_names:
        if name in RESERVED_NAMES or name.startswith("."):
            continue
        tool_dir = home / name
        if not tool_dir.is_dir():
            continue
        exe = detect_store_exe(tool_dir, name)
        if exe:
            add(
                [
                    {
                        "name": name,
                        "version": "local",
                        "platform": platform_key(),
                        "dir": tool_dir,
                        "exe": exe,
                        "indexed": False,
                    }
                ]
            )
    return found


def find_tool(
    home: Path, index: dict[str, Any], name: str, version: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """先查索引，再扫描工具目录，最后看目录根部直接投放的文件。"""
    entry = find_entry(index, name, version)
    if entry:
        exe = home / entry["exe"]
        if exe.exists():
            return {
                "name": name,
                "version": entry_version(index, name, entry),
                "platform": entry.get("platform", "?"),
                "path": str(exe),
                "indexed": True,
            }

    scanned = scan_tools(home, name)
    if version:
        matched = [r for r in scanned if r["version"] == version]
        if matched:
            scanned = matched
    current = [r for r in scanned if r["platform"] == platform_key()]
    pool = current or scanned
    if pool:
        record = max(pool, key=lambda r: version_sort_key(r["version"]))
        return {
            "name": name,
            "version": record["version"],
            "platform": record["platform"],
            "path": str(record["exe"]),
            "indexed": False,
        }

    for candidate in (
        home / f"{name}.exe",
        home / name,
    ):
        if candidate.is_file():
            return {
                "name": name,
                "version": "local",
                "platform": platform_key(),
                "path": str(candidate),
                "indexed": False,
            }
    return None


def indexed_dirs(home: Path, index: dict[str, Any]) -> frozenset[Path]:
    """索引中所有版本目录的绝对路径集合，用于扫描时按“目录”去重。"""
    dirs: set[Path] = set()
    for tool in index.get("tools", {}).values():
        for entry in tool.get("versions", {}).values():
            rel = entry.get("dir")
            if not rel:
                continue
            try:
                dirs.add((home / rel).resolve())
            except OSError:
                pass
    return frozenset(dirs)


def adopt_scanned(home: Path, index: dict[str, Any]) -> list[str]:
    """把目录里存在但索引未登记的工具收编进索引。"""
    adopted: list[str] = []
    skip = frozenset(n for n, t in index.get("tools", {}).items() if t.get("versions"))
    skip_dirs = indexed_dirs(home, index)
    for record in scan_tools(home, skip_names=skip, skip_dirs=skip_dirs):
        tool = index["tools"].setdefault(record["name"], {"versions": {}})
        versions = tool.setdefault("versions", {})
        if record["version"] in versions:
            continue
        try:
            exe_sha = sha256_file(record["exe"])
        except OSError:
            exe_sha = ""
        versions[record["version"]] = {
            "platform": record["platform"],
            "dir": rel_to_home(home, record["dir"]),
            "exe": rel_to_home(home, record["exe"]),
            "sha256": "",
            "exe_sha256": exe_sha,
            "source": "(discovered)",
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        tool["latest"] = choose_latest(versions)
        adopted.append(f"{record['name']} {record['version']} ({record['platform']})")
    return adopted


def adopt_pending(home: Path) -> tuple[dict[str, Any], list[str]]:
    """加载索引，并把目录中发现但未登记的工具“立即收编”进索引。

    有变更才写回 index.json，返回 (最新索引, 本次收编的工具列表)。
    供 list / find / path / doctor 等命令在扫描到未登记工具时即时登记，
    不再需要手动运行 repair。
    """
    with home_lock(home):
        index = load_index(home)
        adopted = adopt_scanned(home, index)
        if adopted:
            save_index(home, index)
    return index, adopted


def report_adopted(adopted: list[str]) -> None:
    """把本次自动登记的工具提示打到 stderr（不污染 JSON/路径的标准输出）。"""
    if adopted:
        print(
            f"已自动登记 {len(adopted)} 个未登记工具: " + ", ".join(adopted),
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# recipe
# ---------------------------------------------------------------------------

def load_recipes(home: Path) -> dict[str, dict[str, Any]]:
    recipes: dict[str, dict[str, Any]] = {}
    bundled = Path(__file__).resolve().parent / "recipes.json"
    if bundled.exists():
        recipes.update(json.loads(bundled.read_text(encoding="utf-8")))
    user = home / "recipes.json"
    if user.exists():
        try:
            recipes.update(json.loads(user.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise ToolboxError(f"无法解析用户 recipe {user}: {exc}") from exc
    return recipes


def clean_version(tag: str, recipe: dict[str, Any]) -> str:
    version = tag or ""
    for prefix in recipe.get("strip_prefix", ["v"]):
        if prefix and version.startswith(prefix):
            version = version[len(prefix) :]
            break
    return version


def select_asset_pattern(recipe: dict[str, Any], plat: str) -> str:
    assets = recipe.get("asset")
    if isinstance(assets, str):
        return assets
    if isinstance(assets, dict):
        for key in (plat, os_family(), "default"):
            if key in assets:
                return assets[key]
    raise ToolboxError("recipe 缺少适用于当前平台的 asset 模式")


def select_exe_hint(recipe: dict[str, Any], plat: str) -> str:
    exe = recipe.get("exe")
    if isinstance(exe, str):
        return exe
    if isinstance(exe, dict):
        for key in (plat, os_family(), "default"):
            if key in exe:
                return exe[key]
    raise ToolboxError("recipe 缺少适用于当前平台的 exe 配置")


def exe_name_candidates(name: str, exe_hint: Optional[str]) -> list[str]:
    """生成在系统 PATH 中探测时使用的命令名候选（去重、保持顺序）。

    同时考虑工具登记名与 recipe 的 exe 别名，例如 ripgrep 的 exe 是 rg。
    """
    candidates: list[str] = [name]
    if exe_hint:
        candidates.append(Path(exe_hint).name)
        candidates.append(Path(exe_hint).stem)
    ordered: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in ordered:
            ordered.append(candidate)
    return ordered


def find_in_path(name: str, exe_hint: Optional[str] = None) -> Optional[str]:
    """在系统 PATH 中查找工具，命中返回可执行文件绝对路径，否则返回 None。"""
    for candidate in exe_name_candidates(name, exe_hint):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def skip_if_in_path(
    name: str,
    exe_hint: Optional[str],
    requested_version: Optional[str],
    force: bool,
    action: str = "安装",
) -> bool:
    """若系统 PATH 中已有同名工具，默认跳过安装，避免与系统版本重复。

    - 显式指定了版本（如 ensure protoc@25.1）时不跳过，尊重精确版本请求；
    - `--force` 时不跳过，强制装入 toolbox。
    """
    if force or requested_version:
        return False
    found = find_in_path(name, exe_hint)
    if found:
        print(
            f"系统 PATH 中已存在 `{name}`：{found}，跳过{action}。"
            f"如需仍装入 toolbox，请加 --force。"
        )
        return True
    return False


# ---------------------------------------------------------------------------
# 锁（同一个共享目录避免并发写坏索引）
# ---------------------------------------------------------------------------

@contextmanager
def home_lock(home: Path):
    ensure_layout(home)
    # 锁文件放系统临时目录，共享目录里只保留各工具目录和 index.json
    digest = hashlib.sha256(str(home.resolve()).lower().encode("utf-8")).hexdigest()[:16]
    lock_path = Path(tempfile.gettempdir()) / f"toolbox-{digest}.lock"
    handle = open(lock_path, "a+")
    try:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:
            pass  # 锁不可用时退化为无锁，保持命令可用
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


# ---------------------------------------------------------------------------
# 安装与登记
# ---------------------------------------------------------------------------

def install_artifact(
    home: Path,
    index: dict[str, Any],
    *,
    name: str,
    version: str,
    plat: str,
    url: str,
    filename: str,
    source: str,
    exe_hint: Optional[str],
    expected_sha: Optional[str],
    force: bool,
    assets: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    existing = find_entry(index, name, version)
    if existing and not force and (home / existing["exe"]).exists():
        print(f"{name} {version} ({plat}) 已存在: {home / existing['exe']}")
        return existing

    if name in RESERVED_NAMES:
        raise ToolboxError(f"`{name}` 是 toolbox 保留名称，不能用作工具名")
    # 独立工具目录带版本号（<名称>-<版本>），支持同一工具多版本共存；
    # 工具集/SDK 类目录（如 AndroidSdk）不在此自动创建，扫描时原样保留。
    dest_dir = home / f"{name}-{version}"
    # 允许下载到共享目录的 downloads/，但本次安装结束（成功或失败）必须清空
    downloads_dir = home / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="toolbox-", dir=str(downloads_dir)))
    archive = workdir / filename
    try:
        download(url, archive, github_token())
        actual_sha = sha256_file(archive)

        wanted_sha = expected_sha
        if not wanted_sha and assets:
            wanted_sha = sidecar_sha256(assets, next((a for a in assets if a.get("name") == filename), {}))
        if wanted_sha and wanted_sha.lower() != actual_sha.lower():
            raise ToolboxError(
                f"{name} 校验失败：期望 sha256={wanted_sha}，实际={actual_sha}。已丢弃下载文件。"
            )

        staging = workdir / "extract"
        extract_archive(archive, staging)
        content_root = single_root(staging)
        if exe_hint:
            found = find_executable(content_root, exe_hint)
        else:
            guessed = [name + ".exe", name]
            found = smart_find_executable(content_root, guessed)
        rel_exe = found.relative_to(content_root)

        if dest_dir.exists():
            if dest_dir.resolve().parent != home.resolve():
                raise ToolboxError(f"拒绝覆盖非工具目录: {dest_dir}")
            shutil.rmtree(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        for item in content_root.iterdir():
            shutil.move(str(item), str(dest_dir / item.name))
        exe = dest_dir / rel_exe

        tool = index["tools"].setdefault(name, {"versions": {}})
        tool.setdefault("versions", {})
        # 多版本共存：仅替换当前 <名称>-<版本> 目录，保留其它已安装版本
        entry = {
            "platform": plat,
            "dir": rel_to_home(home, dest_dir),
            "exe": rel_to_home(home, exe),
            "sha256": actual_sha,
            "exe_sha256": sha256_file(exe),
            "source": source,
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        tool["versions"][version] = entry
        tool["latest"] = choose_latest(tool["versions"].keys())
        print(f"已安装 {name} {version} ({plat}) -> {exe}")
        return entry
    finally:
        # 无论成功失败都清掉本次下载/解压目录；若 downloads 已空则一并移除
        shutil.rmtree(workdir, ignore_errors=True)
        try:
            downloads_dir.rmdir()
        except OSError:
            pass


def split_name_version(spec: str) -> tuple[str, Optional[str]]:
    if "@" in spec and not spec.startswith(("http://", "https://", "github:")):
        name, _, version = spec.partition("@")
        return name.strip(), version.strip() or None
    return spec.strip(), None


def install_recipe(
    home: Path,
    index: dict[str, Any],
    recipes: dict[str, dict[str, Any]],
    name: str,
    version_arg: Optional[str],
    force: bool,
) -> dict[str, Any]:
    recipe = recipes.get(name)
    if not recipe:
        available = ", ".join(sorted(recipes))
        raise ToolboxError(
            f"没有名为 `{name}` 的内置 recipe。可用: {available}\n"
            f"若工具不在列表，用 `toolbox get <url>` 或 `toolbox get github:owner/repo@tag --asset <pattern> --name {name}` 安装。"
        )
    plat = platform_key()
    requested = (version_arg or recipe.get("version") or "latest").strip()
    tag = ""
    if requested.lower() == "latest":
        release = github_release(recipe["repo"], None)
        tag = release.get("tag_name", "")
    else:
        candidates: list[str] = []
        tag_template = recipe.get("tag")
        if tag_template:
            candidates.append(tag_template.format(version=requested))
        candidates.extend([requested, f"v{requested}", f"{name}-{requested}"])
        release = None
        last_error: Optional[Exception] = None
        for candidate in dict.fromkeys(candidates):
            try:
                release = github_release(recipe["repo"], candidate)
                tag = candidate
                break
            except ToolboxError as exc:
                last_error = exc
        if release is None:
            raise ToolboxError(f"找不到 {name} 的版本 {requested}。最后错误: {last_error}")

    version = clean_version(tag, recipe) or requested
    pattern = select_asset_pattern(recipe, plat).format(version=version, tag=tag, platform=plat)
    asset = pick_asset(release.get("assets", []), pattern)
    exe_hint = select_exe_hint(recipe, plat)
    return install_artifact(
        home,
        index,
        name=name,
        version=version,
        plat=plat,
        url=asset["browser_download_url"],
        filename=asset["name"],
        source=f"github:{recipe['repo']}@{tag}",
        exe_hint=exe_hint,
        expected_sha=recipe.get("sha256"),
        force=force,
        assets=release.get("assets", []),
    )


def install_direct(
    home: Path,
    index: dict[str, Any],
    spec: str,
    *,
    name: Optional[str],
    version: Optional[str],
    sha256: Optional[str],
    exe_hint: Optional[str],
    asset_pattern: Optional[str],
    force: bool,
) -> dict[str, Any]:
    plat = platform_key()
    assets: Optional[list[dict[str, Any]]] = None
    if spec.startswith("github:"):
        body = spec[len("github:") :]
        repo, _, tag = body.partition("@")
        release = github_release(repo, tag or None)
        actual_tag = release.get("tag_name", "")
        assets = release.get("assets", [])
        asset = pick_asset(assets, asset_pattern) if asset_pattern else guess_asset(assets)
        url = asset["browser_download_url"]
        filename = asset["name"]
        name = name or normalize_repo(repo).split("/")[-1]
        version = version or clean_version(actual_tag, {}) or actual_tag or "custom"
        source = f"github:{normalize_repo(repo)}@{actual_tag}"
    else:
        url = spec
        parsed = urllib.parse.urlparse(url)
        filename = Path(urllib.parse.unquote(parsed.path)).name or "download.bin"
        name = name or Path(filename).stem
        version = version or "custom"
        source = url
    if not name:
        raise ToolboxError("无法推断工具名，请用 --name 指定")
    if not exe_hint:
        if filename.lower().endswith((".exe",)) or not filename.lower().endswith(
            (".zip", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".tar")
        ):
            exe_hint = filename
    return install_artifact(
        home,
        index,
        name=name,
        version=version,
        plat=plat,
        url=url,
        filename=filename,
        source=source,
        exe_hint=exe_hint,
        expected_sha=sha256,
        force=force,
        assets=assets,
    )


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------

def resolve_exe(home: Path, index: dict[str, Any], name: str, version: Optional[str]) -> dict[str, Any]:
    info = find_tool(home, index, name, version)
    if info:
        return info
    known = set(index.get("tools", {})) | {
        r["name"] for r in scan_tools(home, skip_dirs=indexed_dirs(home, index))
    }
    installed = ", ".join(sorted(known)) or "(空)"
    raise ToolboxError(
        f"未安装 `{name}`，索引和目录中都没有找到。已安装: {installed}\n"
        f"先运行: toolbox ensure {name}"
    )


def entry_version(index: dict[str, Any], name: str, entry: dict[str, Any]) -> str:
    for version, candidate in index["tools"][name]["versions"].items():
        if candidate is entry:
            return version
    return "?"


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}GB"


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for filename in files:
            try:
                total += (Path(root) / filename).stat().st_size
            except OSError:
                pass
    return total


def cleanup_stale_temp(max_age_seconds: int = 24 * 3600) -> int:
    """清理系统临时目录中过期的 toolbox-* 目录（进程崩溃遗留）。返回释放字节数。"""
    freed = 0
    temp_root = Path(tempfile.gettempdir())
    now = time.time()
    try:
        entries = [p for p in temp_root.glob("toolbox-*") if p.is_dir()]
    except OSError:
        return 0
    for entry in entries:
        try:
            if now - entry.stat().st_mtime < max_age_seconds:
                continue  # 可能仍有安装在进行，保留
            size = dir_size(entry)
            shutil.rmtree(entry, ignore_errors=True)
            if not entry.exists():
                freed += size
        except OSError:
            continue
    return freed


def cmd_list(args: argparse.Namespace) -> int:
    home = toolbox_home(args.home)
    index, adopted = adopt_pending(home)
    report_adopted(adopted)
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for name in index.get("tools", {}):
        tool = index["tools"][name]
        for version, entry in tool.get("versions", {}).items():
            platform = entry.get("platform", "?")
            exe = home / entry["exe"]
            rows[(name, version, platform)] = {
                "name": name,
                "version": version,
                "platform": platform,
                "size": dir_size(home / entry["dir"]) if (home / entry["dir"]).exists() else 0,
                "ok": exe.exists(),
                "path": str(exe),
                "indexed": True,
            }
    # 索引里没有、但目录里实际存在的工具也列出来
    skip = frozenset(n for n, t in index.get("tools", {}).items() if t.get("versions"))
    for record in scan_tools(home, skip_names=skip, skip_dirs=indexed_dirs(home, index)):
        key = (record["name"], record["version"], record["platform"])
        if key in rows:
            continue
        rows[key] = {
            "name": record["name"],
            "version": record["version"],
            "platform": record["platform"],
            "size": dir_size(record["dir"]),
            "ok": True,
            "path": str(record["exe"]),
            "indexed": False,
        }
    ordered = sorted(rows.values(), key=lambda r: version_sort_key(r["version"]), reverse=True)
    ordered.sort(key=lambda r: r["name"].lower())
    if args.json:
        print(json.dumps(ordered, ensure_ascii=False, indent=2))
        return 0
    if not ordered:
        print(f"共享目录还没有安装任何工具: {home}")
        print("试试: python toolbox.py ensure ripgrep")
        return 0
    name_w = max([len(r["name"]) for r in ordered] + [4])
    ver_w = max([len(r["version"]) for r in ordered] + [7])
    print(f"共享目录: {home}")
    print(f"{'NAME'.ljust(name_w)}  {'VERSION'.ljust(ver_w)}  {'PLATFORM':<13}  {'SIZE':>9}  STATUS")
    for row in ordered:
        if not row["ok"]:
            status = "MISSING"
        elif not row["indexed"]:
            status = "ok (未登记)"
        else:
            status = "ok"
        print(
            f"{row['name'].ljust(name_w)}  {row['version'].ljust(ver_w)}  "
            f"{row['platform']:<13}  {human_size(row['size']):>9}  {status}"
        )
    if any(not r["indexed"] for r in ordered):
        print("提示: 「未登记」表示目录里有、index.json 没有；运行 toolbox repair 可收编进索引。")
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    home = toolbox_home(args.home)
    index, adopted = adopt_pending(home)
    report_adopted(adopted)
    recipes = load_recipes(home)
    query = args.query.lower()
    scanned_versions: dict[str, list[str]] = {}
    skip = frozenset(n for n, t in index.get("tools", {}).items() if t.get("versions"))
    for record in scan_tools(home, skip_names=skip, skip_dirs=indexed_dirs(home, index)):
        scanned_versions.setdefault(record["name"], []).append(record["version"])
    results = []
    for name in sorted(set(index.get("tools", {})) | set(recipes) | set(scanned_versions)):
        if query and query not in name.lower():
            continue
        installed = index.get("tools", {}).get(name, {})
        on_disk = scanned_versions.get(name, [])
        if installed:
            version = installed.get("latest")
            indexed = True
        elif on_disk:
            version = max(on_disk, key=version_sort_key)
            indexed = False
        else:
            version = None
            indexed = False
        results.append(
            {
                "name": name,
                "installed": bool(installed) or bool(on_disk),
                "version": version,
                "indexed": indexed,
                "recipe": name in recipes,
                "source": recipes.get(name, {}).get("repo"),
                "description": recipes.get(name, {}).get("description"),
            }
        )
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    if not results:
        print(f"没有找到匹配 `{args.query}` 的工具。可用 recipe: {', '.join(sorted(recipes))}")
        return 0
    for item in results:
        if item["installed"]:
            mark = f"已安装 {item['version']}" + ("" if item["indexed"] else " (未登记)")
        elif item["recipe"]:
            mark = "可下载(recipe)"
        else:
            mark = "未知"
        desc = f"  {item['description']}" if item.get("description") else ""
        print(f"{item['name']:<16} {mark:<18}{desc}")
    return 0


def cmd_ensure(args: argparse.Namespace) -> int:
    home = toolbox_home(args.home)
    ensure_layout(home)
    with home_lock(home):
        index = load_index(home)
        recipes = load_recipes(home)
        for spec in args.names:
            name, version = split_name_version(spec)
            recipe = recipes.get(name)
            exe_hint = select_exe_hint(recipe, platform_key()) if recipe else None
            if skip_if_in_path(name, exe_hint, version, args.force, "安装"):
                continue
            # 每装好一个就落盘，避免中途失败丢失已完成记录
            install_recipe(home, index, recipes, name, version, args.force)
            save_index(home, index)
    return 0


def parse_manifest(path: Path) -> list[str]:
    if not path.exists():
        raise ToolboxError(f"找不到工具清单: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ToolboxError(f"清单不是合法 JSON: {path}\n{exc}") from exc
        if isinstance(data, dict):
            data = data.get("tools", [])
        if not isinstance(data, list):
            raise ToolboxError("JSON 清单应为数组，或形如 {\"tools\": [...]}")
        specs: list[str] = []
        for item in data:
            if isinstance(item, str):
                specs.append(item)
            elif isinstance(item, dict) and item.get("name"):
                version = item.get("version")
                specs.append(f"{item['name']}@{version}" if version else item["name"])
            else:
                raise ToolboxError(f"无法识别的清单条目: {item!r}")
        return specs
    specs = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            specs.append(line)
    return specs


def cmd_sync(args: argparse.Namespace) -> int:
    home = toolbox_home(args.home)
    ensure_layout(home)
    manifest = Path(args.file) if args.file else Path.cwd() / ".toolbox.json"
    if not args.file and not manifest.exists():
        fallback = Path.cwd() / ".toolbox"
        if fallback.exists():
            manifest = fallback
    specs = parse_manifest(manifest)
    if not specs:
        print(f"清单为空: {manifest}")
        return 0
    print(f"同步清单: {manifest}（{len(specs)} 个工具）")
    with home_lock(home):
        index = load_index(home)
        recipes = load_recipes(home)
        for spec in specs:
            name, version = split_name_version(spec)
            recipe = recipes.get(name)
            exe_hint = select_exe_hint(recipe, platform_key()) if recipe else None
            if skip_if_in_path(name, exe_hint, version, args.force, "安装"):
                continue
            install_recipe(home, index, recipes, name, version, args.force)
            save_index(home, index)
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    home = toolbox_home(args.home)
    ensure_layout(home)
    with home_lock(home):
        index = load_index(home)
        if args.name and skip_if_in_path(args.name, args.exe, args.version, args.force, "安装"):
            return 0
        install_direct(
            home,
            index,
            args.spec,
            name=args.name,
            version=args.version,
            sha256=args.sha256,
            exe_hint=args.exe,
            asset_pattern=args.asset,
            force=args.force,
        )
        save_index(home, index)
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    home = toolbox_home(args.home)
    index, adopted = adopt_pending(home)
    report_adopted(adopted)
    try:
        info = resolve_exe(home, index, args.name, args.version)
    except ToolboxError:
        # toolbox 中缺失时，回退到系统 PATH 查找，避免重复安装系统已有的工具
        recipe = load_recipes(home).get(args.name)
        exe_hint = select_exe_hint(recipe, platform_key()) if recipe else None
        found = find_in_path(args.name, exe_hint)
        if not found:
            raise
        info = {"name": args.name, "version": None, "path": found, "source": "system PATH"}
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print(info["path"])
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    home = toolbox_home(args.home)
    with home_lock(home):
        index = load_index(home)
        tool = index.get("tools", {}).get(args.name)
        if not tool:
            raise ToolboxError(f"未安装 `{args.name}`")
        targets = list(tool.get("versions", {})) if args.all or not args.version else [args.version]
        removed = []
        for version in targets:
            entry = tool["versions"].get(version)
            if not entry:
                continue
            target = (home / entry["dir"]).resolve()
            home_root = home.resolve()
            if (
                target == home_root
                or home_root not in target.parents
                or target.name in RESERVED_NAMES
            ):
                raise ToolboxError(f"拒绝删除共享目录之外的路径: {target}")
            shutil.rmtree(target, ignore_errors=True)
            del tool["versions"][version]
            removed.append(version)
        if not tool.get("versions"):
            index["tools"].pop(args.name, None)
        else:
            tool["latest"] = choose_latest(tool["versions"].keys())
        save_index(home, index)
    print(f"已移除 {args.name}: {', '.join(removed) if removed else '(无匹配版本)'}")
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    home = toolbox_home(args.home)
    with home_lock(home):
        index = load_index(home)
        freed = 0
        adopted = adopt_scanned(home, index)

        # 0) 可选：删除非当前平台的版本（换电脑/换架构后清理旧平台二进制）
        if getattr(args, "prune_other_platforms", False):
            current = platform_key()
            for name in list(index["tools"]):
                versions = index["tools"][name].get("versions", {})
                for version in list(versions):
                    entry = versions[version]
                    if entry.get("platform") == current:
                        continue
                    target = (home / entry.get("dir", "")).resolve()
                    home_root = home.resolve()
                    if (
                        target != home_root
                        and home_root in target.parents
                        and target.name not in RESERVED_NAMES
                    ):
                        freed += dir_size(target)
                        shutil.rmtree(target, ignore_errors=True)
                    versions.pop(version, None)
                if not versions:
                    index["tools"].pop(name)
                else:
                    index["tools"][name]["latest"] = choose_latest(versions)

        # 1) 清空安装临时区 downloads/（正常安装后已自动删除，这里兜底中断残留）
        downloads = home / "downloads"
        if downloads.exists():
            for item in downloads.iterdir():
                freed += dir_size(item) if item.is_dir() else item.stat().st_size
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
            try:
                downloads.rmdir()
            except OSError:
                pass
        legacy_lock = home / ".lock"
        if legacy_lock.exists():
            try:
                legacy_lock.unlink()
            except OSError:
                pass
        for stray in home.glob("index.json.tmp"):
            try:
                freed += stray.stat().st_size
                stray.unlink()
            except OSError:
                pass
        freed += cleanup_stale_temp()

        # 2) 移除索引中记录已丢失的条目
        for name in list(index["tools"]):
            versions = index["tools"][name].get("versions", {})
            for version in list(versions):
                exe = home / versions[version].get("exe", "")
                if not exe.exists():
                    versions.pop(version)
            if not versions:
                index["tools"].pop(name)
            else:
                index["tools"][name]["latest"] = choose_latest(versions)

        # 3) 不再需要 bin 启动器；旧版本残留的 bin 目录若只含启动器文件则整体删除
        bin_dir = home / "bin"
        if bin_dir.exists():
            items = list(bin_dir.iterdir())
            if all(p.is_file() for p in items):
                freed += dir_size(bin_dir)
                shutil.rmtree(bin_dir, ignore_errors=True)

        save_index(home, index)
    message = f"回收完成，释放约 {human_size(freed)}"
    if adopted:
        message += f"；已收编目录中未登记工具 {len(adopted)} 个"
    print(message)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    home = toolbox_home(args.home)
    index, adopted = adopt_pending(home)
    problems = []
    total = 0
    indexed_keys = set()
    for name, tool in sorted(index.get("tools", {}).items()):
        for version, entry in sorted(tool.get("versions", {}).items()):
            total += 1
            indexed_keys.add((name, version, entry.get("platform")))
            exe = home / entry.get("exe", "")
            if not exe.exists():
                problems.append(f"{name} {version}: 可执行文件缺失 {exe}")
                continue
            if entry.get("exe_sha256"):
                actual = sha256_file(exe)
                if actual != entry["exe_sha256"]:
                    problems.append(f"{name} {version}: 哈希不匹配（可能已损坏或被替换）")
    unindexed = [
        f"{r['name']} {r['version']} ({r['platform']})"
        for r in scan_tools(home, skip_names=frozenset(index.get("tools", {})), skip_dirs=indexed_dirs(home, index))
        if (r["name"], r["version"], r["platform"]) not in indexed_keys
    ]
    if args.json:
        print(json.dumps({"home": str(home), "entries": total, "adopted": adopted, "problems": problems, "unindexed": unindexed}, ensure_ascii=False, indent=2))
    else:
        print(f"共享目录: {home}")
        print(f"检查条目: {total}")
        if adopted:
            print("本次已自动登记:")
            for item in adopted:
                print("  + " + item)
        if problems:
            print("发现问题:")
            for item in problems:
                print("  - " + item)
        if unindexed:
            print("目录中已存在但 index.json 未登记（可运行 toolbox repair 收编）:")
            for item in unindexed:
                print("  - " + item)
        if not problems and not unindexed:
            print("一切正常。")
    return 1 if problems else 0


def cmd_repair(args: argparse.Namespace) -> int:
    """校验索引与工具目录、收编未登记工具；整目录搬迁后运行。"""
    home = toolbox_home(args.home)
    with home_lock(home):
        index = load_index(home)
        adopted = adopt_scanned(home, index)
        verified = 0
        missing: list[str] = []
        mismatched = 0
        for name, tool in sorted(index.get("tools", {}).items()):
            versions = tool.get("versions", {})
            if not versions:
                continue
            latest = tool.get("latest") or choose_latest(versions)
            tool["latest"] = latest
            entry = versions.get(latest)
            exe = home / entry["exe"] if entry else None
            if not entry or not exe or not exe.exists():
                missing.append(f"{name} {latest}")
                continue
            verified += 1
            if entry.get("exe_sha256"):
                actual = sha256_file(exe)
                if actual != entry["exe_sha256"]:
                    mismatched += 1
                    if args.update_hash:
                        entry["exe_sha256"] = actual
        # 旧版本留下的 bin 启动器目录不再需要
        bin_dir = home / "bin"
        if bin_dir.exists() and all(p.is_file() for p in bin_dir.iterdir()):
            shutil.rmtree(bin_dir, ignore_errors=True)
        save_index(home, index)
    if args.json:
        print(json.dumps({"home": str(home), "adopted": adopted, "verified": verified, "missing": missing, "hash_mismatch": mismatched}, ensure_ascii=False, indent=2))
    else:
        print(f"共享目录: {home}")
        if adopted:
            print(f"已从目录收编未登记工具: {len(adopted)} 个")
            for item in adopted:
                print("  + " + item)
        print(f"已校验工具: {verified} 个（工具目录直接存放可执行文件，无需启动器）")
        if missing:
            print("索引存在但文件缺失（建议重新 ensure）：")
            for item in missing:
                print("  - " + item)
        if mismatched:
            suffix = "（已更新索引哈希）" if args.update_hash else "（可用 --update-hash 写回）"
            print(f"哈希不一致: {mismatched} 个{suffix}")
        if not missing and not mismatched:
            print("索引与工具目录一致，可直接使用。")
    return 1 if missing else 0


def cmd_where(args: argparse.Namespace) -> int:
    home = toolbox_home(args.home)
    ensure_layout(home)
    if args.home:
        source = "--home"
    elif (os.environ.get("TOOLBOX_HOME") or "").strip().lower() not in ("", "auto"):
        source = "TOOLBOX_HOME"
    else:
        pref = preferred_fixed_drive()
        source = f"auto(优先{pref}盘)" if pref else "auto(最后一个固定盘)"
    info = {
        "home": str(home),
        "source": source,
        "tools": str(home),
        "downloads": str(home / "downloads"),
        "temp": str(Path(tempfile.gettempdir())),
        "index": str(home / "index.json"),
    }
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print(info["home"])
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="toolbox",
        description="统一管理工程依赖的便携式 CLI 工具：一个共享目录，用时先查找，缺失再下载。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  toolbox list
  toolbox find rip
  toolbox ensure ripgrep
  toolbox ensure fd@10.5.1
  toolbox path ripgrep
  toolbox get github:jqlang/jq --asset jq-windows-amd64.exe --name jq
  toolbox get https://example.com/tool.zip --name mytool --exe mytool.exe
""",
    )
    parser.add_argument(
        "--home",
        help="覆盖共享目录；默认取最后一个固定盘根目录的 <盘符>:\\toolbox，或 TOOLBOX_HOME（设为 auto 则按机器自动解析）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出，便于程序解析")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", aliases=["ls"], help="列出共享目录中已安装的工具")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("find", aliases=["search"], help="查找已安装或可下载的工具")
    p.add_argument("query", nargs="?", default="", help="名称关键字，留空列出全部")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("ensure", aliases=["install"], help="按内置 recipe 确保工具已安装")
    p.add_argument("names", nargs="+", help="工具名，可写 name@version；支持多个")
    p.add_argument("--force", action="store_true", help="强制重新下载安装")
    p.set_defaults(func=cmd_ensure)

    p = sub.add_parser("sync", help="按项目清单安装缺失的工具")
    p.add_argument("--file", help="清单路径（默认 ./.toolbox.json 或 ./.toolbox）")
    p.add_argument("--force", action="store_true", help="强制重新下载安装")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("get", help="从直链或 GitHub Release 安装任意工具")
    p.add_argument("spec", help="URL 或 github:owner/repo[@tag]")
    p.add_argument("--name", help="工具名（默认自动推断）")
    p.add_argument("--version", help="记录到索引的版本号")
    p.add_argument("--sha256", help="期望的 sha256（不填则尝试使用 release 的 .sha256）")
    p.add_argument("--exe", help="压缩包内可执行文件路径或文件名")
    p.add_argument("--asset", help="GitHub release 资产名或通配模式")
    p.add_argument("--force", action="store_true", help="强制重新下载安装")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("path", aliases=["which"], help="打印工具可执行文件的绝对路径")
    p.add_argument("name")
    p.add_argument("--version")
    p.set_defaults(func=cmd_path)

    p = sub.add_parser("remove", aliases=["rm", "uninstall"], help="移除工具")
    p.add_argument("name")
    p.add_argument("--version")
    p.add_argument("--all", action="store_true", help="移除该工具的所有版本")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("gc", help="清理下载缓存与未引用版本")
    p.add_argument(
        "--prune-other-platforms",
        action="store_true",
        help="同时删除非当前平台的版本（换电脑或换 CPU 架构后清理旧二进制）",
    )
    p.set_defaults(func=cmd_gc)

    p = sub.add_parser("doctor", help="校验索引、文件和哈希")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("repair", aliases=["relink"], help="校验索引与工具目录，收编未登记工具")
    p.add_argument("--update-hash", action="store_true", help="把当前文件哈希写回索引")
    p.set_defaults(func=cmd_repair)

    p = sub.add_parser("where", aliases=["home"], help="显示共享目录位置")
    p.set_defaults(func=cmd_where)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ToolboxError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
