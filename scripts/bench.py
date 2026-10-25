"""Measure what memory costs per request: proxy latency with memory on vs. off.

Usage: python scripts/bench.py [--requests 30] [--model mock]
Uses the offline "mock" model by default, so the numbers show the plugin's
overhead without provider latency. Each "on" request uses a new question,
so every one pays a real recall; "follow-up" requests repeat a turn and
should hit the per-turn cache.
"""

from __future__ import annotations

import argparse
import os
import statistics
import time
import uuid

import httpx

QUESTIONS = [
    "How do we authenticate users on the portal?",
    "Which S/4 version does the customer run?",
    "Why does the business partner API return 403?",
    "How should CAP auth be configured for production?",
]


def run(client: httpx.Client, model: str, tags: str, question: str) -> float:
    started = time.perf_counter()
    r = client.post(
        "/v1/chat/completions",
        headers={"x-litellm-tags": tags},
        json={"model": model, "messages": [{"role": "user", "content": question}]},
    )
    r.raise_for_status()
    return (time.perf_counter() - started) * 1000


def summary(name: str, samples: list[float]) -> str:
    samples = sorted(samples)
    p95 = samples[max(0, int(len(samples) * 0.95) - 1)]
    return f"{name:<22} p50 {statistics.median(samples):7.0f} ms   p95 {p95:7.0f} ms   n={len(samples)}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=30)
    parser.add_argument("--model", default="mock")
    parser.add_argument("--tags", default="customer:acme,project:acme-portal")
    args = parser.parse_args()

    client = httpx.Client(
        base_url=os.environ.get("LITELLM_URL", "http://localhost:4000"),
        headers={"Authorization": f"Bearer {os.environ.get('LITELLM_MASTER_KEY', 'sk-local-dev')}"},
        timeout=60,
    )
    run(client, args.model, args.tags, "warm up")  # load models and connections

    off, on, follow = [], [], []
    for i in range(args.requests):
        question = f"{QUESTIONS[i % len(QUESTIONS)]} ({uuid.uuid4().hex[:6]})"
        off.append(run(client, args.model, args.tags + ",memory:off", question))
        on.append(run(client, args.model, args.tags, question + " fresh"))
        follow.append(run(client, args.model, args.tags, question + " fresh"))

    print(summary("memory off", off))
    print(summary("memory on (recall)", on))
    print(summary("memory on (cached)", follow))
    print(f"recall overhead p50: {statistics.median(on) - statistics.median(off):.0f} ms")


if __name__ == "__main__":
    main()
