"""
Xiaohongshu (小红书) collected/liked notes scraper using Playwright.
Strategy: intercept API responses + fallback DOM scraping.
"""
import asyncio


def _run_scraper(on_status):
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    try:
        from playwright_stealth import stealth_sync
    except ImportError:
        stealth_sync = None

    api_intercepted = []
    dom_collected = []

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

        # ── intercept note list API responses ──
        def handle_response(response):
            url = response.url
            if not any(k in url for k in [
                "user/liked", "note/collect", "user_collect",
                "user/posted", "homefeed", "explore", "note/feed",
                "note_list", "user_notes",
            ]):
                return
            try:
                body = response.json()
                notes = (
                    body.get("data", {}).get("notes") or
                    body.get("data", {}).get("items") or
                    body.get("notes") or
                    body.get("items") or []
                )
                for note in notes:
                    nc = note.get("note_card") or note
                    title = nc.get("display_title") or nc.get("title") or ""
                    desc = nc.get("desc") or ""
                    text = f"{title} {desc}".strip()
                    if text and len(text) > 2:
                        api_intercepted.append(text[:200])
            except Exception:
                pass

        page.on("response", handle_response)

        # ── Step 1: open xiaohongshu ──
        on_status({"step": "open", "msg": "打开小红书，请扫码登录..."})
        page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded")

        # ── Step 2: wait for login ──
        on_status({"step": "waiting", "msg": "等待扫码登录（最多2分钟）..."})
        try:
            page.wait_for_function(
                """() => {
                    const hasCookie = document.cookie.includes('web_session')
                                   || document.cookie.includes('a1');
                    const hasAvatar = document.querySelectorAll(
                        'img[class*="avatar"], [class*="Avatar"], .user-avatar'
                    ).length > 0;
                    const noLoginPanel = !document.querySelector('.login-container')
                                      && !document.querySelector('[class*="loginBox"]')
                                      && !document.querySelector('[class*="login-box"]');
                    return hasCookie || (hasAvatar && noLoginPanel);
                }""",
                timeout=120_000,
            )
        except PWTimeout:
            on_status({"step": "error", "msg": "登录超时，请重试"})
            browser.close()
            return []

        page.wait_for_timeout(2500)
        on_status({"step": "logged_in", "msg": "登录成功，正在跳转到收藏..."})

        # ── Step 3: navigate to collections ──
        # Method A: click profile avatar
        profile_reached = False
        for avatar_sel in [
            '[class*="avatar-wrapper"]',
            '[class*="userAvatar"]',
            'header img[class*="ava"]',
            '.header-right img',
        ]:
            try:
                page.locator(avatar_sel).first.click(timeout=4000)
                page.wait_for_timeout(1500)
                profile_reached = True
                break
            except Exception:
                continue

        # Method B: direct URL (works after login)
        if not profile_reached:
            try:
                page.goto("https://www.xiaohongshu.com/user/profile/me",
                          wait_until="domcontentloaded", timeout=10000)
                page.wait_for_timeout(2000)
                profile_reached = True
            except Exception:
                pass

        # Try clicking 收藏 tab
        if profile_reached:
            for tab_sel in ['text=收藏', '[data-tab="collect"]', '[class*="tab"]:has-text("收藏")']:
                try:
                    page.locator(tab_sel).first.click(timeout=4000)
                    page.wait_for_timeout(2000)
                    break
                except Exception:
                    continue

        on_status({"step": "scraping", "msg": "滚动收集笔记内容..."})

        # ── Step 4: scroll + DOM collect ──
        seen = set()

        def collect_dom():
            selectors = [
                'section.note-item',
                '[class*="note-item"]',
                '[class*="NoteItem"]',
                '[class*="noteItem"]',
                '[class*="feedItem"]',
                '[class*="FeedItem"]',
            ]
            found = []
            for sel in selectors:
                cards = page.query_selector_all(sel)
                if not cards:
                    continue
                for card in cards:
                    try:
                        text = card.inner_text().strip()
                        if text and len(text) > 3 and text not in seen:
                            seen.add(text)
                            found.append(text[:200])
                    except Exception:
                        pass
                if found:
                    break
            return found

        for _ in range(8):
            dom_collected.extend(collect_dom())
            if len(dom_collected) >= 50:
                break
            page.evaluate("window.scrollBy(0, 1000)")
            page.wait_for_timeout(1200)

        all_notes = api_intercepted if api_intercepted else dom_collected
        on_status({"step": "done_scraping",
                   "msg": f"收集到 {len(all_notes)} 条笔记，分析中..."})
        browser.close()

    return all_notes


async def scrape_xhs_notes(on_status):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run_scraper, on_status)


ANALYZE_PROMPT = """以下是用户在小红书收藏/点赞的笔记标题和描述（共{n}条）：

{samples}

请分析这些内容，推断出用户的信息茧房（平时关注的领域、兴趣、内容偏好）。

用5-8个关键词或短语描述茧房，用顿号分隔，例如：
"穿搭、美妆、旅行博主、生活方式、家居布置"

只返回关键词描述，不要其他内容。"""


def analyze_bubble(notes: list[str], client) -> str:
    if not notes:
        return ""
    samples = "\n".join(f"- {n}" for n in notes[:60])
    prompt = ANALYZE_PROMPT.format(n=len(notes), samples=samples)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text.strip().strip('"').strip("'")
