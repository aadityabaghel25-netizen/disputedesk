"""
All settings in one place, read from environment variables (or the .env file).

DevOps analogy: this is like a ConfigMap. Code never hard-codes secrets or URLs.
"""

import os
from dataclasses import dataclass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env(path: str | None = None) -> None:
    """Read KEY=VALUE lines from .env into os.environ (existing env vars win)."""
    path = path or os.path.join(PROJECT_ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    use_certifi_if_needed()


def use_certifi_if_needed() -> None:
    """python.org Python on macOS ships without trusted certificates, so every HTTPS call
    fails with CERTIFICATE_VERIFY_FAILED. If certifi is installed, point Python at its bundle."""
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi
    except ImportError:
        return
    os.environ["SSL_CERT_FILE"] = certifi.where()


@dataclass
class Settings:
    # PayPal
    paypal_client_id: str
    paypal_client_secret: str
    paypal_base_url: str
    paypal_webhook_id: str
    # AI model (any OpenAI-compatible API) or "fake" for offline demo mode
    llm_provider: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    # Storage
    database_path: str
    store_path: str
    # Seller rules (used by plain code, never by the AI)
    settle_max_usd: float       # disputes at or below this amount may get a settlement suggestion
    max_offer_pct: int          # never offer more than this % of the disputed amount
    # Protect the dashboard when it is on the public internet
    admin_user: str
    admin_password: str
    # Webhooks without a valid signature are rejected unless this is on (local testing only)
    allow_unverified_webhooks: bool
    # Re-check open disputes with PayPal every N minutes (0 = off). Catches changes even without webhooks.
    watch_minutes: int = 5
    # Protects the AI key on a public demo. 0 = no limit.
    ai_runs_per_hour: int = 30
    agent_turns_per_hour: int = 200   # dashboard agent; one question can take several turns
    agent_model: str = ""             # dashboard agent's model; empty = agent switched off


def get_settings() -> Settings:
    load_env()
    e = os.environ.get
    llm_key = e("LLM_API_KEY", "")
    return Settings(
        paypal_client_id=e("PAYPAL_CLIENT_ID", ""),
        paypal_client_secret=e("PAYPAL_CLIENT_SECRET", ""),
        paypal_base_url=e("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com"),
        paypal_webhook_id=e("PAYPAL_WEBHOOK_ID", ""),
        # If no AI key is set we fall back to the offline "fake" brain so the app still runs.
        llm_provider=e("LLM_PROVIDER", "openai_compatible" if llm_key else "fake"),
        llm_base_url=e("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        llm_api_key=llm_key,
        llm_model=e("LLM_MODEL", ""),
        database_path=e("DATABASE_PATH", os.path.join(PROJECT_ROOT, "data", "disputedesk.db")),
        store_path=e("STORE_PATH", os.path.join(PROJECT_ROOT, "data", "store.json")),
        settle_max_usd=float(e("SETTLE_MAX_USD", "25")),
        max_offer_pct=int(e("MAX_OFFER_PCT", "50")),
        admin_user=e("ADMIN_USER", "seller"),
        admin_password=e("ADMIN_PASSWORD", ""),
        allow_unverified_webhooks=e("ALLOW_UNVERIFIED_WEBHOOKS", "false").lower() == "true",
        watch_minutes=int(e("WATCH_MINUTES", "5")),
        ai_runs_per_hour=int(e("AI_RUNS_PER_HOUR", "30")),
        agent_turns_per_hour=int(e("AGENT_TURNS_PER_HOUR", "200")),
        agent_model=e("AGENT_MODEL", ""),
    )
