# PharmaIntel — News Intelligence Pipeline

Automated daily scraping, extraction, and summarization of pharmaceutical news across 30+ industry portals.

---

## How it works

```
Every day at 06:00 UTC
        ↓
GitHub Actions runner spins up
        ↓
extraction.py  →  scrapes all portals in extractor_registry.json
        ↓
merge_results()  →  flattens all *_results.json into merged_articles.json
        ↓
SUMMARIZER.py  →  calls NVIDIA NIM (Qwen 3.5) → produces intelligence brief
        ↓
Brief saved to  data/briefs/YYYY-MM-DD_<query>_summary.md
        ↓
git commit + push back to repo
```

---

## One-time setup

### 1. Fork / clone this repo

```bash
git clone https://github.com/YOUR_USERNAME/News-Summarizer.git
cd News-Summarizer
```

### 2. Add GitHub Secrets

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret name | Where to get it |
|---|---|
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) → API Keys |
| `NVIDIA_API_KEY` | [integrate.api.nvidia.com](https://integrate.api.nvidia.com) → API Keys |

> **Never put API keys in code or commit them.** The workflow reads them from secrets only.

### 3. Enable GitHub Actions

Go to **Actions** tab → click **"I understand my workflows, go ahead and enable them"** if prompted.

### 4. Install portals (one-time)

Go to **Actions → Pharma Intelligence — Install Portals → Run workflow**

Fill in:
- `query`: `drug discovery` (used for search discovery)
- `limit`: `5` (install 5 portals at a time to avoid timeout)

Repeat until all portals in `search_registry.json` are installed.

---

## Daily runs

The pipeline runs automatically every day at **06:00 UTC (11:30 IST)**.

You can also trigger it manually:

**Actions → Pharma Intelligence — Daily Pipeline → Run workflow**

Parameters:
| Input | Default | Description |
|---|---|---|
| `query` | `monoclonal antibodies` | What to search for |
| `days` | `7` | How many days back to scrape |
| `domain` | *(blank = all)* | Restrict to one portal |
| `no_summarize` | `false` | Skip the NVIDIA summarizer step |

---

## Output files

| Path | Description |
|---|---|
| `data/briefs/YYYY-MM-DD_<query>_summary.md` | Daily intelligence brief |
| `data/archive/YYYY-MM-DD_articles.json` | Raw article snapshot |
| `extraction_output/merged_articles.json` | Flat merged article list |

Briefs are committed back to the repo automatically after each run.

---

## Repository structure

```
.
├── .github/
│   └── workflows/
│       ├── daily_pipeline.yml      ← runs every day at 06:00 UTC
│       └── install_portals.yml     ← manual: set up new portals
│
├── data/
│   ├── briefs/                     ← daily summaries committed here
│   └── archive/                    ← raw article snapshots
│
├── extraction_output/              ← per-domain JSONs (not committed)
│
├── run_pipeline.py                 ← main entry point
├── SUMMARIZER.py                   ← NVIDIA NIM brief generator
├── extraction.py                   ← scraper orchestrator
├── extraction_portals.py           ← per-domain extract functions
├── search_engines.py               ← per-domain search navigation
├── discovery.py                    ← Groq-powered portal discovery
├── install.py                      ← portal installer
├── extractor_registry.json         ← installed portals
├── search_registry.json            ← search URL map
├── requirements.txt
└── .gitignore
```

---

## Running locally

```bash
# Install dependencies
pip install -r requirements.txt
playwright install chromium --with-deps

# Set env vars (never hardcode)
export GROQ_API_KEY="gsk_..."
export NVIDIA_API_KEY="nvapi-..."

# Run the full pipeline
python run_pipeline.py --query "monoclonal antibodies" --days 7

# Run only extraction (no summarizer)
python run_pipeline.py --query "CRISPR" --no-summarize

# Run on a single portal
python run_pipeline.py --domain biopharmadive.com --query "biosimilar"

# Install a new portal
python run_pipeline.py --install --url endpoints.news
```

---

## Cron schedule

Modify `.github/workflows/daily_pipeline.yml` to change the schedule:

```yaml
schedule:
  - cron: "0 6 * * *"    # 06:00 UTC daily  (11:30 IST)
  # - cron: "0 1 * * *"  # 01:00 UTC daily  (06:30 IST)
  # - cron: "0 6 * * 1"  # Every Monday only
```

[Cron expression reference](https://crontab.guru)
