"""
PayPal REST client used by the app (standard library only).

Read-only calls are safe for AI agents to use as tools.
Calls that change something (marked WRITE) are only ever triggered by a human
clicking Approve in the dashboard - never by the AI.
"""

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class PayPalError(Exception):
    def __init__(self, status: int, body):
        self.status, self.body = status, body
        super().__init__(f"PayPal HTTP {status}: {body}")

    def has_issue(self, issue: str) -> bool:
        details = self.body.get("details") if isinstance(self.body, dict) else None
        return any(d.get("issue") == issue for d in details or [])

    def summary(self) -> str:
        """Plain text for the seller. PayPal puts the real reason in `details`, after long boilerplate."""
        if not isinstance(self.body, dict):
            return f"HTTP {self.status}: {str(self.body)[:300]}"
        reasons = [" ".join(str(d[k]) for k in ("field", "issue", "description") if d.get(k))
                   for d in self.body.get("details") or []]
        return f"HTTP {self.status} {self.body.get('name', '')}: " + ("; ".join(reasons) or str(self.body.get("message", "")))


def build_multipart(fields: dict, files: list | None = None) -> tuple[bytes, str]:
    """multipart/form-data body (PayPal's provide-evidence endpoint requires this format)."""
    boundary = "----DisputeDesk" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts += [f"--{boundary}".encode(), f'Content-Disposition: form-data; name="{name}"'.encode(),
                  b"Content-Type: application/json", b"", json.dumps(value).encode()]
    for field_name, filename, content, mime in files or []:
        parts += [f"--{boundary}".encode(),
                  f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"'.encode(),
                  f"Content-Type: {mime}".encode(), b"", content]
    parts += [f"--{boundary}--".encode(), b""]
    return b"\r\n".join(parts), f"multipart/form-data; boundary={boundary}"


class PayPal:
    def __init__(self, client_id: str, client_secret: str, base_url: str):
        self.client_id, self.client_secret, self.base_url = client_id, client_secret, base_url.rstrip("/")
        self._token: str | None = None
        self._token_expires = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    # -- plumbing -------------------------------------------------------------

    def _token_value(self) -> str:
        # Tokens expire after a few hours; refresh 5 minutes early.
        if self._token is None or time.time() > self._token_expires - 300:
            creds = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            req = urllib.request.Request(
                self.base_url + "/v1/oauth2/token", data=b"grant_type=client_credentials", method="POST",
                headers={"Authorization": f"Basic {creds}", "Accept": "application/json",
                         "Content-Type": "application/x-www-form-urlencoded"})
            data = self._send(req)
            self._token = data["access_token"]
            self._token_expires = time.time() + int(data.get("expires_in", 3600))
        return self._token

    @staticmethod
    def _send(req: urllib.request.Request):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                text = resp.read().decode()
                return json.loads(text) if text.strip() else {}
        except urllib.error.HTTPError as e:
            text = e.read().decode()
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                body = text
            raise PayPalError(e.code, body) from None

    def request(self, method: str, path: str, *, json_body=None, raw_body: bytes | None = None,
                content_type: str | None = None, query: dict | None = None):
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        headers = {"Authorization": f"Bearer {self._token_value()}", "Accept": "application/json"}
        data = None
        if json_body is not None:
            data, headers["Content-Type"] = json.dumps(json_body).encode(), "application/json"
        elif raw_body is not None:
            data, headers["Content-Type"] = raw_body, content_type or "application/octet-stream"
        if method == "POST":
            headers["PayPal-Request-Id"] = str(uuid.uuid4())   # idempotency: safe retries
        return self._send(urllib.request.Request(url, data=data, headers=headers, method=method))

    # -- READ: orders, payments, transactions ---------------------------------

    def get_order(self, order_id: str) -> dict:
        return self.request("GET", f"/v2/checkout/orders/{order_id}")

    def get_capture(self, capture_id: str) -> dict:
        return self.request("GET", f"/v2/payments/captures/{capture_id}")

    def search_transactions(self, transaction_id: str, start_iso: str, end_iso: str) -> dict:
        return self.request("GET", "/v1/reporting/transactions", query={
            "transaction_id": transaction_id, "start_date": start_iso, "end_date": end_iso, "fields": "all"})

    # -- READ: disputes -------------------------------------------------------

    def list_disputes(self) -> list[dict]:
        return self.request("GET", "/v1/customer/disputes").get("items", [])

    def get_dispute(self, dispute_id: str) -> dict:
        return self.request("GET", f"/v1/customer/disputes/{dispute_id}")

    # -- WRITE: disputes (human-approved only) --------------------------------

    def send_message(self, dispute_id: str, message: str) -> dict:
        return self.request("POST", f"/v1/customer/disputes/{dispute_id}/send-message",
                            json_body={"message": message})

    def provide_evidence(self, dispute_id: str, evidences: list, files: list | None = None) -> dict:
        body, ctype = build_multipart({"input": {"evidences": evidences}}, files)
        return self.request("POST", f"/v1/customer/disputes/{dispute_id}/provide-evidence",
                            raw_body=body, content_type=ctype)

    def provide_supporting_info(self, dispute_id: str, notes: str, files: list | None = None) -> dict:
        """The seller's reply channel once a case is under PayPal review (e.g. CHARGEBACK stage)."""
        path = f"/v1/customer/disputes/{dispute_id}/provide-supporting-info"
        if not files:
            return self.request("POST", path, json_body={"notes": notes})
        body, ctype = build_multipart({"input": {"notes": notes}}, files)
        return self.request("POST", path, raw_body=body, content_type=ctype)

    def make_offer(self, dispute_id: str, amount: str, currency: str, note: str) -> dict:
        return self.request("POST", f"/v1/customer/disputes/{dispute_id}/make-offer", json_body={
            "note": note, "offer_type": "REFUND", "offer_amount": {"currency_code": currency, "value": amount}})

    def accept_claim(self, dispute_id: str, note: str) -> dict:
        return self.request("POST", f"/v1/customer/disputes/{dispute_id}/accept-claim", json_body={"note": note})

    # -- WRITE: setup / sandbox helpers ---------------------------------------

    def create_order(self, amount: str, description: str, invoice_id: str) -> dict:
        return self.request("POST", "/v2/checkout/orders", json_body={
            "intent": "CAPTURE",
            "purchase_units": [{"invoice_id": invoice_id, "custom_id": invoice_id, "description": description,
                                "amount": {"currency_code": "USD", "value": amount}}]})

    def capture_order(self, order_id: str) -> dict:
        return self.request("POST", f"/v2/checkout/orders/{order_id}/capture", json_body={})

    def add_tracking(self, order_id: str, capture_id: str, tracking_number: str, carrier: str) -> dict:
        return self.request("POST", f"/v2/checkout/orders/{order_id}/track", json_body={
            "capture_id": capture_id, "tracking_number": tracking_number, "carrier": carrier,
            "notify_payer": False})

    def sandbox_adjudicate(self, dispute_id: str, seller_wins: bool) -> dict:
        """Sandbox only: play PayPal's reviewer and close the case. Never reachable from the web app."""
        return self.request("POST", f"/v1/customer/disputes/{dispute_id}/adjudicate",
                            json_body={"adjudication_outcome": "SELLER_FAVOR" if seller_wins else "BUYER_FAVOR"})

    def sandbox_require_evidence(self, dispute_id: str) -> dict:
        return self.request("POST", f"/v1/customer/disputes/{dispute_id}/require-evidence",
                            json_body={"action": "SELLER_EVIDENCE"})

    # -- webhooks ---------------------------------------------------------------

    def create_webhook(self, url: str, event_types: list[str]) -> dict:
        return self.request("POST", "/v1/notifications/webhooks", json_body={
            "url": url, "event_types": [{"name": n} for n in event_types]})

    def list_webhooks(self) -> list[dict]:
        return self.request("GET", "/v1/notifications/webhooks").get("webhooks", [])

    def verify_webhook_signature(self, headers: dict, event: dict, webhook_id: str) -> bool:
        """Ask PayPal: 'did YOU really send this?' Stops anyone faking a dispute alert."""
        h = {k.lower(): v for k, v in headers.items()}
        body = {
            "auth_algo": h.get("paypal-auth-algo"),
            "cert_url": h.get("paypal-cert-url"),
            "transmission_id": h.get("paypal-transmission-id"),
            "transmission_sig": h.get("paypal-transmission-sig"),
            "transmission_time": h.get("paypal-transmission-time"),
            "webhook_id": webhook_id,
            "webhook_event": event,
        }
        if not all(body.values()):
            return False
        return self.request("POST", "/v1/notifications/verify-webhook-signature",
                            json_body=body).get("verification_status") == "SUCCESS"
