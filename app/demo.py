"""
Offline demo mode: a stand-in for PayPal that reads data/demo_disputes.json.

Same method names as app.paypal.PayPal, so the rest of the app can't tell the
difference. This lets you (and the judges) run DisputeDesk with zero keys,
and gives us a demo that can never fail on stage.
Write calls are recorded, NOT sent anywhere.
"""

import copy
import json
import os
from datetime import datetime, timedelta, timezone

from . import core
from .config import PROJECT_ROOT
from .paypal import PayPalError

DEMO_FILE = os.path.join(PROJECT_ROOT, "data", "demo_disputes.json")


class DemoPayPal:
    configured = True
    is_demo = True

    def __init__(self, path: str = DEMO_FILE):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self._disputes = {d["dispute_id"]: d for d in data["disputes"]}
        self._keep_dates_current()
        self._captures = data["captures"]
        self._orders = data["orders"]
        self.sent: list[dict] = []   # what WOULD have been sent to PayPal

    def _keep_dates_current(self) -> None:
        """Sample cases must not read as overdue when someone opens the demo weeks later."""
        newest = max(core.parse_time(d["create_time"]) for d in self._disputes.values())
        days = (datetime.now(timezone.utc) - newest).days - 2
        if days <= 0:
            return

        def moved(value: str) -> str:
            return (core.parse_time(value) + timedelta(days=days)).isoformat().replace("+00:00", "Z")

        for d in self._disputes.values():
            d["create_time"], d["seller_response_due_date"] = moved(d["create_time"]), moved(d["seller_response_due_date"])
            for t in d.get("disputed_transactions") or []:
                t["create_time"] = moved(t["create_time"])
            for m in d.get("messages") or []:
                m["time_posted"] = moved(m["time_posted"])

    def list_disputes(self) -> list[dict]:
        return [copy.deepcopy(d) for d in self._disputes.values()]

    def get_dispute(self, dispute_id: str) -> dict:
        if dispute_id not in self._disputes:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND"})
        return copy.deepcopy(self._disputes[dispute_id])

    def get_capture(self, capture_id: str) -> dict:
        if capture_id not in self._captures:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND"})
        return copy.deepcopy(self._captures[capture_id])

    def get_order(self, order_id: str) -> dict:
        if order_id not in self._orders:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND"})
        return copy.deepcopy(self._orders[order_id])

    def _record(self, action: str, dispute_id: str, payload) -> dict:
        self.sent.append({"action": action, "dispute_id": dispute_id, "payload": payload})
        d = self._disputes.get(dispute_id)
        if d is not None:
            d["status"] = "UNDER_REVIEW"
            d["links"] = [{"rel": "self"}, {"rel": "send_message"}]
            if action in ("provide_evidence", "provide_supporting_info"):   # PayPal keeps a record of what was filed
                notes = payload[0].get("notes") if action == "provide_evidence" else payload["notes"]
                d.setdefault("evidences", []).append({"evidence_type": "OTHER", "notes": notes, "source": "SUBMITTED_BY_SELLER",
                                                      "date": datetime.now(timezone.utc).isoformat()})
                d.pop("seller_response_due_date", None)
        return {"demo": True, "action": action}

    def send_message(self, dispute_id: str, message: str) -> dict:
        return self._record("send_message", dispute_id, message)

    def provide_evidence(self, dispute_id: str, evidences: list, files=None) -> dict:
        return self._record("provide_evidence", dispute_id, evidences)

    def provide_supporting_info(self, dispute_id: str, notes: str, files=None) -> dict:
        return self._record("provide_supporting_info", dispute_id, {"notes": notes})

    def make_offer(self, dispute_id: str, amount: str, currency: str, note: str) -> dict:
        return self._record("make_offer", dispute_id, {"amount": amount, "currency": currency, "note": note})

    def accept_claim(self, dispute_id: str, note: str) -> dict:
        return self._record("accept_claim", dispute_id, note)
