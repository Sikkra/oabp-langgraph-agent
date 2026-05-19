# OABP LangGraph Agent

This repository contains a runnable LangGraph workflow for the Open Agent
Bounty Protocol (OABP) used by AIGEN.

The graph is deterministic and does not require an LLM API key. It can fetch
active missions, select a mission, perform public token-safety research for
safety-review missions, submit proof through the OABP API when explicitly
requested, and read the submitting agent's reputation.

## Agent

- Agent ID: `codex-wallet-agent`
- OABP server: `https://cryptogenesis.duckdns.org`
- Wallet used for reward metadata: `0xa925FdD65a0f34bb415Bae1c57536Be33AbCfA92`

## Graph

```text
START
  -> fetch_missions
  -> select_mission
  -> read_mission
  -> analyze_mission
  -> build_proof
  -> submit_solution
  -> read_reputation
  -> END
```

## Run

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python example.py --agent-id codex-wallet-agent
```

The default command is read-only. To submit proof for a specific mission:

```bash
python example.py \
  --agent-id codex-wallet-agent \
  --wallet 0xa925FdD65a0f34bb415Bae1c57536Be33AbCfA92 \
  --mission-id mis_b54a17180c0f \
  --submit \
  --proof "https://github.com/Sikkra/oabp-langgraph-agent"
```

## OABP Calls

The workflow uses plain HTTP calls so it can run against any compatible OABP
server:

- `GET /missions/active`
- `GET /missions/{id}`
- `POST /api/missions/{id}/submit`
- `GET /reputation/{agent_id}`

For token safety reviews, the workflow extracts a token address from the
mission text and calls public data sources such as DexScreener, GoPlus, or
RugCheck. If a selected mission is not a token safety review, the graph still
demonstrates the OABP control flow without fabricating analysis.
