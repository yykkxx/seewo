# -*- coding: utf-8 -*-
"""
config.py - 全局配置 (v4.0 双进程版)
"""
import os
import sys

IS_FROZEN = getattr(sys, 'frozen', False)


def _base_dir():
    """程序根目录: 打包后为 exe 所在目录, 脚本模式为 codex/ 项目根"""
    if IS_FROZEN:
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = _base_dir()
APP_NAME = "往昔的涟漪"
VERSION = "4.0"

# ---------- 日志 / 状态文件 ----------
GUI_LOG = os.path.join(BASE_DIR, "seewo_guard_gui.log")
DAEMON_LOG = os.path.join(BASE_DIR, "seewo_guard_daemon.log")
STATE_FILE = os.path.join(BASE_DIR, ".seewo_guard_state.json")

# ---------- 互斥锁 / IPC ----------
GUI_MUTEX = "Global\\SeewoGuard_GUI_v4"
DAEMON_MUTEX = "Global\\SeewoGuard_Daemon_v4"
IPC_PORT_BASE = 49000   # IPC 端口基数 (实际端口 = 基数 + 会话ID%1000, 仅回环)

# ---------- 目标进程 ----------
TARGET_EXES = [
    r"C:\Program Files (x86)\Seewo\SeewoYiQiXueStudent\SeewoYiQiXueStudent_1.3.15.4527\resources\cppService\screen-broadcast.exe",
    r"C:\Program Files (x86)\Seewo\SeewoYiQiXueStudent\SeewoYiQiXueStudent_1.3.15.4527\resources\cppService\classroom-protect.exe",
    r"C:\Program Files (x86)\Seewo\SeewoYiQiXueStudent\SeewoYiQiXueStudent_1.3.15.4527\resources\cppService\electronic-classroom.exe",
    r"C:\Program Files (x86)\Seewo\SeewoYiQiXueStudent\SeewoYiQiXueStudent_1.3.15.4527\seewo-ecr-student.exe",
]

# ---------- 守护参数 ----------
MAX_GUI_RESTARTS = 5          # GUI 连续崩溃最大重启次数
DAEMON_GUI_TIMEOUT = 4.0      # GUI 心跳超时阈值 (秒)
DAEMON_START_GRACE = 6.0      # 守护进程启动宽限期 (秒): 等待 GUI 注册
GUI_HEARTBEAT = 2.0           # GUI 心跳间隔 (秒)
DAEMON_TICK = 0.5             # 守护主循环间隔 (秒)


def resource_path(name):
    """定位随程序分发的资源文件 (icon.ico / uiaccess.dll)

    兼容: PyInstaller(_MEIPASS) / Nuitka(解包临时目录) / 脚本模式
    """
    cands = []
    if hasattr(sys, '_MEIPASS'):
        cands.append(os.path.join(sys._MEIPASS, name))
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), name))
    cands.append(os.path.join(BASE_DIR, name))
    cands.append(os.path.join(os.path.dirname(sys.argv[0]), name))
    for p in cands:
        if os.path.exists(p):
            return p
    return cands[0]


# ---------- 测试模式 (仅供开发调试, 生产勿用) ----------
# SEEWO_GUARD_TEST=1          : 跳过提权/UIAccess 链 (测试进程即真实GUI)
# SEEWO_GUARD_AUTO_QUIT_MS=n  : GUI 启动 n 毫秒后自动安全退出
TEST_MODE = os.environ.get("SEEWO_GUARD_TEST") == "1"
TEST_AUTO_QUIT_MS = int(os.environ.get("SEEWO_GUARD_AUTO_QUIT_MS", "0") or 0)
