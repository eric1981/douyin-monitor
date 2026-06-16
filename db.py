"""SQLite 数据层：博主管理 + 视频存储 + 快照追踪"""
import sqlite3
import json
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent / "data" / "douyin_monitor.db"


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    """初始化数据库表"""
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS creators (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            sec_uid     TEXT NOT NULL UNIQUE,
            platform    TEXT DEFAULT 'douyin',
            enabled     INTEGER DEFAULT 1,
            added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_fetched_at TIMESTAMP,
            nickname    TEXT,
            avatar_url  TEXT,
            follower_count INTEGER DEFAULT 0,
            following_count INTEGER DEFAULT 0,
            total_likes INTEGER DEFAULT 0,
            bio         TEXT
        );

        CREATE TABLE IF NOT EXISTS videos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id  INTEGER NOT NULL REFERENCES creators(id),
            video_id    TEXT NOT NULL,
            title       TEXT,
            cover_url   TEXT,
            video_url   TEXT,
            duration_ms INTEGER,
            create_time INTEGER,
            hashtags    TEXT DEFAULT '[]',
            first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(creator_id, video_id)
        );

        CREATE TABLE IF NOT EXISTS comments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id        INTEGER NOT NULL REFERENCES videos(id),
            comment_id      TEXT NOT NULL,
            text            TEXT,
            digg_count      INTEGER DEFAULT 0,
            reply_count     INTEGER DEFAULT 0,
            user_name       TEXT,
            ip_label        TEXT,
            create_time     INTEGER,
            first_seen      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(video_id, comment_id)
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id    INTEGER NOT NULL REFERENCES videos(id),
            like_count  INTEGER DEFAULT 0,
            comment_count INTEGER DEFAULT 0,
            share_count INTEGER DEFAULT 0,
            view_count  INTEGER DEFAULT 0,
            fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS transcripts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id    INTEGER NOT NULL UNIQUE REFERENCES videos(id),
            full_text   TEXT,
            segments    TEXT DEFAULT '[]',
            language    TEXT DEFAULT 'zh',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_snapshots_video_time
            ON snapshots(video_id, fetched_at);

        CREATE INDEX IF NOT EXISTS idx_videos_creator
            ON videos(creator_id);
        """)

    # Migration: add columns that may not exist in older DBs
    _migrate_columns()


def _migrate_columns():
    """为旧数据库补充新增列"""
    new_cols = [
        ("nickname", "TEXT"),
        ("avatar_url", "TEXT"),
        ("follower_count", "INTEGER DEFAULT 0"),
        ("following_count", "INTEGER DEFAULT 0"),
        ("total_likes", "INTEGER DEFAULT 0"),
        ("bio", "TEXT"),
    ]
    with get_db() as db:
        existing = {r["name"] for r in db.execute("PRAGMA table_info(creators)").fetchall()}
        for col, col_type in new_cols:
            if col not in existing:
                db.execute(f"ALTER TABLE creators ADD COLUMN {col} {col_type}")


# ─── Creator CRUD ─────────────────────────────────────────────

def add_creator(name: str, sec_uid: str, platform: str = "douyin") -> int:
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO creators (name, sec_uid, platform) VALUES (?, ?, ?)",
            (name, sec_uid, platform)
        )
        db.commit()
        row = db.execute("SELECT id FROM creators WHERE sec_uid = ?", (sec_uid,)).fetchone()
        return row["id"] if row else 0


def remove_creator(identifier: str):
    with get_db() as db:
        db.execute(
            "DELETE FROM creators WHERE id = ? OR name = ? OR sec_uid = ?",
            (identifier, identifier, identifier)
        )
        db.commit()


def list_creators() -> list[dict]:
    with get_db() as db:
        rows = db.execute("""
            SELECT id, name, sec_uid, platform, enabled, last_fetched_at,
                   nickname, avatar_url, follower_count, following_count, total_likes, bio
            FROM creators ORDER BY id
        """).fetchall()
        return [dict(r) for r in rows]


def get_creator(identifier: str) -> dict | None:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM creators WHERE id = ? OR name = ? OR sec_uid = ?",
            (identifier, identifier, identifier)
        ).fetchone()
        return dict(row) if row else None


def rename_creator(creator_id: int, new_name: str) -> bool:
    """重命名博主，返回是否成功"""
    with get_db() as db:
        cur = db.execute("UPDATE creators SET name = ? WHERE id = ?", (new_name, creator_id))
        db.commit()
        return cur.rowcount > 0


def update_creator_profile(creator_id: int, profile: dict):
    """更新博主主页公开信息（昵称、头像、粉丝等）"""
    with get_db() as db:
        db.execute("""
            UPDATE creators SET
                nickname = ?, avatar_url = ?,
                follower_count = ?, following_count = ?,
                total_likes = ?, bio = ?
            WHERE id = ?
        """, (
            profile.get("nickname"),
            profile.get("avatar_url"),
            profile.get("follower_count", 0),
            profile.get("following_count", 0),
            profile.get("total_likes", 0),
            profile.get("bio"),
            creator_id,
        ))
        db.commit()


def update_last_fetched(creator_id: int):
    with get_db() as db:
        db.execute(
            "UPDATE creators SET last_fetched_at = ? WHERE id = ?",
            (datetime.now().isoformat(), creator_id)
        )
        db.commit()


def ingest_crawl_results(creator_id: int, videos: list[dict], profile: dict | None) -> int:
    """将爬虫结果写入数据库（去重入库 + 快照 + 主页信息 + 时间戳），返回入库数"""
    for vdict in videos:
        db_id = upsert_video(creator_id, vdict)
        add_snapshot(db_id, vdict)
    if profile:
        update_creator_profile(creator_id, profile)
    update_last_fetched(creator_id)
    return len(videos)


# ─── Video + Snapshot ─────────────────────────────────────────

def upsert_video(creator_id: int, video: dict) -> int:
    """插入或更新视频（幂等），返回 videos 表的 id"""
    hashtags_json = json.dumps(video["hashtags"], ensure_ascii=False)
    with get_db() as db:
        row = db.execute("""
            INSERT INTO videos
                (creator_id, video_id, title, cover_url, video_url, duration_ms, create_time, hashtags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(creator_id, video_id)
            DO UPDATE SET title=excluded.title, cover_url=excluded.cover_url,
                          video_url=excluded.video_url, duration_ms=excluded.duration_ms,
                          hashtags=excluded.hashtags
            RETURNING id
        """, (
            creator_id,
            video["video_id"],
            video["title"],
            video["cover_url"],
            video["video_url"],
            video["duration_ms"],
            video["create_time"],
            hashtags_json,
        )).fetchone()
        db.commit()
        return row["id"]


def add_snapshot(video_db_id: int, video: dict):
    """记录一次互动数据快照"""
    with get_db() as db:
        db.execute("""
            INSERT INTO snapshots (video_id, like_count, comment_count, share_count, view_count)
            VALUES (?, ?, ?, ?, ?)
        """, (
            video_db_id,
            video["like_count"],
            video["comment_count"],
            video["share_count"],
            video["view_count"],
        ))
        db.commit()


def get_trend(creator_id: int, limit: int = 10) -> list[dict]:
    """获取博主视频的最新互动变化趋势"""
    with get_db() as db:
        rows = db.execute("""
            SELECT v.title, v.video_id as platform_vid,
                   s1.like_count, s1.comment_count, s1.share_count, s1.fetched_at as latest,
                   s2.like_count as prev_likes, s2.fetched_at as prev_time
            FROM videos v
            JOIN snapshots s1 ON s1.video_id = v.id
            LEFT JOIN snapshots s2 ON s2.video_id = v.id
                AND s2.id = (SELECT id FROM snapshots WHERE video_id = v.id AND id < s1.id ORDER BY id DESC LIMIT 1)
            WHERE v.creator_id = ?
              AND s1.id = (SELECT id FROM snapshots WHERE video_id = v.id ORDER BY id DESC LIMIT 1)
            ORDER BY s1.like_count DESC
            LIMIT ?
        """, (creator_id, limit)).fetchall()
        return [dict(r) for r in rows]


def get_stats(creator_id: int) -> dict:
    """获取博主汇总统计"""
    with get_db() as db:
        row = db.execute("""
            SELECT
                COUNT(DISTINCT v.id) as total_videos,
                COUNT(DISTINCT s.id) as total_snapshots,
                COALESCE(SUM(v.duration_ms) / 1000, 0) as total_duration_sec,
                COALESCE(MAX(s.like_count), 0) as max_likes,
                COALESCE(MAX(s.comment_count), 0) as max_comments
            FROM creators c
            LEFT JOIN videos v ON v.creator_id = c.id
            LEFT JOIN snapshots s ON s.video_id = v.id
            WHERE c.id = ?
        """, (creator_id,)).fetchone()
        return dict(row) if row else {}


def get_all_stats() -> dict[int, dict]:
    """批量获取所有博主的汇总统计（一次查询）"""
    with get_db() as db:
        rows = db.execute("""
            SELECT c.id,
                COUNT(DISTINCT v.id) as total_videos,
                COUNT(DISTINCT s.id) as total_snapshots,
                COALESCE(SUM(v.duration_ms) / 1000, 0) as total_duration_sec,
                COALESCE(MAX(s.like_count), 0) as max_likes,
                COALESCE(MAX(s.comment_count), 0) as max_comments
            FROM creators c
            LEFT JOIN videos v ON v.creator_id = c.id
            LEFT JOIN snapshots s ON s.video_id = v.id
            GROUP BY c.id
        """).fetchall()
        return {r["id"]: dict(r) for r in rows}


def get_batch_transcripts(video_ids: list[int]) -> dict[int, str]:
    """批量获取多个视频的转录文本"""
    if not video_ids:
        return {}
    placeholders = ",".join("?" * len(video_ids))
    with get_db() as db:
        rows = db.execute(
            f"SELECT video_id, full_text FROM transcripts WHERE video_id IN ({placeholders})",
            video_ids,
        ).fetchall()
        return {r["video_id"]: r["full_text"] or "" for r in rows}


def get_today_videos(limit: int = 20) -> list[dict]:
    """获取今日首次发现的视频（按点赞排序）"""
    from datetime import date
    today = date.today().isoformat()
    with get_db() as db:
        rows = db.execute("""
            SELECT v.id, v.video_id as vid, v.title, v.cover_url,
                   v.create_time, v.duration_ms,
                   s.like_count, s.comment_count, s.share_count,
                   c.name as creator_name, c.id as cid
            FROM videos v
            JOIN snapshots s ON s.video_id = v.id
            JOIN creators c ON c.id = v.creator_id
            WHERE date(v.first_seen_at) = ?
              AND s.id = (
                  SELECT id FROM snapshots WHERE video_id = v.id ORDER BY id DESC LIMIT 1
              )
            ORDER BY s.like_count DESC
            LIMIT ?
        """, (today, limit)).fetchall()
        return [dict(r) for r in rows]


def get_today_summary() -> dict:
    """获取今日汇总：新增视频数、新增点赞、新增评论"""
    from datetime import date
    today = date.today().isoformat()
    with get_db() as db:
        row = db.execute("""
            SELECT
                COUNT(DISTINCT v.id) as today_videos,
                COALESCE(SUM(s.like_count), 0) as today_likes,
                COALESCE(SUM(s.comment_count), 0) as today_comments
            FROM videos v
            JOIN snapshots s ON s.video_id = v.id
            WHERE date(v.first_seen_at) = ?
              AND s.id = (
                  SELECT id FROM snapshots WHERE video_id = v.id ORDER BY id DESC LIMIT 1
              )
        """, (today,)).fetchone()
        return dict(row) if row else {}


# ─── Transcript ─────────────────────────────────────────────

def save_transcript(video_db_id: int, full_text: str, segments: list[dict]) -> int:
    """保存转录结果，返回 transcript id"""
    with get_db() as db:
        db.execute("""
            INSERT OR REPLACE INTO transcripts (video_id, full_text, segments)
            VALUES (?, ?, ?)
        """, (video_db_id, full_text, json.dumps(segments, ensure_ascii=False)))
        db.commit()
        row = db.execute(
            "SELECT id FROM transcripts WHERE video_id = ?", (video_db_id,)
        ).fetchone()
        return row["id"] if row else 0


def get_transcript(video_db_id: int) -> dict | None:
    """获取视频的转录文本"""
    with get_db() as db:
        row = db.execute(
            "SELECT full_text, segments, language, created_at FROM transcripts WHERE video_id = ?",
            (video_db_id,)
        ).fetchone()
        if row:
            d = dict(row)
            d["segments"] = json.loads(d["segments"]) if d["segments"] else []
            return d
        return None


# ─── Comments ────────────────────────────────────────────

def upsert_comment(video_db_id: int, comment: dict) -> int:
    """插入或更新一条评论。返回评论 id。"""
    with get_db() as db:
        db.execute("""
            INSERT INTO comments (video_id, comment_id, text, digg_count, reply_count,
                                  user_name, ip_label, create_time, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(video_id, comment_id) DO UPDATE SET
                text        = COALESCE(excluded.text, comments.text),
                digg_count  = excluded.digg_count,
                reply_count = excluded.reply_count,
                user_name   = COALESCE(excluded.user_name, comments.user_name),
                ip_label    = COALESCE(excluded.ip_label, comments.ip_label),
                last_seen   = CURRENT_TIMESTAMP
        """, (
            video_db_id,
            comment["comment_id"],
            comment.get("text"),
            comment.get("digg_count", 0),
            comment.get("reply_count", 0),
            comment.get("user_name"),
            comment.get("ip_label"),
            comment.get("create_time"),
        ))
        db.commit()
        row = db.execute(
            "SELECT id FROM comments WHERE video_id = ? AND comment_id = ?",
            (video_db_id, comment["comment_id"]),
        ).fetchone()
        return row["id"] if row else 0


def list_comments(video_db_id: int, order_by: str = "digg_count DESC", limit: int = 200) -> list[dict]:
    """查询指定视频的评论列表。"""
    with get_db() as db:
        rows = db.execute(f"""
            SELECT id, comment_id, text, digg_count, reply_count,
                   user_name, ip_label, create_time, first_seen, last_seen
            FROM comments
            WHERE video_id = ?
            ORDER BY {order_by}
            LIMIT ?
        """, (video_db_id, limit)).fetchall()
        return [dict(r) for r in rows]


def get_comment_count(video_db_id: int) -> int:
    """获取指定视频的评论数。"""
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) AS cnt FROM comments WHERE video_id = ?",
            (video_db_id,),
        ).fetchone()
        return row["cnt"] if row else 0


def delete_absent_comments(video_db_id: int, active_comment_ids: list[str]):
    """删除不再出现在 API 中的评论（已被作者删除）。"""
    if not active_comment_ids:
        return
    with get_db() as db:
        placeholders = ",".join("?" for _ in active_comment_ids)
        db.execute(f"""
            DELETE FROM comments
            WHERE video_id = ? AND comment_id NOT IN ({placeholders})
        """, (video_db_id, *active_comment_ids))
        db.commit()