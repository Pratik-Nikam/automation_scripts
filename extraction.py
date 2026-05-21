"""
Confluence Scraper using Playwright (Python, Headless Mode)

Install dependencies:
    pip install playwright markdownify
    playwright install chromium

Run:
    python confluence_scraper.py

Credentials via environment variables:
    export CONFLUENCE_EMAIL="you@company.com"
    export CONFLUENCE_TOKEN="your-atlassian-api-token"   # Cloud
    export CONFLUENCE_PASSWORD="yourpassword"              # Self-hosted / SSO
"""

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page, BrowserContext

# ── Try to import markdownify (optional but recommended) ─────────────────────
try:
    from markdownify import markdownify as md_convert
    HAS_MARKDOWNIFY = True
except ImportError:
    HAS_MARKDOWNIFY = False

# ─── CONFIG ──────────────────────────────────────────────────────────────────

BASE_URL      = "https://your-domain.atlassian.net"   # no trailing slash
EMAIL         = os.getenv("CONFLUENCE_EMAIL", "")
API_TOKEN     = os.getenv("CONFLUENCE_TOKEN", "")     # Atlassian Cloud
PASSWORD      = os.getenv("CONFLUENCE_PASSWORD", "")  # self-hosted fallback
HEADLESS      = True
OUTPUT_DIR    = Path("./confluence_output")
OUTPUT_FORMAT = "both"   # "json" | "markdown" | "both"
CONCURRENCY   = 3        # parallel pages
PAGE_DELAY    = 0.5      # seconds between batches

# CSS selectors — adjust if your Confluence version differs
SELECTORS = {
    "login_email":    "#username",
    "login_password": "#password",
    "login_submit":   "#login-submit, [type='submit']",
    "page_title":     "#title-text, h1[data-testid='page-title'], .page-title, h1",
    "page_content":   "#main-content, .wiki-content, [data-testid='page-content']",
    "breadcrumb":     "#breadcrumb-section a, nav[aria-label='breadcrumbs'] a",
    "labels":         ".labels-list .label, [data-testid='label']",
    "last_modified":  "time[datetime]",
    "author":         ".author, [data-testid='page-byline-author']",
}

# ─── URLS TO SCRAPE ───────────────────────────────────────────────────────────

URLS = [
    "https://your-domain.atlassian.net/wiki/spaces/ENG/pages/123456/Page+Title",
    "https://your-domain.atlassian.net/wiki/spaces/HR/pages/789012/Another+Page",
    # Add more URLs here...
]

# ─── DATA MODEL ───────────────────────────────────────────────────────────────

@dataclass
class PageResult:
    url: str
    scraped_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    title: Optional[str] = None
    content_text: Optional[str] = None
    content_html: Optional[str] = None
    content_markdown: Optional[str] = None
    breadcrumb: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    author: Optional[str] = None
    last_modified: Optional[str] = None
    error: Optional[str] = None

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-")[:80]


def html_to_markdown(html: str) -> str:
    """Convert HTML to Markdown. Uses markdownify if available, else basic regex."""
    if HAS_MARKDOWNIFY:
        return md_convert(html, heading_style="ATX", bullets="-").strip()

    # Fallback: basic regex conversion
    md = re.sub(r"<h([1-6])[^>]*>(.*?)</h\1>", lambda m: "\n" + "#" * int(m.group(1)) + " " + re.sub(r"<[^>]+>", "", m.group(2)) + "\n", html, flags=re.DOTALL | re.IGNORECASE)
    md = re.sub(r"<(strong|b)[^>]*>(.*?)</\1>", r"**\2**", md, flags=re.IGNORECASE | re.DOTALL)
    md = re.sub(r"<(em|i)[^>]*>(.*?)</\1>", r"_\2_", md, flags=re.IGNORECASE | re.DOTALL)
    md = re.sub(r"<code[^>]*>(.*?)</code>", r"`\1`", md, flags=re.IGNORECASE | re.DOTALL)
    md = re.sub(r"<pre[^>]*>(.*?)</pre>", r"```\n\1\n```", md, flags=re.IGNORECASE | re.DOTALL)
    md = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r"[\2](\1)", md, flags=re.IGNORECASE | re.DOTALL)
    md = re.sub(r"<li[^>]*>(.*?)</li>", r"- \1\n", md, flags=re.IGNORECASE | re.DOTALL)
    md = re.sub(r"<br\s*/?>", "\n", md, flags=re.IGNORECASE)
    md = re.sub(r"<p[^>]*>(.*?)</p>", r"\1\n\n", md, flags=re.IGNORECASE | re.DOTALL)
    md = re.sub(r"<[^>]+>", "", md)
    for entity, char in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")]:
        md = md.replace(entity, char)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()

# ─── LOGIN ────────────────────────────────────────────────────────────────────

async def login(page: Page) -> None:
    print("🔐 Logging in to Confluence...")
    await page.goto(BASE_URL, wait_until="networkidle")

    # Atlassian Cloud: email field → submit → password field → submit
    email_field = await page.query_selector(SELECTORS["login_email"])
    if email_field:
        await email_field.fill(EMAIL)
        submit = await page.query_selector(SELECTORS["login_submit"])
        if submit:
            await submit.click()
        await page.wait_for_timeout(1200)

        password_field = await page.query_selector(SELECTORS["login_password"])
        if password_field:
            await password_field.fill(API_TOKEN or PASSWORD)
            submit2 = await page.query_selector(SELECTORS["login_submit"])
            if submit2:
                await submit2.click()
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass

    # Check login
    logged_in = await page.query_selector(".aui-header-logo, [data-testid='navigation'], #header")
    if logged_in:
        print("✅ Login successful")
    else:
        print("⚠️  Login may have failed — continuing (might be SSO or already logged in)")

# ─── SCRAPE A SINGLE PAGE ─────────────────────────────────────────────────────

async def scrape_page(page: Page, url: str) -> PageResult:
    result = PageResult(url=url)
    print(f"  📄 {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(800)  # let JS settle

        # Title
        title_el = await page.query_selector(SELECTORS["page_title"])
        result.title = (await title_el.inner_text()).strip() if title_el else None

        # Content
        content_el = await page.query_selector(SELECTORS["page_content"])
        if content_el:
            result.content_text = (await content_el.inner_text()).strip()
            result.content_html = await content_el.inner_html()
            result.content_markdown = html_to_markdown(result.content_html)

        # Breadcrumb
        breadcrumb_els = await page.query_selector_all(SELECTORS["breadcrumb"])
        result.breadcrumb = [
            (await el.inner_text()).strip()
            for el in breadcrumb_els
            if (await el.inner_text()).strip()
        ]

        # Labels
        label_els = await page.query_selector_all(SELECTORS["labels"])
        result.labels = [
            (await el.inner_text()).strip()
            for el in label_els
            if (await el.inner_text()).strip()
        ]

        # Last modified
        time_el = await page.query_selector(SELECTORS["last_modified"])
        if time_el:
            result.last_modified = await time_el.get_attribute("datetime")

        # Author
        author_el = await page.query_selector(SELECTORS["author"])
        if author_el:
            result.author = (await author_el.inner_text()).strip()

    except Exception as e:
        result.error = str(e)
        print(f"  ❌ Error: {e}")

    return result

# ─── SAVE OUTPUT ─────────────────────────────────────────────────────────────

def save_results(results: list[PageResult]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if OUTPUT_FORMAT in ("json", "both"):
        json_path = OUTPUT_DIR / "all_pages.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in results], f, indent=2, ensure_ascii=False)
        print(f"\n💾 JSON → {json_path}")

    if OUTPUT_FORMAT in ("markdown", "both"):
        for page in results:
            if page.error:
                continue
            filename = slugify(page.title or page.url) + ".md"
            md_path = OUTPUT_DIR / filename
            lines = [
                f"# {page.title or 'Untitled'}",
                "",
                f"**URL:** {page.url}",
            ]
            if page.author:
                lines.append(f"**Author:** {page.author}")
            if page.last_modified:
                lines.append(f"**Last Modified:** {page.last_modified}")
            if page.breadcrumb:
                lines.append(f"**Path:** {' > '.join(page.breadcrumb)}")
            if page.labels:
                lines.append(f"**Labels:** {', '.join(page.labels)}")
            lines += ["", "---", "", page.content_markdown or page.content_text or "_No content extracted_"]
            md_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"📝 Markdown files → {OUTPUT_DIR}/")

# ─── MAIN ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        context: BrowserContext = await browser.new_context(
            user_agent="Mozilla/5.0 (compatible; ConfluenceScraper/1.0)",
            viewport={"width": 1280, "height": 800},
        )

        # Login once on a shared page
        login_page = await context.new_page()
        await login(login_page)
        await login_page.close()

        results: list[PageResult] = []
        queue = list(URLS)
        total = len(queue)
        processed = 0

        print(f"\n🚀 Scraping {total} pages (concurrency={CONCURRENCY})...\n")

        while queue:
            batch = queue[:CONCURRENCY]
            queue = queue[CONCURRENCY:]

            pages = [await context.new_page() for _ in batch]
            batch_results = await asyncio.gather(
                *[scrape_page(p, url) for p, url in zip(pages, batch)]
            )
            for p in pages:
                await p.close()

            results.extend(batch_results)
            processed += len(batch)
            print(f"  ✓ {processed}/{total} done")

            if queue:
                await asyncio.sleep(PAGE_DELAY)

        await browser.close()

    save_results(results)

    succeeded = sum(1 for r in results if not r.error)
    failed    = sum(1 for r in results if r.error)
    print(f"\n✅ Complete — {succeeded} succeeded, {failed} failed")
    if failed:
        print("Failed URLs:")
        for r in results:
            if r.error:
                print(f"  - {r.url}: {r.error}")


if __name__ == "__main__":
    asyncio.run(main())