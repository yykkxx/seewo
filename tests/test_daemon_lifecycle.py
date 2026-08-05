# -*- coding: utf-8 -*-
"""守护进程生命周期测试: 启动 -> IPC status/gui_hello -> 安全退出 (无关键进程)"""
import os, sys, time, subprocess

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

CODEX = r"C:\yykkxx\code\seewo\codex"
sys.path.insert(0, CODEX)
os.chdir(CODEX)

for f in ("seewo_guard_daemon.log",):
    try:
        os.remove(os.path.join(CODEX, f))
    except OSError:
        pass

proc = subprocess.Popen(
    [sys.executable, "main.py", "--daemon"],
    cwd=CODEX, creationflags=0x08000000,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print("DAEMON PID:", proc.pid)
time.sleep(3)

from seewo_guard.ipc import IpcClient
c = IpcClient()
print("STATUS:", c.request({"cmd": "status"}))
print("GUI_HELLO:", c.request({"cmd": "gui_hello", "pid": 12345}))
print("STATUS2:", c.request({"cmd": "status"}))
print("SHUTDOWN:", c.request({"cmd": "shutdown", "pid": 12345}))

time.sleep(2)
rc = proc.poll()
print("DAEMON EXIT CODE:", rc)
if rc is None:
    print("DAEMON STILL ALIVE (BAD)")
    proc.terminate(); time.sleep(1)
    if proc.poll() is None:
        proc.kill()
else:
    print("DAEMON EXITED CLEANLY (GOOD)")

log = os.path.join(CODEX, "seewo_guard_daemon.log")
if os.path.exists(log):
    print("----- daemon log tail -----")
    with open(log, "r", encoding="utf-8") as f:
        print("".join(f.readlines()[-16:]))
