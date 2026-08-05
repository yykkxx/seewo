# -*- coding: utf-8 -*-
"""
build.py - 一键打包脚本 (PyInstaller / Nuitka)
用法:
  python build.py pyinstaller
  python build.py nuitka
"""
import os
import shutil
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(HERE, "main.py")
DIST = os.path.join(HERE, "dist")
ICON = os.path.join(os.path.dirname(HERE), "icon.ico")   # seewo/icon.ico
UIACCESS = os.path.join(os.path.dirname(HERE), "uiaccess.dll")


def _ensure_assets():
    """把 icon.ico / uiaccess.dll 复制到项目目录 (打包进产物)"""
    for src, name in ((ICON, "icon.ico"), (UIACCESS, "uiaccess.dll")):
        if os.path.exists(src):
            dst = os.path.join(HERE, name)
            shutil.copy2(src, dst)
            print(f"[资源] {name} -> {dst}")
        else:
            print(f"[提示] 未找到 {src}")


def _output_name():
    return os.environ.get("SEEWO_OUTPUT", "SeewoGuard")




def build_pyinstaller():
    _ensure_assets()
    name = _output_name()
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onefile", "--noconsole", "--uac-admin",
        "--name", name,
        "--hidden-import=psutil",
        "--hidden-import=PySide6.QtCore",
        "--hidden-import=PySide6.QtGui",
        "--hidden-import=PySide6.QtWidgets",
        "--icon", os.path.join(HERE, "icon.ico"),
        "--add-data", f"{os.path.join(HERE, 'icon.ico')};.",
        "--add-binary", f"{os.path.join(HERE, 'uiaccess.dll')};.",
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", HERE,
        ENTRY,
    ]
    print(f"[PyInstaller] {' '.join(cmd)}")
    subprocess.run(cmd, check=True)
    print(f"\n✅ 打包完成: dist\\{name}.exe")


def build_nuitka():
    _ensure_assets()
    name = _output_name()
    cmd = [
        sys.executable, "-m", "nuitka",
        "--onefile",
        "--windows-disable-console",
        "--windows-uac-admin",
        "--enable-plugin=pyside6",
        f"--windows-icon-from-ico={os.path.join(HERE, 'icon.ico')}",
        "--include-package=psutil",
        f"--include-data-file={os.path.join(HERE, 'icon.ico')}=icon.ico",
        f"--include-data-file={os.path.join(HERE, 'uiaccess.dll')}=uiaccess.dll",
        f"--output-dir={os.path.join(HERE, 'dist')}",
        f"--output-filename={name}.exe",
        ENTRY,
    ]
    print(f"[Nuitka] {' '.join(cmd)}")
    subprocess.run(cmd, check=True)
    print(f"\n✅ 打包完成: dist\\{name}.exe")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python build.py [pyinstaller|nuitka]")
        sys.exit(1)
    mode = sys.argv[1].lower()
    if mode == "pyinstaller":
        build_pyinstaller()
    elif mode == "nuitka":
        build_nuitka()
    else:
        print(f"未知模式: {mode}")
        sys.exit(1)
