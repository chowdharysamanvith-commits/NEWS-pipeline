import json
from pathlib import Path

INPUT_DIR   = "./extraction_output"
OUTPUT_FILE = "allinone.json"

all_articles = []

for file in sorted(Path(INPUT_DIR).glob("*.json")):
    if file.name in ("failed.json", "empty.json"):
        continue
    try:
        data = json.load(open(file, encoding="utf-8"))

        if isinstance(data, dict):
            for period_data in data.values():
                if isinstance(period_data, dict):
                    all_articles.extend(period_data.get("articles", []))
                elif isinstance(period_data, list):
                    all_articles.extend(period_data)
        elif isinstance(data, list):
            all_articles.extend(data)

        print(f"✓ {file.name}")
    except Exception as e:
        print(f"✗ {file.name} — {e}")

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump({"articles": all_articles}, f, indent=2, ensure_ascii=False)

print(f"\n✅ Done — {len(all_articles)} articles → {OUTPUT_FILE}")