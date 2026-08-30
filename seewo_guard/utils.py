# -*- coding: utf-8 -*-
"""
utils.py - 通用工具
包含: 会话ID / 控制台隐藏 / 提权 / 单实例锁 / 隐藏启动 / 窗口激活 /
      打包器环境变量清理

关于环境变量清理: 单文件打包 (PyInstaller / Nuitka onefile) 之后, 拉起子
进程前必须剔除 _PYI_* 与 NUITKA_* 变量。否则子进程的 bootloader 会误判
自己仍在同一个进程树里而跳过临时目录解压, 直接初始化嵌入解释器,
必然失败 ("Failed to start embedded Python interpreter")。
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
# 单文件打包后, 本进程的环境里带着 bootloader 注入的内部变量:
#   PyInstaller 6.x : _PYI_ARCHIVE_FILE / _PYI_PARENT_PROCESS_LEVEL
#                     / _PYI_APPLICATION_HOME_DIR
#   PyInstaller 5.x : _MEIPASS2
#   Nuitka onefile  : NUITKA_* 系列
# 子进程若继承这些变量, 会被判定为"同一进程树", 直接复用父进程已解压好的
# _MEI 临时目录而不再自己解压。
#
# 实测 (PyInstaller 6.20) 的两个后果:
#   1) 父进程先退出时, 其 bootloader 会去删除这个仍被子进程占用的临时目录,
#      删除失败 -> 窗口程序弹出 "Failed to remove temporary directory";
#   2) 目录被父进程清掉后, 子进程再初始化解释器会直接失败。
# 因此凡是拉起本程序自身 (常驻进程 / GUI / 提权后的新实例) 都必须清干净。
_STRIP_ENV_PREFIXES = ("_PYI_", "_MEIPASS2", "NUITKA_")


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
            creationflags=hidden_creationflags() | 0x01000000,  # CREATE_BREAKAWAY_FROM_JOB
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        logging.error(f"启动子进程失败 {args}: {e}")
        return None


def spawn_detached(args, show=0):
    """脱离本进程树启动目标进程 (孤儿化), 用于 GUI <-> 守护进程互拉。

    原理: 通过 `cmd.exe /c start` 中间进程派生目标, cmd 启动目标后立即
    退出, 目标进程随即被 Windows 重挂到系统级父进程, 不再属于调用方的
    进程树 —— taskkill /T (结束进程树) 或任务管理器「结束进程树」杀掉
    调用方时, 目标进程不会跟着死, 仍能继续把对方拉回来。

    与 ShellExecute 对比: ShellExecute 在多数环境下仍把新进程挂在调用者
    名下 (实测 parent=调用者), 无法抵抗进程树杀; cmd 中间派生则真正脱离。

    提权保留: 目标进程由 cmd (本进程的子进程) 派生, 继承的是本进程的
    令牌, 管理员/UIAccess 级别不丢失。

    show: 对 GUI 传 1 (正常显示窗口), 守护进程传 0 即可。
    """
    try:
        exe = args[0]
        # 脚本模式下 python.exe 会弹控制台, 改用 pythonw.exe (无控制台)
        if not IS_FROZEN and os.path.basename(exe).lower() in ("python.exe", "python3.exe"):
            pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.exists(pythonw):
                exe = pythonw
        inner = subprocess.list2cmdline([exe, *args[1:]])
        # cmd 立即退出, 目标进程成为孤儿; /b 不弹新窗口
        start_cmd = f'cmd.exe /c start "" /b {inner}'
        proc = subprocess.Popen(
            start_cmd,
            env=clean_child_env(),
            startupinfo=hidden_startupinfo(),
            creationflags=hidden_creationflags() | 0x01000000,  # CREATE_BREAKAWAY_FROM_JOB
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True if proc else None
    except Exception as e:
        logging.error(f"脱离式启动失败 {args}: {e}")
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
    """终止指定 PID (与 window_ops.force_kill_process 实现相同)。

    Windows 的 signal 模块没有 SIGKILL, os.kill 只有 SIGTERM 生效
    (对应 TerminateProcess); 第二行会抛 AttributeError 并被吞掉。

    注意: 当前代码中没有任何地方调用本函数, GUI 走的是
    window_ops.force_kill_process。保留它是为了与 force_kill_process
    保持一致的工具入口, 修改时请两处同步。
    """
    try:
        os.kill(pid, signal.SIGTERM)   # Windows -> TerminateProcess
        os.kill(pid, signal.SIGKILL)   # Windows 下必然抛 AttributeError
    except ProcessLookupError:
        pass
    except (PermissionError, OSError):
        pass
    except Exception:
        pass





