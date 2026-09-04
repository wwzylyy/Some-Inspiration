# -*- coding: utf-8 -*-
import os
import json
import httpx
import asyncio
import re
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel
from anthropic import Anthropic
from dotenv import load_dotenv
from ddgs import DDGS
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup

load_dotenv()
import embedder

app = FastAPI()
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")

BUBBLE_FILE    = Path("saved_bubble.json")
DAILY_CACHE    = Path("daily_cache.json")
FEEDBACK_FILE  = Path("feedback.json")
CENTROID_FILE  = Path("bubble_centroid.npy")
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── bubble persistence ──
def load_bubble() -> str:
    if BUBBLE_FILE.exists():
        try: return json.loads(BUBBLE_FILE.read_text(encoding="utf-8")).get("bubble", "")
        except: pass
    return ""

def save_bubble_file(bubble: str):
    BUBBLE_FILE.write_text(json.dumps({"bubble": bubble}, ensure_ascii=False), encoding="utf-8")

# ── daily cache ──
def load_daily() -> dict:
    if DAILY_CACHE.exists():
        try: return json.loads(DAILY_CACHE.read_text(encoding="utf-8"))
        except: pass
    return {}

def save_daily(data: dict):
    DAILY_CACHE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# ── feedback ──
def load_feedback() -> dict:
    if FEEDBACK_FILE.exists():
        try: return json.loads(FEEDBACK_FILE.read_text(encoding="utf-8"))
        except: pass
    return {"domains": {}}

def save_feedback(data: dict):
    FEEDBACK_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def feedback_hint_for_domain(domain_name: str) -> str:
    fb = load_feedback()
    d = fb["domains"].get(domain_name)
    if not d:
        return ""
    reads = d.get("reads", 0)
    skips = d.get("skips", 0)
    liked = "（注意：用户历史上对“" + domain_name + "”领域感兴趣，请提高评分。）"
    skip_hint = "（注意：用户对“" + domain_name + "”领域兴趣不大，请更换角度。）"
    if reads >= 2 and reads > skips:
        return "\n" + liked
    if skips >= 3 and skips > reads * 2:
        return "\n" + skip_hint
    return ""


# ── prompts ──
OPPOSITE_DOMAINS_PROMPT = """用户描述了他们的信息茧房："{bubble}"
{explored_hint}
请推断出5个与这个茧房完全不同的内容领域，这些领域：
1. 与用户的茧房没有明显交集
2. 但可能存在深层的概念桥接（不是表面相似）
3. 涵盖不同的人群、文化圈、专业背景

返回JSON格式（search_query必须是中文搜索词，用于在知乎/36kr/简书等中文平台搜索）：
{{
  "domains": [
    {{"name": "领域名称", "search_query": "中文搜索词", "reason": "为什么这个领域与用户茧房相反"}}
  ]
}}

只返回JSON，不要其他文字。"""

SCORE_AND_EXPLAIN_PROMPT = """用户的信息茧房是："{bubble}"
{feedback_hint}
以下是一篇来自"{domain}"领域的文章：
标题：{title}
正文节选：
{content}

请为这篇文章生成反推荐说明（语言为中文）。

返回JSON格式：
{{
  "relevant": true/false（文章是否有实质内容，非广告/无意义/404页面）,
  "why_wont_find": "用户为什么不会主动发现这篇（1-2句）",
  "bridge": "这篇文章与用户关注领域之间隐藏的桥梁是什么（1-2句）",
  "hook": "一句吸引用户点击的推荐语"
}}

只返回JSON，不要其他文字。"""


def parse_json_response(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0].strip()
    return json.loads(text)


async def get_opposite_domains(bubble: str, explored: list[str] = []) -> list[dict]:
    explored_hint = ""
    if explored:
        explored_hint = f"\n注意：以下领域用户已经探索过，请避免重复：{', '.join(explored)}\n"
    prompt = OPPOSITE_DOMAINS_PROMPT.format(bubble=bubble, explored_hint=explored_hint)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    data = parse_json_response(response.content[0].text)
    return data["domains"]


async def search_content(query: str) -> list[dict]:
    if FIRECRAWL_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=15.0) as http:
                resp = await http.post(
                    "https://api.firecrawl.dev/v1/search",
                    headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}"},
                    json={"query": query, "limit": 10}
                )
                if resp.status_code == 200:
                    return resp.json().get("data", [])
        except Exception:
            pass

    loop = asyncio.get_event_loop()
    def ddg_search():
        # Search Chinese content: zhihu, 36kr, jianshu, weixin articles
        cn_query = f"{query} site:zhihu.com OR site:36kr.com OR site:jianshu.com OR site:mp.weixin.qq.com"
        with DDGS() as ddgs:
            results = list(ddgs.text(cn_query, max_results=10))
        # fallback: plain query if no results
        if not results:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=8))
        return [{"title": r["title"], "url": r["href"], "description": r["body"]} for r in results]
    try:
        return await loop.run_in_executor(None, ddg_search)
    except Exception:
        return []


async def fetch_article_content(url: str) -> str:
    """Fetch and extract main text from article URL."""
    # Try Firecrawl first (best quality)
    if FIRECRAWL_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=12.0) as http:
                resp = await http.post(
                    "https://api.firecrawl.dev/v1/scrape",
                    headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}"},
                    json={"url": url, "formats": ["markdown"]}
                )
                if resp.status_code == 200:
                    md = resp.json().get("data", {}).get("markdown", "")
                    if md:
                        return md[:3000]
        except Exception:
            pass

    # Fallback: httpx + BeautifulSoup
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; bot/1.0)"}
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as http:
            resp = await http.get(url, headers=headers)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                    tag.decompose()
                text = soup.get_text(separator=" ", strip=True)
                text = re.sub(r'\s+', ' ', text)
                return text[:3000]
    except Exception:
        pass
    return ""


async def score_result(bubble: str, domain_name: str, result: dict, bubble_emb=None) -> dict | None:
    title = result.get("title", "")
    url   = result.get("url", "")
    if not title or not url:
        return None

    article_text = await fetch_article_content(url)
    if not article_text:
        article_text = result.get("description", "") or result.get("markdown", "")

    # ── Embedding-based surprise score ──
    text_for_embed = f"{title}. {article_text[:600]}" if article_text else title
    loop = asyncio.get_event_loop()
    article_emb = await loop.run_in_executor(
        None, lambda: embedder.embed([text_for_embed])[0]
    )
    score = embedder.surprise_score(bubble_emb, article_emb) if bubble_emb is not None else 5

    # Filter only near-identical articles (cosine sim > 0.97 ≈ score 0)
    if score < 1:
        return None

    # ── Claude generates qualitative explanations only (no scoring) ──
    content_for_prompt = article_text[:2500] if article_text else f"(仅标题: {title})"
    prompt = SCORE_AND_EXPLAIN_PROMPT.format(
        bubble=bubble,
        domain=domain_name,
        title=title,
        content=content_for_prompt,
        feedback_hint=feedback_hint_for_domain(domain_name),
    )
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        explained = parse_json_response(response.content[0].text)
        if not explained.get("relevant", True):
            return None
        return {
            "title":         title,
            "url":           url,
            "domain":        domain_name,
            "score":         score,
            "why_wont_find": explained["why_wont_find"],
            "bridge":        explained["bridge"],
            "hook":          explained["hook"],
            "_emb":          article_emb,   # internal — stripped before SSE emit
        }
    except Exception:
        pass
    return None


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_recommend(bubble: str, explored: list[str] = []):
    loop = asyncio.get_event_loop()

    # Prefer stored centroid (history-derived) over fresh text embedding
    stored = embedder.load_centroid(str(CENTROID_FILE))
    if stored is not None:
        bubble_emb = stored
        yield sse("status", {"msg": "正在加载茧房向量画像..."})
    else:
        yield sse("status", {"msg": "正在计算茧房语义向量..."})
        bubble_emb = await loop.run_in_executor(
            None, lambda: embedder.embed([bubble])[0]
        )

    yield sse("status", {"msg": "正在生成反向领域..."})
    try:
        domains = await get_opposite_domains(bubble, explored)
    except Exception as e:
        yield sse("error", {"msg": str(e)})
        return

    yield sse("domains", {"domains": [d["name"] for d in domains]})

    seen_urls = set()

    async def process_domain(domain: dict):
        name = domain["name"]
        results = await search_content(domain["search_query"])
        tasks = [score_result(bubble, name, r, bubble_emb) for r in results[:8]]
        scored_list = await asyncio.gather(*tasks, return_exceptions=True)
        items = []
        for s in scored_list:
            if s and not isinstance(s, Exception) and s["url"] not in seen_urls:
                seen_urls.add(s["url"])
                items.append(s)
        items.sort(key=lambda x: x["score"], reverse=True)
        return name, items

    queue: asyncio.Queue = asyncio.Queue()

    async def worker(domain):
        result = await process_domain(domain)
        await queue.put(result)

    worker_tasks = [asyncio.create_task(worker(d)) for d in domains]

    # Collect all results (stream "searching" events as each domain finishes)
    all_items = []
    for _ in range(len(domains)):
        name, items = await queue.get()
        yield sse("searching", {"domain": name})
        all_items.extend(items)

    await asyncio.gather(*worker_tasks, return_exceptions=True)

    # MMR diversity selection — pick 8 articles spread across semantic space
    final_items = embedder.mmr_select(bubble_emb, all_items, k=8)

    # Hard minimum: if MMR returns fewer than 3, pad with highest-scored items
    if len(final_items) < 3 and len(all_items) >= 3:
        seen = {id(x) for x in final_items}
        extras = [x for x in sorted(all_items, key=lambda x: x['score'], reverse=True)
                  if id(x) not in seen]
        final_items.extend(extras[:3 - len(final_items)])

    # PCA 2D coords — attach pca_x/pca_y to each item, get bubble coords
    viz_coords = embedder.prepare_viz_data(bubble_emb, final_items)
    if viz_coords:
        yield sse("viz_data", viz_coords)

    # Emit cards (strip internal _emb field before sending)
    for item in final_items:
        item.pop("_emb", None)
        yield sse("card", item)

    yield sse("done", {"total": len(final_items)})


# ── daily cache job ──
async def collect_recommend(bubble: str) -> list[dict]:
    """Run stream_recommend and collect card items."""
    items = []
    async for chunk in stream_recommend(bubble):
        if chunk.startswith("event: card"):
            m = re.search(r'data: (.+)', chunk)
            if m:
                try: items.append(json.loads(m.group(1)))
                except: pass
    return items


async def daily_digest_job():
    bubble = load_bubble()
    if not bubble:
        print("[daily] no saved bubble, skipping")
        return
    print(f"[daily] generating digest for: {bubble}")
    items = await collect_recommend(bubble)
    if items:
        import datetime
        save_daily({"bubble": bubble, "date": str(datetime.date.today()), "items": items})
        print(f"[daily] cached {len(items)} items")


# ── scheduler ──
scheduler = AsyncIOScheduler()
scheduler.add_job(daily_digest_job, "cron", hour=8, minute=0)

@app.on_event("startup")
async def startup():
    scheduler.start()
    # Preload embedding model in background thread (downloads ~400MB on first run)
    import threading
    threading.Thread(target=embedder.get_model, daemon=True).start()

@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()


# ── routes ──
@app.get("/douyin-stream")
async def douyin_stream():
    from douyin_scraper import scrape_douyin_likes, analyze_bubble

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def on_status(msg: dict):
        asyncio.run_coroutine_threadsafe(queue.put(msg), loop)

    async def generate():
        task = asyncio.create_task(scrape_douyin_likes(on_status))
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=130.0)
            except asyncio.TimeoutError:
                yield sse("error", {"msg": "操作超时"})
                break
            yield sse("status", msg)
            if msg.get("step") in ("done_scraping", "error"):
                break
        titles = await task
        if titles:
            bubble = await asyncio.get_event_loop().run_in_executor(
                None, analyze_bubble, titles, client
            )
            yield sse("bubble", {"bubble": bubble, "count": len(titles)})
            # Build centroid from scraped titles in background
            def _build():
                c = embedder.compute_centroid(titles[:200])
                embedder.save_centroid(c, str(CENTROID_FILE))
                print(f"[centroid] built from {len(titles)} douyin titles")
            asyncio.get_event_loop().run_in_executor(None, _build)
        else:
            yield sse("error", {"msg": "未获取到数据，请重试"})
        yield sse("done", {})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/xhs-stream")
async def xhs_stream():
    from xhs_scraper import scrape_xhs_notes, analyze_bubble

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def on_status(msg: dict):
        asyncio.run_coroutine_threadsafe(queue.put(msg), loop)

    async def generate():
        task = asyncio.create_task(scrape_xhs_notes(on_status))
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=130.0)
            except asyncio.TimeoutError:
                yield sse("error", {"msg": "操作超时"})
                break
            yield sse("status", msg)
            if msg.get("step") in ("done_scraping", "error"):
                break
        notes = await task
        if notes:
            bubble = await asyncio.get_event_loop().run_in_executor(
                None, analyze_bubble, notes, client
            )
            yield sse("bubble", {"bubble": bubble, "count": len(notes)})
            # Build centroid from scraped notes in background
            def _build():
                c = embedder.compute_centroid(notes[:200])
                embedder.save_centroid(c, str(CENTROID_FILE))
                print(f"[centroid] built from {len(notes)} xhs notes")
            asyncio.get_event_loop().run_in_executor(None, _build)
        else:
            yield sse("error", {"msg": "未获取到笔记，请重试"})
        yield sse("done", {})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/stream")
async def stream(bubble: str, explored: str = ""):
    if not bubble.strip():
        raise HTTPException(status_code=400, detail="bubble cannot be empty")
    explored_list = [e.strip() for e in explored.split(",") if e.strip()] if explored else []
    return StreamingResponse(
        stream_recommend(bubble, explored_list),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class SaveBubbleRequest(BaseModel):
    bubble: str

@app.post("/save-bubble")
async def save_bubble(req: SaveBubbleRequest):
    if not req.bubble.strip():
        raise HTTPException(status_code=400, detail="茧房描述不能为空")
    save_bubble_file(req.bubble.strip())
    return {"status": "ok", "msg": "已保存，每天早上8点自动生成今日推送"}

@app.get("/daily")
async def get_daily():
    data = load_daily()
    return JSONResponse(data)

@app.post("/daily/refresh")
async def refresh_daily():
    bubble = load_bubble()
    if not bubble:
        raise HTTPException(status_code=400, detail="尚未保存茧房描述")
    asyncio.create_task(daily_digest_job())
    return {"status": "ok", "msg": "正在后台刷新，稍后刷新页面查看"}


class FeedbackRequest(BaseModel):
    url: str
    domain: str
    action: str       # "read" | "skip"
    title: str = ""   # article title — used to drift centroid on "read"

@app.post("/feedback")
async def post_feedback(req: FeedbackRequest):
    data = load_feedback()
    d = data["domains"].setdefault(req.domain, {"reads": 0, "skips": 0})
    if req.action == "read":
        d["reads"] += 1
    else:
        d["skips"] += 1
    save_feedback(data)

    # Drift centroid toward the article the user just read
    if req.action == "read" and req.title and CENTROID_FILE.exists():
        loop = asyncio.get_event_loop()
        title = req.title
        def _drift():
            current = embedder.load_centroid(str(CENTROID_FILE))
            if current is None:
                return
            new_emb = embedder.embed([title])[0]
            updated = embedder.update_centroid(current, new_emb, decay=0.92)
            embedder.save_centroid(updated, str(CENTROID_FILE))
        loop.run_in_executor(None, _drift)

    return {"status": "ok"}

@app.get("/feedback")
async def get_feedback():
    return JSONResponse(load_feedback())


class HistoryRequest(BaseModel):
    history: list[dict]  # [{url, title, visitCount}]

@app.post("/import-history")
async def import_history(req: HistoryRequest):
    skip_prefixes = ("chrome://", "chrome-extension://", "localhost", "127.0.0.1",
                     "file://", "about:", "data:")
    entries = []
    for h in req.history[:300]:
        title = h.get("title", "").strip()
        url   = h.get("url", "")
        if title and not any(url.startswith(p) for p in skip_prefixes):
            entries.append(f"{title}")
    if not entries:
        raise HTTPException(status_code=400, detail="没有可用的历史记录")

    samples = "\n".join(f"- {e}" for e in entries[:100])
    prompt = f"""以下是用户最近的浏览历史标题（共{len(entries)}条）：

{samples}

请分析用户的信息茧房，用5-8个关键词描述，顿号分隔。只返回关键词，不要其他内容。"""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )
    bubble = response.content[0].text.strip().strip('"').strip("'")

    # Build centroid from all history titles (background, non-blocking)
    loop = asyncio.get_event_loop()
    titles_for_centroid = entries[:200]
    def _build_centroid():
        centroid = embedder.compute_centroid(titles_for_centroid)
        embedder.save_centroid(centroid, str(CENTROID_FILE))
        print(f"[centroid] built from {len(titles_for_centroid)} history titles")
    loop.run_in_executor(None, _build_centroid)

    return {"bubble": bubble, "count": len(entries)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8768)
