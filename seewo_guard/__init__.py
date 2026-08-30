# -*- coding: utf-8 -*-
"""
seewokiller v4.4 -- 用于关闭希沃易启学(易课堂)学生端管控的双进程工具
==================================================================
作用对象: 希沃易启学学生端 SeewoYiQiXueStudent 的四个进程, 见
          config.TARGET_EXES。本程序用于结束、冻结或解除这些进程对本机的
          屏幕广播、窗口置顶、网络与桌面的管控。

模块划分:
  gui_app  : 操作界面 / 托盘 / 全局热键 / 各项功能按钮
  daemon   : 无界面常驻进程, 负责 GUI 心跳丢失后重新拉起 GUI、
             监听关机消息安全退出、承载 IPC 服务端与状态持久化
  ipc      : 本地回环 TCP 通信 (127.0.0.1 + 会话相关端口),
             每行一条 JSON 的请求/响应协议, 并非命名管道
  window_ops / win_api / protection / keyboard / utils / logging_system
           : 分别为窗口与进程操作、Win32 声明、进程加固、键盘钩子、
             通用工具、日志
"""
__version__ = "4.4"
__description__ = "seewokiller - 关闭希沃易启学学生端管控的工具 (双进程版)"
