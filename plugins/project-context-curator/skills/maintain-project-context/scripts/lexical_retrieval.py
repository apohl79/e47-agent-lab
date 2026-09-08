from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
LEXICAL_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
LEXICAL_ALIASES = {
    "pr": ("pull", "request"),
    "prs": ("pull", "request"),
}
LEXICAL_STOP_TOKENS = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "behavior",
        "by",
        "can",
        "context",
        "did",
        "do",
        "does",
        "explain",
        "fact",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "in",
        "integration",
        "into",
        "is",
        "it",
        "know",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "project",
        "repository",
        "should",
        "that",
        "the",
        "their",
        "them",
        "there",
        "this",
        "to",
        "use",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
    }
)
LEXICAL_SUFFIXES = (
    "ingly",
    "edly",
    "ments",
    "ment",
    "ships",
    "ship",
    "ions",
    "ion",
    "ings",
    "ing",
    "ers",
    "er",
    "ed",
    "es",
    "al",
    "s",
)


@dataclass(frozen=True)
class LexicalEvidence:
    score: float
    matched_terms: int
    query_terms: int
    exact_label: bool
    exact_identifier: bool


def lexical_stem(token: str) -> str:
    stem = token
    for _ in range(2):
        reduced = next(
            (
                stem[: -len(suffix)]
                for suffix in LEXICAL_SUFFIXES
                if stem.endswith(suffix) and len(stem) - len(suffix) >= 4
            ),
            stem,
        )
        if reduced == stem:
            break
        stem = reduced
    return stem[:-1] if len(stem) > 5 and stem.endswith("e") else stem


def lexical_terms(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize(
        "NFKC",
        CAMEL_CASE_BOUNDARY.sub(" ", value),
    )
    raw_tokens = (token.casefold() for token in LEXICAL_TOKEN.findall(normalized))
    expanded = (
        alias
        for token in raw_tokens
        if token not in LEXICAL_STOP_TOKENS
        for alias in LEXICAL_ALIASES.get(token, (token,))
    )
    return tuple(
        dict.fromkeys(
            stem
            for token in expanded
            if len(token) >= 2
            for stem in (lexical_stem(token),)
            if stem
        )
    )


def lexical_evidence(
    query: str,
    *,
    label: str = "",
    summary: str = "",
    text: str = "",
    path: str = "",
) -> LexicalEvidence:
    query_sequence = lexical_terms(query)
    query_terms = frozenset(query_sequence)
    if not query_terms:
        return LexicalEvidence(0.0, 0, 0, False, False)

    label_sequence = lexical_terms(label)
    summary_sequence = lexical_terms(summary)
    text_sequence = lexical_terms(text)
    path_sequence = lexical_terms(path)
    label_terms = frozenset(label_sequence)
    summary_terms = frozenset(summary_sequence)
    text_terms = frozenset(text_sequence)
    path_terms = frozenset(path_sequence)
    all_terms = label_terms | summary_terms | text_terms | path_terms
    matched_terms = len(query_terms & all_terms)
    query_count = len(query_terms)
    normalized_query = " ".join(query_sequence)
    normalized_label = " ".join(label_sequence)
    normalized_text = " ".join(text_sequence)
    normalized_path = " ".join(path_sequence)
    exact_label = query_count >= 2 and (
        normalized_query in normalized_label or normalized_label in normalized_query
    )
    exact_identifier = query_count >= 2 and (
        normalized_query in normalized_text or normalized_query in normalized_path
    )
    score = (
        (matched_terms / query_count) * 0.65
        + (len(query_terms & label_terms) / query_count) * 0.45
        + (len(query_terms & summary_terms) / query_count) * 0.25
        + (len(query_terms & text_terms) / query_count) * 0.15
        + (len(query_terms & path_terms) / query_count) * 0.5
        + (0.15 if matched_terms >= 2 else 0.0)
        + (0.5 if exact_label else 0.0)
        + (0.4 if exact_identifier else 0.0)
    )
    return LexicalEvidence(
        score,
        matched_terms,
        query_count,
        exact_label,
        exact_identifier,
    )


def lexical_query_matches(
    query: str,
    *,
    label: str = "",
    summary: str = "",
    text: str = "",
    path: str = "",
) -> tuple[bool, float]:
    evidence = lexical_evidence(
        query,
        label=label,
        summary=summary,
        text=text,
        path=path,
    )
    normalized_query = unicodedata.normalize("NFKC", query).casefold()
    exact_substring = any(
        normalized_query in unicodedata.normalize("NFKC", value).casefold()
        for value in (label, summary, text, path)
        if normalized_query and value
    )
    return (
        exact_substring
        or (
            evidence.query_terms > 0 and evidence.matched_terms == evidence.query_terms
        ),
        evidence.score + (0.5 if exact_substring else 0.0),
    )
