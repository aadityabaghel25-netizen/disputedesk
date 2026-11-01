# CLAUDE.md: instructions for any AI coding assistant working on DisputeDesk

Read this file first, then `MEMORY.md` (project facts, decisions log, status) and
`docs/NEXT_STEPS.md` (prioritised task list). Update `MEMORY.md` → "Status" and
"Decisions log" at the end of every session.

## Who you are working with
- **Ankit Solanki**: Senior DevOps engineer (AWS, Terraform, K8s, Python), ~11 yrs in banking/fintech.
  **New to building AI apps.** He asked to be taught from the basics while building.
- Explain each new AI concept in plain words with a DevOps analogy before using it.
- He is on **macOS**, Python from **Anaconda** (`python3`), browser **Chrome**, shell **zsh**.
- Give terminal commands **one per code block, one line each**, no trailing `\`.
  He copy-pastes; multi-line pastes have caused stuck `>` prompts before.
- When something fails, ask for a screenshot of the Terminal or page.
- Never ask him to paste secrets into chat. Secrets live only in `.env` (gitignored).

## Goal
Win the **PayPal AI Hackathon** (Devpost). Deadline **Nov 12, 2026, 12:00pm PT = Fri Nov 13, 1:30am IST**.
He wants to move **as fast as possible**; ignore the original day-by-day timeline.
Targets: Grand Prize / HM "Best Use of PayPal + AI"; sponsor tools in use: Bryntum, AG Studio, Render (at most one sponsor prize can be won).
Submission needs: working build (hosted URL or run steps), public repo + license, AI tools list,
YouTube demo < 3 min.

## What DisputeDesk is
An AI legal team for small PayPal sellers: Prevent (auto-add tracking on payment), Defend
(agents build a cited evidence case; verifier strikes unproven claims), Settle (code-computed
partial refund). Seller approves every PayPal write.

## Golden rules (never break)
1. AI reads/writes; **code decides money** (integer cents, caps, deadlines).
2. **No PayPal write without a human click.** Never auto-accept a dispute.
3. Every claim cites evidence ids; final reply is assembled by code from verified claims only.
4. Buyer text is untrusted data (prompt-injection flagged and ignored). AI gets read-only tools only.
5. Keep the offline demo mode (no keys) working: it is the never-fail demo fallback.
6. Keep all tests passing: `python3 -m unittest discover -s tests -v`.

## Commands
```bash
pip install -r requirements.txt                 # once
uvicorn app.main:app --reload                   # run app -> http://127.0.0.1:8000
python3 -m unittest discover -s tests -v        # offline tests
python3 scripts/eval.py                         # 20-case evaluation (uses .env model; --fake for offline)
python3 spike/day1_spike.py help                # raw PayPal sandbox checks
python3 spike/test_llm.py models                # list models for the configured AI key
python3 scripts/setup.py order DD-1004          # sandbox order -> approve as buyer -> ship
python3 scripts/setup.py ship <ORDER_ID> DD-1004
python3 scripts/setup.py webhook https://<public-url>   # register webhook, prints PAYPAL_WEBHOOK_ID
```
If port 8000 is busy: `lsof -ti:8000 | xargs kill`.

## Architecture (files)
- `app/config.py` settings from `.env` (no PayPal keys → demo mode; no AI key → FakeLLM)
- `app/paypal.py` REST client (stdlib urllib). Read calls + human-only writes + webhook verify
- `app/demo.py` offline PayPal stand-in (`data/demo_disputes.json`)
- `app/evidence.py` Collector, `{paid+N}` date resolver, per-reason requirement checks, mock Store, seller-added proof
- `app/core.py` deterministic rules: cents, deadlines, allowed actions (from dispute `links`),
  policy lookup (`policy/dispute_rules.md`), injection flags, claim fact-check, fight/settle/review
- `app/llm.py` OpenAI-compatible client, `ask_json` (pydantic structured output), tool loop, FakeLLM
- `app/agents.py` the agent team (Collector, Onboarding for call recordings, Triage, Investigator, Rule checker, Drafter, Verifier, Advisor, Settle writer)
- `app/service.py` glue: sync, load-by-id, webhooks, Prevent, analyze, approve, evidence pack
- `app/main.py` Starlette routes + Basic auth; `app/templates/*` server-rendered pages; `app/static/agents.js` live agent layer (rail, drop anywhere), polls `/agents/live`
- `scripts/` setup + eval; `tests/test_app.py`; `docs/lessons/` teaching notes; `docs/PROMPTS.md` agent prompts
- Stack choices: Starlette (not FastAPI) and SQLite (not Postgres) for simplicity; Render for hosting.

## Known PayPal sandbox facts (learned the hard way)
- Use **US** sandbox accounts (USD). Ankit's PayPal login is Indian; sandbox seller/buyer are US.
- Sandbox buyer address: 2211 N First St, San Jose, CA 95131 (store.json matches).
- `GET /v1/customer/disputes` (list) returned 0 while `GET /v1/customer/disputes/{id}` worked → app has Load-by-ID.
- Dispute has `disputed_transactions[0].invoice_number` → links to shop order.
- New dispute: status OPEN, links only `self`; send-message and sandbox require-evidence →
  `ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE`. Paying with the Visa card made it a CHARGEBACK
  (status UNDER_REVIEW). Bank-funded disputes also reach that stage after about 10 minutes; then `require-evidence` works and evidence can be filed (see README).
- Orders created by our script show "Continue to Review Order" (no "Pay now"); our `ship` command captures.
- Guest card checkout = no buyer login = no dispute. Always "Log In" as the sandbox buyer.
