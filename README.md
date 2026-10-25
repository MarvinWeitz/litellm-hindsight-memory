# litellm-hindsight-memory

A LiteLLM proxy plugin that gives every tagged request long-term memory from [Hindsight](https://hindsight.vectorize.io).

Request tags decide everything. A request tagged `customer:acme` and `project:acme-portal`:

1. recalls, in parallel, from `customer::acme` (memories tagged `project:acme-portal` or `project:_shared`) and from the shared `global::sap` bank,
2. gets those memories added to the developer's message before the model sees it,
3. has the developer's message and the final answer saved to `customer::acme`, after the response has gone back.

The global bank is only ever read. Requests without a customer tag pass through untouched.

## Quick start (Linux devcontainer)

The devcontainer is one Linux container (Debian trixie, Python 3.12) with its own Docker engine (Docker-in-Docker). LiteLLM, Hindsight and Postgres all run inside it, so the host needs only a devcontainer runtime: VS Code with Dev Containers, GitHub Codespaces, or the `devcontainer` CLI. Give it at least 4 CPUs, 8 GB RAM and 32 GB disk.

1. Open the repository in the devcontainer. In VS Code, choose **Reopen in Container**. With the CLI, run `devcontainer up --workspace-folder .` and then `devcontainer exec --workspace-folder . bash`.
2. In the container's terminal, edit `.env` (created from `.env.example` on first start). Choose how the local proxy reaches models:
   - **Upstream mode (default, no provider keys).** The local proxy forwards every request to an existing LiteLLM proxy, such as your company's, and adds memory on the way. Set `UPSTREAM_LITELLM_URL` and put your SSO token in `UPSTREAM_LITELLM_KEY`. Run `scripts/stack.sh models` to list the models it offers, and set `HINDSIGHT_LLM_MODEL` to a small, cheap one. Hindsight uses it to extract facts. When the token expires, run `scripts/stack.sh token <new-token>`. This updates `.env` and restarts only the proxy.
   - **Direct mode.** Set `LITELLM_CONFIG=litellm.yaml` and `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY`, and change `hindsight-extraction` in `config/litellm.yaml`).
3. Start the stack: `scripts/stack.sh up`. The first start pulls the Hindsight image (about 9 GB) and loads its models, which takes a few minutes. The script waits until both services answer.
4. Create the demo banks and memories: `python scripts/seed_banks.py`. Wait a minute while Hindsight extracts facts. You can watch them arrive in the Hindsight UI on port 9999. `--reset` deletes the demo banks first, for a clean start.
5. Send a tagged request from the container's terminal:

   ```bash
   curl localhost:4000/v1/chat/completions \
     -H "Authorization: Bearer sk-local-dev" \
     -H "x-litellm-tags: customer:acme,project:acme-portal" \
     -H "content-type: application/json" \
     -d '{"model":"<a model from scripts/stack.sh models>","messages":[{"role":"user","content":"How do we handle roles on the portal?"}]}'
   ```

6. Measure the overhead: `python scripts/bench.py`.

`scripts/stack.sh logs` follows the proxy and Hindsight logs, and `scripts/stack.sh down` stops the stack. Docker data inside the devcontainer lives in a named volume, so the pulled images and memory banks survive container rebuilds.

| Service | Port inside the container | |
| --- | --- | --- |
| LiteLLM proxy | 4000 | key `sk-local-dev` locally |
| Hindsight API | 8888 | |
| Hindsight UI | 9999 | browse banks and memories |

The devcontainer forwards these ports, so tools on your host (a browser, or Claude Code pointed at `http://localhost:4000`) reach them too.

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

Routing lives in [`config/hindsight_routing.yaml`](config/hindsight_routing.yaml) and can change without code changes. The plugin has no built-in defaults: every key in that file is required, keys that can be switched off take an explicit `null`, and unknown keys are refused when the proxy loads the file. Set `HINDSIGHT_ROUTING_CONFIG` to its path.

Two kinds of request tags drive routing:

- the **bank tag** (`tags.bank`) picks the memory bank, e.g. `customer:acme` selects `customer::acme`,
- the **scope tag** (`tags.scope`) only narrows recall and retain inside that bank, e.g. `project:acme-portal`. It never selects a bank. Set `tags.scope: null` for banks without scopes.

Each kind has a `name` and a `prefix`. The name is its placeholder in the bank and tag templates (`{customer}`, `{project}` in the shipped file), so renaming `customer` to `kunde` means writing `{kunde}` in the templates. A template with a placeholder the scheme doesn't define is refused at load time.

The file also sets:

- the Hindsight connection (`hindsight.url_env` and `api_key_env` name the environment variables, plus the client timeout and user agent),
- the opt-out tag, the tags copied onto saved memories, the allowed value pattern, and whether matching ignores case,
- the write bank template and any number of read banks, each with its own tag filter, memory types, budget and token cap. The customer bank reads only `world` and `experience` facts: Hindsight's observations are consolidated across the whole bank and can carry one project's facts under another project's tag,
- what gets saved: tags, size cap, the text template, the extraction context, the document id prefix and metadata,
- recall timeout (0.8 s in the shipped file, fail open) and the per-turn cache,
- the memory block's marker and header text,
- `untagged_request` (`passthrough` or `reject`) and an allowlist of bank and scope values, so a typo can't create an invisible bank.

`HINDSIGHT_LOG_LEVEL=INFO` logs one line per recall (banks, facts, milliseconds) and per retain. The local stack sets it.

## How it keeps latency down

- **One recall per developer turn.** Coding agents send many requests per task, mostly tool results. The plugin recalls on the developer's message and reuses that result for the tool-loop requests that follow.
- **Prompt caching stays intact.** Memory goes at the start of the developer's latest message, not into the system prompt, and the same text is reused within a turn.
- **Reranking is the expensive part on CPU.** In the devcontainer, recall from a bank with about 100 candidates took about 4 s with Hindsight's default cross-encoder reranker and under 200 ms without it. For local work, seed with `python scripts/seed_banks.py --no-rerank` or set `HINDSIGHT_RERANKER_PROVIDER=flashrank` in `.env`. In production, give Hindsight a GPU or a hosted reranker.
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

## Promotion into the global bank

The plugin only reads `global::sap`. Reviewed knowledge gets there through a separate job in [`promotion/`](promotion/README.md): it harvests candidates from the customer banks, anonymizes them, stages them for a human review and publishes the approved ones. It shares no code with the plugin and can be deployed, scheduled and secured on its own.

## Development

```bash
pip install -e '.[dev]'
pytest
```

The tests use a fake Hindsight client and LiteLLM's mock responses, including a run through a real in-process proxy on both `/v1/chat/completions` and `/v1/messages`, with and without streaming.
