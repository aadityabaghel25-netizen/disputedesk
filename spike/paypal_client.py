"""
A tiny PayPal Sandbox client that uses ONLY Python's standard library.

Why no 'requests' or SDK? So you can run it right away with plain `python3`,
with nothing to install. Later, the real app will use proper libraries.

Every function here is one PayPal REST API call. Read the docstrings:
they explain what each call does in plain English.
"""

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"


# ---------------------------------------------------------------------------
# Config: read secrets from a .env file (never hard-code secrets in code!)
# ---------------------------------------------------------------------------

def load_env(path: str = ".env") -> None:
    """Read KEY=VALUE lines from a .env file into environment variables.

    Lines starting with # are comments. Existing environment variables win,
    so you can also set them in your terminal instead.
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
    # python.org Python on macOS has no trusted certificates; use certifi's bundle if installed.
    if not os.environ.get("SSL_CERT_FILE"):
        try:
            import certifi
            os.environ["SSL_CERT_FILE"] = certifi.where()
        except ImportError:
            pass


class PayPalError(Exception):
    """Raised when PayPal answers with an error. Carries status + body."""

    def __init__(self, status: int, body):
        self.status = status
        self.body = body
        super().__init__(f"PayPal returned HTTP {status}: {json.dumps(body, indent=2) if isinstance(body, (dict, list)) else body}")


# ---------------------------------------------------------------------------
# Multipart helper (needed only for uploading dispute evidence)
# ---------------------------------------------------------------------------

def build_multipart(fields: dict, files: list | None = None) -> tuple[bytes, str]:
    """Build a multipart/form-data body by hand.

    fields: {"input": {...json...}}  -> sent as application/json parts
    files:  [("evidence_file", "receipt.pdf", b"...bytes...", "application/pdf")]
    Returns (body_bytes, content_type_header).
    """
    boundary = "----DisputeDesk" + uuid.uuid4().hex
    lines: list[bytes] = []
    for name, value in fields.items():
        lines.append(f"--{boundary}".encode())
        lines.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        lines.append(b"Content-Type: application/json")
        lines.append(b"")
        lines.append(json.dumps(value).encode())
    for field_name, filename, content, mime in files or []:
        lines.append(f"--{boundary}".encode())
        lines.append(f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"'.encode())
        lines.append(f"Content-Type: {mime}".encode())
        lines.append(b"")
        lines.append(content)
    lines.append(f"--{boundary}--".encode())
    lines.append(b"")
    return b"\r\n".join(lines), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

class PayPalSandbox:
    def __init__(self, client_id: str, client_secret: str, base_url: str = SANDBOX_BASE):
        if not client_id or not client_secret:
            raise ValueError("Missing PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET. Did you fill in your .env file?")
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url
        self._token: str | None = None

    # -- low level ----------------------------------------------------------

    def _request(self, method: str, path: str, *, json_body=None, raw_body: bytes | None = None,
                 content_type: str | None = None, auth_basic: bool = False, form: dict | None = None):
        url = self.base_url + path
        headers = {"Accept": "application/json"}
        data = None

        if auth_basic:
            creds = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            headers["Authorization"] = f"Basic {creds}"
        else:
            headers["Authorization"] = f"Bearer {self.access_token()}"

        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif raw_body is not None:
            data = raw_body
            headers["Content-Type"] = content_type or "application/octet-stream"

        if method == "POST":
            # PayPal-Request-Id makes POSTs safe to retry (idempotency).
            headers["PayPal-Request-Id"] = str(uuid.uuid4())

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                text = resp.read().decode() or "{}"
                return json.loads(text)
        except urllib.error.HTTPError as e:
            text = e.read().decode()
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                body = text
            raise PayPalError(e.code, body) from None

    # -- auth ---------------------------------------------------------------

    def access_token(self) -> str:
        """Swap Client ID + Secret for a short-lived access token (like a day pass)."""
        if self._token is None:
            result = self._request("POST", "/v1/oauth2/token", form={"grant_type": "client_credentials"},
                                   auth_basic=True)
            self._token = result["access_token"]
        return self._token

    # -- orders (a customer buying something) -------------------------------

    def create_order(self, amount_usd: str, description: str) -> dict:
        """Create an order. The buyer must then approve it in the browser."""
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "description": description,
                "amount": {"currency_code": "USD", "value": amount_usd},
            }],
        }
        return self._request("POST", "/v2/checkout/orders", json_body=body)

    def capture_order(self, order_id: str) -> dict:
        """After the buyer approves, 'capture' actually moves the money."""
        return self._request("POST", f"/v2/checkout/orders/{order_id}/capture", json_body={})

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/v2/checkout/orders/{order_id}")

    # -- disputes (a customer complaining) ----------------------------------

    def list_disputes(self) -> dict:
        """List all disputes filed against this seller."""
        return self._request("GET", "/v1/customer/disputes")

    def get_dispute(self, dispute_id: str) -> dict:
        """Full details of one dispute: reason, amount, status, deadline, allowed actions."""
        return self._request("GET", f"/v1/customer/disputes/{dispute_id}")

    def send_message(self, dispute_id: str, message: str) -> dict:
        """Send a message to the buyer inside the dispute."""
        return self._request("POST", f"/v1/customer/disputes/{dispute_id}/send-message",
                             json_body={"message": message})

    def make_offer(self, dispute_id: str, amount_usd: str, note: str) -> dict:
        """Offer the buyer a (partial) refund to settle the dispute."""
        body = {"note": note, "offer_type": "REFUND",
                "offer_amount": {"currency_code": "USD", "value": amount_usd}}
        return self._request("POST", f"/v1/customer/disputes/{dispute_id}/make-offer", json_body=body)

    def provide_evidence(self, dispute_id: str, evidences: list, files: list | None = None) -> dict:
        """Submit evidence (e.g. tracking number + notes). Uses multipart upload."""
        body, ctype = build_multipart({"input": {"evidences": evidences}}, files)
        return self._request("POST", f"/v1/customer/disputes/{dispute_id}/provide-evidence",
                             raw_body=body, content_type=ctype)

    # -- sandbox-only simulation helpers ------------------------------------

    def sandbox_require_evidence(self, dispute_id: str, action: str = "SELLER_EVIDENCE") -> dict:
        """SANDBOX ONLY: push a dispute into the 'waiting for seller evidence' stage.

        In real life PayPal does this itself. In the sandbox we can trigger it
        so we can test evidence submission without waiting.
        """
        return self._request("POST", f"/v1/customer/disputes/{dispute_id}/require-evidence",
                             json_body={"action": action})
