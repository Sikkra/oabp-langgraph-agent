#!/usr/bin/env python3
"""LangGraph OABP workflow example.

The graph is intentionally deterministic and does not require an LLM API key.
It demonstrates an agent workflow that can:

1. fetch active OABP missions,
2. select a mission it can handle,
3. perform token safety research for safety-review missions,
4. submit proof through the OABP submit endpoint when --submit is passed,
5. read the agent's OABP reputation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from typing import Any, NotRequired, TypedDict

import requests
from langgraph.graph import END, START, StateGraph


DEFAULT_BASE_URL = "https://cryptogenesis.duckdns.org"
DEFAULT_TIMEOUT = 25
USER_AGENT = "oabp-langgraph-agent/1.0"


class OABPError(RuntimeError):
    """Raised when no compatible OABP endpoint returns usable JSON."""


class WorkflowState(TypedDict):
    base_url: str
    agent_id: str
    wallet: NotRequired[str | None]
    requested_mission_id: NotRequired[str | None]
    submit: bool
    proof_override: NotRequired[str | None]
    missions: NotRequired[list[dict[str, Any]]]
    selected_mission: NotRequired[dict[str, Any] | None]
    mission_detail: NotRequired[dict[str, Any] | None]
    analysis: NotRequired[dict[str, Any] | None]
    proof: NotRequired[str | None]
    submission: NotRequired[dict[str, Any] | None]
    reputation: NotRequired[dict[str, Any] | None]
    errors: NotRequired[list[str]]


@dataclass(frozen=True)
class OABPClient:
    base_url: str
    timeout: int = DEFAULT_TIMEOUT

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"

    def request_json(
        self,
        method: str,
        candidate_paths: list[str],
        payload: dict[str, Any] | None = None,
    ) -> tuple[str, Any]:
        errors: list[str] = []
        for path in candidate_paths:
            try:
                response = requests.request(
                    method,
                    self._url(path),
                    json=payload,
                    timeout=self.timeout,
                    headers={"User-Agent": USER_AGENT},
                )
                if response.status_code >= 400:
                    errors.append(f"{method} {path}: HTTP {response.status_code}")
                    continue
                return path, response.json()
            except requests.RequestException as exc:
                errors.append(f"{method} {path}: {exc}")
            except ValueError as exc:
                errors.append(f"{method} {path}: invalid JSON ({exc})")
        raise OABPError("; ".join(errors))


def normalize_missions(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("missions", "data", "items"):
            items = data.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    return []


def mission_reward(mission: dict[str, Any]) -> int:
    reward = mission.get("reward") if isinstance(mission.get("reward"), dict) else {}
    amount = mission.get("reward_aigen") or mission.get("reward_amount") or reward.get("amount") or 0
    try:
        return int(amount)
    except (TypeError, ValueError):
        return 0


def submission_count(mission: dict[str, Any]) -> int:
    submissions = mission.get("submissions")
    if isinstance(submissions, list):
        return len(submissions)
    try:
        return int(mission.get("submission_count") or 0)
    except (TypeError, ValueError):
        return 0


def mission_text(mission: dict[str, Any]) -> str:
    return " ".join(
        str(mission.get(key) or "")
        for key in ("title", "description", "category", "verification_type")
    ).lower()


def is_safety_review(mission: dict[str, Any]) -> bool:
    category = str(mission.get("category") or "").lower()
    title = str(mission.get("title") or "").lower()
    if category in {"code", "translation"}:
        return False
    if any(marker in title for marker in ("workflow", "client", "agent", "translate", "integration example")):
        return False
    text = mission_text(mission)
    return ("safety" in text or "rug" in text or "token review" in text) and "token" in text


def extract_token_address(text: str) -> str | None:
    evm_match = re.search(r"\b0x[a-fA-F0-9]{40}\b", text)
    if evm_match:
        return evm_match.group(0)
    sol_match = re.search(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b", text)
    if sol_match:
        return sol_match.group(0)
    return None


def infer_chain(text: str, address: str | None) -> str:
    lowered = text.lower()
    if "base" in lowered:
        return "base"
    if "solana" in lowered or (address and not address.startswith("0x")):
        return "solana"
    if address and address.startswith("0x"):
        return "ethereum"
    return "unknown"


def fetch_json_url(url: str) -> dict[str, Any]:
    try:
        response = requests.get(url, timeout=DEFAULT_TIMEOUT, headers={"User-Agent": USER_AGENT})
        if response.status_code >= 400:
            return {"ok": False, "status": response.status_code, "url": url}
        return {"ok": True, "status": response.status_code, "url": url, "data": response.json()}
    except (requests.RequestException, ValueError) as exc:
        return {"ok": False, "url": url, "error": str(exc)}


def summarize_api_result(name: str, result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"{name}: unavailable ({result.get('status') or result.get('error')})"
    data = result.get("data")
    if name == "DexScreener" and isinstance(data, dict):
        pairs = data.get("pairs") or []
        return f"DexScreener: {len(pairs)} pair(s) found"
    if name == "GoPlus" and isinstance(data, dict):
        result_data = data.get("result") or {}
        token_count = len(result_data) if isinstance(result_data, dict) else 0
        return f"GoPlus: {token_count} token security record(s) returned"
    if name == "RugCheck" and isinstance(data, dict):
        risks = data.get("risks") or []
        return f"RugCheck: {len(risks)} risk item(s) returned"
    return f"{name}: data returned"


def fetch_missions(state: WorkflowState) -> dict[str, Any]:
    client = OABPClient(state["base_url"])
    endpoint, data = client.request_json("GET", ["/missions/active", "/api/missions", "/missions"])
    return {"missions": normalize_missions(data), "mission_list_endpoint": endpoint}


def select_mission(state: WorkflowState) -> dict[str, Any]:
    requested = state.get("requested_mission_id")
    missions = state.get("missions", [])
    if requested:
        selected = next((mission for mission in missions if mission.get("id") == requested), None)
        return {"selected_mission": selected or {"id": requested}}

    candidates = [mission for mission in missions if is_safety_review(mission)]
    if not candidates:
        candidates = missions
    selected = sorted(candidates, key=lambda item: (submission_count(item), -mission_reward(item)))[0] if candidates else None
    return {"selected_mission": selected}


def read_mission(state: WorkflowState) -> dict[str, Any]:
    selected = state.get("selected_mission")
    if not selected or not selected.get("id"):
        return {"mission_detail": None, "errors": state.get("errors", []) + ["No mission selected"]}
    client = OABPClient(state["base_url"])
    endpoint, data = client.request_json(
        "GET",
        [f"/missions/{selected['id']}", f"/api/missions/{selected['id']}"],
    )
    if isinstance(data, dict):
        data = {**data, "_endpoint": endpoint}
    return {"mission_detail": data}


def analyze_mission(state: WorkflowState) -> dict[str, Any]:
    detail = state.get("mission_detail") or state.get("selected_mission") or {}
    text = " ".join(str(detail.get(key) or "") for key in ("title", "description"))
    address = extract_token_address(text)
    chain = infer_chain(text, address)

    if not is_safety_review(detail):
        return {
            "analysis": {
                "type": "non_safety_mission",
                "summary": "Selected mission is not a token safety review; no external token checks required.",
            }
        }

    checks: dict[str, Any] = {}
    if address:
        checks["dexscreener"] = fetch_json_url(f"https://api.dexscreener.com/latest/dex/search/?q={address}")
        if address.startswith("0x"):
            chain_id = "8453" if chain == "base" else "1"
            checks["goplus"] = fetch_json_url(
                f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={address}"
            )
        else:
            checks["rugcheck"] = fetch_json_url(f"https://api.rugcheck.xyz/v1/tokens/{address}/report/summary")

    summaries = [
        summarize_api_result("DexScreener", checks["dexscreener"])
        for key in ("dexscreener",)
        if key in checks
    ]
    if "goplus" in checks:
        summaries.append(summarize_api_result("GoPlus", checks["goplus"]))
    if "rugcheck" in checks:
        summaries.append(summarize_api_result("RugCheck", checks["rugcheck"]))

    if not summaries:
        summaries.append("No token address was found in the mission text.")

    return {
        "analysis": {
            "type": "token_safety_review",
            "chain": chain,
            "address": address,
            "checks": checks,
            "summary": "; ".join(summaries),
        }
    }


def build_proof(state: WorkflowState) -> dict[str, Any]:
    override = state.get("proof_override")
    if override:
        return {"proof": override}

    selected = state.get("selected_mission") or {}
    analysis = state.get("analysis") or {}
    proof = (
        f"LangGraph OABP workflow completed by {state['agent_id']} for "
        f"{selected.get('id', 'unknown mission')}. Analysis: {analysis.get('summary', 'completed')}."
    )
    return {"proof": proof}


def submit_solution(state: WorkflowState) -> dict[str, Any]:
    if not state["submit"]:
        return {"submission": None}
    selected = state.get("selected_mission")
    if not selected or not selected.get("id"):
        return {"submission": None, "errors": state.get("errors", []) + ["Cannot submit without a mission id"]}

    payload: dict[str, Any] = {
        "submitter_agent_id": state["agent_id"],
        "proof": state.get("proof"),
        "metadata": {
            "client": "oabp-langgraph-agent",
            "framework": "langgraph",
            "graph": ["fetch_missions", "select_mission", "read_mission", "analyze_mission", "submit_solution"],
        },
    }
    if state.get("wallet"):
        payload["submitter_wallet"] = state["wallet"]

    client = OABPClient(state["base_url"])
    endpoint, data = client.request_json(
        "POST",
        [f"/api/missions/{selected['id']}/submit", f"/missions/{selected['id']}/submit"],
        payload=payload,
    )
    return {"submission": {"endpoint": endpoint, "response": data}}


def read_reputation(state: WorkflowState) -> dict[str, Any]:
    client = OABPClient(state["base_url"])
    endpoint, data = client.request_json(
        "GET",
        [f"/reputation/{state['agent_id']}", f"/api/agents/{state['agent_id']}"],
    )
    return {"reputation": {"endpoint": endpoint, "response": data}}


def build_graph():
    graph = StateGraph(WorkflowState)
    graph.add_node("fetch_missions", fetch_missions)
    graph.add_node("select_mission", select_mission)
    graph.add_node("read_mission", read_mission)
    graph.add_node("analyze_mission", analyze_mission)
    graph.add_node("build_proof", build_proof)
    graph.add_node("submit_solution", submit_solution)
    graph.add_node("read_reputation", read_reputation)
    graph.add_edge(START, "fetch_missions")
    graph.add_edge("fetch_missions", "select_mission")
    graph.add_edge("select_mission", "read_mission")
    graph.add_edge("read_mission", "analyze_mission")
    graph.add_edge("analyze_mission", "build_proof")
    graph.add_edge("build_proof", "submit_solution")
    graph.add_edge("submit_solution", "read_reputation")
    graph.add_edge("read_reputation", END)
    return graph.compile()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a LangGraph OABP workflow")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--agent-id", default="codex-wallet-agent")
    parser.add_argument("--wallet", default=None)
    parser.add_argument("--mission-id", default=None)
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--proof", default=None)
    return parser.parse_args(argv)


def run_workflow(args: argparse.Namespace) -> dict[str, Any]:
    app = build_graph()
    initial_state: WorkflowState = {
        "base_url": args.base_url,
        "agent_id": args.agent_id,
        "wallet": args.wallet,
        "requested_mission_id": args.mission_id,
        "submit": args.submit,
        "proof_override": args.proof,
        "errors": [],
    }
    final_state = app.invoke(initial_state)
    return {
        "graph_nodes": [
            "fetch_missions",
            "select_mission",
            "read_mission",
            "analyze_mission",
            "build_proof",
            "submit_solution",
            "read_reputation",
        ],
        "submitted": final_state.get("submission") is not None,
        "state": final_state,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        result = run_workflow(args)
    except OABPError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1
    print(json.dumps({"ok": True, "result": result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
