"""Headless frame sampling for downloaded videos.

This module never starts a media player.  FFprobe reads duration metadata and
FFmpeg decodes four still images directly from the local file.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


DEFAULT_SAMPLE_COUNT = 4
DEFAULT_MAX_DIMENSION = 1280


@dataclass(frozen=True)
class VideoFrameSample:
    path: Path
    timestamp_seconds: float
    position_ratio: float
    duration_seconds: float

    @property
    def position_percent(self) -> float:
        return self.position_ratio * 100.0


def _resolve_media_tools() -> tuple[str, str]:
    from static_ffmpeg import run as static_ffmpeg_run

    ffmpeg_path, ffprobe_path = (
        static_ffmpeg_run.get_or_fetch_platform_executables_else_raise()
    )
    return str(ffmpeg_path), str(ffprobe_path)


def _run_command(command: Sequence[str], *, timeout: int) -> subprocess.CompletedProcess:
    kwargs = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": max(1, int(timeout)),
        "check": False,
    }
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(list(command), **kwargs)


def probe_video_duration(
    video_path: str | Path,
    *,
    ffprobe_bin: str | None = None,
    timeout: int = 20,
) -> float:
    """Return a positive duration using FFprobe's structured output."""
    source = Path(video_path)
    if not source.is_file():
        raise FileNotFoundError(f"Video file does not exist: {source}")
    if ffprobe_bin is None:
        _, ffprobe_bin = _resolve_media_tools()

    result = _run_command(
        [
            str(ffprobe_bin),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=duration:format=duration",
            "-of",
            "json",
            str(source),
        ],
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = str(result.stderr or result.stdout or "").strip()[-800:]
        raise RuntimeError(f"FFprobe failed for quoted video: {detail}")

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("FFprobe returned invalid duration metadata") from exc

    candidates = []
    for stream in payload.get("streams") or []:
        if isinstance(stream, dict):
            candidates.append(stream.get("duration"))
    video_format = payload.get("format") or {}
    if isinstance(video_format, dict):
        candidates.append(video_format.get("duration"))
    for raw_value in candidates:
        try:
            duration = float(raw_value)
        except (TypeError, ValueError):
            continue
        if duration > 0:
            return duration
    raise RuntimeError("Quoted video has no usable duration")


def evenly_spaced_sample_positions(duration_seconds: float, count: int = DEFAULT_SAMPLE_COUNT) -> list[float]:
    """Return the midpoint of each equally sized time segment."""
    duration = float(duration_seconds)
    sample_count = int(count)
    if duration <= 0:
        raise ValueError("Video duration must be positive")
    if sample_count <= 0 or sample_count > 12:
        raise ValueError("Frame sample count must be between 1 and 12")
    return [duration * ((index + 0.5) / sample_count) for index in range(sample_count)]


def extract_evenly_spaced_frames(
    video_path: str | Path,
    output_dir: str | Path,
    *,
    count: int = DEFAULT_SAMPLE_COUNT,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
    ffmpeg_bin: str | None = None,
    ffprobe_bin: str | None = None,
    command_timeout: int = 45,
) -> list[VideoFrameSample]:
    """Decode evenly spaced JPEG frames without opening a playback window."""
    source = Path(video_path)
    if not source.is_file():
        raise FileNotFoundError(f"Video file does not exist: {source}")
    sample_count = int(count)
    dimension = max(320, min(2048, int(max_dimension)))
    if ffmpeg_bin is None or ffprobe_bin is None:
        resolved_ffmpeg, resolved_ffprobe = _resolve_media_tools()
        ffmpeg_bin = ffmpeg_bin or resolved_ffmpeg
        ffprobe_bin = ffprobe_bin or resolved_ffprobe

    duration = probe_video_duration(
        source,
        ffprobe_bin=str(ffprobe_bin),
        timeout=min(max(5, command_timeout), 30),
    )
    positions = evenly_spaced_sample_positions(duration, sample_count)
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    samples: list[VideoFrameSample] = []
    scale_filter = (
        f"scale='if(gt(iw,ih),min(iw,{dimension}),-2)':"
        f"'if(gt(iw,ih),-2,min(ih,{dimension}))'"
    )

    for index, timestamp in enumerate(positions, start=1):
        output_path = target_dir / f"frame_{index:02d}.jpg"
        result = _run_command(
            [
                str(ffmpeg_bin),
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{timestamp:.6f}",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-vf",
                scale_filter,
                "-q:v",
                "4",
                "-y",
                str(output_path),
            ],
            timeout=command_timeout,
        )
        if result.returncode != 0 or not output_path.is_file() or output_path.stat().st_size <= 0:
            detail = str(result.stderr or result.stdout or "").strip()[-800:]
            raise RuntimeError(
                f"FFmpeg failed to extract quoted-video frame {index}/{sample_count}: {detail}"
            )
        samples.append(
            VideoFrameSample(
                path=output_path,
                timestamp_seconds=timestamp,
                position_ratio=timestamp / duration,
                duration_seconds=duration,
            )
        )

    return samples
