import os
import json
import httpx
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from anthropic import Anthropic
from dotenv import load_dotenv
from ddgs import DDGS

load_dotenv()

app = FastAPI()
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")

app.mount("/static", StaticFiles(directory="static"), name="static")


class BubbleRequest(BaseModel):
    bubble: str
    language: str = "zh"


OPPOSITE_DOMAINS_PROMPT = """用户描述了他们的信息茧房："{bubble}"

请推断出5个与这个茧房完全不同的内容领域，这些领域：
1. 与用户的茧房没有明显交集
2. 但可能存在深层的概念桥接（不是表面相似）
3. 涵盖不同的人群、文化圈、专业背景

返回JSON格式：
{{
  "domains": [
    {{"name": "领域名称", "search_query": "英文搜索词（适合用于搜索引擎）", "reason": "为什么这个领域与用户茧房相反"}}
  ]
}}

只返回JSON，不要其他文字。"""

SCORE_AND_EXPLAIN_PROMPT = """用户的信息茧房是："{bubble}"

以下是一篇来自"{domain}"领域的文章：
标题：{title}
摘要：{description}
链接：{url}

请判断这篇文章是否适合作为"反推荐"（即用户不会自己发现但有价值的内容），并生成解释。

返回JSON格式：
{{
  "score": 0-10（10=完美的反推荐，0=用户茧房内的内容），
  "worth_showing": true/false,
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


async def get_opposite_domains(bubble: str) -> list[dict]:
    prompt = OPPOSITE_DOMAINS_PROMPT.format(bubble=bubble)
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
                    json={"query": query, "limit": 5}
                )
                if resp.status_code == 200:
                    return resp.json().get("data", [])
        except Exception:
            pass

    loop = asyncio.get_event_loop()
    def ddg_search():
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
        return [{"title": r["title"], "url": r["href"], "description": r["body"]} for r in results]
    try:
        return await loop.run_in_executor(None, ddg_search)
    except Exception:
        return []


async def score_result(bubble: str, domain_name: str, result: dict) -> dict | None:
    title = result.get("title", "")
    description = result.get("description", "") or result.get("markdown", "")[:300]
    url = result.get("url", "")
    if not title or not url:
        return None

    prompt = SCORE_AND_EXPLAIN_PROMPT.format(
        bubble=bubble,
        domain=domain_name,
        title=title,
        description=description[:400],
        url=url
    )
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}]
        )
        scored = parse_json_response(response.content[0].text)
        if scored.get("worth_showing") and scored.get("score", 0) >= 6:
            return {
                "title": title,
                "url": url,
                "domain": domain_name,
                "score": scored["score"],
                "why_wont_find": scored["why_wont_find"],
                "bridge": scored["bridge"],
                "hook": scored["hook"],
            }
    except Exception:
        pass
    return None


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_recommend(bubble: str):
    # Phase 1: get domains
    yield sse("status", {"msg": "正在分析茧房结构..."})
    try:
        domains = await get_opposite_domains(bubble)
    except Exception as e:
        yield sse("error", {"msg": str(e)})
        return

    yield sse("domains", {"domains": [d["name"] for d in domains]})

    # Phase 2: for each domain, search then score immediately (pipeline per domain)
    seen_urls = set()
    emitted = 0

    async def process_domain(domain: dict):
        nonlocal emitted
        name = domain["name"]
        results = await search_content(domain["search_query"])
        tasks = [score_result(bubble, name, r) for r in results[:3]]
        scored_list = await asyncio.gather(*tasks, return_exceptions=True)
        items = []
        for s in scored_list:
            if s and not isinstance(s, Exception) and s["url"] not in seen_urls:
                seen_urls.add(s["url"])
                items.append(s)
        items.sort(key=lambda x: x["score"], reverse=True)
        return name, items

    # Run all domains concurrently but yield results as each domain finishes
    queue: asyncio.Queue = asyncio.Queue()

    async def worker(domain):
        result = await process_domain(domain)
        await queue.put(result)

    tasks = [asyncio.create_task(worker(d)) for d in domains]

    for _ in range(len(domains)):
        name, items = await queue.get()
        yield sse("searching", {"domain": name})
        for item in items:
            if emitted >= 8:
                break
            emitted += 1
            yield sse("card", item)

    await asyncio.gather(*tasks, return_exceptions=True)
    yield sse("done", {"total": emitted})


@app.get("/douyin-stream")
async def douyin_stream():
    from douyin_scraper import scrape_douyin_likes, analyze_bubble

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def on_status(msg: dict):
        asyncio.run_coroutine_threadsafe(queue.put(msg), loop)

    async def generate():
        # kick off scraper in background
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
        else:
            yield sse("error", {"msg": "未获取到数据，请重试"})

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
async def stream(bubble: str):
    if not bubble.strip():
        raise HTTPException(status_code=400, detail="bubble cannot be empty")
    return StreamingResponse(
        stream_recommend(bubble),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


@app.post("/recommend")
async def recommend(req: BubbleRequest):
    if not req.bubble.strip():
        raise HTTPException(status_code=400, detail="bubble cannot be empty")
    try:
        domains = await get_opposite_domains(req.bubble)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get domains: {e}")

    search_tasks = [search_content(d["search_query"]) for d in domains]
    search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

    score_tasks = []
    for domain, results in zip(domains, search_results):
        if isinstance(results, Exception) or not results:
            continue
        for r in results[:3]:
            score_tasks.append(score_result(req.bubble, domain["name"], r))

    scored = await asyncio.gather(*score_tasks, return_exceptions=True)
    items = [s for s in scored if s and not isinstance(s, Exception)]
    items.sort(key=lambda x: x["score"], reverse=True)

    return {
        "bubble": req.bubble,
        "domains_explored": [d["name"] for d in domains],
        "recommendations": items[:8]
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8767)
