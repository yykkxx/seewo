# -*- coding: utf-8 -*-
"""双进程测试: GUI 自动拉起守护进程 -> 心跳 -> 自动安全退出"""
import os, sys, time, subprocess

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

CODEX = r"C:\yykkxx\code\seewo\codex"
os.chdir(CODEX)

for f in ("seewo_guard_daemon.log", "seewo_guard_gui.log"):
    try:
        os.remove(os.path.join(CODEX, f))
    except OSError:
        pass

env = dict(os.environ)
env["SEEWO_GUARD_TEST"] = "1"
env["SEEWO_GUARD_AUTO_QUIT_MS"] = "4000"

gui = subprocess.Popen(
    [sys.executable, "main.py"],
    cwd=CODEX, env=env, creationflags=0x08000000,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print("GUI PID:", gui.pid)

# 等待 GUI 与守护进程都退出 (最多 30s)
t0 = time.time()
while time.time() - t0 < 30:
    if gui.poll() is not None:
        break
    time.sleep(0.5)
print("GUI EXIT CODE:", gui.poll())

# 查找守护进程是否还在
import psutil
daemons = []
for p in psutil.process_iter(["pid", "cmdline"]):
    try:
        cl = " ".join(p.info["cmdline"] or [])
        if "main.py" in cl and "--daemon" in cl:
            daemons.append(p.info["pid"])
    except Exception:
        pass
print("REMAINING DAEMON PIDS:", daemons)
for pid in daemons:
    try:
        from seewo_guard.ipc import IpcClient
        print(f"shutdown daemon {pid}:", IpcClient().request({"cmd": "shutdown"}))
        time.sleep(1)
    except Exception as e:
        print("shutdown err:", e)

for f in ("seewo_guard_daemon.log", "seewo_guard_gui.log"):
    p = os.path.join(CODEX, f)
    if os.path.exists(p):
        print(f"----- {f} tail -----")
        with open(p, "r", encoding="utf-8") as fh:
            print("".join(fh.readlines()[-14:]))
