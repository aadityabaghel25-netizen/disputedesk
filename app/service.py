"""
The "glue" layer: connects PayPal, the shop data, the AI team, the database and
the human approval step. The web routes in main.py stay thin and call these.
"""

import hashlib
import json
import queue
import re
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from . import agents, core, evidence
from .config import Settings
from .db import DB, now_iso
from .demo import DemoPayPal
from .llm import FakeLLM, LLMError, OpenAICompatibleLLM, gnani_transcribe
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
        self.live: dict[str, dict] = {}   # case id -> the agent working on it right now
        self._call_queue: queue.Queue = queue.Queue()
        self._select_lock = threading.Lock()   # web requests and the call worker both select recordings
        self._call_worker: threading.Thread | None = None
        self.db.fail_interrupted_calls()
        self._lock = threading.Lock()
        self.last_watch: str | None = None
        self._ai_runs: dict[str, list[float]] = {}
        self._rule_defaults = (settings.settle_max_usd, settings.max_offer_pct)
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
        if not self.demo:
            return
        first_visit = not self.db.list_disputes()
        if first_visit:
            self.sync()
        fresh = self.add_sample_recordings() if not self.db.all_calls() else []
        for dispute_id in ([d["dispute_id"] for d in self.db.list_disputes()] if first_visit else fresh):
            self.analyze_quietly(dispute_id)
        if first_visit:
            for u in self.board_data()["unplanned"][:-1]:
                self.autoplan("system", only=u["dispute_id"])

    DEMO_DEADLINE_HOURS = 12   # refresh the sample cases before a deadline gets this close

    def demo_reset_due(self, now: datetime | None = None) -> bool:
        """Did a visitor leave changes behind, or is a sample deadline close? Never interrupts someone still working."""
        minutes = self.settings.demo_reset_minutes
        disputes = self.db.list_disputes()
        if not self.demo or minutes <= 0 or not disputes or self.running or self.live_state()["calls"]:
            return False
        now = now or datetime.now(timezone.utc)
        changed = self.db.last_visitor_change()
        if changed:
            return now - core.parse_time(changed) >= timedelta(minutes=minutes)
        return any(core.parse_time(d["seller_response_due_date"]) - now < timedelta(hours=self.DEMO_DEADLINE_HOURS)
                   for d in disputes if d.get("seller_response_due_date"))

    def reset_demo(self) -> None:
        """Back to what a first-time visitor should see, so one visitor's clicks don't change the next visitor's demo."""
        if not self.demo:
            return
        self.db.clear_case_data()
        self.pp.reset()
        self.settings.settle_max_usd, self.settings.max_offer_pct = self._rule_defaults
        self.db.audit("system", "demo_reset", None, {})
        self.seed_demo()

    def start_demo_keeper(self) -> None:
        if not self.demo or self.settings.demo_reset_minutes <= 0:
            return

        def loop():
            while True:
                time.sleep(300)
                try:
                    if self.demo_reset_due():
                        self.reset_demo()
                except Exception as e:   # the demo must keep serving; log and carry on
                    self.db.audit("system", "demo_reset_failed", None, {"error": str(e)[:300]})

        threading.Thread(target=loop, daemon=True, name="demo-keeper").start()

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

    def analyze(self, dispute_id: str, pause: float = 0) -> dict | None:
        with self._lock:
            if dispute_id in self.running:
                return None
            self.running.add(dispute_id)
        self.live[dispute_id] = {"agent": "Collector", "what": "Gathering the evidence on this case."}

        def on_step(agent, what):
            self.live[dispute_id] = {"agent": agent, "what": what}
            time.sleep(pause)

        try:
            dispute = self.db.get_dispute(dispute_id)
            if not dispute:
                dispute = self.pp.get_dispute(dispute_id)
                self.db.upsert_dispute(dispute)
            self.select_recordings()
            before = self.db.latest_analysis(dispute_id)
            result, items = agents.analyze(dispute, self.pp, self.store, self.llm, self.settings, calls=self.db.list_calls(dispute_id),
                                           on_step=on_step, documents=self.db.list_documents(dispute_id))
            if before and before["advice"]["decision"] != result["advice"]["decision"]:
                result["changed_from"] = before["advice"]["decision"]
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
            self.live.pop(dispute_id, None)
            with self._lock:
                self.running.discard(dispute_id)

    def analyze_quietly(self, dispute_id: str, pause: float = 0) -> None:
        try:
            self.analyze(dispute_id, pause)
        except Exception:
            pass   # already written to the audit log

    # -- proof the seller adds: closes the gap the pre-flight finds. Nothing here can write to PayPal. ----

    CARRIERS = ("UPS", "USPS", "FEDEX", "DHL", "OTHER")
    MAX_DOCUMENT = 10_000_000
    MAX_DOCS_PER_CASE = 5
    MAX_DEMO_RECORDS = 60    # the public demo has no login, so it can't grow without limit

    def _carrier_fields(self, form: dict) -> dict:
        carrier, tracking = form.get("carrier"), (form.get("tracking_number") or "").strip().upper()
        if carrier not in self.CARRIERS:
            raise ActionError("Choose the carrier.")
        if not re.fullmatch(r"[A-Z0-9-]{6,40}", tracking):
            raise ActionError("Enter the tracking number exactly as the carrier shows it.")
        shipped, delivered = core.parse_time(form.get("shipped_on") or ""), core.parse_time(form.get("delivered_on") or "")
        today = datetime.now(timezone.utc).date()
        if not shipped or not delivered:
            raise ActionError("Enter the shipped and delivered dates from the record.")
        if delivered.date() < shipped.date() or delivered.date() > today:
            raise ActionError("Check the dates. Delivery can't be before shipping or in the future.")
        address = " ".join((form.get("delivered_to") or "").split())
        if not 8 <= len(address) <= 200:
            raise ActionError("Enter the delivery address shown on the record.")
        return {"carrier": carrier, "tracking_number": tracking, "shipped_on": shipped.date().isoformat(),
                "delivered_on": delivered.date().isoformat(), "delivered_to": address}

    def add_document(self, dispute_id: str, form: dict, content: bytes, filename: str, actor: str) -> None:
        if not self.db.get_dispute(dispute_id):
            raise ActionError("Unknown dispute.")
        if form.get("genuine") != "yes":
            raise ActionError("Confirm that this is a genuine record. Your reply relies on it.")
        doc_type = form.get("doc_type")
        if doc_type not in ("carrier_record", "photo", "other"):
            raise ActionError("Choose what kind of proof this is.")
        if not content or len(content) > self.MAX_DOCUMENT:
            raise ActionError("Choose a file under 10 MB.")
        mime = core.sniff_file_type(content)
        if not mime or (doc_type == "photo" and not mime.startswith("image/")):
            raise ActionError("Use a PDF, JPG, PNG or GIF file." if doc_type != "photo" else "Use a JPG, PNG or GIF photo.")
        fields = self._carrier_fields(form) if doc_type == "carrier_record" else {}
        if len(self.db.list_documents(dispute_id)) >= self.MAX_DOCS_PER_CASE:
            raise ActionError(f"A case can hold {self.MAX_DOCS_PER_CASE} added documents. Remove one first.")
        if self.demo and self.db.count_documents() >= self.MAX_DEMO_RECORDS:
            raise ActionError("The demo is full for now.")
        name = re.sub(r"[^\w.-]+", "_", filename or "document")[-80:] or "document"
        fingerprint = hashlib.sha256(content).hexdigest()
        # The public demo keeps only the fingerprint, so it can't be used to host files
        doc_id = self.db.add_document(dispute_id, actor, doc_type, name, mime, fingerprint, len(content), fields, None if self.demo else content)
        self.db.audit(actor, "document_added", dispute_id, {"document_id": doc_id, "type": doc_type, "sha256": fingerprint})

    def add_sample_document(self, dispute_id: str, actor: str) -> None:
        """Lets anyone see the gap close with no file to hand. Made up, labelled, and demo only."""
        dispute = self.db.get_dispute(dispute_id)
        if not self.demo or not dispute:
            raise ActionError("Sample proof is only for the demo.")
        if self.db.list_documents(dispute_id):
            raise ActionError("This case already has added proof.")
        _, order, _, paid = evidence.link_to_order(self.pp, dispute)
        paid = paid or datetime.now(timezone.utc).date() - timedelta(days=7)
        today = datetime.now(timezone.utc).date()
        fields = {"carrier": "UPS", "tracking_number": "1Z999AA10123450001", "shipped_on": min(paid + timedelta(days=1), today).isoformat(),
                  "delivered_on": min(paid + timedelta(days=5), today).isoformat(), "delivered_to": evidence.paypal_address_text(order) if order else "2211 N First St, San Jose, CA 95131, US"}
        doc_id = self.db.add_document(dispute_id, actor, "carrier_record", "made-up-sample-record.pdf", "application/pdf",
                                      hashlib.sha256(json.dumps(fields).encode()).hexdigest(), 0, fields, None)
        self.db.audit(actor, "document_added", dispute_id, {"document_id": doc_id, "type": "carrier_record", "sample": True})

    def remove_document(self, dispute_id: str, doc_id: int, actor: str) -> None:
        if not self.db.delete_document(dispute_id, doc_id):
            raise ActionError("That document is already gone.")
        self.db.audit(actor, "document_removed", dispute_id, {"document_id": doc_id})

    # -- call recordings: the library the onboarding agent selects from. Nothing here can write to PayPal. ----

    CALL_PARTIES = ("buyer", "carrier")
    MAX_TRANSCRIPT = 8000
    MAX_AUDIO = 10_000_000   # Gnani's limit for a file sent directly
    SAMPLE_RECORDINGS = (
        ("DD-1003-buyer.mp3", "buyer", "Agent: Thanks for calling about order DD-1003. This call is recorded, is that okay?\nCustomer: Yes, that's fine.\n"
                                       "Agent: Did the keychain reach you?\nCustomer: A parcel did come last week but I haven't opened it yet."),
        ("DD-1003-driver.mp3", "carrier", "Seller: Hi, this call is recorded, is that okay?\nDriver: Sure, no problem.\n"
                                          "Seller: Do you remember this parcel?\nDriver: I think I left it at the front door."),
        ("DD-1001-buyer.mp3", "buyer", "Agent: This call is recorded, is that okay?\nBuyer: Yes, go ahead.\n"
                                       "Agent: Have you found the tote?\nBuyer: It never came and my neighbour has not seen it."),
    )

    def _case_references(self, dispute: dict) -> list[tuple[str, str]]:
        tx = (dispute.get("disputed_transactions") or [{}])[0]
        invoice = tx.get("invoice_number") or tx.get("custom")
        refs = [(f"case {dispute['dispute_id']}", dispute["dispute_id"])]
        shop = self.store.order(invoice)
        if invoice:
            refs.append((f"order {invoice}", invoice))
        if shop:
            refs.append(("the tracking number", shop["shipment"].get("tracking_number")))
            refs.append(("the buyer's email", shop["buyer"].get("email")))
        return refs

    def select_recordings(self) -> list[str]:
        """Attach each unmatched recording to the one open case it mentions. Plain code, not AI: a wrong guess would
        put a stranger's call in a case. No reference, or more than one case, stays for the seller. Returns case ids."""
        cases = [d for d in self.db.list_disputes() if not core.dispute_outcome(d)]
        selected = []
        with self._select_lock:
            for call in self.db.list_calls(""):
                if call["status"] != "ready":
                    continue
                text = f"{call['filename']}\n{call['transcript']}"
                hits = {d["dispute_id"]: label for d in cases if (label := core.find_reference(text, self._case_references(d)))}
                if len(hits) == 1:
                    (dispute_id, label), = hits.items()
                    self.db.assign_call(call["id"], dispute_id, label)
                    self.db.audit("system", "call_selected", dispute_id, {"call_id": call["id"], "matched_on": label})
                    selected.append(dispute_id)
        return list(dict.fromkeys(selected))

    def _listen_for(self) -> list[str]:
        """Order numbers, tracking numbers, items and carriers on open cases, so speech-to-text hears them right.
        These also let the recording find its case. No buyer names or emails are sent."""
        terms = []
        for d in self.db.list_disputes():
            if core.dispute_outcome(d):
                continue
            tx = (d.get("disputed_transactions") or [{}])[0]
            invoice = tx.get("invoice_number") or tx.get("custom")
            shop = self.store.order(invoice) or {}
            ship = shop.get("shipment") or {}
            terms += [invoice, ship.get("tracking_number"), shop.get("item"), ship.get("carrier")]
        return list(dict.fromkeys(t for t in terms if t and t != "OTHER"))

    # The background pipeline: a queue and one worker, like a small job runner. Gnani handles one request at a time per
    # account, so one worker also keeps us inside its limit. Audio stays in memory only until Gnani has it.

    MAX_QUEUED = 5
    SAMPLE_PAUSE = 1.2      # the demo's sample call would finish instantly; slow it down so people can watch it
    SAMPLE_LIMIT = 12
    LIVE_SAMPLE = ("Speaker 1: Thanks for calling about order DD-1002, the leather wallet. This call is recorded, is that okay?\n"
                   "Speaker 2: Yes, that is fine.\nSpeaker 1: Is the color different from the photos?\n"
                   "Speaker 2: Honestly, the color looks the same as the photos, I just changed my mind.")
    CALL_LABELS = {"queued": "Waiting its turn", "transcribing": "Transcribing the audio", "labelling": "Working out who said what",
                   "selecting": "Finding the matching case", "reading": "AI team is reading it", "failed": "Couldn't read this call"}

    def consent_on_file(self) -> str | None:
        return self.db.get_rules().get("calls_consent")

    def set_consent(self, given: bool, actor: str) -> None:
        self.db.set_rule("calls_consent", now_iso() if given else "")
        self.db.audit(actor, "call_consent_confirmed" if given else "call_consent_withdrawn", None, {})

    def _start_call_worker(self) -> None:
        if self._call_worker and self._call_worker.is_alive():
            return

        def loop():
            while True:
                call_id, audio, sample = self._call_queue.get()
                try:
                    self._process_call(call_id, audio, sample)
                except Exception as e:   # one bad recording must not stop the worker
                    self.db.update_call(call_id, status="failed", error=str(e)[:200])
                    self.db.audit("system", "call_failed", None, {"call_id": call_id, "error": str(e)[:200]})
                finally:
                    self._call_queue.task_done()

        self._call_worker = threading.Thread(target=loop, daemon=True, name="call-onboarding")
        self._call_worker.start()

    def enqueue_recording(self, audio: bytes, filename: str, actor: str, consent: bool = False) -> int:
        """Accept a recording and return at once. The agents do the rest in the background."""
        if not self.settings.gnani_api_key:
            raise ActionError("Audio upload needs a Gnani key.")
        if not audio or len(audio) > self.MAX_AUDIO:
            raise ActionError("Choose an audio file under 10 MB.")
        if consent and not self.consent_on_file():
            self.set_consent(True, actor)
        if not self.consent_on_file():
            raise ActionError("Confirm that everyone on your calls agrees to recording.")
        if self._call_queue.qsize() >= self.MAX_QUEUED or not self.allow_ai_run():
            raise ActionError("The agents are busy. Try again in a minute.")
        return self._queue_call(audio, None, filename, "Gnani speech-to-text", actor, hashlib.sha256(audio).hexdigest())

    def enqueue_sample_call(self, actor: str) -> int:
        """Lets anyone watch the whole pipeline with no keys: a made-up transcript takes Gnani's place."""
        if not self.demo:
            raise ActionError("Sample calls are only for the demo.")
        if sum(c["filename"] == "sample-call.mp3" for c in self.db.all_calls()) >= self.SAMPLE_LIMIT:
            raise ActionError("That's enough sample calls for now. Remove one on the Calls page.")
        return self._queue_call(None, self.LIVE_SAMPLE, "sample-call.mp3", "made-up sample for the demo", actor, None)

    def _queue_call(self, audio, sample, filename, origin, actor, fingerprint) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        call_id = self.db.add_call(actor, "buyer", today, "", origin, fingerprint, (filename or "call")[:120], status="queued", reviewed=False)
        self.db.audit(actor, "call_added", None, {"call_id": call_id, "consent_confirmed": True, "audio_sha256": fingerprint})
        self._start_call_worker()
        self._call_queue.put((call_id, audio, sample))
        return call_id

    def _process_call(self, call_id: int, audio: bytes | None, sample: str | None) -> None:
        pause = self.SAMPLE_PAUSE if sample else 0
        call = self.db.get_call(call_id)
        self.db.update_call(call_id, status="transcribing")
        time.sleep(pause)
        if sample:
            text = sample
        else:
            try:
                text = gnani_transcribe(self.settings.gnani_api_key, self.settings.gnani_model, self.settings.gnani_language,
                                        audio, call["filename"], keyterms=self._listen_for())
            except LLMError as e:
                self.db.update_call(call_id, status="failed", error=str(e)[:200])
                self.db.audit("system", "call_failed", None, {"call_id": call_id})
                return
            self.db.audit("system", "call_transcribed", None, {"call_id": call_id, "provider": "gnani", "model": self.settings.gnani_model})
        del audio
        self.db.update_call(call_id, status="labelling", transcript=text)
        time.sleep(pause)
        text = agents.label_speakers(text, self.llm)
        self.db.update_call(call_id, status="selecting", transcript=text, party=core.call_party(text))
        self.db.audit("ai", "call_labelled", None, {"call_id": call_id})
        time.sleep(pause)
        self.db.update_call(call_id, status="ready")
        selected = self.select_recordings()
        dispute_id = self.db.get_call(call_id)["dispute_id"]
        for other in selected:
            if other != dispute_id:
                self.refresh_case(other)
        if dispute_id:
            self.db.update_call(call_id, status="reading")
            try:
                self.refresh_case(dispute_id, pause / 2)
            finally:
                self.db.update_call(call_id, status="ready")

    def refresh_case(self, dispute_id: str, pause: float = 0, already_counted: bool = False) -> None:
        """Re-run the team once a call changes. If it's mid-run, wait, so the new call isn't missed."""
        for _ in range(150):
            if dispute_id not in self.running:
                break
            time.sleep(2)
        if already_counted or self.allow_ai_run():
            self.analyze_quietly(dispute_id, pause)

    def confirm_call(self, call_id: int, transcript: str, actor: str) -> str:
        """The seller's one click. Only now can quotes from this call reach the reply."""
        call = self.db.get_call(call_id)
        if not call or call["status"] != "ready":
            raise ActionError("That recording isn't ready yet.")
        transcript = (transcript or call["transcript"]).replace("\r\n", "\n").strip()
        try:
            core.parse_call_turns(transcript)
        except ValueError as e:
            raise ActionError(str(e)) from None
        self.db.update_call(call_id, transcript=transcript, party=core.call_party(transcript), reviewed=1)
        self.db.audit(actor, "call_confirmed", call["dispute_id"] or None, {"call_id": call_id, "sha256": hashlib.sha256(transcript.encode()).hexdigest()})
        self.select_recordings()
        return self.db.get_call(call_id)["dispute_id"]

    def live_state(self) -> dict:
        """Everything the live agent panel shows. Reads only, and no call or buyer words: just names and progress."""
        calls, needs = self.db.all_calls(), []
        active = [c for c in calls if c["status"] in ("queued", "transcribing", "labelling", "selecting", "reading")]
        for c in calls:
            name = c["filename"] or "A pasted call"
            if c["status"] == "failed":
                needs.append({"kind": "failed", "title": f"Couldn't read {name}", "detail": c["error"], "href": "/recordings#attention"})
            elif c["status"] == "ready" and not c["dispute_id"]:
                needs.append({"kind": "case", "title": f"Choose a case for {name}", "detail": "It doesn't mention one open case.", "href": "/recordings#attention"})
            elif c["status"] == "ready" and not c["reviewed"]:
                needs.append({"kind": "check", "title": f"Check {name}", "detail": f"Quotes for {c['dispute_id']} are waiting for one click.", "href": f"/disputes/{c['dispute_id']}#calls"})
        for d in self.db.list_disputes():
            a = self.db.latest_analysis(d["dispute_id"])
            if a and core.allowed_actions(d) and not core.seller_submissions(d) and not core.dispute_outcome(d):
                due = core.deadline_info(d)
                days = f"{due['hours_left'] / 24:.1f} days left" if due["due"] and due["hours_left"] >= 0 else "Overdue" if due["due"] else "No deadline"
                needs.append({"kind": "approve", "title": f"Approve the reply on {d['dispute_id']}",
                              "detail": f"The team says {a['advice']['decision'].upper()}. {days}.", "href": f"/disputes/{d['dispute_id']}", "hours": due["hours_left"]})
        rank = {"approve": 0, "failed": 1, "check": 2, "case": 3}
        needs.sort(key=lambda n: (rank[n["kind"]], n.get("hours") if n.get("hours") is not None else 1e9))
        running = [{"dispute_id": k, **v} for k, v in list(self.live.items())]
        first_call = active[-1] if active else None
        team = f"{running[0]['agent']} on {running[0]['dispute_id']}" if running else "Waiting for the next case"
        roster = [
            {"name": "PayPal watcher", "state": "idle" if self.demo else "watching",
             "detail": "Paused in demo mode" if self.demo else f"Checks PayPal every {self.settings.watch_minutes} min"},
            {"name": "Case team", "state": "working" if running else "idle", "detail": team},
            {"name": "Verifier", "state": "working" if any(r["agent"] == "Verifier" for r in running) else "idle", "detail": "Strikes any claim without proof"},
            {"name": "Tracking", "state": "idle" if self.demo else "watching", "detail": "Paused in demo mode" if self.demo else "Adds tracking when a payment arrives"},
            {"name": "Call onboarding", "state": "working" if active else "idle",
             "detail": self.CALL_LABELS[first_call["status"]] if first_call else "Waiting for a recording"},
        ]
        return {"calls": [{"id": c["id"], "filename": c["filename"] or "A pasted call", "status": c["status"], "label": self.CALL_LABELS[c["status"]],
                           "dispute_id": c["dispute_id"]} for c in active],
                "running": running, "needs": needs, "agents": roster,
                "upload": bool(self.settings.gnani_api_key), "consent": bool(self.consent_on_file()), "demo": self.demo}

    def add_call(self, form: dict, actor: str, origin: str | None = None) -> list[str]:
        """Save a checked recording to the library, then let code select its case. Returns the case ids it selected."""
        if form.get("consent") != "yes":
            raise ActionError("Confirm that everyone on the call agreed to recording. Some US states, including California, require this.")
        if self.demo and len(self.db.all_calls()) >= self.MAX_DEMO_RECORDS:
            raise ActionError("The demo is full for now.")
        party = form.get("party")
        if party not in self.CALL_PARTIES:
            raise ActionError("Choose who the call was with.")
        day = core.parse_time(form.get("call_date") or "")
        if not day or day.date() > datetime.now(timezone.utc).date():
            raise ActionError("Enter the date of the call. It can't be in the future.")
        transcript = (form.get("transcript") or "").strip()
        for n in (1, 2):   # the seller's choices from the speaker menus
            who = form.get(f"speaker_{n}")
            if who and who.lower() in core.SPEAKER_ROLES:
                transcript = re.sub(rf"(?im)^\s*Speaker {n}\s*:", f"{who}:", transcript)
        if len(transcript) > self.MAX_TRANSCRIPT:
            raise ActionError(f"Keep the transcript under {self.MAX_TRANSCRIPT} characters.")
        try:
            core.parse_call_turns(transcript)
        except ValueError as e:
            raise ActionError(str(e)) from None
        fingerprint = form.get("audio_sha256") or None
        origin = origin or ("Gnani speech-to-text, checked by the seller" if fingerprint else "transcript from the seller")
        call_id = self.db.add_call(actor, party, day.date().isoformat(), transcript, origin, fingerprint, (form.get("filename") or "")[:120])
        self.db.audit(actor, "call_added", None, {"call_id": call_id, "party": party, "consent_confirmed": True,
                                                  "sha256": hashlib.sha256(transcript.encode()).hexdigest()})
        self.select_recordings()
        dispute_id = self.db.get_call(call_id)["dispute_id"]   # this call's own case, even if another job selected it first
        return [dispute_id] if dispute_id else []

    def add_sample_recordings(self) -> list[str]:
        day, selected = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat(), []
        for filename, party, transcript in self.SAMPLE_RECORDINGS:
            selected += self.add_call({"consent": "yes", "party": party, "call_date": day, "transcript": transcript, "filename": filename},
                                      "demo", origin="made-up sample for the demo")
        return list(dict.fromkeys(selected))

    def assign_recording(self, call_id: int, dispute_id: str, actor: str) -> None:
        call = self.db.get_call(call_id)
        if not call or call["status"] != "ready" or not self.db.get_dispute(dispute_id):
            raise ActionError("That recording or case isn't available.")
        self.db.assign_call(call_id, dispute_id, "your choice")
        self.db.audit(actor, "call_selected", dispute_id, {"call_id": call_id, "matched_on": "seller"})

    def remove_call(self, call_id: int, actor: str) -> str:
        call = self.db.get_call(call_id)
        if not call:
            raise ActionError("That recording is already gone.")
        self.db.delete_call(call_id)
        self.db.audit(actor, "call_removed", call["dispute_id"] or None, {"call_id": call_id})
        return call["dispute_id"]

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
                files = [("evidence-file", f["filename"], f["content"], f["mime"]) for f in self.db.document_files(dispute_id)]
                try:
                    result = self.pp.provide_evidence(dispute_id, payload, files) if files else self.pp.provide_evidence(dispute_id, payload)
                    break
                except PayPalError as e:
                    if n == len(options) or not e.has_issue("EVIDENCE_TYPE_IS_NOT_ALLOWED"):
                        raise
                    self.db.audit(actor, "evidence_type_refused", dispute_id, {"evidence_type": payload[0]["evidence_type"]})
            details = {"evidence_types": [p["evidence_type"] for p in payload], "files": len(files),
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
