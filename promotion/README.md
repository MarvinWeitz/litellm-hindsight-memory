# Promotion into the global bank

A separate job that moves reusable SAP CAP/BTP knowledge from customer banks into `global::sap`, following the promotion workflow in the architecture doc. It is the only writer of the global bank. It shares no code with the proxy plugin in `src/litellm_hindsight`, so each can be deployed and secured on its own.

```
customer::*  --harvest-->  anonymize  --triage-->  stage  -->  staging::promotion  --review file-->  publish  -->  global::sap
     ^                                                                                                  |
     +------------------------------- promote:done / promote:rejected on the sources -------------------+
```

| Step | What happens |
| --- | --- |
| Harvest | For each `customer::*` bank, one reflect per domain (`domain:cap`, `domain:xsuaa`, ...) plus one pass over the whole bank asks which patterns, pitfalls and fixes would apply to any customer. Facts that developers nominated with `promote:candidate` are added. Facts already tagged `promote:done` or `promote:rejected` are skipped. |
| Anonymize | An LLM rewrites each candidate in generic terms, or marks it as customer-specific. A mechanical check then drops any result that still names a customer, a project slug, or a word on the blocklist. |
| Triage | Cuts the list to what is worth a person's time (see below). Nothing is deleted: set-aside candidates go into the review file with the reason. |
| Stage | Candidates are saved into `staging::promotion`. The source customer is in metadata only, never in tags or text. |
| Review | A person edits a local YAML file: approve or reject, fix the text, set the topic id and the SAP release it was verified on. |
| Publish | Approved items are saved into `global::sap` with the topic id as document id, so republishing a topic updates it. The source documents get `promote:done` (or `promote:rejected`), and the staged candidate gets `promote:published` (or `promote:rejected`). |

## Triage: a short list instead of everything

A raw harvest returns dozens of candidates, most of them the same advice in different words, or advice any model gives without memory. Triage runs after anonymization, with the same LLM endpoint, and only ever sees the anonymized text:

1. **Merge duplicates.** One LLM call per bank groups notes that say the same thing. A group becomes one candidate with the sources of all members, so a fix seen in three turns counts as three sources.
2. **Novelty.** The model answers the candidate's topic id (for example `pitfall:bp-api-403-role`) with no memory. If that answer already holds the key point, the candidate is set aside as "the model already knows this". `novelty_check: false` turns this off and saves one call per candidate.
3. **Already published.** The closest items in `global::sap` are recalled. If the judge says one of them states the same, the candidate is set aside with that topic id, so you can update the existing topic instead.
4. **Score and cap.** The judge rates specificity (concrete error, config key, role, version) and reusability, 1 to 5 each, plus up to 3 for independent sources. Candidates under `min_score` are set aside, and only the best `max_per_bank` per bank (default 5) reach the short list. Nominated facts are always on the short list.

The review file has the short list under `items` (with `score` and the judge's `assessment`) and the rest under `filtered` with `filtered_because`. Leave filtered entries alone, or set one to `approve` to publish it anyway. Settings are in the `triage` section of [`promotion.yaml`](promotion.yaml); `harvest --no-triage` gives the old full list.

Cost: per bank one merge call, then per candidate one anonymize, one baseline and one judge call, plus a recall on the global bank.

## Running it locally

Inside the devcontainer, with the stack up, it's installed already (`pip install -e './promotion[dev]'` otherwise). The anonymizer calls the local LiteLLM proxy with `HINDSIGHT_LLM_MODEL`, so load `.env` first.

```bash
set -a; source .env; set +a
export PROMOTION_CONFIG=promotion/promotion.yaml   # or pass --config to each command

# 1. See what would be proposed, without writing anything.
#    Progress goes to stderr; each reflect takes seconds to a few minutes on CPU.
#    --bank customer::acme limits the run to one bank.
python -m hindsight_promotion harvest --dry-run

# 2. Stage the candidates and write the review file (short list plus the filtered rest)
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

Settings live in [`promotion.yaml`](promotion.yaml): the Hindsight connection, bank names, the scope, domain, kind and lifecycle tags, the domain list, the harvest query, the LLM endpoint, the subject named in the prompts, the blocklist, triage limits, the review directory and the texts stored with candidates. There are no built-in defaults: every key is required, unknown keys are refused, and the file must be named with `--config` or `PROMOTION_CONFIG`. Keep `tags.scope_prefix` and `tags.shared_scope` in step with the scope tag in `config/hindsight_routing.yaml`, because scope values are blocked from the global bank.

Review files contain the original customer text and source ids, so `promotion/review/` is git-ignored. Don't share them outside the team.

## Known limits of this first version

- **Close the loop is per document.** Hindsight tags documents, not single facts, so `promote:done` on a proxy turn also skips the other facts from that turn in later harvests. Nominating that document again brings them back.
- **Reflect picks the source facts.** When the model doesn't cite fact ids, every fact the reflect call used is recorded as a source.
- **The leak check knows only bank names, project slugs from the source tags, and the blocklist.** Hostnames, people and subaccount names depend on the LLM and on the reviewer.
- **Triage is per bank.** The same fix found in two customer banks becomes two candidates; once the first is published, the global-bank check sets the second aside in the next harvest.
- **Source counts can be high.** When reflect cites no fact ids, every fact it used counts as a source, which can inflate the evidence part of the score (capped at 3).
- **Scores come from one LLM call.** Tune `min_score` and `max_per_bank` after a few real harvests, and look at the `filtered` section now and then.
- **Mental models in the global bank are not refreshed** after publishing. Add that once the global bank uses them.

## For the production version

- Run it as a scheduled job (monthly, per the architecture doc), with its own Hindsight API key, the only one with write access to `global::sap`.
- Replace the review YAML with the team's review tool, such as a merge request or a ticket per candidate. `workflow.publish` takes the same fields from any source.
- Have each domain's knowledge owner approve the items for their domain.
- Add a quarterly retirement run that lists `global::sap` items by `sap_release` metadata.
