# SeewoGuard v4.0 - 双进程模块化版

希沃守护工具重构版: **GUI 进程与守护进程彻底分离 (两个独立进程)**。

> 安全说明: 本项目**已彻底移除关键进程 (RtlSetProcessIsCritical) 代码**,
> 任何情况下都不会设置关键进程标记, 不存在蓝屏风险。

## 架构

```
┌────────────────────────────┐   本地回环TCP    ┌────────────────────────────┐
│        GUI 进程            │  ──────────────>  │        守护进程            │
│  (窗口/托盘/热键/功能按钮)  │  心跳/状态/退出    │  (无界面, 常驻后台)        │
│                            │ <──────────────   │                            │
│  * 被任务管理器结束         │      响应/状态     │  * 独立于 GUI 运行         │
│    不影响守护进程           │                   │  * 监听系统关机自动退出    │
│  * X = 收缩到托盘           │                   │  * 监控 GUI 心跳, 被杀自动 │
│  * 只有「完全退出」才退出   │                   │    重启 GUI (最多5次)      │
└────────────────────────────┘                   └────────────────────────────┘
```

- 两个进程互相独立: 任务管理器结束 GUI 窗口 -> 守护进程不受影响, 并自动重新拉起 GUI。
- 守护进程监听 `WM_QUERYENDSESSION / WM_ENDSESSION` (隐藏窗口消息循环),
  系统关机/注销时自动安全退出, 不残留后台进程。
- 守护进程掉线时, GUI 也会自动重新拉起守护进程 (双向看护)。

## 目录结构

```
codex/
├── main.py                    # 入口 (默认GUI / --daemon / --gui)
├── build.py                   # 打包脚本 (PyInstaller / Nuitka)
├── requirements.txt
└── seewo_guard/
    ├── config.py              # 全局配置 (路径/互斥锁/IPC/目标EXE)
    ├── win_api.py             # Windows API 声明 (ctypes)
    ├── logging_system.py      # 日志系统 + GUI彩色日志框
    ├── utils.py               # 提权/单实例/隐藏启动/窗口激活
    ├── protection.py          # 进程保护 (优先级/特权/缓解/PPL)
    ├── ipc.py                 # 本地回环 TCP IPC (服务端/客户端)
    ├── daemon.py              # 守护进程 (关机监听/GUI看护)
    ├── gui_app.py             # GUI 进程 (托盘/热键/功能)
    └── window_ops.py          # 窗口/杀进程/防录屏/禁网/虚拟桌面
```

## 运行

```powershell
cd C:\yykkxx\code\seewo\codex
pip install -r requirements.txt

# 正常运行 (自动拉起守护进程)
python main.py

# 仅守护进程 / 仅界面
python main.py --daemon
python main.py --gui
```

首次运行会请求管理员权限 (UAC)。两个进程都以管理员运行。
非管理员环境可自动降级 (会话级互斥锁), 仅用于测试。

## 打包

```powershell
# PyInstaller
python build.py pyinstaller

# Nuitka
python build.py nuitka
```

等价手工命令 (需在项目目录下执行):

```powershell
# PyInstaller (需已安装: pip install pyinstaller)
pyinstaller --noconfirm --onefile --noconsole --uac-admin --name SeewoGuard ^
  --hidden-import=psutil --collect-all PySide6 ^
  --add-data "icon.ico;." --add-binary "uiaccess.dll;." main.py

# Nuitka (需已安装: pip install nuitka)
python -m nuitka --onefile --windows-disable-console --windows-uac-admin ^
  --enable-plugin=pyside6 --include-package=psutil ^
  --include-data-file="icon.ico=icon.ico" --include-data-file="uiaccess.dll=uiaccess.dll" ^
  --output-dir=dist --output-filename=SeewoGuard.exe main.py
```

打包产物: `dist\SeewoGuard.exe` (同时包含 GUI 与守护进程, 通过 `--daemon` 参数切换)。

## 托盘与退出规则

- 点击窗口 X / Alt+F4 / 任务栏关闭 = **收缩到托盘**, 程序继续守护。
- 双击托盘图标 = 显示窗口。
- 只有点击「完全退出」或托盘菜单「完全退出」才真正退出 (同时关闭守护进程)。

## 虚拟桌面 (切换屏幕, 纯 COM)

- 全部操作走 Windows COM 接口, **不模拟按键** (不再有 Win+Ctrl+D 热键延迟):
  - 新建/切换/删除桌面: `IVirtualDesktopManagerInternal`
    (通过 `CLSID_ImmersiveShell -> IServiceProvider.QueryService` 获取)
  - 移动窗口: `IApplicationViewCollection.GetViewForHwnd` + `MoveViewToDesktop`
    (公开接口 `MoveWindowToDesktop` 常返回拒绝访问, 故不采用)
- 「新建桌面并移动」: 创建新桌面, 把希沃目标程序的全部窗口移入新桌面,
  然后**主视角自动切回当前桌面** (目标窗口被藏到新桌面, 不影响当前操作)。
- 「移动」: 把当前窗口移动到所选桌面, 并切换到该桌面视角。
- 兼容 Win10 1809~22H2 与 Win11 (含 24H2): 启动时逐一探测内部接口 IID,
  匹配对应 vtable 布局 (参考 pyvda, 纯 ctypes 实现, 无第三方依赖)。
- 提示: 目标窗口移动后可到新桌面找回; 托盘图标与守护进程不受影响。

## 守护进程拉起速度

- GUI 心跳丢失后守护进程**立即重启 GUI** (不再等待长宽限期)。
- 守护进程启动后 6 秒内等待 GUI 注册, 未注册则主动拉起; 心跳超时阈值 4 秒。

## 测试模式 (开发用)

```powershell
$env:SEEWO_GUARD_TEST = "1"          # 跳过提权/UIAccess 链
$env:SEEWO_GUARD_AUTO_QUIT_MS = "4000"  # GUI 启动4秒后自动退出 (便于自动化测试)
python main.py
```

## 性能优化

- 窗口查找: 每轮只 EnumWindows 一次, 按 PID 分组匹配, 不再为每个窗口打开句柄
- psutil 进程扫描按路径缓存 (2秒 TTL)
- 置顶保持 1s/次, 标题随机化 10s/次; 防录屏轮询 2s/轮
