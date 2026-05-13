"""抖音 URL 解析工具 — 提取 sec_uid，支持短链接重定向"""
import asyncio
import re
import urllib.request
import urllib.error


_DOUYIN_URL_RE = re.compile(r"https?://(?:www\.|v\.)?douyin\.com/\S+")
_USER_RE = re.compile(r"/user/([A-Za-z0-9_-]+)")
_SECUID_RE = re.compile(r"^MS4w[A-Za-z0-9_-]{20,}$")


def resolve_secuid(raw: str) -> tuple[str | None, str | None]:
    """从用户输入中提取抖音 sec_uid。

    支持输入格式：
      - 纯 sec_uid: MS4wLjABAAAA...
      - 主页链接: https://www.douyin.com/user/MS4wLj...
      - 短链接: https://v.douyin.com/xxxxx/
      - 分享文案: "长按复制... https://v.douyin.com/xxxxx/"

    Returns:
        (sec_uid, error_message)
        - 成功时 error_message 为 None
        - 失败时 sec_uid 为 None
    """
    raw = raw.strip()

    # 1. 已经是纯 sec_uid
    if _SECUID_RE.match(raw):
        return raw, None

    # 2. 从文本中提取 URL（处理"分享文案 + URL"的场景）
    if not raw.startswith("http"):
        m = _DOUYIN_URL_RE.search(raw)
        if m:
            raw = m.group(0)
        else:
            return None, "未找到抖音链接或 sec_uid，请检查输入"

    # 3. 已经是标准 /user/ 链接
    m = _USER_RE.search(raw)
    if m:
        return m.group(1), None

    # 4. 短链接 (v.douyin.com) — 跟踪重定向
    if "v.douyin.com" in raw or "douyin.com" in raw:
        try:
            req = urllib.request.Request(raw, method="HEAD")
            req.add_header(
                "User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                final_url = resp.geturl()
            m = _USER_RE.search(final_url)
            if m:
                return m.group(1), None
        except urllib.error.URLError as e:
            return None, f"短链接解析失败: {e.reason}"
        except Exception as e:
            return None, f"短链接解析失败: {e}"

    return None, "无法从链接提取博主ID，请使用包含 /user/ 的主页链接或直接粘贴 sec_uid"


async def async_resolve_secuid(raw: str) -> tuple[str | None, str | None]:
    """resolve_secuid 的异步版本。

    将同步的 resolve_secuid 放到线程池执行，避免阻塞事件循环。
    适用于 FastAPI 等异步上下文。

    Returns:
        (sec_uid, error_message) — 同 resolve_secuid
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, resolve_secuid, raw)