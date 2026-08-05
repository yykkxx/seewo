# -*- coding: utf-8 -*-
"""
window_ops.py - 窗口 / 进程 / 网络 / 虚拟桌面操作 (GUI 使用)
性能优化:
  - psutil 进程列表按路径缓存 (2秒TTL), 避免反复全量扫描
  - EnumWindows 每轮只枚举一次, 按 PID 直接分组匹配, 不再为每个窗口打开进程句柄
"""
import ctypes
import os
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
    """强制终止指定 PID"""
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=0.3)
        except psutil.TimeoutExpired:
            proc.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
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
# 虚拟桌面管理
# ==========================================
class VirtualDesktopManager:
    """Windows 10/11 虚拟桌面 (COM 接口)"""

    def __init__(self):
        self._avail = False
        self._ole32 = None
        self._clsid = None
        self._iid = None
        self._init()

    def _init(self):
        try:
            import uuid
            ole32 = ctypes.windll.ole32
            ole32.CoInitialize(None)

            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8),
                ]

            def s2g(s):
                u = uuid.UUID(s)
                g = GUID()
                g.Data1 = u.time_low
                g.Data2 = u.time_mid
                g.Data3 = u.time_hi_version
                for i in range(8):
                    g.Data4[i] = u.bytes[8 + i]
                return g

            self._ole32 = ole32
            self._clsid = s2g("{aa509086-5ca9-4c25-8f95-589d3c07b48a}")
            self._iid = s2g("{a5cd92ff-29be-454c-8d04-d82879fb3f1b}")
            self._avail = True
            logging.info("✓ 虚拟桌面 API 初始化成功")
        except Exception as e:
            logging.debug(f"虚拟桌面初始化失败: {e}")

    def is_available(self):
        return self._avail

    def get_desktop_count(self):
        try:
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\VirtualDesktops")
            v, _ = winreg.QueryValueEx(k, "VirtualDesktopIDs")
            winreg.CloseKey(k)
            return len(v) // 16 if isinstance(v, bytes) else 1
        except Exception:
            return 1

    def move_to_desktop(self, hwnd, idx):
        """移动窗口到指定桌面并切换视角到该桌面"""
        if not self._move_window_only(hwnd, idx):
            return False
        self._switch_desktop(idx)
        return True

    def _move_window_only(self, hwnd, idx):
        """仅把窗口移动到指定桌面 (不切换视角)"""
        if not self._avail:
            return False
        try:
            from ctypes import c_void_p, c_ulong, byref, HRESULT
            self._ole32.CoInitialize(None)
            CoCreate = self._ole32.CoCreateInstance
            CoCreate.argtypes = [c_void_p, c_void_p, c_ulong, c_void_p,
                                 ctypes.POINTER(c_void_p)]
            CoCreate.restype = HRESULT
            ppv = c_void_p(0)
            hr = CoCreate(ctypes.byref(self._clsid), None, 0x17,
                          ctypes.byref(self._iid), byref(ppv))
            if hr != 0:
                return False
            guid = self._get_desktop_guid(idx)
            if not guid:
                return False
            vtable = ctypes.cast(ppv, ctypes.POINTER(c_void_p))
            move_fn = ctypes.cast(vtable[5], ctypes.CFUNCTYPE(
                HRESULT, c_void_p, _wintypes.HWND, c_void_p))
            hr = move_fn(ppv, hwnd, ctypes.byref(guid))
            return hr == 0
        except Exception as e:
            logging.error(f"移动窗口到桌面失败: {e}")
            return False

    def _get_desktop_guid(self, idx):
        try:
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\VirtualDesktops")
            v, _ = winreg.QueryValueEx(k, "VirtualDesktopIDs")
            winreg.CloseKey(k)
            if isinstance(v, bytes) and len(v) >= (idx + 1) * 16:
                b = v[idx * 16:(idx + 1) * 16]

                class G(ctypes.Structure):
                    _fields_ = [("D1", ctypes.c_ulong), ("D2", ctypes.c_ushort),
                                ("D3", ctypes.c_ushort), ("D4", ctypes.c_ubyte * 8)]

                g = G()
                g.D1 = int.from_bytes(b[0:4], 'little')
                g.D2 = int.from_bytes(b[4:6], 'little')
                g.D3 = int.from_bytes(b[6:8], 'little')
                for i in range(8):
                    g.D4[i] = b[8 + i]
                return g
        except Exception:
            return None
        return None

    def _switch_desktop(self, idx, from_idx=None):
        try:
            VK_LWIN, VK_CTRL = 0x5B, 0x11
            VK_LEFT, VK_RIGHT = 0x25, 0x27
            cur = self._get_cur_desktop_idx() if from_idx is None else from_idx
            steps = idx - (cur or 0)
            if steps == 0:
                return
            dir_vk = VK_RIGHT if steps > 0 else VK_LEFT
            user32.keybd_event(VK_LWIN, 0, 0, 0)
            user32.keybd_event(VK_CTRL, 0, 0, 0)
            for _ in range(abs(steps)):
                user32.keybd_event(dir_vk, 0, 0, 0)
                time.sleep(0.05)
                user32.keybd_event(dir_vk, 0, 0x0002, 0)
                time.sleep(0.05)
            user32.keybd_event(VK_CTRL, 0, 0x0002, 0)
            user32.keybd_event(VK_LWIN, 0, 0x0002, 0)
        except Exception:
            pass

    def _get_cur_desktop_idx(self):
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
        return 0

    def create_new_desktop_and_move(self, hwnds):
        """新建桌面并把窗口移动到新桌面, 主视角切回原桌面

        返回 (新桌面索引, 成功移动的窗口数); 失败返回 (-1, 0)。
        """
        if isinstance(hwnds, (int, _wintypes.HWND)):
            hwnds = [hwnds]
        try:
            cur = self._get_cur_desktop_idx()
            # Win+Ctrl+D 新建桌面 (创建后视角自动切到新桌面)
            VK_LWIN, VK_CTRL, VK_D = 0x5B, 0x11, 0x44
            user32.keybd_event(VK_LWIN, 0, 0, 0)
            user32.keybd_event(VK_CTRL, 0, 0, 0)
            time.sleep(0.05)
            user32.keybd_event(VK_D, 0, 0, 0)
            time.sleep(0.05)
            user32.keybd_event(VK_D, 0, 0x0002, 0)
            user32.keybd_event(VK_CTRL, 0, 0x0002, 0)
            user32.keybd_event(VK_LWIN, 0, 0x0002, 0)
            time.sleep(0.6)  # 等待新桌面创建完成 (注册表刷新)
            new_idx = self.get_desktop_count() - 1
            moved = 0
            for hwnd in hwnds:
                if self._move_window_only(hwnd, new_idx):
                    moved += 1
            # 主视角切回原桌面
            if new_idx != cur:
                self._switch_desktop(cur, from_idx=new_idx)
            logging.info(f"✅ 已新建桌面#{new_idx}, 移动 {moved}/{len(hwnds)} "
                         f"个窗口, 视角已切回桌面#{cur}")
            return new_idx, moved
        except Exception as e:
            logging.error(f"新建桌面失败: {e}")
        return -1, 0
