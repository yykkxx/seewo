# SeewoGuard v4.1 - 双进程模块化版

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
seewo/
├── main.py                    # 入口 (默认GUI / --daemon / --gui)
├── build.py                   # 打包脚本 (PyInstaller / Nuitka)
├── requirements.txt
├── tests/                     # 单元/集成测试 (unittest)
└── seewo_guard/
    ├── config.py              # 全局配置 (路径/互斥锁/IPC/目标EXE)
    ├── win_api.py             # Windows API 声明 (ctypes)
    ├── logging_system.py      # 日志系统 + GUI彩色日志框
    ├── utils.py               # 提权/单实例/隐藏启动/窗口激活
    ├── protection.py          # 进程保护 (优先级/特权/缓解/PPL)
    ├── ipc.py                 # 本地回环 TCP IPC (服务端/客户端)
    ├── daemon.py              # 守护进程 (关机监听/GUI看护)
    ├── gui_app.py             # GUI 进程 (托盘/热键/功能)
    ├── keyboard.py            # 全局键盘监听 (WH_KEYBOARD_LL 钩子)
    └── window_ops.py          # 窗口/杀进程/防录屏/禁网/虚拟桌面
```

## 运行

```powershell
cd C:\path\to\seewo
pip install -r requirements.txt

# 正常运行 (自动拉起守护进程)
python main.py

# 仅守护进程 / 仅界面
python main.py --daemon
python main.py --gui
```

首次运行会请求管理员权限 (UAC)。两个进程都以管理员运行。
非管理员环境可自动降级 (会话级互斥锁), 仅用于测试。

## 配置解耦 (环境变量/配置文件)

- 可选配置文件: `seewo_guard_config.json` (默认放在程序目录，也可用 `SEEWO_GUARD_CONFIG` 指定)
- 支持覆盖项:
  - `version` / `SEEWO_GUARD_VERSION`
  - `target_exes` / `SEEWO_GUARD_TARGET_EXES` (推荐 JSON 数组或按行分隔字符串)
  - `ipc_auth_token` / `SEEWO_GUARD_IPC_AUTH_TOKEN`
  - `ipc_max_connections` / `SEEWO_GUARD_IPC_MAX_CONNECTIONS`
  - `max_gui_restarts` / `SEEWO_GUARD_MAX_GUI_RESTARTS`
  - `daemon_gui_timeout` / `SEEWO_GUARD_DAEMON_GUI_TIMEOUT`
  - `daemon_start_grace` / `SEEWO_GUARD_DAEMON_START_GRACE`
  - `gui_heartbeat` / `SEEWO_GUARD_GUI_HEARTBEAT`
  - `daemon_tick` / `SEEWO_GUARD_DAEMON_TICK`

示例:

```json
{
  "version": "4.1",
  "target_exes": [
    "C:\\Program Files (x86)\\Seewo\\...\\screen-broadcast.exe"
  ],
  "ipc_auth_token": "change-me",
  "ipc_max_connections": 32
}
```

## 打包

```powershell
# PyInstaller -> dist\SeewoGuard.exe
python build.py pyinstaller

# Nuitka -> dist\SeewoGuard.exe
python build.py nuitka

# 指定产物名 (两个版本可并存, 如:)
$env:SEEWO_OUTPUT = "SeewoGuard_nuitka"
python build.py nuitka        # -> dist\SeewoGuard_nuitka.exe
```

等价手工命令 (需在项目目录下执行):

```powershell
# PyInstaller (需已安装: pip install pyinstaller)
pyinstaller --noconfirm --onefile --noconsole --uac-admin --name SeewoGuard ^
  --hidden-import=psutil --collect-all PySide6 ^
  --icon icon.ico ^
  --add-data "icon.ico;." --add-binary "uiaccess.dll;." main.py

# Nuitka 4.x (需已安装: pip install nuitka; 带值选项须用 "=" 形式)
python -m nuitka --onefile --windows-disable-console --windows-uac-admin ^
  --enable-plugin=pyside6 --include-package=psutil ^
  --windows-icon-from-ico=icon.ico ^
  --include-data-file="icon.ico=icon.ico" --include-data-file="uiaccess.dll=uiaccess.dll" ^
  --output-dir=dist --output-filename=SeewoGuard.exe main.py
```

> 图标: 两个打包器均通过 `--icon` / `--windows-icon-from-ico` 把 `icon.ico`
> 嵌入 exe (资源图标, 非运行时替换)。

打包产物同时包含 GUI 与守护进程, 通过 `--daemon` 参数切换。
已实测两种打包器产物均可运行 (守护模式 IPC/退出 + GUI 自动退出均通过):

| 打包器 | 产物 | 体积 | 说明 |
| --- | --- | --- | --- |
| PyInstaller | `dist\SeewoGuard.exe` | ~43 MB | 仅打包实际导入的 Qt 模块 |
| Nuitka | `dist\SeewoGuard_nuitka.exe` | ~69 MB | 启动快, 体积小 |

> Nuitka 兼容说明: Nuitka 不设置 `sys.frozen`, 代码用 `__compiled__`
> 识别编译产物; onefile 下 `sys.executable` 指向临时解压目录,
> 程序真实路径取自 `sys.argv[0]` (日志/状态文件仍写在 exe 目录)。

## 托盘与退出规则

- 点击窗口 X / Alt+F4 / 任务栏关闭 = **收缩到托盘**, 程序继续守护。
- 双击托盘图标 = 显示窗口。
- 只有点击「完全退出」或托盘菜单「完全退出」才真正退出 (同时关闭守护进程)。
- 完全退出**无确认弹窗**, 直接关闭 GUI 并通知守护进程一并退出。

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

## 杀进程方式

- Python 版统一使用 `os.kill(pid, signal.SIGTERM)` 后立即
  `os.kill(pid, signal.SIGKILL)` (两个调用无间隔), 不再使用 psutil/taskkill。
- C++ 版使用原生 `TerminateProcess` (等价于 SIGKILL)。
- 按完整 exe 路径 (不区分大小写) 匹配进程, 不会误杀同名程序。

## 键盘监听 (增强)

- 使用 `WH_KEYBOARD_LL` 低级键盘钩子挂到系统钩子链末端, 可绕过普通软件的
  应用层键盘钩子与键盘过滤器, 命中热键时直接吞掉按键 (其他软件收不到)。
- 热键: `Ctrl+Alt+Y` 显示窗口 / `Ctrl+Alt+K` 杀进程 /
  `Ctrl+Alt+Q` 停止持续杀进程、确保希沃运行后退出。
- 钩子安装失败时自动回退 `RegisterHotKey`。
- 驱动级键盘过滤驱动无法通过钩子绕过 (需内核驱动, 不在本版本范围)。

## 希沃窗口控制

- 「拉起希沃」: 先停止持续杀进程，再确保主希沃进程运行；已运行时直接最大化置顶。
- 「修改大小」: 把可见希沃窗口缩小到所在屏幕右上角，按钮随后切换为「最大化」。
- 「最小化置底」: 使用 UIAccess 每 250ms 保持希沃窗口最小化并置底，
  压过目标程序每秒一次的置顶动作；按钮随后切换为「最大化置顶」。
- 置底模式由守护进程持久化；GUI 被任务管理器结束并重新拉起后会自动恢复。
- 不注入目标进程，也不挂起目标线程，前台 GUI 被强制结束时不会遗留冻结线程。

## 启动优化

- 管理员/UIAccess 检查在导入 PySide6 前完成，权限跳转进程不再重复加载 Qt。
- 进程保护和历史日志延迟到首帧显示后执行，窗口更早可见。
- 持续杀进程与防录屏循环使用可中断等待，停止和退出无需等待固定休眠结束。
- Nuitka 单文件使用版本化持久解包缓存，UIAccess 子进程和后续启动可直接复用。

## 守护进程拉起速度

- GUI 心跳丢失后守护进程**立即重启 GUI** (不再等待长宽限期)。
- 守护进程启动后 6 秒内等待 GUI 注册, 未注册则主动拉起; 心跳超时阈值 4 秒。

## GUI 日志框

- 主界面内置彩色日志框 (限 5000 行): 启动时回显日志文件末尾 300 行历史,
  之后实时滚动显示全部日志。
- 日志文件: `seewo_guard_gui.log` / `seewo_guard_daemon.log` (exe 目录)。

## 启动加速

- 守护进程在后台拉起, 窗口**立即显示**, 不再阻塞等待守护就绪
  (未就绪时由心跳轮询自动兜底)。
- 虚拟桌面 COM 探测 / 进程保护 / 自身防录屏延迟到窗口显示后执行。
- PyInstaller 不再 `--collect-all PySide6`, 只打包实际导入的
  QtCore/QtGui/QtWidgets 模块 (体积 240MB -> 43MB, 解压启动更快)。
- 实测: GUI 启动耗时 PyInstaller 版 ~0.4s / Nuitka 版 ~0.3s。

## 测试模式 (开发用)

```powershell
$env:SEEWO_GUARD_TEST = "1"          # 跳过提权/UIAccess 链
$env:SEEWO_GUARD_AUTO_QUIT_MS = "4000"  # GUI 启动4秒后自动退出 (便于自动化测试)
python main.py
```

## 测试

```powershell
# 单元/集成测试入口 (默认仅执行可在当前环境运行的测试)
python -m unittest discover -s tests -p "test_*.py"

# Windows 上执行集成测试 (会真实拉起 GUI/守护进程)
$env:SEEWO_GUARD_RUN_INTEGRATION = "1"
python -m unittest discover -s tests -p "test_*.py"
```

## 性能优化

- 窗口查找: 每轮只 EnumWindows 一次, 按 PID 分组匹配, 不再为每个窗口打开句柄
- psutil 进程扫描按路径缓存 (2秒 TTL)
- 置顶保持 1s/次, 标题随机化 10s/次; 防录屏轮询 2s/轮
