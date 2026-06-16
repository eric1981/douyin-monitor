"""抖音博主监控 - Web 前端 (FastAPI)"""
import asyncio
import sys
import logging
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, Request, Query, Body
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
import uvicorn
import asyncio
import traceback as _tb
import json
import yaml

from db import (
    init_db, add_creator, remove_creator, rename_creator, list_creators, get_creator,
    upsert_video, add_snapshot, update_last_fetched, update_creator_profile, get_stats, get_db,
    get_all_stats, get_batch_transcripts, get_today_videos, get_today_summary,
    ingest_crawl_results, upsert_comment, list_comments, get_comment_count,
    delete_absent_comments,
)
from spider import DouyinSpider
from utils import resolve_secuid, async_resolve_secuid

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
env = Environment(loader=FileSystemLoader(str(BASE_DIR / "templates")))
env.filters["format_number"] = lambda v: f"{v:,}"
env.filters["format_number_cn"] = lambda v: (
    f"{v/100000000:.1f}亿" if v >= 100000000 else
    f"{v/10000:.1f}万" if v >= 10000 else
    f"{v:,}"
)
env.filters["int"] = int


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("数据库已初始化")
    yield


app = FastAPI(title="抖音博主监控", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def render(name: str, **ctx) -> HTMLResponse:
    template = env.get_template(name)
    return HTMLResponse(template.render(**ctx))


def _freshness_level(last_fetched_at):
    if not last_fetched_at:
        return "old"
    try:
        from datetime import datetime, timedelta
        dt = datetime.fromisoformat(last_fetched_at)
        delta = datetime.now() - dt
        if delta < timedelta(hours=6):
            return "fresh"
        elif delta < timedelta(days=1):
            return "stale"
        else:
            return "old"
    except Exception:
        return "old"

env.globals["_freshness_level"] = _freshness_level
env.globals["_freshness_text"] = lambda v: {"fresh": "刚刚", "stale": "今日", "old": "较早"}.get(_freshness_level(v), "较早")


# ─── 页面路由 ──────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    creators = list_creators()
    all_stats = get_all_stats()
    for c in creators:
        s = all_stats.get(c["id"], {})
        c["total_videos"] = s.get("total_videos", 0)
        c["max_likes"] = s.get("max_likes", 0)
        c["max_comments"] = s.get("max_comments", 0)

    total_likes = 0
    with get_db() as db:
        row = db.execute("""
            SELECT COALESCE(SUM(s.like_count), 0) as total_likes
            FROM snapshots s
            WHERE s.id = (SELECT id FROM snapshots WHERE video_id = s.video_id ORDER BY id DESC LIMIT 1)
        """).fetchone()
        total_likes = row["total_likes"] if row else 0

    today_summary = get_today_summary()
    today_videos = get_today_videos(limit=20)
    for v in today_videos:
        v["create_time_str"] = _fmt_time(v["create_time"])
        v["duration_str"] = _fmt_duration(v["duration_ms"])
    # 批量获取今日视频的转录文本
    if today_videos:
        transcripts = get_batch_transcripts([v["id"] for v in today_videos])
        for v in today_videos:
            v["transcript"] = transcripts.get(v["id"], "")

    return render("dashboard.html",
                  creators=creators,
                  today_summary=today_summary,
                  today_videos=today_videos,
                  summary_stats={
                      "total_creators": len(creators),
                      "total_videos": sum(c.get("total_videos", 0) for c in creators),
                      "total_likes": total_likes,
                  })


@app.get("/creators", response_class=HTMLResponse)
async def creators_page(request: Request):
    return render("creators.html", creators=list_creators())


@app.get("/videos", response_class=HTMLResponse)
async def videos_page(request: Request, creator_id: int = Query(None),
                      search: str = Query(None), sort: str = Query("likes"),
                      page: int = Query(1)):
    # 校验排序参数（白名单），防止 SQL 注入
    SORT_MAP = {
        "likes": "s.like_count DESC",
        "comments": "s.comment_count DESC",
        "shares": "s.share_count DESC",
        "time": "v.create_time DESC",
    }
    if sort not in SORT_MAP:
        sort = "likes"
    order = SORT_MAP[sort]

    creator = None
    conditions = []
    params = []
    if creator_id:
        conditions.append("v.creator_id = ?")
        params.append(creator_id)
        creator = get_creator(str(creator_id))
    if search:
        conditions.append("v.title LIKE ?")
        params.append(f"%{search}%")
    where_clause = " AND ".join(conditions) if conditions else "1=1"
    offset = (page - 1) * 20

    with get_db() as db:
        rows = db.execute(f"""
            SELECT v.id, v.video_id as vid, v.title, v.cover_url, v.duration_ms,
                   v.create_time, v.hashtags, v.first_seen_at,
                   s.like_count, s.comment_count, s.share_count, s.view_count, s.fetched_at,
                   t.full_text as transcript,
                   c.name as creator_name, c.id as cid
            FROM videos v JOIN snapshots s ON s.video_id = v.id
            JOIN creators c ON c.id = v.creator_id
            LEFT JOIN transcripts t ON t.video_id = v.id
            WHERE {where_clause} AND s.id = (
                SELECT id FROM snapshots WHERE video_id=v.id ORDER BY id DESC LIMIT 1
            )
            ORDER BY {order} LIMIT 20 OFFSET ?
        """, params + [offset]).fetchall()
        total_row = db.execute(
            f"SELECT COUNT(*) as cnt FROM videos v WHERE {where_clause}", params
        ).fetchone()
        total = total_row["cnt"] if total_row else 0

    videos = []
    for r in rows:
        v = dict(r)
        v["create_time_str"] = _fmt_time(v["create_time"])
        v["duration_str"] = _fmt_duration(v["duration_ms"])
        v["hashtags_list"] = json.loads(v["hashtags"]) if v["hashtags"] else []
        videos.append(v)

    return render("videos.html", videos=videos, creator=creator,
                  creators=list_creators(), search=search or "", sort=sort,
                  page=page, total_pages=(total + 19) // 20, total=total,
                  selected_creator=str(creator_id or ""))


@app.get("/video/{video_db_id}", response_class=HTMLResponse)
async def video_detail(request: Request, video_db_id: int):
    from db import get_db, get_transcript
    with get_db() as db:
        row = db.execute("""
            SELECT v.id, v.video_id as vid, v.title, v.cover_url,
                   v.duration_ms, v.create_time, v.hashtags,
                   s.like_count, s.comment_count, s.share_count,
                   c.name as creator_name, c.id as cid
            FROM videos v
            JOIN snapshots s ON s.video_id = v.id
            JOIN creators c ON c.id = v.creator_id
            WHERE v.id = ?
              AND s.id = (
                  SELECT id FROM snapshots WHERE video_id=v.id ORDER BY id DESC LIMIT 1
              )
        """, (video_db_id,)).fetchone()
        if not row:
            return HTMLResponse("视频不存在", status_code=404)
    v = dict(row)
    v["create_time_str"] = _fmt_time(v["create_time"])
    v["duration_str"] = _fmt_duration(v["duration_ms"])
    v["hashtags_list"] = json.loads(v["hashtags"]) if v["hashtags"] else []
    transcript = get_transcript(video_db_id)
    comment_count = get_comment_count(video_db_id)
    return render("video_detail.html", v=v, transcript=transcript, comment_count=comment_count)
@app.get("/trends/{creator_id}", response_class=HTMLResponse)
async def trends_page(request: Request, creator_id: int):
    creator = get_creator(str(creator_id))
    if not creator:
        return HTMLResponse("博主不存在", status_code=404)
    return render("trends.html", creator=creator)


# ─── API ───────────────────────────────────────────────

@app.post("/api/creators/add")
async def api_add_creator(name: str = Body(""), sec_uid: str = Body("")):
    if not sec_uid:
        return JSONResponse({"error": "sec_uid 不能为空"}, 400)
    sec_uid, err = await async_resolve_secuid(sec_uid)
    if err:
        return JSONResponse({"error": err}, 400)
    if not name:
        name = f"博主_{sec_uid[:8]}"
    cid = add_creator(name, sec_uid)
    return {"id": cid, "name": name}


@app.delete("/api/creators/{creator_id}")
async def api_remove_creator(creator_id: int):
    remove_creator(str(creator_id))
    return {"ok": True}


@app.put("/api/creators/{creator_id}")
async def api_rename_creator(creator_id: int, name: str = Body(..., embed=True)):
    if not name or not name.strip():
        return JSONResponse({"error": "名称不能为空"}, 400)
    ok = rename_creator(creator_id, name.strip())
    if not ok:
        return JSONResponse({"error": "博主不存在"}, 404)
    return {"ok": True, "name": name.strip()}


@app.post("/api/run")
async def api_run_fetch(creator_id: int = None):
    if creator_id:
        c = get_creator(str(creator_id))
        if not c:
            return JSONResponse({"error": "博主不存在"}, 404)
        creators = [c]
    else:
        creators = list_creators()
    creators = [c for c in creators if c.get("enabled")]
    if not creators:
        return {"message": "没有启用的博主", "total": 0}

    def _crawl_one(c: dict) -> tuple:
        loop = asyncio.ProactorEventLoop()
        try:
            spider = DouyinSpider(headless=True, max_scrolls=80, page_load_wait=8, idle_limit=20)
            videos = loop.run_until_complete(spider.fetch(c["sec_uid"]))
            profile = spider.profile.to_dict() if spider.profile else None
            if spider._error:
                return [], None, spider._error
            return [v.to_dict() for v in videos], profile, None
        finally:
            loop.close()

    try:
        total = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            for c in creators:
                videos, profile, err = await asyncio.get_running_loop().run_in_executor(
                    pool, _crawl_one, c
                )
                if err:
                    logger.error("抓取 %s 失败: %s", c["name"], err)
                    return JSONResponse({"error": f"抓取 {c['name']} 失败: {err}"}, 500)
                n = ingest_crawl_results(c["id"], videos, profile)
                total += n

            # 自动转录新视频
            try:
                from transcriber import WHISPER_AVAILABLE
                if WHISPER_AVAILABLE:
                    def _transcribe_all():
                        from transcriber import VideoFetcher, transcribe_pending_videos
                        with VideoFetcher() as fetcher:
                            return transcribe_pending_videos(fetcher)
                    n = await asyncio.get_running_loop().run_in_executor(pool, _transcribe_all)
                    logger.info("自动转录: %d 条", n)
            except ImportError:
                pass

        logger.info("抓取完成: %d 条视频入库", total)
        return {"message": "抓取完成", "total": total}
    except Exception as e:
        detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        logger.exception("抓取出错")
        return JSONResponse({"error": f"抓取出错: {detail}"}, 500)


@app.get("/api/trends/{creator_id}")
async def api_trends(creator_id: int):
    with get_db() as db:
        times = db.execute("""
            SELECT DISTINCT fetched_at FROM snapshots s
            JOIN videos v ON v.id=s.video_id WHERE v.creator_id=?
            ORDER BY fetched_at DESC LIMIT 10
        """, (creator_id,)).fetchall()
        times = [t["fetched_at"] for t in reversed(times)]

        top = db.execute("""
            SELECT v.id, v.title, MAX(s.like_count) as m
            FROM videos v JOIN snapshots s ON s.video_id=v.id
            WHERE v.creator_id=? GROUP BY v.id ORDER BY m DESC LIMIT 5
        """, (creator_id,)).fetchall()

        datasets = []
        for tv in top:
            points = []
            for t in times:
                row = db.execute("""
                    SELECT like_count FROM snapshots WHERE video_id=? AND fetched_at<=?
                    ORDER BY fetched_at DESC LIMIT 1
                """, (tv["id"], t)).fetchone()
                points.append(row["like_count"] if row else None)
            datasets.append({"label": (tv["title"] or "")[:20], "data": points})

    return {"labels": times, "datasets": datasets}


@app.get("/api/transcript/{video_db_id}")
async def api_transcript(video_db_id: int):
    """获取视频完整转录文本"""
    from db import get_transcript
    t = get_transcript(video_db_id)
    if not t:
        return JSONResponse({"error": "未转录"}, 404)
    return t


@app.post("/api/transcribe/{video_db_id}")
async def api_transcribe_video(video_db_id: int):
    """触发单个视频语音转录"""
    from db import get_transcript

    # 检查视频是否存在
    with get_db() as db:
        row = db.execute(
            "SELECT v.id, v.video_id, c.name FROM videos v JOIN creators c ON c.id=v.creator_id WHERE v.id=?",
            (video_db_id,)
        ).fetchone()
        if not row:
            return JSONResponse({"error": "视频不存在"}, 404)

    # 已转录则直接返回
    existing = get_transcript(video_db_id)
    if existing:
        return {"status": "already_done", "full_text": existing.get("full_text", "")}

    video_id = row["video_id"]

    def _do_transcribe():
        from transcriber import VideoFetcher, process_video
        with VideoFetcher() as fetcher:
            return process_video(fetcher, video_id, video_db_id)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = await asyncio.get_running_loop().run_in_executor(pool, _do_transcribe)
        if result:
            return {"status": "ok", "full_text": result.get("full_text", "")}
        else:
            return {"status": "skipped", "message": "已转录或下载失败"}
    except Exception as e:
        logger.exception("转录失败 video_db_id=%d", video_db_id)
        return JSONResponse({"error": f"转录失败: {e}"}, 500)


@app.post("/api/transcribe-all")
async def api_transcribe_all():
    """批量转录所有未转录视频"""

    with get_db() as db:
        rows = db.execute("""
            SELECT v.id, v.video_id FROM videos v
            LEFT JOIN transcripts t ON t.video_id = v.id
            WHERE t.id IS NULL
            ORDER BY v.id DESC
        """).fetchall()

    if not rows:
        return {"status": "ok", "total": 0, "message": "所有视频已转录"}

    video_list = [(r["id"], r["video_id"]) for r in rows]

    def _do_transcribe_all():
        from transcriber import VideoFetcher, process_video
        results = []
        with VideoFetcher() as fetcher:
            for db_id, vid in video_list:
                try:
                    r = process_video(fetcher, vid, db_id)
                    results.append({"video_db_id": db_id, "status": "ok" if r else "skipped"})
                except Exception as e:
                    results.append({"video_db_id": db_id, "status": "error", "error": str(e)})
        return results

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            results = await asyncio.get_running_loop().run_in_executor(pool, _do_transcribe_all)
        ok = sum(1 for r in results if r["status"] == "ok")
        err = sum(1 for r in results if r["status"] == "error")
        skipped = sum(1 for r in results if r["status"] == "skipped")
        return {"status": "ok", "total": len(results), "ok": ok, "error": err, "skipped": skipped}
    except Exception as e:
        logger.exception("批量转录失败")
        return JSONResponse({"error": f"批量转录失败: {e}"}, 500)


@app.get("/api/transcriber-config")
async def api_get_transcriber_config():
    """读取转录配置（模型、设备）"""
    config_path = BASE_DIR.parent / "config.yaml"
    try:
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            tc = cfg.get("transcriber", {})
            return {
                "model": tc.get("model", "small"),
                "device": tc.get("device", "cpu"),
            }
    except Exception:
        pass
    return {"model": "small", "device": "cpu"}


@app.put("/api/transcriber-config")
async def api_update_transcriber_config(
    model: str = Body(None),
    device: str = Body(None),
):
    """更新转录配置并写回 config.yaml"""
    config_path = BASE_DIR.parent / "config.yaml"
    if not config_path.exists():
        return JSONResponse({"error": "config.yaml 不存在"}, 500)

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return JSONResponse({"error": "读取 config.yaml 失败"}, 500)

    tc = cfg.setdefault("transcriber", {})
    updated = {}
    if model is not None:
        tc["model"] = model
        updated["model"] = model
    if device is not None:
        tc["device"] = device
        updated["device"] = device

    try:
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    except Exception as e:
        return JSONResponse({"error": f"写入 config.yaml 失败: {e}"}, 500)

    logger.info("转录配置已更新: %s", updated)
    return {"status": "ok", "updated": updated}


@app.get("/api/stats")
async def api_stats():
    with get_db() as db:
        tc = db.execute("SELECT COUNT(*) as c FROM creators").fetchone()["c"]
        tv = db.execute("SELECT COUNT(*) as c FROM videos").fetchone()["c"]
        ts = db.execute("SELECT COUNT(*) as c FROM snapshots").fetchone()["c"]
        tl = db.execute("""
            SELECT COALESCE(SUM(s.like_count), 0) as total_likes
            FROM snapshots s
            WHERE s.id = (
                SELECT id FROM snapshots WHERE video_id = s.video_id ORDER BY id DESC LIMIT 1
            )
        """).fetchone()["total_likes"]

    return {"total_creators": tc, "total_videos": tv, "total_snapshots": ts, "total_likes": tl}


# ─── 评论 API ─────────────────────────────────────────────

@app.get("/api/comments/{video_db_id}")
async def api_list_comments(video_db_id: int):
    """返回指定视频的已存评论（JSON）。"""
    rows = list_comments(video_db_id)
    return {"comments": rows, "total": len(rows)}


@app.post("/api/comments/fetch/{video_db_id}")
async def api_fetch_comments(video_db_id: int):
    """触发抓取指定视频的评论。（在独立线程跑 Playwright，避免 Python 3.14 事件循环不兼容）"""
    from comment_spider import CommentSpider

    with get_db() as db:
        video = db.execute(
            "SELECT id, video_id FROM videos WHERE id = ?",
            (video_db_id,),
        ).fetchone()
    if not video:
        return JSONResponse({"error": "视频不存在"}, 404)

    logger.info("开始抓取评论 video_db_id=%s", video_db_id)

    def _sync_fetch(vid: str) -> list[dict]:
        """在独立线程中创建自己的事件循环跑 Playwright。"""
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            spider = CommentSpider(headless=True)
            return loop.run_until_complete(
                spider.fetch_comments(vid, max_pages=3, max_total=30)
            )
        finally:
            loop.close()

    loop = asyncio.get_event_loop()
    try:
        comments = await asyncio.wait_for(
            loop.run_in_executor(None, _sync_fetch, video["video_id"]),
            timeout=60,
        )
    except asyncio.TimeoutError:
        return JSONResponse({"error": "抓取超时"}, 504)
    except Exception as e:
        logger.error("抓取失败: type=%s msg=%r\n%s", type(e).__name__, str(e), _tb.format_exc())
        err_msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        return JSONResponse({"error": err_msg}, 500)

    saved = 0
    for c in comments:
        upsert_comment(video_db_id, c)
        saved += 1

    active_ids = [c["comment_id"] for c in comments]
    delete_absent_comments(video_db_id, active_ids)

    return {"saved": saved, "total": len(comments)}


def _fmt_time(ts):
    if not ts:
        return "-"
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _fmt_duration(ms):
    if not ms:
        return "-"
    m, s = divmod(ms // 1000, 60)
    return f"{m}:{s:02d}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    uvicorn.run("app:app", host="127.0.0.1", port=8080, reload=True)