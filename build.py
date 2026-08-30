#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build.py - SeewoGuard 统一构建脚本
=================================
同时支持 Nuitka 与 PyInstaller 两种打包方式, 通过命令行参数选择。

所有路径均基于脚本自身位置推导, 不含任何绝对路径, 也不依赖特定机器环境,
可在 Windows / Linux / macOS 上直接运行。

常用命令:
    python build.py                      # 默认 PyInstaller + onefile
    python build.py --tool nuitka        # 改用 Nuitka 打包
    python build.py --onedir             # 改为单目录模式
    python build.py --onedir --tool nuitka
    python build.py --console            # 保留控制台窗口 (调试用)
    python build.py --clean              # 彻底清理 build/ 与 dist/ 后重建
    python build.py --skip-dep-check     # 跳过 requirements.txt 依赖校验
    python build.py --version            # 仅打印版本号后退出

产物命名:
    可执行文件默认命名为 <EXE_BASE_NAME>_<打包工具>, 即
    PyInstaller -> seewokiller_pyinstaller(.exe)
    Nuitka      -> seewokiller_nuitka(.exe)

增量策略:
    默认保留 build/ 中的中间产物只做增量更新; 需要彻底清理时加 --clean。

退出码:
    0   构建成功
    1   打包工具执行失败
    2   配置或依赖错误
    3   打包工具未安装
    4   构建结束但找不到产物
   130  被用户中断
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections import namedtuple
from pathlib import Path

# ==========================================================================
#  配置区  ——  需要调整打包行为时, 只改这一段即可
# ==========================================================================

# ---- 基本信息 ----
APP_NAME = "SeewoGuard"          # 产品名 (发布包 / 文档 / spec 兼容清理用)
EXE_BASE_NAME = "seewokiller"    # 可执行文件名前缀, 最终产物为 <前缀>_<打包工具>
ENTRY_SCRIPT = "main.py"         # 入口脚本, 相对项目根目录
PACKAGE_DIR = "seewo_guard"      # 主包名, 其子模块会被自动加入隐藏导入
ICON_FILE = "icon.ico"           # 程序图标 (.ico / .icns; 非 Windows 平台自动忽略)

# ---- 版本号来源 ----
VERSION_FILE = "seewo_guard/__init__.py"   # 从中读取 __version__
VERSION_PATTERN = r'__version__\s*=\s*["\']([^"\']+)["\']'
FALLBACK_VERSION = "0.0.0"

# ---- 依赖 ----
REQUIREMENTS_FILE = "requirements.txt"     # 打包前据此校验依赖

# ---- 随程序分发的数据文件 (相对项目根, 不存在的文件会被自动跳过) ----
DATA_FILES = [
    "icon.ico",
    "uiaccess.dll",
]

# ---- 目录 ----
BUILD_DIR = "build"              # 中间产物目录 (打包前会被清理)
DIST_DIR = "dist"                # 最终产物目录 (打包前会被清理)

# ---- 打包方式默认值 (可被命令行参数覆盖) ----
DEFAULT_TOOL = "pyinstaller"     # pyinstaller | nuitka
ONEFILE = True                   # True = 单文件, False = 单目录
CONSOLE = False                  # False = 隐藏控制台窗口 (仅 Windows / macOS 生效)

# ---- 额外隐藏导入: PyInstaller 静态分析可能遗漏的模块 ----
# 说明: PACKAGE_DIR 下的子模块会自动追加, 无需在此重复填写。
HIDDEN_IMPORTS = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "psutil",
    "logging.handlers",
]

# ---- 排除模块: 减小产物体积, 均为本项目未使用 ----
EXCLUDE_MODULES = [
    "tkinter",
    "unittest",
    "pydoc",
    "pytest",
    "numpy",
]

# ---- 传递给打包工具的额外参数 (按需追加, 会拼在命令末尾) ----
PYINSTALLER_EXTRA_ARGS: list[str] = []
NUITKA_EXTRA_ARGS: list[str] = []

# ---- 分发包名 -> 导入名 (部分包的导入名与 pip 名不一致) ----
IMPORT_NAME_OVERRIDES = {
    "pyside6": "PySide6",
    "pyside6-essentials": "PySide6",
    "pyside6-addons": "PySide6",
    "pillow": "PIL",
    "pywin32": "win32api",
    "pyyaml": "yaml",
}

# ---- 构建工具导入名 ----
TOOL_IMPORT_NAMES = {
    "pyinstaller": "PyInstaller",
    "nuitka": "nuitka",
}


def output_name(tool: str) -> str:
    """按打包工具生成产物名: <EXE_BASE_NAME>_<tool>。"""
    return f"{EXE_BASE_NAME}_{tool}"

# ==========================================================================
#  以下为实现, 一般无需修改
# ==========================================================================

EXIT_OK = 0
EXIT_BUILD_FAILED = 1
EXIT_CONFIG_ERROR = 2
EXIT_TOOL_MISSING = 3
EXIT_ARTIFACT_MISSING = 4
EXIT_INTERRUPTED = 130

IS_WINDOWS = os.name == "nt" or sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

PROJECT_ROOT = Path(__file__).resolve().parent

# 需求条目: name=分发包名, specifier=版本约束, marker=环境标记
Requirement = namedtuple("Requirement", ["name", "specifier", "marker"])


class BuildError(Exception):
    """构建过程中的可控异常, 携带退出码。"""

    def __init__(self, message: str, code: int = EXIT_BUILD_FAILED, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint


# --------------------------------------------------------------------------
#  输出辅助
# --------------------------------------------------------------------------
def _force_utf8_stdio() -> None:
    """强制标准输出使用 UTF-8, 避免 Windows 控制台代码页导致中文乱码或报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def info(msg: str = "") -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def detail(msg: str) -> None:
    print(f"        {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[警告 ] {msg}", file=sys.stderr, flush=True)


def error(msg: str) -> None:
    print(f"[错误 ] {msg}", file=sys.stderr, flush=True)


def banner(title: str) -> None:
    line = "=" * 62
    info(line)
    info(f"  {title}")
    info(line)


# --------------------------------------------------------------------------
#  版本与依赖
# --------------------------------------------------------------------------
def read_version() -> str:
    """从 VERSION_FILE 中读取 __version__。"""
    path = PROJECT_ROOT / VERSION_FILE
    if not path.is_file():
        warn(f"未找到版本文件 {VERSION_FILE}, 使用兜底版本号 {FALLBACK_VERSION}")
        return FALLBACK_VERSION
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        warn(f"读取 {VERSION_FILE} 失败 ({exc}), 使用兜底版本号 {FALLBACK_VERSION}")
        return FALLBACK_VERSION
    match = re.search(VERSION_PATTERN, text)
    if not match:
        warn(f"在 {VERSION_FILE} 中未找到 __version__, 使用兜底版本号 {FALLBACK_VERSION}")
        return FALLBACK_VERSION
    return match.group(1).strip()


def _load_packaging():
    """尝试加载 packaging, 不可用则返回 None (此时退化为仅检查是否已安装)。"""
    try:
        from packaging.markers import Marker  # noqa: F401
        from packaging.requirements import Requirement as PkgRequirement  # noqa: F401
        from packaging.specifiers import SpecifierSet  # noqa: F401
        from packaging.version import InvalidVersion, Version  # noqa: F401

        return {
            "Marker": Marker,
            "Requirement": PkgRequirement,
            "SpecifierSet": SpecifierSet,
            "Version": Version,
            "InvalidVersion": InvalidVersion,
        }
    except Exception:
        return None


def parse_requirements(path: Path) -> list:
    """解析 requirements.txt, 跳过空行与注释。"""
    pattern = re.compile(
        r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([^;#]*)"
    )
    requirements: list = []
    try:
        raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise BuildError(f"无法读取依赖清单 {path.name}: {exc}", EXIT_CONFIG_ERROR)

    for lineno, raw in enumerate(raw_lines, start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-"):  # -r / -e / --index-url 等选项行, 跳过校验
            continue
        marker = None
        if ";" in line:
            line, marker = line.split(";", 1)
            marker = marker.strip() or None
        match = pattern.match(line)
        if not match:
            warn(f"{path.name} 第 {lineno} 行无法解析, 已跳过: {raw!r}")
            continue
        name = match.group(1).strip()
        specifier = match.group(2).strip()
        requirements.append(Requirement(name, specifier, marker))
    return requirements


def _installed_version(dist_name: str):
    """查询已安装分发包的版本号, 未安装返回 None。"""
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - Python < 3.8
        try:
            import pkg_resources
        except ImportError:
            return None
        try:
            return pkg_resources.get_distribution(dist_name).version
        except Exception:
            return None

    candidates = [dist_name, dist_name.replace("-", "_"), dist_name.replace("_", "-")]
    for candidate in candidates:
        try:
            return version(candidate)
        except PackageNotFoundError:
            continue
        except Exception:
            continue
    return None


def _is_importable(dist_name: str) -> bool:
    """兜底检查: 该依赖能否被 import (用于导入名与分发名不一致的情况)。"""
    import importlib.util

    import_name = IMPORT_NAME_OVERRIDES.get(dist_name.lower(), dist_name.replace("-", "_"))
    for candidate in (import_name, dist_name, dist_name.replace("-", "_")):
        try:
            if importlib.util.find_spec(candidate) is not None:
                return True
        except Exception:
            continue
    return False


def _marker_applies(marker: str, packaging) -> bool:
    """判断环境标记是否适用于当前平台。"""
    if not marker:
        return True
    if not packaging:
        return True  # 没有 packaging 时保守处理, 仍参与校验
    try:
        return bool(packaging["Marker"](marker).evaluate())
    except Exception:
        return True


def check_requirements() -> None:
    """校验 requirements.txt 中的依赖是否已安装且满足版本约束。"""
    req_file = PROJECT_ROOT / REQUIREMENTS_FILE
    if not req_file.is_file():
        warn(f"未找到 {REQUIREMENTS_FILE}, 跳过依赖校验 "
             f"(建议创建该文件以便复现环境)")
        return

    requirements = parse_requirements(req_file)
    if not requirements:
        warn(f"{REQUIREMENTS_FILE} 中没有可校验的条目, 跳过依赖校验")
        return

    packaging = _load_packaging()
    if packaging is None:
        warn("未安装 packaging, 仅检查依赖是否已安装, 不校验版本约束")

    step(f"校验依赖 ({REQUIREMENTS_FILE})")
    missing: list[str] = []
    mismatched: list[str] = []
    satisfied: list[str] = []

    for req in requirements:
        if not _marker_applies(req.marker, packaging):
            detail(f"跳过 (平台不匹配): {req.name}")
            continue

        installed = _installed_version(req.name)
        label = f"{req.name}{req.specifier}"

        if installed is None:
            if _is_importable(req.name):
                satisfied.append(f"{req.name} (导入可用, 版本未知)")
                detail(f"OK      {label} -> 导入可用 (版本未知)")
                continue
            missing.append(label)
            detail(f"缺失    {label}")
            continue

        if req.specifier and packaging:
            try:
                spec = packaging["SpecifierSet"](req.specifier)
                if not spec.contains(packaging["Version"](installed), prereleases=True):
                    mismatched.append(f"{label} (已安装 {installed})")
                    detail(f"版本不符 {label} -> 已安装 {installed}")
                    continue
            except packaging["InvalidVersion"]:
                warn(f"{req.name} 已安装版本号 {installed} 不合法, 跳过版本校验")
            except Exception as exc:
                warn(f"{req.name} 版本约束 {req.specifier!r} 解析失败 ({exc}), 跳过版本校验")

        satisfied.append(f"{req.name}=={installed}")
        detail(f"OK      {label} -> {installed}")

    detail(f"共 {len(satisfied)} 项满足, {len(missing)} 项缺失, "
           f"{len(mismatched)} 项版本不符")

    if missing or mismatched:
        parts = []
        if missing:
            parts.append("缺失依赖: " + ", ".join(missing))
        if mismatched:
            parts.append("版本不满足: " + ", ".join(mismatched))
        raise BuildError(
            "依赖校验未通过: " + " | ".join(parts),
            EXIT_CONFIG_ERROR,
            hint=f"请执行: pip install -r {REQUIREMENTS_FILE}",
        )


# --------------------------------------------------------------------------
#  文件收集
# --------------------------------------------------------------------------
def discover_package_modules() -> list:
    """自动发现主包下的所有子模块, 作为隐藏导入候补。"""
    pkg_dir = PROJECT_ROOT / PACKAGE_DIR
    if not pkg_dir.is_dir():
        return []
    modules = []
    for py_file in sorted(pkg_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        modules.append(f"{PACKAGE_DIR}.{py_file.stem}")
    return modules


def collect_hidden_imports() -> list:
    """合并手工配置与自动发现的隐藏导入, 去重并保持顺序。"""
    candidates = list(HIDDEN_IMPORTS)
    candidates.extend(discover_package_modules())
    if IS_WINDOWS:
        candidates.append("winreg")  # window_ops 中延迟导入, 显式声明更稳妥

    result: list = []
    seen = set()
    for name in candidates:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def collect_data_files() -> list:
    """收集存在的数据文件, 返回 [(绝对路径, 分发后的相对目标目录)]。

    目标目录使用 "." 表示分发根目录; 元素也支持写成
    ("assets/x.dat", "sub/dir") 这样的二级目录形式。
    """
    found = []
    for item in DATA_FILES:
        if isinstance(item, (tuple, list)):
            rel, dest_dir = item[0], item[1]
        else:
            rel, dest_dir = item, "."

        src = PROJECT_ROOT / rel
        if src.is_file():
            found.append((str(src), dest_dir or "."))
        else:
            warn(f"数据文件不存在, 已跳过: {rel}")
    return found


def resolve_icon():
    """返回可用的图标路径; 非 Windows 平台或文件不存在时返回 None。"""
    if not IS_WINDOWS:
        return None
    icon = PROJECT_ROOT / ICON_FILE
    if icon.is_file():
        return str(icon)
    warn(f"图标文件不存在, 将使用默认图标: {ICON_FILE}")
    return None


def _add_data_arg(src: str, dst: str) -> str:
    """PyInstaller --add-data 参数 (分隔符随平台变化)。"""
    return f"{src}{os.pathsep}{dst}"


def human_size(num_bytes: float) -> str:
    """字节数转人类可读字符串。"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.2f} TB"


def path_size(path: Path) -> int:
    """计算文件或目录占用的总字节数。"""
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            file_path = Path(root) / name
            try:
                if not file_path.is_symlink():
                    total += file_path.stat().st_size
            except OSError:
                continue
    return total


# --------------------------------------------------------------------------
#  清理
# --------------------------------------------------------------------------
def _rmtree_onerror(func, path, _exc_info) -> None:
    """删除失败时尝试加写权限后重试。"""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except Exception:
        warn(f"无法删除 (可能被占用), 已跳过: {path}")


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, onerror=_rmtree_onerror)
    else:
        try:
            path.unlink()
        except OSError:
            _rmtree_onerror(os.unlink, str(path), None)


def clean_outputs() -> None:
    """清理上一轮的中间产物与最终产物 (仅 --clean 时调用)。"""
    step("清理旧产物 (--clean)")
    targets = [PROJECT_ROOT / BUILD_DIR, PROJECT_ROOT / DIST_DIR]
    # 兼容历史遗留: 直接在项目根生成的 Nuitka 中间目录 / PyInstaller spec
    stem = Path(ENTRY_SCRIPT).stem
    targets.extend([
        PROJECT_ROOT / f"{stem}.build",
        PROJECT_ROOT / f"{stem}.dist",
        PROJECT_ROOT / f"{stem}.onefile-build",
        PROJECT_ROOT / f"{APP_NAME}.spec",
        PROJECT_ROOT / f"{stem}.spec",
        PROJECT_ROOT / f"{output_name('pyinstaller')}.spec",
        PROJECT_ROOT / f"{output_name('nuitka')}.spec",
    ])
    removed_any = False
    for target in targets:
        if target.exists():
            detail(f"删除 {target.relative_to(PROJECT_ROOT)}")
            _remove_path(target)
            removed_any = True
    if not removed_any:
        detail("没有需要清理的旧产物")


# --------------------------------------------------------------------------
#  工具可用性
# --------------------------------------------------------------------------
def ensure_tool_available(tool: str) -> None:
    """确认打包工具已安装在当前 Python 环境中。"""
    module_name = TOOL_IMPORT_NAMES[tool]
    display = "PyInstaller" if tool == "pyinstaller" else "Nuitka"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", f"import {module_name}"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
        )
    except Exception as exc:
        raise BuildError(f"检查 {display} 是否安装时出错: {exc}", EXIT_TOOL_MISSING)

    if proc.returncode != 0:
        output = (proc.stdout or b"").decode("utf-8", "replace").strip()
        raise BuildError(
            f"未检测到打包工具 {display} (无法 import {module_name})",
            EXIT_TOOL_MISSING,
            hint=f"请执行: pip install {tool}" + (f"\n        工具输出: {output}" if output else ""),
        )


# --------------------------------------------------------------------------
#  PyInstaller
# --------------------------------------------------------------------------
def build_with_pyinstaller(onefile: bool, console: bool, hidden: list, data: list,
                           full_clean: bool = False) -> Path:
    """调用 PyInstaller 打包。full_clean 时让 PyInstaller 丢弃缓存全量重建。"""
    step("调用 PyInstaller 打包中 ...")
    entry = PROJECT_ROOT / ENTRY_SCRIPT
    work_dir = PROJECT_ROOT / BUILD_DIR
    dist_dir = PROJECT_ROOT / DIST_DIR

    cmd = [
        sys.executable, "-m", "PyInstaller",
        str(entry),
        "--name", output_name("pyinstaller"),
        "--distpath", str(dist_dir),
        "--workpath", str(work_dir),
        "--specpath", str(work_dir),
        "--noconfirm",
        "--log-level", "WARN",
    ]
    if full_clean:
        cmd.append("--clean")
    cmd.append("--onefile" if onefile else "--onedir")

    if console:
        cmd.append("--console")
    elif IS_WINDOWS or IS_MACOS:
        cmd.append("--noconsole")
    # Linux 下无 --noconsole 概念, 保持默认即可

    icon = resolve_icon()
    if icon:
        cmd.extend(["--icon", icon])

    for src, dst in data:
        cmd.extend(["--add-data", _add_data_arg(src, dst)])

    for module_name in hidden:
        cmd.extend(["--hidden-import", module_name])

    for module_name in EXCLUDE_MODULES:
        cmd.extend(["--exclude-module", module_name])

    cmd.extend(PYINSTALLER_EXTRA_ARGS)

    detail("命令: " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    return _run(cmd, "PyInstaller")


# --------------------------------------------------------------------------
#  Nuitka
# --------------------------------------------------------------------------
_NUITKA_HELP_CACHE = None


def _nuitka_help() -> str:
    """获取 `nuitka --help` 输出, 用于探测选项兼容性 (只取一次)。"""
    global _NUITKA_HELP_CACHE
    if _NUITKA_HELP_CACHE is not None:
        return _NUITKA_HELP_CACHE
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "nuitka", "--help"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=300,
        )
        text = (proc.stdout or b"").decode("utf-8", "replace")
    except Exception:
        text = ""
    _NUITKA_HELP_CACHE = text
    return text


def _nuitka_supports(option: str) -> bool:
    """判断当前 Nuitka 版本是否支持某个命令行选项。"""
    return option in _nuitka_help()


def _nuitka_data_arg(src: str, dst: str) -> str:
    """构造 Nuitka 的 --include-data-files 参数。

    与 PyInstaller 不同, Nuitka 的等号右侧必须是「具体文件名」或
    「以 / 结尾的目录」, 直接写 "." 会被当成文件名而打不到正确位置,
    因此这里统一补上源文件的文件名。
    """
    name = Path(src).name
    if dst in ("", ".", "./", ".\\"):
        target = name
    else:
        target = f"{dst.rstrip('/').rstrip(chr(92))}/{name}"
    return f"--include-data-files={src}={target}"


def build_with_nuitka(onefile: bool, console: bool, data: list) -> Path:
    """调用 Nuitka 打包。

    注意: Nuitka 的带值参数必须用 `--opt=value` 连写形式, 用空格分隔
    会报 "The '--xxx' option requires an argument with '--xxx='."。
    """
    step("调用 Nuitka 打包中 (耗时较长, 请耐心等待) ...")
    entry = PROJECT_ROOT / ENTRY_SCRIPT
    dist_dir = PROJECT_ROOT / DIST_DIR

    cmd = [
        sys.executable, "-m", "nuitka",
        str(entry),
        "--standalone",
        f"--output-dir={dist_dir}",
        f"--output-filename={output_name('nuitka')}",
        "--enable-plugin=pyside6",
        f"--include-package={PACKAGE_DIR}",
    ]
    if onefile:
        cmd.append("--onefile")
        # 缺 zstandard 时 onefile 不压缩, 体积明显偏大
        try:
            import zstandard  # noqa: F401
        except ImportError:
            warn("未安装 zstandard, Nuitka onefile 将不做压缩 (体积偏大)")
            detail("可选优化: pip install zstandard")

    # 允许自动下载构建依赖 (MinGW / ccache 等), 便于 CI 无人值守
    if _nuitka_supports("--assume-yes-for-downloads"):
        cmd.append("--assume-yes-for-downloads")

    if not console and IS_WINDOWS:
        if _nuitka_supports("--windows-console-mode"):
            cmd.append("--windows-console-mode=disable")
        elif _nuitka_supports("--windows-disable-console"):
            cmd.append("--windows-disable-console")
        else:
            warn("当前 Nuitka 版本未识别隐藏控制台的选项, 将保留控制台窗口")

    icon = resolve_icon()
    if icon:
        if IS_WINDOWS and _nuitka_supports("--windows-icon-from-ico"):
            cmd.append(f"--windows-icon-from-ico={icon}")

    for src, dst in data:
        cmd.append(_nuitka_data_arg(src, dst))

    for module_name in EXCLUDE_MODULES:
        cmd.append(f"--nofollow-import-to={module_name}")

    cmd.extend(NUITKA_EXTRA_ARGS)

    detail("命令: " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    return _run(cmd, "Nuitka")


def _run(cmd: list, tool_name: str) -> Path:
    """执行命令并实时输出; 失败时抛出中文错误。"""
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if proc.returncode != 0:
        raise BuildError(
            f"{tool_name} 打包失败, 退出码 {proc.returncode}",
            EXIT_BUILD_FAILED,
            hint="请向上滚动查看工具输出的完整错误信息",
        )
    return PROJECT_ROOT / DIST_DIR


# --------------------------------------------------------------------------
#  产物定位与汇报
# --------------------------------------------------------------------------
def locate_artifact(tool: str, onefile: bool) -> Path:
    """按优先级在 dist/ 中定位构建产物。"""
    dist_dir = PROJECT_ROOT / DIST_DIR
    exe_suffix = ".exe" if IS_WINDOWS else ""
    stem = Path(ENTRY_SCRIPT).stem

    candidates: list = []
    name = output_name(tool)
    if tool == "pyinstaller":
        if onefile:
            candidates.append(dist_dir / f"{name}{exe_suffix}")
        if IS_MACOS:
            candidates.append(dist_dir / f"{name}.app")
        candidates.append(dist_dir / name)
    else:  # nuitka
        if onefile:
            candidates.append(dist_dir / f"{name}{exe_suffix}")
            candidates.append(dist_dir / f"{name}.bin")
            candidates.append(dist_dir / name)
        # Nuitka 单目录模式: 目录名可能取自入口模块名或输出名
        candidates.append(dist_dir / f"{stem}.dist")
        candidates.append(dist_dir / f"{name}.dist")

    for candidate in candidates:
        if candidate.exists():
            return candidate

    # 兜底: dist/ 下任何看起来像产物的条目
    if dist_dir.is_dir():
        leftovers = sorted(p for p in dist_dir.iterdir() if not p.name.startswith("."))
        if leftovers:
            return leftovers[0]

    raise BuildError(
        f"构建流程已结束, 但在 {DIST_DIR}/ 中找不到产物",
        EXIT_ARTIFACT_MISSING,
        hint="请检查打包工具输出, 确认入口脚本与依赖是否正确",
    )


def report(artifact: Path, tool: str, onefile: bool, elapsed: float, version: str) -> None:
    """输出产物路径、大小等汇总信息。"""
    rel = artifact.relative_to(PROJECT_ROOT)
    size = path_size(artifact)
    kind = "单文件" if onefile else "单目录"
    tool_name = "PyInstaller" if tool == "pyinstaller" else "Nuitka"

    banner("构建完成")
    detail(f"工具      : {tool_name}")
    detail(f"模式      : {kind} ({'onefile' if onefile else 'onedir'})")
    detail(f"版本      : {version}")
    detail(f"平台      : {sys.platform}")
    detail(f"产物路径  : {rel}")
    detail(f"产物大小  : {human_size(size)} ({size} 字节)")
    if artifact.is_dir():
        try:
            entries = len(list(artifact.rglob("*")))
            detail(f"包含条目  : {entries} 个文件/目录")
        except Exception:
            pass
    detail(f"耗时      : {elapsed:.1f} 秒")
    info("=" * 62)


# --------------------------------------------------------------------------
#  入口
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build.py",
        description="SeewoGuard 构建脚本 (支持 PyInstaller 与 Nuitka)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python build.py\n"
            "  python build.py --tool nuitka\n"
            "  python build.py --onedir --tool nuitka\n"
        ),
    )
    parser.add_argument(
        "--tool",
        choices=sorted(TOOL_IMPORT_NAMES),
        default=DEFAULT_TOOL,
        help=f"选择打包工具 (默认: {DEFAULT_TOOL})",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--onefile",
        dest="onefile",
        action="store_true",
        default=None,
        help="单文件模式",
    )
    mode.add_argument(
        "--onedir",
        dest="onefile",
        action="store_false",
        help="单目录模式",
    )
    parser.add_argument(
        "--console",
        action="store_true",
        default=None,
        help="保留控制台窗口 (调试用, 默认隐藏)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="构建前彻底清理 build/ 与 dist/ (默认增量更新, 保留中间产物)",
    )
    parser.add_argument(
        "--skip-dep-check",
        action="store_true",
        help=f"跳过 {REQUIREMENTS_FILE} 依赖校验",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="仅打印项目版本号后退出",
    )
    return parser


def validate_config() -> None:
    """校验顶部常量区配置是否自洽。"""
    entry = PROJECT_ROOT / ENTRY_SCRIPT
    if not entry.is_file():
        raise BuildError(
            f"入口脚本不存在: {ENTRY_SCRIPT}",
            EXIT_CONFIG_ERROR,
            hint="请在 build.py 顶部配置区修正 ENTRY_SCRIPT",
        )
    if not (PROJECT_ROOT / PACKAGE_DIR).is_dir():
        raise BuildError(
            f"主包目录不存在: {PACKAGE_DIR}",
            EXIT_CONFIG_ERROR,
            hint="请在 build.py 顶部配置区修正 PACKAGE_DIR",
        )
    if DEFAULT_TOOL not in TOOL_IMPORT_NAMES:
        raise BuildError(
            f"DEFAULT_TOOL 取值非法: {DEFAULT_TOOL}",
            EXIT_CONFIG_ERROR,
            hint=f"可选值: {', '.join(sorted(TOOL_IMPORT_NAMES))}",
        )


def main(argv=None) -> int:
    _force_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    version = read_version()
    if args.version:
        info(version)
        return EXIT_OK

    onefile = ONEFILE if args.onefile is None else args.onefile
    console = CONSOLE if args.console is None else args.console
    tool = args.tool

    started = time.time()
    try:
        validate_config()

        banner(f"SeewoGuard 构建  v{version}")
        detail(f"项目根目录: {PROJECT_ROOT}")
        detail(f"入口脚本  : {ENTRY_SCRIPT}")
        exe_suffix = ".exe" if IS_WINDOWS else ""
        detail(f"产物命名  : {output_name(tool)}{exe_suffix}")

        if args.clean:
            clean_outputs()
        else:
            step("增量构建 (默认保留 build/ 中间产物, 只更新 dist/; 彻底清理请加 --clean)")

        if args.skip_dep_check:
            step("已跳过依赖校验 (--skip-dep-check)")
        else:
            check_requirements()

        ensure_tool_available(tool)

        hidden = collect_hidden_imports()
        data = collect_data_files()
        step(f"隐藏导入 {len(hidden)} 项, 数据文件 {len(data)} 项")
        detail("隐藏导入: " + ", ".join(hidden))

        if tool == "pyinstaller":
            build_with_pyinstaller(onefile, console, hidden, data,
                                   full_clean=args.clean)
        else:
            build_with_nuitka(onefile, console, data)

        artifact = locate_artifact(tool, onefile)
        report(artifact, tool, onefile, time.time() - started, version)
        return EXIT_OK

    except BuildError as exc:
        error(exc.message)
        if exc.hint:
            error(f"建议: {exc.hint}")
        return exc.code
    except KeyboardInterrupt:
        error("构建已被用户中断")
        return EXIT_INTERRUPTED
    except FileNotFoundError as exc:
        error(f"缺少必要文件或命令: {exc}")
        return EXIT_CONFIG_ERROR
    except OSError as exc:
        error(f"文件系统操作失败: {exc}")
        return EXIT_CONFIG_ERROR


if __name__ == "__main__":
    sys.exit(main())
