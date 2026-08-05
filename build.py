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


def build_pyinstaller():
    _ensure_assets()
    cmd = [
        sys.executable, "-m", "pyinstaller",
        "--noconfirm", "--onefile", "--noconsole", "--uac-admin",
        "--name", "SeewoGuard",
        "--hidden-import=psutil",
        "--collect-all", "PySide6",
        "--add-data", f"{os.path.join(HERE, 'icon.ico')};.",
        "--add-binary", f"{os.path.join(HERE, 'uiaccess.dll')};.",
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", HERE,
        ENTRY,
    ]
    print(f"[PyInstaller] {' '.join(cmd)}")
    subprocess.run(cmd, check=True)
    print("\n✅ 打包完成: dist\\SeewoGuard.exe")


def build_nuitka():
    _ensure_assets()
    cmd = [
        sys.executable, "-m", "nuitka",
        "--onefile",
        "--windows-disable-console",
        "--windows-uac-admin",
        "--enable-plugin=pyside6",
        "--include-package=psutil",
        "--include-data-file", f"{os.path.join(HERE, 'icon.ico')}=icon.ico",
        "--include-data-file", f"{os.path.join(HERE, 'uiaccess.dll')}=uiaccess.dll",
        "--output-dir", os.path.join(HERE, "dist"),
        "--output-filename=SeewoGuard.exe",
        ENTRY,
    ]
    print(f"[Nuitka] {' '.join(cmd)}")
    subprocess.run(cmd, check=True)
    print("\n✅ 打包完成: dist\\SeewoGuard.exe")


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
