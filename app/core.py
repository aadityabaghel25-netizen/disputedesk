"""
The deterministic core: plain Python rules, no AI.

Golden rule #1: "AI reads and writes. Code decides money."
Everything that touches money, deadlines or permissions lives here, so it is
exact, testable and the same every time.
"""

import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone  # noqa: F401 (re-used by main.py)

from .config import PROJECT_ROOT

# ---------------------------------------------------------------------------
# Money: always integer cents. Floats make rounding errors (0.1 + 0.2 != 0.3).
# ---------------------------------------------------------------------------


def to_cents(value: str | int | float) -> int:
    s = str(value).strip()
    if not re.fullmatch(r"\d+(\.\d{1,2})?", s):
        raise ValueError(f"Not a money amount: {value!r}")
    whole, _, frac = s.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


def from_cents(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def dispute_amount(dispute: dict) -> tuple[int, str]:
    amt = dispute.get("dispute_amount") or {}
    return to_cents(amt.get("value", "0")), amt.get("currency_code", "USD")


# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------

def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    # PayPal sometimes sends "+0000" without a colon
    v = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", v)
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


RESPONSE_DAYS = 10   # PayPal usually gives the seller 10 days; used only when PayPal sends no due date


def deadline_info(dispute: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    created = parse_time(dispute.get("create_time"))
    due, estimated = parse_time(dispute.get("seller_response_due_date")), False
    if not due and created and not seller_submissions(dispute):
        due, estimated = created + timedelta(days=RESPONSE_DAYS), True
    if not due:
        return {"due": None, "hours_left": None, "urgency": "unknown", "estimated": False, "created": None}
    hours = (due - now).total_seconds() / 3600
    urgency = "overdue" if hours < 0 else "critical" if hours < 48 else "soon" if hours < 120 else "ok"
    return {"due": due.isoformat(), "hours_left": round(hours, 1), "urgency": urgency, "estimated": estimated,
            "created": created.isoformat() if created else None}


# ---------------------------------------------------------------------------
# PayPal's `evidences` list holds more than documents: the buyer's opening words (type CREATE),
# what PayPal wants from the seller, and what the seller has already filed.
# ---------------------------------------------------------------------------

def dispute_statements(dispute: dict) -> list[dict]:
    """Every message on a case. PayPal can drop `messages` after escalation, while the buyer's
    opening words survive in `evidences`, so read both or injection text goes unscanned."""
    out, seen = [], set()
    for m in dispute.get("messages") or []:
        out.append(m)
        seen.add((m.get("content") or "").strip())
    for e in dispute.get("evidences") or []:
        notes = (e.get("notes") or "").strip(" \t\r\n,\"\u201c\u201d")
        if e.get("source") == "SUBMITTED_BY_BUYER" and notes and notes not in seen:
            seen.add(notes)
            out.append({"posted_by": "BUYER", "time_posted": e.get("date"), "content": notes})
    return out


def requested_evidence(dispute: dict) -> list[str]:
    types = [e.get("evidence_type", "") for e in dispute.get("evidences") or [] if e.get("source") == "REQUESTED_FROM_SELLER"]
    return [t.replace("_", " ").capitalize() for t in dict.fromkeys(types)]


def seller_submissions(dispute: dict) -> list[dict]:
    """PayPal's own record of what the seller filed: our receipt, and the guard against filing twice."""
    return [e for e in dispute.get("evidences") or [] if e.get("source") == "SUBMITTED_BY_SELLER"]


# ---------------------------------------------------------------------------
# Permissions: PayPal tells us which actions are allowed right now via "links".
# We never attempt an action PayPal hasn't offered.
# ---------------------------------------------------------------------------

ACTION_RELS = {"send_message", "provide_evidence", "make_offer", "accept_claim",
               "appeal", "acknowledge_return_item", "provide_supporting_info", "escalate"}


def allowed_actions(dispute: dict) -> set[str]:
    rels = {link.get("rel", "") for link in dispute.get("links", [])}
    return rels & ACTION_RELS


# ---------------------------------------------------------------------------
# Policy knowledge base (the "R" in RAG: Retrieval)
# ---------------------------------------------------------------------------

POLICY_FILE = os.path.join(PROJECT_ROOT, "policy", "dispute_rules.md")


def load_policy_sections(path: str = POLICY_FILE) -> dict[str, str]:
    sections, current, buf = {}, None, []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("## "):
                if current:
                    sections[current] = "".join(buf).strip()
                current, buf = line[3:].strip(), []
            elif current:
                buf.append(line)
    if current:
        sections[current] = "".join(buf).strip()
    return sections


def retrieve_policy(reason: str) -> str:
    """Return the rules for this dispute reason + the general rules.
    Simple and exact: the reason code IS the lookup key. (Real RAG with vector
    search is useful when you have hundreds of pages; we have one page.)"""
    sections = load_policy_sections()
    specific = sections.get(reason) or sections.get("OTHER", "")
    return f"Rules for {reason}:\n{specific}\n\nGeneral rules:\n{sections.get('General rules for every reply', '')}"


# ---------------------------------------------------------------------------
# Safety: untrusted text (buyer messages) may contain prompt injection
# ---------------------------------------------------------------------------

INJECTION_PATTERNS = [
    # overriding the AI's instructions
    r"(ignore|disregard|forget)\b.{0,25}\b(previous|prior|earlier|above|everything|instructions?|rules)\b",
    r"\b(new|updated) (policy|instructions?|rules?)\s*:",
    # talking to the AI directly
    r"\b(note|message|attention|instructions?) (to|for) (the |any )?(ai|assistant|bot|model|agent|llm)\b",
    r"\b(the |any )?(ai|assistant|bot|model|llm)\b[^.!?]{0,40}\b(must|should|shall|needs? to|has to|have to)\b",
    r"(^|[.!?\n] ?)assistant\s*[,:]",
    r"\bas an ai\b",
    # fake authority
    r"\b(admin|administrator|system|compliance|root)\b.{0,12}\boverride\b",
    r"\boverride\s*(code|token|mode)\b",
    # fake system markup or conversation turns
    r"</?\s*(system|assistant|user|instructions?)\s*>",
    r"\[/?\s*(system|inst|instructions?)\s*\]",
    r"#{2,}\s*(instructions?|system)\b",
    r"\b(system|user|human)\s*:",
    # role play and jailbreaks
    r"\byou are now\b", r"\bpretend (that )?(you are|to be)\b", r"\bact as (an?|the|my)\b",
    r"\bdeveloper mode\b", r"\bjailbreak\b",
    # fake approval, or asking to skip a safety step
    r"\b(seller|merchant|owner) (has )?already (approved|agreed|accepted|authori[sz]ed)\b",
    r"\b(skip|bypass|disable|turn off)\b.{0,20}\b(checks?|verifier|verification|review|approval|rules)\b",
    r"\bno (human )?(approval|review|verification)\b.{0,15}\bneeded\b",
    # reading out the prompt
    r"\bsystem prompt\b",
    r"\b(print|reveal|show|repeat|output)\b.{0,10}\b(your|the)\b.{0,20}\b(prompt|instructions|rules)\b",
    # steering tools, evidence or the decision
    r"\b(call|use|run|invoke)\b.{0,15}\btool\b",
    r"\btreat (this|the|my)\b.{0,20}\bas\b.{0,15}\b(verified|evidence|proof|fact)\b",
    r"\b(do not|don'?t|never) (cite|use|mention)\b.{0,30}\b(tracking|evidence|record)\b",
    r"\boutput\b.{0,20}\b(decision|recommendation|verdict)\b",
    r"\b(decision|recommendation|verdict)\s*=",
    r"\bmust (accept|approve|refund)\b",
    r"\b(issue|approve|send) (a |the )?(full )?refund (now|immediately|right away)\b",
]


def _normalise_for_scan(text: str) -> str:
    """Attackers hide words with invisible characters, odd unicode or extra spaces."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")   # zero-width and other format characters
    return re.sub(r"\s+", " ", text).strip().lower()


def injection_flags(text: str) -> list[str]:
    """A warning for the seller, not the defence itself. Even if an attack slips past this, the AI
    has read-only tools, code decides money, and nothing reaches PayPal without a human click."""
    t = _normalise_for_scan(text)
    return [p for p in INJECTION_PATTERNS if re.search(p, t)]


# ---------------------------------------------------------------------------
# Verifier, code layer: every claim must cite real evidence, and every
# date / amount / tracking number in the claim must appear in that evidence.
# ---------------------------------------------------------------------------

FACT_PATTERNS = [
    r"\b\d{4}-\d{2}-\d{2}\b",                 # dates like 2026-10-20
    r"\$\s?\d+(?:\.\d{2})?",                    # $40 or $40.00
    r"\b\d+\.\d{2}\b",                          # 40.00
    r"\b(?=[A-Z0-9]*\d)[A-Z0-9]{8,}\b",         # tracking numbers / ids (must contain a digit)
]


QUOTE_PATTERN = re.compile(r'["\u201c]([^"\u201d]+)["\u201d]')


def _norm_quote(text: str) -> str:
    text = text.replace("\\", "").replace("\u2019", "'")
    return " ".join(re.sub(r"[^\w' ]+", " ", text).lower().split())


def extract_facts(text: str) -> set[str]:
    facts = set()
    for p in FACT_PATTERNS:
        for m in re.findall(p, text):
            facts.add(m.replace("$", "").replace(" ", ""))
    return facts


def check_claim(claim: dict, evidence_by_id: dict[str, dict]) -> tuple[bool, str]:
    ids = claim.get("evidence_ids") or []
    if not ids:
        return False, "No evidence cited."
    missing = [i for i in ids if i not in evidence_by_id]
    if missing:
        return False, f"Cites evidence that does not exist: {', '.join(missing)}."
    cited_text = " ".join(str(evidence_by_id[i]["content"]) + " " + evidence_by_id[i]["summary"] for i in ids)
    cited_facts = extract_facts(cited_text)
    for quote in QUOTE_PATTERN.findall(claim.get("text", "")):
        if len(quote.split()) >= 3 and _norm_quote(quote) not in _norm_quote(cited_text):
            return False, f"Quotes \"{quote}\", which is not in the cited evidence."
    # A fact in the claim must appear verbatim or as a number in the cited evidence
    for fact in extract_facts(claim.get("text", "")):
        normalized = fact.split(".")[0] if re.fullmatch(r"\d+\.00", fact) else fact
        if fact not in cited_facts and normalized not in cited_text:
            return False, f"Mentions '{fact}', which is not in the cited evidence."
    ok, why = check_call_claim(claim, evidence_by_id)
    if not ok:
        return False, why
    return True, "Cited evidence exists and contains every date, amount and ID mentioned."


# ---------------------------------------------------------------------------
# Call transcripts prove what someone said, never that it happened. A driver saying "I delivered it"
# is not the carrier record PayPal's terms ask for, and speech-to-text can mishear. So a transcript can
# only be quoted word for word, from the other party, and it never satisfies a PayPal requirement.
# ---------------------------------------------------------------------------

CALL_KIND = "call_transcript"
NOT_RECEIVED_REASON, NOT_AS_DESCRIBED_REASON, UNAUTHORISED_REASON = ("MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                                                                  "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", "UNAUTHORISED")
SPEAKER_ROLES = {"buyer": "buyer", "customer": "buyer", "carrier": "carrier", "driver": "carrier", "courier": "carrier",
                 "seller": "staff", "agent": "staff", "staff": "staff", "support": "staff"}
CALL_ASSERTION = re.compile(r"\b(delivered|delivery|received|arrived|confirm\w*|prov\w*|admit\w*|verif\w*)\b", re.I)
NEGATIONS = {"not", "never", "no", "without", "didn't", "don't", "doesn't", "wasn't", "isn't", "hasn't", "haven't", "won't", "can't"}


def parse_call_turns(transcript: str) -> list[dict]:
    """Every line must start with who spoke ("Buyer: ..."). Speech-to-text can't reliably tell
    speakers apart, so the seller labels them, and a quote can be traced to the person who said it."""
    turns = []
    for line in transcript.splitlines():
        if not line.strip():
            continue
        label, sep, text = line.partition(":")
        role = SPEAKER_ROLES.get(label.strip().lower())
        if not sep or not role or not text.strip():
            raise ValueError(f"Start each line with who spoke, like \"Buyer: ...\" or \"Agent: ...\". Check: {line.strip()[:60]}")
        turns.append({"speaker": role, "text": text.strip()})
    if not turns:
        raise ValueError("The transcript is empty.")
    if all(t["speaker"] == "staff" for t in turns):
        raise ValueError("The transcript needs the other person's words, not only your own team's.")
    return turns


def _quote_in_turn(quote: str, turn_text: str) -> bool:
    """True if the words are in this turn and aren't flipped by a negation just before them."""
    turn = f" {_norm_quote(turn_text)} "
    for m in re.finditer(re.escape(f" {_norm_quote(quote)} "), turn):
        if not NEGATIONS & set(turn[:m.start()].split()[-4:]):
            return True
    return False


def check_call_claim(claim: dict, evidence_by_id: dict[str, dict]) -> tuple[bool, str]:
    cited = [evidence_by_id[i] for i in claim.get("evidence_ids") or []]
    calls = [e for e in cited if e.get("kind") == CALL_KIND]
    if not calls:
        return True, ""
    if any(e["content"].get("injection_suspected") for e in calls):
        return False, "Cites a call flagged as trying to manipulate the AI."
    text = claim.get("text", "")
    quotes = [q for q in QUOTE_PATTERN.findall(text) if len(q.split()) >= 3]
    if not quotes:
        return False, "A call can only be quoted word for word. It shows what was said, not what happened."
    outside = QUOTE_PATTERN.sub(" ", text).lower()
    if len(calls) == len(cited) and CALL_ASSERTION.search(outside):
        return False, "States an event as fact from a call. Only the quote stands, and only as what was said."
    wanted = "buyer" if re.search(r"\b(buyer|customer)\b", outside) else "carrier" if re.search(r"\b(carrier|driver|courier)\b", outside) else None
    turns = [t for e in calls for t in e["content"]["turns"] if t["speaker"] != "staff" and (not wanted or t["speaker"] == wanted)]
    for quote in quotes:
        if not any(_quote_in_turn(quote, t["text"]) for t in turns):
            return False, f"\"{quote}\" isn't what {('the ' + wanted) if wanted else 'the other party'} said on the call."
    return True, ""


def call_only(claim: dict, evidence_by_id: dict[str, dict]) -> bool:
    ids = claim.get("evidence_ids") or []
    return bool(ids) and all(evidence_by_id.get(i, {}).get("kind") == CALL_KIND for i in ids)


def call_quote_ok(quote: str, turn_text: str) -> bool:
    return len(quote.split()) >= 3 and _quote_in_turn(quote, turn_text)


def find_reference(text: str, refs: list[tuple[str, str]]) -> str | None:
    """Which case reference does this recording mention? Speech-to-text may write DD-1003 as "DD 1003",
    so separators are optional, but DD-1003 must never match DD-10035: a wrong match attaches a stranger's call."""
    for label, value in refs:
        chars = re.sub(r"[^a-z0-9]", "", (value or "").lower())
        if len(chars) >= 5 and re.search(r"(?<![a-z0-9])" + r"[\s\-_.]*".join(chars) + r"(?![a-z0-9])", text.lower()):
            return label
    return None


# What a buyer would have to say to undercut their own claim. A flag for the seller: it decides nothing.
CONTRADICTING_TOPICS = {NOT_RECEIVED_REASON: ("receipt", "delivery"), NOT_AS_DESCRIBED_REASON: ("item_condition",), UNAUTHORISED_REASON: ("purchase",)}


def call_contradicts_claim(reason: str, finding: dict) -> bool:
    return (finding["speaker"] == "buyer" and finding["stance"] == "helps"
            and finding["topic"] in CONTRADICTING_TOPICS.get(reason, ()))


def call_party(transcript: str) -> str:
    return "carrier" if re.search(r"(?im)^\s*(carrier|driver|courier)\s*:", transcript) else "buyer"


def call_next_steps(missing: list[str], findings: list[dict], needs_check: bool = False) -> list[str]:
    """What the seller should do next, from what the calls said and what is still missing. Plain rules."""
    steps = ["Check the transcript and who said what. Quotes join your reply only after you confirm."] if needs_check else []
    if any(f["contradicts"] for f in findings):
        steps.append("Ask the buyer to confirm this in writing in the PayPal case. Their written words carry more weight than a transcript.")
    if missing and any(f["speaker"] == "carrier" for f in findings):
        steps.append("A driver's word isn't carrier proof. Ask the carrier for its delivery record, then add it to the case.")
    if any(f["stance"] == "hurts" for f in findings):
        steps.append("Read the statements that may hurt your case before you approve. They're left out of your reply, so make sure the reply is still fair and true.")
    return steps


# ---------------------------------------------------------------------------
# Buyer risk: some buyers dispute almost every order ("friendly fraud").
# Computed by code from shop records, so it works even if the AI skips the lookup.
# ---------------------------------------------------------------------------

def buyer_risk(history: dict | None) -> dict:
    h = history or {}
    orders, disputes = int(h.get("previous_orders", 0)), int(h.get("previous_disputes", 0))
    if disputes >= 2 and disputes * 2 >= orders:
        level = "high"
        why = f"Buyer has disputed {disputes} of {orders} previous orders. A settlement would reward the pattern."
    elif disputes >= 1:
        level = "medium"
        why = f"Buyer has disputed {disputes} of {orders} previous orders."
    else:
        level = "low"
        why = f"No previous disputes ({orders} previous orders)." if orders else "New buyer, no history."
    return {"level": level, "why": why, "previous_orders": orders, "previous_disputes": disputes}


# ---------------------------------------------------------------------------
# Decide: fight, settle, or accept. Pure rules, explained in plain words.
# ---------------------------------------------------------------------------

def recommend(dispute: dict, required_met: list[str], required_missing: list[str],
              supported_claims: int, settle_max_usd: float, max_offer_pct: int,
              risk: str = "low") -> dict:
    cents, currency = dispute_amount(dispute)
    actions = allowed_actions(dispute)
    strong = supported_claims >= 2 and not required_missing

    if strong:
        rule, decision = "Enough verified claims and nothing PayPal expects is missing", "fight"
        why = "Strong case: the key evidence PayPal asks for is present and verified."
        offer = None
    elif risk == "high" and required_met:
        rule, decision = "Serial disputer, some evidence: never offer a settlement", "fight"
        why = "Some evidence is missing, but this buyer disputes most orders, so don't pay them off. Answer with what you have."
        offer = None
    elif risk == "high":
        rule, decision = "Serial disputer, little evidence: never offer a settlement", "review"
        why = "Little supporting evidence, and this buyer disputes most orders. Review manually; no settlement suggested."
        offer = None
    elif cents <= to_cents(f"{settle_max_usd:.2f}") and "make_offer" in actions:
        rule, decision = "Weak case, within your settlement limit, and PayPal allows offers", "settle"
        pct = max_offer_pct if not required_met else max(max_offer_pct // 2, 10)
        offer = cents * pct // 100
        why = (f"Weak or incomplete case for a small amount ({from_cents(cents)} {currency}). "
               f"A {pct}% partial refund likely costs less than losing the full amount.")
    elif not required_met:
        rule, decision = "Nothing PayPal expects was found", "review"
        why = "Little supporting evidence. Review manually before deciding; do not auto-accept."
        offer = None
    else:
        rule, decision = "Some evidence found, but too large to settle or settling not allowed", "fight"
        why = "Some key evidence is missing, but the case is still worth answering."
        offer = None

    return {
        "decision": decision,
        "why": why,
        "offer_amount": from_cents(offer) if offer is not None else None,
        "currency": currency,
        "disputed_amount": from_cents(cents),
        "required_met": required_met,
        "required_missing": required_missing,
        "receipt": {"rule": rule, "verified_claims": supported_claims, "expected_found": len(required_met),
                    "expected_missing": len(required_missing), "disputed": from_cents(cents), "currency": currency,
                    "settle_limit": f"{settle_max_usd:.2f}", "max_offer_pct": max_offer_pct, "buyer_risk": risk,
                    "paypal_allows_offer": "make_offer" in actions},
    }


# ---------------------------------------------------------------------------
# Seller Protection pre-flight: a checklist from PayPal's published US terms, run before the seller
# spends effort. PayPal decides eligibility at its sole discretion, so this never says "protected".
# ---------------------------------------------------------------------------

NOT_RECEIVED, NOT_AS_DESCRIBED, UNAUTHORISED = ("MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                                                "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", "UNAUTHORISED")


def protection_preflight(dispute: dict, evidence: list[dict]) -> dict:
    reason = dispute.get("reason")
    txn = (dispute.get("disputed_transactions") or [{}])[0]
    issuer = dispute.get("dispute_channel") == "EXTERNAL"
    checks: list[dict] = []

    def check(label, ok, why):
        checks.append({"label": label, "state": "unknown" if ok is None else "pass" if ok else "fail", "why": why})

    if reason == NOT_AS_DESCRIBED:
        check("Claim type is covered", False, "Seller Protection excludes Significantly Not as Described claims.")
    elif reason in (NOT_RECEIVED, UNAUTHORISED):
        check("Claim type is covered", True, "Item Not Received and Unauthorized claims can be covered.")
    else:
        check("Claim type is covered", None, "This reason isn't one the checklist knows. Check PayPal's terms.")
    if reason == NOT_RECEIVED:
        check("Filed with PayPal, not the card issuer", not issuer,
              "Item Not Received claims filed directly with the card issuer are excluded." if issuer
              else "PayPal's own channel, so the issuer exclusion doesn't apply.")
    flag = txn.get("seller_protection_eligible")
    check("PayPal marks the payment eligible", flag,
          "Not stated on this dispute." if flag is None else f"PayPal's record says {'eligible' if flag else 'not eligible'}.")
    if reason in (NOT_RECEIVED, UNAUTHORISED):
        track = [e["content"] for e in evidence if e["kind"] == "tracking"]
        check("Carrier shows delivered", any(c.get("carrier_status") == "DELIVERED" for c in track),
              "Terms ask for carrier proof with a delivered status.")
        check("Delivered to the transaction address",
              any(e["kind"] == "address_check" and e["content"].get("match") for e in evidence),
              "The recipient address must match Transaction Details. A later redirect voids eligibility.")
    states = {c["state"] for c in checks}
    fatal = any(c["state"] == "fail" and c["label"] in ("Claim type is covered", "Filed with PayPal, not the card issuer", "PayPal marks the payment eligible") for c in checks)
    verdict = "excluded" if fatal else "missing_proof" if "fail" in states else "check_terms" if "unknown" in states else "may_qualify"
    summary = {"excluded": "Seller Protection is unlikely to apply. Fight on evidence alone, or weigh a settlement.",
               "missing_proof": "Seller Protection may apply, but proof is missing. Fix the failed check first.",
               "check_terms": "Not enough information to tell. Check PayPal's terms.",
               "may_qualify": "This case meets the published checklist. PayPal still decides."}[verdict]
    return {"verdict": verdict, "summary": summary, "checks": checks}


# ---------------------------------------------------------------------------
# What each choice costs, in cents, from PayPal's published US fees. No win rate is assumed: nobody
# publishes one for PayPal sellers, so we show the win chance at which fighting beats settling.
# Goods, shipping and ratio effects are left out, and High Volume sellers pay a different fee.
# ---------------------------------------------------------------------------

STANDARD_DISPUTE_FEE, CHARGEBACK_FEE = 1500, 2000


def money_outcomes(dispute: dict, protection_verdict: str, offer_cents: int | None) -> dict:
    cents, currency = dispute_amount(dispute)
    stage = dispute.get("dispute_life_cycle_stage")
    if dispute.get("dispute_channel") == "EXTERNAL":
        lose_fee, win_fee, basis = CHARGEBACK_FEE, CHARGEBACK_FEE, "Card chargeback fee applies whether or not the buyer wins."
    elif stage == "INQUIRY":
        lose_fee, win_fee, basis = 0, 0, "No fee on an inquiry that isn't escalated."
    elif dispute.get("reason") == UNAUTHORISED:
        lose_fee, win_fee, basis = 0, 0, "No standard fee on buyer-filed Unauthorized claims."
    elif cents < 2 * STANDARD_DISPUTE_FEE:
        lose_fee, win_fee, basis = 0, 0, "No standard fee on claims under $30."
    elif protection_verdict == "may_qualify":
        lose_fee, win_fee, basis = STANDARD_DISPUTE_FEE, 0, "$15 fee if you lose. None if you win or the case meets Seller Protection."
    else:
        lose_fee, win_fee, basis = STANDARD_DISPUTE_FEE, 0, "$15 fee if you lose. None if you win."
    lose, win = cents + lose_fee, win_fee
    out = {"win": from_cents(win), "lose": from_cents(lose), "accept": from_cents(cents), "currency": currency, "basis": basis,
           "offer": None, "breakeven_pct": None}
    if offer_cents is not None and lose > win:
        out["offer"] = from_cents(offer_cents)
        # Fighting costs p*win + (1-p)*lose. Below this win chance, the offer is the cheaper choice.
        out["breakeven_pct"] = max(0, min(100, round((lose - offer_cents) * 100 / (lose - win))))
    return out


# ---------------------------------------------------------------------------
# Evidence pack integrity: a fingerprint per item, and dates that cannot be true
# ---------------------------------------------------------------------------

def evidence_manifest(evidence: list[dict]) -> dict:
    rows = []
    for e in evidence:
        body = json.dumps({"id": e["id"], "source": e["source"], "summary": e["summary"], "content": e["content"]},
                          sort_keys=True, default=str)
        rows.append({"id": e["id"], "source": e["source"], "sha256": hashlib.sha256(body.encode()).hexdigest()})
    bundle = hashlib.sha256("".join(r["sha256"] for r in rows).encode()).hexdigest()
    return {"items": rows, "bundle": bundle}


def impossible_dates(evidence: list[dict], now: datetime | None = None) -> list[str]:
    """Problems a reviewer would spot at once, so the seller sees them first."""
    today, problems = (now or datetime.now(timezone.utc)).date().isoformat(), []
    for e in evidence:
        c = e["content"]
        if e["kind"] != "tracking":
            continue
        shipped, delivered = c.get("shipped_on"), c.get("delivered_on")
        if shipped and delivered and delivered < shipped:
            problems.append(f"{e['id']}: delivered on {delivered}, before it shipped on {shipped}.")
        for label, value in (("shipped", shipped), ("delivered", delivered)):
            if value and value > today:
                problems.append(f"{e['id']}: {label} date {value} is in the future.")
    return problems


# ---------------------------------------------------------------------------
# Outcome: PayPal's final decision, for the won/lost record
# ---------------------------------------------------------------------------

def dispute_outcome(dispute: dict) -> str | None:
    code = (dispute.get("dispute_outcome") or {}).get("outcome_code")
    if dispute.get("status") != "RESOLVED" and not code:
        return None
    if code in ("RESOLVED_SELLER_FAVOUR", "CANCELED_BY_BUYER", "DENIED"):
        return "won"
    if code in ("RESOLVED_BUYER_FAVOUR", "ACCEPTED"):
        return "lost"
    return "closed"


def next_step(dispute: dict, analysis: dict | None) -> dict:
    """One plain instruction per case, so a seller always knows what to do next."""
    outcome = dispute_outcome(dispute)
    if outcome:
        label = {"won": "Closed: you won", "lost": "Closed: buyer won"}.get(outcome, "Closed")
        return {"state": "done", "label": label}
    if seller_submissions(dispute):
        return {"state": "waiting", "label": "Filed. Waiting for PayPal"}
    if not analysis:
        return {"state": "needs", "label": "Run the AI team"}
    if allowed_actions(dispute):
        return {"state": "needs", "label": "Review and approve"}
    return {"state": "waiting", "label": "Case ready. PayPal hasn't opened replies"}


def build_supporting_notes(reply_text: str, evidence: list[dict], limit: int = 2000) -> str:
    """Reply + a legend, so a PayPal reviewer can tell what [E3] means. Buyer-written items are left
    out unless the reply cites them, and anything flagged as AI manipulation is never included."""
    lines = [reply_text.strip(), "", "Evidence on file:"]
    for e in evidence:
        cited = f"{e['id']}]" in reply_text or f"{e['id']}," in reply_text
        if (e["trusted"] or cited) and not e["content"].get("injection_suspected"):
            by_buyer = "" if e["trusted"] or e.get("kind") == CALL_KIND else " (written by the buyer)"
            lines.append(f"[{e['id']}] {e['source']}{by_buyer}: {e['summary']}")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def evidence_payload_options(dispute: dict, evidence: list[dict], reply_text: str) -> list[list[dict]]:
    """Payloads to try in order, each one true for this order. Sandbox can list a type as requested
    and still answer EVIDENCE_TYPE_IS_NOT_ALLOWED, so one approval may need a second truthful label."""
    options = [build_evidence_payload(dispute, evidence, reply_text)]
    notes = reply_text[:2000]
    if any(e["kind"] == "tracking" and e["content"].get("signature") for e in evidence):
        options.append([{"evidence_type": "PROOF_OF_DELIVERY_SIGNATURE", "notes": notes}])
    if options[0][0]["evidence_type"] != "OTHER":
        options.append([{"evidence_type": "OTHER", "notes": notes}])
    return options


def build_evidence_payload(dispute: dict, evidence: list[dict], reply_text: str) -> list[dict]:
    """Turn our verified evidence into PayPal's provide-evidence format."""
    payload = []
    for item in evidence:
        c = item["content"]
        if item["kind"] == "tracking" and c.get("tracking_number"):
            payload.append({
                "evidence_type": "PROOF_OF_FULFILLMENT",
                "evidence_info": {"tracking_info": [{"carrier_name": c.get("carrier", "OTHER"),
                                                     "tracking_number": c["tracking_number"]}]},
                "notes": reply_text[:2000],
            })
            break
    if not payload:
        payload.append({"evidence_type": "OTHER", "notes": reply_text[:2000]})
    return payload
