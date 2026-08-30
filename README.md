# SeewoGuard

**用于关闭希沃易启学（易课堂）学生端管控的 Windows 工具 · v4.3 · 双进程架构**

本程序的作用对象是希沃易启学学生端 `SeewoYiQiXueStudent` 的四个进程。它通过**结束进程、冻结线程、解除窗口置顶、切断网络、隔离虚拟桌面、屏蔽窗口被捕获**等方式，解除该软件对本机的管控。

> 本文内容以**代码实际行为**为准，而不是源码注释里的说法。



---

## 目录

- [作用对象](#作用对象)
- [功能清单](#功能清单)
- [界面与热键](#界面与热键)
- [权限与自恢复机制](#权限与自恢复机制)
- [安装与运行](#安装与运行)
- [打包](#打包)
- [自动预发布与手动正式发布](#自动预发布与手动正式发布)
- [版本号规则](#版本号规则)
- [已知限制与副作用](#已知限制与副作用)
- [项目结构](#项目结构)

---

## 作用对象

目标进程清单硬编码在 [`seewo_guard/config.py`](seewo_guard/config.py) 的 `TARGET_EXES`：

| # | 进程                         | 说明                  |
| - | -------------------------- | ------------------- |
| 1 | `screen-broadcast.exe`     | 屏幕广播，接收教师端画面并全屏置顶显示 |
| 2 | `classroom-protect.exe`    | 课堂保护，看护其它希沃进程       |
| 3 | `electronic-classroom.exe` | 电子教室                |
| 4 | `seewo-ecr-student.exe`    | 学生端主程序              |

四个路径都写死了版本号 `SeewoYiQiXueStudent_1.3.15.4527`。**若本机的安装路径或版本不同，必须手动改这四个路径，否则所有功能都不会命中。**

列表中第 4 项被当作主程序：`launch_main_target()` 只取 `TARGET_EXES[-1]`，因此「拉起希沃」仅重启学生端主程序。

---

## 功能清单

### 结束管控进程

| 功能           | 实现                              | 说明                                                     |
| ------------ | ------------------------------- | ------------------------------------------------------ |
| **杀死进程（单次）** | `window_ops.kill_pass`          | 对四个目标进程各执行一轮 `os.kill(pid, SIGTERM)`，连做 3 轮、每轮间隔 0.3 秒 |
| **持续杀进程**    | `gui_app._kill_loop`            | 后台线程每 0.8 秒一轮，直到手动停止                                   |
| **拉起希沃**     | `window_ops.launch_main_target` | 停止持续击杀，并重新拉起学生端主程序（已在运行则直接最大化并置顶）                      |

> Windows 的 `signal` 模块**没有** `SIGKILL`。`force_kill_process` 里第二行 `os.kill(pid, signal.SIGKILL)` 会抛 `AttributeError` 并被 `except Exception` 吞掉，**实际生效的只有第一行的 SIGTERM**（底层即 `TerminateProcess`，立即结束且无法被目标拦截）。因为 `classroom-protect.exe` 会重启其它希沃进程，所以杀进程必须周期性执行才有效。

### 冻结线程（v4.2 新增）

| 功能       | 实现                                | 说明                                                                             |
| -------- | --------------------------------- | ------------------------------------------------------------------------------ |
| **挂起线程** | `window_ops.suspend_target_threads` | 用 `CreateToolhelp32Snapshot` 遍历四个目标进程的**全部线程**，逐个 `OpenThread` + `SuspendThread` |
| **恢复线程** | `window_ops.resume_target_threads`  | 逐个 `ResumeThread`，循环减到挂起计数归零                                                    |

与「杀进程」的区别：**挂起不结束进程**。进程仍留在任务管理器里，但所有线程停止执行。对方的看护（`classroom-protect.exe`）通常按「进程是否还在」判断存活，因此挂起**不会触发重启**，比持续杀进程省得多，也不需要周期轮询。

代价是：目标窗口会变成无响应状态；退出本程序时若仍处于挂起状态，希沃会保持冻结，需要重新运行并点「▶️ 恢复线程」（退出时会打警告日志提示）。

> 挂起计数可累加，同一线程被挂起 n 次就要恢复 n 次，所以恢复统一走 `resume_target_threads` 循环减到 0，不要自行重复调用挂起。

### 解除窗口置顶

| 功能        | 实现                                             | 说明                                                                                               |
| --------- | ---------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| **取消置顶**  | `window_ops.set_zbid_and_notopmost`            | 先尝试未公开的 `SetWindowBand` 把窗口降出高 ZBID；再做一次「先设 `HWND_TOPMOST` 再设 `HWND_NOTOPMOST`」的抖动，强制窗口管理器刷新 Z 序 |
| **最小化置底** | `window_ops.minimize_target_windows_to_bottom` | 最小化并置底；GUI 用 **250 ms** 定时器反复调用，因为希沃大约每秒会把自己重新置顶一次                                               |
| **修改大小**  | `window_ops.compact_target_windows`            | 把希沃窗口缩为 360×220 并挪到所在屏幕右上角；再点一次恢复最大化                                                             |

> 「取消置顶」和「最小化置底」依赖程序的 **UIAccess** 完整性级别才能压过希沃的置顶，详见[权限与自恢复机制](#权限与自恢复机制)。

### 阻断网络

`window_ops.block_network` 用 `netsh advfirewall` 给每个目标进程加一条**出站阻止**规则，规则名为 `SeewoGuard_Block_<文件名>`；「允许网络」按同名删除。

> ⚠️ 这些防火墙规则**持久化保存在系统中**。程序被强杀或异常退出时不会自动清理，需要重新运行后点「允许网络」，或手动执行  
> `netsh advfirewall firewall delete rule name="SeewoGuard_Block_<文件名>"`。

### 防录屏

两套动作，都由 `SetWindowDisplayAffinity` 实现：

- **对希沃窗口**：设置 `WDA_EXCLUDEFROMCAPTURE`（0x11），使其在截图 / 录屏结果中不可见；失败时回退 `WDA_MONITOR`（黑色遮挡）。
- **对本程序自身窗口**：同样设置 `WDA_EXCLUDEFROMCAPTURE`，让本程序的窗口不出现在任何截图 / 录屏里。

由于希沃会自行重置这个属性，GUI 启动 **1.2 秒后自动开启**防录屏，并由后台线程每 **2 秒**复查修复一轮。

### 虚拟桌面隔离

`window_ops.VirtualDesktopManager` 通过未公开的 COM 接口 `IVirtualDesktopManagerInternal` 操作 Windows 10/11 虚拟桌面（不模拟按键）：

- **新建桌面并移动**：新建虚拟桌面，把找到的希沃窗口全部移进去，而把用户的主视角**切回原桌面**。
- **移动**：把本程序窗口移到指定桌面并切换视角过去。

由于该 COM 接口未公开，不同系统版本的 IID 与 vtable 布局不同，代码会按 Windows 内部版本号依次探测多套布局（`v26100` / `v22631` / `v22621` / `v22449` / `hwnd21313` / `hwnd20231` / `v9000`），取第一个可用的。

### 全局热键

优先使用 `WH_KEYBOARD_LL` 低级键盘钩子（在按键派发到任何线程之前拿到通知，并可通过返回 `1` 吞掉按键）；钩子注册失败时回退到 `RegisterHotKey`。

| 热键           | 行为                     |
| ------------ | ---------------------- |
| `Ctrl+Alt+Y` | 显示本程序窗口                |
| `Ctrl+Alt+K` | 杀一轮进程                  |
| `Ctrl+Alt+Q` | 停止持续杀进程 → 拉起希沃 → 退出本程序 |

> `Ctrl+Alt+Q` **不是单纯的退出**，它会先把希沃恢复起来。这一点与 `keyboard.HOTKEYS` 里原先的注释不同。

### 规避检测

- **窗口标题随机化**：维持自身置顶的同时，按 `config.TITLE_RANDOMIZE_SECONDS`（v4.2 起默认 **2 秒**，v4.1 是 10 秒）把标题换成 6–13 位随机字母数字，避免被按窗口标题枚举。界面初始显示名为 `往昔的涟漪`（`config.APP_NAME`）。
- **置顶定时器**由 `config.TOP_KEEP_INTERVAL_MS` 控制，v4.2 起为 **500 ms**（原 1000 ms），既是标题随机化的时间粒度，也保证能压住希沃每秒一次的重新置顶。
- **自身窗口防捕获**：见上文「防录屏」。
- **关闭窗口不退出**：点 X / Alt+F4 只会收缩到系统托盘，必须点「完全退出」或用 `Ctrl+Alt+Q`。

---

## 权限与自恢复机制

这是本项目最容易误解的部分，实际逻辑如下：

### 启动链（`bootstrap.py`）

```
普通进程
  ├─ 非管理员 → ShellExecuteW("runas") 请求 UAC，当前进程退出
  └─ 已是管理员 → 用 uiaccess.dll 的 StartUIAccessProcess 拉起
                  UIAccess 实例，由它运行真正的 GUI，当前进程退出
```

**为什么需要 UIAccess**：只有 UIAccess 完整性级别的进程才能在窗口 Z 序上压过同样以高完整性运行的希沃窗口。UIAccess 要求可执行文件位于受信任目录（如 `Program Files`），拉不起来时会跳过并直接用管理员进程运行 GUI——功能降级但不报错。

### 双进程自恢复

|     | 进程           | 职责                       |
| --- | ------------ | ------------------------ |
| GUI | `gui_app.py` | 界面、托盘、热键、各项功能            |
| 常驻  | `daemon.py`  | 无界面，监控 GUI 心跳并在其被结束后重新拉起 |

- GUI 每 2 秒发一次心跳（`gui_alive`），常驻进程 4 秒收不到就重新拉起 GUI，**最多连续 5 次**，超过则自行退出以避免无限重启。
- 这才是「在任务管理器里杀不掉」的真正原因。

### 退出顺序（v4.2 修复）

点「完全退出」时，GUI **不会立刻结束自己**，而是：

1. 先经 IPC 发出 `shutdown` 指令；
2. 调用 `_wait_daemon_exit()` 轮询等待常驻进程真正消失（上限 3 秒）；
3. 仍未退出则 `TerminateProcess` 强杀，并再确认最多 1 秒（`TerminateProcess` 是异步的，发出请求后进程不会瞬间消失）；
4. 确认常驻进程已不在后，GUI 才退出。

顺序不能颠倒。单文件打包（PyInstaller onefile）时，本进程退出后 bootloader 会删除自己的 `_MEI` 临时目录；如果此时由它拉起的子进程仍活着并占用同一目录，删除就会失败，窗口程序随即弹出：

```
Failed to remove temporary directory: C:\Users\<用户名>\AppData\Local\Temp\_MEIxxxxxx
```

根因实测（PyInstaller 6.20）：子进程若继承了 `_PYI_APPLICATION_HOME_DIR` 等打包器环境变量，会被判定为同一进程树，**直接复用父进程已解压好的 `_MEI` 目录而不再自己解压**——父进程先退，目录就被抢先删掉。

因此 `utils._STRIP_ENV_PREFIXES` 在拉起本程序自身（常驻进程 / GUI / 提权后的新实例）时一律清掉 `_PYI_*`、`_MEIPASS2`、`NUITKA_*`；v4.2 另外补了上述「等子进程先走」的兜底。

### 关于「进程保护」

`protection.py` 的四层（高优先级 / 特权提升 / DEP·ASLR·严格句柄·CFG / PPL 尝试）**都不能阻止任务管理器或 `taskkill` 结束进程**：

- `SeDebugPrivilege` 等特权的实际用途是让本进程有权 `TerminateProcess` 掉希沃进程，属于进攻侧，不提供自我保护。
- DEP / ASLR / CFG 是漏洞缓解，与终止无关。
- PPL 是唯一能阻止第三方结束进程的机制，但要求二进制带微软签名，**实际几乎总是失败**（代码已按「失败不影响运行」处理）。

### 日志与状态文件位置

v4.2 起，日志与状态文件**不再写在程序所在目录**，统一放到：

```
%LOCALAPPDATA%\SeewoGuard\
├── seewo_guard_gui.log       # GUI 日志（滚动 5MB × 5）
├── seewo_guard_daemon.log    # 常驻进程日志
└── .seewo_guard_state.json   # 运行状态（置底开关、PID 等）
```

拿不到 `LOCALAPPDATA` 时回退到系统临时目录下的 `SeewoGuard\`。

之所以必须迁走：UIAccess 要求可执行文件位于 `Program Files` 等**受信任目录**，而那些目录对标准用户通常**不可写**，把日志写在 exe 旁边会直接写入失败。

### 关机安全退出

常驻进程创建隐藏窗口监听 `WM_QUERYENDSESSION` / `WM_ENDSESSION`（窗口创建失败时回退为控制台 Ctrl 处理器），收到后清理状态文件与互斥锁再退出。

### 启动阶段划分（v4.2 优化）

首帧只构建 UI，其余按「越慢越靠后」错开，让窗口尽快可见可点：

| 时间      | 动作                              |
| ------- | ------------------------------- |
| 0 ms    | 构建界面、连接信号、注册热键、建托盘              |
| 120 ms  | 向常驻进程报到（`gui_hello`，先报到免得被误判为已死） |
| 300 ms  | 自身加固（优先级 / 特权 / 缓解 / PPL）+ 自身窗口防捕获 |
| 500 ms  | 回放最近的日志                         |
| 900 ms  | 虚拟桌面 COM 探测（最慢，且只有点「移动 / 新建桌面」才用得到） |
| 1200 ms | 自动开启防录屏                         |

虚拟桌面即使尚未初始化完也可以点——`VirtualDesktopManager` 是惰性初始化，用到时才探测。

> 本项目**不含**关键进程（`RtlSetProcessIsCritical`）相关代码，不会设置关键进程标记，不存在蓝屏风险。

### 进程间通信

不是命名管道，而是**本地回环 TCP**：绑定 `127.0.0.1`，端口为 `49000 + 会话ID % 1000`，每条连接传输一行 JSON 请求 / 响应。支持的指令：`status`、`gui_hello`、`gui_alive`、`set_target_bottom`、`shutdown`。

---

## 安装与运行

### 环境要求

- **Windows 10 / 11**（功能依赖 `user32` / `kernel32` / `ntdll` / `advapi32`，只在 Windows 上可用）
- Python 3.8+
- **管理员权限**（首次运行会请求 UAC，这是正常行为）

### 从源码运行

```bash
pip install -r requirements.txt
python main.py
```

### 命令行参数

| 参数                 | 说明                    |
| ------------------ | --------------------- |
| （无）                | 启动 GUI，并自动拉起常驻进程      |
| `--daemon`         | 仅以常驻进程模式运行（无界面）       |
| `--gui`            | 仅启动界面进程，不主动拉起常驻进程     |
| `--auto-quit-ms=N` | GUI 启动 N 毫秒后自动退出（调试用） |

### 环境变量

| 变量                           | 说明                                   |
| ---------------------------- | ------------------------------------ |
| `SEEWO_GUARD_TEST=1`         | 测试模式：跳过 UAC / UIAccess 启动链，当前进程即 GUI |
| `SEEWO_GUARD_AUTO_QUIT_MS=N` | 同 `--auto-quit-ms=N`                 |

---

## 打包

打包由根目录的 [`build.py`](build.py) 统一负责，同时支持 **PyInstaller**（默认）与 **Nuitka**，通过 `--tool` 选择。脚本全部使用相对路径，不依赖任何绝对路径或机器环境。

### 1. 准备

```bash
pip install -r requirements.txt
pip install pyinstaller nuitka       # 两个都装，或只装需要的那个
```

### 2. 执行

```bash
python build.py                      # PyInstaller 单文件（默认）
python build.py --tool nuitka        # Nuitka 单文件
python build.py --onedir             # 单目录模式
python build.py --onedir --tool nuitka
python build.py --clean              # 彻底清理 build/ 与 dist/ 后重建
```

**产物命名**：`<前缀>_<打包工具>`，即 PyInstaller 产出 `seewokiller_pyinstaller.exe`、Nuitka 产出 `seewokiller_nuitka.exe`（前缀由 `build.py` 顶部 `EXE_BASE_NAME` 配置）。

### 3. 全部参数

| 参数                 | 默认            | 说明                            |
| ------------------ | ------------- | ----------------------------- |
| `--tool`           | `pyinstaller` | 打包工具：`pyinstaller` / `nuitka` |
| `--onefile`        | ✅             | 打包为单个可执行文件                    |
| `--onedir`         |               | 打包为一个目录（启动更快）                 |
| `--console`        | 关（隐藏控制台）      | 保留控制台窗口，便于调试                  |
| `--clean`          | 关             | 彻底清理 `build/` 与 `dist/` 后重建（默认增量更新） |
| `--skip-dep-check` | 关             | 跳过 `requirements.txt` 依赖校验    |
| `--version`        |               | 仅打印版本号后退出                     |

### 4. 执行流程

配置校验 → （仅 `--clean` 时清理 `build/`、`dist/`）→ 校验 `requirements.txt` 依赖 → 检查打包工具 → 自动收集隐藏导入与数据文件 → 打包 → 输出产物路径与大小。

**默认增量更新**：保留 `build/` 中的中间产物，只覆盖 `dist/` 里的最终产物，重复构建更快；需要从零重建时加 `--clean`。配置校验在清理**之前**，配置写错时不会误删历史产物。

### 5. 退出码

| 码   | 含义      |   | 码     | 含义         |
| --- | ------- | - | ----- | ---------- |
| `0` | 成功      |   | `3`   | 打包工具未安装    |
| `1` | 打包失败    |   | `4`   | 构建结束但找不到产物 |
| `2` | 配置或依赖错误 |   | `130` | 被用户中断      |

错误信息为中文，并附带可执行的修复建议。

---

## 自动预发布与手动正式发布

### 预发布 [`pre-release.yml`](.github/workflows/pre-release.yml)

| 触发方式      | 说明                                            |
| --------- | --------------------------------------------- |
| push 到主分支 | `main` / `master` 有新提交时自动触发                   |
| 每周日定时     | cron `0 3 * * 0`（**UTC 03:00**，即北京时间周日 11:00） |
| 手动        | Actions → `Pre-Release` → `Run workflow`      |

流程：生成 `日期 + SHA` 版本号 → **Windows 上并行构建两个 exe**（PyInstaller 版 + Nuitka 版）→ exe 直接作为资产上传 → 创建标记为 **prerelease** 的 Release。创建前会自动删除历史预发布（手动触发时可用 `prune_old` 关闭）。

### 正式发布 [`release.yml`](.github/workflows/release.yml)

**仅支持手动触发**。Actions → `Release` → `Run workflow`，填写 `version`（SemVer，如 `1.2.0`）后运行。

流程：校验版本号（去 `v` 前缀 → 校验 SemVer → 确认标签与 Release 均未被占用，**失败立即中止**）→ Windows 上并行构建两个 exe → exe 直接作为资产上传 → 创建标记为 **latest** 的 Release。

### 共同配置

- `permissions: contents: write`
- `concurrency` 并发策略：预发布 `cancel-in-progress: true`；正式发布设为 `false`，避免发布被打断
- `actions/setup-python` 的 **pip 缓存**，缓存键为 `requirements.txt`

| 打包工具        | Runner           | 产物（直接作为 Release 资产，不打压缩包） |
| ----------- | ---------------- | ------------------------------- |
| PyInstaller | `windows-latest` | `seewokiller_pyinstaller.exe`   |
| Nuitka      | `windows-latest` | `seewokiller_nuitka.exe`        |

两个 exe 功能相同，任选其一；下载后无需解压，直接以管理员身份运行。

---

## 版本号规则

| 类型    | 格式                                 | 示例                     | 标记         |
| ----- | ---------------------------------- | ---------------------- | ---------- |
| 程序内版本 | `__init__.py` 的 `__version__`，手动维护 | `4.2`                  | —          |
| 预发布   | `v<YYYY>.<MM>.<DD>-<7位SHA>`        | `v2026.08.30-a1b2c3d`  | prerelease |
| 正式版   | `v<MAJOR>.<MINOR>.<PATCH>[-标识]`    | `v1.2.0`、`v2.0.0-rc.1` | latest     |

预发布标签冲突时（定时重复构建同一 commit）追加 GitHub 运行序号，如 `v2026.08.30-a1b2c3d-42`。两类格式互斥（日期开头 vs 纯数字开头），不会互相覆盖。

程序内版本同时要改两处，务必保持一致：`seewo_guard/__init__.py` 的 `__version__` 与 `seewo_guard/config.py` 的 `VERSION`（`build.py` 读取的是前者）。

### v4.3 相对 v4.2 的变更

| 项           | v4.2                      | v4.3                                        |
| ---------- | ------------------------- | ------------------------------------------- |
| 启动期 IPC 告警 | 守护进程就绪前每 2 秒刷 `10061 拒绝连接` | 客户端失败日志节流（首条 + 每 30 秒一条），宽限期内仅提示「等待就绪」 |
| 成功日志级别     | 挂起线程 / 防录屏成功记为 WARNING    | 全部改为 INFO；仅「有窗口但透明模式不可用」保留 WARNING          |
| 防录屏首报      | 0 窗口时报「黑色兜底」易误读          | 0 窗口视为启动期正常现象，提示将每 2 秒自动复查                  |
| 守护拉起重试     | 每 5 秒                     | 每 10 秒，且启动宽限期（20 秒）内不触发托盘告警                 |
| 打包产物命名     | `SeewoGuard.exe`          | `seewokiller_pyinstaller.exe` / `seewokiller_nuitka.exe` |
| 构建清理策略     | 每次全量清理 `build/` `dist/`   | 默认增量更新，`--clean` 才彻底清理                      |
| Nuitka 兼容  | `--output-dir` 空格传参报错     | 全部带值参数改为 `=` 连写，实测 Nuitka 4.0.8 通过           |

### v4.2 相对 v4.1 的变更

| 项        | v4.1           | v4.2                                        |
| -------- | -------------- | ------------------------------------------- |
| 线程冻结     | 无              | 新增「⏸️ 挂起线程 / ▶️ 恢复线程」按钮                     |
| 日志与状态    | 写在 exe 所在目录    | 迁到 `%LOCALAPPDATA%\SeewoGuard\`             |
| 退出清理     | 弹 `Failed to remove temporary directory` | 等常驻进程先退出（含强制结束兜底）              |
| 窗口尺寸     | 640×520        | 576×468（缩小 10%）                             |
| 标题随机化    | 约 10 秒一次       | 2 秒一次（按时间判定），置顶定时器 1000→500 ms             |
| 启动阶段     | 集中在 100 ms 一处  | 分 5 档错开（120 / 300 / 500 / 900 / 1200 ms）    |
| 按钮       | 4 列，部分无 emoji  | 3×3 排布，全部带 emoji                            |
| 打包环境变量清理 | 仅 `_PYI_`、`NUITKA_` | 补 `_MEIPASS2`（PyInstaller 5.x）         |

---

## 已知限制与副作用

1. **目标路径写死** —— `TARGET_EXES` 含具体版本号 `1.3.15.4527`，版本或安装路径变化时必须手动更新，否则功能全部落空。
2. **防火墙规则会残留** —— 「禁止网络」写入的是系统持久规则，程序异常退出不会清理，需手动删除或重新运行后点「允许网络」。
3. **`_netsh` 不检查返回码** —— `block_network` 返回的计数是「命令已下发的条数」，不代表规则真的生效（例如非管理员或防火墙服务被禁用时会静默失败）。
4. **「取消置顶」只处理第一个窗口** —— `set_zbid_and_notopmost` 取的是该 exe 找到的第一个句柄，不是全部窗口。
5. **挂起状态会跨退出保留** —— 点「完全退出」时若线程仍被挂起，希沃会保持冻结；不会自动恢复，需重新运行点「▶️ 恢复线程」（退出时会打警告日志）。
6. **挂起需要管理员权限** —— `OpenThread` 对高完整性进程要求 `SeDebugPrivilege`，未提权时挂起计数为 0，按钮不会切换状态。
7. **PPL 基本不可用** —— 需要微软签名，实际总是失败；这不是 bug。
8. **UIAccess 需要受信任目录** —— 可执行文件需放在 `Program Files` 等位置才能拿到 UIAccess，否则「最小化置底」可能压不住希沃的置顶。
9. **Windows-only** —— 功能依赖 Win32 API，CI 只在 `windows-latest` 上构建。
10. **`GUI_HEARTBEAT` 未被使用** —— `config.py` 里定义了该常量，但真实心跳间隔由 `gui_app` 中的 `ipc_timer.start(2000)` 写死，改这个常量没有效果。
11. **命名痕迹不统一** —— 界面显示名为 `往昔的涟漪`，但互斥锁（`SeewoGuard_GUI_v4`）、IPC、日志文件名、防火墙规则名都含 `SeewoGuard` 字样。
12. **强杀路径仍可能残留** —— 若常驻进程被第三方安全软件保护而无法结束，v4.2 会打印 `❌ 常驻进程 ... 强制结束后依然存在`，此时打包版仍可能提示临时目录无法删除，需手动结束该 PID。

---

## 项目结构

```
.
├── main.py                          # 入口（GUI / 常驻 双模式）
├── build.py                         # 统一构建脚本（PyInstaller + Nuitka）
├── requirements.txt                 # 运行时依赖：PySide6、psutil
├── icon.ico                         # 图标
├── uiaccess.dll                     # UIAccess 提权辅助库
├── seewo_guard/
│   ├── bootstrap.py                 # 启动链：UAC 提权 → UIAccess 实例
│   ├── config.py                    # 目标进程清单、路径、各项参数
│   ├── gui_app.py                   # 界面 / 托盘 / 热键 / 功能按钮
│   ├── daemon.py                    # 常驻进程：拉起 GUI、关机退出、IPC 服务端
│   ├── ipc.py                       # 本地回环 TCP 通信（非命名管道）
│   ├── window_ops.py                # 杀进程、窗口操作、防火墙、虚拟桌面
│   ├── win_api.py                   # Win32 API 声明
│   ├── protection.py                # 进程加固与提权（不是防结束）
│   ├── keyboard.py                  # WH_KEYBOARD_LL 全局热键钩子
│   ├── logging_system.py            # 滚动文件日志 + GUI 日志框
│   └── utils.py                     # 单实例锁、隐藏启动、环境变量清理等
└── .github/workflows/
    ├── pre-release.yml              # 自动预发布
    └── release.yml                  # 手动正式发布
```

---

## 使用声明

本工具涉及终止第三方软件进程、修改系统防火墙规则等操作，请**仅在你拥有管理权限的设备上使用**，并自行承担由此产生的后果。
