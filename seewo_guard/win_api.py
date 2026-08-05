# -*- coding: utf-8 -*-
"""
win_api.py - Windows API 声明 (ctypes)

集中管理所有底层绑定: 窗口/热键/令牌/关机消息
注意: 已彻底移除 RtlSetProcessIsCritical (关键进程) 相关代码,
      本项目永不设置关键进程标记, 不存在蓝屏风险。
"""
import ctypes
import logging
from ctypes import wintypes

# ==========================================
# DLL 加载
# ==========================================
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
ntdll = ctypes.windll.ntdll
advapi32 = ctypes.windll.advapi32

# ==========================================
# 窗口置顶常量
# ==========================================
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040

SetWindowPos = user32.SetWindowPos
SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_uint,
]
SetWindowPos.restype = wintypes.BOOL

# ==========================================
# ShowWindow / 控制台
# ==========================================
ShowWindow = user32.ShowWindow
ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
ShowWindow.restype = wintypes.BOOL
SW_HIDE = 0
SW_SHOW = 5
SW_RESTORE = 9
SW_MINIMIZE = 6

GetConsoleWindow = kernel32.GetConsoleWindow
GetConsoleWindow.restype = wintypes.HWND

AllocConsole = kernel32.AllocConsole
AllocConsole.restype = wintypes.BOOL

# ==========================================
# 热键
# ==========================================
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
WM_HOTKEY = 0x0312

RegisterHotKey = user32.RegisterHotKey
RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
RegisterHotKey.restype = wintypes.BOOL

UnregisterHotKey = user32.UnregisterHotKey
UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
UnregisterHotKey.restype = wintypes.BOOL

# ==========================================
# 互斥锁 / 句柄 / 会话
# ==========================================
CreateMutexW = kernel32.CreateMutexW
CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
CreateMutexW.restype = wintypes.HANDLE

ReleaseMutex = kernel32.ReleaseMutex
ReleaseMutex.argtypes = [wintypes.HANDLE]
ReleaseMutex.restype = wintypes.BOOL

CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [wintypes.HANDLE]
CloseHandle.restype = wintypes.BOOL

GetCurrentProcess = kernel32.GetCurrentProcess
GetCurrentProcess.restype = wintypes.HANDLE

GetCurrentProcessId = kernel32.GetCurrentProcessId
GetCurrentProcessId.restype = wintypes.DWORD

ProcessIdToSessionId = kernel32.ProcessIdToSessionId
ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
ProcessIdToSessionId.restype = wintypes.BOOL

# ==========================================
# 进程优先级
# ==========================================
SetPriorityClass = kernel32.SetPriorityClass
SetPriorityClass.argtypes = [wintypes.HANDLE, ctypes.c_uint]
SetPriorityClass.restype = wintypes.BOOL

GetPriorityClass = kernel32.GetPriorityClass
GetPriorityClass.argtypes = [wintypes.HANDLE]
GetPriorityClass.restype = ctypes.c_uint

ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
HIGH_PRIORITY_CLASS = 0x00000080
REALTIME_PRIORITY_CLASS = 0x00000100

# ==========================================
# 窗口显示亲和性 (防录屏)
# ==========================================
WDA_NONE = 0x00000000
WDA_MONITOR = 0x00000001
WDA_EXCLUDEFROMCAPTURE = 0x00000011

SetWindowDisplayAffinity = user32.SetWindowDisplayAffinity
SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
SetWindowDisplayAffinity.restype = wintypes.BOOL

GetWindowDisplayAffinity = user32.GetWindowDisplayAffinity
GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
GetWindowDisplayAffinity.restype = wintypes.BOOL

# ==========================================
# PPL (Protected Process Light)
# ==========================================
ProcessProtectionInformation = 61
PsProtectedTypeNone = 0
PsProtectedTypeProtectedLight = 1
PsProtectedTypeProtected = 2
PsProtectedSignerNone = 0
PsProtectedSignerAuthenticode = 1
PsProtectedSignerCodeGen = 2
PsProtectedSignerAntimalware = 3
PsProtectedSignerLsa = 4
PsProtectedSignerWindows = 5
PsProtectedSignerWinTcb = 6
PsProtectedSignerMax = 7

NtSetInformationProcess = ntdll.NtSetInformationProcess
NtSetInformationProcess.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
]
NtSetInformationProcess.restype = wintypes.LONG

# ==========================================
# 进程缓解策略
# ==========================================
SetProcessMitigationPolicy = kernel32.SetProcessMitigationPolicy
SetProcessMitigationPolicy.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
SetProcessMitigationPolicy.restype = wintypes.BOOL

ProcessDEPPolicy = 0
ProcessASLRPolicy = 1
ProcessDynamicCodePolicy = 2
ProcessStrictHandleCheckPolicy = 3
ProcessSystemCallDisablePolicy = 4
ProcessExtensionPointDisablePolicy = 6
ProcessControlFlowGuardPolicy = 7
ProcessSignaturePolicy = 8
ProcessFontDisablePolicy = 9
ProcessImageLoadPolicy = 10
ProcessChildProcessPolicy = 11
ProcessSideChannelIsolationPolicy = 13

# ==========================================
# 令牌特权
# ==========================================
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x00000002


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [
        ("PrivilegeCount", wintypes.DWORD),
        ("Privileges", LUID_AND_ATTRIBUTES * 1),
    ]


OpenProcessToken = advapi32.OpenProcessToken
OpenProcessToken.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
]
OpenProcessToken.restype = wintypes.BOOL

LookupPrivilegeValueW = advapi32.LookupPrivilegeValueW
LookupPrivilegeValueW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID),
]
LookupPrivilegeValueW.restype = wintypes.BOOL

AdjustTokenPrivileges = advapi32.AdjustTokenPrivileges
AdjustTokenPrivileges.argtypes = [
    wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
    wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
]
AdjustTokenPrivileges.restype = wintypes.BOOL

# ==========================================
# 枚举窗口 / 进程
# ==========================================
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

EnumWindows = user32.EnumWindows
EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
EnumWindows.restype = wintypes.BOOL

GetWindowThreadProcessId = user32.GetWindowThreadProcessId
GetWindowThreadProcessId.argtypes = [
    wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
]
GetWindowThreadProcessId.restype = wintypes.DWORD

IsWindowVisible = user32.IsWindowVisible
IsWindowVisible.argtypes = [wintypes.HWND]
IsWindowVisible.restype = wintypes.BOOL

SetForegroundWindow = user32.SetForegroundWindow
SetForegroundWindow.argtypes = [wintypes.HWND]
SetForegroundWindow.restype = wintypes.BOOL

# ==========================================
# 管理员 / 提权
# ==========================================
try:
    IsUserAnAdmin = user32.IsUserAnAdmin
except AttributeError:
    # 部分 Windows 版本将 IsUserAnAdmin 放在 shell32 中
    IsUserAnAdmin = ctypes.windll.shell32.IsUserAnAdmin
IsUserAnAdmin.restype = wintypes.BOOL

shell32 = ctypes.windll.shell32
ShellExecuteW = shell32.ShellExecuteW
ShellExecuteW.argtypes = [
    wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
    wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int,
]
ShellExecuteW.restype = wintypes.HINSTANCE

# ==========================================
# UIAccess DLL (置顶穿透, 可选加载)
# ==========================================
from seewo_guard.config import resource_path  # noqa: E402

try:
    dll_path = resource_path('uiaccess.dll')
    uiaccess = ctypes.WinDLL(dll_path, use_last_error=True)
    IsUIAccess = uiaccess.IsUIAccess
    IsUIAccess.argtypes = [wintypes.HANDLE]
    IsUIAccess.restype = wintypes.BOOL
    StartUIAccessProcess = uiaccess.StartUIAccessProcess
    StartUIAccessProcess.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
    ]
    StartUIAccessProcess.restype = wintypes.BOOL
    logging.info(f"✓ UIAccess DLL 加载成功 ({dll_path})")
except Exception as e:
    logging.warning(f"[信息] 未找到 uiaccess.dll ({e}), UIAccess 功能不可用")
    IsUIAccess = StartUIAccessProcess = None

# ==========================================
# SetWindowBand (ZBID 取消置顶增强)
# ==========================================
try:
    SetWindowBand = user32.SetWindowBand
    SetWindowBand.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.DWORD]
    SetWindowBand.restype = wintypes.BOOL
    HAS_SETWINDOWBAND = True
except Exception:
    HAS_SETWINDOWBAND = False
    SetWindowBand = None

# ==========================================
# 会话结束 / 关机监听
# ==========================================
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_DESTROY = 0x0002
ENDSESSION_CLOSEAPP = 0x00000001

CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6

PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

SetConsoleCtrlHandler = kernel32.SetConsoleCtrlHandler
SetConsoleCtrlHandler.argtypes = [PHANDLER_ROUTINE, wintypes.BOOL]
SetConsoleCtrlHandler.restype = wintypes.BOOL

# ==========================================
# 窗口类 / 消息循环 (守护进程关机监听)
# ==========================================
WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
    wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", ctypes.c_uint),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


RegisterClassW = user32.RegisterClassW
RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
RegisterClassW.restype = wintypes.ATOM

CreateWindowExW = user32.CreateWindowExW
CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
CreateWindowExW.restype = wintypes.HWND

DefWindowProcW = user32.DefWindowProcW
DefWindowProcW.argtypes = [
    wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM,
]
DefWindowProcW.restype = ctypes.c_ssize_t

GetMessageW = user32.GetMessageW
GetMessageW.argtypes = [
    ctypes.POINTER(MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint,
]
GetMessageW.restype = wintypes.BOOL

TranslateMessage = user32.TranslateMessage
TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
TranslateMessage.restype = wintypes.BOOL

DispatchMessageW = user32.DispatchMessageW
DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
DispatchMessageW.restype = ctypes.c_ssize_t

PostQuitMessage = user32.PostQuitMessage
PostQuitMessage.argtypes = [ctypes.c_int]
PostQuitMessage.restype = None

GetModuleHandleW = kernel32.GetModuleHandleW
GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
GetModuleHandleW.restype = wintypes.HMODULE
