# -*- coding: utf-8 -*-
"""
window_ops.py - 窗口 / 进程 / 网络 / 虚拟桌面操作 (GUI 使用)
性能优化:
  - psutil 进程列表按路径缓存 (2秒TTL), 避免反复全量扫描
  - EnumWindows 每轮只枚举一次, 按 PID 直接分组匹配, 不再为每个窗口打开进程句柄
"""
import ctypes
import os
import signal
import time
import logging
import subprocess
import threading

import psutil

from seewo_guard.config import TARGET_EXES, FIREWALL_RULE_PREFIX
from seewo_guard.win_api import (
    user32, kernel32,
    EnumWindows, GetWindowThreadProcessId, IsWindowVisible, IsIconic,
    WNDENUMPROC,
    SetWindowDisplayAffinity, GetWindowDisplayAffinity,
    WDA_NONE, WDA_MONITOR, WDA_EXCLUDEFROMCAPTURE,
    HAS_SETWINDOWBAND, SetWindowBand,
    SetWindowPos, ShowWindowAsync, SetForegroundWindow,
    HWND_TOPMOST, HWND_NOTOPMOST, HWND_BOTTOM,
    SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE,
    SWP_SHOWWINDOW, SWP_ASYNCWINDOWPOS,
    SW_MINIMIZE, SW_RESTORE, SW_MAXIMIZE,
    MonitorFromWindow, GetMonitorInfoW, MONITORINFO,
    MONITOR_DEFAULTTONEAREST,
    TH32CS_SNAPTHREAD, THREAD_SUSPEND_RESUME, INVALID_HANDLE_VALUE,
    THREADENTRY32, CreateToolhelp32Snapshot, Thread32First, Thread32Next,
    OpenThread, SuspendThread, ResumeThread, CloseHandle,
    wintypes,
)
from ctypes import wintypes as _wintypes
from seewo_guard.utils import (
    hidden_startupinfo, hidden_creationflags, spawn_hidden, clean_child_env,
)


COMPACT_WINDOW_WIDTH = 360
COMPACT_WINDOW_HEIGHT = 220
COMPACT_WINDOW_MARGIN = 8

# ==========================================
# PID 缓存 (按 exe 路径 -> [pids])
# ==========================================
_pid_cache = {}
_pid_cache_lock = threading.Lock()
_PID_CACHE_TTL = 2.0


def get_pids_by_path(path):
    """获取指定 exe 路径的所有 PID (2秒缓存)"""
    key = path.lower()
    now = time.monotonic()
    with _pid_cache_lock:
        hit = _pid_cache.get(key)
        if hit and now - hit[0] < _PID_CACHE_TTL:
            return hit[1]
    pids = []
    try:
        for p in psutil.process_iter(['pid', 'exe']):
            try:
                exe = p.info['exe']
                if exe and exe.lower() == key:
                    pids.append(p.info['pid'])
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess):
                continue
    except Exception:
        pass
    with _pid_cache_lock:
        _pid_cache[key] = (now, pids)
    return pids


def clear_pid_cache():
    with _pid_cache_lock:
        _pid_cache.clear()


# ==========================================
# 窗口查找 (单次枚举 + PID 分组)
# ==========================================
def _enumerate_windows_grouped(visible_only=True):
    """枚举全部窗口一次, 返回 {pid: [hwnd, ...]}"""
    groups = {}

    def _cb(hwnd, _):
        try:
            if visible_only and not IsWindowVisible(hwnd):
                return True
            pid = _wintypes.DWORD(0)
            GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            groups.setdefault(pid.value, []).append(hwnd)
        except Exception:
            pass
        return True

    try:
        EnumWindows(WNDENUMPROC(_cb), 0)
    except Exception:
        pass
    return groups


def find_all_windows_by_path(exe_path, visible=True):
    """根据 exe 路径查找所有匹配窗口句柄 (高效: 一次枚举 + PID 匹配)"""
    pids = set(get_pids_by_path(exe_path))
    if not pids:
        return []
    groups = _enumerate_windows_grouped(visible_only=visible)
    hwnds = []
    for pid in pids:
        hwnds.extend(groups.get(pid, []))
    return hwnds


def find_all_target_windows(visible=False):
    """返回全部希沃目标窗口，单轮枚举并按目标 PID 匹配。"""
    target_pids = set()
    for exe_path in TARGET_EXES:
        target_pids.update(get_pids_by_path(exe_path))
    if not target_pids:
        return []
    groups = _enumerate_windows_grouped(visible_only=visible)
    hwnds = []
    for pid in target_pids:
        hwnds.extend(groups.get(pid, []))
    return list(dict.fromkeys(hwnds))


def launch_main_target():
    """确保主希沃进程正在运行，返回其 PID；失败返回 0。"""
    main_exe = TARGET_EXES[-1]
    if not os.path.exists(main_exe):
        logging.error(f"拉起希沃失败，文件不存在: {main_exe}")
        return 0
    existing_pids = get_pids_by_path(main_exe)
    if existing_pids:
        maximize_target_windows(force_topmost=True)
        logging.info(f"希沃已在运行: PID={existing_pids[0]}")
        return existing_pids[0]
    proc = spawn_hidden([main_exe])
    if not proc:
        logging.error("拉起希沃失败")
        return 0
    clear_pid_cache()
    logging.info(f"已拉起希沃: PID={proc.pid}")
    return proc.pid


def compact_target_windows(width=COMPACT_WINDOW_WIDTH,
                           height=COMPACT_WINDOW_HEIGHT):
    """把全部希沃窗口恢复为小窗口并放到所在屏幕右上角。"""
    changed = 0
    for hwnd in find_all_target_windows(visible=True):
        try:
            monitor = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(info)
            if not monitor or not GetMonitorInfoW(monitor, ctypes.byref(info)):
                continue
            work = info.rcWork
            target_width = min(width, max(1, work.right - work.left))
            target_height = min(height, max(1, work.bottom - work.top))
            x = work.right - target_width - COMPACT_WINDOW_MARGIN
            y = work.top + COMPACT_WINDOW_MARGIN
            ShowWindowAsync(hwnd, SW_RESTORE)
            if SetWindowPos(
                    hwnd, 0, x, y, target_width, target_height,
                    SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW
                    | SWP_ASYNCWINDOWPOS):
                changed += 1
        except Exception as e:
            logging.debug(f"修改希沃窗口大小失败 hwnd={hwnd}: {e}")
    logging.info(f"已把 {changed} 个希沃窗口缩小到右上角")
    return changed


def maximize_target_windows(force_topmost=False):
    """最大化全部希沃窗口，可选同时置顶。"""
    changed = 0
    hwnds = find_all_target_windows(visible=True)
    for hwnd in hwnds:
        try:
            ShowWindowAsync(hwnd, SW_MAXIMIZE)
            if force_topmost:
                SetWindowPos(
                    hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
                    | SWP_ASYNCWINDOWPOS)
            changed += 1
        except Exception as e:
            logging.debug(f"最大化希沃窗口失败 hwnd={hwnd}: {e}")
    if force_topmost and hwnds:
        try:
            SetForegroundWindow(hwnds[0])
        except Exception:
            pass
    logging.info(f"已最大化 {changed} 个希沃窗口"
                 f"{'并置顶' if force_topmost else ''}")
    return changed


def minimize_target_windows_to_bottom(log_result=True):
    """最小化并置底全部希沃窗口，返回处理的窗口数。

    该操作由 GUI 的短周期定时器持续调用，用本程序的 UIAccess 权限
    压过目标程序每秒一次的置顶动作，避免注入或挂起线程留下冻结状态。
    """
    changed = 0
    for hwnd in find_all_target_windows(visible=True):
        try:
            if not IsIconic(hwnd):
                ShowWindowAsync(hwnd, SW_MINIMIZE)
            SetWindowPos(
                hwnd, HWND_BOTTOM, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                | SWP_ASYNCWINDOWPOS)
            changed += 1
        except Exception:
            pass
    if log_result:
        logging.info(f"已最小化置底 {changed} 个希沃窗口")
    return changed


def set_window_display_affinity_all(exe_path, affinity=WDA_MONITOR):
    """对指定 exe 的所有窗口设置显示亲和性, 返回成功数"""
    count = 0
    for hwnd in find_all_windows_by_path(exe_path):
        try:
            if SetWindowDisplayAffinity(hwnd, affinity):
                count += 1
        except Exception:
            pass
    return count


def set_zbid_and_notopmost(exe_path):
    """对指定 exe 的**全部**窗口取消置顶, 返回处理的窗口数。

    每个窗口都执行两步: SetWindowBand 降级 + TOPMOST 立即取消。
    「先设 TOPMOST 再设 NOTOPMOST」是有意为之: 目标窗口处于高 ZBID 时,
    直接设 NOTOPMOST 往往无效, 先顶起来再放下可以强制窗口管理器刷新
    Z 序状态。SetWindowBand 属于未公开 API, 取不到就直接跳过。
    """
    hwnds = find_all_windows_by_path(exe_path, visible=False)
    if not hwnds:
        return 0
    name = os.path.basename(exe_path)
    handled = 0
    for hwnd in hwnds:
        try:
            if HAS_SETWINDOWBAND and SetWindowBand:
                try:
                    if SetWindowBand(hwnd, _wintypes.HWND(0), 1):
                        time.sleep(0.3)
                except Exception:
                    pass
            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0,
                                0x0002 | 0x0001)  # HWND_TOPMOST
            time.sleep(0.1)
            user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0,
                                0x0002 | 0x0001)  # HWND_NOTOPMOST
            handled += 1
        except Exception as e:
            logging.debug(f"取消置顶异常 hwnd={hwnd}: {e}")
    logging.info(f"✅ 已取消置顶 {handled}/{len(hwnds)} 个窗口: {name}")
    return handled


def force_kill_process(pid):
    """终止指定 PID。

    Windows 的 signal 模块没有 SIGKILL, os.kill 只支持 SIGTERM
    (底层为 TerminateProcess, 立即结束、无法被目标拦截)。
    第二行 signal.SIGKILL 在 Windows 上会抛 AttributeError, 并被下面的
    except Exception 吞掉, 因此实际生效的只有第一行。
    目标进程重新启动由对方自身的看护机制负责, 所以杀进程要周期性执行
    (见 kill_pass 与 GUI 的「持续杀进程」)。
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


def kill_pass(stop_event=None):
    """对全部目标进程执行一轮击杀，可由事件提前中断。"""
    for p in TARGET_EXES:
        if stop_event is not None and stop_event.is_set():
            break
        for pid in get_pids_by_path(p):
            if stop_event is not None and stop_event.is_set():
                break
            force_kill_process(pid)
    clear_pid_cache()


# ==========================================
# 线程挂起 / 恢复 (冻结而不结束)
# ==========================================
def _snapshot_threads():
    """快照系统全部线程, 返回 [(线程ID, 所属进程ID), ...]; 失败返回 []。"""
    snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return []
    items = []
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        ok = Thread32First(snap, ctypes.byref(entry))
        while ok:
            items.append((entry.th32ThreadID, entry.th32OwnerProcessID))
            entry = THREADENTRY32()
            entry.dwSize = ctypes.sizeof(entry)
            ok = Thread32Next(snap, ctypes.byref(entry))
    except Exception as e:
        logging.debug(f"线程快照遍历失败: {e}")
    finally:
        CloseHandle(snap)
    return items


def target_thread_ids():
    """返回全部目标进程的线程 ID 列表。"""
    target_pids = set()
    for exe in TARGET_EXES:
        target_pids.update(get_pids_by_path(exe))
    if not target_pids:
        return []
    return [tid for tid, pid in _snapshot_threads() if pid in target_pids]


_DEBUG_PRIV_ENABLED = False


def _ensure_debug_privilege():
    """挂起/恢复前确保 SeDebugPrivilege: 没有它 OpenThread 打不开
    高完整性(管理员)目标进程的线程句柄。幂等, 每个进程只做一次。"""
    global _DEBUG_PRIV_ENABLED
    if _DEBUG_PRIV_ENABLED:
        return
    try:
        from seewo_guard.protection import get_protection
        get_protection().enable()   # 其中包含 SeDebugPrivilege 的启用
        _DEBUG_PRIV_ENABLED = True
    except Exception:
        pass


def suspend_target_threads():
    """挂起目标进程的全部线程, 返回成功挂起的线程数。

    与杀进程的区别: 进程仍在列表里但停止执行, 对方的看护按"进程是否还
    在"判断存活, 所以挂起不会被重启, 比持续杀进程更省资源。

    注意: 挂起计数是可累加的, 同一线程被挂起 n 次就要恢复 n 次,
    因此恢复时统一走 resume_target_threads 循环减到 0。
    需要管理员权限 + SeDebugPrivilege 才能打开对方线程句柄。
    """
    _ensure_debug_privilege()
    count = 0
    for tid in target_thread_ids():
        handle = OpenThread(THREAD_SUSPEND_RESUME, False, tid)
        if not handle:
            continue
        try:
            # 返回值是挂起前的计数, 0xFFFFFFFF (-1) 表示失败
            if SuspendThread(handle) != 0xFFFFFFFF:
                count += 1
        except Exception:
            pass
        finally:
            CloseHandle(handle)
    logging.info(f"⏸️ 已挂起 {count} 个目标线程")
    return count


def resume_target_threads():
    """恢复目标进程被挂起的线程, 返回已完全恢复运行的线程数。"""
    _ensure_debug_privilege()
    count = 0
    for tid in target_thread_ids():
        handle = OpenThread(THREAD_SUSPEND_RESUME, False, tid)
        if not handle:
            continue
        try:
            while True:
                prev = ResumeThread(handle)
                if prev == 0xFFFFFFFF or prev <= 0:
                    break          # 失败, 或原本就没被挂起
                if prev == 1:
                    count += 1     # 从挂起态回到运行态
                    break
                # prev > 1: 还挂着多层, 继续释放
        except Exception:
            pass
        finally:
            CloseHandle(handle)
    logging.info(f"▶️ 已恢复 {count} 个目标线程")
    return count


# ==========================================
# 网络防火墙 (netsh)
# ==========================================
def _netsh(cmd, quiet=True):
    """执行 netsh 命令, 返回 (是否成功, 输出文本)。

    检查返回码: netsh 失败 (例如非管理员、防火墙服务被禁用) 时返回 False,
    调用方据此判断命令是否真正执行成功。
    cmd.exe 用 clean_child_env() 起: 不把打包器的内部变量 (_PYI_* 等)
    带过去, 免得拉起的进程被误判成同一进程树。
    """
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=10, startupinfo=hidden_startupinfo(),
                              creationflags=hidden_creationflags(),
                              env=clean_child_env())
        return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    except Exception as e:
        if not quiet:
            logging.error(f"netsh 异常: {e}")
        return False, str(e)


def _rule_name(fn):
    return f"{FIREWALL_RULE_PREFIX}_{fn}"


def _rule_exists(fn):
    """用 show rule 确认规则真实存在且为出站阻止, 而不是只看 add 是否报错。"""
    rn = _rule_name(fn)
    ok, out = _netsh(f'netsh advfirewall firewall show rule name="{rn}" verbose')
    return ok and "Action:" in out and "Block" in out


def block_network():
    """给所有目标进程加出站阻止防火墙规则, 返回**已验证生效**的规则数。

    每条规则 add 之后再 show rule 复核: 只有确认「规则存在且 Action=Block」
    才计数, 避免 netsh 静默失败时误报成功。
    规则名为 <FIREWALL_RULE_PREFIX>_<文件名>, 恢复时按同名删除
    (见 allow_network / cleanup_firewall_rules)。规则持久保存在系统中,
    异常退出由守护进程在下次启动或放弃看护时清理。
    """
    logging.info("🚫 禁止网络...")
    verified = 0
    for p in TARGET_EXES:
        fn = os.path.basename(p)
        rn = _rule_name(fn)
        ok, out = _netsh(f'netsh advfirewall firewall add rule name="{rn}" '
                         f'dir=out action=block program="{p}" enable=yes')
        if not ok:
            logging.warning(f"⚠️ 添加规则失败: {rn}\n{out.strip()[:200]}")
            continue
        if _rule_exists(fn):
            verified += 1
        else:
            logging.warning(f"⚠️ 规则已添加但未生效(show rule 未确认): {rn}")
    logging.info(f"✅ 已禁止 {verified} 个进程的网络 (均经 show rule 验证)")
    return verified


def allow_network():
    """删除 block_network 添加的出站阻止规则, 返回确认已删除的条数"""
    logging.info("🌐 恢复网络...")
    removed = 0
    for p in TARGET_EXES:
        fn = os.path.basename(p)
        rn = _rule_name(fn)
        ok, _ = _netsh(f'netsh advfirewall firewall delete rule name="{rn}"')
        if ok and not _rule_exists(fn):
            removed += 1
    logging.info(f"✅ 已恢复网络 (确认删除 {removed} 条规则)")
    return removed


def cleanup_firewall_rules():
    """无条件清理本程序创建的全部防火墙规则 (幂等, 可重复调用)。

    用于: 程序退出、守护进程启动时清理上次异常退出残留、以及守护进程
    放弃看护 / 退出前。规则不存在时 delete 同样安全。
    """
    removed = 0
    for p in TARGET_EXES:
        fn = os.path.basename(p)
        rn = _rule_name(fn)
        ok, _ = _netsh(f'netsh advfirewall firewall delete rule name="{rn}"')
        if ok and not _rule_exists(fn):
            removed += 1
    if removed:
        logging.info(f"🧹 已清理 {removed} 条残留防火墙规则")
    return removed


# ==========================================
# 虚拟桌面管理 (纯 COM)
# ==========================================
class VirtualDesktopManager:
    """Windows 10/11 虚拟桌面 (纯 COM, 不模拟按键)

    所有能力都通过 COM 接口实现:
    - 创建/切换/删除桌面: IVirtualDesktopManagerInternal, 未注册为 COM 类,
      通过 CLSID_ImmersiveShell -> IServiceProvider.QueryService 获取;
      不同系统版本使用不同 IID 与 vtable 布局, 按 pyvda 的方式逐一探测,
      首个 QueryService 成功者即为可用布局。
    - 移动窗口: IApplicationViewCollection.GetViewForHwnd 取得窗口视图,
      再调用内部接口 MoveViewToDesktop (比公开 IVirtualDesktopManager
      MoveWindowToDesktop 更可靠, 后者常返回拒绝访问)。
    """

    _CLSID_IMMERSIVE_SHELL = "{c2f03a33-21f5-47fa-b4bb-156362a2f239}"
    _IID_SERVICE_PROVIDER = "{6d5140c1-7436-11ce-8034-00aa006009fa}"
    _CLSID_INTERNAL = "{c5e0cdca-7b6e-41b2-9fc4-d93975cc467b}"
    _IID_VIEW_COLLECTION = "{1841c6d7-4f9d-42c0-af41-8747538f10e5}"

    # (IID, 布局标签): 探测顺序同 pyvda, 先新后旧
    _INTERNAL_VARIANTS = [
        ("{53f5ca0b-158f-4124-900c-057158060b27}", "v26100"),   # Win11 24H2+
        ("{4970ba3d-fd4e-4647-bea3-d89076ef4b9c}", "v22631"),   # Win11 23H2
        ("{a3175f2d-239c-4bd2-8aa0-eeba8b0b138e}", "v22621"),   # Win11 21H2/22H2
        ("{b2f925b9-5a0f-4d2e-9f4d-2b1507593c10}", "hwnd21313"),  # Win11 内测
        ("{094afe11-44f2-4ba0-976f-29a97e263ee0}", "hwnd20231"),  # Win10 内测
        ("{f31574d6-b682-4cdc-bd56-1827860abec6}", "v9000"),    # Win10 1809-22H2
    ]

    # vtable 槽位: IUnknown(0-2) 之后的方法序号, 与 pyvda com_defns 逐项对应;
    # hwnd 变体仅 GetCurrentDesktop / SwitchDesktop / CreateDesktopW 前置 HWND 参数
    _SLOTS = {
        "v26100":    dict(get_current=6, switch=9,  create=11, remove=13, find=14, hwnd=False),
        "v22631":    dict(get_current=6, switch=9,  create=11, remove=13, find=14, hwnd=False),
        "v22621":    dict(get_current=6, switch=9,  create=10, remove=12, find=13, hwnd=False),
        "v22449":    dict(get_current=6, switch=10, create=11, remove=13, find=14, hwnd=True),
        "hwnd21313": dict(get_current=6, switch=9,  create=10, remove=12, find=13, hwnd=True),
        "hwnd20231": dict(get_current=6, switch=9,  create=10, remove=11, find=12, hwnd=True),
        "v9000":     dict(get_current=6, switch=9,  create=10, remove=11, find=12, hwnd=False),
    }

    _ZERO_GUID = "{00000000-0000-0000-0000-000000000000}"

    def __init__(self, lazy=False):
        self._ole32 = None
        self._avail = False
        self._ready = False
        if not lazy:
            self._init()
            self._ready = True

    def ensure_ready(self):
        """惰性初始化 (幂等): 首次调用时执行 COM 探测"""
        if not self._ready:
            self._ready = True
            self._init()
        return self._avail

    def _init(self):
        try:
            from ctypes import c_void_p, c_ulong, c_long, POINTER
            self._ole32 = ctypes.windll.ole32
            self._ole32.CoInitialize(None)
            CoCreate = self._ole32.CoCreateInstance
            CoCreate.argtypes = [c_void_p, c_void_p, c_ulong, c_void_p,
                                 POINTER(c_void_p)]
            CoCreate.restype = c_long
            self._avail = True
            logging.info("✓ 虚拟桌面 API 初始化成功 (纯 COM)")
        except Exception as e:
            logging.debug(f"虚拟桌面初始化失败: {e}")
            self._avail = False

    def is_available(self):
        self.ensure_ready()
        return self._avail

    # ---------- COM 基础 ----------
    @staticmethod
    def _guid(s):
        """字符串 GUID -> ctypes GUID 结构"""
        import uuid
        u = uuid.UUID(s)

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8),
            ]

        g = GUID()
        g.Data1 = u.time_low
        g.Data2 = u.time_mid
        g.Data3 = u.time_hi_version
        for i in range(8):
            g.Data4[i] = u.bytes[8 + i]
        return g

    @staticmethod
    def _guid_equal(a, b):
        try:
            return (a.Data1 == b.Data1 and a.Data2 == b.Data2
                    and a.Data3 == b.Data3
                    and bytes(a.Data4) == bytes(b.Data4))
        except Exception:
            return False

    @staticmethod
    def _vtable(ppv):
        """接口指针 -> vtable 槽位指针 (先解引用对象首字段的 vtable 指针)"""
        from ctypes import c_void_p
        obj = ctypes.cast(ppv, ctypes.POINTER(c_void_p))
        return ctypes.cast(obj[0], ctypes.POINTER(c_void_p))

    @staticmethod
    def _release(*ptrs):
        """按 vtable[2] Release 释放 COM 接口"""
        from ctypes import c_void_p, CFUNCTYPE, c_ulong
        for p in ptrs:
            try:
                if p:
                    vt = VirtualDesktopManager._vtable(p)
                    rel = ctypes.cast(vt[2], CFUNCTYPE(c_ulong, c_void_p))
                    rel(p)
            except Exception:
                pass

    def _co_create(self, clsid_str, iid_str):
        """CoCreateInstance, 返回接口指针 c_void_p 或 None"""
        from ctypes import c_void_p, byref
        self._ole32.CoInitialize(None)
        ppv = c_void_p(0)
        hr = self._ole32.CoCreateInstance(
            byref(self._guid(clsid_str)), None, 0x17,
            byref(self._guid(iid_str)), byref(ppv))
        if hr < 0 or not ppv:
            return None
        return ppv

    def _get_internal(self):
        """ImmersiveShell 服务 -> QueryService 探测 IVirtualDesktopManagerInternal

        返回 (mgr_ptr, slots) 或 None; 布局按首个 QueryService 成功的 IID 确定
        注意: 本方法是所有 COM 调用的总入口, 先确保惰性初始化完成
        """
        self.ensure_ready()
        import sys
        build = sys.getwindowsversion().build
        svc = self._co_create(self._CLSID_IMMERSIVE_SHELL,
                              self._IID_SERVICE_PROVIDER)
        if not svc:
            return None
        try:
            from ctypes import c_void_p, byref, c_long, POINTER, CFUNCTYPE
            vtable = self._vtable(svc)
            query = ctypes.cast(vtable[3], CFUNCTYPE(
                c_long, c_void_p, c_void_p, c_void_p, POINTER(c_void_p)))
            for iid_s, label in self._INTERNAL_VARIANTS:
                ppv = c_void_p(0)
                hr = query(svc, byref(self._guid(self._CLSID_INTERNAL)),
                           byref(self._guid(iid_s)), byref(ppv))
                if hr == 0 and ppv:
                    if label == "hwnd21313" and build >= 22449:
                        # 22449 起方法表插入了 GetAllCurrentDesktops
                        label = "v22449"
                    slots = self._SLOTS[label]
                    logging.info(f"✓ 虚拟桌面内部接口: {label} ({iid_s}) "
                                 f"build={build}")
                    return ppv, slots
            logging.warning("⚠️ 未找到可用的 IVirtualDesktopManagerInternal")
            return None
        finally:
            self._release(svc)

    def _fns(self, mgr, slots):
        """按布局构造 vtable 方法调用器, 返回 dict + hwnd 占位参数"""
        from ctypes import c_void_p, HRESULT, POINTER, CFUNCTYPE
        vtable = self._vtable(mgr)
        H = HRESULT
        if slots["hwnd"]:
            get_current = ctypes.cast(vtable[slots["get_current"]], CFUNCTYPE(
                H, c_void_p, c_void_p, POINTER(c_void_p)))
            switch = ctypes.cast(vtable[slots["switch"]], CFUNCTYPE(
                H, c_void_p, c_void_p, c_void_p))
            create = ctypes.cast(vtable[slots["create"]], CFUNCTYPE(
                H, c_void_p, c_void_p, POINTER(c_void_p)))
            hwnd_args = (0,)
        else:
            get_current = ctypes.cast(vtable[slots["get_current"]], CFUNCTYPE(
                H, c_void_p, POINTER(c_void_p)))
            switch = ctypes.cast(vtable[slots["switch"]], CFUNCTYPE(
                H, c_void_p, c_void_p))
            create = ctypes.cast(vtable[slots["create"]], CFUNCTYPE(
                H, c_void_p, POINTER(c_void_p)))
            hwnd_args = ()
        find = ctypes.cast(vtable[slots["find"]], CFUNCTYPE(
            H, c_void_p, c_void_p, POINTER(c_void_p)))
        remove = ctypes.cast(vtable[slots["remove"]], CFUNCTYPE(
            H, c_void_p, c_void_p, c_void_p))
        # IVirtualDesktop::GetId 在所有版本中槽位一致 (方法序号 1)
        get_id = ctypes.cast(vtable[4], CFUNCTYPE(
            H, c_void_p, c_void_p))
        return dict(get_current=get_current, switch=switch, create=create,
                    find=find, remove=remove, get_id=get_id,
                    hwnd_args=hwnd_args)

    # ---------- 桌面枚举 (注册表) ----------
    def _reg_desktop_guids(self):
        """读取注册表 VirtualDesktopIDs, 返回 GUID 结构列表"""
        try:
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\VirtualDesktops")
            v, _ = winreg.QueryValueEx(k, "VirtualDesktopIDs")
            winreg.CloseKey(k)
            if isinstance(v, bytes):
                import uuid
                return [self._guid(str(uuid.UUID(bytes_le=v[i * 16:(i + 1) * 16])))
                        for i in range(len(v) // 16)]
        except Exception:
            pass
        return []

    def get_desktop_count(self):
        return max(1, len(self._reg_desktop_guids()))

    def _get_desktop_guid(self, idx):
        gs = self._reg_desktop_guids()
        if 0 <= idx < len(gs):
            return gs[idx]
        return None

    def _get_cur_desktop_idx(self):
        """当前桌面索引: 注册表 CurrentVirtualDesktop 优先, 缺失时用 COM 查询"""
        try:
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\VirtualDesktops")
            cur, _ = winreg.QueryValueEx(k, "CurrentVirtualDesktop")
            all_g, _ = winreg.QueryValueEx(k, "VirtualDesktopIDs")
            winreg.CloseKey(k)
            for i in range(len(all_g) // 16):
                if all_g[i * 16:(i + 1) * 16] == cur:
                    return i
        except Exception:
            pass
        try:
            # 部分系统不写 CurrentVirtualDesktop: 用 COM GetCurrentDesktop + GetId
            from ctypes import c_void_p, byref
            got = self._get_internal()
            if got:
                mgr, slots = got
                try:
                    f = self._fns(mgr, slots)
                    vd = c_void_p(0)
                    if (f["get_current"](mgr, *f["hwnd_args"], byref(vd)) == 0
                            and vd):
                        g = self._guid(self._ZERO_GUID)
                        f["get_id"](vd, byref(g))
                        self._release(vd)
                        gs = self._reg_desktop_guids()
                        for i, gg in enumerate(gs):
                            if self._guid_equal(gg, g):
                                return i
                finally:
                    self._release(mgr)
        except Exception:
            pass
        return 0

    def _find_desktop_idx(self, guid, timeout=0.6):
        """在注册表 VirtualDesktopIDs 中定位 GUID 的索引 (带短重试)"""
        deadline = time.monotonic() + timeout
        while True:
            for i, g in enumerate(self._reg_desktop_guids()):
                if self._guid_equal(g, guid):
                    return i
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)

    # ---------- COM: 创建 / 切换 / 删除桌面 ----------
    def create_desktop_com(self):
        """纯 COM 创建新桌面, 视角保持在原桌面; 返回新桌面 GUID 或 None"""
        try:
            from ctypes import c_void_p, byref
            got = self._get_internal()
            if not got:
                return None
            mgr, slots = got
            f = self._fns(mgr, slots)
            cur_vd = c_void_p(0)
            if (f["get_current"](mgr, *f["hwnd_args"], byref(cur_vd)) != 0
                    or not cur_vd):
                self._release(mgr)
                return None
            cur_guid = self._guid(self._ZERO_GUID)
            f["get_id"](cur_vd, byref(cur_guid))

            new_vd = c_void_p(0)
            hr = f["create"](mgr, *f["hwnd_args"], byref(new_vd))
            if hr != 0 or not new_vd:
                self._release(mgr, cur_vd)
                return None
            new_guid = self._guid(self._ZERO_GUID)
            if f["get_id"](new_vd, byref(new_guid)) != 0:
                self._release(mgr, cur_vd, new_vd)
                return None

            # 少数系统创建后视角自动切到新桌面: 检测并切回原桌面
            now_vd = c_void_p(0)
            if (f["get_current"](mgr, *f["hwnd_args"], byref(now_vd)) == 0
                    and now_vd):
                now_guid = self._guid(self._ZERO_GUID)
                f["get_id"](now_vd, byref(now_guid))
                if not self._guid_equal(now_guid, cur_guid):
                    f["switch"](mgr, *f["hwnd_args"], cur_vd)
                self._release(now_vd)
            self._release(mgr, cur_vd, new_vd)
            logging.info("✅ COM 新建桌面成功, 视角保持在原桌面")
            return new_guid
        except Exception as e:
            logging.error(f"COM 创建桌面失败: {e}")
            return None

    def switch_desktop_com(self, idx):
        """纯 COM 切换到指定索引的桌面"""
        try:
            from ctypes import c_void_p, byref
            got = self._get_internal()
            if not got:
                return False
            mgr, slots = got
            f = self._fns(mgr, slots)
            guid = self._get_desktop_guid(idx)
            if guid is None:
                self._release(mgr)
                return False
            target = c_void_p(0)
            hr = f["find"](mgr, byref(guid), byref(target))
            if hr != 0 or not target:
                self._release(mgr)
                return False
            hr = f["switch"](mgr, *f["hwnd_args"], target)
            self._release(mgr, target)
            return hr == 0
        except Exception as e:
            logging.error(f"切换桌面失败: {e}")
            return False

    def remove_desktop_com(self, idx):
        """纯 COM 删除指定桌面 (其窗口移回当前桌面), 供测试/清理使用"""
        try:
            from ctypes import c_void_p, byref
            got = self._get_internal()
            if not got:
                return False
            mgr, slots = got
            f = self._fns(mgr, slots)
            guid = self._get_desktop_guid(idx)
            if guid is None:
                self._release(mgr)
                return False
            target = c_void_p(0)
            hr = f["find"](mgr, byref(guid), byref(target))
            if hr != 0 or not target:
                self._release(mgr)
                return False
            fallback = c_void_p(0)
            f["get_current"](mgr, *f["hwnd_args"], byref(fallback))
            if not fallback or fallback.value == target.value:
                # 不能以被删桌面自身为回退目标: 选列表中的另一个
                self._release(fallback)
                gs = self._reg_desktop_guids()
                if len(gs) <= 1:
                    self._release(mgr, target)
                    return False
                other_guid = gs[1] if idx == 0 else gs[0]
                hr = f["find"](mgr, byref(other_guid), byref(fallback))
                if hr != 0 or not fallback:
                    self._release(mgr, target)
                    return False
            hr = f["remove"](mgr, target, fallback)
            self._release(mgr, target, fallback)
            return hr == 0
        except Exception as e:
            logging.error(f"删除桌面失败: {e}")
            return False

    # ---------- 窗口移动 ----------
    def _get_view_for_hwnd(self, hwnd):
        """QueryService -> IApplicationViewCollection.GetViewForHwnd

        返回 IApplicationView 指针或 None (调用方负责 Release)
        """
        svc = self._co_create(self._CLSID_IMMERSIVE_SHELL,
                              self._IID_SERVICE_PROVIDER)
        if not svc:
            return None
        try:
            from ctypes import c_void_p, byref, c_long, POINTER, CFUNCTYPE
            vtable = self._vtable(svc)
            query = ctypes.cast(vtable[3], CFUNCTYPE(
                c_long, c_void_p, c_void_p, c_void_p, POINTER(c_void_p)))
            col = c_void_p(0)
            hr = query(svc,
                       byref(self._guid(self._IID_VIEW_COLLECTION)),
                       byref(self._guid(self._IID_VIEW_COLLECTION)),
                       byref(col))
            if hr != 0 or not col:
                return None
            try:
                vt = self._vtable(col)
                get_view = ctypes.cast(vt[6], CFUNCTYPE(
                    c_long, c_void_p, _wintypes.HWND, POINTER(c_void_p)))
                view = c_void_p(0)
                hr2 = get_view(col, hwnd, byref(view))
                if hr2 != 0 or not view:
                    return None
                return view
            finally:
                self._release(col)
        except Exception as e:
            logging.debug(f"获取窗口视图失败: {e}")
            return None
        finally:
            self._release(svc)

    def _move_window_to_guid(self, hwnd, guid):
        """移动窗口到指定 GUID 桌面

        走 IApplicationViewCollection.GetViewForHwnd + 内部接口
        MoveViewToDesktop (槽位 4, 各版本一致), 比公开接口
        IVirtualDesktopManager.MoveWindowToDesktop 更可靠。
        """
        try:
            from ctypes import c_void_p, byref, c_long, CFUNCTYPE
            got = self._get_internal()
            if not got:
                return False
            mgr, slots = got
            try:
                f = self._fns(mgr, slots)
                vd = c_void_p(0)
                hr = f["find"](mgr, byref(guid), byref(vd))
                if hr != 0 or not vd:
                    return False
                view = self._get_view_for_hwnd(hwnd)
                if not view:
                    return False
                try:
                    vt = self._vtable(mgr)
                    move_view = ctypes.cast(vt[4], CFUNCTYPE(
                        c_long, c_void_p, c_void_p, c_void_p))
                    hr = move_view(mgr, view, vd)
                    return hr == 0
                finally:
                    self._release(view)
            finally:
                self._release(mgr)
        except Exception as e:
            logging.error(f"移动窗口到桌面失败: {e}")
            return False

    def _move_window_only(self, hwnd, idx):
        """仅把窗口移动到指定桌面 (不切换视角)"""
        guid = self._get_desktop_guid(idx)
        if guid is None:
            return False
        return self._move_window_to_guid(hwnd, guid)

    def move_to_desktop(self, hwnd, idx):
        """移动窗口到指定桌面并切换视角到该桌面"""
        if not self._move_window_only(hwnd, idx):
            return False
        return self.switch_desktop_com(idx)

    def create_new_desktop_and_move(self, hwnds):
        """纯 COM 新建桌面, 把窗口移入新桌面, 主视角保持在原桌面

        返回 (新桌面索引, 成功移动的窗口数); 失败返回 (-1, 0)。
        """
        if isinstance(hwnds, (int, _wintypes.HWND)):
            hwnds = [hwnds]
        if not hwnds:
            return -1, 0
        try:
            cur = self._get_cur_desktop_idx()
            guid = self.create_desktop_com()
            if guid is None:
                return -1, 0
            new_idx = self._find_desktop_idx(guid)
            if new_idx is None:
                new_idx = max(0, self.get_desktop_count() - 1)
            moved = 0
            for hwnd in hwnds:
                if self._move_window_to_guid(hwnd, guid):
                    moved += 1
            logging.info(f"✅ 已新建桌面#{new_idx}, 移动 {moved}/{len(hwnds)} "
                         f"个窗口, 视角保持在桌面#{cur}")
            return new_idx, moved
        except Exception as e:
            logging.error(f"新建桌面失败: {e}")
        return -1, 0
