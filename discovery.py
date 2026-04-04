"""
discoverer.py
=============
Tree-based universal search URL discoverer.

For each domain:
  1. Load homepage body HTML
  2. LOCAL: extract candidate elements (inputs, buttons, search-like)
  3. GROQ: pick best candidate + action (tree node)
  4. PLAYWRIGHT: execute action
  5. LOCAL: check result page (clear yes / clear no / uncertain)
     - uncertain → GROQ semantic check
  6. On success → GROQ generates search_<domain>() function
  7. Appends function to search_engines.py
  8. Updates search_registry.json

Usage:
  # Batch mode (reads INPUT_JSON):
  python discoverer.py
  python discoverer.py --limit 5
  python discoverer.py --query crispr --no-resume

  # Single-URL mode (skips INPUT_JSON entirely):
  python discoverer.py --url nature.com
  python discoverer.py --url https://pubmed.ncbi.nlm.nih.gov --query protac
  python discoverer.py --url biopharmadive.com --query crispr --no-resume

  # or in Jupyter:
  await main()
  await main(single_url="nature.com", query="crispr")
"""

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG  (all overridable via CLI flags)
# ─────────────────────────────────────────────────────────────────────────────

INPUT_JSON         = "articles_clear_info.json"
REGISTRY_JSON      = "s.json"
ENGINES_FILE       = "se.py"

QUERY              = "protac"          # default test query used during discovery
RESUME             = True              # skip already-discovered domains
LIMIT              = None              # set to int to test first N domains
SKIP_DOMAINS       = {"reddit.com"}

import os as _os

def _load_groq_keys() -> list[str]:
    """
    Load Groq API keys from environment variables.

    Single key:   set  GROQ_API_KEY=gsk_...
    Multiple keys (key rotation): set
        GROQ_API_KEY_1=gsk_...
        GROQ_API_KEY_2=gsk_...
        ...up to GROQ_API_KEY_6

    Set these as GitHub Secrets, never hardcode them.
    """
    # Try numbered keys first (rotation pool)
    keys = []
    for i in range(1, 7):
        k = _os.environ.get(f"GROQ_API_KEY_{i}", "").strip()
        if k:
            keys.append(k)

    # Fall back to single GROQ_API_KEY
    if not keys:
        single = _os.environ.get("GROQ_API_KEY", "").strip()
        if single:
            keys = [single]

    if not keys:
        raise RuntimeError(
            "No Groq API keys found. Set GROQ_API_KEY (or GROQ_API_KEY_1 … GROQ_API_KEY_6) "
            "as environment variables or GitHub Secrets."
        )
    return keys

GROQ_KEYS = _load_groq_keys()
GROQ_MODEL         = "llama-3.3-70b-versatile"

PAGE_TIMEOUT       = 20000  # ms
EXTRA_WAIT         = 1.5    # seconds after navigation
MAX_TREE_DEPTH     = 6      # max hops per domain
INTER_DOMAIN_SLEEP = 3      # seconds between domains
DOMAIN_TIMEOUT     = 180    # hard wall per domain — raised from 90s:
                            # worst case = ~30s discovery + 2×30s validation + buffer

# ─────────────────────────────────────────────────────────────────────────────
# IMPORTS
# ─────────────────────────────────────────────────────────────────────────────

import re, json, asyncio, time, argparse, requests, traceback, logging
from pathlib import Path
from urllib.parse import quote_plus, urlparse
from bs4 import BeautifulSoup, Tag
from playwright.async_api import async_playwright, Page, Browser, BrowserContext

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# STEALTH  — all evasion constants live in _stealth_constants.py
# ─────────────────────────────────────────────────────────────────────────────
from _stealth_constants import (
    STEALTH_UA, EXTRA_HEADERS, REQUESTS_HEADERS,
    LAUNCH_ARGS, STEALTH_JS,
    apply_stealth_context, apply_stealth_page,
    random_human_delay, human_mouse_move,
)


# ─────────────────────────────────────────────────────────────────────────────
# ── STEP 1: LOCAL CANDIDATE EXTRACTOR ────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

# ── FIX 1: Unicode-aware search keyword matching ─────────────────────────────
# Original only matched English/Latin keywords. Added 11 pharma-relevant
# languages: Japanese, Chinese, Korean, Arabic, French, German, Spanish,
# Portuguese, Italian, Russian, Dutch. role/aria attributes are language-
# neutral so they still carry the most weight.
SEARCH_RE = re.compile(
    r"search|find|query|magnif|glass|lookup|seek|srch"
    # Spanish / Portuguese
    r"|buscar|busca|pesquisa|pesquisar"
    # German / Dutch
    r"|suche|suchen|zoek|zoeken"
    # French
    r"|recherche|chercher"
    # Italian
    r"|ricerca|cercare"
    # Russian (transliterated common attr values)
    r"|poisk|найти|поиск"
    # Japanese (kanji + hiragana for "search" / "find")
    r"|検索|探す|さがす"
    # Chinese Simplified + Traditional
    r"|搜索|搜尋|查找|查詢"
    # Korean
    r"|검색|찾기"
    # Arabic
    r"|بحث|ابحث",
    re.I | re.UNICODE,
)

def _attr_str(tag: Tag) -> str:
    parts = []
    for k, v in tag.attrs.items():
        if isinstance(v, list):
            v = " ".join(v)
        parts.append(f"{k}={v}")
    return " ".join(parts)


# Elements whose id/class/aria-label contain these strings are cookie banners,
# consent dialogs, or vendor widgets — never the site's real search box.
# Checked against the full attribute blob before scoring.
_COOKIE_EXCLUDE_RE = re.compile(
    r"cookie|consent|gdpr|onetrust|cookiebot|privacy|vendor|ccpa"
    r"|cybot|evidon|trustarc|didomi|usercentrics|cookie.list|cookie.search",
    re.I,
)

def _candidate_score(tag: Tag) -> int:
    attr_blob = _attr_str(tag).lower()

    # ── Hard exclusion: cookie/consent widgets ────────────────────────────────
    # #vendor-search-handler, CookieBot inputs, OneTrust panels etc. all contain
    # the word "search" but are NEVER the site search box.
    if _COOKIE_EXCLUDE_RE.search(attr_blob):
        return 0

    # Also exclude if the element lives inside a known cookie container
    parent = tag.parent
    if parent:
        parent_blob = _attr_str(parent).lower()
        if _COOKIE_EXCLUDE_RE.search(parent_blob):
            return 0

    blob = (attr_blob + " " + tag.get_text(" ", strip=True)).lower()
    score = 0
    if tag.name == "input":
        if tag.get("type") in ("search", "text", ""):
            score += 5
        if SEARCH_RE.search(blob):
            score += 8
    elif tag.name in ("button", "a", "span", "div", "label", "svg", "i"):
        if SEARCH_RE.search(blob):
            score += 6
    if tag.get("role") in ("search", "searchbox"):
        score += 10
    # aria-label is language-neutral intent signal — high weight
    aria = tag.get("aria-label", "")
    if SEARCH_RE.search(aria) and not _COOKIE_EXCLUDE_RE.search(aria):
        score += 8
    # placeholder is a strong signal for input fields regardless of language
    if SEARCH_RE.search(tag.get("placeholder", "")):
        score += 6
    if tag.name == "form" and SEARCH_RE.search(blob):
        score += 7
    return score


def extract_candidates(body_html: str, top_n: int = 20) -> str:
    soup = BeautifulSoup(body_html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg path", "meta"]):
        tag.decompose()

    candidates = []
    seen = set()
    tags = soup.find_all(["input", "button", "form", "a", "span", "div", "label", "i", "svg"])

    for tag in tags:
        score = _candidate_score(tag)
        if score < 4:
            continue

        sel = None
        if tag.get("id"):
            sel = f"#{tag['id']}"
        elif tag.get("name"):
            sel = f"{tag.name}[name='{tag['name']}']"
        elif tag.get("class"):
            cls = tag["class"]
            if isinstance(cls, list):
                cls = cls[0]
            sel = f"{tag.name}.{cls}"
        elif tag.get("placeholder"):
            sel = f"{tag.name}[placeholder='{tag['placeholder']}']"
        elif tag.get("aria-label"):
            sel = f"{tag.name}[aria-label='{tag['aria-label']}']"
        else:
            sel = tag.name

        if sel in seen:
            continue
        seen.add(sel)

        candidates.append((score, {
            "tag":   tag.name,
            "sel":   sel,
            "attrs": _attr_str(tag)[:120],
            "text":  tag.get_text(" ", strip=True)[:60],
            "score": score,
        }))

    candidates.sort(key=lambda x: x[0], reverse=True)
    top = [c for _, c in candidates[:top_n]]

    lines = ["=== CANDIDATE ELEMENTS (sorted by search-relevance score) ==="]
    for i, c in enumerate(top):
        lines.append(
            f"[{i}] tag={c['tag']} sel={c['sel']!r} score={c['score']}\n"
            f"     attrs: {c['attrs']}\n"
            f"     text:  {c['text']!r}"
        )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# ── STEP 2: LOCAL RESULT PAGE CHECKER ────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

_RE_RESULTS = re.compile(
    r"(results?\s+for|results?\s+found|showing\s+\d|search\s+results?"
    r"|\d+\s+article|\d+\s+match|\d+\s+result|found\s+\d)",
    re.I
)
_RE_NO_RESULTS = re.compile(
    r"(no results|nothing found|no matches|0 results|no articles"
    r"|didn.t find|could not find|no items found)",
    re.I
)
_RE_ERROR = re.compile(
    r"\b(404|not found|page not found|error|access denied|forbidden"
    r"|just a moment|attention required|cloudflare)\b",
    re.I
)
# URL pattern that carries a query VALUE — the only kind we trust as "yes" by URL alone
# e.g. /search?q=protac   /?s=crispr   /results?query=crispr
# Does NOT match bare /search/new  or  /search  with no param
_RE_SEARCH_URL_WITH_QUERY = re.compile(
    r"[?&](q|s|query|search|term|keyword|keywords)=[^&\s]{1,}",
    re.I
)

def local_check(before_url: str, after_url: str, after_html: str, query: str) -> str:
    soup = BeautifulSoup(after_html, "html.parser")

    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    title = (soup.title.string or "").strip() if soup.title else ""
    body  = soup.get_text(" ", strip=True)

    # ---------- 1. Hard error ----------
    if _RE_ERROR.search(title) or _RE_ERROR.search(body[:500]):
        return "no"

    url_lower = after_url.lower()

    # ---------- 2. Strong YES: URL has query ----------
    if after_url != before_url and _RE_SEARCH_URL_WITH_QUERY.search(after_url):
        return "yes"

    # ---------- 3. Accept generic search endpoints ----------
    # Covers:
    # /search/
    # /search
    # /news/articleList.html
    # /results
    if after_url != before_url:
        if any(x in url_lower for x in [
            "/search", "search/", "articlelist", "results", "news"
        ]):
            # Validate it's actually a listing page (not empty template)
            items = soup.find_all(["article", "li", "tr", "div"])
            links = soup.find_all("a", href=True)

            if len(items) >= 5 and len(links) >= 5:
                return "yes"

    # ---------- 4. Same URL but content changed ----------
    if before_url == after_url:
        if not _RE_RESULTS.search(body):
            return "no"

    # ---------- 5. Signal scoring ----------
    result_signals = 0

    # Query match (low weight — multilingual issue)
    if query.lower() in title.lower():
        result_signals += 1

    if _RE_RESULTS.search(body):
        result_signals += 2

    if _RE_NO_RESULTS.search(body):
        result_signals += 1

    # STRUCTURE (most reliable globally)
    articles = soup.find_all(["article", "li", "tr"])
    if len(articles) >= 5:
        result_signals += 2

    links = [
        a for a in soup.find_all("a", href=True)
        if len(a.get("href", "")) > 30
        and not a["href"].startswith(("javascript", "#", "mailto"))
    ]
    if len(links) >= 5:
        result_signals += 2

    # ---------- 6. Final ----------
    if result_signals >= 3:
        return "yes"
    if result_signals == 0:
        return "no"

    return "uncertain"


# ─────────────────────────────────────────────────────────────────────────────
# ── GROQ CLIENT ──────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

_groq_key_idx = 0

def groq_call(messages: list[dict], system: str, max_tokens: int = 400) -> str:
    global _groq_key_idx
    nk = len(GROQ_KEYS)
    backoff = 4
    for _ in range(40):
        key = GROQ_KEYS[_groq_key_idx % nk]
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Content-Type":  "application/json",
                    "Authorization": f"Bearer {key}",
                },
                json={
                    "model":       GROQ_MODEL,
                    "temperature": 0,
                    "max_tokens":  max_tokens,
                    "messages":    [{"role": "system", "content": system}] + messages,
                },
                timeout=30,
            )
            if r.status_code == 429:
                _groq_key_idx += 1
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if r.status_code != 200:
                _groq_key_idx += 1
                time.sleep(2)
                continue
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.warning(f"[Groq] error: {e}")
            _groq_key_idx += 1
            time.sleep(2)
    raise RuntimeError("Groq exhausted all retries")


# ─────────────────────────────────────────────────────────────────────────────
# ── GROQ PROMPTS ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PICKER = """You are a browser agent finding the search functionality on a website.

You receive a list of candidate HTML elements scored by search-relevance,
plus a log of previous actions and their outcomes.

You must pick ONE action to take next.

Reply with EXACTLY one line, no explanation:
  CLICK <selector>
  FILL <selector> | <text>
  GIVE_UP <reason>

Rules:
- Prefer inputs with high score for FILL.
- Prefer buttons/icons for CLICK if no visible input yet.
  NOTE: Clicking a search icon/toggle will AUTO-FILL the revealed bar — you do NOT
  need a separate FILL step after a CLICK that reveals a search input.
- Each candidate shows  sel='...'  — use ONLY the value inside the quotes as the selector.
  CORRECT:   CLICK #search-icon
  INCORRECT: CLICK sel="#search-icon"
- Study the action log: if CLICK on a toggle already happened but FAILED due to
  an overlay, the overlay has now been removed — you may retry that CLICK.
- If a previous CLICK revealed a bar but fill failed, try FILL on the revealed input.
- GIVE_UP only if all search-related candidates have been tried and failed.
"""

SYSTEM_SEMANTIC = """You are checking if an HTML page is a search results page.

Reply with EXACTLY one word: YES or NO

A search results page:
- Shows a list of articles, papers, products, or items
- May say "results for", "found X results", or "no results found"
- Has multiple clickable result items
- May NOT have the query in the URL (JS-rendered results are fine)

Not a search results page:
- Homepage, article page, login page, error page, category page
"""

SYSTEM_CODEGEN = """You are generating a Python async function for Playwright browser automation.
You will be given:
  - domain, function name, search_type, search_url, steps taken
  - The RAW HTML of the search results page (or a trimmed section of it)

Your job: generate a function that navigates to the search results page AND
activates "sort by date / newest first" so only the most recent articles appear.

FUNCTION SIGNATURE (mandatory):
  async def search_<domain_fn>(page, query, days=7):

WAIT RULES — CRITICAL, always follow these exactly:
  - After EVERY page.goto() call, use this pattern:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        await asyncio.sleep(2)
  - After clicking a sort control, use:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        await asyncio.sleep(1.5)
  - NEVER use wait_for_load_state("networkidle", ...) — it hangs on many sites.
  - NEVER use bare await page.wait_for_load_state(...) without try/except.
  - Always add "import asyncio" inside the function body.

STEP 1 — Navigate to the search results page (use search_url + query).
STEP 2 — Activate the "newest / date" sort. Study the HTML and choose ONE strategy:

  STRATEGY A — URL param (fastest, preferred):
    If the HTML shows sort <a> links whose href contains the full sort URL, extract
    the EXACT param and value from that href — do NOT guess param names.
    Example: <a href="/search?term=query&sort=newest">Date</a>
    => use: url + "&sort=newest"   (because the href shows sort=newest, not sort=date)
    Common sort param values you might see: sort=newest, sort=date, sort=recent,
    sortby=date, order=newest, dateSort=desc, sortField=date, sortOrder=DESC.
    ALWAYS read the actual href from the HTML rather than inventing a param name.

  STRATEGY B — Checkbox / radio toggle:
    BioPharma Dive example: <input id="sort" type="checkbox" name="sortby">
    The checkbox being checked = sorted by date.
      1. goto(search_url) with safe wait
      2. Check if checkbox is already checked via page.is_checked(selector)
         wrapped in try/except
      3. If NOT checked: await page.click("label[for='sort']") with try/except
      4. Safe wait after click

  STRATEGY C — Dropdown / custom element:
    BioSpace example: <button class="SearchResultsModuleSorts-control"> opens a
    dialog with "Newest" option inside.
      1. goto(search_url) with safe wait
      2. Try clicking the sort button/control with try/except
      3. Try clicking the "Newest"/"Date" option with try/except
      4. Safe wait after click

  STRATEGY D — Sort link click (<a href="...&sort=newest"> or similar):
    Use when the sort link href is visible but you want to click it rather than
    construct the URL manually.
      1. goto(search_url) with safe wait
      2. Click using a[href*='&sort=newest'] — use EXACT param from HTML (not sort=date).
      3. Safe wait

  STRATEGY E — No sort control detected:
    Last resort only. Try appending the most likely param observed in the page HTML.
    NEVER blindly append &sort=date if the HTML shows a different value.

  STRATEGY F — Infinite scroll / load-more pagination (FIX 3):
    Use when the results page has no pagination links but shows a "Load More" button
    or automatically loads more results on scroll.
    Detection signals in HTML: button text contains "load more", "show more",
    "ver más", "plus de résultats"; or class names contain "load-more", "infinite",
    "pagination__next"; or there is NO <a rel="next"> or numbered page links.
    When detected:
      1. goto(search_url) with safe wait
      2. Sort by date first (using whichever of A-D applies, wrapped in try/except)
      3. Collect results already visible
      4. Scroll/click loop — repeat up to MAX_SCROLL_PAGES times:
            try:
                btn = page.locator("button[class*='load-more'], [class*='loadMore'], "
                                   "button:has-text('Load More'), button:has-text('Show More')")
                if await btn.is_visible(timeout=1500):
                    await btn.click()
                else:
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2)
            except Exception:
                break
    MAX_SCROLL_PAGES = 3  # keep reasonable — we only need recent articles

RULES:
- Use exact selectors from the HTML — do NOT invent ids or class names.
- Wrap ALL click/check actions in try/except so the function never crashes.
- Always import asyncio, datetime, urllib.parse inside the function body.
- Compute from_date / to_date dynamically using datetime.date.today().
- The function MUST return page.url at the end.
- Output ONLY the raw Python function. No markdown. No explanation.
- FIX 4 — RATE LIMITING: Every generated function MUST include this block
  immediately after the final navigation/sort step and before return page.url:
      # polite crawl delay — prevents IP bans on repeated calls
      await asyncio.sleep(1.5)
- HIDDEN ELEMENTS: Before clicking any sort button or filter control, always
  check it is visible first. Use this pattern:
      try:
          el = page.locator("selector")
          if await el.is_visible(timeout=2000):
              await el.click()
              <wait pattern>
      except Exception:
          pass
  NEVER use page.click() directly on a sort/filter element — it will hang
  for 30+ seconds if the element exists in the DOM but is not visible.

CORRECT WAIT EXAMPLE:
async def search_example_com(page, query, days=7):
    import asyncio, datetime, urllib.parse
    from_date = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    url = f"https://example.com/search?q={urllib.parse.quote_plus(query)}"
    await page.goto(url)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    await asyncio.sleep(2)
    try:
        if not await page.is_checked("input#sort"):
            await page.click("label[for='sort']")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(1.5)
    except Exception:
        pass
    return page.url
"""


def _extract_sort_html(raw_html: str) -> str:
    """
    Extract the HTML section most likely to contain sort/filter/date controls.
    Sends a generous but focused slice to Groq so it can read real selectors.
    """
    soup = BeautifulSoup(raw_html, "html.parser")

    # Remove noise that wastes tokens
    for tag in soup(["script", "style", "noscript", "svg", "img", "iframe", "meta", "link"]):
        tag.decompose()

    # Priority 1: named sort/filter containers — grab entire subtree
    SORT_SELECTORS = [
        # generic patterns
        "[class*='sort']", "[class*='Sort']",
        "[class*='filter']", "[class*='Filter']",
        "[id*='sort']",   "[id*='filter']",
        # form-level patterns
        "form[class*='search']", "form[action*='search']",
        # specific patterns from screenshots
        "[class*='SearchResultsModule']",   # BioSpace
        "[class*='js-search']",             # BioPharma Dive
        "bsp-search-sorts",                 # BioSpace custom element
        "[class*='feed-header']",
        "[class*='search-header']",
        "[class*='results-header']",
        "[class*='toolbar']",
        "[class*='search-controls']",
        "[class*='search-options']",
    ]

    captured_html = []
    seen_tags     = set()

    for sel in SORT_SELECTORS:
        try:
            for el in soup.select(sel):
                el_id = id(el)
                if el_id in seen_tags:
                    continue
                seen_tags.add(el_id)
                chunk = str(el)
                if len(chunk) > 50:          # skip empty/trivial nodes
                    captured_html.append(chunk)
        except Exception:
            continue

    # Priority 2: any input/select/button/label near date/sort text
    for el in soup.find_all(["input", "select", "button", "label", "a", "li", "span"]):
        el_id = id(el)
        if el_id in seen_tags:
            continue
        attrs_text = " ".join(str(v) for v in el.attrs.values()).lower()
        text       = el.get_text(" ", strip=True).lower()
        blob       = attrs_text + " " + text
        if any(kw in blob for kw in [
            "sort", "date", "newest", "latest", "recent", "order",
            "filter", "week", "month", "period", "relevance", "time",
            # FIX 3: infinite scroll / load-more pagination signals
            "load more", "loadmore", "load-more", "show more", "showmore",
            "infinite", "pagination", "next page", "ver más", "plus de résultats",
        ]):
            seen_tags.add(el_id)
            # Include the parent for context (to see surrounding form/container)
            parent = el.parent
            if parent and id(parent) not in seen_tags:
                seen_tags.add(id(parent))
                captured_html.append(str(parent))
            else:
                captured_html.append(str(el))

    combined = "\n\n".join(captured_html)

    # Priority 3: always include <a> tags whose href contains a sort/order param —
    # these are the clearest possible signal for Strategy A / D code generation.
    sort_link_re = re.compile(r"[?&](sort|sortby|order|sortField|sortOrder|dateSort)=", re.I)
    for el in soup.find_all("a", href=True):
        if sort_link_re.search(el.get("href", "")):
            el_id = id(el)
            if el_id not in seen_tags:
                seen_tags.add(el_id)
                # Include the parent for context (shows sibling sort options)
                parent = el.parent
                if parent and id(parent) not in seen_tags:
                    seen_tags.add(id(parent))
                    captured_html.append(str(parent))
                else:
                    captured_html.append(str(el))

    combined = "\n\n".join(captured_html)

    # Cap at ~12 000 chars — generous enough for Groq to see full selector chains
    if len(combined) > 12000:
        combined = combined[:12000] + "\n\n<!-- [truncated] -->"

    return combined or raw_html[:6000]


# ─────────────────────────────────────────────────────────────────────────────
# ── GROQ CALLS ───────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

def _clean_selector(raw: str) -> str:
    """
    Groq sometimes copies the label from the candidate strip verbatim:
      sel="#search-icon"  ->  #search-icon
    Strip the sel= prefix and surrounding quotes if present.
    """
    s = raw.strip()
    s = re.sub(r"^sel\s*=\s*['\"]?", "", s, flags=re.I)
    s = re.sub(r"['\"]$", "", s)
    return s.strip()


def groq_pick_action(candidate_strip: str, history: list[str]) -> tuple[str, list]:
    hist_text = ""
    if history:
        hist_text = (
            "\n\n=== ACTION LOG (what was tried and what happened) ===\n"
            + "\n".join(f"  {i+1}. {h}" for i, h in enumerate(history))
            + "\n\nDo NOT repeat actions marked DONE unless the log says to retry."
        )

    raw = groq_call(
        [{"role": "user", "content": candidate_strip + hist_text}],
        system=SYSTEM_PICKER,
        max_tokens=80,
    )
    log.info(f"  [Groq-pick] -> {raw!r}")

    line  = raw.strip().splitlines()[0].strip()
    parts = line.split(None, 1)
    verb  = parts[0].upper() if parts else "GIVE_UP"
    rest  = parts[1] if len(parts) > 1 else ""

    if verb in ("FILL", "FORCE_FILL"):
        if "|" in rest:
            sel_raw, txt = rest.split("|", 1)
            args = [_clean_selector(sel_raw), txt.strip()]
        else:
            args = [_clean_selector(rest), QUERY]
    elif verb == "GIVE_UP":
        args = [rest]
    else:
        args = [_clean_selector(rest)] if rest.strip() else []

    return verb, args


def groq_semantic_check(page_html: str, query: str) -> bool:
    soup = BeautifulSoup(page_html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    body_text = soup.get_text(" ", strip=True)[:3000]
    raw = groq_call(
        [{"role": "user", "content": f"Query searched: {query!r}\n\nPage content:\n{body_text}"}],
        system=SYSTEM_SEMANTIC,
        max_tokens=5,
    )
    return raw.strip().upper().startswith("Y")


def groq_generate_code(domain: str, domain_fn: str, search_type: str,
                        search_url: str | None, steps: list[str],
                        result_page_html: str = "",
                        extra_hint: str = "") -> str:
    steps_text = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(steps))

    # Extract the sort/filter HTML section from the results page
    sort_html_section = ""
    if result_page_html:
        sort_html_section = _extract_sort_html(result_page_html)

    msg = (
        f"domain: {domain}\n"
        f"function name: search_{domain_fn}\n"
        f"search_type: {search_type}\n"
        f"search_url: {search_url or 'null'}\n"
        f"steps taken:\n{steps_text}\n\n"
        f"=== SEARCH RESULTS PAGE HTML (sort/filter/date section) ===\n"
        f"{sort_html_section}\n"
        f"=== END HTML ===\n\n"
        f"Now generate the function. Read the HTML carefully to pick the right strategy."
        f"{extra_hint}"
    )
    code = groq_call([{"role": "user", "content": msg}], system=SYSTEM_CODEGEN, max_tokens=1200)
    code = re.sub(r"^```[a-z]*\n?", "", code.strip())
    code = re.sub(r"\n?```$", "", code.strip())
    return code.strip()


# ─────────────────────────────────────────────────────────────────────────────
# ── PLAYWRIGHT HELPERS ───────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

COOKIE_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    ".cc-btn.cc-allow", ".cc-accept", "#accept-cookies",
    "button[id*='accept']", "button[class*='accept']",
    "button[aria-label*='Accept']", "button[aria-label*='Agree']",
]

_JS_KILL_OVERLAYS = """
() => {
    let removed = 0;
    const selectors = [
        '#prestitial-outer', '[class*="prestitial"]', '[class*="interstitial"]',
        '[class*="content-overlay"]', '[class*="ad-overlay"]',
        '[id*="prestitial"]', '[id*="interstitial"]'
    ];
    for (const sel of selectors) {
        document.querySelectorAll(sel).forEach(el => { el.remove(); removed++; });
    }
    document.querySelectorAll('*').forEach(el => {
        const st = window.getComputedStyle(el);
        const z  = parseInt(st.zIndex) || 0;
        const pos = st.position;
        if ((pos === 'fixed' || pos === 'absolute') && z > 1000) {
            const r = el.getBoundingClientRect();
            if (r.width > window.innerWidth * 0.3 && r.height > window.innerHeight * 0.3) {
                el.remove(); removed++;
            }
        }
    });
    document.body.style.overflow = '';
    document.documentElement.style.overflow = '';
    return removed;
}
"""

async def _dismiss_overlays(page: Page):
    for sel in COOKIE_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=200):
                await el.click(timeout=300)
                await asyncio.sleep(0.3)
        except Exception:
            pass
    try:
        removed = await page.evaluate(_JS_KILL_OVERLAYS)
        if removed:
            log.info(f"  [overlay] JS removed {removed} overlay element(s)")
            await asyncio.sleep(0.3)
    except Exception:
        pass
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass


async def _nav(page: Page, url: str) -> bool:
    for wait in ("domcontentloaded", "load"):
        try:
            await page.goto(url, wait_until=wait, timeout=PAGE_TIMEOUT)
            await asyncio.sleep(EXTRA_WAIT)
            await _dismiss_overlays(page)
            return True
        except Exception as e:
            log.warning(f"  [nav:{wait}] {str(e)[:80]}")
    return False


async def _get_body_html(page: Page) -> str:
    try:
        return await page.inner_html("body")
    except Exception:
        return ""


async def _find_revealed_input(page: Page) -> str | None:
    try:
        return await page.evaluate("""
        () => {
            const inputs = Array.from(document.querySelectorAll(
                'input[type="search"], input[type="text"], input[name="q"], ' +
                'input[name="s"], input[placeholder*="earch"], ' +
                'input[class*="search"], input[id*="search"]'
            ));
            for (const inp of inputs) {
                const r  = inp.getBoundingClientRect();
                const st = window.getComputedStyle(inp);
                const visible = (
                    r.width > 0 && r.height > 0 &&
                    st.display !== 'none' &&
                    st.visibility !== 'hidden' &&
                    parseFloat(st.opacity) > 0
                );
                if (visible) {
                    if (inp.id)          return '#' + inp.id;
                    if (inp.name)        return 'input[name="' + inp.name + '"]';
                    if (inp.placeholder) return 'input[placeholder="' + inp.placeholder + '"]';
                    const cls = inp.className && inp.className.trim().split(/\\s+/)[0];
                    if (cls) return 'input.' + cls;
                    return 'input';
                }
            }
            return null;
        }
        """)
    except Exception:
        return None


async def _execute_action(page: Page, verb: str, args: list,
                          query: str = QUERY) -> tuple[bool, str]:
    try:
        if verb == "CLICK":
            sel = args[0] if args else ""
            el  = page.locator(sel).first
            await _dismiss_overlays(page)

            clicked_ok = False
            try:
                await el.wait_for(state="visible", timeout=4000)
                await el.click()
                clicked_ok = True
            except Exception:
                pass

            if not clicked_ok:
                log.info(f"  [force-click] element hidden, using JS click on {sel!r}")
                try:
                    res = await page.evaluate(f"""() => {{
                        const el = document.querySelector({repr(sel)});
                        if (!el) return 'not-found';
                        el.click();
                        return 'ok';
                    }}""")
                    if res == "not-found":
                        return False, f"CLICK: element not found in DOM: {sel!r}"
                    clicked_ok = True
                except Exception as e2:
                    return False, f"CLICK JS fallback failed: {e2}"

            await asyncio.sleep(EXTRA_WAIT)

            # ── TWO-STEP / LANDING-PAGE AUTO-FILL ────────────────────────────
            # Handles two cases:
            #   A) Click revealed a hidden search bar (icon toggle pattern)
            #   B) Click navigated to a search landing page (e.g. /search/new)
            #      that already has a visible input ready to fill
            # In both cases: find the visible input, fill query, submit.
            revealed = await _find_revealed_input(page)
            if revealed:
                log.info(f"  [two-step] visible input found → {revealed!r}, auto-filling")
                try:
                    inp = page.locator(revealed).first
                    await inp.wait_for(state="visible", timeout=3000)
                    await inp.click()
                    await inp.fill(query)
                    await asyncio.sleep(0.4)
                    await inp.press("Enter")
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=8000)
                    except Exception:
                        pass
                    await asyncio.sleep(EXTRA_WAIT)
                    return True, (
                        f"clicked {sel!r} → filled {revealed!r} "
                        f"with {query!r} → submitted → {page.url}"
                    )
                except Exception as e:
                    log.warning(f"  [two-step] fill failed: {e}")
            else:
                log.info(f"  [two-step] no visible input found after clicking {sel!r}")

            return True, f"clicked {sel!r} (no input found to fill)"

        elif verb in ("FILL", "FORCE_FILL"):
            if len(args) < 2:
                return False, "FILL needs selector | text"
            sel  = args[0]
            text = query   # always use the real query, ignore Groq's suggestion
            await _dismiss_overlays(page)
            el = page.locator(sel).first
            url_before_fill = page.url   # capture now for submit-fallback comparison

            if verb == "FILL":
                # Normal path: wait for element to be visible, then fill
                await el.wait_for(state="visible", timeout=5000)
                await el.click()
                await el.fill(text)

            else:
                # FORCE_FILL: element is known-hidden (loop detection escalated here).
                # Unhide it via JS, set value via native setter so framework events
                # fire correctly, then press Enter to submit.
                log.info(f"  [force-fill] JS-filling hidden element {sel!r}")
                js_result = await page.evaluate(f"""
                () => {{
                    const el = document.querySelector({repr(sel)});
                    if (!el) return 'not-found';
                    el.style.display    = 'block';
                    el.style.visibility = 'visible';
                    el.style.opacity    = '1';
                    el.style.width      = '200px';
                    const nativeSet = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    nativeSet.call(el, {repr(text)});
                    el.dispatchEvent(new Event('input',  {{bubbles: true}}));
                    el.dispatchEvent(new Event('change', {{bubbles: true}}));
                    el.focus();
                    return 'ok';
                }}
                """)
                if js_result == "not-found":
                    return False, f"FORCE_FILL: element not found in DOM: {sel!r}"

            await asyncio.sleep(0.4)
            await el.press("Enter")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            await asyncio.sleep(EXTRA_WAIT)

            # ── Case 4: submit button fallback ────────────────────────────────
            # clinicalleader.com: Enter key on the input is ignored — the site
            # requires clicking the submit button next to the input.
            # If URL didn't change after Enter, find and click the nearest
            # submit button inside the same <form> (or a search-icon button).
            if page.url == url_before_fill:
                log.info(f"  [submit-fallback] URL unchanged after Enter on {sel!r} — "
                         f"trying nearest submit button")
                submitted = await page.evaluate(f"""
                () => {{
                    const el = document.querySelector({repr(sel)});
                    if (!el) return 'not-found';
                    // Look for submit button in parent form
                    const form = el.closest('form');
                    if (form) {{
                        const btn = form.querySelector(
                            'button[type="submit"], input[type="submit"], button:not([type="button"])'
                        );
                        if (btn) {{ btn.click(); return 'form-btn-clicked'; }}
                        form.submit();
                        return 'form-submitted';
                    }}
                    // No form — look for a nearby search button (sibling or parent)
                    const parent = el.parentElement;
                    if (parent) {{
                        const btn = parent.querySelector('button, [role="button"]');
                        if (btn) {{ btn.click(); return 'sibling-btn-clicked'; }}
                    }}
                    return 'no-btn-found';
                }}
                """)
                log.info(f"  [submit-fallback] result: {submitted}")
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
                await asyncio.sleep(EXTRA_WAIT)

            return True, f"filled {sel!r} with {text!r} → {page.url}"

        return False, f"unknown verb {verb}"

    except Exception as e:
        return False, f"{verb} error: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# ── XHR INTERCEPTOR ──────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

def _setup_xhr_intercept(page: Page, query: str) -> list[str]:
    captured: list[str] = []
    q_enc = quote_plus(query)

    def _on_req(req):
        u = req.url
        ul = u.lower()
        if any(k in ul for k in ["search", "query", "find", "q=", "suggest"]):
            if query.lower() in ul or q_enc.lower() in ul:
                captured.append(u)

    page.on("request", _on_req)
    return captured


# ─────────────────────────────────────────────────────────────────────────────
# ── DOMAIN FUNCTION NAME ─────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

def domain_to_fn(domain: str) -> str:
    """'pubmed.ncbi.nlm.nih.gov' → 'pubmed_ncbi_nlm_nih_gov'"""
    return re.sub(r"[^a-z0-9]", "_", domain.lower()).strip("_")


# ─────────────────────────────────────────────────────────────────────────────
# ── MAIN TREE TRAVERSAL ───────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

async def discover_domain(domain: str, query: str) -> dict | None:
    """
    Run the full tree-based discovery for one domain.
    Returns a result dict on success, None on failure.
    """
    domain_fn = domain_to_fn(domain)
    base_url  = f"https://{domain}"
    result    = None

    pw_inst = None
    browser: Browser | None = None

    try:
        pw_inst = await async_playwright().start()
        browser = await pw_inst.chromium.launch(headless=True, args=LAUNCH_ARGS)
        ctx: BrowserContext = await browser.new_context(
            user_agent=STEALTH_UA,
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="America/New_York",
            ignore_https_errors=True,
        )
        await apply_stealth_context(ctx)
        page: Page = await ctx.new_page()

        await page.route(
            "**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ttf,ico,mp4,mp3,pdf}",
            lambda r: r.abort()
        )

        print(f"  → Loading {base_url}")
        await human_mouse_move(page, num_moves=2)
        if not await _nav(page, base_url):
            print(f"  ❌ Could not load homepage")
            return None

        history: list[str] = []
        steps:   list[str] = []
        depth = 0
        last_failed_action: str | None = None
        same_fail_count: int = 0

        while depth < MAX_TREE_DEPTH:
            depth += 1
            print(f"  [depth {depth}] url={page.url}")

            # ── Guard: if we landed on an error page, stop wasting depth ──────
            if page.url.startswith("chrome-error://"):
                log.warning("  [guard] browser on error page — aborting")
                break

            before_url = page.url

            body_html = await _get_body_html(page)
            if not body_html:
                print(f"  ❌ Empty body at depth {depth}")
                break

            candidate_strip = extract_candidates(body_html)
            log.info(f"  [local] candidate strip ready ({len(candidate_strip)} chars)")

            verb, args = groq_pick_action(candidate_strip, history)
            if verb == "GIVE_UP":
                print(f"  ↩  Groq GIVE_UP: {args}")
                break

            action_desc  = f"{verb} {' | '.join(args)}"

            # ── Loop detection: same action failed twice → force-fill ─────────
            # Handles the pattern where Groq keeps trying FILL on a hidden input.
            # After 2 consecutive identical failures we escalate to JS force-fill.
            if action_desc == last_failed_action:
                same_fail_count += 1
            else:
                same_fail_count = 0
                last_failed_action = None

            if same_fail_count >= 2 and verb == "FILL":
                log.info(f"  [loop-detect] same FILL failed {same_fail_count}x — "
                         f"escalating to FORCE_FILL on {args[0]!r}")
                verb = "FORCE_FILL"   # _execute_action handles FORCE_FILL same as FILL
                action_desc = f"FORCE_FILL {' | '.join(args)}"

            print(f"  [Groq] action: {action_desc}")

            captured_xhr = _setup_xhr_intercept(page, query)
            ok, status   = await _execute_action(page, verb, args, query=query)
            log.info(f"  [action] {'✅' if ok else '❌'} {status}")

            if not ok:
                history.append(f"FAILED: {action_desc} — {status}")
                last_failed_action = action_desc   # track for loop detection
                await _nav(page, base_url)
                continue

            # Successful action — reset loop counter
            last_failed_action = None
            same_fail_count    = 0

            history.append(f"DONE: {action_desc} → outcome: {status}")
            steps.append(action_desc)

            after_url  = page.url
            after_html = await _get_body_html(page)

            search_type = None
            search_url  = None

            GROQ_FILL_VARIANTS = [
                "test search query", "example search query", "search term",
                "test+search+query", "example+search+query", "search+term",
                "test%20search%20query", "example%20search%20query",
                "search+query", "search%20query", "test", "Test",
            ]

            def _make_tpl(u: str) -> str:
                for v in [query, quote_plus(query), query.replace(" ", "+"),
                           query.replace(" ", "%20")]:
                    u = u.replace(v, "{query}")
                for v in GROQ_FILL_VARIANTS:
                    if v in u:
                        u = u.replace(v, "{query}")
                        break
                return u

            if after_url != before_url:
                tpl     = _make_tpl(after_url)
                parsed  = urlparse(tpl)
                qs_ok   = "{query}" in (parsed.query or "")
                # Only accept a bare path if it actually contains the query token
                # e.g. /search/crispr → /search/{query}  ✅
                # e.g. /search/new    (no token)          ❌
                path_ok = "{query}" in parsed.path
                if qs_ok or path_ok:
                    search_url  = tpl
                    search_type = "url"
                    print(f"  [url] template: {search_url}")
                else:
                    # URL changed to a search-landing page (e.g. /search/new) but
                    # no query is embedded yet — record the base URL for later use
                    # by code-gen but do NOT treat this as a completed result page
                    search_url  = tpl
                    search_type = "url"
                    print(f"  [url] search landing (no query embedded): {search_url}")

            if search_type is None and captured_xhr:
                TRACKER_DOMAINS = {
                    "googleads", "doubleclick", "google-analytics",
                    "googletagmanager", "googlesyndication", "googleadservices",
                    "facebook.com", "twitter.com", "analytics", "gtm",
                    "hotjar", "mixpanel", "segment.io", "amplitude",
                }
                for raw_xhr in captured_xhr:
                    xhr_host = urlparse(raw_xhr).netloc.lower()
                    if any(t in xhr_host for t in TRACKER_DOMAINS):
                        log.info(f"  [xhr] skipped tracker: {xhr_host}")
                        continue
                    site_root = domain.split(".")[-2] if "." in domain else domain
                    if site_root not in xhr_host:
                        log.info(f"  [xhr] skipped off-domain: {xhr_host}")
                        continue
                    tpl         = _make_tpl(raw_xhr)
                    search_url  = tpl
                    search_type = "xhr"
                    print(f"  [xhr] captured own-domain: {search_url[:80]}")
                    break

            if search_type is None and after_url != before_url:
                search_url  = _make_tpl(after_url)
                search_type = "url"
                print(f"  [url] fallback template: {search_url}")

            # A "complete" search_url embeds the query token — e.g. /?s={query}.
            # A bare landing URL like /search/new does NOT count as complete.
            search_url_complete = (
                search_url is not None and "{query}" in search_url
            )

            check = local_check(before_url, after_url, after_html, query)
            print(f"  [local-check] → {check}")

            if check == "yes" or (search_url_complete and check != "no"):
                if check == "uncertain":
                    # ── Case 2: wait for JS render before semantic check ──────
                    # pharmaceuticalcommerce: results are JS-rendered — the HTML
                    # body right after navigation is an empty shell. Wait for
                    # networkidle so content is populated before Groq reads it.
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                        await asyncio.sleep(1.0)
                    except Exception:
                        pass
                    after_html_rendered = await _get_body_html(page)

                    semantic = groq_semantic_check(after_html_rendered, query)
                    print(f"  [Groq-semantic] → {'YES' if semantic else 'NO'}")
                    if not semantic:
                        history.append("SEMANTIC_FAIL: page is not search results")
                        steps.pop()
                        await _nav(page, base_url)
                        continue

                print(f"  ✅ Found search page!")
                print(f"     type={search_type} url={search_url}")

                # ── Case 3: search_url is None (JS search, URL never changed) ─
                # drug-dev.com: fill+Enter worked and local_check said "yes" from
                # body signals, but the URL never changed so search_url stayed None.
                # Treat as xhr/JS type — codegen will replay the interaction steps.
                if search_url is None:
                    search_type = "xhr"
                    log.info("  [xhr-fallback] URL unchanged — treating as JS/XHR search")

                code = await _generate_and_validate(
                    domain, domain_fn, search_type, search_url, steps,
                    result_page_html=after_html or "",
                    query=query,
                )
                print(f"  [Groq-codegen] generated {len(code)} chars")

                result = {
                    "domain":      domain,
                    "search_type": search_type,
                    "search_url":  search_url,
                    "access":      f"search_{domain_fn}",
                    "code":        code,
                }
                break

            elif check == "no":
                print(f"  ↩  Not a results page — backtracking")
                history.append(f"WRONG_PAGE: {action_desc} led to non-results page (url={after_url})")
                if steps:
                    steps.pop()
                await _nav(page, base_url)

    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.error(f"  💥 {e}")
        traceback.print_exc()
    finally:
        for obj, method in [(browser, "close"), (pw_inst, "stop")]:
            if obj:
                try:
                    await getattr(obj, method)()
                except Exception:
                    pass
        await asyncio.sleep(1.5)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# ── REGISTRY + ENGINES FILE MANAGEMENT ───────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

def load_registry() -> dict:
    p = Path(REGISTRY_JSON)
    return json.loads(p.read_text()) if p.exists() else {}


def save_registry(registry: dict):
    Path(REGISTRY_JSON).write_text(json.dumps(registry, indent=2, default=str))


def ensure_engines_file():
    p = Path(ENGINES_FILE)
    if not p.exists():
        p.write_text(
            '"""\nsearch_engines.py\n'
            'AUTO-GENERATED — do not edit manually.\n'
            'Each function is generated by discoverer.py\n"""\n\n'
            'from urllib.parse import quote_plus\n\n'
        )


def append_to_engines(code: str, domain_fn: str):
    p    = Path(ENGINES_FILE)
    text = p.read_text() if p.exists() else ""
    fn   = f"search_{domain_fn}"
    pattern = rf"\nasync def {re.escape(fn)}\(.*?(?=\nasync def |\Z)"
    text    = re.sub(pattern, "", text, flags=re.DOTALL).rstrip()
    text   += f"\n\n{code}\n"
    p.write_text(text)
    log.info(f"  [engines] appended {fn}() to {ENGINES_FILE}")


# ─────────────────────────────────────────────────────────────────────────────
# ── FIX 5: POST-CODEGEN VALIDATION ───────────────────────────────────────────
# Run the generated function once in a real browser and verify it actually
# lands on a page with search results. If it fails or returns a blank/error
# page, regenerate once with the failure reason fed back to Groq.
# ─────────────────────────────────────────────────────────────────────────────

VALIDATION_MIN_LINKS = 2   # minimum article links to consider a page "live"

async def _validate_generated_code(
    code: str,
    domain: str,
    domain_fn: str,
    query: str,
) -> tuple[bool, str]:
    """
    Dynamically exec() the generated function, call it with a real Playwright
    page, and check the result page has actual content.

    Returns (ok: bool, reason: str).
    """
    # ── 1. Syntax-check by compiling ─────────────────────────────────────────
    try:
        compile(code, "<codegen>", "exec")
    except SyntaxError as e:
        return False, f"SyntaxError: {e}"

    # ── 2. Run in a fresh browser context ────────────────────────────────────
    pw_inst = None
    browser = None
    try:
        namespace: dict = {}
        exec(code, namespace)                            # load the function
        fn_name = f"search_{domain_fn}"
        fn = namespace.get(fn_name)
        if fn is None:
            return False, f"function {fn_name!r} not found after exec"

        pw_inst = await async_playwright().start()
        browser = await pw_inst.chromium.launch(headless=True, args=LAUNCH_ARGS)
        ctx = await browser.new_context(
            user_agent=STEALTH_UA,
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="America/New_York",
            ignore_https_errors=True,
        )
        await apply_stealth_context(ctx)
        page: Page = await ctx.new_page()

        # ── 3. Call the generated function ───────────────────────────────────
        # Use asyncio.shield on the inner task so that when wait_for cancels
        # on timeout, the Playwright coroutine gets a proper CancelledError
        # rather than continuing to run against a closing browser (which causes
        # "Future exception was never retrieved" TargetClosedError leaks).
        try:
            task = asyncio.ensure_future(fn(page, query, days=7))
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=30)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                return False, "validation timed out (30s)"
        except Exception as e:
            return False, f"runtime error: {e}"

        # ── 4. Check the resulting page ───────────────────────────────────────
        final_url  = page.url
        final_html = await _get_body_html(page)

        if not final_html:
            return False, f"empty page after validation (url={final_url})"

        if _RE_ERROR.search(final_url) or "just a moment" in final_html[:500].lower():
            return False, f"bot-block or error page (url={final_url})"

        soup = BeautifulSoup(final_html, "html.parser")
        for t in soup(["script", "style", "noscript"]):
            t.decompose()

        # Count substantive article links (href > 30 chars, not js/anchor)
        links = [
            a for a in soup.find_all("a", href=True)
            if len(a.get("href", "")) > 30
            and not a["href"].startswith(("javascript", "#", "mailto"))
        ]
        body_text = soup.get_text(" ", strip=True)
        has_results_signal = bool(_RE_RESULTS.search(body_text))

        if len(links) >= VALIDATION_MIN_LINKS or has_results_signal:
            log.info(f"  [validate] ✅ ok — {len(links)} links, "
                     f"results_signal={has_results_signal}, url={final_url}")
            return True, "ok"
        else:
            return False, (
                f"no results content — {len(links)} links found, "
                f"results_signal={has_results_signal}, url={final_url}"
            )

    except Exception as e:
        return False, f"validation setup error: {e}"
    finally:
        for obj, method in [(browser, "close"), (pw_inst, "stop")]:
            if obj:
                try:
                    await getattr(obj, method)()
                except Exception:
                    pass


async def _generate_and_validate(
    domain: str,
    domain_fn: str,
    search_type: str,
    search_url: str | None,
    steps: list[str],
    result_page_html: str,
    query: str,
    max_attempts: int = 2,
) -> str:
    """
    Generate code, validate it, and retry once with failure feedback if needed.
    Always returns the best code string (even if validation ultimately failed —
    better to save imperfect code than nothing).
    """
    last_code = ""
    extra_hint = ""

    for attempt in range(1, max_attempts + 1):
        code = groq_generate_code(
            domain, domain_fn, search_type, search_url,
            steps, result_page_html,
            extra_hint=extra_hint,
        )
        print(f"  [Groq-codegen] attempt {attempt} — {len(code)} chars")
        last_code = code

        ok, reason = await _validate_generated_code(code, domain, domain_fn, query)
        print(f"  [validate] {'✅' if ok else '❌'} {reason}")

        if ok:
            return code

        if attempt < max_attempts:
            # Feed the failure reason back into the next codegen call
            extra_hint = (
                f"\n\nPREVIOUS ATTEMPT FAILED VALIDATION: {reason}\n"
                f"Fix the issue — check selectors, URL params, and wait patterns."
            )
            log.info(f"  [validate] retrying codegen with hint: {reason}")

    log.warning(f"  [validate] saving best-effort code after {max_attempts} attempts")
    return last_code


# ─────────────────────────────────────────────────────────────────────────────
# ── SHARED DOMAIN PROCESSOR  (used by both modes) ────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

async def _process_domain(domain: str, effective_query: str,
                           registry: dict) -> tuple[bool, str]:
    """
    Run discovery for *domain*, update registry, append to engines file.
    Returns (success: bool, status_msg: str).
    """
    try:
        result = await asyncio.wait_for(
            discover_domain(domain, effective_query),
            timeout=DOMAIN_TIMEOUT,
        )
    except asyncio.TimeoutError:
        print(f"  ⏰ TIMEOUT")
        registry[domain] = {"domain": domain, "status": "timeout"}
        save_registry(registry)
        return False, "timeout"
    except Exception as e:
        print(f"  💥 ERROR: {e}")
        registry[domain] = {"domain": domain, "status": "error", "error": str(e)}
        save_registry(registry)
        return False, f"error: {e}"

    if result:
        domain_fn = domain_to_fn(domain)
        append_to_engines(result["code"], domain_fn)
        registry[domain] = {
            "domain":      result["domain"],
            "search_type": result["search_type"],
            "search_url":  result["search_url"],
            "access":      result["access"],
        }
        save_registry(registry)
        return True, result["access"]
    else:
        registry[domain] = {"domain": domain, "status": "not_found"}
        save_registry(registry)
        return False, "not_found"


# ─────────────────────────────────────────────────────────────────────────────
# ── MAIN ─────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

async def main(
    single_url: str | None = None,
    query:      str | None = None,
    resume:     bool       = RESUME,
    limit:      int | None = LIMIT,
):
    """
    Entry point for both CLI and Jupyter use.

    Args:
        single_url : If set, discover this one domain and exit.
                     Accepts bare domain  (nature.com)
                     or full URL          (https://nature.com/some/path)
        query      : Override the default QUERY test string.
        resume     : Skip already-discovered domains  (default True).
        limit      : Cap the number of domains from INPUT_JSON (batch mode only).
    """
    effective_query = query or QUERY

    # ── SINGLE-URL MODE ───────────────────────────────────────────────────────
    if single_url:
        # Strip scheme and any path — keep only the bare hostname
        domain = re.sub(r"^https?://", "", single_url).split("/")[0].strip().lower()
        if not domain:
            print("❌  Could not parse a domain from the provided --url value.")
            return

        print(f"\n{'═'*60}")
        print(f"  Single-URL mode  :  {domain}")
        print(f"  Query            :  {effective_query!r}")
        print(f"{'═'*60}")

        registry = load_registry()
        ensure_engines_file()

        success, status = await _process_domain(domain, effective_query, registry)

        if success:
            rec = registry[domain]
            print(f"\n  ✅  {domain}  →  {rec['access']}()")
            print(f"       search_url : {rec['search_url']}")
            print(f"\n--- Generated code ---")
            # Re-read the generated function from the engines file for display
            engines_text = Path(ENGINES_FILE).read_text()
            fn  = rec["access"]
            m   = re.search(rf"(async def {re.escape(fn)}\(.*?)(?=\nasync def |\Z)",
                            engines_text, re.DOTALL)
            if m:
                print(m.group(1).strip())
        else:
            print(f"\n  ❌  Discovery failed for {domain}  (status: {status})")
        return

    # ── BATCH MODE ────────────────────────────────────────────────────────────
    with open(INPUT_JSON) as f:
        raw = json.load(f)
    platforms = raw if isinstance(raw, list) else raw.get("platforms", [])
    if limit:
        platforms = platforms[:limit]

    registry = load_registry()
    ensure_engines_file()

    total = found = skipped = errors = 0
    t0 = time.time()

    for p in platforms:
        raw_domain = p.get("domain", "").strip().lower()
        domain     = re.sub(r"^https?://", "", raw_domain).rstrip("/")
        name       = p.get("name", domain)

        if not domain:
            continue

        root = domain.split("/")[0]
        if root in SKIP_DOMAINS:
            print(f"  ⏭  {name} — skipped")
            skipped += 1
            continue

        if resume and domain in registry:
            rec = registry[domain]
            if rec.get("access"):
                print(f"  ✅ {name} — already found, skipping")
                skipped += 1
                continue
            else:
                prev_status = rec.get("status", "unknown")
                print(f"  🔄 {name} — retrying (prev status: {prev_status})")

        total += 1
        print(f"\n{'═'*60}")
        print(f"  [{total}] {name}  ({domain})")
        print(f"{'═'*60}")

        success, status = await _process_domain(domain, effective_query, registry)
        if success:
            found += 1
        else:
            errors += 1

        elapsed = round((time.time() - t0) / 60, 1)
        print(f"  [progress] {total} done | {found} found | "
              f"{errors} err | {skipped} skipped | {elapsed}min")

        await asyncio.sleep(INTER_DOMAIN_SLEEP)

    print(f"\n{'═'*60}")
    print(f"  DONE — {total} processed | {found} found | "
          f"{errors} errors | {skipped} skipped")
    print(f"  Registry → {REGISTRY_JSON}")
    print(f"  Engines  → {ENGINES_FILE}")
    print(f"{'═'*60}\n")

    for domain, rec in sorted(registry.items()):
        if rec.get("access"):
            print(f"  {domain:<40} → {rec['access']}()")


# ─────────────────────────────────────────────────────────────────────────────
# ── CLI ENTRY POINT ───────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="discoverer.py",
        description="Tree-based search URL discoverer for a list of domains or a single URL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Batch mode — process all domains in articles_clear_info.json:
  python discoverer.py

  # Single-URL mode — discover one site immediately:
  python discoverer.py --url nature.com
  python discoverer.py --url https://pubmed.ncbi.nlm.nih.gov
  python discoverer.py --url biopharmadive.com --query crispr

  # Batch mode with overrides:
  python discoverer.py --query crispr --limit 10 --no-resume
        """,
    )

    # ── Single-URL ────────────────────────────────────────────────────────────
    p.add_argument(
        "--url", "-u",
        metavar="URL",
        default=None,
        help=(
            "Discover search for a single domain and exit. "
            "Accepts a bare domain (nature.com) or a full URL "
            "(https://nature.com). All other batch options are ignored."
        ),
    )

    # ── Shared / overrides ────────────────────────────────────────────────────
    p.add_argument(
        "--query", "-q",
        metavar="TERM",
        default=None,
        help=f"Test query to use during discovery (default: {QUERY!r}).",
    )
    p.add_argument(
        "--resume",
        dest="resume",
        action=argparse.BooleanOptionalAction,
        default=RESUME,
        help="Skip domains already in the registry (default: --resume). Use --no-resume to force re-discovery.",
    )

    # ── Batch-only ────────────────────────────────────────────────────────────
    p.add_argument(
        "--limit", "-n",
        type=int,
        metavar="N",
        default=None,
        help="Process only the first N domains from the input JSON (batch mode).",
    )
    p.add_argument(
        "--input",
        metavar="FILE",
        default=INPUT_JSON,
        help=f"Input JSON file with domain list (default: {INPUT_JSON}).",
    )

    return p


def _run(coro):
    """
    Windows-safe asyncio entry point.
    On Windows, ProactorEventLoop (the default) leaves Playwright subprocess
    pipe transports open at GC time, producing noisy ResourceWarnings:
      "unclosed transport" / "I/O operation on closed pipe"
    These are harmless -- the pipes ARE closed, just not before GC runs.
    We suppress the two specific warning categories rather than switching to
    SelectorEventLoop (which breaks Playwright: it cannot spawn subprocesses).
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

    # Apply any file override before running
    if args.input != INPUT_JSON:
        INPUT_JSON = args.input

    _run(main(
        single_url=args.url,
        query=args.query,
        resume=args.resume,
        limit=args.limit,
    ))
else:
    # Jupyter: await main()  or  await main(single_url="nature.com", query="crispr")
    print("✅ Loaded.  Run:  await main()")
    print("           or:   await main(single_url='nature.com', query='crispr')")