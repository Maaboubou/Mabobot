"""Pure helpers for classifying and repairing listener-window geometry."""

from __future__ import annotations

from typing import Iterable, Mapping

OFFSCREEN_SENTINEL = -30000


def _normalized_rect(rect: Mapping[str, int] | None) -> dict[str, int]:
    value = rect or {}
    return {
        "left": int(value.get("left", 0) or 0),
        "top": int(value.get("top", 0) or 0),
        "right": int(value.get("right", 0) or 0),
        "bottom": int(value.get("bottom", 0) or 0),
    }


def is_offscreen_sentinel_rect(
    rect: Mapping[str, int] | None,
    *,
    sentinel: int = OFFSCREEN_SENTINEL,
) -> bool:
    value = rect or {}
    return (
        int(value.get("left", 0) or 0) <= sentinel
        and int(value.get("top", 0) or 0) <= sentinel
    )


def is_unrecoverable_offscreen_window(
    *,
    window_rect: Mapping[str, int] | None,
    normal_rect: Mapping[str, int] | None,
    iconic: bool,
) -> bool:
    """Both current and restore rectangles must be invalid.

    ``iconic`` is diagnostic only: a normal minimized window retains a usable
    restore rectangle, while the broken Qt state uses the -32000 sentinel for
    both rectangles.
    """
    return bool(
        is_offscreen_sentinel_rect(window_rect)
        and is_offscreen_sentinel_rect(normal_rect)
    )


def build_window_observation(
    matches: Iterable[Mapping[str, object]] | None,
    *,
    observable: bool = True,
) -> dict:
    candidates = [dict(item) for item in (matches or [])]
    if not observable:
        return {"state": "unobservable", "match_count": 0, "candidate_hwnds": []}
    if not candidates:
        return {"state": "missing", "match_count": 0, "candidate_hwnds": []}
    candidate_hwnds = sorted(int(item.get("hwnd") or 0) for item in candidates)
    if len(candidates) != 1:
        return {
            "state": "ambiguous",
            "match_count": len(candidates),
            "candidate_hwnds": candidate_hwnds,
        }
    target = candidates[0]
    window_rect = _normalized_rect(target.get("window_rect"))
    normal_rect = _normalized_rect(target.get("normal_rect"))
    visible = bool(target.get("visible"))
    iconic = bool(target.get("iconic"))
    offscreen = bool(
        target.get("offscreen_sentinel", is_offscreen_sentinel_rect(window_rect))
    )
    normal_offscreen = bool(
        target.get(
            "normal_offscreen_sentinel",
            is_offscreen_sentinel_rect(normal_rect),
        )
    )
    unrecoverable = bool(
        target.get(
            "unrecoverable_offscreen",
            is_unrecoverable_offscreen_window(
                window_rect=window_rect,
                normal_rect=normal_rect,
                iconic=iconic,
            ),
        )
    )
    if unrecoverable:
        state = "unrecoverable_offscreen"
    elif not visible:
        state = "hidden"
    elif iconic:
        state = "minimized"
    elif offscreen or normal_offscreen:
        state = "partial_offscreen_sentinel"
    elif target.get("undersized"):
        state = "undersized"
    else:
        state = "healthy"
    return {
        "state": state,
        "match_count": 1,
        "candidate_hwnds": candidate_hwnds,
        "hwnd": int(target.get("hwnd") or 0),
        "pid": int(target.get("pid") or 0),
        "class_name": str(target.get("class_name") or ""),
        "visible": visible,
        "iconic": iconic,
        "show_cmd": int(target.get("show_cmd") or 0),
        "window_rect": window_rect,
        "normal_rect": normal_rect,
        "offscreen_sentinel": offscreen,
        "normal_offscreen_sentinel": normal_offscreen,
        "unrecoverable_offscreen": unrecoverable,
        "undersized": bool(target.get("undersized")),
    }


def window_observation_fingerprint(
    observation: Mapping[str, object] | None,
) -> tuple:
    value = observation or {}
    return (
        str(value.get("state") or ""),
        int(value.get("match_count") or 0),
        tuple(int(item or 0) for item in (value.get("candidate_hwnds") or [])),
        int(value.get("hwnd") or 0),
        int(value.get("pid") or 0),
        bool(value.get("visible")),
        bool(value.get("iconic")),
        int(value.get("show_cmd") or 0),
        bool(value.get("offscreen_sentinel")),
        bool(value.get("normal_offscreen_sentinel")),
        bool(value.get("unrecoverable_offscreen")),
        bool(value.get("undersized")),
    )


def is_undersized_window_rect(
    rect: Mapping[str, int] | None,
    target: Mapping[str, int] | None,
) -> bool:
    """Allow small WM/DPI differences, but retain at least 90% of each axis."""
    if not is_usable_window_rect(rect) or not is_usable_window_rect(target):
        return False
    return any(
        (int(rect[end]) - int(rect[start]))
        < (int(target[end]) - int(target[start])) * 0.9
        for start, end in (("left", "right"), ("top", "bottom"))
    )


def is_usable_window_rect(rect: Mapping[str, int] | None) -> bool:
    try:
        return bool(
            rect
            and not is_offscreen_sentinel_rect(rect)
            and int(rect.get("right", 0)) > int(rect.get("left", 0))
            and int(rect.get("bottom", 0)) > int(rect.get("top", 0))
        )
    except (TypeError, ValueError):
        return False


def choose_window_recovery_rect(
    preferred: Mapping[str, int] | None,
    candidates: Iterable[Mapping[str, int] | None],
    fallback: Mapping[str, int] | None = None,
) -> dict:
    for rect in (preferred, *candidates, fallback):
        if is_usable_window_rect(rect):
            return {
                "left": int(rect["left"]),
                "top": int(rect["top"]),
                "right": int(rect["right"]),
                "bottom": int(rect["bottom"]),
            }
    return {}


def advance_window_repair_confirmation(
    previous: Mapping[str, object] | None,
    *,
    hwnd: int,
    unhealthy: bool,
    now: float,
    threshold: int,
) -> tuple[dict, bool]:
    if not unhealthy:
        return {}, False
    required = max(2, int(threshold))
    same = bool(previous and int(previous.get("hwnd", 0) or 0) == int(hwnd))
    count = int(previous.get("count", 0) or 0) + 1 if same else 1
    first_seen_at = (
        float(previous.get("first_seen_at", now) or now) if same else float(now)
    )
    state = {
        "hwnd": int(hwnd),
        "count": count,
        "first_seen_at": first_seen_at,
        "last_seen_at": float(now),
    }
    return state, count >= required
