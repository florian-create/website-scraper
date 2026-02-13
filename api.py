import json
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request

from categorizer import categorize_page
from scraper import scrape_site

app = FastAPI(title="Website Scraper API")

MAX_OUTPUT_BYTES = 7800
MAX_TEXT_PER_PAGE = 300


@app.api_route("/", methods=["GET", "HEAD"])
def health():
    return {"status": "ok"}


def _build_page_block(page: dict) -> str:
    """Build a readable text block for one page."""
    path = urlparse(page["url"]).path or "/"
    cat = page["category"]
    lines = [f"## [{cat.upper()}] {page['title']}"]
    lines.append(f"Path: {path}")

    if page["meta_description"]:
        lines.append(f"Description: {page['meta_description']}")

    if page["h1"]:
        lines.append(f"H1: {page['h1']}")

    headings = [h for h in page["headings"][:6] if h]
    if headings:
        lines.append("Sections: " + " | ".join(headings))

    if page["text_preview"]:
        text = page["text_preview"][:MAX_TEXT_PER_PAGE]
        if len(page["text_preview"]) > MAX_TEXT_PER_PAGE:
            text += "..."
        lines.append(f"Content: {text}")

    return "\n".join(lines)


@app.post("/scrape")
async def scrape(request: Request):
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

    full_url = url if url.startswith("http") else f"https://{url}"
    domain = urlparse(full_url).netloc

    # Always return 200
    try:
        raw = scrape_site(url)
    except Exception as exc:
        return {
            "domain": domain,
            "error": str(exc),
            "content": f"# {domain}\n\nError: could not scrape this website.",
        }

    # Categorize
    for page in raw["pages"]:
        page["category"] = categorize_page(page["url"], page)

    # Deduplicate: one page per category, allow multiple "other"
    seen_cats: set[str] = set()
    unique_pages: list[dict] = []
    for p in raw["pages"]:
        cat = p["category"]
        if cat not in seen_cats:
            seen_cats.add(cat)
            unique_pages.append(p)
        elif cat == "other":
            unique_pages.append(p)

    categories = sorted({p["category"] for p in unique_pages})

    # Build single text output
    header = f"# {domain}"
    header += f"\nCategories: {', '.join(categories)}"
    header += f"\nPages found: {len(unique_pages)}"
    header += f"\nHas pricing: {'yes' if 'pricing' in categories else 'no'}"
    header += f"\nHas blog: {'yes' if 'blog' in categories else 'no'}"
    header += "\n"

    blocks = [header]
    for p in unique_pages:
        blocks.append(_build_page_block(p))

    content = "\n\n".join(blocks)

    # Trim from the end if over budget
    if len(content.encode("utf-8")) > MAX_OUTPUT_BYTES:
        while blocks and len("\n\n".join(blocks).encode("utf-8")) > MAX_OUTPUT_BYTES:
            # Remove last "other" block first (index > 0 to keep header)
            removed = False
            for i in range(len(blocks) - 1, 0, -1):
                if "[OTHER]" in blocks[i]:
                    blocks.pop(i)
                    removed = True
                    break
            if not removed and len(blocks) > 1:
                blocks.pop()
        content = "\n\n".join(blocks)

    return {
        "domain": domain,
        "categories": categories,
        "has_pricing": "pricing" in categories,
        "has_blog": "blog" in categories,
        "page_count": len(unique_pages),
        "content": content,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
