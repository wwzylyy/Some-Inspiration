import os
import json
import httpx
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
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
    bubble: str  # e.g. "AI, 科技, 创业, 中国互联网"
    language: str = "zh"  # zh or en


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
    # Prefer Firecrawl if key available, fallback to DuckDuckGo (free)
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

    # DuckDuckGo fallback — runs in thread pool to avoid blocking
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


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.post("/recommend")
async def recommend(req: BubbleRequest):
    if not req.bubble.strip():
        raise HTTPException(status_code=400, detail="bubble cannot be empty")

    # Step 1: get opposite domains
    try:
        domains = await get_opposite_domains(req.bubble)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get domains: {e}")

    # Step 2: search each domain in parallel
    search_tasks = [search_content(d["search_query"]) for d in domains]
    search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

    # Step 3: score each result
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
    uvicorn.run("app:app", host="0.0.0.0", port=8765, reload=True)
