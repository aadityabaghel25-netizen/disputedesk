"""
Evidence gathering: plain code that collects every fact we can prove.

Each fact becomes an "evidence item" with a short id (E1, E2, ...). The AI may
only make claims that point to these ids. That's how we stop it inventing things.

trusted=False marks text written by the buyer: we show it to the AI as data,
never as instructions.
"""

import copy
import json
import re
from datetime import date, datetime, timedelta, timezone

from . import core
from .paypal import PayPalError


class Store:
    """The seller's own shop data (orders, shipping, chats). Mocked from data/store.json."""

    def __init__(self, path: str):
        with open(path, encoding="utf-8") as f:
            self.data = json.load(f)

    def order(self, invoice_id: str | None) -> dict | None:
        return self.data["orders"].get(invoice_id or "")

    def buyer_history(self, email: str) -> dict:
        return self.data.get("buyer_history", {}).get(email, {"previous_orders": 0, "previous_disputes": 0})


DATE_TOKEN = re.compile(r"\{paid\+(-?\d+)\}")


def resolve_dates(obj, paid: date, today: date | None = None):
    """Shop data stores dates as {paid+N}. Turn them into real dates relative to the
    PayPal payment date, never later than today (a parcel can't be delivered tomorrow)."""
    today = today or datetime.now(timezone.utc).date()

    def fix(text: str) -> str:
        return DATE_TOKEN.sub(lambda m: min(paid + timedelta(days=int(m.group(1))), today).isoformat(), text)

    if isinstance(obj, dict):
        return {k: resolve_dates(v, paid, today) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_dates(v, paid, today) for v in obj]
    return fix(obj) if isinstance(obj, str) else obj


def _norm_address(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def paypal_address_text(order: dict) -> str | None:
    try:
        a = order["purchase_units"][0]["shipping"]["address"]
    except (KeyError, IndexError, TypeError):
        return None
    parts = [a.get("address_line_1"), a.get("address_line_2"), a.get("admin_area_2"),
             a.get("admin_area_1"), a.get("postal_code"), a.get("country_code")]
    return ", ".join(p for p in parts if p)


def addresses_match(paypal_addr: str | None, delivered_to: str | None) -> bool:
    """Same street line and postal code -> match. Simple and explainable."""
    if not paypal_addr or not delivered_to:
        return False
    street = _norm_address(paypal_addr.split(",")[0])
    postal = re.findall(r"\b\d{5}\b", paypal_addr)
    d = _norm_address(delivered_to)
    return bool(street) and street in d and all(p in delivered_to for p in postal)


def link_to_order(pp, dispute: dict) -> tuple[str | None, dict | None, list[str], date | None]:
    """Find the shop invoice id, the PayPal order and the payment date behind a dispute."""
    notes = []
    tx = (dispute.get("disputed_transactions") or [{}])[0]
    invoice = tx.get("invoice_number") or tx.get("custom")
    order = None
    paid = core.parse_time(tx.get("create_time"))
    capture_id = tx.get("seller_transaction_id")
    if capture_id:
        try:
            cap = pp.get_capture(capture_id)
            invoice = invoice or cap.get("invoice_id") or cap.get("custom_id")
            paid = paid or core.parse_time(cap.get("create_time"))
            order_id = ((cap.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")
            if order_id:
                order = pp.get_order(order_id)
        except PayPalError as e:
            notes.append(f"Could not read PayPal capture/order ({e.status}).")
    if paid is None and order:
        paid = core.parse_time(order.get("create_time"))
    return invoice, order, notes, paid.date() if paid else None


INJECTION_WARNING = ("A buyer message contains text that looks like instructions to an AI. "
                     "It was treated as plain data and ignored as instructions.")


def gather(pp, store: Store, dispute: dict) -> tuple[list[dict], dict, list[str]]:
    """Returns (evidence_items, context, warnings)."""
    items: list[dict] = []
    warnings: list[str] = []

    def add(kind, source, trusted, summary, content):
        items.append({"id": f"E{len(items) + 1}", "kind": kind, "source": source,
                      "trusted": trusted, "summary": summary, "content": content})

    amount_cents, currency = core.dispute_amount(dispute)
    add("dispute", "PayPal dispute", True,
        f"Dispute {dispute['dispute_id']}: reason {dispute.get('reason')}, amount "
        f"{core.from_cents(amount_cents)} {currency}, status {dispute.get('status')}.",
        {"dispute_id": dispute["dispute_id"], "reason": dispute.get("reason"),
         "amount": core.from_cents(amount_cents), "currency": currency,
         "created": dispute.get("create_time"), "due": dispute.get("seller_response_due_date")})

    def flag(text: str) -> bool:
        flagged = bool(core.injection_flags(text))
        if flagged and INJECTION_WARNING not in warnings:
            warnings.append(INJECTION_WARNING)
        return flagged

    for m in core.dispute_statements(dispute):
        text = m.get("content", "")
        by = m.get("posted_by", "UNKNOWN")
        flags = flag(text) if by == "BUYER" else False
        add("dispute_message", f"PayPal dispute message ({by.lower()})", by != "BUYER",
            f"{by.title()} wrote in the dispute on {(m.get('time_posted') or '')[:10]}.",
            {"posted_by": by, "date": (m.get("time_posted") or "")[:10], "text": text,
             "injection_suspected": flags})

    invoice, order, notes, paid = link_to_order(pp, dispute)
    warnings += notes
    shop = store.order(invoice)
    if shop:
        if paid is None:
            paid = (core.parse_time(dispute.get("create_time")) or datetime.now(timezone.utc)).date()
            warnings.append("Payment date not found; shop dates are estimated from the dispute date.")
        shop = resolve_dates(copy.deepcopy(shop), paid)

    pp_addr = paypal_address_text(order) if order else None
    if order:
        pu = order["purchase_units"][0]
        trackers = ((pu.get("shipping") or {}).get("trackers") or [])
        add("paypal_order", "PayPal order", True,
            f"PayPal order {order.get('id')} for {pu.get('amount', {}).get('value')} "
            f"{pu.get('amount', {}).get('currency_code')}, shipping address on transaction: {pp_addr}.",
            {"order_id": order.get("id"), "invoice_id": pu.get("invoice_id"), "amount": pu.get("amount", {}).get("value"),
             "shipping_address": pp_addr, "paypal_trackers": [t.get("id") for t in trackers]})
    else:
        warnings.append("No PayPal order found for this dispute.")

    if not shop:
        warnings.append(f"No shop order found for invoice '{invoice}'. Evidence will be limited.")
        return items, {"invoice": invoice, "order": order, "shop": None}, warnings

    s = shop["shipment"]
    if s.get("tracking_number"):
        add("tracking", f"Carrier record ({s['carrier']}, simulated)", True,
            f"{s['carrier']} tracking {s['tracking_number']}: status {s['carrier_status']}, shipped "
            f"{s.get('shipped_on')}, delivered {s.get('delivered_on') or 'n/a'} to {s.get('delivered_to') or 'n/a'}"
            f"{', signed by ' + s['signature'] if s.get('signature') else ''}.",
            {k: s.get(k) for k in ("carrier", "tracking_number", "carrier_status", "shipped_on",
                                   "delivered_on", "delivered_to", "signature")})
        if s.get("carrier_status") == "DELIVERED":
            match = addresses_match(pp_addr, s.get("delivered_to"))
            add("address_check", "DisputeDesk check (code)", True,
                ("Delivery address matches" if match else "Delivery address does NOT match")
                + " the shipping address on the PayPal transaction.",
                {"paypal_address": pp_addr, "delivered_to": s.get("delivered_to"), "match": match})
    else:
        add("shipping", "Shop records", True,
            f"Shipped on {s.get('shipped_on')} via {s.get('carrier')} without tracking.",
            {"shipped_on": s.get("shipped_on"), "carrier": s.get("carrier"), "tracking_number": None})

    lst = shop.get("listing") or {}
    add("listing", "Shop listing", True, f"Listing '{lst.get('title')}': {lst.get('description')}",
        {"title": lst.get("title"), "description": lst.get("description")})

    for p in shop.get("packing_photos") or []:
        add("photo", "Shop packing photo", True, f"Packing photo: {p}", {"description": p})

    for c in shop.get("chat") or []:
        add("chat", f"Shop chat ({c['from']})", c["from"] != "buyer",
            f"{c['from'].title()} message on {c['at']}.",
            {"from": c["from"], "date": c["at"], "text": c["text"],
             "injection_suspected": c["from"] == "buyer" and flag(c["text"])})

    ctx = {"invoice": invoice, "order": order, "shop": shop, "buyer_email": shop["buyer"]["email"]}
    return items, ctx, warnings


CALL_TOPICS = {"receipt": "whether the order arrived", "delivery": "how the order was delivered",
               "item_condition": "the condition of the item", "purchase": "whether they made the purchase", "other": "the order"}


def add_call_finding(items: list[dict], call: dict, turn: dict, quote: str, topic: str, stance: str) -> str:
    """One quote from one recording becomes one evidence item. The summary is written by code, and no requirement
    below ever accepts this kind, so a call can never stand in for a carrier record."""
    evidence_id = f"E{len(items) + 1}"
    items.append({"id": evidence_id, "kind": core.CALL_KIND, "trusted": False,
                  "source": f"Call with the {call['party']} ({call['origin']})",
                  "summary": f"On a call with the {call['party']} on {call['call_date']}, the {turn['speaker']} spoke about "
                             f"{CALL_TOPICS[topic]}. Transcript reviewed by the seller, who confirmed everyone agreed to recording. "
                             "Spoken words, not carrier or PayPal records.",
                  "content": {"call_date": call["call_date"], "party": call["party"], "turns": [turn], "text": quote,
                              "topic": topic, "stance": stance, "injection_suspected": False,
                              "audio_sha256": call.get("audio_sha256")}})
    return evidence_id


# ---------------------------------------------------------------------------
# What PayPal expects for each reason, checked by code against our evidence
# ---------------------------------------------------------------------------

def _has(items, kind, pred=lambda c: True):
    return [i["id"] for i in items if i["kind"] == kind and pred(i["content"])]


REQUIREMENTS = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": [
        ("Tracking shows the item was delivered",
         lambda it: _has(it, "tracking", lambda c: c.get("carrier_status") == "DELIVERED")),
        ("Delivered to the address on the PayPal transaction",
         lambda it: _has(it, "address_check", lambda c: c.get("match"))),
    ],
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": [
        ("Original listing description", lambda it: _has(it, "listing")),
        ("Photo of the item before shipping", lambda it: _has(it, "photo")),
    ],
    "UNAUTHORISED": [
        ("Tracking shows the item was delivered",
         lambda it: _has(it, "tracking", lambda c: c.get("carrier_status") == "DELIVERED")),
        ("Delivered to the address on the PayPal transaction",
         lambda it: _has(it, "address_check", lambda c: c.get("match"))),
    ],
}


def check_requirements(reason: str, items: list[dict]) -> tuple[list[str], list[str]]:
    met, missing = [], []
    for label, fn in REQUIREMENTS.get(reason, []):
        ids = fn(items)
        (met if ids else missing).append(f"{label} ({', '.join(ids)})" if ids else label)
    return met, missing
