"""Exact and near-duplicate detection.

Exact duplicates come from a content hash.

Near-duplicates — the "FY24 Budget v3 FINAL (John's copy).xlsx" problem that
makes a RAG corpus answer the same question three different ways — use a
two-stage design:

  1. **Candidate generation.** Each document gets a *bottom-k sketch*: the k
     smallest hashes of its word shingles. Two documents that share a lot of
     content necessarily share many of their smallest hashes, so an inverted
     index over sketch values finds candidates in roughly linear time instead
     of comparing all n^2 pairs.
  2. **Confirmation.** Candidates are scored with an actual Jaccard estimate
     over the sketches, and only pairs above the similarity threshold survive.

The two stages matter. A single fixed-distance threshold on a SimHash cannot
separate "the same document with one clause edited" from "two unrelated
documents on the same template" — short business documents put both in the same
distance band. Measuring similarity directly removes the guesswork, and gives
the report a real percentage to show rather than a bit count.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

_TOKEN = re.compile(r"[a-z0-9]+")
_SHINGLE_SIZE = 4
_MIN_TOKENS_FOR_SKETCH = 40   # below this there are too few shingles to be meaningful
_SKETCH_SIZE = 128            # bottom-k; 128 gives a Jaccard estimate within a few points
_MIN_SHARED_FOR_CANDIDATE = 8 # sketch values two files must share to be worth scoring
_MAX_BUCKET = 300             # ignore degenerate buckets (boilerplate shared estate-wide)

# A document with one clause, header, or paragraph changed lands around 0.80-0.90
# shingle Jaccard. Two different documents built from the same template share
# only boilerplate and land far below. 0.78 sits in the gap.
_NEAR_DUP_MIN_SIMILARITY = 0.78

Sketch = tuple[int, ...]


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _shingle_hashes(text: str) -> set[int]:
    tokens = _TOKEN.findall(text.lower())
    if len(tokens) < _MIN_TOKENS_FOR_SKETCH:
        return set()
    return {
        int.from_bytes(
            hashlib.blake2b(" ".join(tokens[i : i + _SHINGLE_SIZE]).encode(), digest_size=8).digest(),
            "big",
        )
        for i in range(len(tokens) - _SHINGLE_SIZE + 1)
    }


def sketch(text: str) -> Sketch | None:
    """Bottom-k sketch of the text's word shingles, or None when too short."""
    hashes = _shingle_hashes(text)
    if not hashes:
        return None
    return tuple(sorted(hashes)[:_SKETCH_SIZE])


def similarity(a: Sketch, b: Sketch) -> float:
    """Estimate shingle Jaccard from two bottom-k sketches.

    Standard bottom-k estimator: take the k smallest values of the union, and
    measure what fraction of them appear in both sketches. Using the union's
    bottom-k (rather than either sketch's own) is what keeps the estimate
    unbiased when the documents differ in length.
    """
    if not a or not b:
        return 0.0
    set_a, set_b = set(a), set(b)
    k = min(len(a), len(b), _SKETCH_SIZE)
    union_bottom = sorted(set_a | set_b)[:k]
    if not union_bottom:
        return 0.0
    shared = sum(1 for value in union_bottom if value in set_a and value in set_b)
    return shared / len(union_bottom)


def sketch_digest(value: Sketch | None) -> str | None:
    """Short stable identifier for a sketch, for the manifest and BigQuery."""
    if not value:
        return None
    joined = b"".join(v.to_bytes(8, "big") for v in value)
    return hashlib.blake2b(joined, digest_size=8).hexdigest()


@dataclass
class DuplicateGroup:
    kind: str                      # "exact" | "near"
    key: str
    members: list[str] = field(default_factory=list)   # file URIs, keeper first
    keeper: str = ""
    redundant_bytes: int = 0
    similarity: float = 1.0


@dataclass
class DedupeReport:
    exact_groups: list[DuplicateGroup] = field(default_factory=list)
    near_groups: list[DuplicateGroup] = field(default_factory=list)
    # uri -> "unique" | "exact_duplicate" | "near_duplicate" | "keeper"
    status_by_uri: dict[str, str] = field(default_factory=dict)
    exact_redundant_bytes: int = 0
    near_redundant_bytes: int = 0

    @property
    def total_redundant_bytes(self) -> int:
        return self.exact_redundant_bytes + self.near_redundant_bytes

    @property
    def duplicate_file_count(self) -> int:
        return sum(1 for v in self.status_by_uri.values() if v in ("exact_duplicate", "near_duplicate"))


def _pick_keeper(members: Sequence[dict]) -> dict:
    """Keep the newest file; break ties on the shallowest path, then shortest name.

    Depth and name length are a decent proxy for "lives somewhere canonical"
    versus "lives in someone's Downloads folder".
    """
    return min(
        members,
        key=lambda m: (
            -(m.get("modified_ts") or 0),
            m.get("depth", 0),
            len(m.get("rel_path", "")),
        ),
    )


def build_report(records: Iterable[dict]) -> DedupeReport:
    """Group files by content hash, then cluster the survivors by text similarity.

    Each record needs: uri, size_bytes, depth, rel_path, modified_ts,
    content_hash, and (optionally) sketch.
    """
    report = DedupeReport()
    items = list(records)

    # --- exact duplicates -------------------------------------------------
    by_hash: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        digest = item.get("content_hash")
        if digest:
            by_hash[digest].append(item)

    exact_dupe_uris: set[str] = set()
    for digest, members in by_hash.items():
        if len(members) < 2:
            continue
        keeper = _pick_keeper(members)
        redundant = sum(m["size_bytes"] for m in members if m["uri"] != keeper["uri"])
        report.exact_groups.append(
            DuplicateGroup(
                kind="exact",
                key=digest[:16],
                members=[keeper["uri"]] + [m["uri"] for m in members if m["uri"] != keeper["uri"]],
                keeper=keeper["uri"],
                redundant_bytes=redundant,
            )
        )
        report.exact_redundant_bytes += redundant
        report.status_by_uri[keeper["uri"]] = "keeper"
        for member in members:
            if member["uri"] != keeper["uri"]:
                report.status_by_uri[member["uri"]] = "exact_duplicate"
                exact_dupe_uris.add(member["uri"])

    # --- near duplicates --------------------------------------------------
    # Files already identified as an exact copy of something are excluded: they
    # are resolved, and leaving them in would double-count the same redundancy.
    candidates = [
        i for i in items
        if i.get("sketch") and i["uri"] not in exact_dupe_uris
    ]
    by_uri = {i["uri"]: i for i in candidates}

    # Stage 1 - inverted index over sketch values gives candidate pairs.
    buckets: dict[int, list[str]] = defaultdict(list)
    for item in candidates:
        for value in item["sketch"]:
            buckets[value].append(item["uri"])

    shared_counts: dict[tuple[str, str], int] = Counter()
    for members_in_bucket in buckets.values():
        if len(members_in_bucket) < 2 or len(members_in_bucket) > _MAX_BUCKET:
            continue
        for index, left in enumerate(members_in_bucket):
            for right in members_in_bucket[index + 1 :]:
                pair = (left, right) if left < right else (right, left)
                shared_counts[pair] += 1

    # Stage 2 - score the candidates and union the ones that clear the bar.
    parent: dict[str, str] = {i["uri"]: i["uri"] for i in candidates}

    def find(uri: str) -> str:
        while parent[uri] != uri:
            parent[uri] = parent[parent[uri]]
            uri = parent[uri]
        return uri

    def union(a: str, b: str) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    pair_similarity: dict[tuple[str, str], float] = {}
    for pair, shared in shared_counts.items():
        if shared < _MIN_SHARED_FOR_CANDIDATE:
            continue
        score = similarity(by_uri[pair[0]]["sketch"], by_uri[pair[1]]["sketch"])
        if score >= _NEAR_DUP_MIN_SIMILARITY:
            pair_similarity[pair] = score
            union(pair[0], pair[1])

    clusters: dict[str, list[dict]] = defaultdict(list)
    for uri in parent:
        clusters[find(uri)].append(by_uri[uri])

    for root, members in clusters.items():
        if len(members) < 2:
            continue
        keeper = _pick_keeper(members)
        redundant = sum(m["size_bytes"] for m in members if m["uri"] != keeper["uri"])
        scores = [
            similarity(keeper["sketch"], m["sketch"])
            for m in members if m["uri"] != keeper["uri"]
        ]
        report.near_groups.append(
            DuplicateGroup(
                kind="near",
                key=(sketch_digest(keeper["sketch"]) or root)[:16],
                members=[keeper["uri"]] + [m["uri"] for m in members if m["uri"] != keeper["uri"]],
                keeper=keeper["uri"],
                redundant_bytes=redundant,
                similarity=round(sum(scores) / len(scores), 3) if scores else 1.0,
            )
        )
        report.near_redundant_bytes += redundant
        report.status_by_uri.setdefault(keeper["uri"], "keeper")
        for member in members:
            if member["uri"] != keeper["uri"]:
                report.status_by_uri[member["uri"]] = "near_duplicate"

    for item in items:
        report.status_by_uri.setdefault(item["uri"], "unique")

    report.exact_groups.sort(key=lambda g: g.redundant_bytes, reverse=True)
    report.near_groups.sort(key=lambda g: g.redundant_bytes, reverse=True)
    return report
