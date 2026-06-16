"""视频语音转录模块 — 下载视频 → 提取音频 → Whisper 转文本

核心函数 get_whisper / find_ffmpeg / extract_audio / transcribe
直接复用 marketing-tools (server.py)；下载用 yt-dlp（无需浏览器拦截）

依赖: faster-whisper, ffmpeg (系统), playwright, yt-dlp, requests, zhconv
"""
import os
import re
import json
import shutil
import subprocess
import tempfile
import logging
import time
from pathlib import Path

import requests
import zhconv

# ── Cookie 来源路径 ──────────────────────────────────────────
# 旧硬编码路径（兼容已有部署，保留为 fallback）
_COOKIE_JSON_LEGACY = os.path.expandvars(
    r"%USERPROFILE%\Desktop\social-monitor\social-auto-upload\cookies\douyin_benxian1.json"
)


def _get_cookie_import_path() -> str | None:
    """获取外部 Cookie JSON 文件路径。

    优先级：
      1. config.yaml 中的 transcriber.cookie_import_path
      2. 旧硬编码路径（向后兼容）
      3. None — 不导入外部 cookies
    """
    # 1. 从配置读取
    try:
        import yaml
        config_path = BASE_DIR / "config.yaml"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            path = cfg.get("transcriber", {}).get("cookie_import_path", "")
            if path:
                expanded = os.path.expandvars(path)
                if Path(expanded).exists():
                    return expanded
                logger.warning("配置的 cookie_import_path 不存在: %s", expanded)
    except Exception:
        pass

    # 2. 回退到旧硬编码路径
    legacy = Path(_COOKIE_JSON_LEGACY)
    if legacy.exists():
        return str(legacy)

    return None


def _ensure_cookies():
    """将外部 Cookie JSON 复制到 Playwright 会话目录（如果有配置）。"""
    out = SESSION_DIR / "douyin_cookies.json"
    if out.exists():
        return
    src = _get_cookie_import_path()
    if src:
        import shutil
        shutil.copy2(str(src), str(out))
        logger.info("已导入 cookies: %s", Path(src).name)
    else:
        logger.info("未配置外部 cookie 文件，将使用 Playwright 持久化会话中的登录态")


def _inject_cookies_to_context(ctx):
    """将 JSON 中的 douyin cookies 注入 Playwright context。"""
    import json as _json
    src = SESSION_DIR / "douyin_cookies.json"
    if not src.exists():
        return
    with open(str(src), encoding="utf-8") as f:
        data = _json.load(f)
    count = 0
    for c in data.get("cookies", []):
        domain = c.get("domain", "")
        if "douyin" not in domain and "bytedance" not in domain:
            continue
        try:
            ctx.add_cookies([{
                "name": c["name"],
                "value": c["value"],
                "domain": domain,
                "path": c.get("path", "/"),
                "secure": c.get("secure", False),
                "httpOnly": c.get("httpOnly", False),
                "sameSite": c.get("sameSite", "Lax"),
                "expires": c.get("expires", -1),
            }])
            count += 1
        except Exception:
            pass
    logger.info("cookies 已注入 Playwright context（%d 条）", count)


# ── 在 import faster-whisper 之前检查模型缓存 ──────────────────
# huggingface_hub 在模块加载时就会初始化 Xet 客户端并联网检查版本，
# 必须在 import 之前设 HF_HUB_OFFLINE=1 才能跳过。
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
MODEL_DIR = DATA_DIR / "models"
_MODEL_CACHED = any(MODEL_DIR.iterdir()) if MODEL_DIR.is_dir() else False
if _MODEL_CACHED:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

logger = logging.getLogger(__name__)

try:
    from faster_whisper import WhisperModel
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False

SESSION_DIR = BASE_DIR / "douyin_session"
os.makedirs(MODEL_DIR, exist_ok=True)

WHISPER_MODEL = None

# 有效的 faster-whisper 模型名白名单（防止无效配置导致崩溃）
_VALID_MODELS = {
    "tiny", "tiny.en", "base", "base.en", "small", "small.en",
    "medium", "medium.en", "large-v1", "large-v2", "large-v3",
    "large-v3-turbo", "distil-small.en", "distil-medium.en",
    "distil-large-v2", "distil-large-v3",
}


def _load_transcriber_config() -> tuple[str, str]:
    """从 config.yaml 读取转录模型配置，返回 (model, device)"""
    model = "small"
    device = "cpu"
    try:
        import yaml
        config_path = BASE_DIR / "config.yaml"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            tc = cfg.get("transcriber", {})
            m = tc.get("model", "small")
            d = tc.get("device", "cpu")
            if m in _VALID_MODELS:
                model = m
            else:
                logger.warning("无效模型名 '%s'，回退到 %s", m, model)
            if d in ("cpu", "cuda", "auto"):
                device = d
            else:
                logger.warning("无效设备 '%s'，回退到 %s", d, device)
    except Exception:
        pass
    return model, device


def get_whisper():
    global WHISPER_MODEL
    if WHISPER_MODEL is None and WHISPER_AVAILABLE:
        global _MODEL_CACHED
        model_name, device = _load_transcriber_config()
        # 量化策略: CPU → int8（快）, CUDA → float16, auto → 默认
        if device == "cpu":
            compute_type = "int8"
        elif device == "cuda":
            compute_type = "float16"
        else:
            compute_type = "default"
        WHISPER_MODEL = WhisperModel(
            model_name, device=device, compute_type=compute_type,
            download_root=str(MODEL_DIR),
        )
        if not _MODEL_CACHED:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            _MODEL_CACHED = True
        logger.info("Whisper 模型已加载: %s (device=%s, compute=%s)",
                    model_name, device, compute_type)
    return WHISPER_MODEL


def find_ffmpeg():
    for p in ["ffmpeg", "ffmpeg.exe",
              os.path.expanduser("~/.local/bin/ffmpeg"),
              "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"]:
        if shutil.which(p) or os.path.exists(p):
            return p
    return "ffmpeg"


_ffmpeg = find_ffmpeg()
if not shutil.which(_ffmpeg) and not os.path.exists(_ffmpeg):
    raise RuntimeError("未找到 ffmpeg。Windows: winget install ffmpeg 或 https://ffmpeg.org/download.html")

FFMPEG_PATH = _ffmpeg


def extract_audio(video_path, audio_path):
    cmd = [
        FFMPEG_PATH, "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le",
        "-ar", "16000", "-ac", "1",
        audio_path,
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=300, check=True)
        return audio_path
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise RuntimeError(f"ffmpeg 提取音频失败: {e}")


def transcribe(audio_path):
    model = get_whisper()
    if not model:
        return [{"text": "Whisper 模型未加载", "start": 0, "end": 0}]
    segments, info = model.transcribe(audio_path, beam_size=5, language="zh")
    result = []
    for seg in segments:
        text = zhconv.convert(seg.text.strip(), "zh-cn")
        result.append({"text": text, "start": round(seg.start, 2), "end": round(seg.end, 2)})
    return result


# ─── 浏览器指纹池（每个会话不同，避免被抖音关联）─────────────

_UA_POOL = [
    # Chrome 120-126, Windows 10/11
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]

_VP_POOL = [
    {"width": 1920, "height": 1080},
    {"width": 1920, "height": 1040},
    {"width": 1600, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
]

_LANG_POOL = ["zh-CN", "zh-CN", "zh-CN", "zh-CN", "zh-TW"]


def _pick_fingerprint(session_dir):
    """根据会话路径 hash 确定性选取指纹，同一会话始终相同"""
    h = hash(str(session_dir))
    ua = _UA_POOL[h % len(_UA_POOL)]
    vp = _VP_POOL[h % len(_VP_POOL)]
    lang = _LANG_POOL[h % len(_LANG_POOL)]
    return ua, vp, lang


# ─── 视频 URL 抓取器 ───────────────────────────────────────

class VideoFetcher:
    """持有一个 Playwright 浏览器实例，批量获取视频 CDN 链接"""

    def __init__(self, session_dir=None):
        self._playwright = None
        self._context = None
        self._session_dir = session_dir or SESSION_DIR

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()
        self._session_dir.mkdir(parents=True, exist_ok=True)
        _ua, _vp, _lang = _pick_fingerprint(self._session_dir)
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._session_dir), headless=True,
            viewport=_vp, user_agent=_ua, locale=_lang,
        )
        # 注入已有 cookies（douyin_benxian1.json）
        _ensure_cookies()
        _inject_cookies_to_context(self._context)
        return self

    def __exit__(self, *args):
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass

    def fetch_url(self, video_id):
        """打开视频页面，从 video 元素提取 CDN URL"""
        page = self._context.new_page()
        video_url = None
        try:
            page.goto(f"https://www.douyin.com/video/{video_id}",
                      wait_until="domcontentloaded", timeout=30000)
            # 等待视频元素出现
            try:
                page.wait_for_selector("video", timeout=15000)
                page.wait_for_timeout(3000)  # 等视频源加载
                video_url = page.eval_on_selector(
                    "video", "el => el.getAttribute('src') || el.querySelector('source')?.getAttribute('src')"
                )
            except Exception:
                pass
            # 如果 video 元素提取失败，退回到网络拦截
            if not video_url:
                page.goto(f"https://www.douyin.com/video/{video_id}",
                          wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(10000)
                video_url = page.evaluate("""
                    () => {
                        const v = document.querySelector('video');
                        if (v && v.src) return v.src;
                        const s = v?.querySelector('source');
                        if (s && s.src) return s.src;
                        return null;
                    }
                """)
        finally:
            page.close()

        if not video_url:
            raise RuntimeError("未获取到视频 CDN 地址")
        return video_url


# ─── 下载 ────────────────────────────────────────────────────

def download_video(fetcher, video_id, output_path):
    """下载抖音视频：从 video 元素取 CDN URL → requests 下载"""
    page = fetcher._context.new_page()
    try:
        page.goto(f"https://www.douyin.com/video/{video_id}",
                  wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_selector("video", timeout=15000)
            page.wait_for_timeout(3000)
        except Exception:
            pass

        # 取原始 CDN src（getAttribute 不受 MSE 篡改影响）
        src = page.evaluate("""
            () => {
                const v = document.querySelector('video');
                if (!v) return null;
                return v.getAttribute('src') ||
                       (v.querySelector('source') || {}).getAttribute('src') ||
                       null;
            }
        """)

        if not src:
            raise RuntimeError("未找到视频 CDN 地址")

        logger.info("视频 CDN: %s...", src[:80])

        if src.startswith("blob:"):
            _download_blob(page, src, output_path)
        else:
            _download_http(src, output_path)
    finally:
        try:
            page.close()
        except Exception:
            pass


def _download_http(url: str, output_path: str):
    headers = {"User-Agent": "Mozilla/5.0 Chrome/120", "Referer": "https://www.douyin.com/"}
    resp = requests.get(url, headers=headers, stream=True, timeout=120)
    resp.raise_for_status()
    expected = int(resp.headers.get("content-length", 0))
    n = 0
    with open(output_path, "wb") as f:
        for c in resp.iter_content(65536):
            f.write(c)
            n += len(c)
    if expected and n < expected * 0.95:
        raise RuntimeError(f"下载不完整: {n/1048576:.1f}/{expected/1048576:.1f} MB")
    logger.info("下载完成: %.1f MB", n / 1048576)


def _download_blob(page, blob_url: str, output_path: str):
    import base64
    r = page.evaluate("""
        async (url) => {
            try {
                const resp = await fetch(url);
                if (!resp.ok) return {error: 'HTTP ' + resp.status};
                const buf = await resp.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let bin = '';
                for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
                return {data: btoa(bin), size: bytes.length};
            } catch(e) { return {error: e.message}; }
        }
    """, blob_url)
    if r.get("error"):
        raise RuntimeError(f"blob 下载失败: {r['error']}")
    data = base64.b64decode(r["data"])
    with open(output_path, "wb") as f:
        f.write(data)
    logger.info("blob 下载完成: %.1f MB", len(data) / 1048576)


# ─── 完整流水线 ─────────────────────────────────────────────

def transcribe_pending_videos(fetcher, limit: int = 20):
    """转录所有待处理的视频（已下载但未转录的）"""
    from db import get_db
    with get_db() as db:
        pending = db.execute("""
            SELECT v.video_id, v.id FROM videos v
            LEFT JOIN transcripts t ON t.video_id = v.id
            WHERE t.id IS NULL ORDER BY v.id DESC LIMIT ?
        """, (limit,)).fetchall()
    if not pending:
        logger.info("没有待转录的视频")
        return 0
    count = 0
    for row in pending:
        try:
            if process_video(fetcher, row["video_id"], row["id"]):
                count += 1
        except Exception as e:
            logger.error("转录失败 %s: %s", row["video_id"], e)
    return count


def process_video(fetcher, video_id, video_db_id):
    """完整流水线：下载 → 提取音频 → 转录 → 入库"""
    from db import get_db
    with get_db() as db:
        if db.execute("SELECT id FROM transcripts WHERE video_id=?", (video_db_id,)).fetchone():
            logger.info("视频 %d 已转录，跳过", video_db_id)
            return None

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        vp = d / "v.mp4"
        ap = d / "a.wav"
        logger.info("下载: %s", video_id)
        download_video(fetcher, video_id, str(vp))
        logger.info("提取音频...")
        try:
            extract_audio(str(vp), str(ap))
        except RuntimeError:
            logger.warning("ffmpeg 失败，重试下载+提取...")
            download_video(fetcher, video_id, str(vp))
            extract_audio(str(vp), str(ap))
        logger.info("Whisper 转录中...")
        segs = transcribe(str(ap))
        txt = "\n".join(s["text"] for s in segs)
        from db import save_transcript
        save_transcript(video_db_id, txt, segs)
        logger.info("转录完成: %d 段 %d 字", len(segs), len(txt))
        return {"full_text": txt, "segments": segs}
