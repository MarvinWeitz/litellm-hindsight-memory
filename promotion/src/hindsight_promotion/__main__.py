"""Command line for the promotion workflow.

  python -m hindsight_promotion harvest [--dry-run] [--bank B]   candidates -> staging bank + review file
  python -m hindsight_promotion publish REVIEW_FILE [--dry-run]
  python -m hindsight_promotion nominate BANK DOCUMENT_ID  put a document into the next harvest
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .anonymize import Anonymizer
from .config import load_config
from .store import HindsightStore
from .workflow import harvest, nominate, publish, stage, summary, write_review

REVIEW_DIR = Path(os.environ.get("PROMOTION_REVIEW_DIR", Path(__file__).parents[2] / "review"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hindsight_promotion", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="promotion.yaml (default: $PROMOTION_CONFIG or the bundled one)")
    sub = parser.add_subparsers(dest="command", required=True)
    h = sub.add_parser("harvest", help="find, anonymize and stage candidates")
    h.add_argument("--dry-run", action="store_true", help="print candidates; stage nothing, write no file")
    h.add_argument("--bank", action="append", help="harvest only this bank (repeatable)")
    p = sub.add_parser("publish", help="publish approved items from a review file")
    p.add_argument("review_file", type=Path)
    p.add_argument("--dry-run", action="store_true", help="check the file; write nothing")
    n = sub.add_parser("nominate", help="tag a customer document promote:candidate")
    n.add_argument("bank")
    n.add_argument("document_id")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    store = HindsightStore(os.environ.get("HINDSIGHT_API_URL", "http://localhost:8888"),
                           os.environ.get("HINDSIGHT_API_KEY") or None)
    try:
        if args.command == "harvest":
            anonymizer = Anonymizer(cfg.llm_base_url, cfg.llm_model, cfg.llm_api_key, cfg.kinds)
            result = harvest(store, anonymizer, cfg, banks=args.bank,
                             progress=lambda msg: print(msg, file=sys.stderr, flush=True))
            print(summary(result))
            if not args.dry_run and result.candidates:
                stage(store, cfg, result)
                path = write_review(result, REVIEW_DIR)
                print(f"\nstaged in {cfg.staging_bank}. Review and decide in:\n  {path}")
        elif args.command == "publish":
            report = publish(store, cfg, args.review_file, dry_run=args.dry_run)
            verb = "would publish" if args.dry_run else "published"
            print(f"{verb} {len(report.published)}: {', '.join(report.published) or '-'}")
            print(f"rejected {len(report.rejected)}, still pending {len(report.pending)}")
            for e in report.errors:
                print(f"error {e}", file=sys.stderr)
            return 1 if report.errors else 0
        elif args.command == "nominate":
            nominate(store, args.bank, args.document_id)
            print(f"{args.document_id} in {args.bank} is nominated for the next harvest")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
