# -*- coding: utf-8 -*-
"""
main.py - SeewoGuard v4.1 入口 (双进程架构)
============================================
默认模式     : 启动 GUI 界面进程 (自动拉起守护进程)
--daemon     : 以守护进程模式运行 (无界面, 常驻后台)
--gui        : 仅启动界面进程

架构说明:
  GUI进程   -> 窗口/托盘/热键/功能按钮, 被任务管理器结束不影响守护
  守护进程  -> 无界面, 监听系统关机自动退出, 并监控 GUI 心跳,
                GUI 被杀后自动重新拉起

安全说明:
  本项目已彻底移除关键进程 (RtlSetProcessIsCritical) 代码,
  任何情况下都不会设置关键进程标记, 不存在蓝屏风险。
"""
import sys
import os
import time


def main():
    started_at = time.monotonic()
    args = sys.argv[1:]

    for arg in args:
        if arg.startswith("--auto-quit-ms="):
            os.environ["SEEWO_GUARD_AUTO_QUIT_MS"] = arg.split("=", 1)[1]

    if "--daemon" in args:
        from seewo_guard.daemon import daemon_main
        daemon_main()
        return

    # 默认 / --gui: 权限引导必须早于 PySide6/GUI 导入，避免首个进程
    # 在切换到 UIAccess 前重复加载整套 Qt。
    from seewo_guard.bootstrap import prepare_gui_process
    if prepare_gui_process():
        return

    from seewo_guard.gui_app import gui_main
    gui_main(started_at=started_at)


if __name__ == "__main__":
    # 避免打包后相对导入问题
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
