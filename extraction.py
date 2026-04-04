"""
extraction.py
=============
RUN PHASE -- execute after install.py has set up all portals.

For each installed domain:
  Stage 1 -- NAVIGATE  : call search_<domain>(page, query)  [from search_engines.py]
  Stage 2 -- EXTRACT   : call extract_<domain>(html, base_url) [from extraction_portals.py]
                         Returns [{title, url, date, tag}, ...]
  Stage 3 -- CRAWL     : walk up to MAX_PAGES of pagination
  Stage 4 -- FILTER    : keep only articles from last DATE_WINDOW_DAYS days
  Stage 5 -- SCRAPE    : call article_<domain>(html)  [from extraction_portals.py]
                         Returns {title, date, author, body_text, tags}
  Stage 6 -- SAVE      : extraction_output/{domain}_results.json


Reads:
  search_registry.json     -- domain -> search_url template + search_fn name
  search_engines.py        -- search_<domain>(page, query) functions
  extractor_registry.json  -- domain -> extract_fn name 
  extraction_portals.py    -- extract_<domain>() + article_<domain>() functions
  articles_clear_info.json -- site metadata

Outputs:
  extraction_output/
    {domain}_results.json   -- articles grouped by month with Groq summaries
    empty.json              -- no results in date window
    failed.json             -- errors

Usage (terminal):
  python extraction.py
  python extraction.py --query crispr
  python extraction.py --query protac --days 7
  python extraction.py --domain biopharmadive.com --query protac
  python extraction.py --limit 5 --no-enrich

Usage (Jupyter):
  await main()
  await main(query="crispr", days=14)
  await main(domain="biopharmadive.com")
"""

# =============================================================================
#  CONFIG
# =============================================================================

SEARCH_REGISTRY     = "search_registry.json"
SEARCH_ENGINES_FILE = "search_engines.py"
EXTRACTOR_REGISTRY  = "extractor_registry.json"
EXTRACTION_PORTALS  = "extraction_portals.py"
OUTPUT_DIR          = "extraction_output"

QUERY               = "PROTAC"
DATE_WINDOW_DAYS    = 7
MAX_PAGES           = 5
SCRAPE_DELAY        = 1.2
ENRICH_ARTICLES     = True

# Domains that require headless=False to pass Cloudflare's stricter bot checks.
# Add any domain whose search results page keeps returning CF walls.
HEADLESS_FALSE_DOMAINS = {
    "aacrjournals.org",
    "cancerres.aacrjournals.org",
    "aacrjournals.com",
}

# How many times to retry a CF-walled article page before giving up
CF_RETRY_COUNT      = 6
CF_RETRY_WAIT       = 5   # seconds between retries

from _stealth_constants import (
    STEALTH_UA,
    EXTRA_HEADERS,
    REQUESTS_HEADERS,
    LAUNCH_ARGS,
    STEALTH_JS,
    random_human_delay,
    human_mouse_move,
    apply_stealth_context,
    apply_stealth_page,
)

# Alias so fetch_static() calls keep working unchanged
HEADERS = REQUESTS_HEADERS

OVERLAY_SELECTORS = [
    "button[id*='accept']",    "button[class*='accept']",
    "button[id*='cookie']",    "button[class*='cookie']",
    "button[id*='consent']",   "button[class*='consent']",
    "button[aria-label*='Accept']", "button[aria-label*='Close']",
    "button[class*='close']",  "button[class*='dismiss']",
    "button[class*='modal-close']", "[aria-label='Close']",
    ".modal button", ".popup button", ".overlay button",
]



# =============================================================================
#  IMPORTS
# =============================================================================

import json, time, re, os, asyncio, requests, types, argparse, logging
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, quote_plus
from collections import defaultdict
from datetime import datetime as dt_datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

os.makedirs(OUTPUT_DIR, exist_ok=True)

def normalize(d):
    return re.sub(r"^www\.", "", d.lower().strip())

# =============================================================================
#  CLOUDFLARE DETECTION
# =============================================================================

# Phrases that appear exclusively on CF challenge / block pages
# Split into "strong" signals (only on challenge pages) and "weak" signals
# (can appear on real CDN-served pages like AACR).
# A page is walled if:  1+ strong signal  OR  2+ weak signals  OR  page too short.
_CF_STRONG_PHRASES = [
    "performing security verification",
    "enable javascript and cookies to continue",
    "checking your browser",
    "just a moment",                    # "Just a moment..." interstitial
    "cf-browser-verification",
    "please stand by, while we are checking your browser",
    "please enable cookies",
]

_CF_WEAK_PHRASES = [
    "cloudflare to restrict access",
    "ray id:",                          # in AACR CDN footer on real pages too
    "security service to protect",      # in AACR footer on real pages too
]

def _is_cloudflare_wall(html: str) -> bool:
    """
    Returns True when the page is a Cloudflare challenge / bot-block page
    rather than real article content.

    Guards:
      • Very short pages (< 2000 chars) are almost certainly not real content.
      • Any STRONG CF phrase alone is sufficient to flag it.
      • Two or more WEAK CF phrases together flag it (avoids false positives on
        sites like AACR that embed Ray ID / CDN notices in their real footers).
    """
    if not html or len(html) < 2000:
        return True
    lower = html.lower()
    if any(phrase in lower for phrase in _CF_STRONG_PHRASES):
        return True
    weak_hits = sum(1 for phrase in _CF_WEAK_PHRASES if phrase in lower)
    return weak_hits >= 2

# =============================================================================
#  LOAD PORTALS  (encoding-safe)
# =============================================================================

def load_portals(portals_file: str) -> dict:
    """
    Load functions from search_engines.py or extraction_portals.py.
    Pre-populates the module namespace with common helpers so Groq-generated
    functions never fail with NameError even if they omit imports.
    """
    p = Path(portals_file)
    if not p.exists():
        raise FileNotFoundError(f"Portals file not found: {portals_file}")
    raw = p.read_bytes()
    for bad, good in {b"\x97": b"--", b"\x96": b"-", b"\x93": b'"',
                      b"\x94": b'"', b"\x91": b"'", b"\x92": b"'", b"\x85": b"..."}.items():
        raw = raw.replace(bad, good)
    module = types.ModuleType(portals_file.replace(".py", ""))
    module.__file__ = str(p.resolve())
    import datetime as _dt_mod
    module.__dict__.update({
        "BeautifulSoup": BeautifulSoup,
        "urljoin":       urljoin,
        "urlparse":      urlparse,
        "re":            re,
        "datetime":      _dt_mod,
        "json":          __import__("json"),
        "html":          __import__("html"),
        "asyncio":       __import__("asyncio"),
    })
    exec(compile(raw.decode("utf-8", errors="replace"), str(p), "exec"), module.__dict__)
    return {name: getattr(module, name)
            for name in dir(module)
            if callable(getattr(module, name)) and not name.startswith("_")}

# =============================================================================
#  DATE PARSING + FILTER
# =============================================================================

_MONTH_MAP: dict[str, int] = {
    "january": 1,  "february": 2, "march": 3,    "april": 4,
    "may": 5,      "june": 6,     "july": 7,      "august": 8,
    "september": 9,"october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9,
    "oct": 10,"nov": 11,"dec": 12,
    "sept": 9,
}

def _month_num(token: str) -> int | None:
    clean = token.rstrip(".").lower().strip()
    if clean in _MONTH_MAP:
        return _MONTH_MAP[clean]
    if len(clean) >= 3 and clean[:3] in _MONTH_MAP:
        return _MONTH_MAP[clean[:3]]
    return None

_RE_ORDINAL      = re.compile(r"(\d)(st|nd|rd|th)\b", re.I)
_RE_RELATIVE     = re.compile(r"(\d+)\s*(second|minute|hour|day|week|month|year)s?\s*ago", re.I)
_RE_ISO          = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_RE_MON_DAY_YEAR = re.compile(r"([A-Za-z]{3,10}\.?)\s+(\d{1,2}),?\s+(\d{4})")
_RE_DAY_MON_YEAR = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,10}\.?)\s+(\d{4})")
_RE_MON_YEAR     = re.compile(r"([A-Za-z]{3,10}\.?)\s+(\d{4})(?:\b|$)")
_RE_US_NUMERIC   = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_RE_EU_NUMERIC   = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")

_RFC2822_FORMATS = [
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S %Z",
]

def _parse_date_inner(date_str: str):
    """
    Robust date parser. Handles:
      - 'Oct. 29, 2025'   (abbreviated month with period)
      - 'Sept. 23, 2025'  (4-char abbreviation)
      - 'March 9, 2026'   (full month name)
      - '2 days ago'      (relative)
      - '2025-10-29'      (ISO date)
      - '2026-03-09T14:30:00Z' (ISO datetime)
      - 'January 2026'    (month + year only, day=1)
      - RFC 2822, EU/US numeric
    Returns a datetime or None. Never raises.
    """
    if not date_str:
        return None
    date_str = date_str.strip()
    if not date_str or len(date_str) > 80:
        return None

    date_str = _RE_ORDINAL.sub(r"\1", date_str)
    lower = date_str.lower()
    now   = dt_datetime.now(timezone.utc).replace(tzinfo=None)

    if lower in ("today", "just now", "moments ago"):
        return now
    if lower == "yesterday":
        return now - timedelta(days=1)

    m = _RE_RELATIVE.search(lower)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {
            "second": timedelta(seconds=n), "minute": timedelta(minutes=n),
            "hour":   timedelta(hours=n),   "day":    timedelta(days=n),
            "week":   timedelta(weeks=n),   "month":  timedelta(days=n * 30),
            "year":   timedelta(days=n * 365),
        }.get(unit)
        if delta:
            return now - delta

    m = _RE_ISO.search(date_str)
    if m:
        try:
            return dt_datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    m = _RE_MON_DAY_YEAR.search(date_str)
    if m:
        mn = _month_num(m.group(1))
        if mn:
            try:
                return dt_datetime(int(m.group(3)), mn, int(m.group(2)))
            except ValueError:
                pass

    m = _RE_DAY_MON_YEAR.search(date_str)
    if m:
        mn = _month_num(m.group(2))
        if mn:
            try:
                return dt_datetime(int(m.group(3)), mn, int(m.group(1)))
            except ValueError:
                pass

    m = _RE_MON_YEAR.search(date_str)
    if m:
        mn = _month_num(m.group(1))
        if mn:
            try:
                return dt_datetime(int(m.group(2)), mn, 1)
            except ValueError:
                pass

    m = _RE_US_NUMERIC.search(date_str)
    if m:
        try:
            return dt_datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass

    m = _RE_EU_NUMERIC.search(date_str)
    if m:
        try:
            return dt_datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass

    for fmt in _RFC2822_FORMATS:
        try:
            dt = dt_datetime.strptime(date_str, fmt)
            return dt.replace(tzinfo=None)
        except Exception:
            continue

    return None


def parse_date(date_str: str):
    """Public entry-point. Cleans whitespace/noise then delegates."""
    if not date_str:
        return None
    cleaned = re.sub(r"[\u200b\u00a0\t]+", " ", date_str).strip()
    cleaned = re.sub(r"&[a-z]+;", " ", cleaned).strip()
    return _parse_date_inner(cleaned)

def is_within_window(date_str: str, window_days: int) -> bool:
    dt = parse_date(date_str)
    if dt is None:
        return False
    cutoff = dt_datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=window_days)
    return dt >= cutoff

def group_by_month(articles: list) -> dict:
    grouped = defaultdict(list)
    for art in articles:
        dt  = parse_date(art.get("date", ""))
        key = dt.strftime("%B %Y") if dt else "Unknown"
        art["_sort"] = (dt.year, dt.month, dt.day) if dt else (0, 0, 0)
        grouped[key].append(art)
    for key in grouped:
        grouped[key].sort(key=lambda x: x.pop("_sort"), reverse=True)
    def month_key(k):
        try:    return dt_datetime.strptime(k, "%B %Y")
        except: return dt_datetime.min
    return dict(sorted(grouped.items(), key=lambda x: month_key(x[0]), reverse=True))

# =============================================================================
#  FETCH HELPERS
# =============================================================================

def fetch_static(url: str) -> str | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.text
    except Exception:
        return None

async def _dismiss_overlays(page):
    for sel in OVERLAY_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=300):
                await el.click(); await asyncio.sleep(0.3)
        except Exception:
            pass

_READ_MORE_SELECTORS = [
    "button[class*='read-more']",   "button[class*='readmore']",
    "button[class*='show-more']",   "button[class*='showmore']",
    "button[class*='load-more']",   "button[class*='loadmore']",
    "button[class*='expand']",      "button[id*='expand']",
    "a[class*='read-more']",        "a[class*='readmore']",
    "a[class*='show-more']",        "a[class*='showmore']",
    "[class*='read-more']",         "[class*='readmore']",
    "button[class*='paywall']",     "[class*='paywall'] button",
    "button:has-text('Read more')", "button:has-text('Read More')",
    "button:has-text('Show more')", "button:has-text('Continue reading')",
    "button:has-text('Full article')", "a:has-text('Read more')",
    "[data-testid*='read-more']",   "[data-testid*='expand']",
]

async def _expand_read_more(page) -> bool:
    for sel in _READ_MORE_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=800):
                await el.scroll_into_view_if_needed()
                await el.click()
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                await asyncio.sleep(1.5)
                log.info(f"    [expand] clicked '{sel}'")
                return True
        except Exception:
            continue
    return False


async def get_rendered_html(page, expand: bool = False) -> str:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass

    await asyncio.sleep(2)

    await _dismiss_overlays(page)

    # Multi-scroll to trigger lazy loading
    last_height = 0
    for _ in range(6):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.5)
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height

    if expand:
        expanded = await _expand_read_more(page)
        if expanded:
            await asyncio.sleep(1)
            for _ in range(3):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1)

    await asyncio.sleep(1.5)

    return await page.inner_html("body")


async def _fetch_article_html(page, url: str) -> str:
    """
    Navigate to an article URL and return its body HTML.

    • Applies a CF-aware retry loop: if the page is still a Cloudflare wall
      after CF_RETRY_COUNT attempts, returns "" (empty string).
    • Falls back to a plain requests.get() only when Playwright itself errors.
    • Never returns Cloudflare wall text — callers always get real HTML or "".
    """
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log.warning(f"    [5] goto failed for {url}: {e}")
        # Static fallback — still guard against CF wall
        static = fetch_static(url) or ""
        if _is_cloudflare_wall(static):
            log.warning(f"    [5] Static fallback also CF-walled for {url}")
            return ""
        return static

    # Retry loop — wait for CF to clear
    art_html = ""
    for attempt in range(1, CF_RETRY_COUNT + 1):
        art_html = await get_rendered_html(page, expand=False)
        if not _is_cloudflare_wall(art_html):
            break
        log.warning(
            f"    [5] CF wall on {url} "
            f"(attempt {attempt}/{CF_RETRY_COUNT}), waiting {CF_RETRY_WAIT}s..."
        )
        await asyncio.sleep(CF_RETRY_WAIT)
    else:
        # All retries exhausted — still walled
        log.warning(f"    [5] CF wall persisted after all retries, skipping: {url}")
        return ""

    # One final pass with expand=True now that the page is real
    art_html = await get_rendered_html(page, expand=True)
    if _is_cloudflare_wall(art_html):
        return ""
    return art_html

# =============================================================================
#  PAGINATION DETECTION
# =============================================================================

def extract_pagination(html: str, base_url: str) -> str | None:
    soup       = BeautifulSoup(html, "html.parser")
    page_links = {}
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a.get("href", "")
        full = href if href.startswith("http") else urljoin(base_url, href)
        if text.isdigit() and 1 < int(text) <= 50:
            page_links[int(text)] = full
        if text in ["next", "Next", "NEXT", "Next ->", "Older", ">", ">>"]:
            page_links.setdefault("next", full)
    page2 = page_links.get(2) or page_links.get("next")
    if not page2:
        return None
    for param in ["page", "p", "pg", "paged", "pn"]:
        if f"{param}=2" in page2:
            return page2.replace(f"{param}=2", f"{param}={{page}}")
    m = re.search(r"/2(/|$|\?)", page2)
    if m:
        return page2[:m.start()] + "/{page}/" + page2[m.end():]
    return None

def get_page_url(pattern: str, page: int) -> str:
    return pattern.replace("{page}", str(page))

def extract_all_text(html: str) -> str:
    """
    Extract clean article text from HTML.
    Strips nav/footer/script/style noise, deduplicates repeated blocks,
    and returns plain text joined by newlines.
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                     "noscript", "iframe", "form"]):
        tag.decompose()

    seen   = set()
    blocks = []

    for el in soup.find_all(True):
        txt = el.get_text(" ", strip=True)
        if not txt or txt in seen:
            continue
        seen.add(txt)
        blocks.append(txt)

    return "\n".join(blocks)

# =============================================================================
#  PROCESS ONE SITE
# =============================================================================

async def process_site(
    domain:          str,
    search_url_tmpl: str,
    search_fn,
    extract_fn,
    query:           str,
    date_window:     int,
    enrich:          bool,
) -> dict:
    """
    Stages 1-6 for one domain.
    """
    base_url = "https://" + re.sub(r"^www\.", "", domain).rstrip("/")

    from playwright.async_api import async_playwright
    pw_inst = browser = ctx = page = None

    # Use headless=False for CF-strict domains (mirrors the working standalone scraper)
    use_headed = any(d in domain for d in HEADLESS_FALSE_DOMAINS)
    if use_headed:
        log.info(f"    [browser] headless=False mode for CF-strict domain: {domain}")

    try:
        pw_inst = await async_playwright().start()
        browser = await pw_inst.chromium.launch(
            headless=not use_headed,
            args=LAUNCH_ARGS + (["--start-maximized"] if use_headed else []),
        )
        ctx     = await browser.new_context(
            user_agent=STEALTH_UA,
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 800} if use_headed else {"width": 1366, "height": 768},
        )
        # Apply full stealth suite: EXTRA_HEADERS + STEALTH_JS on every page/frame
        # (WebGL, Canvas, Audio, Permissions, screen, CDP globals, etc.)
        await apply_stealth_context(ctx)

        page = await ctx.new_page()

        # Belt-and-suspenders: also run playwright-stealth if installed
        try:
            from playwright_stealth import stealth_async
            await stealth_async(page)
        except ImportError:
            pass  # _stealth_constants already covers the major CF signals

        await page.route(
            "**/*.{png,jpg,gif,svg,woff,woff2,ttf,ico,mp4,mp3}",
            lambda r: r.abort()
        )

        # ---- Stage 1: NAVIGATE ---------------------------------------------
        print(f"    [1] Navigating via {search_fn.__name__}(page, {query!r}, days={date_window})...")

        nav_error = None
        try:
            import inspect as _inspect
            _sig = _inspect.signature(search_fn)
            if "days" in _sig.parameters:
                await asyncio.wait_for(search_fn(page, query, days=date_window), timeout=75)
            else:
                await asyncio.wait_for(search_fn(page, query), timeout=75)
        except asyncio.TimeoutError:
            nav_error = "search_fn timed out"
            log.warning(f"    [1] {nav_error}")
        except Exception as e:
            nav_error = str(e)
            log.warning(f"    [1] search_fn error: {e}")

        # ---- Scroll down to load all dynamic content ----------------------
        print("    [1.5] Scrolling to load dynamic content...")

        # CF-strict domains (headless=False): mirror the working standalone scraper —
        # wait 8s first, then do human-like wheel scrolls to pass the CF challenge.
        if use_headed:
            print("    [1.5] CF-strict domain: waiting 8s for challenge to clear...")
            await asyncio.sleep(8)
            # Use human_mouse_move from _stealth_constants to add mouse entropy
            await human_mouse_move(page, num_moves=4)
            for i in range(5):
                await page.mouse.wheel(0, 1000)
                await random_human_delay(0.8, 1.4)
                print(f"        Human scroll {i+1}/5 done")
        else:
            for i in range(8):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1.2)
                print(f"        Scroll {i+1}/8 done")

        page1_html = await get_rendered_html(page, expand=False)

        # Guard: if the search results page itself is CF-walled, abort early
        if _is_cloudflare_wall(page1_html):
            return {
                "status": "failed",
                "data": {},
                "error": "Cloudflare wall on search results page — stealth bypass insufficient"
            }

        if not page1_html or len(page1_html) < 1000:
            return {"status": "failed", "data": {}, "error": "page empty after navigation + scrolling"}

        # ---- Stage 2: EXTRACT article list ---------------------------------
        print(f"    [2] Extracting links via {extract_fn.__name__}...")
        try:
            all_articles = extract_fn(page1_html, base_url)
            print(f"    [2] Extracted {len(all_articles)} articles from page")
        except Exception as e:
            return {"status": "failed", "data": {}, "error": f"extract_fn failed: {e}"}

        if not all_articles:
            return {"status": "empty", "data": {}, "error": "extract_fn returned 0 articles after scrolling"}

        # ---- Stage 3: Pagination ------------------------------------------
        pattern = extract_pagination(page1_html, base_url)
        print(f"    [3] Pagination pattern: {pattern or 'none'}")

        if pattern:
            for page_num in range(2, MAX_PAGES + 1):
                url = get_page_url(pattern, page_num)
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    await asyncio.sleep(1.5)
                    html = await get_rendered_html(page)
                    if _is_cloudflare_wall(html):
                        log.warning(f"    [3] CF wall on pagination page {page_num}, stopping")
                        break
                    page_arts = extract_fn(html, base_url)
                    print(f"    [3] Page {page_num}: {len(page_arts)} articles")
                    all_articles.extend(page_arts)
                except Exception:
                    break

        # Deduplicate by URL
        seen = set()
        deduped = []
        for art in all_articles:
            u = art.get("url")
            if u and u not in seen:
                seen.add(u)
                deduped.append(art)
        print(all_articles)
        print(f"    [3] Total unique articles: {len(deduped)}")

        if not deduped:
            return {"status": "empty", "data": {}, "error": "0 unique articles after dedup"}

        # ---- Stage 4: Date filter + grouping -------------------------------
        _url_date_re = re.compile(r"/(20\d{2})[/_-](0?[1-9]|1[0-2])[/_-](0?[1-9]|[12]\d|3[01])")
        for art in deduped:
            if not art.get("date"):
                m = _url_date_re.search(art.get("url", ""))
                if m:
                    art["date"] = f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"

        grouped  = group_by_month(deduped)
        filtered = {}
        for month, arts in grouped.items():
            if month == "Unknown":
                continue
            in_window = [a for a in arts if is_within_window(a.get("date", ""), date_window)]
            if in_window:
                filtered[month] = in_window

        if not filtered:
            return {"status": "empty", "data": {}, "error": f"0 articles in last {date_window} days"}

        total_in = sum(len(v) for v in filtered.values())
        print(f"    [4] {total_in} articles in window across {len(filtered)} months")

        # ---- Stage 5: Scrape full article text ----------------------------
        output = {}

        for month, articles in filtered.items():
            article_data = {}

            if enrich:
                print(f"    [5] Scraping {len(articles)} articles for {month}...")

                for art in articles:
                    url = art.get("url", "")
                    if not url:
                        continue

                    # _fetch_article_html handles CF retry + wall detection
                    art_html = await _fetch_article_html(page, url)

                    if art_html:
                        clean_text = extract_all_text(art_html)
                        log.info(f"    [5] OK  {url}  ({len(clean_text)} chars)")
                    else:
                        clean_text = ""
                        log.warning(f"    [5] SKIP {url}  (CF wall or fetch error)")

                    article_data[url] = {"text": clean_text}
                    await asyncio.sleep(SCRAPE_DELAY)

            structured = []
            for art in articles:
                url  = art.get("url", "")
                data = article_data.get(url, {})
                structured.append({
                    "title": art.get("title"),
                    "url":   url,
                    "date":  art.get("date"),
                    "tag":   art.get("tag"),
                    "text":  data.get("text", ""),
                })

            output[month] = {
                "article_count": len(structured),
                "articles":      structured,
            }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "failed", "data": {}, "error": str(e)}

    finally:
        for obj, method in [(browser, "close"), (pw_inst, "stop")]:
            if obj:
                try:
                    await getattr(obj, method)()
                except Exception:
                    pass
        await asyncio.sleep(1.5)

    return {
        "status": "ok",
        "domain": domain,
        "data":   output,
    }

# =============================================================================
#  MAIN
# =============================================================================

async def main(
    query:        str        = QUERY,
    domain:       str | None = None,
    limit:        int | None = None,
    enrich:       bool       = ENRICH_ARTICLES,
    days:         int        = DATE_WINDOW_DAYS,
):
    """
    Run the extraction pipeline on all installed portals.

    Args:
        query  : search query            (default "PROTAC")
        domain : process only one domain (default: all)
        limit  : cap number of domains
        enrich : scrape full article body text
        days   : keep articles from last N days
    """
    search_reg    = json.loads(Path(SEARCH_REGISTRY).read_text(encoding="utf-8")) \
                    if Path(SEARCH_REGISTRY).exists() else {}
    extractor_reg = json.loads(Path(EXTRACTOR_REGISTRY).read_text(encoding="utf-8")) \
                    if Path(EXTRACTOR_REGISTRY).exists() else {}

    search_fns  = load_portals(SEARCH_ENGINES_FILE)
    portal_fns  = load_portals(EXTRACTION_PORTALS) \
                  if Path(EXTRACTION_PORTALS).exists() else {}

    work = []
    for dom, ext_rec in extractor_reg.items():
        extract_fn_name = ext_rec.get("extract_fn")
        search_url_tmpl = ext_rec.get("search_url", "")

        if not extract_fn_name or extract_fn_name not in portal_fns:
            continue
        if not search_url_tmpl or "{query}" not in search_url_tmpl:
            srec = search_reg.get(dom) or search_reg.get(f"www.{dom}")
            if srec:
                search_url_tmpl = srec.get("search_url", "")
            if not search_url_tmpl or "{query}" not in search_url_tmpl:
                continue

        search_fn_name = (search_reg.get(dom) or search_reg.get(f"www.{dom}") or {}).get("access")
        if not search_fn_name or search_fn_name not in search_fns:
            continue

        work.append({
            "domain":     dom,
            "search_url": search_url_tmpl,
            "search_fn":  search_fns[search_fn_name],
            "extract_fn": portal_fns[extract_fn_name],
        })

    if domain:
        clean = normalize(domain)
        work  = [w for w in work if normalize(w["domain"]) == clean]
        if not work:
            print(f"No installed entry for: {domain}")
            print(f"Run: python install.py --url {domain}")
            return

    if limit:
        work = work[:limit]

    empty  = {}
    failed = {}
    total  = len(work)

    for old_f in Path(OUTPUT_DIR).glob("*_results.json"):
        old_f.unlink()
    log.info(f"[init] Cleared previous results from {OUTPUT_DIR}/")

    print(f"\n{'='*65}")
    print(f"EXTRACTION  --  query={query!r}")
    print(f"  Date window : last {days} days")
    print(f"  Domains     : {total}")
    print(f"  Enrich      : {enrich}")
    print(f"{'='*65}")

    for i, w in enumerate(work, 1):
        dom  = w["domain"]
        name = dom.split(".")[0].capitalize() if dom else "Unknown"

        print(f"\n{'='*65}")
        print(f"[{i}/{total}] {name}  ({dom})")
        print(f"{'='*65}")

        try:
            result = await process_site(
                domain          = dom,
                search_url_tmpl = w["search_url"],
                search_fn       = w["search_fn"],
                extract_fn      = w["extract_fn"],
                query           = query,
                date_window     = days,
                enrich          = enrich,
            )
        except Exception as e:
            result = {"status": "failed", "data": {}, "error": str(e)}

        if result["status"] == "ok":
            arts_data = result["data"]
            fname = dom.replace(".", "_") + "_results.json"
            path  = os.path.join(OUTPUT_DIR, fname)
            if arts_data:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(arts_data, f, indent=2)
                total_arts = sum(v["article_count"] for v in arts_data.values())
                print(f"\n  OK {total_arts} articles, "
                      f"{len(arts_data)} months --> {path}")
            else:
                empty[dom] = {"name": name, "reason": "ok but no article data"}
                print(f"\n  Empty (no data returned)")

        elif result["status"] == "empty":
            empty[dom]  = {"name": name, "reason": result["error"]}
            print(f"\n  Empty: {result['error']}")

        else:
            failed[dom] = {"name": name, "error": result["error"]}
            print(f"\n  Failed: {result['error']}")

        with open(os.path.join(OUTPUT_DIR, "empty.json"),  "w", encoding="utf-8") as f:
            json.dump(empty,  f, indent=2)
        with open(os.path.join(OUTPUT_DIR, "failed.json"), "w", encoding="utf-8") as f:
            json.dump(failed, f, indent=2)

        time.sleep(SCRAPE_DELAY)

    success = total - len(empty) - len(failed)
    print(f"\n{'='*65}")
    print(f"EXTRACTION COMPLETE -- {total} sites")
    print(f"  OK      : {success}")
    print(f"  Empty   : {len(empty)}  --> {OUTPUT_DIR}/empty.json")
    print(f"  Failed  : {len(failed)} --> {OUTPUT_DIR}/failed.json")
    print(f"  Output  : {OUTPUT_DIR}/")
    print(f"{'='*65}")

# =============================================================================
#  CLI
# =============================================================================

def _build_parser():
    p = argparse.ArgumentParser(
        prog="extraction.py",
        description="Run extraction on all installed portals.",
        epilog="""
Examples:
  python extraction.py
  python extraction.py --query crispr --days 14
  python extraction.py --domain biopharmadive.com
  python extraction.py --limit 5 --no-enrich
        """,
    )
    p.add_argument("--query",  "-q", default=QUERY)
    p.add_argument("--domain", "-d", default=None)
    p.add_argument("--limit",  "-n", type=int, default=None)
    p.add_argument("--days",         type=int, default=DATE_WINDOW_DAYS)
    p.add_argument("--enrich", dest="enrich",
                   action=argparse.BooleanOptionalAction, default=ENRICH_ARTICLES)
    return p


def _run(coro):
    """
    Windows-safe asyncio entry point.
    Suppresses harmless ProactorEventLoop ResourceWarnings on Windows.
    """
    import sys, warnings
    if sys.platform == "win32":
        warnings.filterwarnings(
            "ignore", message="unclosed transport", category=ResourceWarning
        )
        warnings.filterwarnings(
            "ignore", message="Enable tracemalloc", category=ResourceWarning
        )
    asyncio.run(coro)


if __name__ == "__main__":
    args = _build_parser().parse_args()
    _run(main(
        query  = args.query,
        domain = args.domain,
        limit  = args.limit,
        enrich = args.enrich,
        days   = args.days,
    ))
else:
    print("OK  extraction.py loaded.")
    print("    Run:  await main()")
    print("          await main(query='crispr', days=14)")
    print("          await main(domain='biopharmadive.com')")