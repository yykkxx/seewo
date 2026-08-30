# -*- coding: utf-8 -*-
"""
ipc.py - 本地回环 TCP IPC (GUI <-> 守护进程)

协议: 每请求一行 JSON (UTF-8, 以换行结尾), 响应同样为一行 JSON。
客户端每次请求新建连接; 服务端每连接处理一条请求。
绑定 127.0.0.1 + 会话相关端口, 不暴露到局域网。
"""
import json
import logging
import socket
import threading
import time

from seewo_guard.config import IPC_PORT_BASE
from seewo_guard.utils import get_session_id

BUF_SIZE = 65536


def ipc_address():
    """监听地址: 仅回环 + 按会话错开的端口"""
    port = IPC_PORT_BASE + (get_session_id() % 1000)
    return ("127.0.0.1", port)


# ==========================================
# 服务端 (守护进程)
# ==========================================
class IpcServer:
    """TCP 服务端: 每连接一个线程, 处理一行 JSON 请求"""

    def __init__(self, handler, address=None):
        self._handler = handler
        self._address = address or ipc_address()
        self._sock = None
        self._stop = threading.Event()

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(self._address)
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        t = threading.Thread(target=self._accept_loop, daemon=True)
        t.start()
        logging.info(f"🔄 IPC 服务已启动 {self._address}")

    def stop(self):
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            t.start()

    def _serve(self, conn):
        try:
            conn.settimeout(5.0)
            with conn:
                data = self._recv_line(conn)
                if not data:
                    return
                try:
                    req = json.loads(data.decode('utf-8', errors='replace'))
                except ValueError:
                    resp = {"ok": False, "error": "bad_json"}
                else:
                    try:
                        resp = self._handler(req) or {"ok": True}
                    except Exception as e:
                        logging.error(f"IPC handler 异常: {e}")
                        resp = {"ok": False, "error": str(e)}
                payload = json.dumps(resp, ensure_ascii=False).encode('utf-8') + b"\n"
                conn.sendall(payload)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _recv_line(conn):
        """按行读取 (缓冲, 直到换行)"""
        buf = b""
        while len(buf) < BUF_SIZE:
            chunk = conn.recv(4096)
            if not chunk:
                return b""
            buf += chunk
            if b"\n" in buf:
                return buf.split(b"\n", 1)[0]
        return buf


# ==========================================
# 客户端 (GUI)
# ==========================================
class IpcClient:
    """TCP 客户端: 每次 request 一条请求"""

    # 连续失败日志的节流间隔 (秒): 守护进程启动需要数秒, 期间的连接拒绝
    # 属正常现象, 不值得每 2 秒刷一条
    _FAIL_LOG_INTERVAL = 30.0

    def __init__(self, address=None, timeout=3.0):
        self._address = address or ipc_address()
        self._timeout = timeout
        self._fail_streak = 0        # 连续失败次数 (成功后归零)
        self._last_fail_log = 0.0    # 上次记录失败日志的时刻

    def request(self, payload) -> dict:
        """发送请求并等待响应, 失败返回 None"""
        try:
            with socket.create_connection(self._address, timeout=self._timeout) as s:
                self._fail_streak = 0
                s.settimeout(self._timeout)
                data = json.dumps(payload, ensure_ascii=False).encode('utf-8') + b"\n"
                s.sendall(data)
                buf = b""
                while len(buf) < BUF_SIZE:
                    chunk = s.recv(4096)
                    if not chunk:
                        return None
                    buf += chunk
                    if b"\n" in buf:
                        line = buf.split(b"\n", 1)[0]
                        return json.loads(line.decode('utf-8', errors='replace'))
                return None
        except (OSError, ValueError) as e:
            self._fail_streak += 1
            now = time.monotonic()
            if self._fail_streak == 1 or now - self._last_fail_log >= self._FAIL_LOG_INTERVAL:
                logging.debug(f"IPC 请求失败 (连续第 {self._fail_streak} 次): {e}")
                self._last_fail_log = now
            return None

    def alive(self) -> bool:
        return self.request({"cmd": "status"}) is not None
