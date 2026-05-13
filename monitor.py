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
  python monitor.py export [--creator <id>]                  导出 CSV
  python monitor.py web [--port <port>]                      启动 Web 面板
"""

import asyncio
import csv
import json
import logging
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

import yaml

from db import (
    init_db, add_creator, remove_creator, list_creators, get_creator,
    upsert_video, add_snapshot, update_last_fetched, update_creator_profile,
    get_stats, get_trend,
)
from spider import DouyinSpider
from utils import resolve_secuid

CONFIG_PATH = Path(__file__).parent / "config.yaml"
EXPORT_DIR = Path(__file__).parent / "exports"

logger = logging.getLogger(__name__)


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─── 命令：add ─────────────────────────────────────────────────

def cmd_add(args: list[str]):
    """添加博主到监控列表"""
    name = None
    url = None
    i = 0
    while i < len(args):
        if args[i] == "--name" and i + 1 < len(args):
            name = args[i + 1]
            i += 2
        else:
            url = args[i]
            i += 1

    if not url:
        logger.info("用法: python monitor.py add <url或sec_uid> [--name <名称>]")
        return

    # 提取 sec_uid（支持短链接、分享文案等）
    sec_uid, err = resolve_secuid(url)
    if err:
        logger.error("%s", err)
        return

    if not name:
        name = f"博主_{sec_uid[:8]}"

    init_db()
    cid = add_creator(name, sec_uid)
    if cid:
        logger.info("添加 %s (ID=%d)", name, cid)
    else:
        logger.info("跳过 %s: 已存在", sec_uid)


# ─── 命令：remove ──────────────────────────────────────────────

def cmd_remove(args: list[str]):
    if not args:
        logger.info("用法: python monitor.py remove <ID或名称或sec_uid>")
        return
    init_db()
    remove_creator(args[0])
    logger.info("删除 %s", args[0])


# ─── 命令：list ────────────────────────────────────────────────

def cmd_list():
    init_db()
    creators = list_creators()
    if not creators:
        logger.warning("列表为空，请用 'python monitor.py add <url>' 添加博主")
        return

    print(f"{'ID':<4} {'名称':<16} {'最后抓取':<20} {'状态'}")
    print("-" * 55)
    for c in creators:
        last = c["last_fetched_at"] or "从未"
        status = "启用" if c["enabled"] else "停用"
        print(f"{c['id']:<4} {c['name']:<16} {last:<20} {status}")


# ─── 命令：run ─────────────────────────────────────────────────

async def cmd_run(args: list[str]):
    """运行抓取"""
    target = None
    if "--creator" in args:
        idx = args.index("--creator")
        if idx + 1 < len(args):
            target = args[idx + 1]

    config = load_config()
    spider_cfg = config.get("spider", {})

    init_db()

    if target:
        creator = get_creator(target)
        if not creator:
            logger.error("未找到博主: %s", target)
            return
        creators = [creator]
    else:
        creators = list_creators()

    creators = [c for c in creators if c["enabled"]]

    if not creators:
        logger.warning("没有启用的博主，请先 add")
        return

    spider = DouyinSpider(
        headless=spider_cfg.get("headless", True),
        max_scrolls=spider_cfg.get("max_scrolls", 80),
        page_load_wait=spider_cfg.get("page_load_wait", 8),
        idle_limit=spider_cfg.get("scroll_idle_limit", 20),
    )

    total_videos = 0
    for creator in creators:
        print(f"\n{'='*50}")
        print(f"[博主] {creator['name']} (ID={creator['id']})")
        print(f"{'='*50}")

        videos = await spider.fetch(creator["sec_uid"])

        if spider._error:
            logger.error("%s", spider._error)
            continue

        new_videos = 0
        for v in videos:
            vdict = v.to_dict()
            db_id = upsert_video(creator["id"], vdict)
            add_snapshot(db_id, vdict)
            new_videos += 1

        if spider.profile:
            update_creator_profile(creator["id"], spider.profile.to_dict())
            logger.info("  [主页] %s  粉丝:%s",
                        spider.profile.nickname,
                        f"{spider.profile.follower_count:,}")

        update_last_fetched(creator["id"])
        logger.info("  [存储] %d 条视频入库", new_videos)
        total_videos += new_videos

    logger.info("共 %d 条视频入库", total_videos)

    # 自动导出
    if config.get("output", {}).get("auto_export_csv"):
        export_csv(None)


# ─── 命令：report ──────────────────────────────────────────────

def cmd_report(args: list[str]):
    """查看报告"""
    target = None
    if "--creator" in args:
        idx = args.index("--creator")
        if idx + 1 < len(args):
            target = args[idx + 1]

    init_db()

    if target:
        creators = [get_creator(target)]
        if not creators[0]:
            logger.error("未找到: %s", target)
            return
    else:
        creators = list_creators()

    for c in creators:
        stats = get_stats(c["id"])
        print(f"\n{'='*50}")
        print(f"  {c['name']}")
        print(f"{'='*50}")
        print(f"  总视频数: {stats.get('total_videos', 0)}")
        print(f"  总时长:   {stats.get('total_duration_sec', 0):.0f} 秒")
        print(f"  最高点赞: {stats.get('max_likes', 0):,}")
        print(f"  最高评论: {stats.get('max_comments', 0):,}")
        print(f"  快照次数: {stats.get('total_snapshots', 0)}")

        last = c.get("last_fetched_at") or "从未"
        print(f"  最后更新: {last}")

        # 趋势 Top 5
        trends = get_trend(c["id"], limit=5)
        if trends:
            print(f"\n  {'视频标题':<30} {'点赞':>10} {'变化':>8} {'最近更新'}")
            print(f"  {'-'*60}")
            for t in trends:
                title = (t["title"] or "")[:28]
                likes = t["like_count"] or 0
                prev = t.get("prev_likes") or 0
                delta = likes - prev
                delta_str = f"+{delta:,}" if delta > 0 else str(delta)
                print(f"  {title:<30} {likes:>10,} {delta_str:>8}  {t['latest'][:16]}")


# ─── 命令：export ─────────────────────────────────────────────

def export_csv(creator_filter: str | None):
    """导出数据为 CSV"""
    import sqlite3
    from db import DB_PATH

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    if creator_filter:
        c = conn.execute("SELECT id, name FROM creators WHERE id=? OR name=?",
                         (creator_filter, creator_filter)).fetchone()
        if not c:
            logger.error("未找到博主: %s", creator_filter)
            conn.close()
            return
        creators = [dict(c)]
    else:
        creators = [dict(r) for r in conn.execute("SELECT id, name FROM creators").fetchall()]

    for c in creators:
        rows = conn.execute("""
            SELECT v.title, v.video_id as vid, v.duration_ms, v.create_time,
                   v.hashtags, v.first_seen_at,
                   s.like_count, s.comment_count, s.share_count, s.view_count,
                   s.fetched_at
            FROM videos v
            JOIN snapshots s ON s.video_id = v.id
            WHERE v.creator_id = ?
            AND s.id = (SELECT id FROM snapshots WHERE video_id = v.id ORDER BY id DESC LIMIT 1)
            ORDER BY s.like_count DESC
        """, (c["id"],)).fetchall()

        if not rows:
            logger.warning("  [%s] 无数据", c["name"])
            continue

        filename = EXPORT_DIR / f"{c['name']}_{ts}.csv"
        with open(filename, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["标题", "视频ID", "时长(ms)", "发布时间", "标签",
                             "首次发现", "点赞", "评论", "分享", "播放", "抓取时间"])
            for r in rows:
                create_ts = datetime.fromtimestamp(r["create_time"]).strftime("%Y-%m-%d %H:%M") if r["create_time"] else ""
                writer.writerow([
                    r["title"], r["vid"], r["duration_ms"], create_ts,
                    r["hashtags"], r["first_seen_at"],
                    r["like_count"], r["comment_count"], r["share_count"],
                    r["view_count"], r["fetched_at"],
                ])

        logger.info("  [导出] %s → %s (%d 条)", c["name"], filename, len(rows))

    conn.close()


def cmd_export(args: list[str]):
    target = None
    if "--creator" in args:
        idx = args.index("--creator")
        if idx + 1 < len(args):
            target = args[idx + 1]
    init_db()
    export_csv(target)


# ─── 命令：schedule ────────────────────────────────────────────

async def cmd_schedule():
    """定时调度循环"""
    config = load_config()
    interval = config.get("schedule", {}).get("interval_minutes", 240)
    jitter = config.get("schedule", {}).get("jitter_minutes", 15)

    logger.info("每 %d 分钟运行一次 (抖动 ±%d 分钟)", interval, jitter)
    logger.info("Ctrl+C 停止")

    while True:
        print(f"\n{'#'*50}")
        logger.info("[%s] 开始新一轮抓取", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        await cmd_run([])

        wait = interval * 60 + random.randint(-jitter * 60, jitter * 60)
        wait = max(wait, 60)
        next_run = datetime.now().timestamp() + wait
        logger.info("下次运行: %s", datetime.fromtimestamp(next_run).strftime("%H:%M:%S"))
        await asyncio.sleep(wait)


# ─── 命令：login ──────────────────────────────────────────────

async def cmd_login():
    """打开有头浏览器手动扫码登录"""
    logger.info("打开浏览器，请扫码...")
    from spider import SESSION_DIR
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    context = await async_playwright().start()
    ctx = await context.chromium.launch_persistent_context(
        user_data_dir=str(SESSION_DIR),
        headless=False,
        viewport={"width": 1920, "height": 1080},
        locale="zh-CN",
    )
    page = await ctx.new_page()
    await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
    logger.info("请在浏览器中完成登录，然后按 Enter...")
    input()
    await ctx.close()
    await context.stop()
    logger.info("会话已保存")


# ─── 命令：web ─────────────────────────────────────────────────

def cmd_web(args: list[str]):
    """启动 Web 面板"""
    port = 8080
    if "--port" in args:
        idx = args.index("--port")
        if idx + 1 < len(args):
            port = int(args[idx + 1])
    import uvicorn
    logger.info("启动面板 → http://127.0.0.1:%d", port)
    uvicorn.run("web.app:app", host="127.0.0.1", port=port, reload=True)


# ─── 入口 ──────────────────────────────────────────────────────

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

    if cmd == "add":
        cmd_add(args)
    elif cmd == "remove":
        cmd_remove(args)
    elif cmd == "list":
        cmd_list()
    elif cmd == "run":
        _run_async(cmd_run(args))
    elif cmd == "report":
        cmd_report(args)
    elif cmd == "export":
        cmd_export(args)
    elif cmd == "schedule":
        try:
            _run_async(cmd_schedule())
        except KeyboardInterrupt:
            logger.info("调度已停止")
    elif cmd == "login":
        _run_async(cmd_login())
    elif cmd == "web":
        cmd_web(args)
    else:
        logger.error("未知命令: %s", cmd)
        print(__doc__)


if __name__ == "__main__":
    main()
