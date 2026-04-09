"""Clip ID resolution — fuzzy matching abbreviated IDs to full IDs.

LLMs frequently abbreviate clip IDs (e.g., "C0073" instead of
"20260330114125_C0073"). This module provides the canonical resolution
logic used after every Phase 2 and Phase 3 LLM call.

Pure domain logic — no I/O, no LLM calls.
"""

from __future__ import annotations


def resolve_clip_id_refs(storyboard, known_ids: set[str]) -> None:
    """Fix abbreviated clip IDs in a storyboard by matching against known IDs.

    Mutates storyboard in-place. Resolves clip_id references in:
    - segments[].clip_id
    - discarded[].clip_id
    - cast[].appears_in[]

    Resolution strategy:
    1. Exact match against known_ids
    2. Suffix match (e.g., "C0073" matches "20260330114125_C0073")
    3. Case-insensitive suffix match
    4. Return as-is if no match found
    """
    suffix_map = _build_suffix_map(known_ids)

    def resolve(clip_id: str) -> str:
        if clip_id in known_ids:
            return clip_id
        if clip_id in suffix_map:
            return suffix_map[clip_id]
        # Try case-insensitive
        for k, v in suffix_map.items():
            if k.lower() == clip_id.lower():
                return v
        return clip_id  # give up, return as-is

    for seg in storyboard.segments:
        seg.clip_id = resolve(seg.clip_id)
    for d in storyboard.discarded:
        d.clip_id = resolve(d.clip_id)
    for c in storyboard.cast:
        c.appears_in = [resolve(cid) for cid in c.appears_in]


def _build_suffix_map(known_ids: set[str]) -> dict[str, str]:
    """Build a map from all possible suffix abbreviations to full IDs.

    For "20260330114125_C0073", creates entries for:
    - "C0073" → "20260330114125_C0073"
    - "114125_C0073" → "20260330114125_C0073"
    - etc.

    First-come wins for ambiguous suffixes.
    """
    suffix_map: dict[str, str] = {}
    for kid in known_ids:
        parts = kid.split("_")
        for i in range(len(parts)):
            suffix = "_".join(parts[i:])
            if suffix not in suffix_map:
                suffix_map[suffix] = kid
    return suffix_map
