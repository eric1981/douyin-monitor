"""评论区采集模块 — Playwright 拦截 + 翻页"""
import logging
import re
import time
import random
from pathlib import Path

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

COMMENT_API_PATTERN = re.compile(r"/aweme/v1/web/comment/list/")


class CommentSpider:
    """评论区采集器。复用 DouyinSpider 的持久化浏览器会话。"""

    def __init__(self, headless: bool | None = None):
        from config_manager import load_config, get
        cfg = load_config()
        self.headless = headless if headless is not None else cfg["spider"]["headless"]
        self.page_count = cfg["comments"]["page_count"]
        self.filter_digg_min = cfg["comments"]["filter_digg_min"]
        self.filter_reply_min = cfg["comments"]["filter_reply_min"]
        self.session_dir = Path(get("paths.session_dir", str(Path(__file__).parent / "douyin_session")))

    async def fetch_comments(
        self, video_id: str, max_pages: int = 3, max_total: int = 30
    ) -> list[dict]:
        """
        抓取一个抖音视频的评论。

        返回 dict 列表，每个 dict 含：
            comment_id, text, digg_count, reply_count, user_name, ip_label, create_time
        """
        comments: list[dict] = []
        seen_ids: set[str] = set()

        logger.info("fetch_comments: 开始 video_id=%s", video_id)
        logger.info("  playwright 导入成功，准备启动浏览器")

        async with async_playwright() as p:
            logger.info("  playwright 上下文已创建")
            from config_manager import load_config
            cfg = load_config()
            s = cfg["spider"]
            self.session_dir.mkdir(parents=True, exist_ok=True)
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(self.session_dir),
                headless=self.headless,
                viewport={"width": s["viewport_width"], "height": s["viewport_height"]},
                user_agent=s["user_agent"],
                locale=s["locale"],
            )
            page = context.pages[0] if context.pages else await context.new_page()

            # ── 收集拦截到的响应 ──
            collected: list[dict] = []

            async def on_response(resp):
                if not COMMENT_API_PATTERN.search(resp.url):
                    return
                try:
                    body = await resp.json()
                except Exception:
                    return
                if isinstance(body, dict) and "comments" in body:
                    collected.append(body)

            page.on("response", on_response)

            # 导航到视频页
            vid_url = f"https://www.douyin.com/video/{video_id}"
            await page.goto(vid_url, wait_until="domcontentloaded", timeout=30000)
            logger.info("导航到 %s", vid_url)
            await page.wait_for_timeout(random.randint(2000, 3500))

            # 等待评论首次加载（页面自动触发）
            await self._wait_for_comments(page, collected, timeout=12)

            # ── 翻页循环 ──
            page_count = 0
            has_more = True

            while has_more and len(comments) < max_total and page_count < max_pages:
                if not collected:
                    break
                data = collected.pop(0)
                page_count += 1

                parsed = self._parse_comments(data)
                for c in parsed:
                    if c["comment_id"] not in seen_ids and self._pass_filter(c):
                        seen_ids.add(c["comment_id"])
                        comments.append(c)

                has_more = bool(data.get("has_more", 0))
                cursor = data.get("cursor", 0)

                logger.info(
                    "  第 %d 页: 拉取 %d 条, 过滤后 %d 条, has_more=%s",
                    page_count, len(data.get("comments", [])),
                    len(parsed), has_more,
                )

                if has_more and len(comments) < max_total and page_count < max_pages:
                    await page.wait_for_timeout(random.randint(800, 1500))
                    fetched = await self._fetch_via_page(
                        page, video_id, cursor, self.page_count
                    )
                    if fetched and "comments" in fetched:
                        collected.append(fetched)
                    else:
                        logger.warning("翻页 fetch 失败，终止翻页")
                        break

            await context.close()

        return comments

    # ── 内部方法 ──────────────────────────────────────────

    async def _wait_for_comments(
        self, page, collected: list[dict], timeout: int = 12
    ) -> None:
        """等待评论 API 返回数据。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if collected:
                return
            try:
                await page.wait_for_timeout(800)
            except Exception:
                break
        logger.warning("等待评论超时 (%ds)", timeout)

    async def _fetch_via_page(
        self, page, video_id: str, cursor: int, count: int
    ) -> dict | None:
        """在浏览器上下文内用 fetch() 翻页，携带已有 cookies 和安全参数。"""
        script = f"""
        (async () => {{
            const url = new URL('/aweme/v1/web/comment/list/', location.origin);
            url.searchParams.set('device_platform', 'webapp');
            url.searchParams.set('aid', '6383');
            url.searchParams.set('channel', 'channel_pc_web');
            url.searchParams.set('aweme_id', '{video_id}');
            url.searchParams.set('cursor', '{cursor}');
            url.searchParams.set('count', '{count}');
            url.searchParams.set('item_type', '0');
            try {{
                const resp = await fetch(url.toString(), {{credentials: 'include'}});
                return await resp.json();
            }} catch(e) {{
                return {{error: e.message}};
            }}
        }})()
        """
        try:
            result = await page.evaluate(script)
            if isinstance(result, dict) and result.get("error"):
                logger.warning("fetch 翻页失败: %s", result["error"])
                return None
            return result
        except Exception as exc:
            logger.warning("fetch 翻页异常: %s", exc)
            return None

    def _parse_comments(self, data: dict) -> list[dict]:
        """将 API 原始评论转为统一 dict。"""
        results = []
        for raw in data.get("comments", []):
            user = raw.get("user") or {}
            results.append({
                "comment_id": str(raw.get("cid", "")),
                "text": raw.get("text", "") or "",
                "digg_count": raw.get("digg_count", 0) or 0,
                "reply_count": raw.get("reply_comment_total", 0) or 0,
                "user_name": user.get("nickname", "") or "",
                "ip_label": raw.get("ip_label", "") or "",
                "create_time": raw.get("create_time", 0) or 0,
            })
        return results

    def _pass_filter(self, c: dict) -> bool:
        """过滤规则：digg_count >= N AND reply_count >= N（可配置）"""
        return c["digg_count"] >= self.filter_digg_min and c["reply_count"] >= self.filter_reply_min
