"""
run_all_modalities.py
=====================
Runs the full pharma pipeline for each modality in sequence.
Called by GitHub Actions cron daily.

Output structure:
  data/
    bispecific_antibodies/
      YYYY-MM-DD_summary.md
      YYYY-MM-DD_articles.json
    monoclonal_antibodies/
      YYYY-MM-DD_summary.md
      ...
    molecular_glues/
      ...
    gene_editing/
      ...
"""

import asyncio, sys, os, pathlib

# ── Ensure repo root is on sys.path ──────────────────────────────────────────
_repo_root = str(pathlib.Path(__file__).resolve().parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

# =============================================================================
#  PRE-FLIGHT CHECK — fail fast with a clear message if files are missing
# =============================================================================

REQUIRED_FILES = [
    "extraction.py",
    "extraction_portals.py",
    "search_engines.py",
    "run_pipeline.py",
    "SUMMARIZER.py",
    "extractor_registry.json",
    "search_registry.json",
    "_stealth_constants.py",   # local stealth module used by extraction + discovery
]

def preflight_check():
    missing = [f for f in REQUIRED_FILES if not pathlib.Path(f).exists()]
    if missing:
        print("\n[preflight] MISSING FILES — pipeline cannot start:")
        for f in missing:
            print(f"  ✗  {f}")
        print("\n  These files must be committed to the repo root.")
        sys.exit(1)
    else:
        print("[preflight] All required files present ✓")

preflight_check()


from run_pipeline import run_pipeline

# =============================================================================
#  MODALITY DEFINITIONS
#  Each entry:
#    slug     → folder name under data/ and extraction_output/
#    query    → what gets sent to every portal's search box
#    keywords → extra search terms (comma-separated, passed as query variants)
# =============================================================================

MODALITIES = [
    {
        "slug":    "bispecific_antibodies",
        "label":   "Bispecific Antibodies",
        "query":   "bispecific antibodies BiTE T-cell engager bsAb",
    },
    {
        "slug":    "monoclonal_antibodies",
        "label":   "Monoclonal Antibodies",
        "query":   "monoclonal antibodies mAb therapeutic antibody",
    },
    {
        "slug":    "molecular_glues",
        "label":   "Molecular Glues",
        "query":   "molecular glue degrader TPD E3 ligase IKZF",
    },
    {
        "slug":    "gene_editing",
        "label":   "Gene Editing",
        "query":   "CRISPR Cas9 gene editing base editing prime editing",
    },
]

DATE_WINDOW_DAYS = int(os.environ.get("PIPELINE_DAYS", "7"))


async def run_all():
    results = {}

    for m in MODALITIES:
        slug  = m["slug"]
        label = m["label"]
        query = m["query"]

        print(f"\n{'#'*65}")
        print(f"  MODALITY: {label}")
        print(f"  Slug    : {slug}")
        print(f"  Query   : {query!r}")
        print(f"{'#'*65}\n")

        # Each modality gets its own extraction_output subfolder
        # so domains don't overwrite each other across modalities
        output_dir  = f"extraction_output/{slug}"
        briefs_dir  = f"data/{slug}"

        pathlib.Path(output_dir).mkdir(parents=True, exist_ok=True)
        pathlib.Path(briefs_dir).mkdir(parents=True, exist_ok=True)

        # Patch the globals in run_pipeline so it writes to the right folders
        import run_pipeline as rp
        rp.OUTPUT_DIR = output_dir
        rp.BRIEFS_DIR = briefs_dir
        rp.ARCHIVE_DIR = briefs_dir  # keep articles alongside brief

        try:
            await run_pipeline(
                query     = query,
                days      = DATE_WINDOW_DAYS,
                summarize = True,
            )
            results[slug] = "ok"
            print(f"\n[all_modalities] {label} — DONE")
        except Exception as e:
            results[slug] = f"ERROR: {e}"
            print(f"\n[all_modalities] {label} — FAILED: {e}")
            # Continue to next modality instead of aborting everything
            continue

    # ── Final summary ─────────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print("  ALL MODALITIES COMPLETE")
    print(f"{'='*65}")
    for slug, status in results.items():
        icon = "✓" if status == "ok" else "✗"
        print(f"  {icon}  {slug:<30} {status}")

    failed = [s for s, r in results.items() if r != "ok"]
    if failed:
        print(f"\n[all_modalities] {len(failed)} modality(s) failed — check logs above.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run_all())
