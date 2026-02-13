import json
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request

from categorizer import categorize_page
from scraper import scrape_site

app = FastAPI(title="Website Scraper API")


@app.get("/")
def health():
    return {"status": "ok"}

MAX_OUTPUT_BYTES = 7800
MAX_HEADINGS = 5
MAX_TEXT = 150
MAX_META = 120
MAX_TITLE = 80


def _truncate(s: str, maxlen: int) -> str:
    if len(s) <= maxlen:
        return s
    return s[: maxlen - 3] + "..."


def _compact_page(page: dict) -> dict:
    path = urlparse(page["url"]).path or "/"
    return {
        "p": path,
        "cat": page["category"],
        "t": _truncate(page["title"], MAX_TITLE),
        "d": _truncate(page["meta_description"], MAX_META),
        "h1": _truncate(page["h1"], MAX_TITLE),
        "h": [_truncate(h, 60) for h in page["headings"][:MAX_HEADINGS]],
        "txt": _truncate(page["text_preview"], MAX_TEXT),
        "img": page["images_count"],
        "lnk": page["links_count"],
    }


@app.post("/scrape")
async def scrape(request: Request):
    # Handle both proper JSON and double-serialized string from Clay
    body = await request.body()
    try:
        data = json.loads(body)
        if isinstance(data, str):
            data = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    url = data.get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Missing 'url' field")

    try:
        raw = scrape_site(url)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Scraping failed: {exc}") from exc

    # Categorize each page
    for page in raw["pages"]:
        page["category"] = categorize_page(page["url"], page)

    # Deduplicate: keep one page per category, allow multiple "other"
    seen_cats: dict[str, dict] = {}
    unique_pages: list[dict] = []
    for p in raw["pages"]:
        cat = p["category"]
        if cat not in seen_cats:
            seen_cats[cat] = p
            unique_pages.append(p)
        elif cat == "other":
            unique_pages.append(p)

    categories_found = sorted({p["category"] for p in unique_pages})
    compact_pages = [_compact_page(p) for p in unique_pages]

    result = {
        "url": url if url.startswith("http") else f"https://{url}",
        "domain": raw["domain"],
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n": len(compact_pages),
        "pages": compact_pages,
        "summary": {
            "cats": categories_found,
            "pricing": "pricing" in categories_found,
            "blog": "blog" in categories_found,
        },
    }

    # Trim "other" pages if over budget
    output = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    while len(output) > MAX_OUTPUT_BYTES and result["pages"]:
        removed = False
        for i in range(len(result["pages"]) - 1, -1, -1):
            if result["pages"][i]["cat"] == "other":
                result["pages"].pop(i)
                result["n"] = len(result["pages"])
                removed = True
                break
        if not removed:
            result["pages"].pop()
            result["n"] = len(result["pages"])
        output = json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    return result


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
