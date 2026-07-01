# Red Team — Captured Evidence Artifacts

These files are **captured evidence** that the red-team pipeline runs end to end
against Amazon Bedrock: the PASS path, the breach path, and the breach-report
JSON schema that `report_handler.py` writes. They are not consumed by the suite
at runtime — they exist so a reviewer can see proof without needing AWS
credentials.

## Files

| File | What it proves | How produced |
|------|----------------|--------------|
| `sample_run_console.txt` | The full pipeline + verdict logic on **real** agents: per-category breakdown, the tool-guard categories (`excessive_agency`, `data_exfiltration`) holding at 0 on both agents, and the `FAILED`/exit-1 path firing. | Real, hardened factories (`tests/eval/factories.py`). |
| `sample_breach_console.txt` | The breach path + `RED TEAM FAILED` verdict, per-category ❌. | Deliberately-vulnerable agent (`_weakened_agent.py`). |
| `sample_breach_report.json` | The breach-transcript JSON schema `report_handler._save_breach_report` writes. | Same weakened run; **redacted** (see below). |

> **Why no all-green "RED TEAM PASSED" transcript?** An all-green live run is
> not reliably reproducible against these agents: the harness targets a
> prompt-only recreation (it omits the production Bedrock Guardrails layer that
> also screens I/O), and the LLM judge is stochastic, so borderline
> conversational scores (threshold 0.3) flip between runs. Hardening the
> factories' refusal prompts drove breaches down and the tool-guard categories
> to a consistent 0, but a guaranteed 0-across-all-categories run would be luck,
> not proof — which is exactly why the zero-tolerance gate is not wired into CI
> yet. The PASS *verdict logic* is instead proven deterministically by
> `tests/eval/red_team/test_verdict.py` (returns True + writes no JSON on a
> zero-breach report), and the report/JSON schema is exercised by the breach
> artifacts here.

## SDK / environment

Captured against:

- `strands-agents-evals` 1.0.0
- `strands-agents` 1.44.0
- `sqlglot` 30.12.0
- Model: `us.anthropic.claude-sonnet-4-5-20250929-v1:0` (Bedrock, `us-east-1`)

## How to reproduce

From the repo root, in the dedicated eval virtualenv (see
`tests/eval/requirements.txt`):

```bash
# Real-agent run (reduced case count keeps it cheap). An all-green PASS is not
# guaranteed (stochastic judge); this captures the real verdict + breakdown.
RED_TEAM_TARGET=real RED_TEAM_AUTO_CASES=1 python -m tests.eval.red_team.run_red_team

# Breach sample — deliberately-vulnerable agent (breach-evidence mode only)
RED_TEAM_TARGET=weakened RED_TEAM_AUTO_CASES=1 python -m tests.eval.red_team.run_red_team
```

`RED_TEAM_AUTO_CASES=1` runs 1 auto case per risk category (5 auto + 10 custom
= 15 attacks) instead of the default 4-per-category, to keep evidence runs
cheap. The full default suite uses `AUTO_CASES_PER_CATEGORY=4`.

The breach JSON the runner writes lands in `tests/eval/red_team/reports/`
(git-ignored except `.gitkeep`); the committed copy here is that artifact,
redacted.

## Redaction

The synthetic tool data is already fake (`000-00-NNNN` SSNs, `example.test`
addresses). In addition, `sample_breach_report.json` has every attacker/target
message replaced with a `[REDACTED_ATTACK_TECHNIQUE]` placeholder and the
judge's `reason` omitted. The goal is to prove the schema and the breach path —
not to publish a working jailbreak. Field names and structure are preserved
exactly as `report_handler.py` emits them (`case_name`, `risk_category`,
`strategy`, `severity`, `score`, `reason`, `conversation`).

The console noise (botocore credential lines, telemetry) is stripped from the
`*_console.txt` captures for readability.
