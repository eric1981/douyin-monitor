"""抖音博主视频采集模块 - 滚动触发 + API 拦截 (可复用)"""
import logging
import asyncio
import random
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page

SESSION_DIR = Path(__file__).parent / "douyin_session"

logger = logging.getLogger(__name__)


@dataclass
class Profile:
    nickname: str
    avatar_url: str
    follower_count: int
    following_count: int
    total_likes: int
    bio: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Video:
    video_id: str
    title: str
    cover_url: str
    video_url: str
    duration_ms: int
    create_time: int
    like_count: int
    comment_count: int
    share_count: int
    view_count: int
    hashtags: list[str]
    fetched_at: str

    @property
    def public_url(self) -> str:
        """抖音公开播放链接 https://www.douyin.com/video/{video_id}"""
        if self.video_id:
            return f"https://www.douyin.com/video/{self.video_id}"
        return ""

    def to_dict(self) -> dict:
        return asdict(self)


class DouyinSpider:
    API_PATTERN = "/aweme/v1/web/aweme/post/"
    PROFILE_PATTERNS = [
        "/user/profile/other/",
        "/user/info/",
        "/aweme/v1/web/user/profile/",
        "/web/api/v2/user/info/",
        "/aweme/v1/web/im/user/info/",
    ]

    def __init__(self, headless: bool = True, max_scrolls: int = 80,
                 page_load_wait: int = 8, idle_limit: int = 20):
        self.headless = headless
        self.max_scrolls = max_scrolls
        self.page_load_wait = page_load_wait
        self.idle_limit = idle_limit
        self.videos: list[Video] = []
        self.profile: Profile | None = None
        self._seen_ids: set[str] = set()
        self._scroll_count = 0
        self._last_hit_scroll = 0
        self._stopped = False
        self._error: str | None = None

    @staticmethod
    def _parse_aweme(aweme: dict) -> Video:
        stats = aweme.get("statistics", {})
        vi = aweme.get("video", {})
        cover_list = vi.get("cover", {}).get("url_list", [])
        play_addr = vi.get("play_addr", {})
        hashtags = [e.get("hashtag_name", "") for e in aweme.get("text_extra", []) if e.get("hashtag_name")]
        return Video(
            video_id=str(aweme.get("aweme_id", "")),
            title=aweme.get("desc", ""),
            cover_url=cover_list[-1] if cover_list else "",
            video_url=play_addr.get("url_list", [""])[0] if play_addr else "",
            duration_ms=vi.get("duration", 0),
            create_time=aweme.get("create_time", 0),
            like_count=stats.get("digg_count", 0),
            comment_count=stats.get("comment_count", 0),
            share_count=stats.get("share_count", 0),
            view_count=stats.get("play_count", 0),
            hashtags=hashtags,
            fetched_at=datetime.now().isoformat(),
        )

    async def _on_response(self, response):
        if self.API_PATTERN not in response.url:
            return
        try:
            data = await response.json()
        except Exception:
            return

        aweme_list = data.get("aweme_list", [])
        new = 0
        for aweme in aweme_list:
            # Profile fallback: extract from first video's author
            if self.profile is None:
                author = aweme.get("author", {})
                if author:
                    avatar_list = (
                        author.get("avatar_medium", {}).get("url_list")
                        or author.get("avatar_thumb", {}).get("url_list")
                        or []
                    )
                    self.profile = Profile(
                        nickname=author.get("nickname", ""),
                        avatar_url=avatar_list[0] if avatar_list else "",
                        follower_count=author.get("follower_count", 0),
                        following_count=author.get("following_count", 0),
                        total_likes=author.get("total_favorited", 0),
                        bio=author.get("signature", ""),
                    )
                    logger.info("  [主页(fallback)] %s  粉丝:%s",
                                self.profile.nickname,
                                f"{self.profile.follower_count:,}")

            vid = str(aweme.get("aweme_id", ""))
            if vid and vid not in self._seen_ids:
                self._seen_ids.add(vid)
                self.videos.append(self._parse_aweme(aweme))
                new += 1

        gap = self._scroll_count - self._last_hit_scroll
        self._last_hit_scroll = self._scroll_count
        has_more = bool(data.get("has_more", False))
        logger.info("  [API] +%d/%d 条, 累计 %d, gap=%d %s",
                    new, len(aweme_list), len(self.videos), gap,
                    "(last page)" if not has_more else "")
        if not has_more and len(aweme_list) > 0:
            self._stopped = True

    async def _on_profile_response(self, response):
        if self.profile is not None:
            return  # already captured
        url = response.url
        if not any(p in url for p in self.PROFILE_PATTERNS):
            return
        try:
            data = await response.json()
        except Exception:
            return
        user = data.get("user", {})
        if not user:
            return
        avatar_list = user.get("avatar_medium", {}).get("url_list") or user.get("avatar_thumb", {}).get("url_list") or []
        self.profile = Profile(
            nickname=user.get("nickname", ""),
            avatar_url=avatar_list[0] if avatar_list else "",
            follower_count=user.get("follower_count", 0),
            following_count=user.get("following_count", 0),
            total_likes=user.get("total_favorited", 0),
            bio=user.get("signature", ""),
        )
        logger.info("  [主页] %s  粉丝:%s",
                    self.profile.nickname,
                    f"{self.profile.follower_count:,}")

    async def _scroll_naturally(self, page: Page):
        vp = page.viewport_size or {"width": 1920, "height": 1080}
        await page.mouse.move(vp["width"] // 2, vp["height"] - 200)
        for _ in range(random.randint(2, 4)):
            await page.mouse.wheel(0, random.randint(400, 900))
            await page.wait_for_timeout(random.randint(200, 500))
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(random.randint(600, 1200))

    async def fetch(self, sec_uid: str) -> list[Video]:
        self.videos = []
        self.profile = None
        self._seen_ids = set()
        self._scroll_count = 0
        self._last_hit_scroll = 0
        self._stopped = False
        self._error = None

        async with async_playwright() as p:
            SESSION_DIR.mkdir(parents=True, exist_ok=True)
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(SESSION_DIR),
                headless=self.headless,
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
            )
            page = await context.new_page()
            page.on("response", self._on_response)
            page.on("response", self._on_profile_response)

            url = f"https://www.douyin.com/user/{sec_uid}"
            logger.info("  [加载] %s", url)
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(self.page_load_wait)

            if not self.videos:
                self._error = "未收到首页数据(可能未登录或触发风控)"
                await context.close()
                return []

            for i in range(self.max_scrolls):
                if self._stopped:
                    break
                self._scroll_count += 1
                await self._scroll_naturally(page)
                idle = self._scroll_count - self._last_hit_scroll
                if idle >= self.idle_limit:
                    break

            await context.close()

        return self.videos