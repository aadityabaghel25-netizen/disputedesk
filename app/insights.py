"""
Data for the AG Studio insights page. Numbers only: buyer text never enters these rows, so a
message written to manipulate an AI can't reach a dashboard or a dashboard agent.
"""

import random
from datetime import datetime, timedelta, timezone

from . import core
from .llm import FakeLLM

REASONS = {core.NOT_RECEIVED: "Item not received", core.NOT_AS_DESCRIBED: "Not as described", core.UNAUTHORISED: "Unauthorised"}
HIGH_VOLUME_RATIO_PCT = 1.5   # PayPal's published High Volume threshold, with more than 100 sales in 3 months
VERDICTS = {"may_qualify": "May qualify", "excluded": "Likely excluded", "missing_proof": "Proof missing", "check_terms": "Unclear"}


def _case_row(d: dict, a: dict | None) -> dict:
    cents, _ = core.dispute_amount(d)
    created = core.parse_time(d.get("create_time")) or datetime.now(timezone.utc)
    return {"id": d["dispute_id"], "month": created.strftime("%Y-%m"), "reason": REASONS.get(d.get("reason"), "Other"),
            "amount": cents / 100, "decision": (a["advice"]["decision"].capitalize() if a else "Not analysed"),
            "protection": VERDICTS.get((a.get("protection") or {}).get("verdict"), "Unclear") if a else "Unclear",
            "serial_buyer": 1 if a and (a.get("buyer_risk") or {}).get("level") == "high" else 0,
            "flagged": 1 if a and any("instructions to an AI" in w for w in a["warnings"]) else 0,
            "kept": len(a["claims_kept"]) if a else 0, "struck": len(a["claims_struck"]) if a else 0,
            "filed": 1 if core.seller_submissions(d) else 0, "source": "Live case"}


def _sample_history(seed: int = 7) -> list[dict]:
    """Made-up past cases so the page isn't empty in demo mode. Clearly labelled. No outcomes, so it can't imply a win rate."""
    rng, rows, now = random.Random(seed), [], datetime.now(timezone.utc)
    for n in range(60):
        reason = rng.choices(list(REASONS.values()), weights=[6, 3, 1])[0]
        decision = rng.choices(["Fight", "Settle", "Review"], weights=[6, 3, 1])[0]
        drafted = rng.randint(2, 6)
        struck = rng.choices([0, 1, 2], weights=[7, 2, 1])[0]
        rows.append({"id": f"SAMPLE-{n + 1:03d}", "month": (now - timedelta(days=rng.randint(0, 170))).strftime("%Y-%m"),
                     "reason": reason, "amount": round(rng.uniform(9, 240), 2), "decision": decision,
                     "protection": "Likely excluded" if reason == "Not as described" else rng.choice(["May qualify", "May qualify", "Proof missing"]),
                     "serial_buyer": int(rng.random() < 0.08), "flagged": int(rng.random() < 0.05),
                     "kept": drafted - min(struck, drafted), "struck": min(struck, drafted), "filed": int(rng.random() < 0.7), "source": "Sample history"})
    return rows


def build(service) -> dict:
    cases = [_case_row(d, service.db.latest_analysis(d["dispute_id"])) for d in service.db.list_disputes()]
    if service.demo:
        cases += _sample_history()
    # Dispute ratio needs total sales, which only the shop knows. Sample figures in demo mode, otherwise not shown.
    ratio = {"disputes": sum(1 for c in cases if c["source"] == "Sample history"), "sales": 2400, "limit_pct": HIGH_VOLUME_RATIO_PCT} if service.demo else None
    cases.sort(key=lambda c: c["month"])
    has_ai = bool(service.settings.agent_model) and not isinstance(service.llm, FakeLLM)   # off unless a model is chosen
    return {"cases": cases, "ratio": ratio, "sample": service.demo, "ai": has_ai}
