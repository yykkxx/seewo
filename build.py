import os
import shutil  # 不再用 copy2 后可删
import subprocess
import sys

from seewo_guard.config import VERSION

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(HERE, "main.py")
DIST = os.path.join(HERE, "dist")
ICON = os.path.join(HERE, "icon.ico")      # seewo/icon.ico
UIACCESS = os.path.join(HERE, "uiaccess.dll")


def _ensure_assets():
    """资产已随仓库提交到根目录(HERE)，只校验存在性，不复制。"""
    for p, tag in ((ICON, "icon.ico"), (UIACCESS, "uiaccess.dll")):
        if os.path.exists(p):
            print(f"[资源] {tag} -> {p}")
        else:
            print(f"[警告] 缺少 {tag}，打包可能失败: {p}")


def _output_name():
    return os.environ.get("SEEWO_OUTPUT", "SeewoGuard")




def build_pyinstaller():
    _ensure_assets()
    name = _output_name()
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onefile", "--noconsole", "--uac-admin",
        "--optimize=1",
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
        "--windows-console-mode=disable",
        "--windows-uac-admin",
        "--assume-yes-for-downloads",          # ← 新增：自动同意下载 Dependency Walker
        f"--onefile-tempdir-spec={{CACHE_DIR}}/SeewoGuard/{VERSION}",
        "--onefile-cache-mode=cached",
        "--onefile-no-compression",
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
