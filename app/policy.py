"""
Policy layer: loads sops.yaml and decides which rules fire.

Three things to be able to defend:

1. SOPs are DATA. This module holds no policy - it is an interpreter for a
   small declarative condition language. Adding an 11th SOP is a YAML edit.

2. Matching is HYBRID, on purpose:
     - numeric conditions are evaluated in Python against the live facts.
       Deterministic, auditable, cheap, cannot hallucinate.
     - the user's activity is classified by an LLM into a CLOSED enum, so
       "can I take my kid to the park" reaches child_outdoor_play without
       keyword matching.
     - rules marked `fuzzy: true` have no thresholds and are judged by an LLM
       against their `when` text plus the facts.
   Pure Python cannot answer "is today good for a picnic". Pure LLM cannot be
   audited or held to a threshold. So: both, with a clear boundary.

3. Conflict resolution is data too (policy_config.conflict_resolution).
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any

import yaml

SOP_PATH = os.environ.get(
    "SOP_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "sops.yaml")
)


class PolicyError(ValueError):
    """Malformed sops.yaml. Fail at load, loudly, not silently at runtime."""


@dataclass
class SOP:
    id: str
    category: str
    severity: str
    priority: int
    title: str
    when: str
    guidance: str
    source: str = ""
    conditions: dict[str, Any] | None = None
    applies_to: list[str] = field(default_factory=list)
    fuzzy: bool = False

    def citation(self) -> str:
        return f"{self.id} - {self.title}"


@dataclass
class PolicyBook:
    sops: list[SOP]
    config: dict[str, Any]

    @property
    def severity_rank(self) -> dict[str, int]:
        return self.config["severity_rank"]

    @property
    def activities(self) -> list[str]:
        return self.config["activities"]

    def by_id(self, sop_id: str) -> SOP | None:
        return next((s for s in self.sops if s.id == sop_id), None)


_cache: dict[str, Any] = {"mtime": None, "book": None}
_lock = threading.Lock()


def load_policy_book(path: str = SOP_PATH, force: bool = False) -> PolicyBook:
    """Load sops.yaml, re-reading whenever the file changes on disk.

    Hot reload is what makes "add an 11th SOP live, no code change" true in
    practice: save the YAML, ask the next question, it is in effect.
    """
    mtime = os.path.getmtime(path)
    with _lock:
        if not force and _cache["mtime"] == mtime and _cache["book"] is not None:
            return _cache["book"]

        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)

        sops = [
            SOP(
                id=e["id"],
                category=e["category"],
                severity=e["severity"],
                priority=int(e.get("priority", 10)),
                title=e["title"],
                when=" ".join(e["when"].split()),
                guidance=" ".join(e["guidance"].split()),
                source=" ".join(e.get("source", "").split()),
                conditions=e.get("conditions"),
                applies_to=e.get("applies_to") or [],
                fuzzy=bool(e.get("fuzzy", False)),
            )
            for e in raw["sops"]
        ]

        book = PolicyBook(sops=sops, config=raw["policy_config"])
        _validate(book)
        _cache["mtime"] = mtime
        _cache["book"] = book
        return book


def _facts_referenced(node: Any) -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("any_of", "all_of"):
                for child in v:
                    out.extend(_facts_referenced(child))
            else:
                out.append(k)
    elif isinstance(node, list):
        for child in node:
            out.extend(_facts_referenced(child))
    return out


def _validate(book: PolicyBook) -> None:
    seen: set[str] = set()
    known = set(book.config["known_facts"])
    cats = set(book.config["categories"])
    acts = set(book.activities)

    for s in book.sops:
        if s.id in seen:
            raise PolicyError(f"duplicate SOP id: {s.id}")
        seen.add(s.id)
        if s.severity not in book.severity_rank:
            raise PolicyError(f"{s.id}: unknown severity {s.severity!r}")
        if s.category not in cats:
            raise PolicyError(f"{s.id}: unknown category {s.category!r}")
        for a in s.applies_to:
            if a not in acts:
                raise PolicyError(f"{s.id}: unknown activity {a!r}")
        if not s.conditions and not s.fuzzy:
            raise PolicyError(
                f"{s.id}: needs `conditions` or `fuzzy: true`, else it can never match"
            )
        for fact in _facts_referenced(s.conditions or {}):
            if fact not in known:
                raise PolicyError(
                    f"{s.id}: references unknown fact {fact!r}. Add it to "
                    f"weather.py::build_facts and policy_config.known_facts."
                )


def _cmp(value: Any, op: str, target: Any) -> bool:
    if value is None:
        # A condition over a fact we do not have is NOT satisfied. Guessing a
        # missing value is the same error class as inventing a forecast.
        return False
    try:
        if op == "gte":
            return float(value) >= float(target)
        if op == "gt":
            return float(value) > float(target)
        if op == "lte":
            return float(value) <= float(target)
        if op == "lt":
            return float(value) < float(target)
        if op == "eq":
            return value == target or float(value) == float(target)
        if op == "ne":
            return not (value == target or float(value) == float(target))
        if op == "between":
            lo, hi = target
            return float(lo) <= float(value) <= float(hi)
        if op == "in":
            return value in target
        if op == "not_in":
            return value not in target
        if op == "is_true":
            return bool(value) is bool(target)
    except (TypeError, ValueError):
        return False
    raise PolicyError(f"unsupported operator: {op!r}")


def evaluate_condition(node: Any, facts: dict[str, Any]) -> bool:
    if not node:
        return True
    if isinstance(node, list):
        return all(evaluate_condition(c, facts) for c in node)

    results = []
    for key, spec in node.items():
        if key == "any_of":
            results.append(any(evaluate_condition(c, facts) for c in spec))
        elif key == "all_of":
            results.append(all(evaluate_condition(c, facts) for c in spec))
        else:
            results.append(all(_cmp(facts.get(key), op, t) for op, t in spec.items()))
    return all(results)


@dataclass
class Match:
    sop: SOP
    reason: str
    kind: str  # deterministic | fuzzy

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.sop.id,
            "title": self.sop.title,
            "severity": self.sop.severity,
            "category": self.sop.category,
            "guidance": self.sop.guidance,
            "when": self.sop.when,
            "source": self.sop.source,
            "reason": self.reason,
            "kind": self.kind,
        }


def _activity_ok(sop: SOP, activity: str) -> bool:
    # Empty applies_to = activity-agnostic. That is how situational overrides
    # reach every question regardless of category.
    return not sop.applies_to or activity in sop.applies_to


def match_deterministic(book: PolicyBook, facts: dict, activity: str) -> list[Match]:
    """Threshold rules only. No LLM. Fully reproducible."""
    out = []
    for sop in book.sops:
        if sop.fuzzy or not sop.conditions or not _activity_ok(sop, activity):
            continue
        if evaluate_condition(sop.conditions, facts):
            used = sorted(set(_facts_referenced(sop.conditions)))
            out.append(
                Match(
                    sop=sop,
                    reason="conditions met: "
                    + ", ".join(f"{f}={facts.get(f)}" for f in used),
                    kind="deterministic",
                )
            )
    return out


def candidate_fuzzy_sops(book: PolicyBook, activity: str) -> list[SOP]:
    return [s for s in book.sops if s.fuzzy and _activity_ok(s, activity)]


def rank_and_split(book: PolicyBook, matches: list[Match]) -> tuple[Match | None, list[Match]]:
    """Declared strategy: rank by priority then severity. Top rule is primary
    and drives the advice; others at >= threshold are surfaced as secondary."""
    if not matches:
        return None, []

    rank = book.severity_rank
    cfg = book.config["conflict_resolution"]
    ordered = sorted(
        matches, key=lambda m: (m.sop.priority, rank[m.sop.severity]), reverse=True
    )
    primary = ordered[0]

    if not cfg.get("surface_secondary", True):
        return primary, []

    bar = rank[cfg.get("secondary_surface_threshold", "moderate")]
    secondary = [m for m in ordered[1:] if rank[m.sop.severity] >= bar]
    return primary, secondary[: int(cfg.get("max_secondary", 2))]