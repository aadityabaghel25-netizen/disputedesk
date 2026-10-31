"""
One-time setup helpers for the real PayPal sandbox.

  python3 scripts/setup.py webhook https://YOUR-APP.onrender.com
      Registers our webhook URL with PayPal and prints the WEBHOOK_ID to put in .env / Render.

  python3 scripts/setup.py order DD-1001
      Creates a sandbox order linked to shop order DD-1001 (data/store.json) and prints the approve link.

  python3 scripts/setup.py ship <ORDER_ID> DD-1001
      Captures the approved order and adds the shop's tracking number to it in PayPal.
      Then log in as the sandbox buyer and "Report a problem" to create the dispute.

  python3 scripts/setup.py evidence-stage PP-R-XXX
      Sandbox only. Once a dispute has PayPal's require_evidence link (about 10 minutes after filing),
      moves it to "waiting for seller response" so evidence can be filed from the app.

  python3 scripts/setup.py decide PP-R-XXX seller|buyer
      Sandbox only. Plays PayPal's reviewer and closes the case, to show the ending of the flow.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import get_settings  # noqa: E402
from app.paypal import PayPal, PayPalError  # noqa: E402

EVENTS = ["CUSTOMER.DISPUTE.CREATED", "CUSTOMER.DISPUTE.UPDATED", "CUSTOMER.DISPUTE.RESOLVED",
          "PAYMENT.CAPTURE.COMPLETED"]


def main() -> int:
    s = get_settings()
    pp = PayPal(s.paypal_client_id, s.paypal_client_secret, s.paypal_base_url)
    if not pp.configured:
        print("Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in .env first.")
        return 1
    with open(s.store_path, encoding="utf-8") as f:
        store = json.load(f)["orders"]
    args = sys.argv[1:]
    try:
        if args[:1] == ["webhook"]:
            url = args[1].rstrip("/") + "/webhooks/paypal"
            hook = pp.create_webhook(url, EVENTS)
            print(f"[OK] Webhook registered for {url}\n\nAdd this to .env and to Render:\nPAYPAL_WEBHOOK_ID={hook['id']}")
        elif args[:1] == ["order"]:
            shop = store[args[1]]
            order = pp.create_order(shop["price"], shop["item"], args[1])
            link = next(l["href"] for l in order["links"] if l["rel"] in ("payer-action", "approve"))
            print(f"[OK] Order {order['id']} for {shop['item']} (${shop['price']}).\n"
                  f"Approve it as the sandbox BUYER:\n  {link}\n"
                  f"Then run:  python3 scripts/setup.py ship {order['id']} {args[1]}")
        elif args[:1] == ["ship"]:
            order_id, invoice = args[1], args[2]
            cap = pp.capture_order(order_id)["purchase_units"][0]["payments"]["captures"][0]
            print(f"[OK] Captured {cap['id']}")
            t = store[invoice]["shipment"]
            if t.get("tracking_number"):
                pp.add_tracking(order_id, cap["id"], t["tracking_number"], t["carrier"])
                print(f"[OK] Added {t['carrier']} tracking {t['tracking_number']} in PayPal")
            print("Now log in at https://www.sandbox.paypal.com as the buyer -> this payment -> Report a problem.")
        elif args[:1] == ["evidence-stage"]:
            pp.sandbox_require_evidence(args[1])
            print(f"[OK] Requested. Check it: python3 spike/day1_spike.py dispute {args[1]}")
        elif args[:1] == ["decide"] and args[2:3] in (["seller"], ["buyer"]):
            pp.sandbox_adjudicate(args[1], seller_wins=args[2] == "seller")
            print(f"[OK] Sandbox decision sent: {args[2]} wins. The app shows it after the next PayPal check or Sync.")
        else:
            print(__doc__)
    except PayPalError as e:
        print(f"[FAILED] {e}")
        return 1
    except (IndexError, KeyError) as e:
        print(f"[FAILED] Missing or unknown argument ({e}).\n{__doc__}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
