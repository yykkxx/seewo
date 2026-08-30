# -*- coding: utf-8 -*-
"""
gui_app.py - GUI 界面进程 (v4.1)

职责:
  - 操作界面与系统托盘, 提供功能按钮:
      杀进程(单次 / 持续) | 拉起希沃 | 取消置顶 | 修改大小 | 最小化置底 |
      防录屏 | 禁止网络 | 虚拟桌面(移入现有桌面, 或新建桌面后移入)
  - 全局热键 Ctrl+Alt+Y / K / Q; 优先用 WH_KEYBOARD_LL 钩子,
    注册失败时回退 RegisterHotKey
  - 通过本地回环 TCP 与守护进程通信: 心跳、状态轮询、
    同步「最小化置底」开关、通知守护进程退出
  - 被任务管理器结束不影响守护进程; 守护进程会重新拉起本进程
  - 关闭窗口 = 收缩到托盘, 只有「完全退出」或 Ctrl+Alt+Q 才真正退出

启动后自动执行的动作:
  - 1.2 秒后自动开启防录屏
  - 每秒维持自身窗口置顶, 并每 10 秒把窗口标题换成随机字符串
  - 自身窗口设置 WDA_EXCLUDEFROMCAPTURE, 不出现在截图 / 录屏结果中
  - 虚拟桌面 COM 探测与进程加固放到首帧之后, 避免拖慢界面出现
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
    QWidget, QPushButton, QVBoxLayout, QHBoxLayout, QGridLayout,
    QComboBox, QLabel, QMessageBox, QStyle, QSystemTrayIcon, QMenu,
)
from PySide6.QtGui import QIcon, QAction, QTextCursor

from seewo_guard.config import (
    APP_NAME, GUI_LOG, GUI_MUTEX, TARGET_EXES,
    STATE_FILE, TEST_AUTO_QUIT_MS, resource_path,
    VERSION, DATA_DIR, TITLE_RANDOMIZE_SECONDS, TOP_KEEP_INTERVAL_MS,
)
from seewo_guard.win_api import (
    user32, SetWindowPos, HWND_TOPMOST,
    SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW,
    WM_HOTKEY, WM_QUERYENDSESSION, WM_ENDSESSION,
    MOD_CONTROL, MOD_ALT,
    WDA_EXCLUDEFROMCAPTURE, WDA_MONITOR, WDA_NONE,
    SetWindowDisplayAffinity, GetWindowDisplayAffinity,
    ShowWindow, SW_HIDE,
    wintypes,
)
from ctypes import wintypes as _wintypes
from seewo_guard.logging_system import (
    setup_logging, shutdown_logging, attach_log_box, LogBox,
)
from seewo_guard.protection import get_protection
from seewo_guard.ipc import IpcClient
from seewo_guard.window_ops import (
    find_all_windows_by_path, set_window_display_affinity_all,
    set_zbid_and_notopmost, kill_pass, block_network, allow_network,
    cleanup_firewall_rules,
    launch_main_target, compact_target_windows, maximize_target_windows,
    minimize_target_windows_to_bottom,
    suspend_target_threads, resume_target_threads, force_kill_process,
    VirtualDesktopManager,
)
from seewo_guard.utils import (
    get_session_id, hide_console, SingleInstanceLock, spawn_hidden,
    spawn_detached, daemon_cmd, activate_window_of_pid, pid_alive,
    is_admin,
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
        self._boot_monotonic = time.monotonic()  # 用于守护进程启动宽限期判断
        self._net_blocked = False
        self._record_blocked = False
        self._killing = False
        self._kill_stop = threading.Event()
        self._record_stop = threading.Event()
        self._kill_thread = None
        self._record_thread = None
        self._target_compact = False
        self._target_minimized = False
        self._target_bottom_pending = False
        self._tray = None
        self._top_ticks = 0
        self._unregister_hotkey = None
        self._daemon_pid = 0
        self._threads_suspended = False
        self._last_title_at = time.monotonic()

        self.setWindowTitle(APP_NAME)
        # v4.2: 窗口整体缩小约 10% (640x520 -> 576x468)
        self.resize(576, 468)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setAttribute(Qt.WA_QuitOnClose, False)

        self.vd = VirtualDesktopManager(lazy=True)  # COM 探测延迟到窗口显示后

        self._build_ui()
        self._connect_signals()
        self._register_hotkeys()
        self._setup_tray()

        # ---------- 分阶段启动 ----------
        # 首帧只做 UI; 其余按「越慢越靠后」错开, 让窗口尽快可见可点:
        #   120ms 向守护进程报到 (先报到, 免得被看护误判为已死)
        #   300ms 自身加固 + 自身窗口防捕获
        #   500ms 回放最近的日志
        #   900ms 虚拟桌面 COM 探测 (最慢, 用到才需要)
        #  1200ms 自动开启防录屏
        QTimer.singleShot(120, self._send_hello)
        QTimer.singleShot(300, self._enable_self_protect)
        QTimer.singleShot(500, self._load_recent_logs)
        QTimer.singleShot(900, self._init_virtual_desktop)
        QTimer.singleShot(1200, self._auto_start_record_protect)

        # 心跳 / 状态轮询
        self.ipc_timer = QTimer(self)
        self.ipc_timer.timeout.connect(self._ipc_tick)
        self.ipc_timer.start(2000)

        # 置顶保持; 标题随机化按时间判定, 不再按 tick 计数
        self.top_timer = QTimer(self)
        self.top_timer.timeout.connect(self.keep_on_top)
        self.top_timer.start(TOP_KEEP_INTERVAL_MS)

        self.target_bottom_timer = QTimer(self)
        self.target_bottom_timer.timeout.connect(self._keep_target_minimized_bottom)
        self.target_bottom_timer.setInterval(250)

        # 测试模式: 自动安全退出
        if TEST_AUTO_QUIT_MS > 0:
            logging.warning(f"[测试模式] {TEST_AUTO_QUIT_MS}ms 后自动退出")
            QTimer.singleShot(TEST_AUTO_QUIT_MS, self._request_quit)

        logging.info("🛡️ 界面进程启动完成 (守护进程独立运行)")
        logging.info(f"📁 日志与状态目录: {DATA_DIR}")
        logging.info("💡 关闭窗口 = 收缩到托盘 | 只有「完全退出」才退出")
        logging.info("💡 热键: Ctrl+Alt+Y=显示 | Ctrl+Alt+K=杀进程 | "
                     "Ctrl+Alt+Q=恢复希沃并退出")

    # ==========================================
    # 延迟初始化 (窗口显示后执行, 不阻塞首帧)
    # ==========================================
    def _enable_self_protect(self):
        """自身加固 (优先级/特权/缓解/PPL) + 自身窗口防捕获。

        与虚拟桌面 COM 探测拆成两步, 避免一次性塞太多重活拖慢首屏。
        """
        try:
            get_protection().enable()
        except Exception as e:
            logging.error(f"GUI 自身加固启用失败: {e}")
        self._apply_self_affinity()

    def _init_virtual_desktop(self):
        """虚拟桌面 COM 探测 + 桌面列表刷新。

        这是启动阶段最慢的一步 (要按 Windows 版本逐个试探 IID 与 vtable
        布局), 且只有点「移动 / 新建桌面」时才用得到, 所以放得最靠后。
        """
        try:
            self.vd.ensure_ready()
            self._refresh_desktops()
        except Exception as e:
            logging.debug(f"虚拟桌面延迟初始化失败: {e}")

    def _load_recent_logs(self):
        self.log_box.load_recent(GUI_LOG)

    # ==========================================
    # UI
    # ==========================================
    def _build_ui(self):
        self.btn_net = QPushButton("🚫 禁止网络")
        self.btn_kill_once = QPushButton("⚔️ 杀死进程 (单次)")
        self.btn_unpin = QPushButton("📌 取消置顶")
        self.btn_kill_forever = QPushButton("🔄 持续杀进程")
        self.btn_launch_target = QPushButton("▶️ 拉起希沃")
        self.btn_resize_target = QPushButton("🪟 修改大小")
        self.btn_minimize_bottom = QPushButton("⬇️ 最小化置底")
        self.btn_block_record = QPushButton("🔒 防录屏")
        self.btn_suspend = QPushButton("⏸️ 挂起线程")

        self.lbl_daemon = QLabel("常驻进程:")
        self.lbl_daemon_status = QLabel("连接中...")
        self.lbl_daemon_status.setStyleSheet("color:orange;font-weight:bold;")

        self.lbl_desk = QLabel("🖥️ 桌面:")
        self.cmb_desk = QComboBox()
        self.btn_move_desk = QPushButton("📤 移动")
        self.btn_new_desk = QPushButton("🆕 新建桌面并移动")

        self.btn_quit = QPushButton("❌ 完全退出程序")
        self.btn_quit.setStyleSheet(
            "color:white;background:#c0392b;font-weight:bold;padding:8px 20px;"
            "border-radius:4px;font-size:14px;")

        self.lbl_hint = QLabel(
            "关闭窗口=收缩到托盘 | 只有「完全退出」才退出程序\n"
            "热键: Ctrl+Alt+Y=显示 | Ctrl+Alt+K=杀进程 | "
            "Ctrl+Alt+Q=恢复希沃并退出")
        self.lbl_hint.setStyleSheet("color:#888;font-size:11px;")

        self.log_box = LogBox()
        attach_log_box(self.log_box)          # 实时日志进框

        lay = QVBoxLayout()
        lay.setSpacing(8)

        # 3x3 排列: 窗口缩小后 4 列会挤, 三列更整齐
        actions = QGridLayout()
        actions.setHorizontalSpacing(8)
        actions.setVerticalSpacing(8)
        actions.addWidget(self.btn_net, 0, 0)
        actions.addWidget(self.btn_kill_once, 0, 1)
        actions.addWidget(self.btn_kill_forever, 0, 2)
        actions.addWidget(self.btn_launch_target, 1, 0)
        actions.addWidget(self.btn_unpin, 1, 1)
        actions.addWidget(self.btn_block_record, 1, 2)
        actions.addWidget(self.btn_resize_target, 2, 0)
        actions.addWidget(self.btn_minimize_bottom, 2, 1)
        actions.addWidget(self.btn_suspend, 2, 2)
        lay.addLayout(actions)

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
        self.btn_launch_target.clicked.connect(self.on_launch_target)
        self.btn_resize_target.clicked.connect(self.on_resize_target)
        self.btn_minimize_bottom.clicked.connect(self.on_minimize_bottom)
        self.btn_block_record.clicked.connect(self.on_block_record_toggle)
        self.btn_move_desk.clicked.connect(self.on_move_desktop)
        self.btn_new_desk.clicked.connect(self.on_new_desktop)
        self.btn_suspend.clicked.connect(self.on_suspend_toggle)
        self.btn_quit.clicked.connect(self.on_full_quit)

    # ==========================================
    # 热键
    # ==========================================
    def _register_hotkeys(self):
        self._kb_hook = None
        self._WM_APP_HOTKEY = 0
        # 优先 LL 键盘钩子 (绕过普通应用层钩子/键盘过滤器)
        try:
            from seewo_guard.keyboard import KeyboardHook, WM_APP_HOTKEY
            hwnd = int(self.winId())
            hook = KeyboardHook(hwnd)
            if hook.start():
                self._kb_hook = hook
                self._WM_APP_HOTKEY = WM_APP_HOTKEY
                logging.info("✓ 全局热键已注册 (LL钩子: Ctrl+Alt+Y/K/Q)")
                return
            logging.warning("LL 钩子未就绪, 回退 RegisterHotKey")
        except Exception as e:
            logging.error(f"LL 钩子注册异常: {e}, 回退 RegisterHotKey")
        # 兜底: 传统 RegisterHotKey
        try:
            from seewo_guard.win_api import RegisterHotKey, UnregisterHotKey
            self._unregister_hotkey = UnregisterHotKey
            hwnd = int(self.winId())
            ok = (RegisterHotKey(hwnd, 1, MOD_CONTROL | MOD_ALT, 0x59) and
                  RegisterHotKey(hwnd, 2, MOD_CONTROL | MOD_ALT, 0x4B) and
                  RegisterHotKey(hwnd, 3, MOD_CONTROL | MOD_ALT, 0x51))
            if ok:
                logging.info("✓ 全局热键已注册 (RegisterHotKey: Ctrl+Alt+Y/K/Q)")
        except Exception as e:
            logging.error(f"热键注册异常: {e}")

    def nativeEvent(self, eventType, message):
        try:
            msg = _wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY or (self._WM_APP_HOTKEY
                                           and msg.message == self._WM_APP_HOTKEY):
                hid = msg.wParam
                if hid == 1:
                    self._show_window()
                elif hid == 2:
                    threading.Thread(target=kill_pass, daemon=True).start()
                elif hid == 3:
                    self.on_hotkey_quit()
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
                    else self.style().standardIcon(QStyle.SP_ComputerIcon))
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
        if not self.isVisible():
            return  # 窗口已收缩到托盘: 不再置顶/显示 (避免白屏残留)
        try:
            hwnd = int(self.winId())
            SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                         SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            self._top_ticks += 1

            # 标题随机化: 按真实时间判定, 与定时器间隔解耦。
            # 间隔见 config.TITLE_RANDOMIZE_SECONDS (v4.1 是 10 秒,
            # 太慢容易被按标题枚举, v4.2 收紧到 2 秒)。
            if TITLE_RANDOMIZE_SECONDS > 0:
                now = time.monotonic()
                if now - self._last_title_at >= TITLE_RANDOMIZE_SECONDS:
                    self._last_title_at = now
                    self._randomize_title()
        except Exception:
            pass

    def _randomize_title(self):
        """把窗口标题换成随机字符串, 防止被按标题枚举检测。"""
        try:
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
            self._sync_target_bottom_from_daemon(
                bool(resp.get("target_bottom", False)))
        return resp

    def _ipc_tick(self):
        now = time.monotonic()
        # 心跳 + 状态
        resp = self._client.request({"cmd": "gui_alive", "pid": os.getpid()})
        if resp is None:
            # 守护进程被拉起后需要数秒编译外载荷才就绪, 启动宽限期内
            # (前 20 秒) 只提示等待, 不算故障
            in_grace = (now - self._boot_monotonic) < 20.0
            self._set_daemon_down(quiet=in_grace)
            # 节流: 每 10 秒尝试拉起守护进程
            if now - self._last_spawn_try > 10.0:
                self._last_spawn_try = now
                if in_grace:
                    logging.info("⏳ 守护进程尚未就绪, 等待其完成启动...")
                else:
                    logging.warning("🔄 常驻进程离线, 尝试重新拉起...")
                    spawn_detached(daemon_cmd(), show=0)
            return

        if self._daemon_down:
            logging.info("✅ 守护进程已恢复连接")
            self._daemon_down = False

        status = self._client.request({"cmd": "status"})
        if status:
            if self._target_bottom_pending:
                synced = self._client.request({
                    "cmd": "set_target_bottom",
                    "enabled": self._target_minimized,
                })
                if synced and synced.get("ok"):
                    self._target_bottom_pending = False
                    status["target_bottom"] = self._target_minimized
            self._update_daemon_status(status)

    def _set_daemon_down(self, quiet=False):
        if not self._daemon_down:
            self._daemon_down = True
            if quiet:
                return  # 启动宽限期内不打扰用户 (托盘气泡/红色状态留到真离线)
            self.lbl_daemon_status.setText("离线, 重启中...")
            self.lbl_daemon_status.setStyleSheet("color:red;font-weight:bold;")
            if self._tray:
                self._tray.showMessage(APP_NAME, "常驻进程离线, 正在重启...",
                                       QSystemTrayIcon.Warning, 3000)

    def _update_daemon_status(self, status):
        pid = status.get("pid", 0)
        self._sync_target_bottom_from_daemon(
            bool(status.get("target_bottom", False)))

        # 记住常驻进程 PID, 退出时要等它先走 (见 _wait_daemon_exit)
        self._daemon_pid = pid if pid_alive(pid) else 0

        if pid_alive(pid):
            self.lbl_daemon_status.setText(f"运行中 (PID={pid})")
            self.lbl_daemon_status.setStyleSheet("color:green;font-weight:bold;")
        else:
            self.lbl_daemon_status.setText("异常")
            self.lbl_daemon_status.setStyleSheet("color:red;font-weight:bold;")

    def _wait_daemon_exit(self, timeout=3.0):
        """等常驻进程真正退出后再结束本进程。

        单文件打包 (PyInstaller onefile) 时, 本进程退出后 bootloader 会
        删除自己的 _MEI 临时目录。如果这时由本进程拉起的子进程还活着、
        并且仍占用同一临时目录, 删除就会失败, 于是弹出
        "Failed to remove temporary directory: ...\\_MEIxxxxxx"。

        所以「完全退出」必须保证: 子进程先走, 本进程后走。
        """
        pid = self._daemon_pid
        if not pid:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                logging.info("✅ 常驻进程已退出")
                self._daemon_pid = 0
                return
            time.sleep(0.05)
        logging.warning(f"⚠️ 常驻进程 {pid} 未在 {timeout}s 内退出, 强制结束")
        force_kill_process(pid)
        # TerminateProcess 是异步的: 发出请求后进程不会立刻消失, 这里再确认
        # 一小段时间, 否则本进程可能仍然先于它退出, 临时目录照样清不掉。
        grace_deadline = time.monotonic() + 1.0
        while time.monotonic() < grace_deadline:
            if not pid_alive(pid):
                logging.info("✅ 常驻进程已强制结束")
                break
            time.sleep(0.05)
        else:
            logging.error(f"❌ 常驻进程 {pid} 强制结束后依然存在, "
                          "若打包版仍提示临时目录无法删除, 请手动结束该进程")
        self._daemon_pid = 0

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
            self._kill_stop.clear()
            self._kill_thread = threading.Thread(target=self._kill_loop, daemon=True)
            self._kill_thread.start()
            self.btn_kill_forever.setText("⏹️ 停止杀进程")
            self.btn_kill_forever.setStyleSheet("color:darkred;font-weight:bold;")
        else:
            self._stop_killing(launch_target=True)

    def _stop_killing(self, launch_target=False):
        if self._killing:
            logging.info("⏹️ 持续杀进程 OFF")
            self._killing = False
            self._kill_stop.set()
            if self._kill_thread and self._kill_thread.is_alive():
                self._kill_thread.join(timeout=1.2)
        self.btn_kill_forever.setText("🔄 持续杀进程")
        self.btn_kill_forever.setStyleSheet("")
        if launch_target:
            launch_main_target()

    def on_launch_target(self):
        """停止持续击杀后拉起主希沃进程。"""
        self._stop_killing(launch_target=True)

    def on_resize_target(self):
        if self._target_minimized:
            self._set_target_bottom_mode(False, maximize=False)
        if not self._target_compact:
            if compact_target_windows() > 0:
                self._target_compact = True
                self.btn_resize_target.setText("🪟 最大化")
        else:
            if maximize_target_windows() > 0:
                self._target_compact = False
                self.btn_resize_target.setText("🪟 修改大小")

    def on_minimize_bottom(self):
        self._set_target_bottom_mode(
            not self._target_minimized, maximize=self._target_minimized)

    def _keep_target_minimized_bottom(self):
        if self._target_minimized:
            minimize_target_windows_to_bottom(log_result=False)

    def _apply_target_bottom_local(self, enabled, maximize=False):
        if enabled:
            self._target_minimized = True
            self.btn_minimize_bottom.setText("⬆️ 最大化置顶")
            minimize_target_windows_to_bottom()
            if not self.target_bottom_timer.isActive():
                self.target_bottom_timer.start()
            return
        was_enabled = self._target_minimized
        self.target_bottom_timer.stop()
        self._target_minimized = False
        self.btn_minimize_bottom.setText("⬇️ 最小化置底")
        if maximize and was_enabled:
            maximize_target_windows(force_topmost=True)

    def _set_target_bottom_mode(self, enabled, maximize=False):
        self._apply_target_bottom_local(enabled, maximize=maximize)
        resp = self._client.request({
            "cmd": "set_target_bottom",
            "enabled": enabled,
        })
        self._target_bottom_pending = not (resp and resp.get("ok"))

    def _sync_target_bottom_from_daemon(self, enabled):
        if self._target_bottom_pending or enabled == self._target_minimized:
            return
        self._apply_target_bottom_local(enabled, maximize=not enabled)

    def _kill_loop(self):
        while not self._kill_stop.is_set():
            kill_pass(self._kill_stop)
            if self._kill_stop.wait(0.8):
                break

    def on_unpin(self):
        logging.info("📌 取消置顶 (ZBID双重模式)")
        for p in TARGET_EXES:
            set_zbid_and_notopmost(p)

    # ==========================================
    # 线程挂起 / 恢复
    # ==========================================
    def on_suspend_toggle(self):
        if self._threads_suspended:
            self._resume_threads()
        else:
            self._suspend_threads()

    def _report_flags(self):
        """把运行时开关状态上报给守护进程 (异常退出时由其恢复现场)"""
        try:
            self._client.request({
                "cmd": "set_flags",
                "threads_suspended": self._threads_suspended,
                "net_blocked": self._net_blocked,
            })
        except Exception:
            pass

    def _suspend_threads(self):
        """挂起希沃全部线程: 进程还在但不执行, 比持续杀进程省资源。"""
        if not is_admin():
            logging.error("❌ 挂起线程需要管理员权限, 请以管理员身份运行")
            if self._tray:
                self._tray.showMessage(
                    APP_NAME, "挂起线程需要管理员权限",
                    QSystemTrayIcon.Warning, 3000)
            return
        logging.info("⏸️ 正在挂起目标线程...")
        n = suspend_target_threads()
        if n == 0:
            logging.warning("⚠️ 没有挂起任何线程: 目标进程可能未运行, "
                            "或权限不足 (需要管理员)")
            return
        self._threads_suspended = True
        self.btn_suspend.setText("▶️ 恢复线程")
        self.btn_suspend.setStyleSheet("color:darkred;font-weight:bold;")
        self._report_flags()
        logging.info(f"✅ 已挂起 {n} 个线程 (希沃已冻结)")

    def _resume_threads(self):
        logging.info("▶️ 正在恢复目标线程...")
        n = resume_target_threads()
        self._threads_suspended = False
        self.btn_suspend.setText("⏸️ 挂起线程")
        self.btn_suspend.setStyleSheet("")
        self._report_flags()
        logging.info(f"✅ 已恢复 {n} 个线程")

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
        self._record_stop.clear()
        self._record_thread = threading.Thread(target=self._record_loop, daemon=True)
        self._record_thread.start()
        self._record_blocked = True
        self.btn_block_record.setText("🔓 停止防录屏")
        self.btn_block_record.setStyleSheet("color:darkred;font-weight:bold;")
        mode = "透明" if transparent > 0 else "黑色兜底"
        if total == 0:
            # GUI 启动 1.2s 后首次执行时希沃窗口可能尚未出现, 属正常现象
            logging.info(f"✅ 防录屏已启用 ({mode}模式), 暂未发现希沃窗口, "
                         f"后台将每 2 秒自动复查")
        elif transparent == 0:
            logging.warning(f"✅ 防录屏已启用 (黑色兜底模式, 共 {total} 个窗口; "
                            f"透明模式不可用)")
        else:
            logging.info(f"✅ 防录屏已启用 (透明模式, 共 {total} 个窗口)")

    def _disable_record_block(self):
        logging.info("🔓 关闭防录屏...")
        self._record_stop.set()
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
        while not self._record_stop.is_set():
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
            if self._record_stop.wait(2):
                break

    def on_net_toggle(self):
        if not self._net_blocked:
            if block_network() > 0:
                self._net_blocked = True
                self.btn_net.setText("🌐 允许网络")
                self.btn_net.setStyleSheet("color:darkred;font-weight:bold;")
                self._report_flags()
        else:
            allow_network()
            self._net_blocked = False
            self.btn_net.setText("🚫 禁止网络")
            self.btn_net.setStyleSheet("")
            self._report_flags()

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
        """新建桌面, 把目标程序窗口移过去, 主视角切回当前桌面"""
        hwnds = []
        for exe in TARGET_EXES:
            hwnds.extend(find_all_windows_by_path(exe))
        if not hwnds:
            QMessageBox.information(self, "提示", "未找到目标程序窗口, 无法移动")
            return
        idx, moved = self.vd.create_new_desktop_and_move(hwnds)
        if idx >= 0:
            self._refresh_desktops()
            self.cmb_desk.setCurrentIndex(idx)
            if self._tray:
                self._tray.showMessage(
                    APP_NAME, f"已把 {moved} 个窗口移入新桌面 {idx}, 主视角已切回",
                    QSystemTrayIcon.Information, 2500)
            logging.info(f"✅ 已新建桌面 {idx}, 移动 {moved} 个窗口, 主视角已切回")
        else:
            logging.error("❌ 新建桌面失败")

    # ==========================================
    # 退出
    # ==========================================
    def on_full_quit(self):
        """完全退出 (不弹确认框, 直接安全退出)"""
        self._request_quit()

    def on_hotkey_quit(self):
        """Ctrl+Alt+Q: 停止杀进程，拉起希沃，再退出。"""
        logging.info("Ctrl+Alt+Q: 停止杀进程并拉起希沃后退出")
        self._stop_killing(launch_target=True)
        self._request_quit()

    def _request_quit(self):
        """安全退出: 恢复现场, 通知常驻进程关闭并等它先退出, 再结束本进程"""
        # 调试辅助: 记录退出触发来源, 便于排查非预期的自动退出
        try:
            import traceback
            logging.debug("退出调用栈:\n" + "".join(traceback.format_stack()[-6:]))
        except Exception:
            pass
        logging.info("👋 请求完全退出...")
        self._quitting = True  # 完全退出中: closeEvent 不再拦截
        self._kill_stop.set()
        self._record_stop.set()
        for t, name in [(self._kill_thread, "杀进程"), (self._record_thread, "防录屏")]:
            if t and t.is_alive():
                t.join(timeout=1)

        if self._target_minimized:
            self._set_target_bottom_mode(False, maximize=True)

        # 恢复现场: 若线程被挂起先恢复, 再拉起希沃, 最后清防火墙规则
        if self._threads_suspended:
            logging.info("🛠️ 退出前恢复被挂起的希沃线程...")
            self._resume_threads()
            self._threads_suspended = False
        try:
            launch_main_target()
        except Exception as e:
            logging.error(f"退出时拉起希沃失败: {e}")
        try:
            # 无论当前开关状态, 退出时无条件清理可能残留的防火墙规则 (幂等)
            cleanup_firewall_rules()
        except Exception as e:
            logging.error(f"退出时清理防火墙规则失败: {e}")
        self._net_blocked = False

        # 通知常驻进程退出
        resp = self._client.request({"cmd": "shutdown", "pid": os.getpid()})
        logging.info(f"🛑 常驻进程关闭指令: {'已送达' if resp else '未送达(可能已退出)'}")

        # 必须等常驻进程真的退出: 否则本进程的 bootloader 清理 _MEI 临时
        # 目录时会失败并弹出 "Failed to remove temporary directory"
        self._wait_daemon_exit()

        try:
            if self._tray:
                self._tray.hide()
        except Exception:
            pass
        try:
            if self._kb_hook:
                self._kb_hook.stop()
            hwnd = int(self.winId())
            if self._unregister_hotkey:
                self._unregister_hotkey(hwnd, 1)
                self._unregister_hotkey(hwnd, 2)
                self._unregister_hotkey(hwnd, 3)
        except Exception:
            pass

        self.top_timer.stop()
        self.target_bottom_timer.stop()
        self.ipc_timer.stop()

        if self._lock:
            self._lock.release()

        logging.info("✅ GUI 已退出")
        app = self.window().windowHandle()  # noqa
        from PySide6.QtWidgets import QApplication
        QApplication.instance().quit()

    def closeEvent(self, event):
        """点击 X / Alt+F4 / 任务栏关闭 -> 收缩到托盘; 完全退出时放行"""
        if getattr(self, "_quitting", False):
            event.accept()
            return
        logging.info("🚫 关闭请求已拦截, 收缩到托盘 (只有「完全退出」才退出)")
        event.ignore()
        self.hide()
        self.top_timer.stop()  # 防止置顶定时器把窗口重新显示
        if self._tray:
            self._tray.showMessage(APP_NAME, "程序仍在守护中, 双击托盘图标显示窗口",
                                   QSystemTrayIcon.Information, 2000)

    def _show_window(self):
        self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        self.keep_on_top()
        if not self.top_timer.isActive():
            self.top_timer.start(1000)


# ==========================================
# 入口
# ==========================================
def gui_main(started_at=None):
    hide_console()
    setup_logging(GUI_LOG)
    _t0 = started_at if started_at is not None else time.monotonic()

    logging.info("=" * 60)
    logging.info(f"  {APP_NAME} GUI 进程 v{VERSION}")
    logging.info(f"  PID: {os.getpid()}  会话: {get_session_id()}")
    logging.info("=" * 60)

    # ---------- 单实例 ----------
    def _gui_stale_owner_dead():
        """旧 GUI 已被杀死且不再持有互斥锁时, 允许新实例接管。

        场景: 守护进程重拉的 GUI 恰逢旧实例正被 taskmgr 结束、互斥锁尚未
        释放; 此时若状态文件里记录的旧 gui_pid 已死, 就接管继续跑,
        而不是退出后让守护进程反复重拉、耗尽看护预算。
        """
        try:
            import json
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    pid = int(json.load(f).get("gui_pid", 0) or 0)
                if pid > 0 and pid != os.getpid():
                    return not pid_alive(pid)
        except Exception:
            pass
        return False

    lock = SingleInstanceLock(GUI_MUTEX)
    # 带重试地抢锁: 旧实例正被 taskmgr 结束任务时互斥锁可能还占着,
    # 但状态文件里的旧 gui_pid 已死 (stale) -> 允许接管;
    # 重试窗口内若新实例已登记状态则放弃, 避免两个 GUI 并存
    _lock_deadline = time.monotonic() + 4.0
    while time.monotonic() < _lock_deadline:
        if lock.acquire(stale_owner_check=_gui_stale_owner_dead):
            break
        time.sleep(0.4)
    else:
        logging.info("检测到已有界面进程, 激活其窗口")
        try:
            import json
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                activate_window_of_pid(state.get("gui_pid", 0))
        except Exception:
            pass
        shutdown_logging()
        sys.exit(0)

    # ---------- 确保守护进程运行 (后台执行, 不阻塞窗口显示) ----------
    client = IpcClient()

    def _ensure_daemon_async():
        try:
            if client.request({"cmd": "status"}):
                return
            logging.info("🚀 守护进程未运行, 正在拉起...")
            # 脱离式拉起: 守护进程挂在 explorer 下, 结束本进程树不会带走它
            spawn_detached(daemon_cmd(), show=0)
            # 后台等待就绪 (最多 3 秒), 未就绪由心跳轮询兜底
            for _ in range(6):
                time.sleep(0.5)
                if client.request({"cmd": "status"}):
                    return
        except Exception:
            pass

    threading.Thread(target=_ensure_daemon_async, daemon=True).start()

    # ---------- GUI ----------
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    win = GuardWindow(client, lock)
    win.show()
    logging.info(f"🚀 GUI 启动完成 (PID={os.getpid()}, "
                 f"启动耗时 {time.monotonic() - _t0:.2f}s)")

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
