"""
Day 1 sandbox test ("spike") for DisputeDesk.

Goal: prove, before writing any AI code, that we can
  1. log in to PayPal Sandbox with our app credentials,
  2. create a real test payment,
  3. see a dispute the test buyer opens,
  4. reply to that dispute (message, evidence, offer).

Run every command from the project folder, for example:
    python3 spike/day1_spike.py auth

Each command saves PayPal's full answer in spike/results/ so you can send
it to Claude if something looks wrong. (Secrets are never saved.)
"""

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paypal_client import PayPalError, PayPalSandbox, load_env  # noqa: E402

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")

HELP = """
DisputeDesk - Day 1 sandbox test

  auth                         Check your Client ID/Secret work
  create-order [amount]        Create a $ test order (default 40.00) and print the approve link
  capture <order_id>           Take the money after the buyer approved in the browser
  disputes                     List disputes against your seller account
  dispute <dispute_id>         Show one dispute in detail
  message <dispute_id> "text"  Send a message to the buyer
  require-evidence <id>        (sandbox only) Move dispute to 'seller evidence needed'
  evidence <dispute_id>        Submit sample tracking evidence
  offer <dispute_id> <amount>  Offer a partial refund

Typical order: auth -> create-order -> (approve in browser) -> capture
               -> (buyer reports a problem) -> disputes -> dispute <id>
               -> message -> require-evidence -> evidence
"""


def save(name: str, data) -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    path = os.path.join(RESULTS_DIR, f"{stamp}_{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return path


def ok(msg: str) -> None:
    print(f"\n[OK] {msg}")


def find_link(obj: dict, rel: str) -> str | None:
    for link in obj.get("links", []):
        if link.get("rel") == rel:
            return link.get("href")
    return None


def main() -> int:
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_env(os.path.join(project_root, ".env"))

    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0

    cmd, rest = args[0], args[1:]

    try:
        pp = PayPalSandbox(os.environ.get("PAYPAL_CLIENT_ID", ""), os.environ.get("PAYPAL_CLIENT_SECRET", ""))

        if cmd == "auth":
            token = pp.access_token()
            ok(f"Logged in to PayPal Sandbox. Got an access token ({len(token)} characters).")
            print("Your credentials work. Next: python3 spike/day1_spike.py create-order")

        elif cmd == "create-order":
            amount = rest[0] if rest else "40.00"
            order = pp.create_order(amount, "Handmade leather bag (DisputeDesk test)")
            path = save("create_order", order)
            ok(f"Order created: {order['id']}  (status: {order['status']})")
            print("\nNow open this link in your browser and log in as your SANDBOX PERSONAL (buyer) account:")
            print(f"\n   {find_link(order, 'payer-action') or find_link(order, 'approve')}\n")
            print(f"After approving, run:  python3 spike/day1_spike.py capture {order['id']}")
            print(f"(saved to {path})")

        elif cmd == "capture":
            order_id = rest[0]
            result = pp.capture_order(order_id)
            path = save("capture", result)
            capture = result["purchase_units"][0]["payments"]["captures"][0]
            ok(f"Payment captured: ${capture['amount']['value']} - capture id {capture['id']}")
            print("\nNow log in to https://www.sandbox.paypal.com as the BUYER, open this payment,")
            print("and choose 'Report a problem' -> 'I didn't receive it'. Then wait a few minutes and run:")
            print("   python3 spike/day1_spike.py disputes")
            print(f"(saved to {path})")

        elif cmd == "disputes":
            result = pp.list_disputes()
            path = save("disputes", result)
            items = result.get("items", [])
            ok(f"Found {len(items)} dispute(s).")
            for d in items:
                amt = d.get("dispute_amount", {})
                print(f" - {d.get('dispute_id')} | {d.get('reason')} | {d.get('status')} | "
                      f"{amt.get('value')} {amt.get('currency_code')} | stage: {d.get('dispute_life_cycle_stage')}")
            if not items:
                print("No disputes yet. Disputes can take a few minutes to appear after the buyer reports a problem.")
            print(f"(saved to {path})")

        elif cmd == "dispute":
            d = pp.get_dispute(rest[0])
            path = save("dispute", d)
            ok(f"Dispute {d.get('dispute_id')}")
            print(f"   reason:   {d.get('reason')}")
            print(f"   status:   {d.get('status')}")
            print(f"   stage:    {d.get('dispute_life_cycle_stage')}")
            print(f"   amount:   {d.get('dispute_amount', {}).get('value')} {d.get('dispute_amount', {}).get('currency_code')}")
            print(f"   reply by: {d.get('seller_response_due_date', 'n/a')}")
            actions = sorted({link.get('rel') for link in d.get('links', [])} - {'self'})
            print(f"   actions PayPal allows right now: {', '.join(actions) or 'none listed'}")
            print(f"(full details saved to {path})")

        elif cmd == "message":
            dispute_id, text = rest[0], " ".join(rest[1:]) or "Hello, your parcel was delivered. Tracking attached soon."
            result = pp.send_message(dispute_id, text)
            path = save("message", result)
            ok("Message sent to the buyer.")
            print(f"(saved to {path})")

        elif cmd == "require-evidence":
            result = pp.sandbox_require_evidence(rest[0])
            path = save("require_evidence", result)
            ok("Dispute moved to 'seller evidence needed' (sandbox simulation).")
            print(f"Now try:  python3 spike/day1_spike.py evidence {rest[0]}")
            print(f"(saved to {path})")

        elif cmd == "evidence":
            evidences = [{
                "evidence_type": "PROOF_OF_FULFILLMENT",
                "evidence_info": {"tracking_info": [{"carrier_name": "FEDEX", "tracking_number": "122533485"}]},
                "notes": "Order shipped on time and delivered to the buyer's address. Tracking attached.",
            }]
            result = pp.provide_evidence(rest[0], evidences)
            path = save("evidence", result)
            ok("Evidence submitted! This is the big Day 1 milestone.")
            print(f"(saved to {path})")

        elif cmd == "offer":
            dispute_id, amount = rest[0], rest[1]
            result = pp.make_offer(dispute_id, amount, "Sorry for the trouble. We'd like to offer a partial refund.")
            path = save("offer", result)
            ok(f"Offered a ${amount} refund to settle the dispute.")
            print(f"(saved to {path})")

        else:
            print(f"Unknown command: {cmd}")
            print(HELP)
            return 1

    except PayPalError as e:
        path = save(f"ERROR_{cmd}", {"status": e.status, "body": e.body})
        print(f"\n[FAILED] PayPal said no (HTTP {e.status}).")
        print(json.dumps(e.body, indent=2) if isinstance(e.body, (dict, list)) else e.body)
        print(f"\nSaved to {path} - send this file to Claude and we'll fix it together.")
        return 1
    except (IndexError, KeyError) as e:
        print(f"\n[FAILED] Missing argument or unexpected response ({e!r}). Run with 'help' to see usage.")
        return 1
    except ValueError as e:
        print(f"\n[FAILED] {e}")
        return 1
    except OSError as e:
        print(f"\n[FAILED] Could not reach PayPal ({e}). Check your internet connection or VPN/proxy.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
