from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import BookCandidate, BookRequest, compact_text, normalize_doi, normalize_isbn


@dataclass(frozen=True)
class PolicyMatch:
    decision: str = "allow"
    rule_id: str = ""
    source_tier: str = ""

    @property
    def blocked(self) -> bool:
        return self.decision in {"deny", "review"}


class PolicyEngine:
    """Exact-title/identifier matcher. A/B deny; C requires review."""

    def __init__(self, rules: Iterable[dict[str, Any]] = ()):
        self.rules = tuple(rule for rule in rules if isinstance(rule, dict) and rule.get("status", "active") == "active")

    @classmethod
    def from_paths(cls, *paths: str | Path | None) -> "PolicyEngine":
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw_path in paths:
            if not raw_path:
                continue
            path = Path(raw_path)
            if not path.exists():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            rules = payload.get("rules", []) if isinstance(payload, dict) else []
            for rule in rules:
                rule_id = str(rule.get("id") or "").strip()
                if rule_id and rule_id in seen:
                    continue
                if rule_id:
                    seen.add(rule_id)
                merged.append(rule)
        return cls(merged)

    @staticmethod
    def _decision(rule: dict[str, Any]) -> PolicyMatch:
        tier = str(rule.get("source_tier") or "C").strip().upper()
        decision = "deny" if tier in {"A", "B"} else "review"
        return PolicyMatch(decision, str(rule.get("id") or ""), tier)

    @staticmethod
    def _titles(rule: dict[str, Any]) -> tuple[str, ...]:
        values = [rule.get("title"), *(rule.get("aliases") or [])]
        return tuple(value for value in (compact_text(item) for item in values) if value)

    @staticmethod
    def _keywords(rule: dict[str, Any]) -> tuple[str, ...]:
        return tuple(
            value
            for value in (compact_text(item) for item in (rule.get("keywords") or []))
            if value
        )

    def check_raw(self, message: str) -> PolicyMatch:
        compact = compact_text(message)
        isbn = normalize_isbn(message)
        doi = normalize_doi(message)
        for rule in self.rules:
            rule_isbns = {normalize_isbn(item) for item in rule.get("isbn", []) if normalize_isbn(item)}
            rule_dois = {normalize_doi(item) for item in rule.get("doi", []) if normalize_doi(item)}
            if isbn and len(isbn) in {10, 13} and isbn in rule_isbns:
                return self._decision(rule)
            if doi and doi in rule_dois:
                return self._decision(rule)
            if any(keyword in compact for keyword in self._keywords(rule)):
                return self._decision(rule)
            for title in self._titles(rule):
                if compact == title:
                    return self._decision(rule)
                if len(title) < 4:
                    continue
                if title.isdigit():
                    if re.search(rf"(?<!\d){re.escape(title)}(?!\d)", str(message or "")):
                        return self._decision(rule)
                elif title in compact:
                    return self._decision(rule)
        return PolicyMatch()

    def _check_structured(self, *, titles: Iterable[str], isbn: str = "", doi: str = "") -> PolicyMatch:
        normalized_titles = {compact_text(item) for item in titles if compact_text(item)}
        isbn = normalize_isbn(isbn)
        doi = normalize_doi(doi)
        for rule in self.rules:
            if isbn and isbn in {normalize_isbn(item) for item in rule.get("isbn", [])}:
                return self._decision(rule)
            if doi and doi in {normalize_doi(item) for item in rule.get("doi", [])}:
                return self._decision(rule)
            if any(
                keyword in title
                for keyword in self._keywords(rule)
                for title in normalized_titles
            ):
                return self._decision(rule)
            if normalized_titles.intersection(self._titles(rule)):
                return self._decision(rule)
        return PolicyMatch()

    def check_request(self, request: BookRequest) -> PolicyMatch:
        return self._check_structured(titles=request.titles, isbn=request.isbn, doi=request.doi)

    def check_candidate(self, candidate: BookCandidate) -> PolicyMatch:
        return self._check_structured(titles=(candidate.title,), isbn=candidate.isbn, doi=candidate.doi)
