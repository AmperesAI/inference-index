# Bolt.new — Pilot Console build prompt

Paste the prompt below into [bolt.new](https://bolt.new) verbatim. It builds the **Inference Index Pilot Console**: the interactive companion app where a pilot partner (Databricks / Cursor / Nebius) pastes their workload mix and sees a per-workload quality-adjusted savings simulation, powered by the live index.

---

Build a single-page React app called "Inference Index — Pilot Console" with a dark Bloomberg-terminal aesthetic (near-black background #07090c, IBM Plex Mono for numbers, green #3ddc84 accents, thin #1d2530 borders).

Data: on load, fetch the live index from https://vnmoorthy.github.io/inference-index/data.json — it has shape { as_of, providers: [{ provider, models: [{ model, input_per_1m, output_per_1m, source }] }], verified_pairs: [{ baseline_model, cheap_model, preservation_median, preservation_spread, savings_pct, qix }] }.

Layout, top to bottom:
1. Header: "PILOT CONSOLE" + a live-dot badge + the data as_of date.
2. Workload builder: three sliders (Coding %, Extraction/Summarization %, Reasoning/Chat % — always summing to 100) plus a "Monthly token volume" input (default 5B tokens/month) and a "Current model" dropdown populated from the fetched models.
3. Simulation panel: pick a "Downshift candidate" model from a second dropdown. Compute monthly cost for each at blended price = (3×input + output)/4 per 1M tokens × volume. Show: current monthly cost, downshifted monthly cost, raw savings %, and — if the candidate appears in verified_pairs — the quality-preserved savings "QIX = savings% × preservation_median" with the preservation spread printed underneath and a green VERIFIED badge; otherwise show an amber "PANEL PENDING — raw savings only, quality unverified" badge.
4. A large animated counter showing projected annual savings in dollars.
5. Three pilot-track cards: "Provider · Databricks", "Buyer · Cursor", "Open-model · Nebius", each with one sentence on what the index gives them.
6. Footer: link to github.com/vnmoorthy/inference-index and the line "usable ≠ equivalent: on real traffic the cheap model is usable ~92–95% of the time but equivalent-or-better only ~21–32% — route with escalation."

No backend, no auth, everything client-side. Handle the fetch failing with a friendly retry card. Make it deploy-ready.
