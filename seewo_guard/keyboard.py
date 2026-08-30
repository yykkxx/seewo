# -*- coding: utf-8 -*-
"""
keyboard.py - 全局键盘监听 (WH_KEYBOARD_LL)

低级键盘钩子在按键被派发到任何线程之前收到通知, 因此先于普通应用层
钩子或键盘过滤器拿到按键; 钩子链中「后安装的先收到」。
- 命中本程序热键时回调返回 1, 按键不再向下传递, 其它软件收不到
- 未命中则 CallNextHookEx 交还给钩子链, 不影响正常输入
- 内核/驱动级键盘过滤运行在本层之下, 无法用钩子绕过

命中后通过 PostMessage 发送 WM_APP+0x100 通知 GUI 主窗口,
避免在钩子线程里直接操作界面 (钩子线程自带消息循环)。
"""
import ctypes
import logging
import threading

from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_APP_HOTKEY = 0x8000 + 0x100  # WM_APP + 0x100: 钩子热键通知

VK_CONTROL = 0x11
VK_MENU = 0x12
VK_Y = 0x59
VK_K = 0x4B
VK_Q = 0x51

# 热键 ID -> (Ctrl, Alt, 主键)
# ID 与 gui_app.nativeEvent 的分支一一对应, 改这里要同步改那边:
#   1 显示窗口
#   2 杀一轮进程
#   3 停止持续杀进程 + 拉起希沃 + 退出 (不只是退出, 见 on_hotkey_quit)
HOTKEYS = {
    1: (VK_CONTROL, VK_MENU, VK_Y),  # 显示窗口
    2: (VK_CONTROL, VK_MENU, VK_K),  # 杀一轮进程
    3: (VK_CONTROL, VK_MENU, VK_Q),  # 停止持续杀进程 + 拉起希沃 + 退出
}


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

# ---------- 函数原型 (防止 64 位句柄按 32 位 int 转换溢出) ----------
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = wintypes.LPARAM
user32.PostMessageW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [
    wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.CallNextHookEx.argtypes = [
    wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_long
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short


class KeyboardHook:
    """WH_KEYBOARD_LL 全局键盘钩子 (独立线程 + 消息循环)"""

    def __init__(self, target_hwnd, record_keys=False):
        self._target_hwnd = int(target_hwnd)
        self._record_keys = record_keys
        self._hook = None
        self._thread = None
        self._proc = None
        self._running = False

    def start(self):
        if self._running:
            return True
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="keyboard-hook")
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        tid = self._thread.ident if self._thread else None
        if self._hook:
            try:
                user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                pass
            self._hook = None
        if tid:
            try:
                user32.PostThreadMessageW(tid, 0x0012, 0, 0)  # WM_QUIT
            except Exception:
                pass

    def _run(self):
        try:
            self._proc = HOOKPROC(self._callback)
            self._hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._proc,
                kernel32.GetModuleHandleW(None), 0)
            if not self._hook:
                logging.error(f"LL 键盘钩子安装失败: "
                              f"err={ctypes.get_last_error()}")
                self._running = False
                return
            logging.info("✓ 全局键盘钩子已安装 (WH_KEYBOARD_LL, 可绕过普通钩子)")
            msg = wintypes.MSG()
            while self._running:
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            logging.error(f"键盘钩子线程异常: {e}")
        finally:
            self._running = False
            if self._hook:
                try:
                    user32.UnhookWindowsHookEx(self._hook)
                except Exception:
                    pass
                self._hook = None

    def _callback(self, nCode, wParam, lParam):
        if nCode >= 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
            try:
                kb = KBDLLHOOKSTRUCT.from_address(lParam)
                vk = kb.vkCode
                if self._record_keys:
                    logging.debug(f"按键: vk={vk:#x} sc={kb.scanCode:#x}")
                for hid, (ctrl, alt, key) in HOTKEYS.items():
                    # 0x8000 位为 1 表示该键当前处于按下状态
                    ctrl_down = user32.GetAsyncKeyState(ctrl) & 0x8000
                    alt_down = user32.GetAsyncKeyState(alt) & 0x8000
                    if vk == key and ctrl_down and alt_down:
                        user32.PostMessageW(self._target_hwnd,
                                            WM_APP_HOTKEY, hid, 0)
                        return 1  # 吞掉按键, 其他软件收不到
            except Exception:
                pass
        return user32.CallNextHookEx(self._hook, nCode, wParam, lParam)
