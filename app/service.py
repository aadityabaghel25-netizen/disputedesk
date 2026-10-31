"""
The "glue" layer: connects PayPal, the shop data, the AI team, the database and
the human approval step. The web routes in main.py stay thin and call these.
"""

import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from . import agents, core, evidence
from .config import Settings
from .db import DB, now_iso
from .demo import DemoPayPal
from .llm import FakeLLM, OpenAICompatibleLLM
from .paypal import PayPal, PayPalError

DISPUTE_EVENTS = ("CUSTOMER.DISPUTE.CREATED", "CUSTOMER.DISPUTE.UPDATED", "CUSTOMER.DISPUTE.RESOLVED")


class ActionError(Exception):
    pass


def _edited(text: str, analysis: dict) -> bool:
    """Browsers send textarea line breaks as CRLF, which is not a seller edit."""
    return text.replace("\r\n", "\n").strip() != analysis["reply_text"].strip()


class Service:
    def __init__(self, settings: Settings, db: DB | None = None, pp=None, llm=None, store=None):
        self.settings = settings
        self.db = db or DB(settings.database_path)
        real = PayPal(settings.paypal_client_id, settings.paypal_client_secret, settings.paypal_base_url)
        self.pp = pp or (real if real.configured else DemoPayPal())
        self.demo = getattr(self.pp, "is_demo", False)
        if llm is not None:
            self.llm = llm
        elif settings.llm_provider == "fake":
            self.llm = FakeLLM()
        else:
            self.llm = OpenAICompatibleLLM(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
        self.store = store or evidence.Store(settings.store_path)
        self.running: set[str] = set()
        self._lock = threading.Lock()
        self.last_watch: str | None = None
        self._ai_runs: dict[str, list[float]] = {}
        self._apply_rules(self.db.get_rules())

    # -- seller rules (edited in the UI, enforced by code) ----------------------

    RULE_LIMITS = {"settle_max_usd": (0, 1000), "max_offer_pct": (0, 100)}

    def _apply_rules(self, rules: dict) -> None:
        if "settle_max_usd" in rules:
            self.settings.settle_max_usd = float(rules["settle_max_usd"])
        if "max_offer_pct" in rules:
            self.settings.max_offer_pct = int(rules["max_offer_pct"])

    def update_rules(self, form: dict, actor: str) -> None:
        try:
            new = {"settle_max_usd": core.from_cents(core.to_cents(form.get("settle_max_usd", ""))),
                   "max_offer_pct": str(int(form.get("max_offer_pct", "")))}
        except ValueError:
            raise ActionError("Enter a dollar amount like 25 or 25.00, and a whole-number percentage.") from None
        for name, (lo, hi) in self.RULE_LIMITS.items():
            if not lo <= float(new[name]) <= hi:
                raise ActionError(f"{name} must be between {lo} and {hi}.")
        old = {"settle_max_usd": f"{self.settings.settle_max_usd:.2f}", "max_offer_pct": str(self.settings.max_offer_pct)}
        for name, value in new.items():
            self.db.set_rule(name, value)
        self._apply_rules(new)
        self.db.audit(actor, "rules_changed", None, {"old": old, "new": new})

    # -- deadline board: internal planning only. Nothing here can write to PayPal. ------

    HANDLERS = ("You", "Helper")
    BLOCK_MINUTES = 60

    def _readiness(self, d: dict) -> str:
        if core.seller_submissions(d):
            return "Filed with PayPal"
        if not self.db.latest_analysis(d["dispute_id"]):
            return "AI team hasn't run"
        return "Ready to approve" if core.allowed_actions(d) else "Case ready, waiting on PayPal"

    def board_data(self) -> dict:
        now = datetime.now(timezone.utc)
        blocks, stored, unplanned = [], self.db.get_blocks(), []
        for d in self.db.list_disputes():
            due = core.deadline_info(d, now)
            if core.dispute_outcome(d) or core.seller_submissions(d) or not due["due"]:
                continue
            cents, cur = core.dispute_amount(d)
            info = {"dispute_id": d["dispute_id"], "amount": f"{core.from_cents(cents)} {cur}", "due": due["due"],
                    "hours_left": due["hours_left"], "urgency": due["urgency"], "estimated": due["estimated"],
                    "readiness": self._readiness(d)}
            b = stored.get(d["dispute_id"])
            if b:
                blocks.append({**info, "handler": b["handler"], "start": b["start_at"], "end": b["end_at"],
                               "late": core.parse_time(b["end_at"]) > core.parse_time(due["due"])})
            else:
                unplanned.append(info)
        unplanned.sort(key=lambda u: u["due"])
        return {"handlers": list(self.HANDLERS), "blocks": blocks, "unplanned": unplanned, "now": now.isoformat()}

    def save_block(self, dispute_id: str, handler: str, start: str, end: str, actor: str) -> None:
        begin, finish = core.parse_time(start), core.parse_time(end)
        if not self.db.get_dispute(dispute_id):
            raise ActionError("Unknown dispute.")
        if handler not in self.HANDLERS:
            raise ActionError("Unknown handler.")
        if not begin or not finish or finish <= begin or finish - begin > timedelta(hours=8):
            raise ActionError("A work block must be between 1 minute and 8 hours.")
        self.db.set_block(dispute_id, handler, begin.isoformat(), finish.isoformat())
        self.db.audit(actor, "block_planned", dispute_id, {"handler": handler, "start": begin.isoformat(), "end": finish.isoformat()})

    def autoplan(self, actor: str, only: str | None = None) -> int:
        """Earliest deadline first: give every unplanned case time on the handler who is free soonest."""
        data = self.board_data()
        free = {h: datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1) for h in self.HANDLERS}
        for b in data["blocks"]:
            end = core.parse_time(b["end"])
            if end > free[b["handler"]]:
                free[b["handler"]] = end
        count = 0
        for u in data["unplanned"]:
            if only and u["dispute_id"] != only:
                continue
            handler = min(free, key=free.get)
            start, end = free[handler], free[handler] + timedelta(minutes=self.BLOCK_MINUTES)
            self.save_block(u["dispute_id"], handler, start.isoformat(), end.isoformat(), actor)
            free[handler] = end
            count += 1
        return count

    def _keep_block_before_deadline(self, dispute: dict) -> None:
        """If PayPal's deadline moved in front of the plan, pull the block forward and say so."""
        block = self.db.get_blocks().get(dispute["dispute_id"])
        due = core.parse_time(core.deadline_info(dispute)["due"])
        if not block or not due or core.seller_submissions(dispute) or core.parse_time(block["end_at"]) <= due:
            return
        length = core.parse_time(block["end_at"]) - core.parse_time(block["start_at"])
        start = max(due - length, datetime.now(timezone.utc))
        self.db.set_block(dispute["dispute_id"], block["handler"], start.isoformat(), (start + length).isoformat())
        self.db.audit("system", "block_moved", dispute["dispute_id"], {"reason": "PayPal deadline moved", "new_start": start.isoformat()})

    # -- dashboard numbers ----------------------------------------------------------

    def stats(self) -> dict:
        disputes = self.db.list_disputes()
        open_ = [d for d in disputes if d.get("status") != "RESOLVED"]
        at_stake = sum(core.dispute_amount(d)[0] for d in open_)
        analyses = self.db.all_analyses()
        decisions = [a["advice"]["decision"] for a in analyses]
        by_id = {d["dispute_id"]: d for d in disputes}
        ready = sum(1 for a in analyses if a["dispute_id"] in by_id and core.allowed_actions(by_id[a["dispute_id"]])
                    and not core.seller_submissions(by_id[a["dispute_id"]]) and not core.dispute_outcome(by_id[a["dispute_id"]]))
        outcomes = [core.dispute_outcome(d) for d in disputes]
        return {
            "open": len(open_), "at_stake": core.from_cents(at_stake),
            "analysed": len(analyses), "fight": decisions.count("fight"), "settle": decisions.count("settle"),
            "review": decisions.count("review"),
            "injections": sum(1 for a in analyses if any("instructions to an AI" in w for w in a["warnings"])),
            "struck": sum(len(a["claims_struck"]) for a in analyses),
            "risky_buyers": sum(1 for a in analyses if (a.get("buyer_risk") or {}).get("level") == "high"),
            "ready": ready, "filed": sum(1 for d in disputes if core.seller_submissions(d)),
            "won": outcomes.count("won"), "lost": outcomes.count("lost"),
        }

    # -- auto-watch: notice when PayPal changes a dispute, even without a webhook ----

    def watch_once(self) -> list[str]:
        """Re-read every open dispute. If PayPal now allows replies, log it and re-run
        the AI team so the advice matches what PayPal allows. Returns ids that opened up."""
        opened = []
        for old in self.db.list_disputes():
            if old.get("status") == "RESOLVED":
                continue
            try:
                new = self.pp.get_dispute(old["dispute_id"])
            except PayPalError:
                continue
            self.db.upsert_dispute(new)
            dispute_id = new["dispute_id"]
            before, after = core.allowed_actions(old), core.allowed_actions(new)
            changed = {k: [old.get(k), new.get(k)] for k in ("status", "dispute_life_cycle_stage")
                       if old.get(k) != new.get(k)}
            if changed:
                self.db.audit("paypal", "status_changed", dispute_id, changed)
            if after and not before:
                self.db.audit("paypal", "replies_opened", dispute_id, {"allowed": sorted(after)})
                opened.append(dispute_id)
                self.analyze_quietly(dispute_id)
                self.autoplan("system", only=dispute_id)   # PayPal opened replies, so make sure time is planned
            self._keep_block_before_deadline(new)
        self.last_watch = now_iso()
        return opened

    def seed_demo(self) -> None:
        """A first-time visitor in demo mode should see a working product, not an empty table."""
        if not self.demo or self.db.list_disputes():
            return
        self.sync()
        for d in self.db.list_disputes():
            self.analyze_quietly(d["dispute_id"])

    def start_watcher(self) -> None:
        minutes = self.settings.watch_minutes
        if minutes <= 0 or self.demo:
            return

        def loop():
            while True:
                try:
                    self.watch_once()
                except Exception as e:   # keep watching; log and carry on
                    self.db.audit("system", "watch_failed", None, {"error": str(e)[:300]})
                time.sleep(minutes * 60)

        threading.Thread(target=loop, daemon=True, name="paypal-watcher").start()

    # -- pulling disputes -------------------------------------------------------

    def sync(self) -> int:
        """Fetch all disputes from PayPal (fallback if a webhook was missed).
        PayPal's list can lag behind in the sandbox, so we also refresh every
        dispute we already know about."""
        ids = {s["dispute_id"] for s in self.pp.list_disputes()}
        ids |= {d["dispute_id"] for d in self.db.list_disputes()}
        for dispute_id in ids:
            try:
                self.db.upsert_dispute(self.pp.get_dispute(dispute_id))
            except PayPalError:
                pass
        self.db.audit("system", "sync", None, {"disputes": len(ids), "demo": self.demo})
        return len(ids)

    def load(self, dispute_id: str) -> dict:
        """Add one dispute by its id (e.g. PP-R-NKX-10195211)."""
        dispute = self.pp.get_dispute(dispute_id.strip())
        self.db.upsert_dispute(dispute)
        self.db.audit("seller", "load_dispute", dispute["dispute_id"], {"status": dispute.get("status")})
        return dispute

    # -- PREVENT: add tracking to PayPal as soon as a payment arrives -----------

    def prevent(self, capture: dict) -> str:
        """Most 'item not received' disputes are lost because PayPal never saw a
        tracking number. When a payment completes, attach the shop's tracking to it."""
        invoice = capture.get("invoice_id") or capture.get("custom_id")
        order_id = ((capture.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")
        shop = self.store.order(invoice)
        if not (shop and order_id and capture.get("id")):
            return "payment stored (no matching shop order)"
        ship = shop["shipment"]
        if not ship.get("tracking_number"):
            self.db.audit("system", "prevent_no_tracking", None, {"invoice": invoice})
            return "payment stored (shop has no tracking number yet)"
        if not hasattr(self.pp, "add_tracking"):
            return "payment stored (demo mode)"
        try:
            self.pp.add_tracking(order_id, capture["id"], ship["tracking_number"], ship["carrier"])
        except PayPalError as e:
            self.db.audit("system", "prevent_failed", None, {"invoice": invoice, "status": e.status})
            return f"tracking not added ({e.status})"
        self.db.audit("system", "prevent_tracking_added", None, {
            "invoice": invoice, "carrier": ship["carrier"], "tracking": ship["tracking_number"]})
        return "tracking added to PayPal"

    # -- evidence pack (always available, even if PayPal blocks replies) ---------

    def evidence_pack(self, dispute_id: str) -> dict | None:
        d = self.db.get_dispute(dispute_id)
        a = self.db.latest_analysis(dispute_id)
        if not d or not a:
            return None
        self.db.audit("seller", "download_pack", dispute_id, {"analysis_id": a["id"]})
        cents, cur = core.dispute_amount(d)
        return {"dispute": d, "analysis": a, "evidence": self.db.get_evidence(dispute_id),
                "amount": f"{core.from_cents(cents)} {cur}", "deadline": core.deadline_info(d),
                "audit_ok": self.db.verify_audit_chain()[0],
                "manifest": core.evidence_manifest(self.db.get_evidence(dispute_id)),
                "date_problems": core.impossible_dates(self.db.get_evidence(dispute_id))}

    # -- webhooks -----------------------------------------------------------------

    def handle_webhook(self, headers: dict, event: dict) -> tuple[int, str, str | None]:
        """Returns (http_status, message, dispute_id_to_analyze)."""
        verified = False
        if self.settings.paypal_webhook_id and not self.demo:
            try:
                verified = self.pp.verify_webhook_signature(headers, event, self.settings.paypal_webhook_id)
            except PayPalError:
                verified = False
        if not verified and not self.settings.allow_unverified_webhooks:
            self.db.audit("system", "webhook_rejected", None, {"event_id": event.get("id"),
                                                                "reason": "signature not verified"})
            return 400, "signature not verified", None

        is_new = self.db.save_event(event, verified)
        if not is_new:
            return 200, "duplicate ignored", None
        etype = event.get("event_type", "")
        if etype == "PAYMENT.CAPTURE.COMPLETED":
            return 200, self.prevent(event.get("resource") or {}), None
        if etype not in DISPUTE_EVENTS:
            return 200, f"stored {etype}", None
        dispute_id = (event.get("resource") or {}).get("dispute_id")
        if not dispute_id:
            return 200, "no dispute id", None
        dispute = self.pp.get_dispute(dispute_id)        # always re-read from PayPal (source of truth)
        self.db.upsert_dispute(dispute)
        self.db.audit("paypal", "webhook", dispute_id, {"event_type": etype, "verified": verified})
        analyze = etype != "CUSTOMER.DISPUTE.RESOLVED" and bool(core.allowed_actions(dispute))
        return 200, "ok", dispute_id if analyze else None

    # -- AI analysis ---------------------------------------------------------------

    def allow_ai_run(self, kind: str = "team") -> bool:
        limit = self.settings.ai_runs_per_hour if kind == "team" else self.settings.agent_turns_per_hour
        if limit <= 0 or isinstance(self.llm, FakeLLM):
            return True
        with self._lock:
            cutoff = time.time() - 3600
            runs = [t for t in self._ai_runs.get(kind, []) if t > cutoff]
            allowed = len(runs) < limit
            if allowed:
                runs.append(time.time())
            self._ai_runs[kind] = runs
            return allowed

    def analyze(self, dispute_id: str) -> dict | None:
        with self._lock:
            if dispute_id in self.running:
                return None
            self.running.add(dispute_id)
        try:
            dispute = self.db.get_dispute(dispute_id)
            if not dispute:
                dispute = self.pp.get_dispute(dispute_id)
                self.db.upsert_dispute(dispute)
            result, items = agents.analyze(dispute, self.pp, self.store, self.llm, self.settings)
            self.db.replace_evidence(dispute_id, items)
            aid = self.db.save_analysis(dispute_id, result)
            self.db.audit("ai", "analysis", dispute_id, {
                "analysis_id": aid, "model": result["model"], "decision": result["advice"]["decision"],
                "kept": len(result["claims_kept"]), "struck": len(result["claims_struck"])})
            return result
        except Exception as e:  # never crash the server because of one dispute
            self.db.audit("ai", "analysis_failed", dispute_id, {"error": str(e), "trace": traceback.format_exc()[-1500:]})
            raise
        finally:
            with self._lock:
                self.running.discard(dispute_id)

    def analyze_quietly(self, dispute_id: str) -> None:
        try:
            self.analyze(dispute_id)
        except Exception:
            pass   # already written to the audit log

    # -- human-approved actions -------------------------------------------------------

    def approve(self, dispute_id: str, action: str, form: dict, actor: str) -> dict:
        """The ONLY place money-related calls happen, and only after a human clicks."""
        dispute = self.pp.get_dispute(dispute_id)          # fresh state: what is allowed right now?
        allowed = core.allowed_actions(dispute)
        analysis = self.db.latest_analysis(dispute_id)
        if not analysis:
            raise ActionError("Run the analysis first.")

        if action == "submit_evidence":
            if "provide_evidence" not in allowed:
                raise ActionError("PayPal is not accepting evidence for this dispute right now. Try 'Send message'.")
            text = (form.get("reply_text") or analysis["reply_text"]).strip()
            notes = core.build_supporting_notes(text, self.db.get_evidence(dispute_id))
            options = core.evidence_payload_options(dispute, self.db.get_evidence(dispute_id), notes)
            for n, payload in enumerate(options, 1):
                try:
                    result = self.pp.provide_evidence(dispute_id, payload)
                    break
                except PayPalError as e:
                    if n == len(options) or not e.has_issue("EVIDENCE_TYPE_IS_NOT_ALLOWED"):
                        raise
                    self.db.audit(actor, "evidence_type_refused", dispute_id, {"evidence_type": payload[0]["evidence_type"]})
            details = {"evidence_types": [p["evidence_type"] for p in payload],
                       "edited_by_seller": _edited(text, analysis)}

        elif action == "submit_supporting_info":
            if "provide_supporting_info" not in allowed:
                raise ActionError("PayPal is not accepting supporting information for this dispute right now.")
            text = (form.get("reply_text") or analysis["reply_text"]).strip()
            notes = core.build_supporting_notes(text, self.db.get_evidence(dispute_id))
            result = self.pp.provide_supporting_info(dispute_id, notes)
            details = {"chars": len(notes), "edited_by_seller": _edited(text, analysis)}

        elif action == "send_message":
            if "send_message" not in allowed:
                raise ActionError("PayPal does not allow messages on this dispute right now.")
            text = (form.get("reply_text") or analysis["reply_text"]).strip()[:2000]
            result = self.pp.send_message(dispute_id, text)
            details = {"chars": len(text), "edited_by_seller": _edited(text, analysis)}

        elif action == "make_offer":
            if "make_offer" not in allowed:
                raise ActionError("PayPal does not allow offers on this dispute right now.")
            disputed, currency = core.dispute_amount(dispute)
            try:
                cents = core.to_cents(form.get("offer_amount") or analysis["advice"].get("offer_amount") or "")
            except ValueError as e:
                raise ActionError(str(e)) from None
            cap = disputed * self.settings.max_offer_pct // 100
            if not 0 < cents <= cap:
                raise ActionError(f"Offer must be between 0.01 and {core.from_cents(cap)} {currency} "
                                  f"(your limit is {self.settings.max_offer_pct}% of the disputed amount).")
            note = (form.get("note") or analysis.get("settle_note") or "We'd like to resolve this.")[:2000]
            result = self.pp.make_offer(dispute_id, core.from_cents(cents), currency, note)
            details = {"amount": core.from_cents(cents), "currency": currency}

        elif action == "accept_claim":
            if "accept_claim" not in allowed:
                raise ActionError("PayPal does not allow accepting this claim right now.")
            if (form.get("confirm") or "").strip().upper() != "ACCEPT":
                raise ActionError("Type ACCEPT to confirm. This refunds the buyer.")
            result = self.pp.accept_claim(dispute_id, "Seller accepted the claim.")
            details = {"amount": dispute.get("dispute_amount")}
        else:
            raise ActionError(f"Unknown action: {action}")

        self.db.set_analysis_state(analysis["id"], action)   # only reached after PayPal accepted the action
        self.db.audit(actor, action, dispute_id, {**details, "demo": self.demo})
        try:
            self.db.upsert_dispute(self.pp.get_dispute(dispute_id))
        except PayPalError:
            pass
        return result
