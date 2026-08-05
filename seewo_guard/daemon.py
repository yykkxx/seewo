# -*- coding: utf-8 -*-
"""
daemon.py - 守护进程 (v4.0 双进程架构核心)

职责:
  1. 无界面常驻后台, 独立于 GUI 进程 (任务管理器结束 GUI 不影响守护)
  2. 隐藏窗口消息循环监听系统关机/注销 (WM_QUERYENDSESSION / WM_ENDSESSION),
     收到后自动安全退出
  3. 通过本地回环 TCP IPC 响应 GUI 指令: 状态查询 / GUI 注册 / 安全退出
  4. 监控 GUI 心跳: GUI 被任务管理器结束等异常退出时自动重新拉起,
     连续崩溃超过 MAX_GUI_RESTARTS 次则自行安全退出

安全说明:
  本项目已彻底移除关键进程 (RtlSetProcessIsCritical) 代码,
  守护进程不再设置任何关键进程标记, 不存在蓝屏风险。
"""
import ctypes
import logging
import os
import sys
import threading
import time

from seewo_guard.config import (
    DAEMON_LOG, DAEMON_MUTEX, STATE_FILE,
    DAEMON_GUI_TIMEOUT, DAEMON_START_GRACE, DAEMON_TICK,
    MAX_GUI_RESTARTS, VERSION,
)
from seewo_guard.win_api import (
    user32, kernel32,
    RegisterClassW, CreateWindowExW, DefWindowProcW,
    GetMessageW, TranslateMessage, DispatchMessageW, PostQuitMessage,
    GetModuleHandleW, AllocConsole, GetConsoleWindow, ShowWindow, SW_HIDE,
    SetConsoleCtrlHandler, PHANDLER_ROUTINE,
    WM_QUERYENDSESSION, WM_ENDSESSION, WM_DESTROY,
    MSG, WNDCLASSW, WNDPROC,
    CTRL_SHUTDOWN_EVENT, CTRL_LOGOFF_EVENT, CTRL_CLOSE_EVENT,
    wintypes,
)
from ctypes import wintypes as _wintypes
from seewo_guard.logging_system import setup_logging, shutdown_logging
from seewo_guard.protection import get_protection
from seewo_guard.ipc import IpcServer
from seewo_guard.utils import (
    SingleInstanceLock, spawn_hidden, gui_cmd, get_session_id, pid_alive,
)


class DaemonApp:
    """守护进程主体"""

    def __init__(self):
        self._stop = threading.Event()
        self._shutdown_requested = threading.Event()
        self._lock = None
        self._ipc = None
        self._protection = get_protection()
        self._start_time = time.monotonic()

        # GUI 看护状态
        self._gui_pid = 0
        self._last_hb = 0.0
        self._restart_count = 0
        self._expecting_pid = None

    # ==========================================
    # 关机监听 (隐藏窗口 + 消息循环)
    # ==========================================
    def _start_shutdown_listener(self):
        """启动关机监听: 优先隐藏窗口消息循环, 失败则回退控制台处理器"""
        self._win_cb = None  # 保持 WNDPROC 引用防止 GC

        def _on_shutdown():
            logging.warning("🚨 系统关机/注销信号: 守护进程安全退出")
            self._shutdown_requested.set()
            self._stop.set()

        try:
            self._win_cb = WNDPROC(self._wndproc)
            wc = WNDCLASSW()
            wc.style = 0
            wc.lpfnWndProc = self._win_cb
            wc.cbClsExtra = 0
            wc.cbWndExtra = 0
            wc.hInstance = GetModuleHandleW(None)
            wc.hIcon = None
            wc.hCursor = None
            wc.hbrBackground = None
            wc.lpszMenuName = None
            wc.lpszClassName = f"SeewoGuardDaemon_{os.getpid()}"

            atom = RegisterClassW(ctypes.byref(wc))
            if not atom:
                raise ctypes.WinError(ctypes.get_last_error())

            hwnd = CreateWindowExW(0, wc.lpszClassName, "sgd", 0,
                                   0, 0, 0, 0, None, None, wc.hInstance, None)
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._hwnd = hwnd

            t = threading.Thread(target=self._msg_loop, daemon=True)
            t.start()
            logging.info("🪟 关机监听窗口已创建 (WM_QUERYENDSESSION/WM_ENDSESSION)")
            return
        except Exception as e:
            logging.warning(f"隐藏窗口创建失败, 回退控制台处理器: {e}")

        # 兜底: 隐藏控制台 + SetConsoleCtrlHandler
        try:
            AllocConsole()
            cw = GetConsoleWindow()
            if cw:
                ShowWindow(cw, SW_HIDE)
            self._ctrl_cb = PHANDLER_ROUTINE(
                lambda ctrl: self._console_ctrl(ctrl, _on_shutdown))
            SetConsoleCtrlHandler(self._ctrl_cb, True)
            logging.info("🎛️ 控制台关机处理器已安装 (兜底方案)")
        except Exception as e:
            logging.error(f"控制台处理器安装失败: {e}")

    def _console_ctrl(self, ctrl_type, on_shutdown):
        if ctrl_type in (CTRL_SHUTDOWN_EVENT, CTRL_LOGOFF_EVENT,
                         CTRL_CLOSE_EVENT):
            on_shutdown()
            return True
        return False

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_QUERYENDSESSION:
            logging.warning("🚨 WM_QUERYENDSESSION: 守护进程准备退出")
            self._shutdown_requested.set()
            self._stop.set()
            return 1  # 同意结束会话
        if msg == WM_ENDSESSION:
            logging.warning("🚨 WM_ENDSESSION: 守护进程退出")
            self._shutdown_requested.set()
            self._stop.set()
            PostQuitMessage(0)
            return 0
        if msg == WM_DESTROY:
            PostQuitMessage(0)
            return 0
        return DefWindowProcW(hwnd, msg, wparam, lparam)

    def _msg_loop(self):
        msg = MSG()
        while GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            TranslateMessage(ctypes.byref(msg))
            DispatchMessageW(ctypes.byref(msg))

    # ==========================================
    # IPC 请求处理
    # ==========================================
    def _handle_request(self, req):
        cmd = req.get("cmd")

        if cmd == "status":
            return {
                "ok": True,
                "role": "daemon",
                "pid": os.getpid(),
                "version": VERSION,
                "gui_pid": self._gui_pid,
                "restart_count": self._restart_count,
                "uptime": round(time.monotonic() - self._start_time, 1),
            }

        if cmd == "gui_hello":
            pid = int(req.get("pid", 0) or 0)
            self._gui_pid = pid
            self._last_hb = time.monotonic()
            self._restart_count = 0
            self._expecting_pid = None
            logging.info(f"👋 GUI 已连接 (PID={pid})")
            self._write_state()
            return {"ok": True, "gui_pid": pid}

        if cmd == "gui_alive":
            pid = int(req.get("pid", 0) or 0)
            if pid:
                self._gui_pid = pid
                self._last_hb = time.monotonic()
            return {"ok": True}

        if cmd == "shutdown":
            logging.warning("🛑 收到 GUI 完全退出指令, 守护进程退出")
            self._stop.set()
            return {"ok": True}

        return {"ok": False, "error": f"unknown_cmd:{cmd}"}

    # ==========================================
    # GUI 看护
    # ==========================================
    def _stale_owner_dead(self):
        """检查状态文件记录的旧守护进程 PID 是否已死 (死则视为过期锁)"""
        try:
            import json
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                pid = int(data.get("daemon_pid", 0) or 0)
                if pid > 0 and pid != os.getpid():
                    return not pid_alive(pid)
        except Exception:
            pass
        return False

    def _watchdog_tick(self):
        """GUI 心跳检查: 超时则重启 GUI"""
        now = time.monotonic()
        if not self._gui_pid:
            return
        # 启动宽限期: 守护刚启动时 GUI 可能尚未就绪
        if now - self._start_time < DAEMON_START_GRACE:
            return
        if now - self._last_hb <= DAEMON_GUI_TIMEOUT:
            return

        self._restart_count += 1
        self._gui_pid = 0
        if self._restart_count > MAX_GUI_RESTARTS:
            logging.critical(f"💀 GUI 连续异常 {MAX_GUI_RESTARTS} 次, "
                             "守护进程安全退出")
            self._stop.set()
            return

        logging.warning(f"⚠️ GUI 心跳丢失 ({self._restart_count}/{MAX_GUI_RESTARTS}), "
                        "重新拉起 GUI...")
        proc = spawn_hidden(gui_cmd())
        if proc:
            self._expecting_pid = proc.pid
            self._last_hb = now  # 防止连续快速误判
        else:
            self._restart_count += 1

    def _write_state(self):
        try:
            import json
            data = {
                "role": "daemon",
                "daemon_pid": os.getpid(),
                "gui_pid": self._gui_pid,
                "restart_count": self._restart_count,
                "timestamp": int(time.time()),
                "version": VERSION,
            }
            tmp = STATE_FILE + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            logging.error(f"状态文件写入失败: {e}")

    def _clear_state(self):
        try:
            if os.path.exists(STATE_FILE):
                os.remove(STATE_FILE)
        except OSError:
            pass

    # ==========================================
    # 主流程
    # ==========================================
    def run(self):
        logging.info("=" * 60)
        logging.info(f"  SeewoGuard 守护进程 v{VERSION} (双进程架构)")
        logging.info(f"  PID: {os.getpid()}  会话: {get_session_id()}")
        logging.info("=" * 60)

        # 单实例 (持有者已死时自动接管过期互斥锁)
        self._lock = SingleInstanceLock(DAEMON_MUTEX)
        if not self._lock.acquire(stale_owner_check=self._stale_owner_dead):
            logging.info("已有守护进程在运行, 本实例退出")
            return

        # 关机监听
        self._start_shutdown_listener()

        # 进程保护 (优先级/特权/缓解/PPL)
        self._protection.enable()

        self._write_state()

        # IPC 服务
        self._ipc = IpcServer(self._handle_request)
        self._ipc.start()

        logging.info("🚀 守护进程就绪, 开始监控 GUI 心跳")

        # 主循环
        try:
            while not self._stop.is_set():
                time.sleep(DAEMON_TICK)
                self._watchdog_tick()
        finally:
            self._graceful_exit()

    def _graceful_exit(self):
        logging.info("🔄 守护进程退出流程...")
        self._clear_state()
        try:
            if self._ipc:
                self._ipc.stop()
        except Exception:
            pass
        try:
            self._protection.disable()
        except Exception:
            pass
        try:
            if self._lock:
                self._lock.release()
        except Exception:
            pass
        logging.info("✅ 守护进程已安全退出")
        shutdown_logging()
        os._exit(0)


def daemon_main():
    setup_logging(DAEMON_LOG)
    try:
        DaemonApp().run()
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        logging.critical(f"💥 守护进程异常: {e}\n{traceback.format_exc()}")
        shutdown_logging()
        os._exit(1)
