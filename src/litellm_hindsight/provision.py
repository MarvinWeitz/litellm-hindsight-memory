"""Create or update the global banks listed in the routing config.

  python -m litellm_hindsight.provision [--config FILE] [--bank ID] [--dry-run] [--no-rerank]

Safe to run again: Hindsight's create_bank creates a missing bank and updates
the profile of an existing one, and memories are never touched. Run it after
adding a global bank or changing its profile.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterable
from typing import Any

from .config import GlobalBank, load_config


def provision(client: Any, banks: Iterable[GlobalBank], *, only: Iterable[str] | None = None,
              config_overrides: dict[str, Any] | None = None, dry_run: bool = False,
              progress: Callable[[str], None] = print) -> list[str]:
    """Apply each bank's profile and config. Returns the ids of the banks handled."""
    banks = list(banks)
    only = set(only or ())
    unknown = only - {b.id for b in banks}
    if unknown:
        raise ValueError(f"not in global_banks: {', '.join(sorted(unknown))}")
    done: list[str] = []
    for bank in banks:
        if only and bank.id not in only:
            continue
        create = bank.create_kwargs()
        config = {**bank.bank_config, **(config_overrides or {})}
        if dry_run:
            progress(f"would provision {bank.id}:\n  create_bank {json.dumps(create, ensure_ascii=False)}"
                     f"\n  update_bank_config {json.dumps(config, ensure_ascii=False)}")
        else:
            client.create_bank(bank.id, **create)
            if config:
                client.update_bank_config(bank.id, **config)
            progress(f"provisioned {bank.id}")
        done.append(bank.id)
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="litellm_hindsight.provision", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="routing YAML (default: $HINDSIGHT_ROUTING_CONFIG)")
    parser.add_argument("--bank", action="append", help="provision only this global bank (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="print what would be sent; change nothing")
    parser.add_argument("--no-rerank", action="store_true",
                        help="set enable_reranking false on every bank (much faster recall on CPU)")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if not cfg.global_banks:
        print("no global_banks in the routing config; nothing to do")
        return 0
    overrides = {"enable_reranking": False} if args.no_rerank else None
    client = None
    if not args.dry_run:
        from hindsight_client import Hindsight

        client = Hindsight(base_url=cfg.hindsight_url, api_key=cfg.hindsight_api_key,
                           timeout=cfg.client_timeout, user_agent=cfg.user_agent)
    try:
        provision(client, cfg.global_banks, only=args.bank, config_overrides=overrides, dry_run=args.dry_run)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if client is not None:
            client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
