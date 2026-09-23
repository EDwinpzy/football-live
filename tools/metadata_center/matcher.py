"""Match and merge records that describe the same title."""

from __future__ import annotations

from dataclasses import dataclass

from .model import NormalizedMedia, normalize_text

MATCH_THRESHOLD = 80.0
REVIEW_THRESHOLD = 60.0


def _title_forms(item: NormalizedMedia) -> set[str]:
    values = [item.title, *item.alias_titles()]
    return {normalize_text(value) for value in values if normalize_text(value)}


def _shared_external_id(left: NormalizedMedia, right: NormalizedMedia) -> str | None:
    for provider, value in left.external_ids.items():
        other = right.external_ids.get(provider)
        if other and other == value:
            return f"{provider}:{value}"
    return None


def score_pair(left: NormalizedMedia, right: NormalizedMedia) -> tuple[float, str]:
    """Score 0..100 with the reasons that produced the score."""
    shared = _shared_external_id(left, right)
    if shared and shared not in (f"{left.key}", f"{right.key}"):
        return 100.0, f"shared_external_id:{shared}"

    score = 0.0
    reasons: list[str] = []
    if _title_forms(left) & _title_forms(right):
        score += 40
        reasons.append("title")
    if left.year and right.year:
        if left.year == right.year:
            score += 25
            reasons.append("year")
        elif abs(left.year - right.year) == 1:
            score += 15
            reasons.append("year+-1")
    if left.director_set() & right.director_set():
        score += 20
        reasons.append("director")
    if left.actor_set() & right.actor_set():
        score += 10
        reasons.append("actor")
    if left.runtime_minutes and right.runtime_minutes and abs(left.runtime_minutes - right.runtime_minutes) <= 5:
        score += 5
        reasons.append("runtime")
    return score, "+".join(reasons) or "no_signal"


@dataclass
class MatchOutcome:
    groups: list[list[NormalizedMedia]]
    review: list[tuple[NormalizedMedia, NormalizedMedia, float, str]]


def group_media(items: list[NormalizedMedia]) -> MatchOutcome:
    """Union records that certainly describe the same title.

    Only cross-provider pairs are merged, so a single provider's export can never
    collapse two different titles by accident.
    """
    ordered = list(items)
    parent = list(range(len(ordered)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    review: list[tuple[NormalizedMedia, NormalizedMedia, float, str]] = []
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            left, right = ordered[i], ordered[j]
            if left.provider == right.provider and not _shared_external_id(left, right):
                continue
            score, reason = score_pair(left, right)
            if score >= MATCH_THRESHOLD:
                union(i, j)
            elif score >= REVIEW_THRESHOLD:
                review.append((left, right, score, reason))

    buckets: dict[int, list[NormalizedMedia]] = {}
    for index, item in enumerate(ordered):
        buckets.setdefault(find(index), []).append(item)
    groups = [buckets[key] for key in sorted(buckets)]
    groups.sort(key=lambda group: (group[0].provider, group[0].external_id))
    return MatchOutcome(groups=groups, review=review)


def merge_group(group: list[NormalizedMedia]) -> NormalizedMedia:
    """Fold a matched group into one record.  Detail-rich records win on ties."""
    ordered = sorted(group, key=lambda item: (not item.has_detail, item.provider, item.external_id))
    primary = ordered[0]
    for item in ordered[1:]:
        primary.fill_blanks_from(item)
    identifiers: dict[str, str] = {}
    for item in ordered:
        identifiers.update(item.external_ids)
    primary.external_ids = identifiers
    return primary
