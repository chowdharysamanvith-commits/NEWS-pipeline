"""
SUMMARIZER.py  —  Pharma Intelligence Brief Generator

Reads NVIDIA_API_KEY from environment variable (set as GitHub secret).
Falls back to hardcoded key if env var not present (local dev only).

Usage:
  python SUMMARIZER.py --input filtered_files.json --query "PROTAC"
  python SUMMARIZER.py --input filtered_files.json --query "CAR-T" --output briefs.txt
"""

import argparse, json, os, sys, requests
from pathlib import Path

# ── NVIDIA CONFIG ─────────────────────────────────────────────────────────────
# Key is read from environment variable NVIDIA_API_KEY.
# Set this as a GitHub secret — never hardcode it.

INVOKE_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

_raw_key = os.environ.get("NVIDIA_API_KEY", "")
API_KEY  = f"Bearer {_raw_key}" if _raw_key and not _raw_key.startswith("Bearer") else _raw_key

MODEL = "qwen/qwen3.5-122b-a10b"

HEADERS = {
    "Authorization": API_KEY,
    "Accept": "text/event-stream",
}

# ── SYSTEM PROMPT ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are a pharmaceutical intelligence analyst specializing in biotechnology, drug development, and clinical trial reporting.
Your task is to convert a collection of pharmaceutical news articles into a single unified MONTHLY PHARMA INTELLIGENCE BRIEF.

OUTPUT STRUCTURE:
Use the following exact sections and headings.

1. Overview
Provide a concise summary (5-10 sentences) describing the most important pharmaceutical developments across all articles provided.

2. Key Developments
List the major developments reported across all articles.
Each bullet must contain:
- Company name
- Drug name
- Indication (disease)
- Trial phase or regulatory stage
- Outcome or key result
Format:
• Company — Drug — Indication — Development

3. Companies in Focus
List the companies mentioned and explain their strategic activity.
Include:
- Company name
- Strategic objective (pipeline expansion, trial success, regulatory filing, acquisition, etc.)
- Associated drug or program

4. Clinical & Scientific Highlights
Extract scientific or clinical trial information including:
- Drug mechanism of action
- Trial phase
- Patient population
- Comparator treatment (if mentioned)
- Clinical endpoints or results (e.g., progression-free survival)
Do NOT generalize. Only report information explicitly stated in the articles.

5. Business & Deals
Report business or strategic implications such as:
- acquisitions
- partnerships
- licensing deals
- pipeline positioning
- competitive positioning
If no such information exists, write:
"No relevant business developments reported."

STRICT RULES:
- Use only information explicitly present in the articles.
- Do NOT hallucinate data.
- Do NOT invent statistics or trial results.
- Every sentence must reference a real entity (company, drug, trial, regulator).
- Avoid generic language such as "the company aims to improve outcomes".
- Write in a formal pharmaceutical intelligence tone similar to industry analyst reports.
- Only and Only include data if it is related to the query {query}.
STYLE REQUIREMENTS:
- Concise
- Fact-driven
- Technical but readable
- No marketing language

OUTPUT FORMAT:
Plain text with the exact section headings listed above.
Along with the link (source) of the article(s) that support each key point, in parentheses at the end of the relevant sentence.
"""

# ── HELPERS ───────────────────────────────────────────────────────────────────

def build_combined_prompt(articles, query):
    sections = []
    for i, art in enumerate(articles, 1):
        title  = art.get("title", "Untitled")
        source = art.get("url", "Unknown")
        date   = art.get("date") or art.get("period", "")
        body   = (art.get("text") or "").strip()
        if not body:
            continue
        sections.append(
            f"--- ARTICLE {i} ---\n"
            f"Source : {source}\n"
            f"Date   : {date}\n"
            f"Title  : {title}\n\n"
            f"{body}"
        )

    return f"""
Query focus: {query}

Below are {len(sections)} pharmaceutical news articles.
Synthesize ALL of them into a single unified MONTHLY PHARMA INTELLIGENCE BRIEF.
Do not summarize each article separately — combine insights across all articles.

{chr(10).join(sections)}
"""


def call_nvidia_api(system_prompt, user_prompt, query):
    # Re-read key at call time so hot-patching from run_pipeline works
    raw = os.environ.get("NVIDIA_API_KEY", "")
    auth = f"Bearer {raw}" if raw and not raw.startswith("Bearer") else (raw or API_KEY)

    headers = {
        "Authorization": auth,
        "Accept": "text/event-stream",
    }

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "max_tokens": 16384,
        "temperature": 0.30,
        "top_p": 0.95,
        "stream": True,
        "chat_template_kwargs": {"enable_thinking": True},
    }

    response = requests.post(INVOKE_URL, headers=headers, json=payload, stream=True)
    response.raise_for_status()

    output = ""
    for line in response.iter_lines():
        if not line:
            continue
        decoded = line.decode("utf-8")
        if decoded.startswith("data: "):
            data_str = decoded[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    output += content
                    print(content, end="", flush=True)
            except json.JSONDecodeError:
                continue

    print()
    return output.strip()


def write_output(brief, query, article_count, output_path):
    header = (
        "=" * 80 + "\n"
        "  MONTHLY PHARMA INTELLIGENCE BRIEF\n"
        f"  Query   : {query}\n"
        f"  Articles: {article_count}\n"
        "=" * 80 + "\n\n"
    )
    footer = "\n" + "=" * 80
    full = header + brief + footer

    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(full, encoding="utf-8")
        print(f"\n[INFO] Saved brief → {output_path}")

    return full


def chunk_articles(articles, chunk_size=5):
    for i in range(0, len(articles), chunk_size):
        yield articles[i:i + chunk_size]


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Summarize all pharma articles into one combined intelligence brief."
    )
    p.add_argument("--input",  "-i", default="filtered_files.json")
    p.add_argument("--output", "-o", default=None)
    p.add_argument("--query",  "-q", required=True)
    return p.parse_args()


def main():
    args = parse_args()

    if not _raw_key:
        sys.exit("[ERROR] NVIDIA_API_KEY environment variable is not set.")

    path = Path(args.input)
    if not path.exists():
        sys.exit(f"[ERROR] File not found: {path}")

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    articles = data.get("articles", []) if isinstance(data, dict) else data

    valid   = [a for a in articles if (a.get("text") or "").strip()]
    skipped = len(articles) - len(valid)

    print(f"[INFO] Loaded   : {len(articles)} articles")
    if skipped:
        print(f"[INFO] Skipped  : {skipped} articles (empty text)")
    print(f"[INFO] Using    : {len(valid)} articles")
    print(f"[INFO] Query    : {args.query}")

    if not valid:
        sys.exit("[WARN] No articles with usable text found.")

    all_summaries = []
    chunks = list(chunk_articles(valid, chunk_size=5))
    print(f"[INFO] Processing in {len(chunks)} chunks...\n")

    for idx, chunk in enumerate(chunks, 1):
        print(f"[INFO] Chunk {idx}/{len(chunks)}")
        chunk_prompt = build_combined_prompt(chunk, args.query)
        try:
            summary = call_nvidia_api(SYSTEM_PROMPT, chunk_prompt, args.query)
            all_summaries.append(summary)
        except Exception as e:
            print(f"[ERROR] Chunk {idx} failed: {e}")

    if not all_summaries:
        sys.exit("[WARN] All chunks failed.")

    print("\n[INFO] Generating FINAL combined brief...\n")
    final_prompt = (
        "You are given multiple partial pharmaceutical summaries.\n"
        "Combine them into ONE unified MONTHLY PHARMA INTELLIGENCE BRIEF.\n\n"
        + "\n\n".join(all_summaries)
    )
    final_brief = call_nvidia_api(SYSTEM_PROMPT, final_prompt, args.query)

    if not final_brief:
        sys.exit("[WARN] Empty response from model.")

    write_output(final_brief, args.query, len(valid), args.output)


if __name__ == "__main__":
    main()
