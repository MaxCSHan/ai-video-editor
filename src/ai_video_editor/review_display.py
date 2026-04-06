"""Display functions for Editorial Director review output.

Renders pre-review scores, per-turn progress, post-review summary with
change diffs, and convergence explanations. Used by both TUI and CLI.
"""

from .config import ReviewBudget
from .models import ReviewLog
from .storyboard_format import format_duration

# Tool category labels
_TOOL_CATEGORY = {
    "screenshot_segment": "INSPECT",
    "get_transcript_excerpt": "INSPECT",
    "get_full_transcript": "INSPECT",
    "get_clip_review": "INSPECT",
    "run_eval_check": "INSPECT",
    "get_unused_footage": "INSPECT",
    "edit_timeline": "EDIT",
    "finalize_review": "DONE",
}

# Human-readable convergence reasons
_CONVERGENCE_MESSAGES = {
    "finalized": "Director completed review",
    "budget": "Turn or cost budget exhausted",
    "timeout": "Wall clock timeout reached",
    "no_response": "Model returned empty response (possible context overflow)",
    "no_tool_calls": "Director finished (no further actions needed)",
    "error": "API error during review",
}

_CONVERGENCE_TIPS = {
    "budget": "Re-run with --review-budget or --review-max-turns to increase limits.",
    "timeout": "The review took too long. Try reducing --review-max-turns.",
    "no_response": "The model may have hit a context limit. Try re-running.",
    "error": "Check your API key and network connection, then retry.",
}


def print_pre_review(
    eval_summary: str,
    seg_count: int,
    duration_sec: float,
    budget: ReviewBudget,
) -> None:
    """Print the pre-review overview box with eval scores and budget."""
    print("  +-- Director Review ----------------------------------------")
    print(f"  | {seg_count} segments, {format_duration(duration_sec)}")
    print(
        f"  | Budget: {budget.max_turns} turns, "
        f"{budget.max_fixes} fixes, ${budget.max_cost_usd:.2f}"
    )
    print("  |")
    for line in eval_summary.strip().split("\n"):
        print(f"  | {line}")
    print("  +------------------------------------------------------------")
    print()


def print_turn(
    turn: int,
    tool_name: str,
    tool_args: dict,
    result_data: str,
    budget: ReviewBudget,
) -> None:
    """Print a single turn status line with category and detail."""
    category = _TOOL_CATEGORY.get(tool_name, "???")

    if tool_name == "screenshot_segment":
        idx = tool_args.get("segment_index", "?")
        detail = f"screenshot_segment({idx})"
    elif tool_name == "get_transcript_excerpt":
        clip = tool_args.get("clip_id", "?")
        s = tool_args.get("start_sec", 0)
        e = tool_args.get("end_sec", 0)
        detail = f"get_transcript({clip}, {s:.0f}-{e:.0f}s)"
    elif tool_name == "get_full_transcript":
        clip = tool_args.get("clip_id", "?")
        detail = f"get_full_transcript({clip})"
    elif tool_name == "get_clip_review":
        clip = tool_args.get("clip_id", "?")
        detail = f"get_clip_review({clip})"
    elif tool_name == "run_eval_check":
        dim = tool_args.get("dimension", "?")
        detail = f"eval_check({dim})"
    elif tool_name == "get_unused_footage":
        clip = tool_args.get("clip_id", "")
        detail = f"get_unused_footage({clip})" if clip else "get_unused_footage(all)"
    elif tool_name == "edit_timeline":
        action = tool_args.get("action", "?")
        reverted = "reverted" in result_data.lower()
        mark = " (reverted)" if reverted else ""
        if action == "update":
            idx = tool_args.get("segment_index", "?")
            fields = ", ".join(tool_args.get("updated_fields", {}).keys())
            detail = f"update segment {idx}: {fields}{mark}"
        elif action == "add":
            clip = tool_args.get("clip_id", "?")
            pos = tool_args.get("position", "?")
            detail = f"add {clip} at position {pos}{mark}"
        elif action == "remove":
            idx = tool_args.get("segment_index", "?")
            detail = f"remove segment {idx}{mark}"
        elif action == "move":
            idx = tool_args.get("segment_index", "?")
            to = tool_args.get("to_position", "?")
            detail = f"move segment {idx} -> {to}{mark}"
        else:
            detail = f"edit_timeline({action}){mark}"
    elif tool_name == "finalize_review":
        passed = tool_args.get("passed", False)
        detail = f"finalize_review -- {'PASSED' if passed else 'FAILED'}"
    else:
        detail = tool_name

    print(f"  Turn {turn:<3} {category:<8} {detail}")


def print_post_review(review_log: ReviewLog, had_changes: bool) -> None:
    """Print the post-review summary box with verdict, changes, and score deltas."""
    reason = review_log.convergence_reason
    is_finalized = reason == "finalized" or reason == "no_tool_calls"

    # Header
    if is_finalized and review_log.final_verdict:
        verdict = "PASSED" if review_log.final_verdict.passed else "NEEDS WORK"
        header = f"Review Complete -- {verdict}"
    elif is_finalized:
        header = "Review Complete"
    else:
        header = "Review Stopped"

    print()
    print(f"  +-- {header} " + "-" * max(0, 46 - len(header)))

    # Stats line
    print(
        f"  | {review_log.total_turns} turns, "
        f"{review_log.total_fixes} fixes, "
        f"${review_log.total_cost_usd:.3f}, "
        f"{review_log.total_duration_sec:.1f}s"
    )

    # Convergence reason (for non-finalized exits)
    if not is_finalized:
        msg = _CONVERGENCE_MESSAGES.get(reason, reason)
        print(f"  | Reason: {msg}")

    # Verdict summary from the agent
    if review_log.final_verdict and review_log.final_verdict.summary:
        summary = review_log.final_verdict.summary
        # Wrap long summaries
        if len(summary) > 60:
            summary = summary[:60] + "..."
        print(f"  | {summary}")

    # Changes
    if review_log.changes:
        print("  |")
        counts: dict[str, int] = {}
        for c in review_log.changes:
            counts[c.change_type] = counts.get(c.change_type, 0) + 1
        summary_parts = []
        for ct, n in counts.items():
            label = {"update": "updated", "add": "added", "remove": "removed", "move": "moved"}
            summary_parts.append(f"{n} {label.get(ct, ct)}")
        print(f"  | Changes ({', '.join(summary_parts)}):")

        for change in review_log.changes:
            if change.change_type == "update":
                for field in change.fields_changed:
                    bval = change.before.get(field, "?")
                    aval = change.after.get(field, "?")
                    if isinstance(bval, float):
                        bval = f"{bval:.1f}"
                    if isinstance(aval, float):
                        aval = f"{aval:.1f}"
                    if isinstance(bval, str) and len(bval) > 40:
                        bval = bval[:40] + "..."
                    if isinstance(aval, str) and len(aval) > 40:
                        aval = aval[:40] + "..."
                    print(f"  |   Seg {change.segment_index:<3} {field:<14} {bval} -> {aval}")
            elif change.change_type == "add":
                clip = change.after.get("clip_id", "?")
                in_s = change.after.get("in_sec", 0)
                out_s = change.after.get("out_sec", 0)
                purpose = change.after.get("purpose", "?")
                print(
                    f"  |   Seg {change.segment_index:<3} ADDED    "
                    f"{clip} {in_s:.1f}-{out_s:.1f}s ({purpose})"
                )
            elif change.change_type == "remove":
                clip = change.before.get("clip_id", "?")
                in_s = change.before.get("in_sec", 0)
                out_s = change.before.get("out_sec", 0)
                print(
                    f"  |   Seg {change.segment_index:<3} REMOVED  ({clip} {in_s:.1f}-{out_s:.1f}s)"
                )
            elif change.change_type == "move":
                from_pos = change.before.get("position", "?")
                to_pos = change.after.get("position", "?")
                print(f"  |   Seg {change.segment_index:<3} MOVED    {from_pos} -> {to_pos}")
    elif not had_changes:
        print("  |")
        print("  | No changes -- storyboard passed review as-is.")

    # Score comparison
    if review_log.eval_before and review_log.eval_after and review_log.changes:
        before_lines = review_log.eval_before.strip().split("\n")
        after_lines = review_log.eval_after.strip().split("\n")
        if before_lines != after_lines:
            print("  |")
            print("  | Scores:  before -> after")
            for bl, al in zip(before_lines, after_lines):
                if bl != al:
                    print(f"  |   {bl}  ->  {al}")

    # Tip for non-finalized exits
    tip = _CONVERGENCE_TIPS.get(reason)
    if tip:
        print("  |")
        print(f"  | Tip: {tip}")

    print("  +------------------------------------------------------------")
    print()


def print_change_diff(review_log: ReviewLog) -> None:
    """Print a detailed diff of all changes for the 'Show full diff' option."""
    if not review_log.changes:
        print("  No changes to show.")
        return

    print()
    print("  === Detailed Change Diff ===")
    print()

    for i, change in enumerate(review_log.changes):
        label = change.change_type.upper()
        print(f"  [{i + 1}] {label} segment {change.segment_index}")

        if change.change_type == "update":
            for field in change.fields_changed:
                bval = change.before.get(field, "?")
                aval = change.after.get(field, "?")
                if isinstance(bval, float):
                    bval = f"{bval:.2f}"
                if isinstance(aval, float):
                    aval = f"{aval:.2f}"
                print(f"       {field}: {bval}")
                print(f"            -> {aval}")
        elif change.change_type == "add":
            for k, v in change.after.items():
                if isinstance(v, float):
                    v = f"{v:.2f}"
                print(f"       {k}: {v}")
        elif change.change_type == "remove":
            for k, v in change.before.items():
                if isinstance(v, float):
                    v = f"{v:.2f}"
                print(f"       {k}: {v}")
        elif change.change_type == "move":
            print(f"       from position: {change.before.get('position', '?')}")
            print(f"       to position:   {change.after.get('position', '?')}")
        print()


def print_proposal(proposal_text: str) -> None:
    """Print a proposed edit plan from the director for user confirmation."""
    print()
    print("  +-- Director's Proposal ------------------------------------")
    for line in proposal_text.strip().split("\n"):
        print(f"  | {line}")
    print("  +------------------------------------------------------------")
    print()
