#!/usr/bin/env python3
"""
抖音博主监控系统 - CLI 入口
用法:
  python monitor.py add <url_or_sec_uid> [--name <name>]   添加博主
  python monitor.py remove <id_or_name>                     删除博主
  python monitor.py list                                     列出所有博主
  python monitor.py run [--creator <id>]                     运行抓取
  python monitor.py report [--creator <id>]                  查看报告
  python monitor.py schedule                                 启动定时调度
  python monitor.py login                                    扫码登录
  python monitor.py login --slot 1                           登录到多会话槽位
  python monitor.py export-cookies                           从已登录的 Chrome 导出 cookies（需先关闭 Chrome）
  python monitor.py transcribe [--creator <id>] [--limit N] [--workers 2] 转录视频语音
  python monitor.py export [--creator <id>]                  导出 CSV
  python monitor.py web [--port <port>]                      启动 Web 面板
"""

import asyncio
import logging
import sys

from commands import SYNC_COMMANDS, ASYNC_COMMANDS, _ensure_cookies

logger = logging.getLogger(__name__)


def _run_async(coro):
    """在 Windows 上使用 ProactorEventLoop 运行异步函数（Playwright 需要）"""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    return asyncio.run(coro)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "export-cookies":
        _ensure_cookies()
        logger.info("cookies 已导入，可以使用 transcribe 命令了")
        return

    if cmd in ASYNC_COMMANDS:
        try:
            _run_async(ASYNC_COMMANDS[cmd](args))
        except KeyboardInterrupt:
            logger.info("已停止")
    elif cmd in SYNC_COMMANDS:
        SYNC_COMMANDS[cmd](args)
    else:
        logger.error("未知命令: %s", cmd)
        print(__doc__)


if __name__ == "__main__":
    main()
