"""Command line for the promotion workflow.

  python -m hindsight_promotion harvest [--dry-run] [--bank B] [--no-triage]   short list -> staging bank + review file
  python -m hindsight_promotion publish REVIEW_FILE [--dry-run]
  python -m hindsight_promotion nominate BANK DOCUMENT_ID  put a document into the next harvest
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .anonymize import Anonymizer
from .config import load_config
from .llm import LLM
from .store import HindsightStore
from .workflow import harvest, nominate, publish, stage, summary, write_review


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hindsight_promotion", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="promotion.yaml (or set PROMOTION_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)
    h = sub.add_parser("harvest", help="find, anonymize and stage candidates")
    h.add_argument("--dry-run", action="store_true", help="print candidates; stage nothing, write no file")
    h.add_argument("--bank", action="append", help="harvest only this bank (repeatable)")
    h.add_argument("--no-triage", action="store_true", help="skip merging, scoring and the per-bank cap")
    p = sub.add_parser("publish", help="publish approved items from a review file")
    p.add_argument("review_file", type=Path)
    p.add_argument("--dry-run", action="store_true", help="check the file; write nothing")
    n = sub.add_parser("nominate", help="tag a customer document promote:candidate")
    n.add_argument("bank")
    n.add_argument("document_id")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    store = HindsightStore(cfg.hindsight_url, cfg.hindsight_api_key, timeout=cfg.hindsight_timeout)
    try:
        if args.command == "harvest":
            llm = LLM(cfg.llm_base_url, cfg.llm_model, cfg.llm_api_key, timeout=cfg.llm_timeout)
            result = harvest(store, Anonymizer(llm, cfg.kinds, cfg.subject), cfg, llm=None if args.no_triage else llm,
                             banks=args.bank,
                             progress=lambda msg: print(msg, file=sys.stderr, flush=True))
            print(summary(result))
            if not args.dry_run and (result.candidates or result.filtered):
                stage(store, cfg, result)
                path = write_review(result, cfg.review_dir)
                print(f"\nstaged in {cfg.staging_bank}. Review and decide in:\n  {path}")
        elif args.command == "publish":
            report = publish(store, cfg, args.review_file, dry_run=args.dry_run)
            verb = "would publish" if args.dry_run else "published"
            print(f"{verb} {len(report.published)}: {', '.join(report.published) or '-'}")
            print(f"rejected {len(report.rejected)}, still pending {len(report.pending)}, "
                  f"left filtered {len(report.skipped)}")
            for e in report.errors:
                print(f"error {e}", file=sys.stderr)
            return 1 if report.errors else 0
        elif args.command == "nominate":
            nominate(store, cfg, args.bank, args.document_id)
            print(f"{args.document_id} in {args.bank} is nominated for the next harvest")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
