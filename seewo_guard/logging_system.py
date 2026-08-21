# -*- coding: utf-8 -*-
"""
logging_system.py - 日志系统
- RotatingFileHandlerSafe: 线程安全的滚动文件日志 (5MB x 5)
- LogBox: GUI 彩色日志框 (PySide6, 线程安全信号转发)
- setup_logging / shutdown_logging
"""
import os
import re
import threading
import logging
from datetime import datetime

from seewo_guard.config import IS_FROZEN


class RotatingFileHandlerSafe(logging.Handler):
    """线程安全的滚动文件日志处理器"""

    def __init__(self, filename, max_bytes=5 * 1024 * 1024,
                 backup_count=5, encoding='utf-8'):
        super().__init__()
        self.filename = os.path.abspath(filename)
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.encoding = encoding
        self._lock = threading.Lock()
        self._ensure_dir()

    def _ensure_dir(self):
        d = os.path.dirname(self.filename)
        if d and not os.path.exists(d):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                pass

    def _roll_over(self):
        try:
            if not os.path.exists(self.filename):
                return
            if os.path.getsize(self.filename) < self.max_bytes:
                return
            for i in range(self.backup_count - 1, 0, -1):
                src = f"{self.filename}.{i}"
                dst = f"{self.filename}.{i + 1}"
                if os.path.exists(src):
                    if os.path.exists(dst):
                        os.remove(dst)
                    os.rename(src, dst)
            dst = f"{self.filename}.1"
            if os.path.exists(dst):
                os.remove(dst)
            os.rename(self.filename, dst)
        except OSError:
            pass

    def emit(self, record):
        with self._lock:
            try:
                self._roll_over()
                msg = self.format(record) + "\n"
                with open(self.filename, 'a', encoding=self.encoding) as f:
                    f.write(msg)
                    f.flush()
            except OSError:
                pass


_logger_initialized = False


def log_suppressed_exception(context, level=logging.DEBUG):
    logging.log(level, f"{context}", exc_info=True)


def setup_logging(log_file, log_box=None, level=logging.DEBUG):
    """初始化日志: 文件 + 控制台(脚本模式) + GUI(可选)"""
    global _logger_initialized
    if _logger_initialized:
        return

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        '%(asctime)s [%(levelname)-8s] [%(threadName)-12s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    try:
        fh = RotatingFileHandlerSafe(log_file)
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG)
        root.addHandler(fh)
    except Exception as e:
        print(f"[警告] 文件日志初始化失败: {e}")

    if not IS_FROZEN:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        ch.setLevel(logging.INFO)
        root.addHandler(ch)

    if log_box is not None and GUILogHandler is not None:
        gh = GUILogHandler(log_box)
        gh.setFormatter(logging.Formatter('%(message)s'))
        gh.setLevel(logging.DEBUG)
        root.addHandler(gh)

    _logger_initialized = True
    logging.info("=" * 60)
    logging.info(f"📋 日志系统初始化完成: {log_file}")
    logging.info("=" * 60)


_gui_handler_ref = None


def attach_log_box(log_box):
    """把 GUI 日志框挂到 root logger (幂等, 可重复调用)"""
    global _gui_handler_ref
    if GUILogHandler is None or log_box is None:
        return
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, GUILogHandler):
            root.removeHandler(h)
    gh = GUILogHandler(log_box)
    gh.setFormatter(logging.Formatter('%(message)s'))
    gh.setLevel(logging.DEBUG)
    root.addHandler(gh)
    _gui_handler_ref = gh  # 持有引用, 防止被 GC


def shutdown_logging():
    global _logger_initialized
    root = logging.getLogger()
    for h in list(root.handlers):
        try:
            h.flush()
            h.close()
        except Exception:
            pass
    root.handlers.clear()
    _logger_initialized = False


# ==========================================
# GUI 彩色日志框 (PySide6)
# ==========================================
try:
    from PySide6.QtCore import Signal, QObject
    from PySide6.QtWidgets import QTextEdit
    from PySide6.QtGui import QTextCursor
    HAS_QT = True
except ImportError:
    HAS_QT = False


if HAS_QT:
    class LogSignal(QObject):
        log_signal = Signal(str, str)

    class LogBox(QTextEdit):
        """彩色 GUI 日志框 (限 5000 行)"""
        MAX_BLOCKS = 5000

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setReadOnly(True)
            self.setLineWrapMode(QTextEdit.NoWrap)
            self.document().setMaximumBlockCount(self.MAX_BLOCKS)
            self._sig = LogSignal()
            self._sig.log_signal.connect(self._append_log)
            self.setStyleSheet("""
                QTextEdit {
                    background-color: #1e1e1e; color: #d4d4d4;
                    font-family: 'Consolas', 'Courier New', monospace;
                    font-size: 12px; border: 1px solid #3e3e3e;
                    border-radius: 4px; padding: 4px;
                }
            """)

        def _append_log(self, level, msg):
            colors = {
                "DEBUG": "#888888", "INFO": "#d4d4d4",
                "WARNING": "#ffcc00", "ERROR": "#ff6666",
                "CRITICAL": "#ff4444",
            }
            icons = {
                "DEBUG": "🔍", "INFO": "ℹ️", "WARNING": "⚠️",
                "ERROR": "❌", "CRITICAL": "🚨",
            }
            color = colors.get(level, "#d4d4d4")
            icon = icons.get(level, "•")
            try:
                now = datetime.now().strftime("%H:%M:%S")
                escaped = (msg.replace("&", "&amp;")
                              .replace("<", "&lt;")
                              .replace(">", "&gt;"))
                html = (f'<span style="color:#666;">[{now}]</span> '
                        f'<span style="color:{color};">{icon} {escaped}</span>')
                c = self.textCursor()
                c.movePosition(QTextCursor.End)
                c.insertHtml(html + "<br>")
                self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
            except Exception:
                pass

        def append_log(self, level, msg):
            self._sig.log_signal.emit(level, msg)

        _LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

        def load_recent(self, log_file, max_lines=300):
            """把日志文件末尾若干行显示到日志框 (启动即可见历史)"""
            try:
                with open(log_file, 'rb') as f:
                    f.seek(0, 2)
                    position = f.tell()
                    chunks = []
                    newline_count = 0
                    while position > 0 and newline_count <= max_lines:
                        size = min(8192, position)
                        position -= size
                        f.seek(position)
                        chunk = f.read(size)
                        chunks.append(chunk)
                        newline_count += chunk.count(b'\n')
                    data = b''.join(reversed(chunks))
                    lines = data.decode('utf-8', errors='replace').splitlines()
                    lines = lines[-max_lines:]
            except OSError:
                return
            for line in lines:
                line = line.rstrip('\r\n')
                if not line.strip():
                    continue
                level = "INFO"
                msg = line
                m = re.match(
                    r'^\S+ \S+ \[(\w+)\s*\] \[[^\]]*\] (.*)$', line)
                if m:
                    if m.group(1) in self._LEVELS:
                        level = m.group(1)
                    msg = m.group(2)
                try:
                    self._append_log(level, msg)
                except Exception:
                    pass

    class GUILogHandler(logging.Handler):
        """将 logging 记录转发到 LogBox"""

        def __init__(self, log_box):
            super().__init__()
            self.log_box = log_box

        def emit(self, record):
            try:
                self.log_box.append_log(record.levelname, self.format(record))
            except Exception:
                pass

else:
    LogBox = None
    GUILogHandler = None
