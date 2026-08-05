# -*- coding: utf-8 -*-
"""
gui_app.py - GUI 界面进程 (v4.0)

职责:
  - 窗口 / 托盘 / 热键 / 功能按钮 (杀进程/取消置顶/防录屏/禁网/虚拟桌面)
  - 通过本地回环 TCP 与守护进程通信: 心跳 / 状态轮询 / 完全退出
  - 被任务管理器结束不影响守护进程; 守护进程会自动重新拉起本进程
  - 关闭窗口 = 收缩到托盘, 只有「完全退出」才真正退出
"""
import ctypes
import os
import secrets
import string
import sys
import threading
import time
import logging

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QWidget, QPushButton, QVBoxLayout, QHBoxLayout,
    QComboBox, QLabel, QMessageBox, QSystemTrayIcon, QMenu,
)
from PySide6.QtGui import QIcon, QAction, QTextCursor

from seewo_guard.config import (
    APP_NAME, GUI_LOG, GUI_MUTEX, TARGET_EXES,
    TEST_MODE, TEST_AUTO_QUIT_MS, resource_path,
)
from seewo_guard.win_api import (
    user32, SetWindowPos, HWND_TOPMOST,
    SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW,
    WM_HOTKEY, WM_QUERYENDSESSION, WM_ENDSESSION,
    MOD_CONTROL, MOD_ALT,
    WDA_EXCLUDEFROMCAPTURE, WDA_MONITOR, WDA_NONE,
    SetWindowDisplayAffinity, GetWindowDisplayAffinity,
    ShowWindow, SW_HIDE,
    IsUserAnAdmin, IsUIAccess, StartUIAccessProcess,
    wintypes,
)
from ctypes import wintypes as _wintypes
from seewo_guard.logging_system import (
    setup_logging, shutdown_logging, LogBox, GUILogHandler,
)
from seewo_guard.protection import get_protection
from seewo_guard.ipc import IpcClient
from seewo_guard.window_ops import (
    find_all_windows_by_path, set_window_display_affinity_all,
    set_zbid_and_notopmost, kill_pass, block_network, allow_network,
    VirtualDesktopManager,
)
from seewo_guard.utils import (
    get_self_path, get_session_id, hide_console, is_admin,
    request_elevation, SingleInstanceLock, spawn_hidden,
    daemon_cmd, activate_window_of_pid, pid_alive,
)


# ==========================================
# 主窗口
# ==========================================
class GuardWindow(QWidget):
    def __init__(self, client, lock):
        super().__init__()
        self._client = client
        self._lock = lock
        self._daemon_down = False
        self._last_spawn_try = 0.0
        self._net_blocked = False
        self._record_blocked = False
        self._killing = False
        self._stop = threading.Event()
        self._kill_thread = None
        self._record_thread = None
        self._tray = None
        self._top_ticks = 0

        self.setWindowTitle(APP_NAME)
        self.resize(460, 470)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setAttribute(Qt.WA_QuitOnClose, False)

        self.vd = VirtualDesktopManager()

        self._build_ui()
        self._connect_signals()
        self._register_hotkeys()
        self._setup_tray()
        self._apply_self_affinity()

        # 心跳 / 状态轮询
        self.ipc_timer = QTimer(self)
        self.ipc_timer.timeout.connect(self._ipc_tick)
        self.ipc_timer.start(2000)

        # 置顶保持 (1秒, 标题随机化每10次)
        self.top_timer = QTimer(self)
        self.top_timer.timeout.connect(self.keep_on_top)
        self.top_timer.start(1000)

        # 自动启动防录屏
        QTimer.singleShot(1200, self._auto_start_record_protect)

        # 测试模式: 自动安全退出
        if TEST_AUTO_QUIT_MS > 0:
            logging.warning(f"[测试模式] {TEST_AUTO_QUIT_MS}ms 后自动退出")
            QTimer.singleShot(TEST_AUTO_QUIT_MS, self._request_quit)

        # GUI 自身保护
        try:
            get_protection().enable()
        except Exception as e:
            logging.error(f"GUI 自身保护启用失败: {e}")

        # 首次心跳
        QTimer.singleShot(300, self._send_hello)

        logging.info("🛡️ 界面进程启动完成 (守护进程独立运行)")
        logging.info("💡 关闭窗口 = 收缩到托盘 | 只有「完全退出」才退出")
        logging.info("💡 热键: Ctrl+Alt+Y=显示 | Ctrl+Alt+K=杀进程 | Ctrl+Alt+Q=退出")

    # ==========================================
    # UI
    # ==========================================
    def _build_ui(self):
        self.btn_net = QPushButton("🚫 禁止网络")
        self.btn_kill_once = QPushButton("⚔️ 杀死进程 (单次)")
        self.btn_unpin = QPushButton("📌 取消置顶")
        self.btn_kill_forever = QPushButton("🔄 持续杀进程")
        self.btn_block_record = QPushButton("🔒 防录屏")


        self.lbl_daemon = QLabel("守护进程:")
        self.lbl_daemon_status = QLabel("连接中...")
        self.lbl_daemon_status.setStyleSheet("color:orange;font-weight:bold;")

        self.lbl_desk = QLabel("桌面:")
        self.cmb_desk = QComboBox()
        self._refresh_desktops()
        self.btn_move_desk = QPushButton("移动")
        self.btn_new_desk = QPushButton("新建桌面并移动")

        self.btn_quit = QPushButton("❌ 完全退出程序")
        self.btn_quit.setStyleSheet(
            "color:white;background:#c0392b;font-weight:bold;padding:8px 20px;"
            "border-radius:4px;font-size:14px;")

        self.lbl_hint = QLabel(
            "关闭窗口=收缩到托盘 | 只有「完全退出」才退出程序\n"
            "热键: Ctrl+Alt+Y=显示 | Ctrl+Alt+K=杀进程 | Ctrl+Alt+Q=退出")
        self.lbl_hint.setStyleSheet("color:#888;font-size:11px;")

        self.log_box = LogBox()

        lay = QVBoxLayout()
        lay.setSpacing(8)

        r1 = QHBoxLayout()
        r1.addWidget(self.btn_net)
        r1.addWidget(self.btn_kill_once)
        r1.addWidget(self.btn_unpin)
        r1.addWidget(self.btn_kill_forever)
        r1.addWidget(self.btn_block_record)
        lay.addLayout(r1)

        r2 = QHBoxLayout()
        r2.addWidget(self.lbl_daemon)
        r2.addWidget(self.lbl_daemon_status, 1)
        lay.addLayout(r2)

        r3 = QHBoxLayout()
        r3.addWidget(self.lbl_desk)
        r3.addWidget(self.cmb_desk, 1)
        r3.addWidget(self.btn_move_desk)
        r3.addWidget(self.btn_new_desk)
        lay.addLayout(r3)

        lay.addWidget(self.btn_quit)
        lay.addWidget(self.lbl_hint)
        lay.addWidget(self.log_box, 1)
        self.setLayout(lay)

    def _connect_signals(self):
        self.btn_net.clicked.connect(self.on_net_toggle)
        self.btn_kill_once.clicked.connect(self.on_kill_once)
        self.btn_unpin.clicked.connect(self.on_unpin)
        self.btn_kill_forever.clicked.connect(self.on_kill_forever)
        self.btn_block_record.clicked.connect(self.on_block_record_toggle)
        self.btn_move_desk.clicked.connect(self.on_move_desktop)
        self.btn_new_desk.clicked.connect(self.on_new_desktop)
        self.btn_quit.clicked.connect(self.on_full_quit)

    # ==========================================
    # 热键
    # ==========================================
    def _register_hotkeys(self):
        try:
            from seewo_guard.win_api import RegisterHotKey, UnregisterHotKey
            self._unregister_hotkey = UnregisterHotKey
            hwnd = int(self.winId())
            ok = (RegisterHotKey(hwnd, 1, MOD_CONTROL | MOD_ALT, 0x59) and
                  RegisterHotKey(hwnd, 2, MOD_CONTROL | MOD_ALT, 0x4B) and
                  RegisterHotKey(hwnd, 3, MOD_CONTROL | MOD_ALT, 0x51))
            if ok:
                logging.info("✓ 全局热键已注册 (Ctrl+Alt+Y/K/Q)")
        except Exception as e:
            logging.error(f"热键注册异常: {e}")

    def nativeEvent(self, eventType, message):
        try:
            msg = _wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY:
                hid = msg.wParam
                if hid == 1:
                    self._show_window()
                elif hid == 2:
                    threading.Thread(target=kill_pass, daemon=True).start()
                elif hid == 3:
                    self.on_full_quit()
                return True, 0
            if msg.message == WM_QUERYENDSESSION:
                logging.warning("⚠️ 系统关机/注销 (守护进程将安全退出)")
                return True, 1
            if msg.message == WM_ENDSESSION:
                if msg.wParam:
                    logging.warning("⚠️ 系统会话结束, GUI 退出")
                    self._request_quit()
                return True, 0
        except Exception:
            pass
        r = super().nativeEvent(eventType, message)
        return r if isinstance(r, tuple) else (r, 0)

    # ==========================================
    # 托盘
    # ==========================================
    def _setup_tray(self):
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                logging.warning("系统托盘不可用, 跳过托盘图标")
                return
            icon_path = resource_path('icon.ico')
            icon = (QIcon(icon_path) if os.path.exists(icon_path)
                    else self.style().standardIcon(self.style().SP_ComputerIcon))
            self.setWindowIcon(icon)

            menu = QMenu(self)
            act_show = QAction("显示窗口", self)
            act_show.triggered.connect(self._show_window)
            act_quit = QAction("完全退出", self)
            act_quit.triggered.connect(self.on_full_quit)
            menu.addAction(act_show)
            menu.addSeparator()
            menu.addAction(act_quit)

            self._tray = QSystemTrayIcon(icon, self)
            self._tray.setToolTip(f"{APP_NAME} 守护中")
            self._tray.setContextMenu(menu)
            self._tray.activated.connect(
                lambda r: self._show_window() if r == QSystemTrayIcon.DoubleClick else None)
            self._tray.show()
            logging.info("🖥️ 系统托盘图标已创建 (双击显示窗口)")
        except Exception as e:
            self._tray = None
            logging.error(f"托盘图标创建失败: {e}")

    # ==========================================
    # 置顶 / 防录屏
    # ==========================================
    def keep_on_top(self):
        try:
            hwnd = int(self.winId())
            SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                         SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            self._top_ticks += 1
            # 每10秒随机化一次标题, 防止被按标题枚举检测
            if self._top_ticks % 10 == 0:
                title = ''.join(secrets.choice(string.ascii_letters + string.digits)
                                for _ in range(secrets.randbelow(8) + 6))
                self.setWindowTitle(title)
        except Exception:
            pass

    def _apply_self_affinity(self):
        try:
            hwnd = int(self.winId())
            if not SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
                SetWindowDisplayAffinity(hwnd, WDA_MONITOR)
                logging.info("自身窗口防录屏: 黑色兜底模式")
            else:
                logging.info("自身窗口防录屏: 透明模式")
        except Exception:
            pass

    # ==========================================
    # IPC (守护进程通信)
    # ==========================================
    def _send_hello(self):
        resp = self._client.request({"cmd": "gui_hello", "pid": os.getpid()})
        if resp:
            logging.info(f"👋 已向守护进程注册 (PID={os.getpid()})")
            self._daemon_down = False
        return resp

    def _ipc_tick(self):
        now = time.monotonic()
        # 心跳 + 状态
        resp = self._client.request({"cmd": "gui_alive", "pid": os.getpid()})
        if resp is None:
            self._set_daemon_down()
            # 节流: 每5秒尝试拉起守护进程
            if now - self._last_spawn_try > 5.0:
                self._last_spawn_try = now
                logging.warning("🔄 守护进程离线, 尝试重新拉起...")
                spawn_hidden(daemon_cmd())
            return

        if self._daemon_down:
            logging.info("✅ 守护进程已恢复连接")
            self._daemon_down = False

        status = self._client.request({"cmd": "status"})
        if status:
            self._update_daemon_status(status)

    def _set_daemon_down(self):
        if not self._daemon_down:
            self._daemon_down = True
            self.lbl_daemon_status.setText("离线, 重启中...")
            self.lbl_daemon_status.setStyleSheet("color:red;font-weight:bold;")
            if self._tray:
                self._tray.showMessage("SeewoGuard", "守护进程离线, 正在重启...",
                                       QSystemTrayIcon.Warning, 3000)

    def _update_daemon_status(self, status):
        pid = status.get("pid", 0)

        if pid_alive(pid):
            self.lbl_daemon_status.setText(f"运行中 (PID={pid})")
            self.lbl_daemon_status.setStyleSheet("color:green;font-weight:bold;")
        else:
            self.lbl_daemon_status.setText("异常")
            self.lbl_daemon_status.setStyleSheet("color:red;font-weight:bold;")

    # ==========================================
    # 功能按钮
    # ==========================================
    def on_kill_once(self):
        logging.info("⚔️ 单次杀进程")
        threading.Thread(target=self._kill_once, daemon=True).start()

    def _kill_once(self):
        for _ in range(3):
            kill_pass()
            time.sleep(0.3)

    def on_kill_forever(self):
        if not self._killing:
            logging.info("🔄 持续杀进程 ON")
            self._killing = True
            self._stop.clear()
            self._kill_thread = threading.Thread(target=self._kill_loop, daemon=True)
            self._kill_thread.start()
            self.btn_kill_forever.setText("⏹️ 停止杀进程")
            self.btn_kill_forever.setStyleSheet("color:darkred;font-weight:bold;")
        else:
            logging.info("⏹️ 持续杀进程 OFF")
            self._killing = False
            self._stop.set()
            if self._kill_thread:
                self._kill_thread.join(timeout=2)
            self.btn_kill_forever.setText("🔄 持续杀进程")
            self.btn_kill_forever.setStyleSheet("")
            main_exe = TARGET_EXES[-1]
            if os.path.exists(main_exe):
                try:
                    from seewo_guard.utils import hidden_startupinfo, hidden_creationflags
                    import subprocess
                    subprocess.Popen([main_exe], startupinfo=hidden_startupinfo(),
                                     creationflags=hidden_creationflags())
                    logging.info("已恢复应用")
                except Exception as e:
                    logging.error(f"恢复失败: {e}")

    def _kill_loop(self):
        while not self._stop.is_set():
            kill_pass()
            time.sleep(0.8)

    def on_unpin(self):
        logging.info("📌 取消置顶 (ZBID双重模式)")
        for p in TARGET_EXES:
            set_zbid_and_notopmost(p)

    def on_block_record_toggle(self):
        if not self._record_blocked:
            self._enable_record_block()
        else:
            self._disable_record_block()

    def _auto_start_record_protect(self):
        logging.info("🚀 自动启动防录屏保护...")
        self._enable_record_block()

    def _enable_record_block(self):
        logging.info("🔒 启用防录屏...")
        total = 0
        transparent = 0
        for exe in TARGET_EXES:
            for hwnd in find_all_windows_by_path(exe):
                try:
                    if SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
                        transparent += 1
                        total += 1
                    elif SetWindowDisplayAffinity(hwnd, WDA_MONITOR):
                        total += 1
                except Exception:
                    pass
        self._stop.clear()
        self._record_thread = threading.Thread(target=self._record_loop, daemon=True)
        self._record_thread.start()
        self._record_blocked = True
        self.btn_block_record.setText("🔓 停止防录屏")
        self.btn_block_record.setStyleSheet("color:darkred;font-weight:bold;")
        mode = "透明" if transparent > 0 else "黑色兜底"
        logging.warning(f"✅ 防录屏已启用 ({mode}模式, 共 {total} 个窗口)")

    def _disable_record_block(self):
        logging.info("🔓 关闭防录屏...")
        self._stop.set()
        if self._record_thread:
            self._record_thread.join(timeout=2)
        for exe in TARGET_EXES:
            set_window_display_affinity_all(exe, WDA_NONE)
        self._record_blocked = False
        self.btn_block_record.setText("🔒 防录屏")
        self.btn_block_record.setStyleSheet("")
        logging.info("✅ 防录屏已关闭")

    def _record_loop(self):
        """持续修复防录屏设置 (2秒一轮, 单次枚举)"""
        while not self._stop.is_set():
            try:
                for exe in TARGET_EXES:
                    for hwnd in find_all_windows_by_path(exe):
                        try:
                            affinity = _wintypes.DWORD(0)
                            if GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity)):
                                if affinity.value == WDA_NONE:
                                    if not SetWindowDisplayAffinity(
                                            hwnd, WDA_EXCLUDEFROMCAPTURE):
                                        SetWindowDisplayAffinity(hwnd, WDA_MONITOR)
                        except Exception:
                            pass
            except Exception:
                pass
            time.sleep(2)

    def on_net_toggle(self):
        if not self._net_blocked:
            if block_network() > 0:
                self._net_blocked = True
                self.btn_net.setText("🌐 允许网络")
                self.btn_net.setStyleSheet("color:darkred;font-weight:bold;")
        else:
            allow_network()
            self._net_blocked = False
            self.btn_net.setText("🚫 禁止网络")
            self.btn_net.setStyleSheet("")

    # ==========================================
    # 虚拟桌面
    # ==========================================
    def _refresh_desktops(self):
        self.cmb_desk.clear()
        n = self.vd.get_desktop_count() if self.vd.is_available() else 1
        for i in range(n):
            self.cmb_desk.addItem(f"桌面 {i}", i)
        self.cmb_desk.setCurrentIndex(max(0, n - 1))

    def on_move_desktop(self):
        idx = self.cmb_desk.currentData()
        hwnd = int(self.winId())
        if self.vd.move_to_desktop(hwnd, idx):
            logging.info(f"✅ 已移动到桌面 {idx}")
            self.hide()
        else:
            logging.error("❌ 移动失败")

    def on_new_desktop(self):
        idx = self.vd.create_new_desktop_and_move(int(self.winId()))
        if idx >= 0:
            self._refresh_desktops()
            logging.info(f"✅ 已新建桌面并移动 (桌面 {idx})")
            self.hide()
        else:
            logging.error("❌ 新建桌面失败")

    # ==========================================
    # 退出
    # ==========================================
    def on_full_quit(self):
        reply = QMessageBox.question(
            self, "确认完全退出",
            "完全退出将同时关闭守护进程\n"
            "(守护进程会一并安全退出)\n\n"
            "确定要完全退出吗?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            logging.info("已取消退出")
            return
        self._request_quit()

    def _request_quit(self):
        """安全退出: 通知守护进程关闭, 然后退出本进程"""
        logging.info("👋 请求完全退出...")
        self._stop.set()
        for t, name in [(self._kill_thread, "杀进程"), (self._record_thread, "防录屏")]:
            if t and t.is_alive():
                t.join(timeout=1)

        # 通知守护进程退出
        resp = self._client.request({"cmd": "shutdown", "pid": os.getpid()})
        logging.info(f"🛑 守护进程关闭指令: {'已送达' if resp else '未送达(可能已退出)'}")

        try:
            if self._tray:
                self._tray.hide()
        except Exception:
            pass
        try:
            hwnd = int(self.winId())
            self._unregister_hotkey(hwnd, 1)
            self._unregister_hotkey(hwnd, 2)
            self._unregister_hotkey(hwnd, 3)
        except Exception:
            pass

        self.top_timer.stop()
        self.ipc_timer.stop()

        if self._lock:
            self._lock.release()

        logging.info("✅ GUI 已退出")
        app = self.window().windowHandle()  # noqa
        from PySide6.QtWidgets import QApplication
        QApplication.instance().quit()

    def closeEvent(self, event):
        """点击 X / Alt+F4 / 任务栏关闭 -> 一律收缩到托盘"""
        logging.info("🚫 关闭请求已拦截, 收缩到托盘 (只有「完全退出」才退出)")
        event.ignore()
        self.hide()
        if self._tray:
            self._tray.showMessage("SeewoGuard", "程序仍在守护中, 双击托盘图标显示窗口",
                                   QSystemTrayIcon.Information, 2000)

    def _show_window(self):
        self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        self.keep_on_top()


# ==========================================
# 入口
# ==========================================
def gui_main():
    hide_console()
    setup_logging(GUI_LOG)

    logging.info("=" * 60)
    logging.info(f"  SeewoGuard GUI 进程 v{getattr(__import__('seewo_guard.config'), 'VERSION', '4.0')}")
    logging.info(f"  PID: {os.getpid()}  会话: {get_session_id()}")
    logging.info("=" * 60)

    # ---------- 管理员权限 (测试模式跳过) ----------
    if not TEST_MODE and not is_admin():
        logging.info("⚡ 请求管理员权限...")
        request_elevation()
        shutdown_logging()
        sys.exit(0)

    # ---------- UIAccess 置顶穿透 (测试模式跳过) ----------
    if not TEST_MODE and IsUIAccess is not None and not IsUIAccess(_wintypes.HANDLE(-1)):
        if StartUIAccessProcess is not None:
            script = get_self_path()
            cmd = f'"{sys.executable}" "{script}"' if script.endswith('.py') else f'"{script}"'
            logging.info("🔐 通过 UIAccess 启动新实例...")
            try:
                from seewo_guard.win_api import ProcessIdToSessionId, kernel32
                sid = _wintypes.DWORD(0)
                pid = _wintypes.DWORD(0)
                kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(),
                                              ctypes.byref(sid))
                success = StartUIAccessProcess(None, cmd, 0,
                                               ctypes.byref(pid), sid.value)
                if success:
                    logging.info(f"✅ UIAccess 子进程已启动 PID={pid.value}")
                    shutdown_logging()
                    sys.exit(0)
                logging.warning("UIAccess 启动失败, 降级运行")
            except Exception as e:
                logging.error(f"UIAccess 异常: {e}, 降级运行")

    # ---------- 单实例 ----------
    lock = SingleInstanceLock(GUI_MUTEX)
    if not lock.acquire():
        logging.info("检测到已有界面进程, 激活其窗口")
        try:
            import json
            if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           '..', '.seewo_guard_state.json')):
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       '..', '.seewo_guard_state.json'),
                          'r', encoding='utf-8') as f:
                    state = json.load(f)
                activate_window_of_pid(state.get("gui_pid", 0))
        except Exception:
            pass
        shutdown_logging()
        sys.exit(0)

    # ---------- 确保守护进程运行 ----------
    client = IpcClient()
    if not client.request({"cmd": "status"}):
        logging.info("🚀 守护进程未运行, 正在拉起...")
        spawn_hidden(daemon_cmd())
        # 等待守护进程就绪 (最多 8 秒)
        for _ in range(16):
            time.sleep(0.5)
            if client.request({"cmd": "status"}):
                break

    # ---------- GUI ----------
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    win = GuardWindow(client, lock)
    win.show()
    logging.info(f"🚀 GUI 启动完成 (PID={os.getpid()})")

    exit_code = 0
    try:
        exit_code = app.exec()
    except Exception as e:
        logging.critical(f"GUI 异常: {e}")
        exit_code = 1
    finally:
        try:
            lock.release()
        except Exception:
            pass
        shutdown_logging()
    sys.exit(exit_code)





