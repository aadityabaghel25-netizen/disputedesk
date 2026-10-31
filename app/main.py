"""
The web server (Starlette + Uvicorn).

Run locally:   uvicorn app.main:app --reload
Then open:     http://127.0.0.1:8000

Routes
  GET  /                        dashboard: all disputes, deadlines, recommendations
  GET  /disputes/{id}           one dispute: evidence, AI draft, struck claims, approve buttons
  POST /disputes/{id}/analyze   run the AI team (in the background)
  POST /disputes/{id}/approve   human-approved action -> PayPal
  GET  /recordings              calls the onboarding agent has picked up; POST /recordings/upload runs it in the background
  GET  /agents/live             what every agent is doing right now (JSON, polled by the live panel)
  POST /sync                    pull disputes from PayPal now
  POST /disputes/load           add one dispute by its ID
  POST /webhooks/paypal         PayPal calls this when a dispute is created/updated
  GET  /disputes/{id}/pack      printable evidence pack (?format=txt to download)
  GET  /board                   deadline board (timeline of every open dispute)
  GET  /rules, POST /rules      seller rules: settlement limits, enforced by code
  GET  /proof, POST /proof      test the guardrails yourself; attack and evaluation results
  GET  /deadlines.ics           reply deadlines as a calendar file
  GET  /audit                   tamper-evident audit log
  GET  /healthz                 for Render's health check
"""

import base64
import contextlib
import json
import os
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import core, insights
from .config import PROJECT_ROOT, get_settings
from .llm import FakeLLM
from .paypal import PayPalError
from .service import ActionError, Service

templates = Jinja2Templates(directory=os.path.join(PROJECT_ROOT, "app", "templates"))

REASON_LABELS = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": "Item not received",
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": "Not as described",
    "UNAUTHORISED": "Unauthorised payment",
    "CREDIT_NOT_PROCESSED": "Refund not processed",
    "DUPLICATE_TRANSACTION": "Charged twice",
    "INCORRECT_AMOUNT": "Wrong amount",
}
templates.env.globals["injection_flags"] = core.injection_flags
ACTIVITY_LABELS = {
    "analysis": "AI team finished the case", "analysis_failed": "AI team hit a problem", "load_dispute": "Dispute loaded", "sync": "Checked PayPal",
    "submit_evidence": "Evidence sent to PayPal", "submit_supporting_info": "Case sent to PayPal", "send_message": "Message sent", "make_offer": "Offer sent",
    "accept_claim": "Claim accepted", "paypal_rejected": "PayPal refused an action", "evidence_type_refused": "PayPal refused an evidence type",
    "status_changed": "PayPal status changed", "replies_opened": "PayPal opened the case for replies", "webhook": "PayPal sent an update",
    "webhook_rejected": "Unverified webhook blocked", "rules_changed": "Your rules changed", "block_planned": "Work planned", "block_moved": "Work moved before the deadline",
    "download_pack": "Evidence pack opened", "prevent_tracking_added": "Tracking added to PayPal", "prevent_no_tracking": "No tracking number to add",
    "prevent_failed": "Tracking could not be added", "watch_failed": "PayPal check failed",
    "call_added": "Call recording added", "call_removed": "Call recording removed", "call_transcribed": "Call recording transcribed", "call_selected": "Call recording selected for a case", "call_labelled": "Speakers worked out on a call", "call_confirmed": "You confirmed a call",
    "call_failed": "A call couldn't be read", "call_consent_confirmed": "You confirmed recording consent", "call_consent_withdrawn": "You withdrew recording consent",
}
templates.env.globals["activity_label"] = lambda a: ACTIVITY_LABELS.get(a, a.replace("_", " ").capitalize())
templates.env.globals["reason_label"] = lambda r: REASON_LABELS.get(r, (r or "Other").replace("_", " ").title())


class BasicAuth(BaseHTTPMiddleware):
    """Password-protect the dashboard (it can move money!). PayPal's webhook and the
    health check stay open; the webhook is protected by its signature instead."""

    OPEN_PATHS = ("/webhooks/paypal", "/healthz")

    async def dispatch(self, request, call_next):
        s = request.app.state.service.settings
        if not s.admin_password or request.url.path.startswith(self.OPEN_PATHS):
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                if secrets.compare_digest(user, s.admin_user) and secrets.compare_digest(pw, s.admin_password):
                    return await call_next(request)
            except (ValueError, UnicodeDecodeError):
                pass
        return PlainTextResponse("Login required", 401, headers={"WWW-Authenticate": 'Basic realm="DisputeDesk"'})


class SameOrigin(BaseHTTPMiddleware):
    """Browsers send saved logins with cross-site form posts, so another website could otherwise press
    Approve for you. Golden rule 2 needs a real click on this site. PayPal's webhook has no Origin and is signed."""

    async def dispatch(self, request, call_next):
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and urllib.parse.urlsplit(origin).netloc != request.headers.get("host"):
            return PlainTextResponse("Cross-site request blocked", 403)
        return await call_next(request)


def svc(request: Request) -> Service:
    return request.app.state.service


async def index(request: Request):
    s = svc(request)
    rows = []
    for d in s.db.list_disputes():
        a = s.db.latest_analysis(d["dispute_id"])
        cents, cur = core.dispute_amount(d)
        rows.append({"d": d, "a": a, "amount": f"{core.from_cents(cents)} {cur}",
                     "deadline": core.deadline_info(d), "running": d["dispute_id"] in s.running,
                     "submitted": bool(core.seller_submissions(d)), "outcome": core.dispute_outcome(d), "next": core.next_step(d, a)})
    rows.sort(key=lambda r: (r["deadline"]["hours_left"] is None, r["deadline"]["hours_left"] or 0))
    return templates.TemplateResponse(request, "index.html", {
        "rows": rows, "demo": s.demo, "model": getattr(s.llm, "model", None) or s.llm.name,
        "events": s.db.recent_events(5), "flash": request.query_params.get("msg"), "stats": s.stats(),
        "urgent": [r for r in rows if r["deadline"]["urgency"] in ("critical", "overdue") and not r["outcome"] and not r["submitted"]],
        "last_watch": s.last_watch, "watch_minutes": 0 if s.demo else s.settings.watch_minutes,
        "no_password": not s.settings.admin_password and bool(os.environ.get("RENDER")) and not s.demo})


async def dispute_page(request: Request):
    s = svc(request)
    dispute_id = request.path_params["dispute_id"]
    d = s.db.get_dispute(dispute_id)
    if not d:
        return PlainTextResponse("Dispute not found. Try 'Sync from PayPal' on the dashboard.", 404)
    a = s.db.latest_analysis(dispute_id)
    cents, cur = core.dispute_amount(d)
    return templates.TemplateResponse(request, "dispute.html", {
        "calls": s.db.list_calls(dispute_id), "call_labels": s.CALL_LABELS,
        "d": d, "a": a, "evidence": s.db.get_evidence(dispute_id), "amount": f"{core.from_cents(cents)} {cur}",
        "deadline": core.deadline_info(d), "allowed": core.allowed_actions(d), "demo": s.demo,
        "running": dispute_id in s.running, "audit": s.db.audit_log(dispute_id, 20),
        "flash": request.query_params.get("msg"), "error": request.query_params.get("err"),
        "max_offer_pct": s.settings.max_offer_pct, "statements": core.dispute_statements(d),
        "requested": core.requested_evidence(d), "submissions": core.seller_submissions(d), "outcome": core.dispute_outcome(d)})


async def analyze(request: Request):
    s = svc(request)
    dispute_id = request.path_params["dispute_id"]
    if not s.allow_ai_run():
        return RedirectResponse(f"/disputes/{dispute_id}?err=The+demo+has+reached+its+hourly+AI+limit.+Please+try+later.", 303)
    return RedirectResponse(f"/disputes/{dispute_id}?msg=The+AI+team+is+working.+This+page+refreshes+by+itself.",
                            303, background=BackgroundTask(s.analyze_quietly, dispute_id))


async def approve(request: Request):
    s = svc(request)
    dispute_id = request.path_params["dispute_id"]
    form = dict(await request.form())
    action = form.pop("action", "")
    try:
        s.approve(dispute_id, action, form, actor=s.settings.admin_user)
    except ActionError as e:
        return RedirectResponse(f"/disputes/{dispute_id}?err={str(e)}", 303)
    except PayPalError as e:  # PayPal said no: keep the full answer in the audit log, show the reason
        s.db.audit(s.settings.admin_user, "paypal_rejected", dispute_id, {"action": action, "status": e.status, "body": e.body})
        return RedirectResponse(f"/disputes/{dispute_id}?err=PayPal+said+no:+{e.summary()[:500]}", 303)
    except Exception as e:
        return RedirectResponse(f"/disputes/{dispute_id}?err=PayPal+error:+{str(e)[:300]}", 303)
    done = {"submit_evidence": "Evidence submitted", "submit_supporting_info": "Case sent",
            "send_message": "Message sent",
            "make_offer": "Offer sent", "accept_claim": "Claim accepted"}.get(action, "Done")
    suffix = " (demo: nothing was sent to PayPal)" if s.demo else " to PayPal"
    return RedirectResponse(f"/disputes/{dispute_id}?msg={done}{suffix}.", 303)


def _render_recordings(request: Request, s: Service):
    calls = s.db.all_calls()
    working = ("queued", "transcribing", "labelling", "selecting", "reading")
    return templates.TemplateResponse(request, "recordings.html", {
        "failed": [c for c in calls if c["status"] == "failed"],
        "unmatched": [c for c in calls if c["status"] == "ready" and not c["dispute_id"]],
        "to_check": [c for c in calls if c["status"] == "ready" and c["dispute_id"] and not c["reviewed"]],
        "selected": [c for c in calls if c["status"] not in working and c["status"] != "failed" and c["dispute_id"] and c["reviewed"]],
        "cases": [d["dispute_id"] for d in s.db.list_disputes() if not core.dispute_outcome(d)],
        "stt_on": bool(s.settings.gnani_api_key), "today": core.datetime.now(core.timezone.utc).date().isoformat(), "demo": s.demo,
        "flash": request.query_params.get("msg"), "error": request.query_params.get("err")})


async def recordings(request: Request):
    """The Calls page, and pasting a transcript by hand when there's no audio."""
    s = svc(request)
    if request.method == "GET":
        return _render_recordings(request, s)
    try:
        selected = s.add_call(dict(await request.form()), s.settings.admin_user)
    except ActionError as e:
        return RedirectResponse(f"/recordings?err={urllib.parse.quote(str(e))}", 303)
    if not selected:
        return RedirectResponse("/recordings?msg=Saved.+It+doesn't+mention+an+open+case,+so+choose+one+below.", 303)
    return _rerun_agents(s, selected, "/recordings", f"Selected for {selected[0]}.")


def _rerun_agents(s: Service, dispute_ids: list[str], back: str, done: str):
    """The agent team runs itself once a recording joins a case. It only prepares a draft."""
    allowed = [d for d in dispute_ids if s.allow_ai_run()]
    note = " The AI team is reading it now." if allowed else " Run the AI team on the case to use it."

    def work():
        for dispute_id in allowed:
            s.refresh_case(dispute_id, already_counted=True)   # waits if the team is already on this case
    return RedirectResponse(f"{back}?msg={urllib.parse.quote(done + note)}", 303, background=BackgroundTask(work))


async def upload_recording(request: Request):
    """Takes the file and answers at once. The agents do the rest in the background."""
    s = svc(request)
    form = await request.form()
    upload = form.get("audio")
    try:
        if not hasattr(upload, "read"):
            raise ActionError("Choose an audio file.")
        audio = await upload.read(s.MAX_AUDIO + 1)
        call_id = s.enqueue_recording(audio, upload.filename or "call", s.settings.admin_user, consent=form.get("consent") == "yes")
    except ActionError as e:
        return JSONResponse({"error": str(e)}, 400)
    return JSONResponse({"id": call_id})


async def sample_recording(request: Request):
    s = svc(request)
    try:
        return JSONResponse({"id": s.enqueue_sample_call(s.settings.admin_user)})
    except ActionError as e:
        return JSONResponse({"error": str(e)}, 400)


async def confirm_recording(request: Request):
    s = svc(request)
    call_id = request.path_params["call_id"]
    form = await request.form()
    try:
        dispute_id = s.confirm_call(call_id, str(form.get("transcript") or ""), s.settings.admin_user)
    except ActionError as e:
        call = s.db.get_call(call_id) or {}
        back = f"/disputes/{call['dispute_id']}" if call.get("dispute_id") else "/recordings"
        return RedirectResponse(f"{back}?err={urllib.parse.quote(str(e))}#calls", 303)
    return _rerun_agents(s, [dispute_id] if dispute_id else [], f"/disputes/{dispute_id}" if dispute_id else "/recordings", "Confirmed.")


async def calls_consent(request: Request):
    s = svc(request)
    s.set_consent((await request.form()).get("consent") == "yes", s.settings.admin_user)
    return RedirectResponse("/rules?msg=Saved.", 303)


AGENT_ACTORS = {"ai": "Agents", "system": "Agents", "paypal": "PayPal", "demo": "Demo"}


async def agents_live(request: Request):
    s = svc(request)
    state = s.live_state()
    state["feed"] = [{"ts": r["ts"][11:16], "who": AGENT_ACTORS.get(r["actor"], "You"), "what": ACTIVITY_LABELS.get(r["action"], r["action"].replace("_", " ").capitalize()),
                      "dispute_id": r["dispute_id"]} for r in s.db.audit_log(limit=14)]
    return JSONResponse(state)


async def assign_recording(request: Request):
    s = svc(request)
    dispute_id = str((await request.form()).get("dispute_id") or "")
    try:
        s.assign_recording(request.path_params["call_id"], dispute_id, s.settings.admin_user)
    except ActionError as e:
        return RedirectResponse(f"/recordings?err={urllib.parse.quote(str(e))}", 303)
    return _rerun_agents(s, [dispute_id], "/recordings", f"Added to {dispute_id}.")


async def remove_recording(request: Request):
    s = svc(request)
    try:
        dispute_id = s.remove_call(request.path_params["call_id"], s.settings.admin_user)
    except ActionError as e:
        return RedirectResponse(f"/recordings?err={urllib.parse.quote(str(e))}", 303)
    return _rerun_agents(s, [dispute_id] if dispute_id else [], "/recordings", "Recording removed.")


async def sync(request: Request):
    s = svc(request)
    try:
        n = s.sync()
        return RedirectResponse(f"/?msg=Synced+{n}+dispute(s).", 303)
    except Exception as e:
        return RedirectResponse(f"/?msg=Sync+failed:+{str(e)[:300]}", 303)


async def load(request: Request):
    s = svc(request)
    form = await request.form()
    dispute_id = (form.get("dispute_id") or "").strip()
    if not dispute_id:
        return RedirectResponse("/?msg=Enter+a+dispute+ID.", 303)
    try:
        s.load(dispute_id)
    except Exception as e:
        return RedirectResponse(f"/?msg=Could+not+load+{dispute_id}:+{str(e)[:200]}", 303)
    return RedirectResponse(f"/disputes/{dispute_id}", 303)


async def webhook(request: Request):
    s = svc(request)
    raw = await request.body()
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON"}, 400)
    try:
        status, message, to_analyze = s.handle_webhook(dict(request.headers), event)
    except Exception as e:
        # 500 makes PayPal retry later, which is what we want for temporary errors
        return JSONResponse({"error": str(e)[:300]}, 500)
    task = BackgroundTask(s.analyze_quietly, to_analyze) if to_analyze else None
    return JSONResponse({"result": message}, status, background=task)


async def pack(request: Request):
    s = svc(request)
    dispute_id = request.path_params["dispute_id"]
    data = s.evidence_pack(dispute_id)
    if not data:
        return PlainTextResponse("Run the AI team on this dispute first.", 404)
    if request.query_params.get("format") == "txt":
        a, lines = data["analysis"], []
        lines += [f"DISPUTE EVIDENCE PACK - {dispute_id}", f"Amount: {data['amount']}",
                  f"Reason: {data['dispute'].get('reason')}", f"Status: {data['dispute'].get('status')}", "",
                  "SELLER RESPONSE", a["reply_text"], "", "EVIDENCE"]
        for e in data["evidence"]:
            by_buyer = not e["trusted"] and e["kind"] != core.CALL_KIND
            lines.append(f"[{e['id']}] {e['source']}{' (written by buyer)' if by_buyer else ''}: {e['summary']}")
        headers = {"Content-Disposition": f'attachment; filename="{dispute_id}-evidence.txt"'}
        return PlainTextResponse("\n".join(lines), headers=headers)
    return templates.TemplateResponse(request, "pack.html", data)


async def board(request: Request):
    """Timeline: one bar per open dispute, from the day it was opened to its reply deadline."""
    s = svc(request)
    now = core.datetime.now(core.timezone.utc)
    rows = []
    for d in s.db.list_disputes():
        if d.get("status") == "RESOLVED":
            continue
        dl = core.deadline_info(d, now)
        if not dl["due"]:
            continue
        a = s.db.latest_analysis(d["dispute_id"])
        start = core.parse_time(dl["created"]) or now
        cents, cur = core.dispute_amount(d)
        rows.append({"d": d, "dl": dl, "start": start, "end": core.parse_time(dl["due"]),
                     "amount": f"{core.from_cents(cents)} {cur}", "decision": a["advice"]["decision"] if a else None})
    rows.sort(key=lambda r: r["end"])
    if rows:
        lo = min(min(r["start"] for r in rows), now) - core.timedelta(days=1)
        hi = max(max(r["end"] for r in rows), now) + core.timedelta(days=1)
        span = (hi - lo).total_seconds()
        pct = lambda t: round((t - lo).total_seconds() / span * 100, 2)  # noqa: E731
        for r in rows:
            r["left"], r["width"] = pct(r["start"]), max(pct(r["end"]) - pct(r["start"]), 1)
        today = pct(now)
        days = [(lo + core.timedelta(days=i)) for i in range(1, (hi - lo).days + 1)]
        ticks = [{"left": pct(t.replace(hour=0, minute=0, second=0, microsecond=0)), "label": t.strftime("%b %d")}
                 for t in days[::max(len(days) // 8, 1)]]
    else:
        today, ticks = 0, []
    return templates.TemplateResponse(request, "board.html", {"rows": rows, "today": today, "ticks": ticks, "demo": s.demo,
                                                              "flash": request.query_params.get("msg"), "unplanned": s.board_data()["unplanned"]})


async def board_data(request: Request):
    return JSONResponse(svc(request).board_data())


async def board_block(request: Request):
    s = svc(request)
    try:
        body = await request.json()
        s.save_block(str(body["dispute_id"]), str(body["handler"]), str(body["start"]), str(body["end"]), s.settings.admin_user)
    except (ActionError, KeyError, TypeError, ValueError) as e:
        return JSONResponse({"error": str(e)[:200]}, 400)
    return JSONResponse(s.board_data())


async def board_autoplan(request: Request):
    s = svc(request)
    n = s.autoplan(s.settings.admin_user)
    return RedirectResponse(f"/board?msg=Planned+time+for+{n}+dispute(s).", 303)


async def rules(request: Request):
    s = svc(request)
    if request.method == "POST":
        try:
            s.update_rules(dict(await request.form()), actor=s.settings.admin_user)
        except ActionError as e:
            return RedirectResponse(f"/rules?err={str(e)}", 303)
        return RedirectResponse("/rules?msg=Rules+saved.+New+analyses+use+them.", 303)
    return templates.TemplateResponse(request, "rules.html", {
        "settle_max_usd": f"{s.settings.settle_max_usd:.2f}", "max_offer_pct": s.settings.max_offer_pct, "calls_consent": s.consent_on_file(),
        "demo": s.demo, "flash": request.query_params.get("msg"), "error": request.query_params.get("err")})


def _ics_stamp(iso: str) -> str:
    return core.parse_time(iso).astimezone(core.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


async def deadlines_ics(request: Request):
    """Reply deadlines as a calendar file, so a required human click isn't missed."""
    s = svc(request)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//DisputeDesk//Deadlines//EN"]
    for d in s.db.list_disputes():
        dl = core.deadline_info(d)
        if not dl["due"] or d.get("status") == "RESOLVED":
            continue
        due = _ics_stamp(dl["due"])
        label = "estimated " if dl["estimated"] else ""
        lines += ["BEGIN:VEVENT", f"UID:{d['dispute_id']}@disputedesk", f"DTSTAMP:{_ics_stamp(core.datetime.now(core.timezone.utc).isoformat())}",
                  f"DTSTART:{due}", f"DTEND:{due}", f"SUMMARY:Reply to PayPal dispute {d['dispute_id']} ({label}deadline)",
                  "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:PayPal dispute reply due in 2 days", "TRIGGER:-P2D", "END:VALARM", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return PlainTextResponse("\r\n".join(lines) + "\r\n", media_type="text/calendar",
                             headers={"Content-Disposition": 'attachment; filename="disputedesk-deadlines.ics"'})


# A tiny, fixed case for the Proof page, so anyone can test the guardrails with no keys or AI call.
PROOF_EVIDENCE = {
    "E1": {"content": {"carrier": "FEDEX", "tracking_number": "785412369874", "delivered_on": "2026-10-18"},
           "summary": "FEDEX tracking 785412369874: delivered 2026-10-18 to 2211 N First St, San Jose, CA 95131."},
    "E2": {"content": {"text": "Bag arrived, looks nice, thanks!"}, "summary": "Buyer message on 2026-10-19."},
}
PROOF_DISPUTE = {"dispute_amount": {"value": "40.00", "currency_code": "USD"}, "links": [{"rel": "provide_evidence"}]}
TOOL_POLICY = [
    ("Read the dispute, order and tracking", "Code", "Allowed", "Read-only PayPal calls."),
    ("Look up buyer history", "AI", "Allowed", "The only tool the AI is given. Read-only."),
    ("Choose fight, settle or review", "Code", "Allowed", "Plain rules. The AI has no say."),
    ("Calculate an offer amount", "Code", "Allowed", "Integer cents, capped by your rules."),
    ("Submit evidence or send a message", "You", "Human only", "Runs only after you click, and only if PayPal offers the action."),
    ("Send a settlement offer", "You", "Human only", "Blocked by code above your limit."),
    ("Accept the claim and refund", "You", "Human only", "You must type ACCEPT."),
    ("Any PayPal write", "AI", "Blocked", "The AI has no tool that can write to PayPal."),
]


def _results(name: str) -> dict | None:
    try:
        with open(os.path.join(PROJECT_ROOT, "docs", name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


async def proof(request: Request):
    s = svc(request)
    claim, message, claim_result, message_result = "", "", None, None
    if request.method == "POST":
        form = await request.form()
        claim, message = (form.get("claim") or "").strip()[:500], (form.get("message") or "").strip()[:2000]
        if claim:
            ok, why = core.check_claim({"text": claim, "evidence_ids": ["E1", "E2"]}, PROOF_EVIDENCE)
            claim_result = {"ok": ok, "why": why}
        if message:
            advice = core.recommend(PROOF_DISPUTE, ["Tracking shows the item was delivered"], [], 3, 25, 50)
            message_result = {"flagged": bool(core.injection_flags(message)), "decision": advice["decision"]}
    ok, bad = s.db.verify_audit_chain()
    return templates.TemplateResponse(request, "proof.html", {
        "demo": s.demo, "claim": claim, "message": message, "claim_result": claim_result, "message_result": message_result,
        "evidence": PROOF_EVIDENCE, "attacks": _results("attack_results.json"), "evals": _results("eval_results.json"),
        "policy": TOOL_POLICY, "chain_ok": ok, "chain_bad": bad})


async def insights_page(request: Request):
    s = svc(request)
    return templates.TemplateResponse(request, "insights.html", {"demo": s.demo, "sample": s.demo, "ask_ai": bool(s.settings.agent_model)})


MAX_AGENT_REQUEST = 2_000_000
ITEM_ID_PREFIX = {"function_call": "fc", "reasoning": "rs", "message": "msg"}


def _with_item_ids(items):
    """Nebius requires an id on replayed items, where OpenAI's API treats it as optional."""
    if not isinstance(items, list):
        return items
    out = []
    for item in items:
        prefix = ITEM_ID_PREFIX.get(item.get("type")) if isinstance(item, dict) else None
        out.append({**item, "id": f"{prefix}_{secrets.token_hex(8)}"} if prefix and not item.get("id") else item)
    return out


async def insights_ai(request: Request):
    """Proxy for the dashboard agent. The key stays on the server, the model is fixed here, and turns are
    limited. The agent only has AG Studio's dashboard tools and the buyer-text-free data, so it has no PayPal access."""
    s = svc(request)
    if isinstance(s.llm, FakeLLM) or not s.settings.agent_model:
        return JSONResponse({"error": "The dashboard agent is switched off."}, 503)
    if not s.allow_ai_run("agent"):
        return JSONResponse({"error": {"message": "The demo has reached its hourly AI limit. Please try later."}}, 429)
    raw = await request.body()
    if len(raw) > MAX_AGENT_REQUEST:
        return JSONResponse({"error": {"message": "Request too large."}}, 413)
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return JSONResponse({"error": {"message": "Invalid JSON."}}, 400)
    if not isinstance(body, dict):
        return JSONResponse({"error": {"message": "Invalid request."}}, 400)
    body["model"] = s.settings.agent_model
    body["input"] = _with_item_ids(body.get("input"))
    upstream = urllib.request.Request(s.settings.llm_base_url.rstrip("/") + "/responses", data=json.dumps(body).encode(), method="POST",
                                      headers={"Authorization": f"Bearer {s.settings.llm_api_key}", "Content-Type": "application/json"})
    try:
        resp = await run_in_threadpool(urllib.request.urlopen, upstream, timeout=180)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        print(f"[agent proxy] provider returned {e.code}: {detail}", flush=True)   # the seller only sees the short message
        return JSONResponse({"error": {"message": f"AI provider said no ({e.code})."}}, 502)
    except (urllib.error.URLError, TimeoutError):
        return JSONResponse({"error": {"message": "Could not reach the AI provider."}}, 502)

    def stream():
        with resp:
            while chunk := resp.read1(8192):
                yield chunk
    return StreamingResponse(stream(), media_type=resp.headers.get("Content-Type", "text/event-stream"))


async def insights_data(request: Request):
    return JSONResponse(insights.build(svc(request)))


async def audit(request: Request):
    s = svc(request)
    ok, bad = s.db.verify_audit_chain()
    return templates.TemplateResponse(request, "audit.html", {"rows": s.db.audit_log(limit=200), "ok": ok,
                                                              "bad": bad, "demo": s.demo})


async def healthz(request: Request):
    return PlainTextResponse("ok")


@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    threading.Thread(target=app.state.service.seed_demo, daemon=True).start()
    app.state.service.start_watcher()   # background re-check of open disputes
    yield


def create_app(service: Service | None = None) -> Starlette:
    app = Starlette(routes=[
        Route("/", index),
        Route("/disputes/{dispute_id}", dispute_page),
        Route("/disputes/{dispute_id}/analyze", analyze, methods=["POST"]),
        Route("/disputes/{dispute_id}/approve", approve, methods=["POST"]),
        Route("/disputes/{dispute_id}/pack", pack),
        Route("/recordings", recordings, methods=["GET", "POST"]),
        Route("/recordings/upload", upload_recording, methods=["POST"]),
        Route("/recordings/sample", sample_recording, methods=["POST"]),
        Route("/recordings/{call_id:int}/confirm", confirm_recording, methods=["POST"]),
        Route("/recordings/consent", calls_consent, methods=["POST"]),
        Route("/agents/live", agents_live),
        Route("/recordings/{call_id:int}/assign", assign_recording, methods=["POST"]),
        Route("/recordings/{call_id:int}/delete", remove_recording, methods=["POST"]),
        Route("/sync", sync, methods=["POST"]),
        Route("/disputes/load", load, methods=["POST"]),
        Route("/webhooks/paypal", webhook, methods=["POST"]),
        Route("/insights", insights_page),
        Route("/insights/data", insights_data),
        Route("/insights/ai/responses", insights_ai, methods=["POST"]),
        Mount("/static", StaticFiles(directory=os.path.join(PROJECT_ROOT, "app", "static"))),
        Route("/board", board),
        Route("/board/data", board_data),
        Route("/board/block", board_block, methods=["POST"]),
        Route("/board/autoplan", board_autoplan, methods=["POST"]),
        Route("/deadlines.ics", deadlines_ics),
        Route("/proof", proof, methods=["GET", "POST"]),
        Route("/rules", rules, methods=["GET", "POST"]),
        Route("/audit", audit),
        Route("/healthz", healthz),
    ], middleware=[Middleware(SameOrigin), Middleware(BasicAuth)], lifespan=lifespan)
    app.state.service = service or Service(get_settings())
    return app


app = create_app() if os.environ.get("DISPUTEDESK_NO_AUTOAPP") != "1" else None
