# litellm-hindsight-memory

A LiteLLM proxy plugin that gives every tagged request long-term memory from [Hindsight](https://hindsight.vectorize.io).

Request tags decide everything. A request tagged `customer:acme` and `project:acme-portal`:

1. recalls, in parallel, from `customer::acme` (memories tagged `project:acme-portal` or `project:_shared`) and from the shared `global::sap` bank,
2. gets those memories added to the developer's message before the model sees it,
3. has the developer's message and the final answer saved to `customer::acme`, after the response has gone back.

The global bank is only ever read. Requests without a customer tag pass through untouched.

## Quick start (devcontainer)

1. `cp .env.example .env` and set `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY` and change `hindsight-extraction` in `config/litellm.yaml`).
2. Open the folder in VS Code and choose **Reopen in Container**. Or, without VS Code, run `docker compose up -d --build`.
3. Create the demo banks and memories: `python scripts/seed_banks.py`. Wait a minute while Hindsight extracts facts.
4. Send a tagged request:

   ```bash
   curl localhost:4000/v1/chat/completions \
     -H "Authorization: Bearer sk-local-dev" \
     -H "x-litellm-tags: customer:acme,project:acme-portal" \
     -H "content-type: application/json" \
     -d '{"model":"claude-sonnet-5-5","messages":[{"role":"user","content":"How do we handle roles on the portal?"}]}'
   ```

5. Measure the overhead: `python scripts/bench.py`.

| Service | Port | |
| --- | --- | --- |
| LiteLLM proxy | 4000 | key `sk-local-dev` locally |
| Hindsight API | 8888 | |
| Hindsight UI | 9999 | browse banks and memories |

The first start pulls the full Hindsight image (about 9 GB, with local embedding and reranker models).

## Tagging requests

LiteLLM reads tags from the `x-litellm-tags` header, a top-level `tags` array, or `metadata.tags`. No virtual keys are needed, so this works with SSO tokens.

For Claude Code, set the header per repository in `.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:4000",
    "ANTHROPIC_CUSTOM_HEADERS": "x-litellm-tags: customer:acme,project:acme-portal"
  }
}
```

Extra tags with a `domain:` or `kind:` prefix are copied onto saved memories. Add `memory:off` to skip memory for a request.

## Configuration

Routing lives in [`config/hindsight_routing.yaml`](config/hindsight_routing.yaml) and can change without code changes:

- tag prefixes for customer and project,
- the write bank template (`customer::{customer}`),
- any number of read banks, each with its own tag filter, budget and token cap (add an industry bank, a team bank, and so on),
- an allowlist of customers and projects, so a typo can't create an invisible bank,
- `untagged_request: reject` to require a customer tag on every request,
- recall timeout (default 0.8 s, fail open) and the per-turn cache.

Point `HINDSIGHT_ROUTING_CONFIG` at the file. `HINDSIGHT_API_URL` and `HINDSIGHT_API_KEY` set the Hindsight connection.

## How it keeps latency down

- **One recall per developer turn.** Coding agents send many requests per task, mostly tool results. The plugin recalls on the developer's message and reuses that result for the tool-loop requests that follow.
- **Prompt caching stays intact.** Memory goes at the start of the developer's latest message, not into the system prompt, and the same text is reused within a turn.
- **Fail open.** A bank that doesn't answer within the timeout is skipped and logged.
- **Saving never blocks.** Retain runs in LiteLLM's success logger with `retain_async=True`. Tool-call responses are not saved, only the final answer of a turn.

## Using it with an existing proxy

Install the package into the proxy's environment and add the callback:

```bash
pip install git+https://github.com/MarvinWeitz/litellm-hindsight-memory
```

```yaml
litellm_settings:
  callbacks: litellm_hindsight.proxy_handler_instance
```

## Development

```bash
pip install -e '.[dev]'
pytest
```

The tests use a fake Hindsight client and LiteLLM's mock responses, including a run through a real in-process proxy on both `/v1/chat/completions` and `/v1/messages`, with and without streaming.
