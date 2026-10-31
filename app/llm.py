"""
The AI "brain" connection.

Two brains with the same interface:
  * OpenAICompatibleLLM - talks to NVIDIA Nemotron, Gemini, Claude-compatible
    gateways, OpenAI, etc. Switch with LLM_BASE_URL / LLM_API_KEY / LLM_MODEL.
  * FakeLLM - an offline, rule-based stand-in. No key, no internet, same
    answers every time. Used for tests and as a never-fails demo fallback.

Lesson: an LLM call is just an HTTP POST with a list of messages.
  messages = [{"role": "system", "content": "you are..."},   # the job description
              {"role": "user",   "content": "the task..."}]  # the work item
The model replies with one more message.
"""

import json
import re
import time
import urllib.error
import urllib.request
import uuid

from pydantic import BaseModel, ValidationError


class LLMError(Exception):
    pass


class OpenAICompatibleLLM:
    name = "openai_compatible"

    def __init__(self, base_url: str, api_key: str, model: str):
        if not model:
            raise LLMError("LLM_MODEL is empty. Run: python3 spike/test_llm.py models")
        self.base_url, self.api_key, self.model = base_url.rstrip("/"), api_key, model

    def chat(self, messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.1,
             max_tokens: int = 4000) -> dict:
        # Reasoning models (e.g. Nemotron Super) "think" first, and those hidden tokens count
        # against max_tokens. 1200 cut long drafts off mid-JSON, so leave plenty of room.
        body = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if tools:
            body["tools"], body["tool_choice"] = tools, "auto"
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    return json.loads(resp.read().decode())["choices"][0]["message"]
            except urllib.error.HTTPError as e:
                # 429 = rate limited, 5xx = provider hiccup: wait and retry (2s, 4s, 8s)
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise LLMError(f"LLM HTTP {e.code}: {e.read().decode()[:500]}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < 3:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise LLMError(f"Could not reach the LLM: {e}") from None
        raise LLMError("LLM retries exhausted")


GNANI_JOBS = "https://api.vachana.ai/stt/v3/batch/jobs"


def _gnani_json(url: str, api_key: str | None, body: bytes | None = None, content_type: str | None = None) -> dict | list:
    headers = {"User-Agent": "DisputeDesk/1.0"}   # Gnani's firewall refuses Python's default name (error 1010)
    if api_key:   # the transcript link is presigned: it gets no key
        headers["X-API-Key-ID"] = api_key
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET", headers=headers)
    for wait in (5, 10, 20, 30, None):   # Gnani sends 429 with no Retry-After when an account is busy
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code != 429 or wait is None:
                raise LLMError(f"Gnani said no (HTTP {e.code}).") from None
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise LLMError(f"Could not reach Gnani: {e}") from None
    raise LLMError("Gnani is busy. Try again in a minute.")


def _speaker_lines(result: dict) -> str:
    """Join Gnani's segments into "Speaker 1: ..." lines. The seller replaces those with who really spoke."""
    speakers, lines = {}, []
    for seg in result.get("segments") or []:
        text = (seg.get("text") or "").strip()
        if not text or seg.get("speaker_id") is None:
            continue
        label = f"Speaker {speakers.setdefault(seg['speaker_id'], len(speakers) + 1)}"
        if lines and lines[-1][0] == label:
            lines[-1][1] += " " + text
        else:
            lines.append([label, text])
    return "\n".join(f"{who}: {text}" for who, text in lines) or (result.get("full_transcript") or "").strip()


GNANI_TOPIC = "A phone call between an online shop, its customer or a delivery driver about an order, its delivery and a PayPal dispute."


def gnani_transcribe(api_key: str, model: str, language: str, audio: bytes, filename: str, keyterms: list[str] | None = None,
                     poll_seconds: int = 10, max_polls: int = 30) -> str:
    """Speech-to-text with Gnani's batch API: create a job, start it, poll, then fetch the transcript.
    Its instant endpoint stops at 30 seconds and can't separate speakers, so it doesn't suit phone calls.
    The seller reviews the result before it is saved, because speech models can mishear or invent words."""
    boundary = uuid.uuid4().hex
    # Phone audio is noisy, and order numbers are easy to mishear: key terms tell the model what to listen for.
    config = {"model": model, "language_code": language, "mode": "transcribe", "with_diarization": True, "num_speakers": 2,
              "with_denoise": True, "topic": GNANI_TOPIC}
    if keyterms:
        config["keyterms"] = keyterms[:200]
    config = json.dumps(config)
    safe_name = re.sub(r"[^\w.-]", "_", filename)[:80] or "call.mp3"
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="config"\r\nContent-Type: application/json\r\n\r\n{config}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{safe_name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n").encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    job = _gnani_json(GNANI_JOBS, api_key, body, f"multipart/form-data; boundary={boundary}").get("job_id")
    if not job:
        raise LLMError("Gnani didn't return a job id.")
    _gnani_json(f"{GNANI_JOBS}/{job}/start", api_key, b"")
    for _ in range(max_polls):
        status = _gnani_json(f"{GNANI_JOBS}/{job}", api_key).get("status")
        if status == "COMPLETED":
            break
        if status in ("FAILED", "PARTIAL_FAILURE", "START_FAILED", "CANCELLED"):
            raise LLMError(f"Gnani couldn't transcribe this call ({status}).")
        time.sleep(poll_seconds)
    else:
        raise LLMError("Gnani is taking too long. Try again, or paste the transcript.")
    files = _gnani_json(f"{GNANI_JOBS}/{job}/files?status=COMPLETED", api_key)
    url = ((files.get("data") or [{}])[0]).get("transcript_url") or ""
    if not url.startswith("https://"):
        raise LLMError("Gnani didn't return a transcript link.")
    text = _speaker_lines(_gnani_json(url, None))
    if not text:
        raise LLMError("Gnani heard no speech in this recording.")
    return text


def extract_json(text: str) -> dict:
    """Models sometimes wrap JSON in ```json fences or add a sentence. Pull out the {...}."""
    if not text:
        raise ValueError("empty response")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)   # some models "think out loud"
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found")
    return json.loads(text[start:end + 1])


def ask_json(llm, system: str, user: str, model_cls: type[BaseModel], retries: int = 1) -> BaseModel:
    """Ask for JSON, validate it against a schema (pydantic). If invalid, show the
    model its error and ask again. This is 'structured output'."""
    schema = json.dumps(model_cls.model_json_schema())
    messages = [{"role": "system", "content": f"{system}\n\nReply with ONLY a JSON object matching this JSON schema:\n{schema}"},
                {"role": "user", "content": user}]
    last_error = None
    for _ in range(retries + 1):
        msg = llm.chat(messages)
        content = msg.get("content") or ""
        try:
            return model_cls.model_validate(extract_json(content))
        except (ValueError, ValidationError) as e:
            last_error = e
            messages += [{"role": "assistant", "content": content},
                         {"role": "user", "content": f"That was not valid. Error: {str(e)[:400]}. "
                                                     "Reply again with ONLY the corrected JSON object."}]
    raise LLMError(f"Model did not return valid JSON: {last_error}")


def run_tool_loop(llm, system: str, user: str, tools: list[dict], handlers: dict, max_steps: int = 4) -> tuple[str, list[dict]]:
    """The agent loop: model asks for a tool -> we run it -> give the result back -> repeat.
    Returns (final_text, trace). Only READ-ONLY tools are ever offered to the model."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    trace = []
    for _ in range(max_steps):
        msg = llm.chat(messages, tools=tools)
        calls = msg.get("tool_calls") or []
        if not calls:
            return msg.get("content") or "", trace
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = handlers[name](**args) if name in handlers else {"error": f"unknown tool {name}"}
            trace.append({"tool": name, "args": args, "result": result})
            messages.append({"role": "tool", "tool_call_id": c.get("id", name), "content": json.dumps(result)})
    return "", trace


# ---------------------------------------------------------------------------
# FakeLLM: deterministic offline brain. It reads the [agent:...] tag in the
# system prompt to know which job it is doing.
# ---------------------------------------------------------------------------

class FakeLLM:
    name = "fake (offline demo brain)"

    def __init__(self):
        self.calls = 0

    def chat(self, messages: list[dict], tools=None, **_) -> dict:
        self.calls += 1
        system = messages[0]["content"]
        user = messages[1]["content"] if len(messages) > 1 else ""
        if "[agent:investigator]" in system:
            if any(m["role"] == "tool" for m in messages):
                return {"role": "assistant", "content": "Checked the buyer's history."}
            return {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "lookup_buyer_history", "arguments": "{}"}}]}
        if "[agent:triage]" in system:
            return self._json({"summary": "The buyer says there is a problem with the order and asks for their money back.",
                               "buyer_claim": "Problem with the order."})
        if "[agent:onboarding]" in system:
            return self._json(self._onboard(user))
        if "[agent:drafter]" in system:
            return self._json(self._draft(user))
        if "[agent:judge]" in system:
            n = len(re.findall(r"^CLAIM \d+:", user, re.M))
            return self._json({"results": [{"index": i, "supported": True, "reason": "Matches the cited evidence."}
                                           for i in range(n)]})
        if "[agent:settle]" in system:
            return self._json({"message": "We're sorry this order didn't go as expected and would like to make it right."})
        return {"role": "assistant", "content": "OK"}

    @staticmethod
    def _json(obj) -> dict:
        return {"role": "assistant", "content": json.dumps(obj)}

    @staticmethod
    def _onboard(user: str) -> dict:
        turns = json.loads(user.split("CALL_JSON:", 1)[1].split("END_CALL", 1)[0])
        for t in reversed(turns):   # the last thing they said, not the "yes" to being recorded
            if t["speaker"] == "staff" or len(t["text"].split()) < 3:
                continue
            words = t["text"].lower()
            hurts = bool(re.search(r"\b(never|not|no)\b", words))
            topic = "delivery" if t["speaker"] == "carrier" else "receipt" if re.search(r"came|come|arrived|received|got", words) else "other"
            stance = "hurts" if hurts else "helps" if topic != "other" else "neutral"
            return {"findings": [{"turn": t["turn"], "quote": t["text"], "topic": topic, "stance": stance}]}
        return {"findings": []}

    @staticmethod
    def _draft(user: str) -> dict:
        evidence = json.loads(user.split("EVIDENCE_JSON:", 1)[1].split("END_EVIDENCE", 1)[0])
        claims = []
        for e in evidence:
            if not e["trusted"]:
                continue
            c = e["content"]
            if e["kind"] == "tracking" and c.get("carrier_status") == "DELIVERED":
                claims.append({"text": f"The order was shipped on {c['shipped_on']} with {c['carrier']} "
                                       f"(tracking {c['tracking_number']}) and delivered on {c['delivered_on']}.",
                               "evidence_ids": [e["id"]]})
                if c.get("signature"):
                    claims.append({"text": f"The delivery was signed for by {c['signature']}.", "evidence_ids": [e["id"]]})
            elif e["kind"] == "address_check" and c.get("match"):
                claims.append({"text": "The delivery address matches the shipping address on the PayPal transaction.",
                               "evidence_ids": [e["id"]]})
            elif e["kind"] == "listing":
                claims.append({"text": f"The listing described the item as: \"{c['description']}\"",
                               "evidence_ids": [e["id"]]})
            elif e["kind"] == "photo":
                claims.append({"text": "We photographed the item before shipping.", "evidence_ids": [e["id"]]})
            elif e["kind"] == "chat" and e["content"].get("from") == "seller":
                pass
        for e in evidence:  # buyer messages acknowledging receipt are cited as quotes, never followed
            if e["kind"] == "chat" and "arrived" in e["content"].get("text", "").lower():
                claims.append({"text": f"On {e['content']['date']} the buyer wrote: \"{e['content']['text']}\"",
                               "evidence_ids": [e["id"]]})
        for e in evidence:  # calls can only be quoted; the over-reach below shows the verifier striking it
            if e["kind"] != "call_transcript" or e["content"]["stance"] != "helps":
                continue
            c, speaker = e["content"], e["content"]["turns"][0]["speaker"]
            claims.append({"text": f"In a call on {c['call_date']}, the {speaker} said: \"{c['text']}\"", "evidence_ids": [e["id"]]})
            if speaker == "carrier":
                claims.append({"text": "The carrier confirmed on the phone that the parcel was delivered.", "evidence_ids": [e["id"]]})
        return {"opening": "Thank you for reviewing this case. Here are the facts, each with supporting evidence.",
                "claims": claims,
                "closing": "Based on this evidence, we respectfully ask PayPal to resolve the case in the seller's favour."}
