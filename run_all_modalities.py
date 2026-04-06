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
      ...
    molecular_glues/
      ...
    gene_editing/
      ...

  extraction_output/
    bispecific_antibodies/
      fiercebiotech_com_results.json
      ...
      merged_articles.json
    monoclonal_antibodies/
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
    "_stealth_constants.py",
]

def preflight_check():
    missing = [f for f in REQUIRED_FILES if not pathlib.Path(f).exists()]
    if missing:
        print("\n[preflight] MISSING FILES — pipeline cannot start:")
        for f in missing:
            print(f"  ✗  {f}")
        print("\n  These files must be committed to the repo root.")
        sys.exit(1)
    print("[preflight] All required files present ✓")

preflight_check()

# Import both modules NOW so we can patch their globals reliably
import run_pipeline as rp
import extraction   as ex

# =============================================================================
#  MODALITY DEFINITIONS
# =============================================================================

MODALITIES = [
    {
        "slug":  "bispecific_antibodies",
        "label": "Bispecific Antibodies",
        "query": "bispecific antibodies",
    },
    {
        "slug":  "monoclonal_antibodies",
        "label": "Monoclonal Antibodies",
        "query": "monoclonal antibodies",
    },
    {
        "slug":  "molecular_glues",
        "label": "Molecular Glues",
        "query": "molecular glue",
    },
    {
        "slug":  "gene_editing",
        "label": "Gene Editing",
        "query": "CRISPR Gene Editing",
    },
]

DATE_WINDOW_DAYS = int(os.environ.get("PIPELINE_DAYS", "7"))


def _patch_output_dir(output_dir: str, briefs_dir: str) -> None:
    """
    Patch OUTPUT_DIR in EVERY module that uses it so they all
    read from and write to the same per-modality folder.

    The bug was that run_pipeline.OUTPUT_DIR was patched but
    extraction.OUTPUT_DIR was not — so extraction wrote files to
    'extraction_output/' while merge looked in
    'extraction_output/<slug>/' and found nothing.
    """
    # run_pipeline uses OUTPUT_DIR for merge_results() and BRIEF_FILE path
    rp.OUTPUT_DIR   = output_dir
    rp.BRIEFS_DIR   = briefs_dir
    rp.ARCHIVE_DIR  = briefs_dir

    # extraction uses OUTPUT_DIR to write *_results.json and empty/failed.json
    ex.OUTPUT_DIR   = output_dir

    # Make sure both directories exist before the run starts
    pathlib.Path(output_dir).mkdir(parents=True, exist_ok=True)
    pathlib.Path(briefs_dir).mkdir(parents=True, exist_ok=True)

    print(f"[patch] extraction_output → {output_dir}")
    print(f"[patch] briefs/data       → {briefs_dir}")


async def run_all():
    results = {}

    for m in MODALITIES:
        slug  = m["slug"]
        label = m["label"]
        query = m["query"]

        output_dir = f"extraction_output/{slug}"
        briefs_dir = f"data/{slug}"

        print(f"\n{'#'*65}")
        print(f"  MODALITY : {label}")
        print(f"  Slug     : {slug}")
        print(f"  Query    : {query!r}")
        print(f"  Out dir  : {output_dir}")
        print(f"{'#'*65}\n")

        # ── Patch BOTH modules before every modality run ──────────────────
        _patch_output_dir(output_dir, briefs_dir)

        try:
            await rp.run_pipeline(
                query     = query,
                days      = DATE_WINDOW_DAYS,
                summarize = True,
            )
            results[slug] = "ok"
            print(f"\n[all_modalities] {label} — DONE")

        except Exception as e:
            results[slug] = f"ERROR: {e}"
            print(f"\n[all_modalities] {label} — FAILED: {e}")
            continue

        # ── Verify files were actually written (helps catch silent failures) ─
        written = list(pathlib.Path(output_dir).glob("*_results.json"))
        merged  = pathlib.Path(output_dir) / "merged_articles.json"
        print(f"[verify] {len(written)} *_results.json files in {output_dir}/")
        print(f"[verify] merged_articles.json exists: {merged.exists()}")
        if merged.exists():
            import json
            data = json.loads(merged.read_text(encoding="utf-8"))
            print(f"[verify] total_articles in merged: {data.get('total_articles', '?')}")

    # ── Final summary ──────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print("  ALL MODALITIES COMPLETE")
    print(f"{'='*65}")
    for slug, status in results.items():
        icon = "✓" if status == "ok" else "✗"
        print(f"  {icon}  {slug:<30} {status}")

    failed = [s for s, r in results.items() if r != "ok"]
    if failed:
        print(f"\n[all_modalities] {len(failed)} modality(s) failed.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run_all())
