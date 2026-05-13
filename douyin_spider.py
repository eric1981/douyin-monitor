"""抖音博主视频数据抓取 - 兼容包装 (已合并到 spider.py)

用法:
  python douyin_spider.py <sec_uid> [--login]

此文件已废弃，核心逻辑在 spider.DouyinSpider 中。
保留此文件仅为向后兼容。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from spider import DouyinSpider, Video  # noqa: F401


def extract_sec_uid(raw: str) -> str:
    if "/user/" in raw:
        return raw.split("/user/")[-1].split("?")[0]
    return raw


async def main():
    import asyncio
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    need_login = "--login" in sys.argv

    if len(args) < 1:
        print("用法: python douyin_spider.py <sec_uid> [--login]")
        sys.exit(1)

    sec_uid = extract_sec_uid(args[0])
    spider = DouyinSpider(headless=not need_login)
    videos = await spider.fetch(sec_uid)
    if spider._error:
        print(f"[错误] {spider._error}")
    print(f"[结果] {len(videos)} 条视频")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
