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

from seewo_guard.config import TARGET_EXES
from seewo_guard.win_api import (
    user32, kernel32,
    EnumWindows, GetWindowThreadProcessId, IsWindowVisible, WNDENUMPROC,
    SetWindowDisplayAffinity, GetWindowDisplayAffinity,
    WDA_NONE, WDA_MONITOR, WDA_EXCLUDEFROMCAPTURE,
    HAS_SETWINDOWBAND, SetWindowBand,
    wintypes,
)
from ctypes import wintypes as _wintypes
from seewo_guard.utils import hidden_startupinfo, hidden_creationflags

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
    """双重取消置顶: SetWindowBand 降级 + TOPMOST 立即取消"""
    hwnds = find_all_windows_by_path(exe_path, visible=False)
    if not hwnds:
        return
    hwnd = hwnds[0]
    try:
        if HAS_SETWINDOWBAND and SetWindowBand:
            try:
                if SetWindowBand(hwnd, _wintypes.HWND(0), 1):
                    logging.info(f"ZBID降级: {os.path.basename(exe_path)}")
                    time.sleep(0.3)
            except Exception:
                pass
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0,
                            0x0002 | 0x0001)  # HWND_TOPMOST
        time.sleep(0.1)
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0,
                            0x0002 | 0x0001)  # HWND_NOTOPMOST
        logging.info(f"✅ 双重取消置顶完成: {os.path.basename(exe_path)}")
    except Exception as e:
        logging.error(f"取消置顶异常: {e}")


def force_kill_process(pid):
    """强制终止指定 PID (os.kill: SIGTERM 后直接 SIGKILL, 无间隔)"""
    try:
        os.kill(pid, signal.SIGTERM)
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except (PermissionError, OSError):
        pass
    except Exception:
        pass


def kill_pass():
    """对全部目标进程执行一轮击杀"""
    for p in TARGET_EXES:
        for pid in get_pids_by_path(p):
            force_kill_process(pid)
    clear_pid_cache()


# ==========================================
# 网络防火墙 (netsh)
# ==========================================
def _netsh(cmd, quiet=True):
    try:
        subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       timeout=10, startupinfo=hidden_startupinfo(),
                       creationflags=hidden_creationflags())
        return True
    except Exception as e:
        if not quiet:
            logging.error(f"netsh 异常: {e}")
        return False


def block_network():
    """禁止所有目标进程出站网络, 返回成功数"""
    logging.info("🚫 禁止网络...")
    success = 0
    for p in TARGET_EXES:
        fn = os.path.basename(p)
        rn = f"SeewoGuard_Block_{fn}"
        cmd = (f'netsh advfirewall firewall add rule name="{rn}" '
               f'dir=out action=block program="{p}" enable=yes')
        if _netsh(cmd):
            success += 1
    logging.warning(f"✅ 已禁止 {success} 个进程的网络")
    return success


def allow_network():
    """删除阻止规则恢复网络, 返回成功数"""
    logging.info("🌐 恢复网络...")
    removed = 0
    for p in TARGET_EXES:
        fn = os.path.basename(p)
        rn = f"SeewoGuard_Block_{fn}"
        cmd = f'netsh advfirewall firewall delete rule name="{rn}"'
        if _netsh(cmd):
            removed += 1
    logging.info(f"✅ 已恢复网络 (删除 {removed} 条规则)")
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

    def __init__(self):
        self._ole32 = None
        self._avail = False
        self._init()

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
        """
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

