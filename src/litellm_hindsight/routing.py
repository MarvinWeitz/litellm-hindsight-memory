"""Turn request tags into a memory route: which banks to read and write."""

from __future__ import annotations

from dataclasses import dataclass

from .config import ReadTarget, RoutingConfig


class RoutingError(ValueError):
    """The request carries memory tags that cannot be routed safely."""


@dataclass(frozen=True)
class Route:
    bank_value: str  # value of the bank tag, e.g. "acme" from customer:acme
    scope_value: str | None  # value of the scope tag, e.g. "acme-portal" from project:acme-portal
    label: str  # "customer acme, project acme-portal", for the memory header and logs
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
    several bank tags or scope tags, a malformed value, or a value outside the allowlist.
    """
    if cfg.opt_out_tag and cfg.opt_out_tag in tags:
        return None

    bank_kind, scope_kind = cfg.bank_tag, cfg.scope_tag
    values = _values(tags, bank_kind.prefix, cfg.lowercase)
    scopes = _values(tags, scope_kind.prefix, cfg.lowercase) if scope_kind else []
    if not values:
        if cfg.untagged_request == "reject":
            raise RoutingError(f"request has no '{bank_kind.prefix}<{bank_kind.name}>' tag")
        return None
    if len(values) > 1:
        raise RoutingError(f"request names several {bank_kind.name} values: {', '.join(values)}")
    if len(scopes) > 1:
        raise RoutingError(f"request names several {scope_kind.name} values: {', '.join(scopes)}")

    value = values[0]
    scope = scopes[0] if scopes else cfg.scope_when_missing
    for slug in (value, scope):
        if slug is not None and not cfg.slug_pattern.fullmatch(slug):
            raise RoutingError(f"invalid tag value {slug!r}: it must match {cfg.slug_pattern.pattern}")
    _check_allowed(value, scope, cfg)

    fields = {bank_kind.name: value}
    if scope_kind:
        fields[scope_kind.name] = scope or ""

    def keep(template: str) -> bool:
        # Without a scope value, drop tag templates that would need one.
        return scope is not None or scope_kind is None or f"{{{scope_kind.name}}}" not in template

    reads = tuple(
        ReadTarget(
            bank=r.bank.format(**fields),
            tags=tuple(t.format(**fields) for t in r.tags if keep(t)),
            tags_match=r.tags_match,
            types=r.types,
            budget=r.budget,
            max_tokens=r.max_tokens,
        )
        for r in cfg.reads
    ) + tuple(g.read for g in cfg.global_banks if g.applies_to.matches(value, scope, tags))
    write_bank = cfg.write_bank.format(**fields)
    if any(g.id == write_bank for g in cfg.global_banks):
        raise RoutingError(f"write bank {write_bank!r} is a global bank; global banks are read-only")
    retain_tags = [t.format(**fields) for t in cfg.retain_tags if keep(t)]
    retain_tags += [t for t in tags if t.startswith(cfg.passthrough_prefixes) and t not in retain_tags]
    label = f"{bank_kind.name} {value}" + (f", {scope_kind.name} {scope}" if scope_kind and scope else "")
    return Route(
        bank_value=value,
        scope_value=scope,
        label=label,
        write_bank=write_bank,
        reads=reads,
        retain_tags=tuple(retain_tags),
    )


def _values(tags: list[str], prefix: str, lowercase: bool) -> list[str]:
    out: list[str] = []
    for tag in tags:
        matches = tag.lower().startswith(prefix.lower()) if lowercase else tag.startswith(prefix)
        if matches:
            value = tag[len(prefix):].strip()
            value = value.lower() if lowercase else value
            if value and value not in out:
                out.append(value)
    return out


def _check_allowed(value: str, scope: str | None, cfg: RoutingConfig) -> None:
    if cfg.allowed is None:
        return
    if value not in cfg.allowed:
        raise RoutingError(f"unknown {cfg.bank_tag.name} {value!r}")
    scopes = cfg.allowed[value]
    if scopes is None or scope is None or scope == cfg.scope_when_missing:
        return
    if scope not in scopes:
        raise RoutingError(f"unknown {cfg.scope_tag.name} {scope!r} for {cfg.bank_tag.name} {value!r}")
