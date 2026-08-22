# Contributing

## Ground rule: no unverified quality claims

This project's entire value is that its numbers hold up. Two hard rules:

1. **Prices** must link an official source URL. Corrections welcome — use the
   [price correction issue template](.github/ISSUE_TEMPLATE/price_correction.yml).
2. **Preservation / quality numbers** are only published from a judge-panel run
   (median + full spread, never a single judge). PRs adding "estimated" or
   single-judge quality figures will be declined regardless of how plausible
   they look.

## Dev setup

```bash
git clone https://github.com/AmperesAI/inference-index && cd inference-index

# end-to-end selftest — zero API keys needed (mock fleet)
python3 pipeline/mock_fleet.py --selftest

# rebuild the index
python3 pipeline/compute_index.py
python3 -m http.server -d docs 8080
```

CI runs the same selftest plus JSON validation on every push. A red build
blocks merge; `agent_verify.py` itself exits non-zero on any FAIL verdict.

## Good first contributions

- A verified route pair: run `pipeline/agent_verify.py` with ≥3 judges from
  different model families against a baseline/candidate pair and open a PR
  with the report JSON + exact command used.
- New provider in the Apify crawl config (`pipeline/apify/`).
- Task suites for new agent domains (`pipeline/agent_tasks.jsonl` format).
