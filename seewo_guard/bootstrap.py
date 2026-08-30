# -*- coding: utf-8 -*-
"""GUI 启动前的轻量权限引导。

该模块不导入 PySide6、psutil 或 GUI 模块，确保普通管理员进程在跳转到
UIAccess 实例前不会重复支付完整 GUI 导入成本。

启动链:
    普通进程
      -> 若非管理员: ShellExecuteW(runas) 请求 UAC, 当前进程退出
      -> 若已是管理员: 用 uiaccess.dll 的 StartUIAccessProcess 拉起一个
         UIAccess 实例, 并由它运行真正的 GUI, 当前进程退出

为什么需要 UIAccess: 只有 UIAccess 完整性级别的进程才能在窗口 Z 序上
压过同样以高完整性运行的希沃窗口。希沃会周期性把窗口重新置顶, 普通
管理员进程的 SetWindowPos 会被它覆盖; 拿到 UIAccess 后本程序的
「最小化置底」才能压住 (见 window_ops.minimize_target_windows_to_bottom)。
UIAccess 要求可执行文件位于受信任目录 (Program Files 等), 拉不起来时会
跳过并直接用当前管理员进程运行 GUI, 功能降级但不报错。
"""
import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

from seewo_guard.config import IS_FROZEN, TEST_MODE, resource_path, self_exe


_PACKAGER_ENV_PREFIXES = ("_PYI_", "NUITKA_")


class _CleanPackagerEnv:
    def __enter__(self):
        self._saved = {}
        for key in [k for k in os.environ
                    if k.startswith(_PACKAGER_ENV_PREFIXES)]:
            self._saved[key] = os.environ.pop(key)
        return self

    def __exit__(self, *exc_info):
        os.environ.update(self._saved)
        return False


def _gui_command():
    if IS_FROZEN:
        return subprocess.list2cmdline([self_exe(), *sys.argv[1:]])
    script = os.path.abspath(sys.argv[0])
    return subprocess.list2cmdline(
        [sys.executable, script, *sys.argv[1:]])


def _request_admin():
    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.argtypes = [
        wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int,
    ]
    shell32.ShellExecuteW.restype = wintypes.HINSTANCE
    if IS_FROZEN:
        executable = self_exe()
        params = subprocess.list2cmdline(sys.argv[1:]) or None
    else:
        executable = sys.executable
        params = subprocess.list2cmdline(
            [os.path.abspath(sys.argv[0]), *sys.argv[1:]])
    with _CleanPackagerEnv():
        shell32.ShellExecuteW(None, "runas", executable, params, None, 1)


def _start_uiaccess():
    dll = ctypes.WinDLL(resource_path("uiaccess.dll"), use_last_error=True)
    is_uiaccess = dll.IsUIAccess
    is_uiaccess.argtypes = [wintypes.HANDLE]
    is_uiaccess.restype = wintypes.BOOL
    if is_uiaccess(wintypes.HANDLE(-1)):
        return False

    start_uiaccess = dll.StartUIAccessProcess
    start_uiaccess.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
    ]
    start_uiaccess.restype = wintypes.BOOL

    kernel32 = ctypes.windll.kernel32
    session_id = wintypes.DWORD(0)
    child_pid = wintypes.DWORD(0)
    kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(),
                                  ctypes.byref(session_id))
    with _CleanPackagerEnv():
        started = start_uiaccess(
            None, _gui_command(), 0, ctypes.byref(child_pid), session_id.value)
    return bool(started)


def prepare_gui_process():
    """准备最终 GUI 进程；返回 True 表示当前引导进程应退出。"""
    if TEST_MODE:
        return False
    try:
        if not ctypes.windll.shell32.IsUserAnAdmin():
            _request_admin()
            return True
    except Exception:
        return False
    try:
        return _start_uiaccess()
    except Exception:
        return False
