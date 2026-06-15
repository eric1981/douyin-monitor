"""监控命令实现 — 从 monitor.py 拆分，所有 import 在顶层"""
import asyncio
import concurrent.futures
import csv
import itertools
import json
import logging
import random
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from playwright.async_api import async_playwright

from db import (
    init_db, add_creator, remove_creator, list_creators, get_creator,
    get_stats, get_trend, get_db, DB_PATH, ingest_crawl_results,
)
from spider import DouyinSpider
from transcriber import (
    WHISPER_AVAILABLE, VideoFetcher, process_video, transcribe_pending_videos,
    get_whisper, _ensure_cookies,
)
from utils import resolve_secuid

CONFIG_PATH = Path(__file__).parent / "config.yaml"
EXPORT_DIR = Path(__file__).parent / "exports"

logger = logging.getLogger(__name__)


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─── 命令：add ─────────────────────────────────────────────────

def cmd_add(args: list[str]):
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

def cmd_list(_args=None):
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

        profile = spider.profile.to_dict() if spider.profile else None
        new_videos = ingest_crawl_results(creator["id"],
                                          [v.to_dict() for v in videos],
                                          profile)

        if spider.profile:
            logger.info("  [主页] %s  粉丝:%s",
                        spider.profile.nickname,
                        f"{spider.profile.follower_count:,}")

        logger.info("  [存储] %d 条视频入库", new_videos)
        total_videos += new_videos

    logger.info("共 %d 条视频入库", total_videos)

    # 自动转录新视频
    if not WHISPER_AVAILABLE:
        logger.info("faster-whisper 未安装，跳过自动转录（可用 pip install faster-whisper 安装）")
    else:
        try:
            with VideoFetcher() as fetcher:
                n = transcribe_pending_videos(fetcher)
                logger.info("自动转录: %d 条", n)
        except Exception as e:
            logger.error("自动转录过程异常: %s", e)

    # 自动导出
    if config.get("output", {}).get("auto_export_csv"):
        export_csv(None)


# ─── 命令：report ──────────────────────────────────────────────

def cmd_report(args: list[str]):
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

def _seconds_until(hour_str: str) -> float:
    now = datetime.now()
    parts = hour_str.strip().split(":")
    h, m = int(parts[0]), int(parts[1])
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def cmd_schedule(_args=None):
    config = load_config()
    sched = config.get("schedule", {})

    daily_at = sched.get("daily_at")

    if daily_at:
        logger.info("每日定时模式: 每天 %s 执行一次", daily_at)
        logger.info("Ctrl+C 停止")
        while True:
            wait = _seconds_until(daily_at)
            next_run = datetime.now().timestamp() + wait
            logger.info("下次运行: %s",
                        datetime.fromtimestamp(next_run).strftime("%Y-%m-%d %H:%M:%S"))
            await asyncio.sleep(wait)

            print(f"\n{'#'*50}")
            logger.info("[%s] 开始每日抓取", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            await cmd_run([])
    else:
        interval = sched.get("interval_minutes", 240)
        jitter = sched.get("jitter_minutes", 15)

        logger.info("间隔模式: 每 %d 分钟运行一次 (抖动 ±%d 分钟)", interval, jitter)
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

async def cmd_login(_args=None):
    args = (_args or [])
    slot = None
    if "--slot" in args:
        idx = args.index("--slot")
        if idx + 1 < len(args):
            slot = args[idx + 1]

    session_dir = Path(__file__).parent / "douyin_session"
    if slot:
        session_dir = Path(__file__).parent / f"douyin_session_{slot}"
    logger.info("打开浏览器（会话: %s），请扫码...", session_dir.name)
    session_dir.mkdir(parents=True, exist_ok=True)
    context = await async_playwright().start()
    ctx = await context.chromium.launch_persistent_context(
        user_data_dir=str(session_dir),
        headless=False,
        viewport={"width": 1920, "height": 1080},
        locale="zh-CN",
    )
    page = await ctx.new_page()
    await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
    logger.info("请在浏览器中完成登录，然后按 Enter...")
    input()
    try:
        await ctx.close()
    except Exception:
        pass
    try:
        await context.stop()
    except Exception:
        pass
    logger.info("会话已保存")


# ─── 命令：web ─────────────────────────────────────────────────

def cmd_web(args: list[str]):
    port = 8080
    if "--port" in args:
        idx = args.index("--port")
        if idx + 1 < len(args):
            port = int(args[idx + 1])
    import uvicorn
    logger.info("启动面板 → http://127.0.0.1:%d", port)
    uvicorn.run("web.app:app", host="127.0.0.1", port=port, reload=True)


# ─── 命令：transcribe ──────────────────────────────────────────

def cmd_transcribe(args: list[str]):
    target = None
    limit = None
    workers = None
    i = 0
    while i < len(args):
        if args[i] == "--creator" and i + 1 < len(args):
            target = args[i + 1]
            i += 2
        elif args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
        elif args[i] == "--workers" and i + 1 < len(args):
            workers = int(args[i + 1])
            i += 2
        else:
            i += 1

    init_db()

    if target:
        creator = get_creator(target)
        if not creator:
            logger.error("未找到博主: %s", target)
            return
        creator_ids = [creator["id"]]
    else:
        creator_ids = [c["id"] for c in list_creators() if c["enabled"]]

    total_pending = 0
    for cid in creator_ids:
        with get_db() as db:
            n = db.execute("""
                SELECT COUNT(*) as c FROM videos v
                LEFT JOIN transcripts t ON t.video_id = v.id
                WHERE v.creator_id = ? AND t.id IS NULL
            """, (cid,)).fetchone()["c"]
            total_pending += n
    logger.info("待转录: %d 条", total_pending)

    all_rows = []
    for cid in creator_ids:
        with get_db() as db:
            rows = db.execute("""
              SELECT v.id, v.video_id, v.title
              FROM videos v
              LEFT JOIN transcripts t ON t.video_id = v.id
              WHERE v.creator_id = ? AND t.id IS NULL""" +
              (" ORDER BY v.id DESC LIMIT ?" if limit else " ORDER BY v.id DESC"),
              (cid,) + ((limit,) if limit else ())
            ).fetchall()
            all_rows.extend([dict(r) for r in rows])

    if not all_rows:
        logger.info("没有待转录视频")
        return

    get_whisper()

    _base = Path(__file__).parent
    _sessions = sorted(_base.glob("douyin_session*"))
    _sessions = [d for d in _sessions if d.is_dir() and (
        (d / "Default" / "Network" / "Cookies").exists() or (d / "Default" / "Cookies").exists()
    )]
    if not _sessions:
        _base_dir = _base / "douyin_session"
        _sessions = [_base_dir]
    logger.info("会话: %d 个 (%s)", len(_sessions), ", ".join(d.name for d in _sessions))

    if workers is None:
        workers = len(_sessions)
    workers = max(1, min(workers, len(_sessions), 4))

    done_lock = threading.Lock()

    def worker(task):
        rows_slice, session_dir = task
        with VideoFetcher(session_dir=session_dir) as fetcher:
            for row in rows_slice:
                with get_db() as db:
                    if db.execute("SELECT id FROM transcripts WHERE video_id=?",
                                  (row["id"],)).fetchone():
                        with done_lock:
                            done_count[0] += 1
                        continue
                    time.sleep(random.uniform(3, 5))
                try:
                    process_video(fetcher, row["video_id"], row["id"])
                except Exception as e:
                    logger.error("转录失败 %s: %s", row["video_id"], e)
                with done_lock:
                    done_count[0] += 1
                logger.info("[%d/%d] %s", done_count[0], total_pending, row["title"][:40])

    chunk_size = max(1, len(all_rows) // workers)
    done_count = [0]
    _tasks = []
    for i in range(workers):
        chunk = all_rows[i * chunk_size : (i + 1) * chunk_size] if i < workers - 1 else all_rows[i * chunk_size:]
        _tasks.append((chunk, _sessions[i]))
    logger.info("启动 %d worker（各占 1 会话）", workers)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, _tasks))

    logger.info("转录完成: %d/%d 条", done_count[0], total_pending)


# ─── 入口函数表 ────────────────────────────────────────────────

SYNC_COMMANDS = {
    "add": cmd_add,
    "remove": cmd_remove,
    "list": cmd_list,
    "report": cmd_report,
    "export": cmd_export,
    "web": cmd_web,
    "transcribe": cmd_transcribe,
}

ASYNC_COMMANDS = {
    "run": cmd_run,
    "schedule": cmd_schedule,
    "login": cmd_login,
}
