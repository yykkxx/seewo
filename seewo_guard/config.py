# -*- coding: utf-8 -*-
"""
config.py - 全局配置 (v4.1 双进程版)
"""
import os
import sys
import json

IS_COMPILED = "__compiled__" in globals()          # Nuitka 编译产物
IS_FROZEN = getattr(sys, 'frozen', False) or IS_COMPILED


def self_exe():
    """当前程序可执行文件: 打包后为真实 exe, 脚本模式为 python

    Nuitka onefile 的 sys.executable 指向临时解压目录, 不可用;
    sys.argv[0] 才是真实 exe 路径 (PyInstaller 两者一致)。
    """
    if IS_FROZEN:
        if IS_COMPILED:
            return os.path.abspath(sys.argv[0])
        return sys.executable
    return sys.executable


def _base_dir():
    """程序根目录: 打包后为 exe 所在目录, 脚本模式为 codex/ 项目根"""
    if IS_FROZEN:
        return os.path.dirname(self_exe())
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = _base_dir()
APP_NAME = "往昔的涟漪"
_CONFIG_FILE = os.environ.get(
    "SEEWO_GUARD_CONFIG",
    os.path.join(BASE_DIR, "seewo_guard_config.json"),
)
_DEFAULT_VERSION = "4.1"
_DEFAULT_TARGET_EXES = [
    r"C:\Program Files (x86)\Seewo\SeewoYiQiXueStudent\SeewoYiQiXueStudent_1.3.15.4527\resources\cppService\screen-broadcast.exe",
    r"C:\Program Files (x86)\Seewo\SeewoYiQiXueStudent\SeewoYiQiXueStudent_1.3.15.4527\resources\cppService\classroom-protect.exe",
    r"C:\Program Files (x86)\Seewo\SeewoYiQiXueStudent\SeewoYiQiXueStudent_1.3.15.4527\resources\cppService\electronic-classroom.exe",
    r"C:\Program Files (x86)\Seewo\SeewoYiQiXueStudent\SeewoYiQiXueStudent_1.3.15.4527\seewo-ecr-student.exe",
]


def _load_user_config():
    if not os.path.exists(_CONFIG_FILE):
        return {}
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _str_from_env_or_cfg(env_key, cfg, cfg_key, default):
    value = os.environ.get(env_key)
    if value is not None:
        value = str(value).strip()
        return value if value else default
    value = cfg.get(cfg_key, default)
    if value is None:
        return default
    value = str(value).strip()
    return value if value else default


def _int_from_env_or_cfg(env_key, cfg, cfg_key, default):
    value = os.environ.get(env_key)
    if value is None:
        value = cfg.get(cfg_key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_from_env_or_cfg(env_key, cfg, cfg_key, default):
    value = os.environ.get(env_key)
    if value is None:
        value = cfg.get(cfg_key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _target_exes_from_env_or_cfg(cfg):
    raw = os.environ.get("SEEWO_GUARD_TARGET_EXES")
    if raw is None:
        value = cfg.get("target_exes")
    else:
        value = raw
    if value is None:
        return list(_DEFAULT_TARGET_EXES)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return list(_DEFAULT_TARGET_EXES)
        try:
            value = json.loads(text)
        except ValueError:
            value = [p.strip() for p in text.splitlines() if p.strip()]
    if isinstance(value, (list, tuple)):
        parsed = [str(p).strip() for p in value if str(p).strip()]
        if parsed:
            return parsed
    return list(_DEFAULT_TARGET_EXES)


_USER_CONFIG = _load_user_config()
VERSION = _str_from_env_or_cfg(
    "SEEWO_GUARD_VERSION", _USER_CONFIG, "version", _DEFAULT_VERSION)

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
    *_target_exes_from_env_or_cfg(_USER_CONFIG)
]

# ---------- 守护参数 ----------
MAX_GUI_RESTARTS = _int_from_env_or_cfg(
    "SEEWO_GUARD_MAX_GUI_RESTARTS", _USER_CONFIG, "max_gui_restarts", 5)
DAEMON_GUI_TIMEOUT = _float_from_env_or_cfg(
    "SEEWO_GUARD_DAEMON_GUI_TIMEOUT", _USER_CONFIG, "daemon_gui_timeout", 4.0)
DAEMON_START_GRACE = _float_from_env_or_cfg(
    "SEEWO_GUARD_DAEMON_START_GRACE", _USER_CONFIG, "daemon_start_grace", 6.0)
GUI_HEARTBEAT = _float_from_env_or_cfg(
    "SEEWO_GUARD_GUI_HEARTBEAT", _USER_CONFIG, "gui_heartbeat", 2.0)
DAEMON_TICK = _float_from_env_or_cfg(
    "SEEWO_GUARD_DAEMON_TICK", _USER_CONFIG, "daemon_tick", 0.5)
IPC_MAX_CONNECTIONS = _int_from_env_or_cfg(
    "SEEWO_GUARD_IPC_MAX_CONNECTIONS",
    _USER_CONFIG,
    "ipc_max_connections",
    32,
)
IPC_AUTH_TOKEN = _str_from_env_or_cfg(
    "SEEWO_GUARD_IPC_AUTH_TOKEN",
    _USER_CONFIG,
    "ipc_auth_token",
    "",
)


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
    # Nuitka onefile: 资源解压在 sys.executable 所在的临时目录
    cands.append(os.path.join(os.path.dirname(sys.executable), name))
    for p in cands:
        if os.path.exists(p):
            return p
    return cands[0]


# ---------- 测试模式 (仅供开发调试, 生产勿用) ----------
# SEEWO_GUARD_TEST=1          : 跳过提权/UIAccess 链 (测试进程即真实GUI)
# SEEWO_GUARD_AUTO_QUIT_MS=n  : GUI 启动 n 毫秒后自动安全退出
TEST_MODE = os.environ.get("SEEWO_GUARD_TEST") == "1"
TEST_AUTO_QUIT_MS = int(os.environ.get("SEEWO_GUARD_AUTO_QUIT_MS", "0") or 0)
