#!/usr/bin/env python3
"""Build docs/data.json for the Inference Index site.

Inputs
------
pipeline/seed_prices.json   scraped price snapshot (from Apify/n8n, or the
                            checked-in snapshot refreshed at build time)
docs/data.json (previous)   used to compute delta_pct for the ticker

Quality layer: verified route pairs from the 10-judge panel methodology
(TMLR-track research, 2026). Preservation is ONLY attached where the panel
actually ran — no modeled guesses.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "pipeline" / "seed_prices.json"
OUT = ROOT / "docs" / "data.json"

# Panel-verified route pairs. Source: 10-independent-judge re-scoring of the
# 10K downshift artifact (Llama-3.1-70B -> 8B). Cost reduction is arithmetic
# and judge-independent; preservation is panel median with full spread.
VERIFIED_PAIRS = [
    {
        "baseline_model": "Llama 3.1 70B Instruct",
        "cheap_model": "Llama 3.1 8B Instruct",
        "preservation_median": 0.90,
        "preservation_spread": "61–97% across 10 judges",
        "savings_pct": 98.35,
        "qix": 88.5,
        "note": "Benchmark workloads. Real-traffic caveat: usable ~92–95%, equivalent-or-better ~21–32%.",
    }
]


def blended(m: dict) -> float:
    return (3 * m["input_per_1m"] + m["output_per_1m"]) / 4


def norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def main() -> None:
    seed = json.loads(SEED.read_text())

    # Anchor each verified pair to the exact scraped model name so the site
    # can join on it (catalog names vary: "Llama 3.1 8B" vs "Meta-Llama-3.1-8B-Instruct-fast").
    all_names = [m["model"] for p in seed["providers"] for m in p["models"]]
    for pair in VERIFIED_PAIRS:
        pair.setdefault("cheap_model_label", pair["cheap_model"])
        for name in all_names:
            if norm(pair["cheap_model_label"]).replace("instruct", "") in norm(name).replace("instruct", "").replace("meta", "").replace("fast", ""):
                pair["cheap_model"] = name
                break

    prev_rows: dict[str, float] = {}
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text())
            for p in prev.get("providers", []):
                for m in p.get("models", []):
                    prev_rows[m["model"].lower()] = blended(m)
        except json.JSONDecodeError:
            pass

    for p in seed["providers"]:
        for m in p["models"]:
            old = prev_rows.get(m["model"].lower())
            new = blended(m)
            m["delta_pct"] = round(100 * (new - old) / old) if old else None

    out = {
        "as_of": seed.get("as_of", date.today().isoformat()),
        "methodology": "Blended $/1M = (3*input + output)/4. $/MQT = blended / panel-median preservation, verified pairs only.",
        "providers": seed["providers"],
        "verified_pairs": VERIFIED_PAIRS,
        "skipped": seed.get("skipped", []),
        "notes": seed.get("notes", []),
    }
    OUT.write_text(json.dumps(out, indent=2) + "\n")
    n = sum(len(p["models"]) for p in seed["providers"])
    print(f"wrote {OUT} — {n} models, {len(seed['providers'])} providers, {len(VERIFIED_PAIRS)} verified pair(s)")


if __name__ == "__main__":
    main()
