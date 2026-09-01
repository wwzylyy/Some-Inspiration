"""
Douyin liked-video scraper using Playwright.
Strategy: intercept API responses for liked-video list + fallback DOM scraping.
"""
import asyncio
import json
import re


def _run_scraper(on_status):
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    try:
        from playwright_stealth import stealth_sync
    except ImportError:
        stealth_sync = None

    collected = []          # list of title strings
    api_intercepted = []    # captured from network

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--window-size=1200,850", "--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            viewport={"width": 1200, "height": 850},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
        )
        page = ctx.new_page()
        if stealth_sync:
            stealth_sync(page)

        # ── intercept liked-video API responses ──
        def handle_response(response):
            url = response.url
            if "aweme/v1/web/aweme/favorite" in url or "favorite/video/list" in url:
                try:
                    body = response.json()
                    items = (
                        body.get("aweme_list") or
                        body.get("data", {}).get("aweme_list") or []
                    )
                    for item in items:
                        desc = item.get("desc", "")
                        tags = " ".join(
                            f"#{t['hashtag_name']}"
                            for t in item.get("text_extra", [])
                            if t.get("hashtag_name")
                        )
                        text = f"{desc} {tags}".strip()
                        if text:
                            api_intercepted.append(text[:200])
                except Exception:
                    pass

        page.on("response", handle_response)

        # ── Step 1: open douyin ──
        on_status({"step": "open", "msg": "打开抖音，请扫码登录..."})
        page.goto("https://www.douyin.com", wait_until="domcontentloaded")

        # ── Step 2: wait for login ──
        on_status({"step": "waiting", "msg": "等待扫码登录（最多2分钟）..."})
        try:
            page.wait_for_function(
                """() => {
                    const hasCookie = document.cookie.includes('passport_csrf_token')
                                   || document.cookie.includes('sid_guard')
                                   || document.cookie.includes('uid_tt');
                    const hasAvatar = !!document.querySelector('[data-e2e="header-login-button"]') === false
                                   && document.querySelectorAll('img[class*="avatar"], img[class*="Avatar"]').length > 0;
                    const noLoginModal = !document.querySelector('[data-e2e="login-modal"]')
                                      && !document.querySelector('.login-container');
                    return hasCookie || hasAvatar || noLoginModal;
                }""",
                timeout=120_000,
            )
        except PWTimeout:
            on_status({"step": "error", "msg": "登录超时，请重试"})
            browser.close()
            return []

        # Extra wait to let the page settle
        page.wait_for_timeout(2000)
        on_status({"step": "logged_in", "msg": "登录成功，正在跳转到点赞列表..."})

        # ── Step 3: navigate to liked videos ──
        # Try clicking profile avatar → profile page → 喜欢 tab
        profile_reached = False

        # Method A: direct URL with /user/self redirect
        try:
            page.goto("https://www.douyin.com/user/self", wait_until="domcontentloaded", timeout=10_000)
            page.wait_for_timeout(2000)
            profile_reached = True
        except Exception:
            pass

        # Method B: click the avatar in header
        if not profile_reached:
            try:
                avatar = page.locator(
                    '[data-e2e="header-avatar"], '
                    'header img[class*="avatar"], '
                    'header img[class*="Avatar"], '
                    '.header-right img'
                ).first
                avatar.click(timeout=5_000)
                page.wait_for_load_state("domcontentloaded", timeout=8_000)
                profile_reached = True
            except Exception:
                pass

        # Click 喜欢 tab on profile page
        if profile_reached:
            try:
                like_tab = page.locator(
                    'text=喜欢, '
                    '[data-e2e="user-tab-like"], '
                    '.tab-item:has-text("喜欢")'
                ).first
                like_tab.click(timeout=5_000)
                page.wait_for_timeout(2000)
            except Exception:
                pass  # tab click failed, still try scraping below

        on_status({"step": "scraping", "msg": "滚动收集点赞数据..."})

        # ── Step 4: scroll and collect via DOM ──
        seen = set()

        def collect_from_dom():
            # Try multiple selectors for video cards
            selectors = [
                '[data-e2e="user-post-item"]',
                '[class*="videoCard"]',
                '[class*="VideoCard"]',
                '[class*="video-card"]',
                'li[class*="video"]',
                '[class*="feedItem"]',
            ]
            found = []
            for sel in selectors:
                cards = page.query_selector_all(sel)
                if cards:
                    for card in cards:
                        try:
                            text = card.inner_text().strip()
                            if text and len(text) > 3 and text not in seen:
                                seen.add(text)
                                found.append(text[:200])
                        except Exception:
                            pass
                    if found:
                        break  # stop at first working selector
            return found

        for i in range(8):
            items = collect_from_dom()
            collected.extend(items)
            if len(collected) >= 50:
                break
            page.evaluate("window.scrollBy(0, 1000)")
            page.wait_for_timeout(1200)

        # ── Step 5: merge API-intercepted data (usually better quality) ──
        all_titles = api_intercepted if api_intercepted else collected
        on_status({"step": "done_scraping",
                   "msg": f"收集到 {len(all_titles)} 条点赞记录，分析中..."})
        browser.close()

    return all_titles


async def scrape_douyin_likes(on_status):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run_scraper, on_status)


ANALYZE_PROMPT = """以下是用户在抖音上点赞的视频标题和话题（共{n}条）：

{samples}

请分析这些内容，推断出用户的信息茧房（他们平时关注的领域、兴趣、内容偏好）。

用5-8个关键词或短语描述茧房，用顿号分隔，例如：
"搞笑段子、美食探店、科技数码、运动健身"

只返回关键词描述，不要其他内容。"""


def analyze_bubble(titles: list[str], client) -> str:
    if not titles:
        return ""
    samples = "\n".join(f"- {t}" for t in titles[:60])
    prompt = ANALYZE_PROMPT.format(n=len(titles), samples=samples)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text.strip().strip('"').strip("'")
