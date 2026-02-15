import json
import re
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request

from categorizer import categorize_page
from scraper import scrape_site

app = FastAPI(title="Website Scraper API")

MAX_OUTPUT_BYTES = 7800

# Priority order for page categories (lower index = higher priority)
_CATEGORY_PRIORITY = [
    "home", "product", "pricing", "about", "api", "security",
    "faq", "case-study", "partners", "investors", "careers",
    "blog", "press", "contact", "legal", "other",
]


def _priority(category: str) -> int:
    try:
        return _CATEGORY_PRIORITY.index(category)
    except ValueError:
        return len(_CATEGORY_PRIORITY)


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences."""
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s.strip() for s in sentences if s.strip()]


def _sentence_overlap(a: str, b: str) -> float:
    """Return word overlap ratio between two sentences."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    return len(intersection) / min(len(words_a), len(words_b))


def _extract_best_summary(page: dict) -> str:
    """Pick the best description source, then add unique body sentences."""
    sd = page.get("structured_data") or {}

    # Best single-source description (priority: meta > og > schema > h1)
    best = (
        page.get("meta_description")
        or sd.get("og_description")
        or sd.get("schema_description")
        or page.get("h1")
        or ""
    ).strip()

    if not best:
        # Fallback: first 2 sentences of body text
        body_sentences = _split_sentences(page.get("text_preview") or "")
        return " ".join(body_sentences[:2])

    # Add unique sentences from body that aren't redundant with best
    body_sentences = _split_sentences(page.get("text_preview") or "")
    best_sentences = _split_sentences(best)
    unique_additions = []
    for sentence in body_sentences:
        if len(sentence) < 15:
            continue
        is_redundant = any(
            _sentence_overlap(sentence, existing) > 0.5
            for existing in best_sentences + unique_additions
        )
        if not is_redundant:
            unique_additions.append(sentence)
        if len(unique_additions) >= 3:
            break

    if unique_additions:
        return best + " " + " ".join(unique_additions)
    return best


def _deduplicate_text(existing_sentences: list[str], new_text: str) -> str:
    """Remove sentences from new_text that overlap with already-seen sentences."""
    new_sentences = _split_sentences(new_text)
    unique = []
    for sentence in new_sentences:
        is_dup = any(
            _sentence_overlap(sentence, seen) > 0.5
            for seen in existing_sentences
        )
        if not is_dup:
            unique.append(sentence)
            existing_sentences.append(sentence)
    return " ".join(unique)


def _extract_company_signals(pages: list[dict]) -> dict:
    """Extract tagline, product list, and boolean flags from all pages."""
    categories = {p["category"] for p in pages}

    # Tagline: best from homepage h1 or og_title
    tagline = ""
    for p in pages:
        if p["category"] == "home":
            sd = p.get("structured_data") or {}
            tagline = (
                p.get("h1")
                or sd.get("og_title")
                or p.get("meta_description")
                or ""
            ).strip()
            # Trim overly long taglines
            if len(tagline) > 120:
                sentences = _split_sentences(tagline)
                tagline = sentences[0] if sentences else tagline[:120]
            break

    # Products: collect h1/titles from product pages
    products = []
    for p in pages:
        if p["category"] == "product":
            name = p.get("h1") or p.get("title") or ""
            name = name.strip()
            if name and name not in products and len(name) < 80:
                products.append(name)

    # Site name from structured data
    site_name = ""
    for p in pages:
        sd = p.get("structured_data") or {}
        if sd.get("og_site_name"):
            site_name = sd["og_site_name"]
            break

    return {
        "tagline": tagline,
        "products": products,
        "site_name": site_name,
        "has_pricing": "pricing" in categories,
        "has_blog": "blog" in categories,
        "has_careers": "careers" in categories,
    }


def _build_page_line(page: dict, existing_sentences: list[str], budget: int) -> str:
    """Build a compact line for one page: CAT path\\nDeduplicated summary."""
    path = urlparse(page["url"]).path or "/"
    cat = page["category"].upper()

    summary = _extract_best_summary(page)
    summary = _deduplicate_text(existing_sentences, summary)

    # Add unique headings not already in summary
    headings = page.get("headings") or []
    unique_headings = []
    summary_lower = summary.lower()
    for h in headings[:8]:
        if h.lower() not in summary_lower and len(h) < 60:
            unique_headings.append(h)
    heading_str = ""
    if unique_headings:
        heading_str = " [" + " | ".join(unique_headings[:4]) + "]"

    line = f"{cat} {path}\n{summary}{heading_str}"

    # Trim to budget
    if len(line.encode("utf-8")) > budget:
        while len(line.encode("utf-8")) > budget and line:
            line = line[:len(line) - 50].rstrip() + "..."

    return line


def _build_output(domain: str, pages: list[dict]) -> str:
    """Assemble the final compact output."""
    signals = _extract_company_signals(pages)

    # Header
    header_lines = [f"# {domain}"]
    if signals["tagline"]:
        header_lines.append(f"Tagline: {signals['tagline']}")
    if signals["products"]:
        header_lines.append(f"Products: {', '.join(signals['products'][:6])}")

    flags = []
    flags.append(f"Pages: {len(pages)}")
    flags.append(f"Pricing: {'yes' if signals['has_pricing'] else 'no'}")
    flags.append(f"Blog: {'yes' if signals['has_blog'] else 'no'}")
    flags.append(f"Careers: {'yes' if signals['has_careers'] else 'no'}")
    header_lines.append(" | ".join(flags))

    header = "\n".join(header_lines)

    # Sort pages by priority
    pages_sorted = sorted(pages, key=lambda p: _priority(p["category"]))

    # Calculate per-page budget
    header_bytes = len(header.encode("utf-8")) + 20  # 20 for separator
    remaining = MAX_OUTPUT_BYTES - header_bytes
    page_count = len(pages_sorted)
    if page_count == 0:
        return header

    base_budget = remaining // page_count

    # Build page blocks with deduplication
    existing_sentences: list[str] = []
    page_blocks: list[str] = []

    for i, page in enumerate(pages_sorted):
        # Give more budget to high-priority pages
        if i < 3:
            budget = int(base_budget * 1.4)
        elif i < 6:
            budget = base_budget
        else:
            budget = int(base_budget * 0.7)

        block = _build_page_line(page, existing_sentences, budget)
        page_blocks.append(block)

    # Assemble
    content = header + "\n\n## Key Content\n" + "\n\n".join(page_blocks)

    # Progressive trimming if over budget
    while len(content.encode("utf-8")) > MAX_OUTPUT_BYTES and page_blocks:
        # Remove lowest priority page (last in sorted list)
        page_blocks.pop()
        content = header + "\n\n## Key Content\n" + "\n\n".join(page_blocks)

    return content


@app.api_route("/", methods=["GET", "HEAD"])
def health():
    return {"status": "ok"}


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

    # Allow multiple product pages, deduplicate other categories
    seen_cats: set[str] = set()
    unique_pages: list[dict] = []
    for p in raw["pages"]:
        cat = p["category"]
        if cat in ("product", "other"):
            unique_pages.append(p)
        elif cat not in seen_cats:
            seen_cats.add(cat)
            unique_pages.append(p)

    categories = sorted({p["category"] for p in unique_pages})

    content = _build_output(domain, unique_pages)

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
