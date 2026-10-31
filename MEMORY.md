# DisputeDesk: project memory

> Context file for any AI assistant (Claude, Claude Code, Cursor, etc.) or human picking up this
> project. Read this first. Update the "Status" and "Decisions log" sections after every session.

## 1. Who and what

- **Builder:** Ankit Solanki. Solo. Senior DevOps Engineer (Bengaluru), ~11 yrs in banking/fintech
  and e-commerce. Strong: AWS, Terraform, Kubernetes, Docker, Python. **New to building AI apps**,
  so explain every new AI concept in plain language, with DevOps analogies, before using it.
- **Project:** DisputeDesk, "an AI legal team for small PayPal sellers".
- **Goal:** win the **PayPal AI Hackathon** on Devpost.

## 2. The hackathon (hard facts)

| Item | Value |
|---|---|
| Link | https://paypalaihackathon.devpost.com |
| Deadline | **Nov 12, 2026, 12:00pm PT = Fri Nov 13, 1:30am IST**. Target submit: **Wed Nov 11** |
| Rule | Must meaningfully use **PayPal (sandbox OK) + AI**. Any AI model allowed |
| Submission | Working build (hosted URL or clear run steps), **public repo with open-source license**, list of AI tools used, **YouTube demo < 3 min** |
| Judging | 5 equal criteria: Technical implementation, Design, Impact, Innovation, Presentation. May include automated AI review, so README quality matters |
| Prizes | 1st $12k / 2nd $8k / 3rd $5k; HMs $5k each: Most Creative, Most Impactful, Best Demo Delivery, Best Use of PayPal+AI, Best Use of Agentic Commerce. **Sponsor:** AG Grid $5k/$2k/$1k x3; Channel3 $1.5k; Bryntum $1k x3; APIMatic $1k x3 + subscription; Render credits $1k/$750/$500 |
| Stacking | One Grand **or** one HM, **plus** one sponsor prize |
| Judges run it | Test access must stay free and working until judging ends **Dec 15, 2026**; include any login in the testing instructions |
| Our targets | Grand Prize / HM "Best Use of PayPal + AI"; sponsor: **Bryntum** (deadline board), Render as secondary |
| India | Eligible. Use **US sandbox accounts in USD** |
| Out of scope | Nebius x NVIDIA hackathon: **decided not to enter** (too little time) |

## 3. The product

**One line:** DisputeDesk prevents disputes, fights them with real evidence, and settles fairly
when that's smarter. The seller always approves before anything reaches PayPal.

**Three modes (build in this order):**
1. **Defend (core):** dispute webhook → agents gather order, tracking, transactions and chats →
   check PayPal rules for that reason (RAG) → draft reply where every claim cites evidence →
   **verifier agent strikes unsupported claims** → seller approves → evidence filed via REST.
2. **Settle:** propose a partial refund; amount computed by **code** within seller-set limits.
3. **Prevent:** auto-add shipment tracking on fulfilment; answer early complaints with proof.

**Nice-to-have (cut first if late):** photo evidence via vision, serial-disputer flag.

## 4. Golden rules (non-negotiable)

1. **AI reads and writes; code decides money.** Amounts, deadlines and limits are computed in code, in integer cents.
2. **Nothing moves money without a human click.** Never auto-accept a dispute.
3. **Every claim needs an evidence id.** The verifier rejects anything that doesn't resolve.
4. **Buyer text is untrusted.** Demo includes a planted prompt-injection that gets ignored.
5. **Agents return schema-validated JSON**, e.g. `{claim, evidence_ids[], confidence}`.
6. **Secrets only in `.env`** (gitignored). Never paste keys in chat or code.

## 5. Tech stack

| Layer | Choice |
|---|---|
| Language | **Python** (Ankit knows it). Note: PayPal Agent Toolkit's Python package lacks `create_refund`/`get_refund`; call REST directly for those |
| PayPal | Agent Toolkit tools: `list_disputes`, `get_dispute`, `accept_dispute_claim` (gated), `get_order(_details)`, `get_shipment_tracking`, `create_shipment_tracking`, `list_transactions`. **Direct REST:** `provide-evidence` (multipart), `send-message`, `make-offer`, webhooks + `verify-webhook-signature`, refunds |
| AI model | Any **OpenAI-compatible** API, switched via `.env` (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`). Default: **NVIDIA Nemotron** free key (`https://integrate.api.nvidia.com/v1`). Backups: Gemini free tier; ~$5-10 Anthropic credit for final demo |
| Server | **Starlette + Uvicorn** (FastAPI's foundation; simpler, fewer deps) |
| DB | **SQLite** (one file, zero setup) with hash-chained audit log; all SQL in `app/db.py` so Postgres is a later swap. Policy retrieval = exact lookup by reason code in `policy/dispute_rules.md` |
| Hosting | **Render** (public HTTPS needed for webhooks; sponsor credit) |
| UI | Simple web page + **Bryntum Scheduler** deadline board |

## 6. Sandbox gotchas

- Disputes must be created **by hand**: buyer logs in at sandbox.paypal.com → Activity → "Report a problem". Use buyer-approved orders (card-only orders have no buyer login).
- Sandbox-only `require-evidence` call moves a dispute to "seller evidence needed".
- Simulator webhooks are **unsigned**; use real sandbox transactions for signed events (~10-75s delay).
- Webhook URL must be public HTTPS (no localhost); max 10 webhooks per app.
- Payouts needs dashboard approval (we don't depend on it). Venmo and Pay Later are not demo-safe.
- Sandbox tokens expire after a few hours; refresh automatically.

## 7. Repo layout (current)

```
disputedesk/
├── MEMORY.md, README.md, LICENSE (MIT), .gitignore, .env.example, requirements.txt, render.yaml
├── app/
│   ├── config.py     settings from env/.env (no keys -> demo mode + fake AI)
│   ├── db.py         SQLite: events, disputes, evidence, analyses, hash-chained audit
│   ├── paypal.py     REST client (read calls + human-only write calls + webhook verify)
│   ├── demo.py       DemoPayPal: offline stand-in using data/demo_disputes.json
│   ├── evidence.py   Collector + per-reason requirement checks; Store (mock shop)
│   ├── core.py       deterministic rules: cents, deadlines, allowed actions, policy lookup,
│   │                 injection flags, claim fact-check, fight/settle/review advice
│   ├── llm.py        OpenAI-compatible client, ask_json (structured output), tool loop, FakeLLM
│   ├── agents.py     the 8-step agent team
│   ├── service.py    glue: sync, webhook handling, analyze, approve (only PayPal writes)
│   ├── main.py       Starlette routes + Basic auth
│   └── templates/    base, index (dashboard), dispute (review + approve), audit
├── data/store.json, data/demo_disputes.json   mock shop + 4 demo disputes (fight/fight/settle/injection)
├── policy/dispute_rules.md                    evidence rules per dispute reason
├── scripts/setup.py                           webhook register, sandbox order, ship+track
├── tests/test_app.py                          17 offline tests (all passing)
├── scripts/eval.py                            20-case evaluation harness -> docs/eval_results.md
├── spike/                                     Day 1 scripts (sandbox test, first LLM call)
└── docs/: SETUP_DAY1, NEXT_STEPS (task list), PROMPTS (agent prompts), eval_results, lessons/01-02, research/

CLAUDE.md = instructions for any AI coding assistant (read first).
```

Research report with 18 ideas and sources: `reports/PayPal AI hackathon project ideas.md` (outside repo).

## 8. 19-day plan

| Days | Dates | Goal | Exit criterion |
|---|---|---|---|
| 1-2 | Oct 24-25 | Sandbox spike | Evidence submission works on ≥1 sandbox dispute |
| 3-5 | Oct 26-28 | FastAPI on Render, webhook + signature verify, Postgres, audit log, mock store data | Real dispute lands in DB within ~75s |
| 6-9 | Oct 29-Nov 1 | Agent pipeline: triage, evidence curator, policy RAG, drafter, verifier + deterministic core | Draft for every seeded dispute, zero uncited claims |
| 10-12 | Nov 2-4 | Approve flow, REST evidence submit, gated refund/accept, tracking hook, injection defense | One-click approve files evidence |
| 13-14 | Nov 5-6 | UI + Bryntum deadline board | Stranger understands it in 30s |
| 15-16 | Nov 7-8 | Eval harness on 20 cases (completeness, citation precision, time saved) | Numbers for impact slide |
| 17 | Nov 9 | README, architecture diagram, AI-tools list | Fresh clone runs from README |
| 18 | Nov 10 | Demo video ≤ 2:50 | Final cut |
| 19 | Nov 11 | **Submit** | Confirmed |

Fallback if evidence submission fails in sandbox: use `send-message` + `make-offer` + downloadable evidence pack.

## 9. Teaching plan (lessons to write as we go)

1. ✅ The big picture: API, sandbox, LLM, agent, tools, RAG, guardrails
2. Tool calling hands-on (`test_llm.py tools`)
3. Webhooks + signature verification
4. Structured output + validation (Pydantic)
5. RAG over PayPal dispute rules
6. Multi-agent pipeline + verifier
7. Human-in-the-loop + guardrails
8. Evals: measuring AI quality
9. Demo and pitch craft

## 10. Status (update every session)

- **Last updated:** 2026-10-31. Onboarding agent for call recordings built and tested on the real model with made-up calls. Gnani batch speech-to-text tested live with a made-up two-voice call: key is in local `.env`, not yet on Render. Gnani's firewall needs a User-Agent header; files list is under `data`. Not committed yet.
- **Don't run `scripts/eval.py --fake` casually:** it overwrites the real-model `docs/eval_results.md`. Restore with git if it happens.
- **Oct 30:** first local Claude Code session.
- **Oct 30 local session:** real AI connected. Key is **Nebius** (Token Factory, OpenAI-compatible), model
  `nvidia/nemotron-3-super-120b-a12b` (reasoning model: hidden thinking tokens count toward max_tokens, so default
  raised 1200 -> 4000). Tool calling works. Real-model eval: 20/20, 0 unproven claims, 4/4 injections, ~20 s/case.
  python.org Python 3.13 on this Mac had no CA certs (CERTIFICATE_VERIFY_FAILED); `load_env` now sets SSL_CERT_FILE
  to certifi's bundle. PayPal sandbox keys in local `.env`, auth OK. Order DD-1004 `0SP7842600022632G` created,
  waiting for buyer approval (PayPal balance).
  **Dispute #2: PP-R-NEQ-10195226** (DD-1004, order 0SP7842600022632G, capture 60047431MK491672H, UPS 1Z999AA10123456784).
  Paid from sandbox buyer's BANK (CREDIT UNION 1). Buyer had no PayPal balance; avoid "PayPal Credit"/"Pay Later" (asks SSN).
  Result: dispute_channel INTERNAL, stage INQUIRY (not a chargeback). Status UNDER_REVIEW, links only `self`,
  require-evidence -> ACTION_NOT_ALLOWED. Real AI team on it: injection flagged, FIGHT, 4 cited claims, 0 struck.
  PayPal resolution-center date field rejects future dates.
  ~10 min later (update_time 00:20Z) sandbox moved it to stage **CHARGEBACK** (channel still INTERNAL), links still only `self`.
  Both disputes ended UNDER_REVIEW/CHARGEBACK regardless of funding source -> looks like sandbox auto-escalation, not the card.
  **BREAKTHROUGH ~00:27-00:33Z:** after escalation both disputes gained link `provide_supporting_info`; dispute #1 also got
  `require_evidence` (sandbox). Calling `require-evidence` {action: SELLER_EVIDENCE} on PP-R-NKX-10195211 moved it to
  **WAITING_FOR_SELLER_RESPONSE** with links `provide_evidence` + `accept_claim`, real due date 2026-11-02T04:59:59Z.
  Recipe: file dispute -> wait ~10 min for CHARGEBACK stage -> require-evidence becomes allowed -> call it.
  PayPal DROPS dispute `messages` after escalation -> db.upsert_dispute now merges old messages (keeps injection proof).
- **provide-evidence on NKX -> 400 EVIDENCE_TYPE_IS_NOT_ALLOWED** for PROOF_OF_FULFILLMENT, even though PayPal lists it as
  REQUESTED_FROM_SELLER (cause unknown). One click now tries FULFILLMENT -> DELIVERY_SIGNATURE -> OTHER (truthful only). Full
  PayPal errors go to audit as `paypal_rejected`. Not yet proven accepted. Buyer's opening words also survive in `evidences` (type CREATE).
- **Evidence ACCEPTED (07:29Z) on PP-R-NKX-10195211:** FULFILLMENT refused, PROOF_OF_DELIVERY_SIGNATURE accepted; PayPal stored it as
  type OTHER, SUBMITTED_BY_SELLER, status -> UNDER_REVIEW. Full path proven in sandbox: dispute -> AI case -> human click -> PayPal.
- **Oct 30 additions:** attack suite with controls (tests/injection_fixtures.py, scripts/attack_eval.py), buyer words from `evidences` CREATE,
  requested-evidence list, filed-with-PayPal receipt, code quote check, notes legend.
  Word filter alone: 22/22 tuned, 0/12 held out, do NOT tune on HELD_OUT. Added AI `manipulation_attempt` flag (warning only). Real model:
  34/34 attacks changed nothing, 34/34 flagged, 1/22 false alarm. Possible next: attach PDF evidence pack.
- **Oct 30 CTO pass:** research report in reports/ (gitignored). Built: Seller Protection pre-flight, fee-aware costs + breakeven, decision receipt, /proof page (try-to-break + attack results + tool policy), .ics export, urgent banner, evidence hashes and date checks, won/lost tracking, setup.py evidence-stage/decide, Bryntum board (handlers, blocks, unplanned alert, autoplan, watcher moves blocks; plain timeline fallback), demo auto-seed, demo dates stay current, cross-site POST block, AI run limit. README rewritten for judges. Early review: submit by **Nov 5**. Not done: GitHub, deploy, `decide` on a real dispute, video, AG Studio, timed baseline.
- **AG Studio (Oct 30):** /insights works from the CDN with theming and a custom ratio tile (plain JS class widget). Field formats must be valid (`integerFormat`, `currencyFormat`, `textFormat`) or widgets show "No data". Studio Agent Framework WORKS with `AGENT_MODEL=zai-org/GLM-5.3` on Nebius and the default harness (about 100s per question). Nemotron looped; Kimi K2.6 returned broken tool arguments; gpt-oss-120b has no Responses API on Nebius. Proxy adds item ids Nebius requires. Custom single-agent harness sent no requests (not debugged). Sandbox error test: ERRDIS050 returned MISSING_TRACKING_INFO; ERRDIS039 timed out. Submission text and video script: reports/Submission kit.md (gitignored).
- **UI redesign (Oct 30, 2nd pass):** sidebar app shell with attention count, dashboard Next step column and filters, dispute page progress tracker + sticky decision panel + confirm dialog + double-submit guard, details collapsed, plain language (Seller Protection check, Activity), readable activity log. Checked desktop, phone and dark mode.
- **Oct 30 improvements (Ankit chose all):** buyer risk flag (code: >=2 disputes and >=50% of orders = high -> never
  settle), dashboard stats cards, auto-watcher thread (WATCH_MINUTES, logs status_changed/replies_opened, re-analyses),
  /board deadline timeline (estimated 10-day deadline when PayPal sends none, striped), /rules page (DB table `rules`,
  validated, audited), provide-supporting-info action ("Approve & send case to PayPal"), full professional UI redesign
  (Inter font, two-column dispute page, sticky decision panel, mobile OK). 26 tests pass.
- **Added Oct 30:** Prevent mode (PAYMENT.CAPTURE.COMPLETED webhook -> add tracking), evidence pack page
  (/disputes/{id}/pack, print/PDF/.txt), load-by-ID, not-ready banner, eval harness (20/20 with fake brain),
  CLAUDE.md + docs/NEXT_STEPS.md + docs/PROMPTS.md for local handoff. Ankit asked to go as fast as possible.
- **Done:** Days 3-9 code built and tested offline: web server, webhook receiver with signature check,
  SQLite + audit log, evidence collector, 8-step agent team, verifier, settle advice, approve flow,
  dashboard (desktop + mobile checked), Render config, 13 passing tests, Lesson 2.
  Demo mode works with zero keys. Pipeline also tested over HTTP against a mock OpenAI-style server.
- **Real sandbox progress (Oct 30):** PayPal dev account + US sandbox seller/buyer + app created; `auth` OK;
  order DD-1001 created, approved by buyer, captured (39S96924L1285760W), FedEx tracking added; buyer filed
  dispute **PP-R-NKX-10195211** (NOT_RECEIVED, INQUIRY, status OPEN). Sandbox buyer address = 2211 N First St,
  San Jose CA 95131 (store.json updated; shop dates now `{paid+N}` relative to payment date).
  Findings: `list_disputes` returned 0 while `get_dispute` works (list lags -> app has Load-by-ID + sync refreshes
  known ids); dispute has `invoice_number` (links to shop); links only `self` while OPEN; send-message and
  require-evidence -> 422/400 ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE. Wait for status
  WAITING_FOR_SELLER_RESPONSE and retry. 03:36 IST: app loaded the real dispute end-to-end (FIGHT, address
  match, real order + tracking). Status became UNDER_REVIEW / stage CHARGEBACK (buyer paid with Visa via PayPal);
  require-evidence still ACTION_NOT_ALLOWED. Next tries: (a) check seller's sandbox Resolution Center UI,
  (b) new order paid with PayPal BALANCE (not card), dispute it, wait for WAITING_FOR_SELLER_RESPONSE,
  (c) ask hackathon Discord. Fallback already built: message/offer/evidence pack. App shows "PayPal isn't accepting replies yet" banner.
- **Not yet verified against real services:** PayPal sandbox (Claude's workspace can't reach PayPal)
  and a real Nemotron model. Ankit must run these.
- **Next action (Ankit):** (1) run demo mode locally; (2) Day 1 sandbox spike (`docs/SETUP_DAY1.md`);
  (3) `spike/test_llm.py models` + `tools` with NVIDIA key; (4) create GitHub repo + Render deploy.
- **Next action (Claude):** fix whatever breaks against real PayPal/Nemotron; then Days 10-16:
  prevention hook (auto-tracking), Bryntum deadline board, eval harness on 20 cases.
- **Open questions:** Does sandbox `provide-evidence` work? Which Nemotron model id? Does the dispute
  object include `invoice_number` (our link to shop orders) or do we fall back to capture lookup?

## 11. Decisions log

| Date | Decision |
|---|---|
| 2026-10-24 | Chose DisputeDesk over GigShield / MilestoneLock / Haggle (least crowded, deepest PayPal use, banking-domain fit, no Payouts dependency) |
| 2026-10-25 | Merged best features of winners into DisputeDesk (Prevent / Defend / Settle) |
| 2026-10-25 | Python, not TypeScript (Ankit's comfort); REST for gaps in Python toolkit |
| 2026-10-31 | Onboarding agent added for call recordings (Gnani batch speech-to-text). Code selects a recording only when it mentions one open case; AI picks quotes and says helps/hurts; code keeps exact quotes from the other person only. A call shows what was said: consent required, seller reviews it, it never meets a PayPal requirement and doesn't change fight/settle. No voice approval or live phone line |
| 2026-10-25 | AI provider kept swappable; start on NVIDIA Nemotron free key |
| 2026-10-25 | **Not entering the Nebius x NVIDIA hackathon**. PayPal only |
| 2026-10-29 | Starlette instead of FastAPI, SQLite instead of Postgres (simpler, testable offline); offline demo mode + FakeLLM as never-fail fallback |
| 2026-10-29 | AI gets read-only tools only; final reply assembled by code from verified claims |
| 2026-10-30 | "Least crowded" (2026-10-24 decision) is now false: many dispute entries exist. Win by proving every sentence on a real case, not by being a dispute agent |
| 2026-10-30 | No hours-saved or win-rate claims until measured; removed the estimated Time saved tile |
| 2026-10-30 | Sponsor order: Bryntum (built) > AG Studio (time-box Nov 6-8, drop if unstable) > Render deploy; APIMatic only if genuinely used; skip Channel3. A project wins at most 1 sponsor prize but is considered for each tool used |
| 2026-10-31 | Razorpay AI Buildathon: students only, with an in-person internship as the prize, so not entered unless Ankit qualifies |
| 2026-10-30 | Render Starter + 1 GB disk, paid from the $50 hackathon credit (Ankit's decision). Cancel after winners are announced, about Dec 21 |
| 2026-10-30 | Hosted judge demo = demo PayPal + real AI, rate-limited; real sandbox shown in video and README |
| 2026-10-30 | AI provider = Nebius Token Factory hosting Nemotron 3 Super (the key Ankit has); still swappable via .env |
| 2026-10-30 | Buyer risk + deadlines + settlement limits stay in code; AI never decides them. Estimated deadline = create_time + 10 days, labelled est. |
| 2026-10-30 | Ignore day-by-day timeline; move as fast as possible. Work moves to Ankit's local machine (Claude Code); CLAUDE.md is the handoff |
