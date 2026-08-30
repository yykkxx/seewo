# -*- coding: utf-8 -*-
"""
config.py - 全局配置 (v4.4 双进程版)
"""
import os
import sys
import tempfile

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

# ---- 统一品牌名 ----
# 所有对外痕迹共用这一个名字: 窗口标题(初始)、托盘提示、互斥锁、
# 数据目录、日志/状态文件名、防火墙规则前缀。
# 注意: GUI 启动后窗口标题很快会被随机字符串替换
# (间隔见 TITLE_RANDOMIZE_SECONDS), 这里的取值只在最初几秒可见。
BRAND = "seewokiller"
APP_NAME = BRAND
VERSION = "4.4"


def _data_dir():
    """可写的数据目录: %LOCALAPPDATA%\\seewokiller, 拿不到时回退到系统临时目录。

    不能放在 exe 所在目录: UIAccess 要求 exe 位于 Program Files 等受信任
    目录, 而那些目录对标准用户通常不可写, 日志与状态文件会写入失败。
    """
    base = os.environ.get("LOCALAPPDATA") or ""
    candidates = [os.path.join(base, BRAND)] if base else []
    candidates.append(os.path.join(tempfile.gettempdir(), BRAND))
    for path in candidates:
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except OSError:
            continue
    return tempfile.gettempdir()


DATA_DIR = _data_dir()

# ---------- 日志 / 状态文件 ----------
# 统一放在 DATA_DIR (%LOCALAPPDATA%\seewokiller), 不写在程序目录:
#   1) UIAccess 要求 exe 在受信任目录, 那些目录往往不可写;
#   2) 避免把运行痕迹留在分发目录里。
GUI_LOG = os.path.join(DATA_DIR, f"{BRAND}_gui.log")
DAEMON_LOG = os.path.join(DATA_DIR, f"{BRAND}_daemon.log")
STATE_FILE = os.path.join(DATA_DIR, f".{BRAND}_state.json")

# ---------- 界面 ----------
# 窗口标题随机化间隔 (秒)。值越小越难被按标题枚举, 但会略微增加
# 标题栏闪烁; 0 或负数表示关闭随机化。
TITLE_RANDOMIZE_SECONDS = 2.0
# 维持自身窗口置顶的定时器间隔 (毫秒)。希沃大约每秒重新置顶一次,
# 这里取 500ms 才能稳定压住; 同时也是标题随机化的时间粒度。
TOP_KEEP_INTERVAL_MS = 500

# ---------- 互斥锁 / IPC / 防火墙规则前缀 ----------
GUI_MUTEX = f"Global\\{BRAND}_GUI_v4"
DAEMON_MUTEX = f"Global\\{BRAND}_Daemon_v4"
IPC_PORT_BASE = 49000   # IPC 端口基数 (实际端口 = 基数 + 会话ID%1000, 仅回环)
FIREWALL_RULE_PREFIX = f"{BRAND}_Block"   # 禁网规则的统一前缀

# ---------- 目标进程: 希沃易启学(易课堂)学生端 ----------
# 顺序有意义: 最后一项 seewo-ecr-student.exe 被视为主程序,
# "拉起希沃" 只会启动它 (见 window_ops.launch_main_target)。
# 若本机安装路径或版本号不同, 需要同步修改这里的四个路径。
TARGET_EXES = [
    # 屏幕广播: 接收教师端广播并全屏置顶显示
    r"C:\Program Files (x86)\Seewo\SeewoYiQiXueStudent\SeewoYiQiXueStudent_1.3.15.4527\resources\cppService\screen-broadcast.exe",
    # 课堂保护: 看护其它希沃进程
    r"C:\Program Files (x86)\Seewo\SeewoYiQiXueStudent\SeewoYiQiXueStudent_1.3.15.4527\resources\cppService\classroom-protect.exe",
    # 电子教室
    r"C:\Program Files (x86)\Seewo\SeewoYiQiXueStudent\SeewoYiQiXueStudent_1.3.15.4527\resources\cppService\electronic-classroom.exe",
    # 学生端主程序
    r"C:\Program Files (x86)\Seewo\SeewoYiQiXueStudent\SeewoYiQiXueStudent_1.3.15.4527\seewo-ecr-student.exe",
]

# ---------- 守护进程参数 ----------
# GUI 看护按「时间窗」计预算: 在一个时间窗内连续重拉超过上限才放弃,
# 避免 taskmgr 结束任务时「新实例抢互斥锁失败」被误计为 5 次后守护自杀。
MAX_GUI_RESTARTS = 10         # 时间窗内最多重拉 GUI 的次数
GUI_RESTART_WINDOW = 900.0    # 看护预算时间窗 (秒) = 15 分钟
DAEMON_GUI_TIMEOUT = 4.0      # GUI 心跳超时阈值 (秒); 超过即判定 GUI 已被结束
DAEMON_START_GRACE = 6.0      # 守护进程启动宽限期 (秒): 等待 GUI 首次注册
GUI_HEARTBEAT = 2.0           # 参考值: 与实际心跳间隔保持一致, 但代码中并未读取;
                              # GUI 的真实间隔由 gui_app 里 ipc_timer.start(2000) 写死
DAEMON_TICK = 0.5             # 守护进程主循环间隔 (秒)


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
