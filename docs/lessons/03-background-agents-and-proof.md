# Lesson 3: Background agents, and why proof beats polish

This lesson covers what changed when DisputeDesk went from "click and wait" to agents that work by themselves.

## 1. A background agent is a queue and a worker (`app/service.py`)

You drop a call on the page. The web request answers at once, and the work happens elsewhere.

**DevOps analogy:** an SQS queue with one worker. The web request is the producer, and `_start_call_worker` is the consumer. We use one worker because Gnani handles one request per account at a time. That's like setting a consumer's concurrency to 1 to respect a rate limit.

The queue lives in memory, so a restart loses it. `fail_interrupted_calls` marks those calls as failed, so nothing disappears silently. A production system would use a durable queue.

## 2. Shared state needs a lock (`app/db.py`)

We found a real bug by running the tests many times: about 1 run in 9 failed. Web requests and the worker share one SQLite connection. Writes took a lock, but reads didn't, so a read could collide with a background job and come back empty.

**DevOps analogy:** a flaky pipeline is often a race condition telling you something true. Don't just re-run it. Loop it until it fails, read the failure, and fix the cause.

After every read took the lock too, 100 runs out of 100 passed.

## 3. Speaker diarization, and why the seller still confirms

Speech-to-text turns audio into words. **Diarization** splits those words by speaker, but it only says "Speaker 1", not who that is. So `label_speakers` asks the AI to work out the names from the conversation.

That's a guess, and a wrong guess could put the seller's words in the buyer's mouth. So quotes stay out of the reply until the seller confirms.

**DevOps analogy:** the AI opens a pull request, and the seller is the required reviewer.

## 4. Treat AI output and typed input as untrusted, then check with code (`app/core.py`)

Three checks run with no AI at all:

- `check_call_claim` keeps a quote only if it's word for word from the right person, and a "never" just before it wasn't dropped.
- `sniff_file_type` reads a file's first bytes, because a file name can say anything.
- `add_documents` runs a typed delivery address through the same match as shop records, so a wrong address doesn't close the gap.

**DevOps analogy:** admission control. It doesn't matter who wrote the manifest, because the same policy checks every one.

## 5. Evaluate after you change a prompt

Changing a prompt is like changing code with no compiler. The only way to know what it did is to run your evaluation again: `scripts/eval.py` and `scripts/attack_eval.py`. Use the real model, because the offline brain is scripted and can't catch a real model's behaviour.

That happened here: the offline brain tagged the buyer's words as `receipt`, so the contradiction flag worked. The real model tagged them `delivery`, and the flag didn't fire. Only the real-model run showed it.
