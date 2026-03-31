"""Shared storyboard markdown template and prompt text for both analysis patterns."""


STORYBOARD_PROMPT = """\
Analyze this video content and produce a detailed storyboard in the exact markdown format below.

For each distinct scene/shot:
1. Identify the timestamp range (start - end)
2. Classify the shot type (wide, medium, close-up, extreme close-up, aerial, POV, etc.)
3. Describe what is happening visually (setting, subjects, action, lighting)
4. Note camera movement (static, pan left/right, tilt up/down, zoom in/out, tracking, handheld)
5. Transcribe any audible speech or describe ambient audio
6. Note the transition to the next scene (cut, dissolve, fade, etc.)

Group consecutive similar frames into a single shot. Be thorough — identify every distinct shot or camera change.

Use this EXACT markdown format:

# Video Storyboard: {filename}
**Duration**: {duration} | **Resolution**: {resolution} | **Date**: {date}

## Summary
[2-3 sentence overview of the entire video — what is it about, where, what activity]

## Scenes

### Scene 1: [Descriptive Title] (start_time - end_time)
**Setting**: [Location/environment description]

| # | Timestamp | Duration | Shot Type | Description | Camera | Audio |
|---|-----------|----------|-----------|-------------|--------|-------|
| 1 | 00:00-00:08 | 8s | Wide | Establishing shot of... | Static | [ambient noise] |
| 2 | 00:08-00:22 | 14s | Medium | Subject walks toward... | Pan right | "Let's go..." |

**Transition**: Cut to Scene 2

[Repeat for all scenes...]

## Highlights
[List 3-5 most visually interesting or important moments with timestamps]

## Shot Statistics
- Total shots: N
- Average shot duration: Xs
- Most common shot type: [type]
- Scene transitions: N

## Audio Summary
[Key dialogue points, music cues, ambient sound notes]
"""


def build_storyboard_prompt(
    filename: str,
    duration: str,
    resolution: str,
    date: str = "",
) -> str:
    """Fill in the video metadata placeholders in the storyboard prompt."""
    return STORYBOARD_PROMPT.format(
        filename=filename,
        duration=duration,
        resolution=resolution,
        date=date or "Unknown",
    )


def format_duration(seconds: float) -> str:
    """Format duration as M:SS or H:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
