"""Protocol definitions for provider-agnostic interfaces.

These Protocols document the target interface for adapter extraction.
They are not yet consumed by runtime dispatch (Phase 3 uses a unified
function with concrete dispatch), but establish the contract that
future adapter classes should satisfy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..config import EditorialProjectPaths


class Phase1Reviewer(Protocol):
    """Reviews clips and returns structured reviews. Provider-agnostic."""

    def run(
        self,
        editorial_paths: EditorialProjectPaths,
        manifest: dict,
        force: bool = False,
        tracer: object | None = None,
        style_supplement: str | None = None,
        only_clip_ids: list[str] | None = None,
        user_context: dict | None = None,
    ) -> tuple[list[dict], list[str]]:
        """Run Phase 1 clip reviews.

        Returns (reviews, failed_clip_ids). Reviews are in original clip order.
        """
        ...
