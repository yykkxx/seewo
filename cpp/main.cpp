// main.cpp - SeewoGuardCpp (C++ 版, 单文件静态编译)
// ============================================================
// 功能:
//   - 双进程架构: GUI 进程 + 守护进程 (--daemon), 互相独立
//   - 托盘图标: X = 收缩到托盘, 只有「完全退出」才退出
//   - 全局键盘钩子 WH_KEYBOARD_LL (绕过普通应用层钩子/过滤器)
//   - 杀进程: TerminateProcess (C++ 原生等价于 SIGKILL)
//   - 虚拟桌面: 纯 COM (IVirtualDesktopManagerInternal 探测),
//     新建桌面并把目标窗口移入, 主视角保持在原桌面
//   - 守护进程监听系统关机 (WM_QUERYENDSESSION/WM_ENDSESSION),
//     监控 GUI 心跳, GUI 被杀后自动重新拉起 (最多5次)
// 编译: 见 build_cpp.ps1 (/MT 静态, 单文件无依赖)
// ============================================================
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <objbase.h>
#include <shellapi.h>
#include <tlhelp32.h>
#include <psapi.h>
#include <string>
#include <vector>
#include <atomic>
#include <thread>
#include <chrono>
#include <cstdio>
#include <cstdarg>
#include <mutex>
#include <utility>

using namespace std::chrono;

// ============================================================
// 配置
// ============================================================
static const wchar_t* APP_NAME = L"SeewoGuardCpp";
static const wchar_t* TARGET_EXES[] = {
    L"C:\\Program Files (x86)\\Seewo\\SeewoYiQiXueStudent\\SeewoYiQiXueStudent_1.3.15.4527\\resources\\cppService\\screen-broadcast.exe",
    L"C:\\Program Files (x86)\\Seewo\\SeewoYiQiXueStudent\\SeewoYiQiXueStudent_1.3.15.4527\\resources\\cppService\\classroom-protect.exe",
    L"C:\\Program Files (x86)\\Seewo\\SeewoYiQiXueStudent\\SeewoYiQiXueStudent_1.3.15.4527\\resources\\cppService\\electronic-classroom.exe",
    L"C:\\Program Files (x86)\\Seewo\\SeewoYiQiXueStudent\\SeewoYiQiXueStudent_1.3.15.4527\\seewo-ecr-student.exe",
};
static const size_t TARGET_COUNT = sizeof(TARGET_EXES) / sizeof(TARGET_EXES[0]);

// 守护参数
static const int DAEMON_TICK_MS = 1000;      // 守护监控间隔
static const int DAEMON_START_GRACE = 6;     // 守护启动宽限期(秒)
static const int GUI_HEARTBEAT_TIMEOUT = 4;  // GUI 心跳超时(秒)
static const int GUI_HEARTBEAT_MS = 2000;    // GUI 心跳间隔
static const int MAX_GUI_RESTARTS = 5;       // GUI 最大重启次数

// 消息常量
#define WM_APP_TRAY   (WM_APP + 1)
#define WM_APP_HOTKEY (WM_APP + 2)

// 控件 ID
#define IDC_BTN_KILL 1001
#define IDC_BTN_DESK 1002
#define IDC_BTN_QUIT 1003
#define IDM_SHOW 2001
#define IDM_KILL 2002
#define IDM_DESK 2003
#define IDM_QUIT 2004

// ============================================================
// 全局状态
// ============================================================
static std::wstring g_exePath;   // 本程序 exe 路径
static std::wstring g_logPath;   // 日志路径 (exe 目录)
static std::mutex g_logMtx;
static HWND g_hwnd = nullptr;
static HHOOK g_hook = nullptr;
static std::atomic<bool> g_guiQuit{false};

// ============================================================
// 日志 (seewo_guard_cpp.log, UTF-8)
// ============================================================
static void Log(const wchar_t* fmt, ...) {
    wchar_t buf[2048];
    va_list ap;
    va_start(ap, fmt);
    vswprintf_s(buf, 2048, fmt, ap);
    va_end(ap);
    SYSTEMTIME st;
    GetLocalTime(&st);
    char t[64];
    sprintf_s(t, "%04d-%02d-%02d %02d:%02d:%02d ", st.wYear, st.wMonth,
              st.wDay, st.wHour, st.wMinute, st.wSecond);
    int n = WideCharToMultiByte(CP_UTF8, 0, buf, -1, nullptr, 0, nullptr, nullptr);
    std::string s(n, 0);
    WideCharToMultiByte(CP_UTF8, 0, buf, -1, &s[0], n, nullptr, nullptr);
    std::string line = std::string(t) + s + "\n";
    std::lock_guard<std::mutex> lk(g_logMtx);
    FILE* f = nullptr;
    if (_wfopen_s(&f, g_logPath.c_str(), L"ab") == 0 && f) {
        fwrite(line.c_str(), 1, line.size(), f);
        fclose(f);
    }
}

// ============================================================
// 工具函数
// ============================================================
static std::wstring GetExePath() {
    wchar_t buf[MAX_PATH] = {0};
    GetModuleFileNameW(nullptr, buf, MAX_PATH);
    return std::wstring(buf);
}

static DWORD SessionId() {
    DWORD sid = 0;
    ProcessIdToSessionId(GetCurrentProcessId(), &sid);
    return sid;
}

static std::wstring PipeName() {
    return L"\\\\.\\pipe\\SeewoGuardCpp_" + std::to_wstring(SessionId());
}

static std::wstring MutexName(bool daemon) {
    return L"Global\\SeewoGuardCpp_"
           + std::wstring(daemon ? L"Daemon_v1" : L"GUI_v1");
}

static std::wstring GetProcessPath(DWORD pid) {
    HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!h) return L"";
    wchar_t buf[1024] = {0};
    DWORD sz = 1024;
    BOOL ok = QueryFullProcessImageNameW(h, 0, buf, &sz);
    CloseHandle(h);
    if (!ok) return L"";
    return std::wstring(buf, sz);
}

static std::wstring Lower(std::wstring s) {
    for (auto& c : s) c = towlower(c);
    return s;
}

// 按完整路径(不区分大小写)查找 PID
static std::vector<DWORD> GetPidsByPath(const std::wstring& path) {
    std::vector<DWORD> out;
    std::wstring key = Lower(path);
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return out;
    PROCESSENTRY32W pe = {0};
    pe.dwSize = sizeof(pe);
    if (Process32FirstW(snap, &pe)) {
        do {
            std::wstring p = GetProcessPath(pe.th32ProcessID);
            if (!p.empty() && Lower(p) == key) out.push_back(pe.th32ProcessID);
        } while (Process32NextW(snap, &pe));
    }
    CloseHandle(snap);
    return out;
}

static bool KillPid(DWORD pid) {
    HANDLE h = OpenProcess(PROCESS_TERMINATE, FALSE, pid);
    if (!h) return false;
    BOOL ok = TerminateProcess(h, 1);
    CloseHandle(h);
    return ok != FALSE;
}

// 击杀全部目标进程
static int KillProcesses() {
    int n = 0;
    for (size_t i = 0; i < TARGET_COUNT; i++) {
        for (DWORD pid : GetPidsByPath(TARGET_EXES[i])) {
            if (KillPid(pid)) n++;
        }
    }
    Log(L"[杀进程] 完成: %d 个", n);
    return n;
}

struct EnumCtx {
    std::wstring key;
    std::vector<HWND> hwnds;
};

static BOOL CALLBACK EnumProc(HWND hwnd, LPARAM lp) {
    EnumCtx* ctx = reinterpret_cast<EnumCtx*>(lp);
    if (!IsWindowVisible(hwnd)) return TRUE;
    DWORD pid = 0;
    GetWindowThreadProcessId(hwnd, &pid);
    if (!pid) return TRUE;
    std::wstring p = GetProcessPath(pid);
    if (p.empty()) return TRUE;
    if (Lower(p) == ctx->key) ctx->hwnds.push_back(hwnd);
    return TRUE;
}

// 按 exe 路径查找所有可见窗口
static std::vector<HWND> FindWindowsByPath(const std::wstring& path) {
    EnumCtx ctx;
    ctx.key = Lower(path);
    EnumWindows(EnumProc, reinterpret_cast<LPARAM>(&ctx));
    return ctx.hwnds;
}

static void SpawnDaemon() {
    std::wstring cmd = L"\"" + g_exePath + L"\" --daemon";
    STARTUPINFOW si = {0};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi = {0};
    if (CreateProcessW(nullptr, &cmd[0], nullptr, nullptr, FALSE,
                       CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi)) {
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        Log(L"[守护] 已拉起");
    } else {
        Log(L"[守护] 拉起失败: err=%lu", GetLastError());
    }
}

static void SpawnGui() {
    std::wstring cmd = L"\"" + g_exePath + L"\"";
    STARTUPINFOW si = {0};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi = {0};
    if (CreateProcessW(nullptr, &cmd[0], nullptr, nullptr, FALSE, 0,
                       nullptr, nullptr, &si, &pi)) {
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        Log(L"[守护] GUI 已重新拉起");
    } else {
        Log(L"[守护] GUI 拉起失败: err=%lu", GetLastError());
    }
}

static int GetBuildNumber() {
    typedef LONG(WINAPI* fnRtlGetVersion)(void*);
    HMODULE nt = GetModuleHandleW(L"ntdll.dll");
    if (!nt) return 0;
    auto f = reinterpret_cast<fnRtlGetVersion>(GetProcAddress(nt, "RtlGetVersion"));
    if (!f) return 0;
    struct RtlOvi {
        DWORD dwOSVersionInfoSize;
        DWORD dwMajorVersion;
        DWORD dwMinorVersion;
        DWORD dwBuildNumber;
        DWORD dwPlatformId;
        WCHAR szCSDVersion[128];
    } ovi = {0};
    ovi.dwOSVersionInfoSize = sizeof(ovi);
    if (f(&ovi) == 0) return (int)ovi.dwBuildNumber;
    return 0;
}

// ============================================================
// 虚拟桌面 (纯 COM, 参考 pyvda 布局探测)
// ============================================================
static const GUID CLSID_ImmersiveShell = {0xc2f03a33, 0x21f5, 0x47fa,
    {0xb4, 0xbb, 0x15, 0x63, 0x62, 0xa2, 0xf2, 0x39}};
static const GUID IID_IServiceProvider = {0x6d5140c1, 0x7436, 0x11ce,
    {0x80, 0x34, 0x00, 0xaa, 0x00, 0x60, 0x09, 0xfa}};
static const GUID CLSID_Internal = {0xc5e0cdca, 0x7b6e, 0x41b2,
    {0x9f, 0xc4, 0xd9, 0x39, 0x75, 0xcc, 0x46, 0x7b}};
static const GUID IID_ViewCollection = {0x1841c6d7, 0x4f9d, 0x42c0,
    {0xaf, 0x41, 0x87, 0x47, 0x53, 0x8f, 0x10, 0xe5}};
static const GUID ZERO_GUID = {0, 0, 0, {0, 0, 0, 0, 0, 0, 0, 0}};

struct VariantInfo {
    const wchar_t* iid;   // 内部接口 IID
    const wchar_t* label;
    int get_current, sw, create, remove, find;
    bool hwnd;            // hwnd 变体: GetCurrent/Switch/Create 前置 HWND 参数
};
static const VariantInfo VARIANTS[] = {
    {L"{53f5ca0b-158f-4124-900c-057158060b27}", L"v26100", 6, 9, 11, 13, 14, false},
    {L"{4970ba3d-fd4e-4647-bea3-d89076ef4b9c}", L"v22631", 6, 9, 11, 13, 14, false},
    {L"{a3175f2d-239c-4bd2-8aa0-eeba8b0b138e}", L"v22621", 6, 9, 10, 12, 13, false},
    {L"{b2f925b9-5a0f-4d2e-9f4d-2b1507593c10}", L"hwnd21313", 6, 9, 10, 12, 13, true},
    {L"{094afe11-44f2-4ba0-976f-29a97e263ee0}", L"hwnd20231", 6, 9, 10, 11, 12, true},
    {L"{f31574d6-b682-4cdc-bd56-1827860abec6}", L"v9000", 6, 9, 10, 11, 12, false},
};
// hwnd21313 在 build>=22449 时方法表插入了 GetAllCurrentDesktops -> v22449
static const VariantInfo V22449 = {L"{b2f925b9-5a0f-4d2e-9f4d-2b1507593c10}",
                                   L"v22449", 6, 10, 11, 13, 14, true};

typedef HRESULT(__stdcall* FnQueryService)(IUnknown*, REFGUID, REFIID, void**);
typedef HRESULT(__stdcall* FnGetCurrent)(void*, void**);
typedef HRESULT(__stdcall* FnGetCurrentH)(void*, HWND, void**);
typedef HRESULT(__stdcall* FnSwitch)(void*, void*);
typedef HRESULT(__stdcall* FnSwitchH)(void*, HWND, void*);
typedef HRESULT(__stdcall* FnCreate)(void*, void**);
typedef HRESULT(__stdcall* FnCreateH)(void*, HWND, void**);
typedef HRESULT(__stdcall* FnFind)(void*, GUID*, void**);
typedef HRESULT(__stdcall* FnRemove)(void*, void*);
typedef HRESULT(__stdcall* FnGetId)(void*, GUID*);
typedef HRESULT(__stdcall* FnGetViewForHwnd)(void*, HWND, void**);
typedef HRESULT(__stdcall* FnMoveViewToDesktop)(void*, void*, void*);

class VirtualDesktop {
public:
    bool available = false;
    static VariantInfo mgrSlots;   // 当前生效布局

    bool Init() {
        HRESULT hr = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
        if (FAILED(hr) && hr != RPC_E_CHANGED_MODE) CoInitialize(nullptr);
        int build = GetBuildNumber();
        void* svc = CoCreateInstanceRaw(CLSID_ImmersiveShell, IID_IServiceProvider);
        if (!svc) return false;
        bool ok = false;
        FnQueryService qs = SlotFn<FnQueryService>(svc, 3);
        for (const auto& v : VARIANTS) {
            GUID iid;
            if (!ParseGuid(v.iid, iid)) continue;
            void* ppv = nullptr;
            HRESULT h = qs(static_cast<IUnknown*>(svc), CLSID_Internal, iid, &ppv);
            if (SUCCEEDED(h) && ppv) {
                static_cast<IUnknown*>(ppv)->Release();
                mgrSlots = v;
                if (v.hwnd && build >= 22449 &&
                    wcscmp(v.label, L"hwnd21313") == 0) {
                    mgrSlots = V22449;
                }
                ok = true;
                Log(L"[桌面] 内部接口: %s (build=%d)", mgrSlots.label, build);
                break;
            }
        }
        static_cast<IUnknown*>(svc)->Release();
        available = ok;
        return ok;
    }

    static void** Vt(void* p) { return *reinterpret_cast<void***>(p); }

    // void* -> 函数指针 (标准 C++ 不允许 reinterpret_cast, 用 memcpy)
    template <typename Fn>
    static Fn SlotFn(void* p, int idx) {
        void* raw = Vt(p)[idx];
        Fn fn = nullptr;
        memcpy(&fn, &raw, sizeof(fn));
        return fn;
    }

    static bool ParseGuid(const wchar_t* s, GUID& g) {
        return swscanf_s(s,
            L"{%08lx-%04hx-%04hx-%02hhx%02hhx-%02hhx%02hhx%02hhx%02hhx%02hhx%02hhx}",
            &g.Data1, &g.Data2, &g.Data3,
            &g.Data4[0], &g.Data4[1], &g.Data4[2], &g.Data4[3],
            &g.Data4[4], &g.Data4[5], &g.Data4[6], &g.Data4[7]) == 11;
    }

    static void* CoCreateInstanceRaw(REFCLSID clsid, REFIID iid) {
        void* ppv = nullptr;
        HRESULT hr = CoCreateInstance(clsid, nullptr, 0x17, iid, &ppv);
        return SUCCEEDED(hr) ? ppv : nullptr;
    }

    static void* QueryViewCollection() {
        void* svc = CoCreateInstanceRaw(CLSID_ImmersiveShell, IID_IServiceProvider);
        if (!svc) return nullptr;
        FnQueryService qs = SlotFn<FnQueryService>(svc, 3);
        void* col = nullptr;
        HRESULT hr = qs(static_cast<IUnknown*>(svc), IID_ViewCollection,
                        IID_ViewCollection, &col);
        static_cast<IUnknown*>(svc)->Release();
        return SUCCEEDED(hr) ? col : nullptr;
    }

    // ---------- vtable 调用 ----------
    static HRESULT GetCurrent(void* mgr, void** out) {
        if (mgrSlots.hwnd)
            return SlotFn<FnGetCurrentH>(mgr, mgrSlots.get_current)(mgr, nullptr, out);
        return SlotFn<FnGetCurrent>(mgr, mgrSlots.get_current)(mgr, out);
    }
    static HRESULT SwitchTo(void* mgr, void* vd) {
        if (mgrSlots.hwnd)
            return SlotFn<FnSwitchH>(mgr, mgrSlots.sw)(mgr, nullptr, vd);
        return SlotFn<FnSwitch>(mgr, mgrSlots.sw)(mgr, vd);
    }
    static HRESULT CreateDesk(void* mgr, void** out) {
        if (mgrSlots.hwnd)
            return SlotFn<FnCreateH>(mgr, mgrSlots.create)(mgr, nullptr, out);
        return SlotFn<FnCreate>(mgr, mgrSlots.create)(mgr, out);
    }
    static HRESULT FindDesk(void* mgr, const GUID* g, void** out) {
        return SlotFn<FnFind>(mgr, mgrSlots.find)(mgr, const_cast<GUID*>(g), out);
    }
    static HRESULT GetDesktopId(void* vd, GUID* out) {
        return SlotFn<FnGetId>(vd, 4)(vd, out);
    }
    static HRESULT MoveView(void* mgr, void* view, void* vd) {
        return SlotFn<FnMoveViewToDesktop>(mgr, 4)(mgr, view, vd);
    }

    // 获取内部接口管理器 (调用方 Release)
    void* GetInternal() {
        void* svc = CoCreateInstanceRaw(CLSID_ImmersiveShell, IID_IServiceProvider);
        if (!svc) return nullptr;
        FnQueryService qs = SlotFn<FnQueryService>(svc, 3);
        void* mgr = nullptr;
        GUID iid;
        if (ParseGuid(mgrSlots.iid, iid)) {
            HRESULT hr = qs(static_cast<IUnknown*>(svc), CLSID_Internal,
                            iid, &mgr);
            if (FAILED(hr)) mgr = nullptr;
        }
        static_cast<IUnknown*>(svc)->Release();
        return mgr;
    }

    // ---------- 注册表 ----------
    static std::vector<GUID> RegDesktopGuids() {
        std::vector<GUID> out;
        HKEY k = nullptr;
        if (RegOpenKeyExW(HKEY_CURRENT_USER,
                L"Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\VirtualDesktops",
                0, KEY_READ, &k) != ERROR_SUCCESS) {
            return out;
        }
        DWORD sz = 0;
        if (RegQueryValueExW(k, L"VirtualDesktopIDs", nullptr, nullptr,
                            nullptr, &sz) == ERROR_SUCCESS && sz >= 16) {
            std::vector<BYTE> buf(sz);
            if (RegQueryValueExW(k, L"VirtualDesktopIDs", nullptr, nullptr,
                                buf.data(), &sz) == ERROR_SUCCESS) {
                for (size_t i = 0; i + 16 <= sz; i += 16) {
                    GUID g;
                    memcpy(&g, buf.data() + i, 16);
                    out.push_back(g);
                }
            }
        }
        RegCloseKey(k);
        return out;
    }

    static bool RegCurrentDesktop(GUID& out) {
        HKEY k = nullptr;
        if (RegOpenKeyExW(HKEY_CURRENT_USER,
                L"Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\VirtualDesktops",
                0, KEY_READ, &k) != ERROR_SUCCESS) {
            return false;
        }
        DWORD sz = 16;
        BYTE buf[16] = {0};
        LONG r = RegQueryValueExW(k, L"CurrentVirtualDesktop", nullptr, nullptr,
                                  buf, &sz);
        RegCloseKey(k);
        if (r != ERROR_SUCCESS || sz != 16) return false;
        memcpy(&out, buf, 16);
        return true;
    }

    static bool GuidEq(const GUID& a, const GUID& b) {
        return memcmp(&a, &b, 16) == 0;
    }

    int GetDesktopCount() {
        int n = (int)RegDesktopGuids().size();
        return n >= 1 ? n : 1;
    }

    bool GetDesktopGuid(int idx, GUID& out) {
        auto gs = RegDesktopGuids();
        if (idx >= 0 && idx < (int)gs.size()) {
            out = gs[idx];
            return true;
        }
        return false;
    }

    // 当前桌面索引: 注册表优先, COM 兜底
    int GetCurrentDesktopIdx() {
        GUID cur;
        if (RegCurrentDesktop(cur)) {
            auto gs = RegDesktopGuids();
            for (size_t i = 0; i < gs.size(); i++) {
                if (GuidEq(gs[i], cur)) return (int)i;
            }
        }
        void* mgr = GetInternal();
        if (mgr) {
            void* vd = nullptr;
            if (SUCCEEDED(GetCurrent(mgr, &vd)) && vd) {
                GUID g;
                if (SUCCEEDED(GetDesktopId(vd, &g))) {
                    auto gs = RegDesktopGuids();
                    for (size_t i = 0; i < gs.size(); i++) {
                        if (GuidEq(gs[i], g)) {
                            static_cast<IUnknown*>(vd)->Release();
                            static_cast<IUnknown*>(mgr)->Release();
                            return (int)i;
                        }
                    }
                }
                static_cast<IUnknown*>(vd)->Release();
            }
            static_cast<IUnknown*>(mgr)->Release();
        }
        return 0;
    }

    // 创建新桌面 (纯 COM), 视角被自动切走则切回; 返回新桌面 GUID
    bool CreateDesktopCom(GUID& newGuid) {
        void* mgr = GetInternal();
        if (!mgr) return false;
        bool ok = false;
        void* curVd = nullptr;
        if (SUCCEEDED(GetCurrent(mgr, &curVd)) && curVd) {
            GUID curGuid;
            GetDesktopId(curVd, &curGuid);
            void* newVd = nullptr;
            if (SUCCEEDED(CreateDesk(mgr, &newVd)) && newVd) {
                if (SUCCEEDED(GetDesktopId(newVd, &newGuid))) ok = true;
                static_cast<IUnknown*>(newVd)->Release();
            }
            // 少数系统创建后视角自动切到新桌面: 切回原桌面
            void* nowVd = nullptr;
            if (SUCCEEDED(GetCurrent(mgr, &nowVd)) && nowVd) {
                GUID nowGuid;
                GetDesktopId(nowVd, &nowGuid);
                if (!GuidEq(nowGuid, curGuid)) {
                    SwitchTo(mgr, curVd);
                    Log(L"[桌面] 视角被自动切走, 已切回原桌面");
                }
                static_cast<IUnknown*>(nowVd)->Release();
            }
            static_cast<IUnknown*>(curVd)->Release();
        }
        static_cast<IUnknown*>(mgr)->Release();
        return ok;
    }

    // 移动窗口到指定 GUID 桌面 (IApplicationViewCollection + MoveViewToDesktop)
    bool MoveWindowToGuid(HWND hwnd, const GUID& guid) {
        void* mgr = GetInternal();
        if (!mgr) return false;
        bool ok = false;
        void* vd = nullptr;
        if (SUCCEEDED(FindDesk(mgr, &guid, &vd)) && vd) {
            void* col = QueryViewCollection();
            if (col) {
                FnGetViewForHwnd gv =
                    SlotFn<FnGetViewForHwnd>(col, 6);
                void* view = nullptr;
                if (SUCCEEDED(gv(col, hwnd, &view)) && view) {
                    ok = SUCCEEDED(MoveView(mgr, view, vd));
                    static_cast<IUnknown*>(view)->Release();
                }
                static_cast<IUnknown*>(col)->Release();
            }
            static_cast<IUnknown*>(vd)->Release();
        }
        static_cast<IUnknown*>(mgr)->Release();
        return ok;
    }

    // 仅移动窗口到指定索引桌面 (不切换视角)
    bool MoveWindowToDesktop(HWND hwnd, int idx) {
        GUID g;
        if (!GetDesktopGuid(idx, g)) return false;
        return MoveWindowToGuid(hwnd, g);
    }

    // 切换到指定索引桌面
    bool SwitchDesktop(int idx) {
        GUID g;
        if (!GetDesktopGuid(idx, g)) return false;
        void* mgr = GetInternal();
        if (!mgr) return false;
        void* vd = nullptr;
        if (SUCCEEDED(FindDesk(mgr, &g, &vd)) && vd) {
            HRESULT hr = SwitchTo(mgr, vd);
            static_cast<IUnknown*>(vd)->Release();
            static_cast<IUnknown*>(mgr)->Release();
            return SUCCEEDED(hr);
        }
        static_cast<IUnknown*>(mgr)->Release();
        return false;
    }

    // 新建桌面并把窗口移入, 主视角保持在原桌面
    // 返回 (新桌面索引, 成功移动数); 失败 (-1, 0)
    std::pair<int, int> CreateNewDesktopAndMove(const std::vector<HWND>& hwnds) {
        if (hwnds.empty()) return {-1, 0};
        int cur = GetCurrentDesktopIdx();
        GUID newGuid = ZERO_GUID;
        if (!CreateDesktopCom(newGuid)) return {-1, 0};
        int newIdx = -1;
        {
            auto gs = RegDesktopGuids();
            for (size_t i = 0; i < gs.size(); i++) {
                if (GuidEq(gs[i], newGuid)) { newIdx = (int)i; break; }
            }
        }
        if (newIdx < 0) newIdx = std::max(0, GetDesktopCount() - 1);
        int moved = 0;
        for (HWND h : hwnds) {
            if (MoveWindowToGuid(h, newGuid)) moved++;
        }
        Log(L"[桌面] 已新建桌面#%d, 移动 %d/%d 个窗口 (原桌面#%d)",
            newIdx, moved, (int)hwnds.size(), cur);
        return {newIdx, moved};
    }
};
VariantInfo VirtualDesktop::mgrSlots;

// 全局虚拟桌面实例 (GUI 进程使用)
static VirtualDesktop g_vd;

// ============================================================
// IPC (命名管道)
// ============================================================
// GUI -> 守护: HEARTBEAT <pid>\n / SHUTDOWN\n
// 守护 -> GUI: OK\n
static std::atomic<bool> g_daemonShutdown{false};
static std::atomic<long long> g_lastHeartbeatMs{0};   // 毫秒时间戳
static std::atomic<bool> g_guiRegistered{false};
static std::atomic<int> g_guiRestarts{0};
static DWORD g_daemonMainTid = 0;

static long long NowMs() {
    return duration_cast<milliseconds>(
        steady_clock::now().time_since_epoch()).count();
}

// 单个管道连接的读写循环 (每连接一线程)
static void DaemonHandleConnection(HANDLE hPipe) {
    char buf[512];
    while (!g_daemonShutdown.load()) {
        DWORD rd = 0;
        if (!ReadFile(hPipe, buf, sizeof(buf) - 1, &rd, nullptr) || rd == 0) {
            break;
        }
        buf[rd] = 0;
        std::string msg(buf, rd);
        DWORD wr = 0;
        if (msg.find("SHUTDOWN") != std::string::npos) {
            WriteFile(hPipe, "OK\n", 3, &wr, nullptr);
            Log(L"[守护] 收到 GUI 完全退出指令, 守护进程退出");
            g_daemonShutdown.store(true);
            // 唤醒守护主线程的消息循环
            PostThreadMessageW(g_daemonMainTid, WM_QUIT, 0, 0);
            break;
        }
        if (msg.find("HEARTBEAT") != std::string::npos) {
            g_lastHeartbeatMs.store(NowMs());
            if (!g_guiRegistered.load()) {
                g_guiRegistered.store(true);
                std::string pid = msg.substr(10);
                while (!pid.empty() &&
                       (pid.back() == '\n' || pid.back() == '\r')) {
                    pid.pop_back();
                }
                Log(L"[守护] 收到 GUI 心跳 (PID=%hs)", pid.c_str());
            }
            WriteFile(hPipe, "OK\n", 3, &wr, nullptr);
        }
    }
    CloseHandle(hPipe);
}

// 守护进程: 管道服务线程 (多实例, 心跳与指令可并行连接)
static void DaemonPipeServer() {
    std::wstring name = PipeName();
    Log(L"[IPC] 服务启动 (%ls)", name.c_str());
    while (!g_daemonShutdown.load()) {
        HANDLE hPipe = CreateNamedPipeW(name.c_str(),
            PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
            4, 4096, 4096, 0, nullptr);
        if (hPipe == INVALID_HANDLE_VALUE) {
            Sleep(500);
            continue;
        }
        BOOL ok = ConnectNamedPipe(hPipe, nullptr)
            ? TRUE : (GetLastError() == ERROR_PIPE_CONNECTED);
        if (!ok) {
            CloseHandle(hPipe);
            continue;
        }
        if (g_daemonShutdown.load()) {
            CloseHandle(hPipe);
            break;
        }
        // 每连接一个处理线程
        std::thread(DaemonHandleConnection, hPipe).detach();
    }
    Log(L"[IPC] 服务已停止");
}

// 守护进程: 心跳监控线程 (GUI 被杀自动重启)
static void DaemonMonitor() {
    auto start = steady_clock::now();
    while (!g_daemonShutdown.load()) {
        Sleep(DAEMON_TICK_MS);
        if (g_daemonShutdown.load()) break;
        auto now = steady_clock::now();
        bool needRestart = false;
        const wchar_t* why = L"";
        if (!g_guiRegistered.load()) {
            if (duration_cast<seconds>(now - start).count() > DAEMON_START_GRACE) {
                needRestart = true;
                why = L"宽限期已过仍未收到 GUI 心跳";
            }
        } else {
            long long last = g_lastHeartbeatMs.load();
            long long cur = NowMs();
            if (last > 0 && (cur - last) / 1000 > GUI_HEARTBEAT_TIMEOUT) {
                needRestart = true;
                why = L"GUI 心跳丢失";
            }
        }
        if (needRestart) {
            if (g_guiRestarts.load() >= MAX_GUI_RESTARTS) {
                Log(L"[守护] GUI 重启已达 %d 次上限, 停止", MAX_GUI_RESTARTS);
                g_daemonShutdown.store(true);
                break;
            }
            Log(L"[守护] %ls, 立即重启 GUI", why);
            SpawnGui();
            g_guiRestarts.fetch_add(1);
            g_guiRegistered.store(false);
            start = now;  // 重置宽限期
        }
    }
}

// 守护进程隐藏窗口: 监听系统关机/注销
static LRESULT CALLBACK DaemonWndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
        case WM_QUERYENDSESSION:
            Log(L"[守护] WM_QUERYENDSESSION: 准备退出");
            g_daemonShutdown.store(true);
            return TRUE;
        case WM_ENDSESSION:
            Log(L"[守护] WM_ENDSESSION: 退出");
            g_daemonShutdown.store(true);
            PostQuitMessage(0);
            return 0;
        case WM_DESTROY:
            PostQuitMessage(0);
            return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

// 守护进程主入口
static int DaemonMain() {
    Log(L"=====================================================");
    Log(L"  SeewoGuardCpp 守护进程 (双进程架构)");
    Log(L"  PID: %lu  会话: %lu", GetCurrentProcessId(), SessionId());
    Log(L"=====================================================");

    // 单实例
    HANDLE hMutex = CreateMutexW(nullptr, FALSE, MutexName(true).c_str());
    if (hMutex && GetLastError() == ERROR_ALREADY_EXISTS) {
        Log(L"[守护] 已有守护进程运行, 退出");
        return 0;
    }

    // 隐藏窗口 (监听关机)
    WNDCLASSW wc = {0};
    wc.lpfnWndProc = DaemonWndProc;
    wc.hInstance = GetModuleHandleW(nullptr);
    wc.lpszClassName = L"SeewoGuardCppDaemonWnd";
    RegisterClassW(&wc);
    CreateWindowW(wc.lpszClassName, L"SeewoGuardCpp Daemon",
                  0, 0, 0, 0, 0, nullptr, nullptr, wc.hInstance, nullptr);

    // IPC + 监控线程
    g_daemonMainTid = GetCurrentThreadId();
    std::thread pipeThread(DaemonPipeServer);
    std::thread monThread(DaemonMonitor);
    Log(L"[守护] 就绪, 开始监控 GUI 心跳");

    // 消息循环
    MSG msg;
    while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }

    g_daemonShutdown.store(true);
    // 唤醒阻塞在 ConnectNamedPipe 的 accept 循环
    HANDLE wake = CreateFileW(PipeName().c_str(),
        GENERIC_READ | GENERIC_WRITE, 0, nullptr, OPEN_EXISTING, 0, nullptr);
    if (wake != INVALID_HANDLE_VALUE) CloseHandle(wake);
    if (pipeThread.joinable()) pipeThread.join();
    if (monThread.joinable()) monThread.join();
    if (hMutex) CloseHandle(hMutex);
    Log(L"[守护] 已安全退出");
    return 0;
}

// ============================================================
// GUI 进程
// ============================================================
static void GuiShowWindow() {
    if (g_hwnd) {
        ShowWindow(g_hwnd, SW_SHOW);
        SetForegroundWindow(g_hwnd);
    }
}

static void GuiHideToTray() {
    if (g_hwnd) ShowWindow(g_hwnd, SW_HIDE);
}

// 完全退出
static void GuiFullQuit() {
    g_guiQuit.store(true);
    Log(L"[GUI] 请求完全退出...");

    if (g_hook) {
        UnhookWindowsHookEx(g_hook);
        g_hook = nullptr;
    }

    // 通知守护进程关闭
    HANDLE hPipe = CreateFileW(PipeName().c_str(),
        GENERIC_READ | GENERIC_WRITE, 0, nullptr, OPEN_EXISTING, 0, nullptr);
    if (hPipe != INVALID_HANDLE_VALUE) {
        const char* msg = "SHUTDOWN\n";
        DWORD wr = 0;
        WriteFile(hPipe, msg, 9, &wr, nullptr);
        CloseHandle(hPipe);
        Log(L"[GUI] 守护进程关闭指令: 已送达");
    } else {
        Log(L"[GUI] 守护进程关闭指令: 未送达 (可能已退出)");
    }

    // 删除托盘图标
    NOTIFYICONDATAW nid = {0};
    nid.cbSize = sizeof(nid);
    nid.hWnd = g_hwnd;
    nid.uID = 1;
    Shell_NotifyIconW(NIM_DELETE, &nid);

    Log(L"[GUI] 已退出");
    PostQuitMessage(0);
}

// 托盘右键菜单
static void ShowTrayMenu() {
    HMENU menu = CreatePopupMenu();
    AppendMenuW(menu, MF_STRING, IDM_SHOW, L"显示窗口");
    AppendMenuW(menu, MF_STRING, IDM_KILL, L"杀进程");
    AppendMenuW(menu, MF_STRING, IDM_DESK, L"新建桌面并移动");
    AppendMenuW(menu, MF_SEPARATOR, 0, nullptr);
    AppendMenuW(menu, MF_STRING, IDM_QUIT, L"完全退出");
    POINT pt;
    GetCursorPos(&pt);
    SetForegroundWindow(g_hwnd);
    TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN,
                   pt.x, pt.y, 0, g_hwnd, nullptr);
    DestroyMenu(menu);
}

// 键盘钩子回调 (WH_KEYBOARD_LL, 可绕过普通应用层钩子/过滤器)
static LRESULT CALLBACK LowLevelKeyboardProc(int nCode, WPARAM wParam,
                                             LPARAM lParam) {
    if (nCode == HC_ACTION &&
        (wParam == WM_KEYDOWN || wParam == WM_SYSKEYDOWN)) {
        auto* kb = reinterpret_cast<KBDLLHOOKSTRUCT*>(lParam);
        if ((GetAsyncKeyState(VK_CONTROL) & 0x8000) &&
            (GetAsyncKeyState(VK_MENU) & 0x8000)) {
            int hid = 0;
            if (kb->vkCode == 'Y') hid = 1;      // 显示窗口
            else if (kb->vkCode == 'K') hid = 2; // 杀进程
            else if (kb->vkCode == 'Q') hid = 3; // 完全退出
            if (hid) {
                PostMessageW(g_hwnd, WM_APP_HOTKEY, (WPARAM)hid, 0);
                return 1;  // 吞掉按键, 其他软件收不到
            }
        }
    }
    return CallNextHookEx(g_hook, nCode, wParam, lParam);
}

// GUI 心跳线程
static void GuiHeartbeatThread() {
    while (!g_guiQuit.load()) {
        HANDLE hPipe = CreateFileW(PipeName().c_str(),
            GENERIC_READ | GENERIC_WRITE, 0, nullptr, OPEN_EXISTING, 0, nullptr);
        if (hPipe == INVALID_HANDLE_VALUE) {
            Sleep(GUI_HEARTBEAT_MS);
            continue;
        }
        while (!g_guiQuit.load()) {
            std::string msg = "HEARTBEAT " +
                std::to_string(GetCurrentProcessId()) + "\n";
            DWORD wr = 0, rd = 0;
            char resp[16] = {0};
            if (!WriteFile(hPipe, msg.c_str(), (DWORD)msg.size(), &wr, nullptr))
                break;
            if (!ReadFile(hPipe, resp, sizeof(resp), &rd, nullptr)) break;
            Sleep(GUI_HEARTBEAT_MS);
        }
        CloseHandle(hPipe);
    }
}

static void GuiStatus(const wchar_t* text) {
    if (g_hwnd) SetWindowTextW(g_hwnd, text);
}

// 杀进程 (按钮/托盘/热键共用)
static void DoKill() {
    std::thread([&] {
        int n = KillProcesses();
        wchar_t buf[128];
        swprintf_s(buf, L"SeewoGuardCpp - 已杀 %d 个目标进程", n);
        GuiStatus(buf);
    }).detach();
}

// 新建桌面并移动 (按钮/托盘/热键共用)
static void DoNewDesktop() {
    std::thread([&] {
        std::vector<HWND> hwnds;
        for (size_t i = 0; i < TARGET_COUNT; i++) {
            auto ws = FindWindowsByPath(TARGET_EXES[i]);
            hwnds.insert(hwnds.end(), ws.begin(), ws.end());
        }
        if (hwnds.empty()) {
            Log(L"[桌面] 未找到目标程序窗口, 无法移动");
            GuiStatus(L"SeewoGuardCpp - 未找到目标窗口");
            return;
        }
        auto res = g_vd.CreateNewDesktopAndMove(hwnds);
        int idx = res.first, moved = res.second;
        if (idx >= 0 && g_hwnd) {
            wchar_t buf[128];
            swprintf_s(buf, L"SeewoGuardCpp - 已把 %d 个窗口移入新桌面 %d",
                       moved, idx);
            GuiStatus(buf);
            NOTIFYICONDATAW nid = {0};
            nid.cbSize = sizeof(nid);
            nid.hWnd = g_hwnd;
            nid.uID = 1;
            nid.uFlags = NIF_INFO;
            nid.dwInfoFlags = NIIF_INFO;
            wcscpy_s(nid.szInfoTitle, APP_NAME);
            swprintf_s(nid.szInfo, L"已把 %d 个窗口移入新桌面 %d, 主视角已切回",
                       moved, idx);
            Shell_NotifyIconW(NIM_MODIFY, &nid);
        } else {
            GuiStatus(L"SeewoGuardCpp - 新建桌面失败");
        }
    }).detach();
}

// 主窗口过程
static LRESULT CALLBACK GuiWndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
        case WM_CREATE:
            CreateWindowW(L"BUTTON", L"杀进程", WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
                          16, 16, 220, 40, hwnd, (HMENU)IDC_BTN_KILL, nullptr, nullptr);
            CreateWindowW(L"BUTTON", L"新建桌面并移动",
                          WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
                          16, 64, 220, 40, hwnd, (HMENU)IDC_BTN_DESK, nullptr, nullptr);
            CreateWindowW(L"BUTTON", L"完全退出", WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
                          16, 112, 220, 40, hwnd, (HMENU)IDC_BTN_QUIT, nullptr, nullptr);
            CreateWindowW(L"STATIC",
                L"点击 X = 收缩到托盘\n只有「完全退出」才退出\n热键: Ctrl+Alt+Y 显示 | K 杀进程 | Q 退出",
                WS_CHILD | WS_VISIBLE, 16, 160, 220, 90, hwnd, nullptr, nullptr, nullptr);
            return 0;
        case WM_COMMAND: {
            int id = LOWORD(wp);
            if (id == IDC_BTN_KILL || id == IDM_KILL) DoKill();
            else if (id == IDC_BTN_DESK || id == IDM_DESK) DoNewDesktop();
            else if (id == IDC_BTN_QUIT || id == IDM_QUIT) GuiFullQuit();
            else if (id == IDM_SHOW) GuiShowWindow();
            return 0;
        }
        case WM_APP_TRAY:
            if (LOWORD(lp) == WM_RBUTTONUP || LOWORD(lp) == WM_CONTEXTMENU) {
                ShowTrayMenu();
            } else if (LOWORD(lp) == WM_LBUTTONDBLCLK) {
                GuiShowWindow();
            }
            return 0;
        case WM_APP_HOTKEY:
            if (wp == 1) GuiShowWindow();
            else if (wp == 2) DoKill();
            else if (wp == 3) GuiFullQuit();
            return 0;
        case WM_CLOSE:
            // X / Alt+F4 / 任务栏关闭 -> 收缩到托盘
            Log(L"[GUI] 关闭请求已拦截, 收缩到托盘");
            GuiHideToTray();
            {
                NOTIFYICONDATAW nid = {0};
                nid.cbSize = sizeof(nid);
                nid.hWnd = hwnd;
                nid.uID = 1;
                nid.uFlags = NIF_INFO;
                nid.dwInfoFlags = NIIF_INFO;
                wcscpy_s(nid.szInfoTitle, APP_NAME);
                wcscpy_s(nid.szInfo, L"程序仍在守护中, 双击托盘图标显示窗口");
                Shell_NotifyIconW(NIM_MODIFY, &nid);
            }
            return 0;
        case WM_QUERYENDSESSION:
            Log(L"[GUI] 系统关机/注销 (守护进程将安全退出)");
            return TRUE;
        case WM_ENDSESSION:
            if (wp) {
                Log(L"[GUI] 系统会话结束, GUI 退出");
                g_guiQuit.store(true);
            }
            return 0;
        case WM_DESTROY:
            PostQuitMessage(0);
            return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

// GUI 主入口
static int GuiMain(bool testAutoQuit) {
    Log(L"=====================================================");
    Log(L"  SeewoGuardCpp GUI 进程 (双进程架构)");
    Log(L"  PID: %lu  会话: %lu", GetCurrentProcessId(), SessionId());
    Log(L"=====================================================");

    // 单实例
    HANDLE hMutex = CreateMutexW(nullptr, FALSE, MutexName(false).c_str());
    if (hMutex && GetLastError() == ERROR_ALREADY_EXISTS) {
        Log(L"[GUI] 已有界面进程运行, 退出");
        return 0;
    }

    // 确保守护进程运行
    HANDLE hPipe = CreateFileW(PipeName().c_str(),
        GENERIC_READ | GENERIC_WRITE, 0, nullptr, OPEN_EXISTING, 0, nullptr);
    if (hPipe == INVALID_HANDLE_VALUE) {
        Log(L"[GUI] 守护进程未运行, 正在拉起...");
        SpawnDaemon();
    } else {
        CloseHandle(hPipe);
    }

    // 虚拟桌面初始化
    if (g_vd.Init()) {
        Log(L"[GUI] 虚拟桌面 API 初始化成功 (纯 COM)");
    } else {
        Log(L"[GUI] 虚拟桌面 API 不可用 (不影响其他功能)");
    }

    // 主窗口
    HINSTANCE hInst = GetModuleHandleW(nullptr);
    WNDCLASSW wc = {0};
    wc.lpfnWndProc = GuiWndProc;
    wc.hInstance = hInst;
    wc.hIcon = LoadIconW(hInst, MAKEINTRESOURCEW(1));
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.lpszClassName = L"SeewoGuardCppMainWnd";
    RegisterClassW(&wc);
    g_hwnd = CreateWindowW(wc.lpszClassName, L"SeewoGuardCpp",
                           WS_OVERLAPPEDWINDOW,
                           CW_USEDEFAULT, CW_USEDEFAULT, 268, 300,
                           nullptr, nullptr, hInst, nullptr);

    // 托盘图标
    NOTIFYICONDATAW nid = {0};
    nid.cbSize = sizeof(nid);
    nid.hWnd = g_hwnd;
    nid.uID = 1;
    nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP;
    nid.uCallbackMessage = WM_APP_TRAY;
    nid.hIcon = LoadIconW(hInst, MAKEINTRESOURCEW(1));
    if (!nid.hIcon) nid.hIcon = LoadIconW(nullptr, IDI_APPLICATION);
    wcscpy_s(nid.szTip, APP_NAME);
    if (Shell_NotifyIconW(NIM_ADD, &nid)) {
        Log(L"[GUI] 系统托盘图标已创建 (双击显示窗口)");
    } else {
        Log(L"[GUI] 系统托盘图标创建失败: err=%lu", GetLastError());
    }

    // 全局键盘钩子 (LL, 绕过普通应用层钩子/过滤器)
    g_hook = SetWindowsHookExW(WH_KEYBOARD_LL, LowLevelKeyboardProc,
                               GetModuleHandleW(nullptr), 0);
    if (g_hook) {
        Log(L"[GUI] 全局键盘钩子已安装 (WH_KEYBOARD_LL, Ctrl+Alt+Y/K/Q)");
    } else {
        Log(L"[GUI] LL 键盘钩子安装失败: err=%lu", GetLastError());
    }

    ShowWindow(g_hwnd, SW_SHOW);

    // 心跳线程
    std::thread hbThread(GuiHeartbeatThread);

    // 测试模式: 4 秒后自动完全退出
    if (testAutoQuit) {
        Log(L"[GUI] [测试模式] 4 秒后自动完全退出");
        std::thread([&] {
            Sleep(4000);
            PostMessageW(g_hwnd, WM_APP_HOTKEY, 3, 0);
        }).detach();
    }

    Log(L"[GUI] 关闭窗口 = 收缩到托盘 | 只有「完全退出」才退出");

    // 消息循环
    MSG msg;
    while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }

    // 清理
    if (g_hook) {
        UnhookWindowsHookEx(g_hook);
        g_hook = nullptr;
    }
    g_guiQuit.store(true);
    if (hbThread.joinable()) hbThread.join();
    Shell_NotifyIconW(NIM_DELETE, &nid);
    if (hMutex) CloseHandle(hMutex);
    Log(L"[GUI] 进程退出完成");
    return 0;
}

// ============================================================
// 入口
// ============================================================
int WINAPI wWinMain(HINSTANCE, HINSTANCE, LPWSTR, int) {
    g_exePath = GetExePath();
    g_logPath = g_exePath.substr(0, g_exePath.find_last_of(L'\\') + 1)
                + L"seewo_guard_cpp.log";

    bool daemon = false, testAutoQuit = false;
    int argc = 0;
    LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    for (int i = 1; argv && i < argc; i++) {
        std::wstring a = Lower(argv[i]);
        if (a == L"--daemon") daemon = true;
        if (a == L"--test-auto-quit") testAutoQuit = true;
    }
    if (argv) LocalFree(argv);

    Log(L"启动: mode=%s pid=%lu",
        daemon ? L"daemon" : L"gui", GetCurrentProcessId());

    if (daemon) return DaemonMain();
    return GuiMain(testAutoQuit);
}
