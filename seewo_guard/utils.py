# -*- coding: utf-8 -*-
"""
utils.py - 通用工具
包含: 会话ID / 控制台隐藏 / 提权 / 单实例锁 / 隐藏启动 / 窗口激活
"""
import os
import signal
import sys
import ctypes
import logging
import subprocess
import threading

import psutil

from seewo_guard.config import IS_FROZEN, self_exe
from seewo_guard.win_api import (
    kernel32, user32, GetConsoleWindow, ShowWindow, SW_HIDE, SW_RESTORE,
    ProcessIdToSessionId, CreateMutexW, ReleaseMutex, CloseHandle,
    EnumWindows, GetWindowThreadProcessId, SetForegroundWindow,
    IsUserAnAdmin, ShellExecuteW, WNDENUMPROC, wintypes,
)
from ctypes import wintypes as _wintypes


# ==========================================
# 会话 / 路径
# ==========================================
def get_session_id():
    """当前进程所在 Windows 会话 ID"""
    pid = kernel32.GetCurrentProcessId()
    sid = _wintypes.DWORD(0)
    if ProcessIdToSessionId(pid, ctypes.byref(sid)):
        return sid.value
    return 0


def get_self_path():
    """当前脚本 / exe 的绝对路径"""
    if IS_FROZEN:
        return self_exe()
    return os.path.abspath(sys.argv[0])


def hide_console():
    """隐藏控制台窗口"""
    try:
        cw = GetConsoleWindow()
        if cw:
            ShowWindow(cw, SW_HIDE)
            return True
    except Exception:
        pass
    return False


# ==========================================
# 子进程隐藏启动
# ==========================================
def hidden_startupinfo():
    si = subprocess.STARTUPINFO()
    si.dwFlags = subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def hidden_creationflags():
    return 0x08000000  # CREATE_NO_WINDOW


# ==========================================
# 打包器环境变量清理 (PyInstaller / Nuitka onefile)
# ==========================================
# 单文件打包后, 当前 Python 子进程会继承 bootloader 的内部环境变量
# (_PYI_ARCHIVE_FILE / _PYI_PARENT_PROCESS_LEVEL / _PYI_APPLICATION_HOME_DIR,
#  Nuitka onefile 为 NUITKA_* 系列)。
# 若不清理, 由本程序拉起的 exe 会被 bootloader 误判为"同一进程树的子进程",
# 从而跳过临时目录解压, 直接初始化 Python -> 必然失败
# ("Failed to start embedded Python interpreter"), 并连带临时目录无法移除。
_STRIP_ENV_PREFIXES = ("_PYI_", "NUITKA_")


def _strip_packager_env(env):
    for key in [k for k in env if k.startswith(_STRIP_ENV_PREFIXES)]:
        del env[key]
    return env


def clean_child_env():
    """返回传给子进程的环境副本: 移除打包器内部变量"""
    return _strip_packager_env(os.environ.copy())


class _CleanEnvContext:
    """临时从当前进程环境移除打包器变量 (ShellExecuteW 等无法传 env 的场景)"""

    def __enter__(self):
        self._saved = {}
        for key in [k for k in os.environ if k.startswith(_STRIP_ENV_PREFIXES)]:
            self._saved[key] = os.environ.pop(key)
        return self

    def __exit__(self, *exc_info):
        for key, value in self._saved.items():
            os.environ[key] = value
        return False


def clean_env_context():
    """上下文管理器: 临时清理当前进程的打包器环境变量, 退出时恢复"""
    return _CleanEnvContext()


def spawn_hidden(args):
    """以隐藏方式启动子进程, 返回 Popen 或 None

    必须传干净环境: 避免打包器内部变量 (_PYI_* / NUITKA_*) 被继承,
    否则子进程 bootloader 会误判父子关系导致 Python 启动失败。
    """
    try:
        return subprocess.Popen(
            args,
            env=clean_child_env(),
            startupinfo=hidden_startupinfo(),
            creationflags=hidden_creationflags(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        logging.error(f"启动子进程失败 {args}: {e}")
        return None


def daemon_cmd():
    """构造守护进程启动命令 (兼容打包/脚本)"""
    if IS_FROZEN:
        return [self_exe(), "--daemon"]
    return [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         '..', 'main.py'), "--daemon"]


def gui_cmd():
    """构造 GUI 进程启动命令 (守护进程拉起 GUI 用)"""
    if IS_FROZEN:
        return [self_exe(), "--gui"]
    return [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         '..', 'main.py'), "--gui"]


# ==========================================
# 管理员提权
# ==========================================
def is_admin():
    try:
        return bool(IsUserAnAdmin())
    except Exception:
        return False


def request_elevation():
    """请求 UAC 提权并退出当前实例"""
    script = get_self_path()
    try:
        # ShellExecuteW 无法显式传 env, 临时清掉打包器变量再启动,
        # 避免提权后的新 bootloader 误判继承关系
        with clean_env_context():
            if IS_FROZEN:
                ShellExecuteW(None, "runas", script, None, None, 1)
            else:
                ShellExecuteW(None, "runas", sys.executable, f'"{script}"', None, 1)
    except Exception as e:
        logging.error(f"提权失败: {e}")
    return


# ==========================================
# 单实例锁
# ==========================================
class SingleInstanceLock:
    """基于 Global Mutex 的单实例锁"""

    def __init__(self, mutex_name):
        self._mutex_name = mutex_name
        self._mutex = None
        self._owned = False

    def acquire(self, stale_owner_check=None):
        """获取锁; stale_owner_check() 返回 True 时允许接管过期互斥锁"""
        if self._owned:
            return True
        self._mutex = self._try_create(self._mutex_name, stale_owner_check)
        if not self._mutex:
            return False
        self._owned = True
        return True

    def _try_create(self, name, stale_owner_check=None):
        """创建互斥锁; Global 失败 (非管理员) 时回退到会话级 Local 锁"""
        for candidate in (name, name.replace("Global\\", "Local\\", 1)):
            m = CreateMutexW(None, True, candidate)
            err = ctypes.get_last_error()
            if m and m != 0:
                if err == 183:  # ERROR_ALREADY_EXISTS
                    if stale_owner_check and stale_owner_check():
                        logging.warning("检测到过期互斥锁 (原持有者已退出), 接管继续运行")
                        return m
                    CloseHandle(m)
                    return None
                return m
            if err == 5:  # ERROR_ACCESS_DENIED
                if candidate.startswith("Global"):
                    logging.info("非管理员环境, 使用会话级互斥锁 (Local\\...)")
                    continue
                logging.error("创建互斥锁失败: 拒绝访问")
                return None
            logging.error(f"CreateMutexW 失败: {ctypes.WinError(err)}")
            return None
        return None

    def release(self):
        if self._mutex and self._owned:
            try:
                ReleaseMutex(self._mutex)
            except Exception:
                pass
            try:
                CloseHandle(self._mutex)
            except Exception:
                pass
            self._mutex = None
            self._owned = False

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass


# ==========================================
# 激活已有实例窗口
# ==========================================
def activate_window_of_pid(target_pid):
    """枚举窗口并激活指定 PID 的第一个可见窗口"""
    if not target_pid:
        return False
    found = []

    def _cb(hwnd, _):
        pid = _wintypes.DWORD(0)
        GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == target_pid:
            found.append(hwnd)
            return False  # 停止枚举
        return True

    try:
        EnumWindows(WNDENUMPROC(_cb), 0)
    except Exception:
        return False
    if not found:
        return False
    try:
        ShowWindow(found[0], SW_RESTORE)
        SetForegroundWindow(found[0])
        return True
    except Exception:
        return False


def pid_alive(pid):
    try:
        return pid > 0 and psutil.pid_exists(pid)
    except Exception:
        return False


def kill_pid(pid):
    """强制结束指定 PID (os.kill: SIGTERM 后直接 SIGKILL, 无间隔)"""
    try:
        os.kill(pid, signal.SIGTERM)
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except (PermissionError, OSError):
        pass
    except Exception:
        pass





