# Promotion into the global bank

A separate job that moves reusable SAP CAP/BTP knowledge from customer banks into `global::sap`, following the promotion workflow in the architecture doc. It is the only writer of the global bank. It shares no code with the proxy plugin in `src/litellm_hindsight`, so each can be deployed and secured on its own.

```
customer::*  --harvest-->  anonymize  --stage-->  staging::promotion  --review file-->  publish  -->  global::sap
     ^                                                                                    |
     +------------------------- promote:done / promote:rejected on the sources -----------+
```

| Step | What happens |
| --- | --- |
| Harvest | For each `customer::*` bank, one reflect per domain (`domain:cap`, `domain:xsuaa`, ...) plus one pass over the whole bank asks which patterns, pitfalls and fixes would apply to any customer. Facts that developers nominated with `promote:candidate` are added. Facts already tagged `promote:done` or `promote:rejected` are skipped. |
| Anonymize | An LLM rewrites each candidate in generic terms, or marks it as customer-specific. A mechanical check then drops any result that still names a customer, a project slug, or a word on the blocklist. |
| Stage | Candidates are saved into `staging::promotion`. The source customer is in metadata only, never in tags or text. |
| Review | A person edits a local YAML file: approve or reject, fix the text, set the topic id and the SAP release it was verified on. |
| Publish | Approved items are saved into `global::sap` with the topic id as document id, so republishing a topic updates it. The source documents get `promote:done` (or `promote:rejected`), and the staged candidate gets `promote:published` (or `promote:rejected`). |

## Running it locally

Inside the devcontainer, with the stack up, it's installed already (`pip install -e './promotion[dev]'` otherwise). The anonymizer calls the local LiteLLM proxy with `HINDSIGHT_LLM_MODEL`, so load `.env` first.

```bash
set -a; source .env; set +a

# 1. See what would be proposed, without writing anything
python -m hindsight_promotion harvest --dry-run

# 2. Stage the candidates and write the review file
python -m hindsight_promotion harvest
#    -> promotion/review/<harvest-id>.yaml

# 3. Edit that file: decision approve|reject, sap_release, topic_id, text

# 4. Check, then publish
python -m hindsight_promotion publish promotion/review/<harvest-id>.yaml --dry-run
python -m hindsight_promotion publish promotion/review/<harvest-id>.yaml
```

To nominate a finding for the next harvest, find its document id in the Hindsight UI (bank, then Documents; proxy turns look like `turn:3f9a…`) and run:

```bash
python -m hindsight_promotion nominate customer::acme turn:3f9a...
```

Settings live in [`promotion.yaml`](promotion.yaml): bank names, the domain list, the harvest query, the anonymizer endpoint and the blocklist. `PROMOTION_CONFIG` points at another file, and `PROMOTION_REVIEW_DIR` moves the review files.

Review files contain the original customer text and source ids, so `promotion/review/` is git-ignored. Don't share them outside the team.

## Known limits of this first version

- **Close the loop is per document.** Hindsight tags documents, not single facts, so `promote:done` on a proxy turn also skips the other facts from that turn in later harvests. Nominating that document again brings them back.
- **Reflect picks the source facts.** When the model doesn't cite fact ids, every fact the reflect call used is recorded as a source.
- **The leak check knows only bank names, project slugs from the source tags, and the blocklist.** Hostnames, people and subaccount names depend on the LLM and on the reviewer.
- **Mental models in the global bank are not refreshed** after publishing. Add that once the global bank uses them.

## For the production version

- Run it as a scheduled job (monthly, per the architecture doc), with its own Hindsight API key, the only one with write access to `global::sap`.
- Replace the review YAML with the team's review tool, such as a merge request or a ticket per candidate. `workflow.publish` takes the same fields from any source.
- Have each domain's knowledge owner approve the items for their domain.
- Add a quarterly retirement run that lists `global::sap` items by `sap_release` metadata.
