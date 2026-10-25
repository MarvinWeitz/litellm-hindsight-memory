"""Turn request tags into a memory route: which banks to read and write."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import ReadTarget, RoutingConfig

_SLUG = re.compile(r"^[a-z0-9_][a-z0-9_-]{0,62}$")


class RoutingError(ValueError):
    """The request carries memory tags that cannot be routed safely."""


@dataclass(frozen=True)
class Route:
    customer: str
    project: str | None
    write_bank: str
    reads: tuple[ReadTarget, ...]
    retain_tags: tuple[str, ...]


def collect_tags(data: dict) -> list[str]:
    """Gather request tags from every place the LiteLLM proxy puts them."""
    tags: list[str] = []
    for source in (data.get("tags"), (data.get("metadata") or {}).get("tags"),
                   (data.get("litellm_metadata") or {}).get("tags")):
        if isinstance(source, str):
            source = source.split(",")
        for tag in source or ():
            if isinstance(tag, str) and tag.strip() and tag.strip() not in tags:
                tags.append(tag.strip())
    return tags


def resolve(tags: list[str], cfg: RoutingConfig) -> Route | None:
    """Return the route for these tags, or None when memory does not apply.

    Raises RoutingError for tags that look like memory routing but are unsafe:
    several customers, a malformed slug, or a slug outside the allowlist.
    """
    if cfg.opt_out_tag and cfg.opt_out_tag in tags:
        return None

    customers = _values(tags, cfg.customer_prefix)
    projects = _values(tags, cfg.project_prefix)
    if not customers:
        if cfg.untagged_request == "reject":
            raise RoutingError(f"request has no '{cfg.customer_prefix}<slug>' tag")
        return None
    if len(customers) > 1:
        raise RoutingError(f"request names several customers: {', '.join(customers)}")
    if len(projects) > 1:
        raise RoutingError(f"request names several projects: {', '.join(projects)}")

    customer = customers[0]
    project = projects[0] if projects else cfg.project_when_missing
    for slug in (customer, project):
        if slug is not None and not _SLUG.match(slug):
            raise RoutingError(f"invalid slug {slug!r}: use lowercase letters, digits, '-' or '_'")
    _check_allowed(customer, project, cfg)

    fields = {"customer": customer, "project": project or ""}
    reads = tuple(
        ReadTarget(
            bank=r.bank.format(**fields),
            tags=tuple(t.format(**fields) for t in r.tags if project or "{project}" not in t),
            tags_match=r.tags_match,
            types=r.types,
            budget=r.budget,
            max_tokens=r.max_tokens,
        )
        for r in cfg.reads
    )
    retain_tags = [t.format(**fields) for t in cfg.retain_tags if project or "{project}" not in t]
    retain_tags += [t for t in tags if t.startswith(cfg.passthrough_prefixes) and t not in retain_tags]
    return Route(
        customer=customer,
        project=project,
        write_bank=cfg.write_bank.format(**fields),
        reads=reads,
        retain_tags=tuple(retain_tags),
    )


def _values(tags: list[str], prefix: str) -> list[str]:
    out: list[str] = []
    for tag in tags:
        if tag.lower().startswith(prefix.lower()):
            value = tag[len(prefix):].strip().lower()
            if value and value not in out:
                out.append(value)
    return out


def _check_allowed(customer: str, project: str | None, cfg: RoutingConfig) -> None:
    if cfg.allowed is None:
        return
    if customer not in cfg.allowed:
        raise RoutingError(f"unknown customer {customer!r}")
    projects = cfg.allowed[customer]
    if projects is None or project is None or project == cfg.project_when_missing:
        return
    if project not in projects:
        raise RoutingError(f"unknown project {project!r} for customer {customer!r}")
