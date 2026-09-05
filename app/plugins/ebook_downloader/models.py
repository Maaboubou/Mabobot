from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable


SUPPORTED_FORMATS = ("epub", "pdf", "mobi", "azw3")
REFLOWABLE_FORMATS = ("epub", "pdf", "mobi", "azw3")
FIXED_LAYOUT_FORMATS = ("pdf", "epub", "mobi", "azw3")


def compact_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(ch for ch in text if ch.isalnum())


def normalize_isbn(value: Any) -> str:
    return re.sub(r"[^0-9Xx]", "", str(value or "")).upper()


def normalize_doi(value: Any) -> str:
    value = str(value or "").strip().casefold()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value)
    return value.rstrip(".,;，。；")


def normalize_format(value: Any) -> str | None:
    result = str(value or "").strip().casefold().lstrip(".")
    return result if result in SUPPORTED_FORMATS else None


def normalize_language(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("_", "-")
    compact = compact_text(text)
    if not compact:
        return ""
    if any(token in compact for token in ("简体", "simplified", "zhcn", "zhhans")):
        return "zh-hans"
    if any(token in compact for token in ("繁体", "繁體", "traditional", "zhtw", "zhhant")):
        return "zh-hant"
    if text.startswith("zh") or compact in {"中文", "汉语", "漢語", "chinese"}:
        return "zh"
    aliases = {
        "英文": "en", "英语": "en", "英語": "en", "english": "en", "en": "en",
        "日文": "ja", "日语": "ja", "日語": "ja", "japanese": "ja", "ja": "ja",
        "法文": "fr", "法语": "fr", "法語": "fr", "french": "fr", "fr": "fr",
        "德文": "de", "德语": "de", "德語": "de", "german": "de", "de": "de",
        "西班牙文": "es", "西班牙语": "es", "spanish": "es", "es": "es",
        "俄文": "ru", "俄语": "ru", "俄語": "ru", "russian": "ru", "ru": "ru",
    }
    return aliases.get(compact, text)


def is_chinese_language(value: Any) -> bool:
    return normalize_language(value) in {"zh", "zh-hans", "zh-hant"}


def _as_strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


@dataclass(frozen=True)
class BookRequest:
    input_title: str = ""
    canonical_title: str = ""
    alternate_titles: tuple[str, ...] = ()
    authors: tuple[str, ...] = ()
    isbn: str = ""
    doi: str = ""
    year: str = ""
    publisher: str = ""
    edition: str = ""
    language: str = "zh"
    language_explicit: bool = False
    requested_format: str | None = None
    format_explicit: bool = False
    layout_preference: str = "unknown"
    search_queries: tuple[str, ...] = ()
    policy_decision: str = "allow"
    refusal_code: str = ""
    parse_confidence: float = 0.0
    model_valid: bool = True
    missing_fields: tuple[str, ...] = ()

    @property
    def has_identity(self) -> bool:
        return bool(self.input_title or self.canonical_title or self.isbn or self.doi)

    @property
    def titles(self) -> tuple[str, ...]:
        result: list[str] = []
        for value in (self.canonical_title, self.input_title, *self.alternate_titles):
            if value and value not in result:
                result.append(value)
        return tuple(result)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "BookRequest":
        book = payload.get("book") if isinstance(payload.get("book"), dict) else {}
        language = payload.get("language") if isinstance(payload.get("language"), dict) else {}
        fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
        policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}

        requested_language = normalize_language(language.get("requested"))
        language_explicit = bool(language.get("explicit"))
        if not requested_language:
            requested_language = "zh"
            language_explicit = False

        requested_format = normalize_format(fmt.get("requested"))
        format_explicit = bool(fmt.get("explicit")) and requested_format is not None
        try:
            confidence = min(1.0, max(0.0, float(payload.get("parse_confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0

        result = cls(
            input_title=str(book.get("input_title") or "").strip(),
            canonical_title=str(book.get("canonical_title") or "").strip(),
            alternate_titles=_as_strings(book.get("alternate_titles")),
            authors=_as_strings(book.get("authors")),
            isbn=normalize_isbn(book.get("isbn")),
            doi=normalize_doi(book.get("doi")),
            year=str(book.get("year") or "").strip(),
            publisher=str(book.get("publisher") or "").strip(),
            edition=str(book.get("edition") or "").strip(),
            language=requested_language,
            language_explicit=language_explicit,
            requested_format=requested_format,
            format_explicit=format_explicit,
            layout_preference=str(payload.get("layout_preference") or "unknown").strip().casefold(),
            search_queries=_as_strings(payload.get("search_queries")),
            policy_decision=str(policy.get("decision") or "allow").strip().casefold(),
            refusal_code=str(policy.get("refusal_code") or "").strip(),
            parse_confidence=confidence,
            model_valid=bool(payload.get("valid", True)),
            missing_fields=_as_strings(payload.get("missing_fields")),
        )
        queries = list(result.search_queries)
        if result.isbn:
            queries.insert(0, result.isbn)
        elif result.doi:
            queries.insert(0, result.doi)
        elif result.canonical_title:
            canonical = " ".join((result.canonical_title, result.authors[0] if result.authors else "")).strip()
            queries.insert(0, canonical)
        elif result.input_title:
            queries.insert(0, result.input_title)
        deduplicated = tuple(dict.fromkeys(item for item in queries if item))
        return cls(**{**result.__dict__, "search_queries": deduplicated[:4]})


@dataclass(frozen=True)
class BookCandidate:
    source_id: str
    title: str
    authors: tuple[str, ...] = ()
    language: str = ""
    extension: str = ""
    year: str = ""
    publisher: str = ""
    isbn: str = ""
    doi: str = ""
    href: str = ""
    download_href: str = ""
    filesize: str = ""
    quality: float = 0.0
    rating: float = 0.0
    identity_score: float = 0.0

    @property
    def normalized_format(self) -> str:
        return normalize_format(self.extension) or ""


@dataclass(frozen=True)
class WorkChoice:
    candidate: BookCandidate
    formats: tuple[str, ...]
    identity_score: float


@dataclass(frozen=True)
class SelectionResult:
    mode: str
    selected: BookCandidate | None = None
    choices: tuple[WorkChoice, ...] = ()
    reason: str = ""


def _similarity(left: str, right: str) -> float:
    a, b = compact_text(left), compact_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    if a in b or b in a:
        ratio = max(ratio, min(len(a), len(b)) / max(len(a), len(b)))
    return ratio


def candidate_identity_score(request: BookRequest, candidate: BookCandidate) -> float:
    if request.isbn and candidate.isbn:
        return 1.0 if normalize_isbn(request.isbn) == normalize_isbn(candidate.isbn) else 0.0
    if request.doi and candidate.doi:
        return 1.0 if normalize_doi(request.doi) == normalize_doi(candidate.doi) else 0.0

    title_score = max((_similarity(title, candidate.title) for title in request.titles), default=0.0)
    if not request.authors:
        score = title_score
    else:
        author_score = max(
            (_similarity(author, found) for author in request.authors for found in candidate.authors),
            default=0.0,
        )
        score = title_score * 0.78 + author_score * 0.22
    if request.year and candidate.year:
        score = score * 0.96 + (0.04 if request.year == candidate.year else 0.0)
    return min(1.0, max(0.0, score))


def _language_matches(requested: str, candidate: str) -> bool:
    requested = normalize_language(requested)
    candidate = normalize_language(candidate)
    if requested == "zh":
        return is_chinese_language(candidate)
    return bool(requested and candidate and requested == candidate)


def _format_priority(request: BookRequest) -> tuple[str, ...]:
    if request.format_explicit and request.requested_format:
        return (request.requested_format,)
    if request.layout_preference == "fixed":
        return FIXED_LAYOUT_FORMATS
    return REFLOWABLE_FORMATS


def _candidate_sort_key(request: BookRequest, candidate: BookCandidate) -> tuple[Any, ...]:
    priorities = _format_priority(request)
    try:
        format_rank = priorities.index(candidate.normalized_format)
    except ValueError:
        format_rank = len(priorities) + 1
    language = normalize_language(candidate.language)
    chinese_rank = 0 if language == "zh-hans" else 1 if language in {"zh", "zh-hant"} else 2
    return (format_rank, chinese_rank, -candidate.quality, -candidate.rating, candidate.source_id)


def select_candidate(
    request: BookRequest,
    candidates: Iterable[BookCandidate],
    *,
    title_author_threshold: float = 0.90,
    title_only_threshold: float = 0.96,
    title_author_margin: float = 0.08,
    title_only_margin: float = 0.12,
    max_choices: int = 5,
) -> SelectionResult:
    scored: list[BookCandidate] = []
    for candidate in candidates:
        if candidate.normalized_format not in SUPPORTED_FORMATS:
            continue
        score = candidate_identity_score(request, candidate)
        scored.append(BookCandidate(**{**candidate.__dict__, "identity_score": score}))
    if not scored:
        return SelectionResult("no_results", reason="没有支持格式的结果")

    identifier_exact = [item for item in scored if (request.isbn or request.doi) and item.identity_score == 1.0]
    if request.language_explicit:
        language_candidates = [item for item in scored if _language_matches(request.language, item.language)]
        if not language_candidates:
            return SelectionResult("language_unavailable", reason="没有符合指定语言的结果")
    elif identifier_exact:
        # A verified ISBN/DOI identifies its edition and therefore overrides only
        # the implicit Chinese default. An explicitly requested language remains hard.
        language_candidates = identifier_exact
        foreign_only = False
    else:
        chinese = [item for item in scored if is_chinese_language(item.language)]
        language_candidates = chinese or scored
        foreign_only = not bool(chinese)

    if request.format_explicit and request.requested_format:
        format_candidates = [
            item for item in language_candidates if item.normalized_format == request.requested_format
        ]
        if not format_candidates:
            return SelectionResult("format_unavailable", reason="没有符合指定格式的结果")
        language_candidates = format_candidates

    grouped: dict[tuple[str, str, str], list[BookCandidate]] = {}
    for item in language_candidates:
        if request.isbn and normalize_isbn(item.isbn) == request.isbn:
            key = ("isbn", request.isbn, normalize_language(item.language))
        elif request.doi and normalize_doi(item.doi) == request.doi:
            key = ("doi", request.doi, normalize_language(item.language))
        else:
            key = (
                compact_text(item.title),
                compact_text(item.authors[0] if item.authors else ""),
                normalize_language(item.language),
            )
        grouped.setdefault(key, []).append(item)

    choices: list[WorkChoice] = []
    for variants in grouped.values():
        variants.sort(key=lambda item: _candidate_sort_key(request, item))
        best = variants[0]
        formats = tuple(
            dict.fromkeys(
                item.normalized_format
                for item in sorted(variants, key=lambda item: _candidate_sort_key(request, item))
                if item.normalized_format
            )
        )
        choices.append(WorkChoice(best, formats, max(item.identity_score for item in variants)))
    choices.sort(key=lambda item: (-item.identity_score, _candidate_sort_key(request, item.candidate)))
    choices = choices[: max(1, min(5, int(max_choices)))]
    if not choices:
        return SelectionResult("no_results")

    if not request.language_explicit and foreign_only:
        return SelectionResult("foreign_confirmation", choices=tuple(choices), reason="未找到中文版本")

    threshold = title_author_threshold if request.authors else title_only_threshold
    margin_required = title_author_margin if request.authors else title_only_margin
    top = choices[0]
    runner_up = choices[1].identity_score if len(choices) > 1 else 0.0
    margin = top.identity_score - runner_up if len(choices) > 1 else 1.0
    if top.identity_score >= threshold and margin >= margin_required:
        return SelectionResult("auto", selected=top.candidate, choices=tuple(choices))
    return SelectionResult("ambiguous", choices=tuple(choices), reason="作品身份置信度不足")


def format_preferences(layout_preference: str) -> tuple[str, ...]:
    return FIXED_LAYOUT_FORMATS if str(layout_preference).casefold() == "fixed" else REFLOWABLE_FORMATS
