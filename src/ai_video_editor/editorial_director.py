"""Editorial Director — tool-using ReAct agent for storyboard self-review.

The director reviews a Phase 2 storyboard using multimodal inspection (contact
strip + on-demand segment grids), computable eval scores, transcript excerpts,
and clip reviews. It applies targeted fixes and signals completion via
finalize_review.

The harness provides: tool dispatch, budget tracking, micro-compact context
management, wall clock timeout, and regression protection on fixes.
"""

import logging
import time
from pathlib import Path

from .config import ReviewBudget, ReviewConfig
from .director_tools import (
    DirectorToolContext,
    FIX_TOOLS,
    TOOL_HANDLERS,
    execute_proposal_batch,
)
from .models import (
    ChatMessage,
    ChatSession,
    EditorialStoryboard,
    ReviewIteration,
    ReviewLog,
    ReviewVerdict,
)

log = logging.getLogger(__name__)


def _load_transcripts(clips_dir: Path, clip_ids: set[str]) -> dict[str, list[dict]]:
    """Load transcript segments for the given clip IDs."""
    import json

    from .versioning import resolve_transcript_path

    transcripts: dict[str, list[dict]] = {}
    for clip_id in clip_ids:
        clip_root = clips_dir / clip_id
        transcript_path = resolve_transcript_path(clip_root)
        if transcript_path:
            try:
                data = json.loads(transcript_path.read_text())
                transcripts[clip_id] = data.get("segments", [])
            except Exception:
                pass
    return transcripts


def _build_filming_timeline(clips_dir: Path, clip_reviews: list[dict]) -> str | None:
    """Build a filming timeline string from the master manifest.

    Also sorts *clip_reviews* in-place to chronological filming order.
    Returns None if no manifest or no creation times are available.
    """
    import json

    manifest_file = clips_dir.parent / "manifest.json"
    if not manifest_file.exists():
        return None

    manifest_data = json.loads(manifest_file.read_text())
    creation_times = {c["clip_id"]: c.get("creation_time") for c in manifest_data.get("clips", [])}
    clip_reviews.sort(
        key=lambda r: (creation_times.get(r.get("clip_id"), "") or "", r.get("clip_id", ""))
    )
    timeline_lines = []
    for i, r in enumerate(clip_reviews, 1):
        cid = r.get("clip_id", "unknown")
        ct = creation_times.get(cid)
        timeline_lines.append(f"  {i}. {cid} — filmed {ct}" if ct else f"  {i}. {cid}")
    return "\n".join(timeline_lines)


def _build_gemini_contents(parts: list[dict]) -> list:
    """Convert our internal content parts to Gemini API format."""
    from google.genai import types

    gemini_parts = []
    for part in parts:
        if part["type"] == "text":
            gemini_parts.append(types.Part.from_text(text=part["text"]))
        elif part["type"] == "image":
            gemini_parts.append(types.Part.from_bytes(data=part["data"], mime_type="image/jpeg"))
    return [types.Content(role="user", parts=gemini_parts)]


def _build_tool_result_contents(tool_results: list[dict]) -> list:
    """Build Gemini function response content from tool results."""
    from google.genai import types

    parts = []
    for tr in tool_results:
        name = tr["name"]
        result = tr["result"]
        if result["type"] == "image":
            # For image results, describe them as text + inline image
            # Gemini function responses are text-only, so we return as a
            # separate user message with the image
            parts.append(
                types.Part.from_function_response(
                    name=name,
                    response={"result": "Image returned — see attached thumbnail grid."},
                )
            )
        else:
            parts.append(
                types.Part.from_function_response(
                    name=name,
                    response={"result": result["data"]},
                )
            )
    return parts


def _build_image_parts_from_results(tool_results: list[dict]) -> list:
    """Extract image parts from tool results for multimodal follow-up."""
    from google.genai import types

    parts = []
    for tr in tool_results:
        result = tr["result"]
        if result["type"] == "image":
            parts.append(types.Part.from_bytes(data=result["data"], mime_type="image/jpeg"))
    return parts


def _format_current_segments(storyboard) -> str:
    """Format the current segment list for injection into the conversation."""
    from .storyboard_format import format_duration

    lines = []
    for seg in storyboard.segments:
        line = (
            f"Seg {seg.index}: [{seg.clip_id}] {format_duration(seg.in_sec)}-"
            f"{format_duration(seg.out_sec)} | {seg.purpose} | "
            f"{seg.audio_note or 'no audio note'}"
        )
        if seg.description:
            line += f" — {seg.description}"
        lines.append(line)
    return "\n".join(lines)


def _micro_compact(messages: list, keep_recent_turns: int = 5) -> None:
    """Clear old tool results to manage context window.

    Replaces function response content in messages older than keep_recent_turns
    with a compact placeholder. Modifies messages in-place.

    A "turn" = one assistant message + one user (tool results) message = 2 messages.
    The first message (initial overview prompt) is never compacted.
    """
    from google.genai import types

    # Count turns: each turn after the initial prompt is 2 messages (assistant + tool response).
    # messages[0] is the initial overview prompt — never compact it.
    # Protect the last keep_recent_turns * 2 messages plus message[0].
    protected_tail = keep_recent_turns * 2
    if len(messages) <= 1 + protected_tail:
        return

    cutoff = len(messages) - protected_tail

    for i in range(1, cutoff):  # skip messages[0] (initial overview)
        msg = messages[i]
        if not isinstance(msg, types.Content):
            continue
        if msg.role != "user":
            continue
        # Check if this contains function responses
        has_fn_response = any(
            hasattr(p, "function_response") and p.function_response for p in (msg.parts or [])
        )
        if has_fn_response:
            # Replace with compact placeholder
            messages[i] = types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text="[previous tool results cleared — use tools to re-fetch if needed]"
                    )
                ],
            )


def run_editorial_review(
    storyboard: EditorialStoryboard,
    clip_reviews: list[dict],
    user_context: dict | None,
    clips_dir: Path,
    review_config: ReviewConfig,
    tracer=None,
    interactive: bool = False,
    turn_callback=None,
    style_guidelines: str | None = None,
) -> tuple[EditorialStoryboard, ReviewLog]:
    """Tool-using agent loop for editorial review.

    Args:
        turn_callback: Optional callable(turn, tool_name, tool_args, result_data, budget)
            called after each tool execution for real-time display.
        style_guidelines: Optional style-specific assembly guidelines from the project's
            StylePreset (phase2_supplement). Appended to the system prompt.

    Returns (storyboard, review_log) tuple.
    """
    from google.genai import types

    from .director_prompts import (
        build_eval_summary,
        build_initial_message,
        build_system_prompt,
        get_tool_declarations,
    )
    from .infra.gemini_client import GeminiClient
    from .render import generate_contact_strip
    from .tracing import estimate_cost, otel_phase_span, otel_session_span, otel_tool_span

    budget = ReviewBudget.from_config(review_config)

    # Load transcripts for ALL clips (director may add footage from unused clips)
    clip_ids = {r.get("clip_id", "") for r in clip_reviews} - {""}
    transcripts = _load_transcripts(clips_dir, clip_ids)

    # Build tool context
    tool_ctx = DirectorToolContext(
        storyboard=storyboard,
        clip_reviews=clip_reviews,
        clips_dir=clips_dir,
        user_context=user_context,
        transcripts_by_clip=transcripts,
        budget=budget,
    )

    # Sort clip reviews chronologically and build filming timeline
    filming_timeline = _build_filming_timeline(clips_dir, clip_reviews)

    # Pre-compute overview data (free)
    eval_summary = build_eval_summary(storyboard, clip_reviews, user_context, transcripts)

    log.info("Generating contact strip for visual review...")
    contact_strip = generate_contact_strip(storyboard, clips_dir)

    # Build prompts
    system_prompt = build_system_prompt(budget, style_guidelines=style_guidelines)
    initial_parts = build_initial_message(
        storyboard=storyboard,
        eval_summary=eval_summary,
        contact_strip_image=contact_strip,
        user_context=user_context,
        budget=budget,
        clip_reviews=clip_reviews,
        filming_timeline=filming_timeline,
    )

    # Initialize messages with the initial overview
    messages = _build_gemini_contents(initial_parts)

    # Set up Gemini client with function calling
    client = GeminiClient.from_env()

    tool_declarations = get_tool_declarations()
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=0.2,
        tools=[
            types.Tool(
                function_declarations=[types.FunctionDeclaration(**td) for td in tool_declarations]
            )
        ],
    )

    # Review log for audit trail
    review_log = ReviewLog(eval_before=eval_summary)
    start_time = time.monotonic()

    # Session ID for OTel tracing — groups all turns in Phoenix Sessions tab
    trace_session_id = f"review_{int(time.time())}"
    _session_cm = otel_session_span(
        "director_review",
        session_id=trace_session_id,
        attributes={
            "vx.segment_count": len(storyboard.segments),
            "vx.budget.max_turns": budget.max_turns,
            "vx.budget.max_fixes": budget.max_fixes,
            "vx.model": review_config.model,
        },
    )
    _session_span = _session_cm.__enter__()

    while budget.can_continue():
        elapsed = time.monotonic() - start_time
        if elapsed > review_config.wall_clock_timeout_sec:
            log.warning("Review wall clock timeout (%.1fs)", elapsed)
            review_log.convergence_reason = "timeout"
            break

        # Check if this should be the finalization turn
        near_budget_limit = (
            budget.turns_used >= budget.max_turns - 2
            or budget.fixes_used >= budget.max_fixes - 1
            or budget.cost_used_usd >= budget.max_cost_usd * 0.85
        )
        turn_config = config
        if near_budget_limit:
            # Inject budget warning and restrict to inspect + finalize tools
            messages.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(
                            text=f"[BUDGET WARNING] {budget.remaining_summary()}. "
                            "Wrap up now — call finalize_review with your verdict."
                        )
                    ],
                )
            )
            # Create restricted tool config (finalize + inspect only)
            finalize_declarations = [
                td
                for td in tool_declarations
                if td["name"]
                in (
                    "finalize_review",
                    "run_eval_check",
                    "screenshot_segment",
                    "get_transcript_excerpt",
                    "get_full_transcript",
                    "get_clip_review",
                    "get_unused_footage",
                )
            ]
            turn_config = types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.2,
                tools=[
                    types.Tool(
                        function_declarations=[
                            types.FunctionDeclaration(**td) for td in finalize_declarations
                        ]
                    )
                ],
            )

        # Call Gemini with tools
        turn_start = time.monotonic()
        try:
            with otel_phase_span("editorial_review", stage="director", provider="gemini"):
                response = client.raw.models.generate_content(
                    model=review_config.model,
                    contents=messages,
                    config=turn_config,
                )
        except Exception as e:
            log.error("Gemini API error during review: %s", e)
            review_log.convergence_reason = "error"
            break

        budget.turns_used += 1
        turn_duration = time.monotonic() - turn_start

        # Estimate cost from usage metadata
        turn_cost = 0.0
        input_tokens = 0
        output_tokens = 0
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            um = response.usage_metadata
            input_tokens = getattr(um, "prompt_token_count", 0) or 0
            output_tokens = getattr(um, "candidates_token_count", 0) or 0
            turn_cost = estimate_cost(review_config.model, input_tokens, output_tokens)
            budget.cost_used_usd += turn_cost

        # Record trace for project-level tracking
        if tracer:
            from .tracing import LLMCallTrace

            trace = LLMCallTrace(
                phase="editorial_review",
                provider="gemini",
                model=review_config.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                estimated_cost_usd=turn_cost,
                duration_sec=round(turn_duration, 2),
            )
            tracer.record(trace)

        # Extract response parts
        candidate = response.candidates[0] if response.candidates else None
        if not candidate or not candidate.content or not candidate.content.parts:
            log.info("Empty response from director — ending review")
            review_log.convergence_reason = "no_response"
            break

        # Add assistant response to messages
        messages.append(candidate.content)

        # Check for function calls
        function_calls = [
            p for p in candidate.content.parts if hasattr(p, "function_call") and p.function_call
        ]

        if not function_calls:
            # No tool calls = agent is done (implicit finalize)
            text_parts = [p.text for p in candidate.content.parts if hasattr(p, "text") and p.text]
            log.info("Director ended with text (no tool calls): %s", " ".join(text_parts)[:200])
            review_log.convergence_reason = "no_tool_calls"

            review_log.iterations.append(
                ReviewIteration(
                    turn=budget.turns_used,
                    result_summary="Text response (implicit finalize)",
                    cost_usd=turn_cost,
                    duration_sec=round(turn_duration, 2),
                )
            )
            break

        # Execute function calls
        tool_results = []
        for fc in function_calls:
            call_name = fc.function_call.name
            call_args = dict(fc.function_call.args) if fc.function_call.args else {}

            handler = TOOL_HANDLERS.get(call_name)
            if not handler:
                tool_results.append(
                    {
                        "name": call_name,
                        "result": {"type": "text", "data": f"Unknown tool: {call_name}"},
                    }
                )
                continue

            with otel_tool_span(call_name, call_args) as tool_span:
                try:
                    result = handler(tool_ctx, **call_args)
                except Exception as e:
                    log.error("Tool %s failed: %s", call_name, e)
                    result = {"type": "text", "data": f"Tool error: {e}"}
                if tool_span:
                    tool_span.set_attribute("vx.tool.result_type", result["type"])
                    if result["type"] == "text":
                        tool_span.set_attribute("output.value", result["data"][:300])

            tool_results.append({"name": call_name, "result": result})

            # Track fix budget + mid-turn budget check
            if call_name in FIX_TOOLS:
                budget.fixes_used += 1
                if not budget.can_continue():
                    # Budget exhausted mid-turn — skip remaining tool calls
                    remaining = function_calls[function_calls.index(fc) + 1 :]
                    for skip_fc in remaining:
                        skip_name = skip_fc.function_call.name
                        tool_results.append(
                            {
                                "name": skip_name,
                                "result": {
                                    "type": "text",
                                    "data": "Budget exhausted — call skipped. "
                                    "Call finalize_review to complete.",
                                },
                            }
                        )
                    break

            # Log iteration
            review_log.iterations.append(
                ReviewIteration(
                    turn=budget.turns_used,
                    tool_name=call_name,
                    tool_args=call_args,
                    result_summary=result["data"][:200] if result["type"] == "text" else "image",
                    cost_usd=turn_cost / max(len(function_calls), 1),
                    duration_sec=round(turn_duration / max(len(function_calls), 1), 2),
                )
            )

            if turn_callback:
                result_data = result.get("data", "") if result["type"] == "text" else "(image)"
                turn_callback(budget.turns_used, call_name, call_args, result_data, budget)

            # Check for finalize
            if call_name == "finalize_review":
                review_log.convergence_reason = "finalized"
                break

        # Build function response message
        fn_response_parts = _build_tool_result_contents(tool_results)
        image_parts = _build_image_parts_from_results(tool_results)

        # Combine function responses and any images into user message
        all_parts = fn_response_parts + image_parts
        messages.append(types.Content(role="user", parts=all_parts))

        # Check if finalized
        if tool_ctx.finalized:
            break

        # Micro-compact old messages to manage context
        _micro_compact(messages, keep_recent_turns=5)

    # Finalize review log
    review_log.total_turns = budget.turns_used
    review_log.total_fixes = budget.fixes_used
    review_log.total_cost_usd = round(budget.cost_used_usd, 4)
    review_log.total_duration_sec = round(time.monotonic() - start_time, 2)

    if not review_log.convergence_reason:
        review_log.convergence_reason = "budget"

    if tool_ctx.finalized:
        review_log.final_verdict = ReviewVerdict(
            passed=tool_ctx.final_passed,
            summary=tool_ctx.final_summary,
        )

    # Compute post-review eval summary and copy changes from tool context
    review_log.eval_after = build_eval_summary(
        tool_ctx.storyboard, clip_reviews, user_context, transcripts
    )
    review_log.changes = list(tool_ctx.segment_changes)

    log.info(
        "Review complete: %s, %d turns, %d fixes, $%.4f, %.1fs",
        review_log.convergence_reason,
        review_log.total_turns,
        review_log.total_fixes,
        review_log.total_cost_usd,
        review_log.total_duration_sec,
    )

    # Close the OTel session span, recording final stats
    if _session_span:
        _session_span.set_attribute("vx.total_turns", review_log.total_turns)
        _session_span.set_attribute("vx.total_fixes", review_log.total_fixes)
        _session_span.set_attribute("vx.convergence_reason", review_log.convergence_reason or "")
        _session_span.set_attribute("output.value", review_log.convergence_reason or "")
    _session_cm.__exit__(None, None, None)

    return tool_ctx.storyboard, review_log


_APPROVAL_PATTERNS = {
    "yes",
    "y",
    "yep",
    "yeah",
    "ok",
    "okay",
    "sure",
    "go ahead",
    "do it",
    "proceed",
    "approved",
    "go",
    "yes proceed",
    "yes please",
    "looks good",
    "lgtm",
    "ship it",
}
_REJECTION_PATTERNS = {
    "no",
    "n",
    "nope",
    "cancel",
    "never mind",
    "nevermind",
    "stop",
    "abort",
    "undo",
    "scratch that",
    "forget it",
}


def _is_approval(text: str) -> bool:
    """Check if user input is an approval of a pending proposal."""
    normalized = text.strip().lower().rstrip("!.,")
    return normalized in _APPROVAL_PATTERNS


def _is_rejection(text: str) -> bool:
    """Check if user input is a rejection of a pending proposal."""
    normalized = text.strip().lower().rstrip("!.,")
    return normalized in _REJECTION_PATTERNS


def run_director_chat(
    storyboard: EditorialStoryboard,
    clip_reviews: list[dict],
    user_context: dict | None,
    clips_dir: Path,
    review_config: ReviewConfig,
    tracer=None,
    style_guidelines: str | None = None,
    turn_callback=None,
    input_callback=None,
    print_fn=None,
    editorial_paths=None,
    session: ChatSession | None = None,
) -> tuple[EditorialStoryboard, ReviewLog]:
    """Conversational director loop — user drives, model executes.

    Args:
        input_callback: Callable that returns user input string, or None to exit.
        print_fn: Callable(str) for printing director responses. Defaults to print().
        turn_callback: Same as run_editorial_review — for tool execution display.
        editorial_paths: Project paths (required for auto-save versioning).
        session: ChatSession for persistence. If provided, auto-saves after each edit.

    Returns (storyboard, review_log) tuple.
    """
    from google.genai import types

    from .director_prompts import (
        build_chat_system_prompt,
        build_eval_summary,
        build_initial_message,
        get_chat_tool_declarations,
    )
    from .infra.gemini_client import GeminiClient
    from .render import generate_contact_strip
    from .tracing import estimate_cost, otel_phase_span, otel_session_span, otel_tool_span

    if print_fn is None:
        print_fn = print

    # Chat mode is user-driven — use generous budget defaults.
    # The user controls when to stop via "done"/"exit", so tight limits
    # just cause frustration. Override conservative auto-review defaults.
    budget = ReviewBudget.from_config(review_config)
    budget.max_turns = max(budget.max_turns, 200)
    budget.max_fixes = max(budget.max_fixes, 100)
    budget.max_cost_usd = max(budget.max_cost_usd, 2.0)

    # Load transcripts for ALL clips
    clip_ids = {r.get("clip_id", "") for r in clip_reviews} - {""}
    transcripts = _load_transcripts(clips_dir, clip_ids)

    # Build tool context
    tool_ctx = DirectorToolContext(
        storyboard=storyboard,
        clip_reviews=clip_reviews,
        clips_dir=clips_dir,
        user_context=user_context,
        transcripts_by_clip=transcripts,
        budget=budget,
    )

    # Sort clip reviews chronologically and build filming timeline
    filming_timeline = _build_filming_timeline(clips_dir, clip_reviews)

    # Pre-compute overview
    eval_summary = build_eval_summary(storyboard, clip_reviews, user_context, transcripts)
    contact_strip = generate_contact_strip(storyboard, clips_dir)

    # Build initial context (same as auto-review)
    system_prompt = build_chat_system_prompt(budget, style_guidelines=style_guidelines)
    initial_parts = build_initial_message(
        storyboard=storyboard,
        eval_summary=eval_summary,
        contact_strip_image=contact_strip,
        user_context=user_context,
        budget=budget,
        clip_reviews=clip_reviews,
        filming_timeline=filming_timeline,
    )

    # Resume or fresh start
    if session and session.messages:
        messages = _rebuild_messages_from_session(session, initial_parts)
        # Budget resets each conversation round — the session tracks cumulative
        # edits in session.total_edits for display, but each round gets a fresh budget.
        print_fn(
            f"\n  Director: Resuming session {session.session_id} "
            f"(v{session.storyboard_version}, {session.total_edits} edits so far). "
            f"What would you like to work on?\n"
        )
    else:
        messages = _build_gemini_contents(initial_parts)
        print_fn(
            f"\n  Director: I've reviewed your storyboard — "
            f"{len(storyboard.segments)} segments, "
            f"{storyboard.total_segments_duration:.0f}s. "
            f"What would you like to work on?\n"
        )

    # Set up Gemini with chat tools
    client = GeminiClient.from_env()
    tool_declarations = get_chat_tool_declarations()
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=0.3,
        tools=[
            types.Tool(
                function_declarations=[types.FunctionDeclaration(**td) for td in tool_declarations]
            )
        ],
    )

    review_log = ReviewLog(eval_before=eval_summary)
    start_time = time.monotonic()

    # Session ID for OTel tracing — groups all turns in Phoenix Sessions tab
    trace_session_id = session.session_id if session else f"chat_{int(time.time())}"
    _session_cm = otel_session_span(
        "director_chat",
        session_id=trace_session_id,
        attributes={
            "vx.segment_count": len(storyboard.segments),
            "vx.budget.max_turns": budget.max_turns,
            "vx.budget.max_fixes": budget.max_fixes,
            "vx.model": review_config.model,
        },
    )
    _session_span = _session_cm.__enter__()

    while budget.can_continue():
        # Get user input
        user_input = input_callback() if input_callback else None
        if user_input is None or user_input.strip().lower() in ("done", "save", "exit", "quit"):
            review_log.convergence_reason = "user_exit"
            break

        # Append user message
        user_content = types.Content(role="user", parts=[types.Part.from_text(text=user_input)])
        messages.append(user_content)

        # Persist user message in session
        if session and editorial_paths:
            session.messages.append(_serialize_gemini_content(user_content))
            save_session(session, editorial_paths)

        # Handle pending proposal approval/rejection
        if tool_ctx.pending_proposal is not None:
            if _is_approval(user_input):
                edit_count = len(tool_ctx.pending_proposal or [])
                with otel_tool_span(
                    "execute_proposal_batch",
                    {"edit_count": edit_count},
                ) as batch_span:
                    batch_results = execute_proposal_batch(tool_ctx)
                    # Summarize results
                    success_count = sum(1 for r in batch_results if r.get("ok", False))
                    total = len(batch_results)
                    is_reverted = any(
                        "batch reverted" in r.get("data", "").lower() for r in batch_results
                    )
                    if batch_span:
                        batch_span.set_attribute("vx.batch.success_count", success_count)
                        batch_span.set_attribute("vx.batch.total", total)
                        batch_span.set_attribute("vx.batch.reverted", is_reverted)

                if is_reverted:
                    summary_text = batch_results[0]["data"]
                    print_fn(f"\n  Director: {summary_text}\n")
                else:
                    fail_count = total - success_count
                    header = f"Applied {success_count}/{total} edits"
                    if fail_count:
                        header += f" ({fail_count} failed)"
                    summary_lines = [f"{header}:"]
                    for r in batch_results:
                        mark = "OK" if r.get("ok") else "FAIL"
                        summary_lines.append(f"  [{mark}] {r['data'][:120]}")
                    summary_text = "\n".join(summary_lines)
                    print_fn(f"\n  Director: {summary_text}\n")

                    # Budget tracking — a batch counts as 1 fix (one user-approved action)
                    budget.fixes_used += 1

                    # Auto-save after successful batch
                    if editorial_paths and session and success_count > 0:
                        provider = session.provider
                        v = auto_save_version(
                            tool_ctx.storyboard,
                            editorial_paths,
                            provider,
                            source_label="director_chat",
                            config_snapshot={"review_model": review_config.model},
                        )
                        session.storyboard_version = v
                        session.total_edits += success_count
                        session.budget_state = {
                            "turns_used": budget.turns_used,
                            "fixes_used": budget.fixes_used,
                            "cost_used_usd": budget.cost_used_usd,
                        }
                        save_session(session, editorial_paths)
                        print_fn(f"  [Saved as v{v}]")

                # Inject as synthetic model message so Gemini sees what happened
                messages.append(
                    types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=summary_text)],
                    )
                )

                # Inject refreshed segment list so the agent has ground truth
                # after edits shifted indices (prevents context drift)
                if success_count > 0:
                    refreshed = _format_current_segments(tool_ctx.storyboard)
                    messages.append(
                        types.Content(
                            role="user",
                            parts=[
                                types.Part.from_text(
                                    text=("## Updated Segment List (after edits)\n\n" + refreshed)
                                )
                            ],
                        )
                    )

                # Log iteration
                review_log.iterations.append(
                    ReviewIteration(
                        turn=budget.turns_used,
                        tool_name="execute_proposal_batch",
                        tool_args={"edit_count": total, "success_count": success_count},
                        result_summary=summary_text[:200],
                    )
                )

                if turn_callback:
                    turn_callback(
                        budget.turns_used,
                        "edit_timeline",
                        {"action": f"batch ({success_count}/{total})"},
                        summary_text[:200],
                        budget,
                    )

                continue  # Back to outer loop for next user input

            elif _is_rejection(user_input):
                rejected_plan = tool_ctx.pending_proposal_plan
                tool_ctx.pending_proposal = None
                tool_ctx.pending_proposal_plan = ""
                print_fn("\n  Director: Proposal cancelled. What would you like to do instead?\n")
                # Inject cancellation + correction note so the model remembers
                # what was rejected (prevents sycophantic re-proposal)
                correction = "Proposal cancelled."
                if rejected_plan:
                    correction += (
                        f" [CORRECTION: The filmmaker rejected this plan: "
                        f'"{rejected_plan[:200]}". Do NOT re-propose the same approach.]'
                    )
                correction += " What would you like to do instead?"
                messages.append(
                    types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=correction)],
                    )
                )
                continue

            else:
                # Ambiguous — user gave new instructions while proposal was pending.
                # Inject correction note so the model doesn't re-propose the same thing.
                rejected_plan = tool_ctx.pending_proposal_plan
                tool_ctx.pending_proposal = None
                tool_ctx.pending_proposal_plan = ""
                if rejected_plan:
                    correction_note = (
                        f"[CORRECTION: Previous proposal was rejected. Plan was: "
                        f'"{rejected_plan[:200]}". '
                        f"The filmmaker's new instructions supersede this. "
                        f"Do NOT repeat the rejected approach.]"
                    )
                    messages.append(
                        types.Content(
                            role="model",
                            parts=[types.Part.from_text(text=correction_note)],
                        )
                    )

        # Agent loop — model may need multiple turns to inspect + propose
        # Track time spent in model calls (not user think time) per user request
        agent_loop_start = time.monotonic()
        while budget.can_continue():
            agent_elapsed = time.monotonic() - agent_loop_start
            if agent_elapsed > review_config.wall_clock_timeout_sec:
                review_log.convergence_reason = "timeout"
                print_fn("  [Budget] Agent loop timeout reached.")
                break

            turn_start = time.monotonic()
            try:
                with otel_phase_span("director_chat", stage="director", provider="gemini"):
                    response = client.raw.models.generate_content(
                        model=review_config.model,
                        contents=messages,
                        config=config,
                    )
            except Exception as e:
                log.error("Gemini API error: %s", e)
                print_fn(f"  [Error] API error: {e}")
                break

            budget.turns_used += 1
            turn_duration = time.monotonic() - turn_start

            # Cost tracking
            turn_cost = 0.0
            input_tokens = 0
            output_tokens = 0
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                um = response.usage_metadata
                input_tokens = getattr(um, "prompt_token_count", 0) or 0
                output_tokens = getattr(um, "candidates_token_count", 0) or 0
                turn_cost = estimate_cost(review_config.model, input_tokens, output_tokens)
                budget.cost_used_usd += turn_cost

            if tracer:
                from .tracing import LLMCallTrace

                tracer.record(
                    LLMCallTrace(
                        phase="director_chat",
                        provider="gemini",
                        model=review_config.model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        total_tokens=input_tokens + output_tokens,
                        estimated_cost_usd=turn_cost,
                        duration_sec=round(turn_duration, 2),
                    )
                )

            candidate = response.candidates[0] if response.candidates else None
            if not candidate or not candidate.content or not candidate.content.parts:
                break

            messages.append(candidate.content)

            # Persist model response in session
            if session and editorial_paths:
                session.messages.append(_serialize_gemini_content(candidate.content))

            # Separate text and tool calls
            function_calls = [
                p
                for p in candidate.content.parts
                if hasattr(p, "function_call") and p.function_call
            ]
            text_parts = [p.text for p in candidate.content.parts if hasattr(p, "text") and p.text]

            # Print any text response to the user
            for text in text_parts:
                # Skip internal thoughts (thought_signature)
                if text.strip():
                    print_fn(f"\n  Director: {text}\n")

            if not function_calls:
                # No tool calls — model responded with text, wait for user input
                break

            # Execute tool calls
            tool_results = []
            needs_user_input = False

            for fc in function_calls:
                call_name = fc.function_call.name
                call_args = dict(fc.function_call.args) if fc.function_call.args else {}

                handler = TOOL_HANDLERS.get(call_name)
                if not handler:
                    tool_results.append(
                        {
                            "name": call_name,
                            "result": {"type": "text", "data": f"Unknown tool: {call_name}"},
                        }
                    )
                    continue

                with otel_tool_span(call_name, call_args) as tool_span:
                    try:
                        result = handler(tool_ctx, **call_args)
                    except Exception as e:
                        log.error("Tool %s failed: %s", call_name, e)
                        result = {"type": "text", "data": f"Tool error: {e}"}
                    if tool_span:
                        tool_span.set_attribute("vx.tool.result_type", result["type"])
                        if result["type"] == "text":
                            tool_span.set_attribute("output.value", result["data"][:300])

                tool_results.append({"name": call_name, "result": result})

                if call_name in FIX_TOOLS:
                    budget.fixes_used += 1

                    # Auto-save after successful edit
                    if (
                        editorial_paths
                        and session
                        and "reverted" not in result.get("data", "").lower()
                    ):
                        provider = session.provider
                        v = auto_save_version(
                            tool_ctx.storyboard,
                            editorial_paths,
                            provider,
                            source_label="director_chat",
                            config_snapshot={"review_model": review_config.model},
                        )
                        session.storyboard_version = v
                        session.total_edits += 1
                        session.budget_state = {
                            "turns_used": budget.turns_used,
                            "fixes_used": budget.fixes_used,
                            "cost_used_usd": budget.cost_used_usd,
                        }
                        save_session(session, editorial_paths)
                        print_fn(f"  [Saved as v{v}]")

                # Log iteration
                review_log.iterations.append(
                    ReviewIteration(
                        turn=budget.turns_used,
                        tool_name=call_name,
                        tool_args=call_args,
                        result_summary=(
                            result["data"][:200] if result["type"] == "text" else "image"
                        ),
                        cost_usd=turn_cost / max(len(function_calls), 1),
                        duration_sec=round(turn_duration / max(len(function_calls), 1), 2),
                    )
                )

                if turn_callback:
                    result_data = result.get("data", "") if result["type"] == "text" else "(image)"
                    turn_callback(budget.turns_used, call_name, call_args, result_data, budget)

                # propose_edits signals we need user confirmation
                if call_name == "propose_edits":
                    from .review_display import print_proposal

                    print_proposal(result["data"])
                    needs_user_input = True

                if call_name == "finalize_review":
                    review_log.convergence_reason = "finalized"

            # Build function responses
            fn_response_parts = _build_tool_result_contents(tool_results)
            image_parts = _build_image_parts_from_results(tool_results)
            all_parts = fn_response_parts + image_parts
            messages.append(types.Content(role="user", parts=all_parts))

            if tool_ctx.finalized:
                break

            # If propose_edits was called, break to get user confirmation
            if needs_user_input:
                break

            _micro_compact(messages, keep_recent_turns=8)

        if tool_ctx.finalized or review_log.convergence_reason == "timeout":
            break

        _micro_compact(messages, keep_recent_turns=8)

    # Finalize review log
    review_log.total_turns = budget.turns_used
    review_log.total_fixes = budget.fixes_used
    review_log.total_cost_usd = round(budget.cost_used_usd, 4)
    review_log.total_duration_sec = round(time.monotonic() - start_time, 2)

    if not review_log.convergence_reason:
        review_log.convergence_reason = "user_exit"

    if tool_ctx.finalized:
        review_log.final_verdict = ReviewVerdict(
            passed=tool_ctx.final_passed,
            summary=tool_ctx.final_summary,
        )

    review_log.eval_after = build_eval_summary(
        tool_ctx.storyboard, clip_reviews, user_context, transcripts
    )
    review_log.changes = list(tool_ctx.segment_changes)

    # Save session state — always stays "active" so it can be resumed.
    # Sessions are persistent workspaces, not one-shot runs.
    if session and editorial_paths:
        session.budget_state = {
            "turns_used": budget.turns_used,
            "fixes_used": budget.fixes_used,
            "cost_used_usd": budget.cost_used_usd,
        }
        save_session(session, editorial_paths)

    # Close the OTel session span, recording final stats
    if _session_span:
        _session_span.set_attribute("vx.total_turns", review_log.total_turns)
        _session_span.set_attribute("vx.total_fixes", review_log.total_fixes)
        _session_span.set_attribute("vx.convergence_reason", review_log.convergence_reason or "")
        _session_span.set_attribute("output.value", review_log.convergence_reason or "")
    _session_cm.__exit__(None, None, None)

    return tool_ctx.storyboard, review_log


# ---------------------------------------------------------------------------
# Auto-save versioned storyboard
# ---------------------------------------------------------------------------


def auto_save_version(
    storyboard: EditorialStoryboard,
    editorial_paths,
    provider: str,
    source_label: str = "director_chat",
    config_snapshot: dict | None = None,
) -> int:
    """Save storyboard as a new versioned artifact. Returns the version number.

    Creates JSON + MD + HTML preview + _latest symlinks.
    Encapsulates the versioning boilerplate used by review/chat save flows.
    """
    from .render import render_html_preview, render_markdown
    from .versioning import (
        begin_version,
        commit_version,
        current_version,
        update_latest_symlink,
        versioned_dir,
        versioned_path,
    )

    editorial_paths.storyboard.mkdir(parents=True, exist_ok=True)

    rv_version = current_version(editorial_paths.root, f"review_{provider}")
    if rv_version == 0:
        rv_version = current_version(editorial_paths.root, "review")
    review_parent_id = f"rv.{rv_version}" if rv_version > 0 else None

    art_meta = begin_version(
        editorial_paths.root,
        phase="storyboard",
        provider=provider,
        inputs={"source": source_label},
        config_snapshot=config_snapshot or {},
        target_dir=editorial_paths.storyboard,
        parent_id=review_parent_id,
    )
    v = art_meta.version
    base = f"editorial_{provider}"

    json_path = versioned_path(editorial_paths.storyboard / f"{base}.json", v)
    json_path.write_text(storyboard.model_dump_json(indent=2))
    update_latest_symlink(json_path)

    md_path = versioned_path(editorial_paths.storyboard / f"{base}.md", v)
    md_path.write_text(render_markdown(storyboard))
    update_latest_symlink(md_path)

    export_dir = versioned_dir(editorial_paths.exports, v)
    html = render_html_preview(
        storyboard, clips_dir=editorial_paths.clips_dir, output_dir=export_dir, version=v
    )
    preview_path = export_dir / "preview.html"
    preview_path.write_text(html)
    update_latest_symlink(export_dir)

    commit_version(
        editorial_paths.root,
        art_meta,
        output_paths=[json_path, md_path],
        target_dir=editorial_paths.storyboard,
    )

    # Write metadata for live-reload preview server
    import json as _json

    metadata_path = editorial_paths.exports / "__metadata.json"
    metadata_path.write_text(
        _json.dumps({"version": v, "timestamp": time.time(), "preview": str(preview_path)})
    )

    return v


# ---------------------------------------------------------------------------
# Chat session persistence
# ---------------------------------------------------------------------------


def _session_dir(editorial_paths) -> Path:
    """Get the sessions directory for a project."""
    d = editorial_paths.root / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _next_session_id(session_dir: Path) -> str:
    """Generate the next session ID."""
    existing = sorted(session_dir.glob("session_*.json"))
    if not existing:
        return "session_001"
    last = existing[-1].stem  # "session_003"
    num = int(last.split("_")[1]) + 1
    return f"session_{num:03d}"


def find_active_session(editorial_paths) -> ChatSession | None:
    """Find an active (non-completed) chat session for a project."""
    from .models import ChatSession

    sd = editorial_paths.root / "sessions"
    if not sd.exists():
        return None
    for f in sorted(sd.glob("session_*.json"), reverse=True):
        try:
            session = ChatSession.model_validate_json(f.read_text())
            if session.status == "active":
                return session
        except Exception:
            continue
    return None


def save_session(session: ChatSession, editorial_paths) -> Path:
    """Save a chat session to disk. Returns the file path."""
    from datetime import datetime, timezone

    sd = _session_dir(editorial_paths)
    path = sd / f"{session.session_id}.json"
    session.updated_at = datetime.now(timezone.utc).isoformat()
    path.write_text(session.model_dump_json(indent=2))
    return path


def load_session(session_id: str, editorial_paths) -> ChatSession:
    """Load a chat session from disk."""
    from .models import ChatSession

    sd = editorial_paths.root / "sessions"
    path = sd / f"{session_id}.json"
    return ChatSession.model_validate_json(path.read_text())


def _serialize_gemini_content(content) -> ChatMessage:
    """Convert a Gemini types.Content to a serializable ChatMessage."""
    from datetime import datetime, timezone

    from .models import ChatMessage

    text_parts = []
    tool_calls = []
    tool_responses = []

    for part in content.parts or []:
        if hasattr(part, "text") and part.text:
            text_parts.append(part.text)
        if hasattr(part, "function_call") and part.function_call:
            fc = part.function_call
            tool_calls.append(
                {
                    "name": fc.name,
                    "args": dict(fc.args) if fc.args else {},
                }
            )
        if hasattr(part, "function_response") and part.function_response:
            fr = part.function_response
            tool_responses.append(
                {
                    "name": fr.name,
                    "result": str(fr.response)[:200] if fr.response else "",
                }
            )

    return ChatMessage(
        role=content.role or "model",
        text="\n".join(text_parts),
        tool_calls=tool_calls,
        tool_responses=tool_responses,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _rebuild_messages_from_session(session: ChatSession, initial_parts: list[dict]) -> list:
    """Rebuild Gemini message list from a saved session for resume.

    Returns a list of types.Content ready for the Gemini API.
    The initial overview is rebuilt fresh; conversation history is
    reconstructed from serialized ChatMessages as text summaries.
    """
    from google.genai import types

    # Start with fresh initial context
    messages = _build_gemini_contents(initial_parts)

    # Replay conversation history as text summaries
    for msg in session.messages:
        parts = []
        if msg.text:
            parts.append(types.Part.from_text(text=msg.text))
        if msg.tool_calls:
            for tc in msg.tool_calls:
                # Represent past tool calls as text (not re-executable)
                parts.append(
                    types.Part.from_text(
                        text=f"[Previous tool call: {tc['name']}({tc.get('args', {})})]"
                    )
                )
        if msg.tool_responses:
            for tr in msg.tool_responses:
                parts.append(
                    types.Part.from_text(
                        text=f"[Previous tool result: {tr['name']}: {tr.get('result', '')[:100]}]"
                    )
                )
        if parts:
            messages.append(types.Content(role=msg.role, parts=parts))

    return messages
