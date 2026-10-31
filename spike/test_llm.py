"""
Your first AI calls - using NVIDIA's free API (Nemotron models).

NVIDIA's API speaks the same "language" as OpenAI's API (the /chat/completions
format), so later we can switch to Gemini or Claude by changing .env only.

Commands (run from the project folder):
    python3 spike/test_llm.py models   # list Nemotron models your key can use
    python3 spike/test_llm.py chat     # ask the AI a simple question
    python3 spike/test_llm.py tools    # LESSON: watch the AI ask to call a tool

Needs in .env:
    LLM_BASE_URL=https://integrate.api.nvidia.com/v1
    LLM_API_KEY=nvapi-...your key...
    LLM_MODEL=<a model id from the 'models' command>
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paypal_client import load_env  # noqa: E402


def call(method: str, path: str, body: dict | None = None) -> dict:
    base = os.environ.get("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
    key = os.environ.get("LLM_API_KEY", "")
    if not key:
        raise SystemExit("[FAILED] LLM_API_KEY is empty. Put your NVIDIA key in .env")
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"[FAILED] HTTP {e.code}: {e.read().decode()[:1000]}") from None


def model() -> str:
    m = os.environ.get("LLM_MODEL", "")
    if not m:
        raise SystemExit("[FAILED] LLM_MODEL is empty. Run 'models' first and copy one id into .env")
    return m


# A FAKE tool, just for the lesson. Later the real tools call PayPal.
FAKE_DISPUTE = {
    "dispute_id": "PP-D-1001",
    "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
    "amount": "40.00 USD",
    "buyer_message": "I never got my bag!",
    "tracking": {"carrier": "FEDEX", "number": "122533485", "status": "DELIVERED", "delivered_on": "2026-10-20"},
}

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_dispute",
        "description": "Get the details of a PayPal dispute, including tracking info.",
        "parameters": {
            "type": "object",
            "properties": {"dispute_id": {"type": "string", "description": "The PayPal dispute id"}},
            "required": ["dispute_id"],
        },
    },
}]


def run_tools_lesson() -> None:
    messages = [
        {"role": "system", "content": "You help small online sellers answer PayPal disputes. "
                                      "Use tools to get facts. Never invent facts."},
        {"role": "user", "content": "A buyer opened dispute PP-D-1001. Did they really not receive the item? "
                                    "Answer in 2 sentences and mention the evidence."},
    ]
    print("STEP 1 - We send the question + the list of tools the AI is allowed to use.")
    r1 = call("POST", "/chat/completions", {"model": model(), "messages": messages, "tools": TOOLS,
                                            "tool_choice": "auto", "temperature": 0.2, "max_tokens": 400})
    msg = r1["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    if not calls:
        print("\nThe model answered without using the tool (this model may not support tool calling well):")
        print(msg.get("content"))
        return

    print("\nSTEP 2 - The AI did NOT answer. It asked US to run a tool:")
    for c in calls:
        print(f"   -> {c['function']['name']}({c['function']['arguments']})")

    print("\nSTEP 3 - OUR code runs the tool (the AI never touches PayPal itself) and sends back the result.")
    messages.append(msg)
    for c in calls:
        messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(FAKE_DISPUTE)})

    r2 = call("POST", "/chat/completions", {"model": model(), "messages": messages, "tools": TOOLS,
                                            "temperature": 0.2, "max_tokens": 400})
    print("\nSTEP 4 - Now the AI answers, using the facts our tool returned:\n")
    print(r2["choices"][0]["message"].get("content"))
    print("\nThat loop (ask -> tool -> result -> answer) IS an 'agent'. DisputeDesk is several of these.")


def main() -> int:
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_env(os.path.join(project_root, ".env"))
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "models":
        data = call("GET", "/models").get("data", [])
        names = sorted(m["id"] for m in data if "nemotron" in m["id"].lower())
        print(f"[OK] {len(data)} models available. Nemotron ones:")
        for n in names:
            print("   ", n)
        print("\nPick one (bigger 'super'/'ultra' models are smarter, 'nano' is faster) and set LLM_MODEL in .env")
    elif cmd == "chat":
        r = call("POST", "/chat/completions", {
            "model": model(), "temperature": 0.2, "max_tokens": 200,
            "messages": [{"role": "user", "content": "In 2 sentences: what is a PayPal dispute?"}]})
        print("[OK] The AI says:\n")
        print(r["choices"][0]["message"].get("content"))
    elif cmd == "tools":
        run_tools_lesson()
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
