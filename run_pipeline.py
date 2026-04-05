"""
run_pipeline.py
===============
Top-level entry point for the Pharma News Intelligence Pipeline.

TWO MODES:

  --install   (run once per new portal)
    Calls install.py to:
      Step 1 -- search URL discovery  (discovery.py + Groq)
      Step 2 -- extraction parser     (Groq writes extract_<domain>())
      Step 3 -- article scraper       (Groq writes article_<domain>())
    Writes: search_registry.json, search_engines.py,
            extractor_registry.json, extraction_portals.py

  (default)   (run daily / on-demand)
    Calls extraction.py to:
      Stage 1 -- navigate to search page   (search_engines.py)
      Stage 2 -- extract article links     (extraction_portals.py)
      Stage 3 -- crawl pages
      Stage 4 -- filter to last N days
      Stage 5 -- scrape article bodies     (extraction_portals.py)
      Stage 6 -- save per-domain JSON
    Then merges all *_results.json → merged_articles.json
    Then calls SUMMARIZER to produce a MONTHLY PHARMA INTELLIGENCE BRIEF
    Brief is saved to data/briefs/<date>_<query>_summary.md

Usage (terminal):
  python run_pipeline.py
  python run_pipeline.py --query "monoclonal antibodies" --days 7
  python run_pipeline.py --domain biopharmadive.com --query protac
  python run_pipeline.py --install
  python run_pipeline.py --install --url biopharmadive.com
"""

# =============================================================================
#  IMPORTS  (must come before CONFIG so os is available)
# =============================================================================

import argparse, asyncio, json, os, sys, time, logging, pathlib
from datetime import datetime, timezone
from pathlib import Path

# ── Ensure repo root is always on sys.path so local imports work in Actions ──
_repo_root = str(pathlib.Path(__file__).resolve().parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# =============================================================================
#  CONFIG  (uses os — must come after imports)
# =============================================================================

QUERY            = "monoclonal antibodies"
DATE_WINDOW_DAYS = 7
ENRICH_ARTICLES  = True
OUTPUT_DIR       = os.environ.get("PIPELINE_OUTPUT_DIR", "extraction_output")
MERGED_FILE      = "merged_articles.json"
BRIEFS_DIR       = os.environ.get("PIPELINE_BRIEFS_DIR",  "data/briefs")
ARCHIVE_DIR      = os.environ.get("PIPELINE_ARCHIVE_DIR", "data/archive")

# =============================================================================
#  ENV KEY INJECTION
#  Patch SUMMARIZER and extraction modules to read keys from env at runtime.
# =============================================================================

def _inject_env_keys():
    """
    Override hardcoded API keys in SUMMARIZER.py and any other module
    that uses them.  This is called before any module import.

    Keys expected in environment (set as GitHub secrets):
      NVIDIA_API_KEY   — used by SUMMARIZER.py
      GROQ_API_KEY     — used by install.py, discovery.py
    """
    nvidia_key = os.environ.get("NVIDIA_API_KEY", "")
    groq_key   = os.environ.get("GROQ_API_KEY", "")

    if not nvidia_key:
        log.warning("NVIDIA_API_KEY not set — summarizer will fail.")
    if not groq_key:
        log.warning("GROQ_API_KEY not set — install/install step will fail.")

    # These will be read by SUMMARIZER.py and install.py after import
    os.environ.setdefault("NVIDIA_API_KEY", nvidia_key)
    os.environ.setdefault("GROQ_API_KEY", groq_key)

_inject_env_keys()

# =============================================================================
#  PATHS
# =============================================================================

def _ensure_dirs():
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    Path(BRIEFS_DIR).mkdir(parents=True, exist_ok=True)
    Path(ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)

# =============================================================================
#  MERGE
# =============================================================================

def merge_results(query: str, days: int) -> list:
    output_path = Path(OUTPUT_DIR)
    all_articles: list = []

    for fpath in sorted(output_path.glob("*_results.json")):
        domain_key = fpath.stem.replace("_results", "").replace("_", ".")
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"[merge] Could not read {fpath.name}: {e}")
            continue

        for month, month_data in data.items():
            for art in month_data.get("articles", []):
                art.setdefault("domain", domain_key)
                art.setdefault("period", month)
                all_articles.append(art)

    merged_path = output_path / MERGED_FILE
    with open(merged_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "query":          query,
                "date_window":    f"last {days} days",
                "merged_at":      datetime.now(timezone.utc).isoformat(),
                "total_articles": len(all_articles),
                "articles":       all_articles,
            },
            f, indent=2,
        )

    # Also snapshot to archive
    date_str     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    archive_path = Path(ARCHIVE_DIR) / f"{date_str}_articles.json"
    import shutil
    shutil.copy(merged_path, archive_path)

    print(f"\n[merge] {len(all_articles)} articles → {merged_path}")
    print(f"[merge] Snapshot → {archive_path}")
    return all_articles


# =============================================================================
#  SUMMARIZER RUNNER
# =============================================================================

def run_summarizer(articles: list, query: str) -> str | None:
    if not articles:
        print("[summarizer] No articles to summarize — skipping.")
        return None

    try:
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("SUMMARIZER", "SUMMARIZER.py")
        if spec is None:
            raise ImportError("SUMMARIZER.py not found in working directory.")
        mod = importlib.util.module_from_spec(spec)

        # Inject env key into the module's namespace before exec
        nvidia_key = os.environ.get("NVIDIA_API_KEY", "")
        if nvidia_key:
            # SUMMARIZER.py hardcodes API_KEY — we patch it after load
            pass

        sys.modules["SUMMARIZER"] = mod
        spec.loader.exec_module(mod)

        # Patch hardcoded key if module loaded with a placeholder
        if nvidia_key and hasattr(mod, "API_KEY"):
            mod.API_KEY = f"Bearer {nvidia_key}"
            mod.HEADERS = {
                "Authorization": mod.API_KEY,
                "Accept": "text/event-stream",
            }

    except Exception as e:
        import traceback
        print(f"[summarizer] ERROR importing SUMMARIZER.py: {e}")
        traceback.print_exc()
        return None

    chunks = list(mod.chunk_articles(articles, chunk_size=5))
    print(f"[summarizer] {len(articles)} articles in {len(chunks)} chunks — streaming...\n")
    print("─" * 65)

    all_summaries = []
    for idx, chunk in enumerate(chunks, 1):
        print(f"[summarizer] Chunk {idx}/{len(chunks)}")
        chunk_prompt = mod.build_combined_prompt(chunk, query)
        try:
            summary = mod.call_nvidia_api(mod.SYSTEM_PROMPT, chunk_prompt, query)
            if summary:
                all_summaries.append(summary)
        except Exception as e:
            print(f"[summarizer] Chunk {idx} failed: {e}")

    if not all_summaries:
        print("[summarizer] All chunks failed — no brief produced.")
        return None

    print("\n[summarizer] Generating final combined brief...\n")
    final_prompt = (
        "You are given multiple partial pharmaceutical summaries.\n\n"
        "Combine them into ONE unified MONTHLY PHARMA INTELLIGENCE BRIEF.\n\n"
        + "\n\n".join(all_summaries)
    )
    final_brief = mod.call_nvidia_api(mod.SYSTEM_PROMPT, final_prompt, query)
    print("─" * 65)

    if not final_brief:
        print("[summarizer] Empty final response.")
        return None

    # ── Date-stamped output path ──────────────────────────────────────────────
    date_str    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    safe_query  = query.replace(" ", "_").replace("/", "-")[:40]
    brief_fname = f"{date_str}_{safe_query}_summary.md"
    brief_path  = Path(BRIEFS_DIR) / brief_fname

    full_output = mod.write_output(final_brief, query, len(articles), str(brief_path))
    print(f"[summarizer] Brief saved → {brief_path}")
    return full_output


# =============================================================================
#  MAIN
# =============================================================================

async def run_pipeline(
    install:      bool       = False,
    url:          str | None = None,
    query:        str        = QUERY,
    days:         int        = DATE_WINDOW_DAYS,
    enrich:       bool       = ENRICH_ARTICLES,
    limit:        int | None = None,
    skip_search:  bool       = False,
    skip_article: bool       = False,
    resume:       bool       = True,
    domain:       str | None = None,
    summarize:    bool       = True,
):
    _ensure_dirs()

    t0      = time.time()
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if install:
        print(f"\n{'='*65}")
        print(f"  PHARMA PIPELINE -- INSTALL MODE")
        print(f"  Query   : {query!r}")
        print(f"  Target  : {url or 'all portals in articles_clear_info.json'}")
        print(f"  Started : {started}")
        print(f"{'='*65}")

        try:
            from install import install as run_install
        except Exception as e:
            import traceback
            print(f"  ERROR importing install.py: {e}")
            traceback.print_exc()
            raise RuntimeError(f"Failed to import install.py: {e}") from e

        await run_install(
            url          = url,
            query        = query,
            limit        = limit,
            resume       = resume,
            skip_search  = skip_search,
            skip_article = skip_article,
        )

    else:
        target_domain = domain or url
        print(f"\n{'='*65}")
        print(f"  PHARMA PIPELINE -- EXTRACTION + MERGE + SUMMARIZE")
        print(f"  Query       : {query!r}")
        print(f"  Date window : last {days} days")
        print(f"  Domain      : {target_domain or 'all installed portals'}")
        print(f"  Enrich      : {enrich}")
        print(f"  Summarize   : {summarize}")
        print(f"  Briefs dir  : {BRIEFS_DIR}/")
        print(f"  Started     : {started}")
        print(f"{'='*65}")

        try:
            from extraction import main as run_extraction
        except Exception as e:
            import traceback
            print(f"  ERROR importing extraction.py: {e}")
            traceback.print_exc()
            raise RuntimeError(f"Failed to import extraction.py: {e}") from e

        await run_extraction(
            query  = query,
            domain = target_domain,
            limit  = limit,
            enrich = enrich,
            days   = days,
        )

        print(f"\n{'='*65}")
        print(f"  STAGE B -- MERGE")
        print(f"{'='*65}")
        articles = merge_results(query=query, days=days)

        if not articles:
            print("\n[merge] No articles found — nothing to summarize.")
            elapsed = round((time.time() - t0) / 60, 1)
            print(f"\n  Total time: {elapsed} minutes")
            return

        if summarize:
            print(f"\n{'='*65}")
            print(f"  STAGE C -- SUMMARIZE  ({len(articles)} articles, query={query!r})")
            print(f"{'='*65}")
            run_summarizer(articles=articles, query=query)
        else:
            print("\n[summarize] Skipped (--no-summarize).")

    elapsed = round((time.time() - t0) / 60, 1)
    print(f"\n  Total time: {elapsed} minutes")


# =============================================================================
#  CLI
# =============================================================================

def _build_parser():
    p = argparse.ArgumentParser(
        prog="run_pipeline.py",
        description="Pharma News Intelligence Pipeline.",
    )
    p.add_argument("--install", action="store_true", default=False)
    p.add_argument("--url",    "-u", default=None)
    p.add_argument("--query",  "-q", default=QUERY)
    p.add_argument("--limit",  "-n", type=int, default=None)
    p.add_argument("--domain", "-d", default=None)
    p.add_argument("--days",         type=int, default=DATE_WINDOW_DAYS)
    p.add_argument("--enrich", dest="enrich",
                   action=argparse.BooleanOptionalAction, default=ENRICH_ARTICLES)
    p.add_argument("--summarize", dest="summarize",
                   action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--skip-search",  action="store_true", default=False)
    p.add_argument("--skip-article", action="store_true", default=False)
    p.add_argument("--resume", dest="resume",
                   action=argparse.BooleanOptionalAction, default=True)
    return p


if __name__ == "__main__":
    args = _build_parser().parse_args()
    asyncio.run(run_pipeline(
        install      = args.install,
        url          = args.url,
        query        = args.query,
        days         = args.days,
        enrich       = args.enrich,
        limit        = args.limit,
        skip_search  = args.skip_search,
        skip_article = args.skip_article,
        resume       = args.resume,
        domain       = args.domain,
        summarize    = args.summarize,
    ))
else:
    print("OK  run_pipeline.py loaded.")
