"""Language-aware metrics used by the model-comparison benchmark."""

from __future__ import annotations

import random
import re
import statistics
import unicodedata
from collections.abc import Sequence
from typing import Any


NORMALIZATION_PROFILES = {"latin_wer", "han_cer", "char_cer", "whitespace_wer"}


def normalize_text(text: str, profile: str) -> str:
    if profile not in NORMALIZATION_PROFILES:
        raise ValueError(f"Unknown text normalization profile: {profile}")
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(char for char in text if not unicodedata.category(char).startswith("P"))
    text = re.sub(r"\s+", " ", text).strip()
    if profile in {"han_cer", "char_cer"}:
        return text.replace(" ", "")
    return text


def edit_distance(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for row, ref_token in enumerate(reference, start=1):
        current = [row]
        for column, hyp_token in enumerate(hypothesis, start=1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (ref_token != hyp_token),
            ))
        previous = current
    return previous[-1]


def error_rate(references: Sequence[str], hypotheses: Sequence[str], profile: str) -> float | None:
    if len(references) != len(hypotheses) or not references:
        return None
    errors = total = 0
    for reference, hypothesis in zip(references, hypotheses, strict=True):
        reference = normalize_text(reference, profile)
        hypothesis = normalize_text(hypothesis, profile)
        character_metric = profile in {"han_cer", "char_cer"}
        ref_tokens = list(reference) if character_metric else reference.split()
        hyp_tokens = list(hypothesis) if character_metric else hypothesis.split()
        errors += edit_distance(ref_tokens, hyp_tokens)
        total += len(ref_tokens)
    return errors / total if total else (0.0 if errors == 0 else None)


def _error_counts(reference: str, hypothesis: str, profile: str) -> tuple[int, int]:
    reference = normalize_text(reference, profile)
    hypothesis = normalize_text(hypothesis, profile)
    character_metric = profile in {"han_cer", "char_cer"}
    ref_tokens = list(reference) if character_metric else reference.split()
    hyp_tokens = list(hypothesis) if character_metric else hypothesis.split()
    return edit_distance(ref_tokens, hyp_tokens), len(ref_tokens)


def bootstrap_interval(
    references: Sequence[str], hypotheses: Sequence[str], profile: str,
    *, samples: int = 1000, seed: int = 314159,
) -> dict[str, float | None]:
    score = error_rate(references, hypotheses, profile)
    if score is None or len(references) < 2:
        return {"lower_95": None, "upper_95": None}
    rng = random.Random(seed)
    counts = [_error_counts(reference, hypothesis, profile) for reference, hypothesis in zip(references, hypotheses, strict=True)]
    scores = []
    for _ in range(samples):
        indices = [rng.randrange(len(references)) for _ in references]
        errors = sum(counts[index][0] for index in indices)
        total = sum(counts[index][1] for index in indices)
        if total:
            scores.append(errors / total)
    scores.sort()
    if not scores:
        return {"lower_95": None, "upper_95": None}
    return {
        "lower_95": round(scores[int(0.025 * (len(scores) - 1))], 6),
        "upper_95": round(scores[int(0.975 * (len(scores) - 1))], 6),
    }


def quality_summary(
    records: list[dict[str, str]], profile: str, *, bootstrap_samples: int = 1000,
) -> dict[str, Any]:
    references = [record["reference"] for record in records]
    hypotheses = [record["hypothesis"] for record in records]
    metric = "cer" if profile in {"han_cer", "char_cer"} else "wer"
    score = error_rate(references, hypotheses, profile)
    return {
        "metric": metric,
        "value": round(score, 6) if score is not None else None,
        "confidence_interval_95": bootstrap_interval(
            references, hypotheses, profile, samples=bootstrap_samples,
        ),
        "sample_count": len(records),
    }


def numeric_summary(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"median": None, "p50": None, "p95": None, "p99": None, "min": None, "max": None}
    ordered = sorted(values)
    def percentile(percent: float) -> float:
        index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percent + 0.5)))
        return round(ordered[index], 6)
    return {
        "median": round(statistics.median(values), 6),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }
