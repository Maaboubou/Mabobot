"""Local audio transcription helpers for summary_plus."""

from __future__ import annotations

import hashlib
import html
import os
import re
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import parse_qs, urlparse

from app.utils.subprocess_utils import hidden_creation_flags


__all__ = [
    "LocalASRError",
    "bili_transcribe_local",
    "douyin_transcribe_local",
    "file_transcribe_local",
    "srt_text_to_plain",
]


_LOCAL_ASR_SEMAPHORE = threading.BoundedSemaphore(value=1)


class LocalASRError(RuntimeError):
    """Raised when local audio preparation or transcription fails."""


def _log(logger, level: str, message: str) -> None:
    if logger is None:
        return
    fn = getattr(logger, level, None)
    if callable(fn):
        fn(message)


def _bilibili_identity(url: str) -> tuple[str, str]:
    match = re.search(r"BV[a-zA-Z0-9]+", url or "")
    if not match:
        return url, "bilibili"

    bvid = match.group()
    page = (parse_qs(urlparse(url).query).get("p") or [""])[0]
    page_suffix = f"_p{page}" if str(page).isdigit() and int(page) > 1 else ""
    clean_url = f"https://www.bilibili.com/video/{bvid}/"
    if page_suffix:
        clean_url = f"{clean_url}?p={page}"
    return clean_url, f"{bvid}{page_suffix}"


def _model_cache_token(model_path: Path) -> str:
    stat = model_path.stat()
    identity = f"{model_path.name}:{stat.st_size}:{stat.st_mtime_ns}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]


def _cache_path(url: str, model_path: Path, cache_dir: Path) -> Path:
    _, video_key = _bilibili_identity(url)
    safe_key = re.sub(r"[^a-zA-Z0-9_.-]+", "_", video_key)
    return cache_dir / f"{safe_key}_{_model_cache_token(model_path)}.txt"


def _run_process(
    command: list[str],
    *,
    label: str,
    timeout: int,
    logger=None,
    below_normal_priority: bool = False,
) -> subprocess.CompletedProcess[str]:
    creationflags = 0
    if below_normal_priority and os.name == "nt":
        creationflags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, int(timeout)),
            creationflags=hidden_creation_flags(creationflags),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise LocalASRError(f"{label}超时（>{timeout} 秒）") from exc
    except OSError as exc:
        raise LocalASRError(f"无法启动{label}: {exc}") from exc

    if result.returncode != 0:
        details = (result.stderr or result.stdout or "未知错误").strip()
        if len(details) > 1000:
            details = details[-1000:]
        raise LocalASRError(f"{label}失败（退出码 {result.returncode}）: {details}")
    _log(logger, "debug", f"{label}完成")
    return result


def _download_bilibili_audio(
    url: str,
    *,
    output_base: Path,
    cookies_path: str,
    yt_dlp_command: Sequence[str],
    ffmpeg_bin: str,
    logger=None,
) -> Path:
    clean_url, _ = _bilibili_identity(url)
    _log(logger, "info", f"[*] 正在为本地 ASR 下载音频: {clean_url}")

    command = [
        *yt_dlp_command,
        "--no-playlist",
        "--proxy",
        "",
        "--add-headers",
        "Referer:https://www.bilibili.com/",
        "--add-headers",
        "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "--ffmpeg-location",
        str(Path(ffmpeg_bin).parent if Path(ffmpeg_bin).is_file() else ffmpeg_bin),
        "-f",
        "bestaudio/best",
        "-x",
        "--audio-format",
        "m4a",
        "-o",
        f"{output_base}.%(ext)s",
    ]
    if cookies_path and os.path.isfile(cookies_path):
        command.extend(["--cookies", cookies_path])
    command.append(clean_url)

    _run_process(command, label="B站音频下载", timeout=300, logger=logger)
    candidates = sorted(
        path
        for path in output_base.parent.glob(f"{output_base.name}.*")
        if path.suffix.lower() not in {".part", ".ytdl"}
    )
    if not candidates:
        raise LocalASRError("B站音频下载完成，但未找到输出文件")
    return candidates[0]


def _download_douyin_audio(
    url: str,
    *,
    output_base: Path,
    cookie_args: list[str],
    yt_dlp_command: Sequence[str],
    ffmpeg_bin: str,
    logger=None,
) -> Path:
    _log(logger, "info", f"[*] 正在为本地 ASR 下载抖音音频: {url}")
    command = [
        *yt_dlp_command,
        "--ignore-config",
        "--no-playlist",
        *cookie_args,
        "--ffmpeg-location",
        str(Path(ffmpeg_bin).parent if Path(ffmpeg_bin).is_file() else ffmpeg_bin),
        "-f",
        "bestaudio/best",
        "-x",
        "--audio-format",
        "m4a",
        "-o",
        f"{output_base}.%(ext)s",
        url,
    ]
    _run_process(command, label="抖音音频下载", timeout=300, logger=logger)
    candidates = sorted(
        path
        for path in output_base.parent.glob(f"{output_base.name}.*")
        if path.suffix.lower() not in {".part", ".ytdl"}
    )
    if not candidates:
        raise LocalASRError("抖音音频下载完成，但未找到输出文件")
    return candidates[0]


def _convert_to_wav(
    source_path: Path,
    *,
    wav_path: Path,
    ffmpeg_bin: str,
    logger=None,
) -> None:
    _log(logger, "info", "[*] 正在转换为 16kHz 单声道 PCM 音频...")
    _run_process(
        [
            ffmpeg_bin,
            "-y",
            "-i",
            str(source_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ],
        label="音频转换",
        timeout=300,
        logger=logger,
    )


def srt_text_to_plain(srt_text: str) -> str:
    """Convert native SenseVoice SRT output into newline-delimited plain text."""
    lines: list[str] = []
    for raw_line in (srt_text or "").replace("\ufeff", "").splitlines():
        line = raw_line.strip()
        if not line or line.isdigit() or "-->" in line:
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = html.unescape(line).strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return "\n".join(lines)


def _transcribe_wav(
    wav_path: Path,
    *,
    runtime_path: Path,
    model_path: Path,
    vad_path: Path,
    timeout_sec: int,
    logger=None,
) -> str:
    _log(logger, "info", "[*] 等待本地 ASR 单任务执行槽...")
    with _LOCAL_ASR_SEMAPHORE:
        _log(logger, "info", "[*] SenseVoice F32 开始本地识别（CPU AVX2）...")
        result = _run_process(
            [
                str(runtime_path),
                "-m",
                str(model_path),
                "--vad",
                str(vad_path),
                "-a",
                str(wav_path),
                "--backend",
                "cpu",
            ],
            label="本地 ASR",
            timeout=timeout_sec,
            logger=logger,
            below_normal_priority=True,
        )

    diagnostic = (result.stderr or "").strip().replace("\r", " ").replace("\n", "; ")
    if diagnostic:
        _log(logger, "info", f"[+] 本地 ASR 运行信息: {diagnostic}")
    transcript = srt_text_to_plain(result.stdout)
    if not transcript:
        raise LocalASRError("本地 ASR 已完成，但没有生成有效文字")
    _log(logger, "info", f"[+] 本地 ASR 完成，共提取 {len(transcript)} 个字符")
    return transcript


def _write_cache(cache_path: Path, transcript: str) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
    temporary.write_text(transcript, encoding="utf-8")
    os.replace(temporary, cache_path)


def file_transcribe_local(
    source_path: str | Path,
    *,
    runtime_path: str,
    model_path: str,
    vad_path: str,
    ffmpeg_bin: Optional[str] = None,
    timeout_sec: int = 600,
    logger=None,
) -> str:
    """Transcribe an already downloaded video using the shared local ASR slot.

    Only the temporary audio is removed; the original media belongs to the caller.
    """
    source = Path(source_path)
    if not source.is_file():
        raise LocalASRError(f"本地媒体文件不存在: {source}")
    runtime, model, vad = (
        Path(os.path.expandvars(path)).expanduser().resolve()
        for path in (runtime_path, model_path, vad_path)
    )
    missing = [str(path) for path in (runtime, model, vad) if not path.is_file()]
    if missing:
        raise LocalASRError(f"本地 ASR 资源不存在: {', '.join(missing)}")
    if not ffmpeg_bin:
        from app.utils.video_frames import _resolve_media_tools

        ffmpeg_bin, _ = _resolve_media_tools()

    with tempfile.TemporaryDirectory(prefix="mabobot_video_asr_") as work_dir:
        wav_path = Path(work_dir) / "audio.wav"
        _convert_to_wav(source, wav_path=wav_path, ffmpeg_bin=ffmpeg_bin, logger=logger)
        return _transcribe_wav(
            wav_path,
            runtime_path=runtime,
            model_path=model,
            vad_path=vad,
            timeout_sec=max(30, int(timeout_sec)),
            logger=logger,
        )


def bili_transcribe_local(
    url: str,
    *,
    cookies_path: str,
    yt_dlp_command: Sequence[str],
    ffmpeg_bin: str,
    runtime_path: str,
    model_path: str,
    vad_path: str,
    timeout_sec: int = 600,
    cache_enabled: bool = True,
    cache_dir: Optional[str] = None,
    logger=None,
) -> str:
    """Download one Bilibili audio track and transcribe it locally."""
    runtime = Path(runtime_path).expanduser().resolve()
    model = Path(model_path).expanduser().resolve()
    vad = Path(vad_path).expanduser().resolve()
    missing = [str(path) for path in (runtime, model, vad) if not path.is_file()]
    if missing:
        raise LocalASRError(f"本地 ASR 资源不存在: {', '.join(missing)}")

    resolved_cache_dir = Path(cache_dir or Path.cwd() / "data" / "asr_cache" / "summary_plus")
    transcript_cache = _cache_path(url, model, resolved_cache_dir)
    if cache_enabled and transcript_cache.is_file():
        cached = transcript_cache.read_text(encoding="utf-8").strip()
        if cached:
            _log(logger, "info", f"[+] 命中本地 ASR 缓存: {transcript_cache.name}")
            return cached

    work_dir = Path.cwd() / "tmp" / "videos"
    work_dir.mkdir(parents=True, exist_ok=True)
    output_base = work_dir / f"local_asr_{uuid.uuid4().hex[:10]}"
    wav_path = output_base.with_suffix(".wav")

    try:
        source_path = _download_bilibili_audio(
            url,
            output_base=output_base,
            cookies_path=cookies_path,
            yt_dlp_command=yt_dlp_command,
            ffmpeg_bin=ffmpeg_bin,
            logger=logger,
        )
        _convert_to_wav(
            source_path,
            wav_path=wav_path,
            ffmpeg_bin=ffmpeg_bin,
            logger=logger,
        )
        transcript = _transcribe_wav(
            wav_path,
            runtime_path=runtime,
            model_path=model,
            vad_path=vad,
            timeout_sec=max(30, int(timeout_sec)),
            logger=logger,
        )
        if cache_enabled:
            _write_cache(transcript_cache, transcript)
        return transcript
    finally:
        for artifact in work_dir.glob(f"{output_base.name}*"):
            try:
                artifact.unlink()
            except OSError:
                _log(logger, "warning", f"[!] 无法清理本地 ASR 临时文件: {artifact}")


def douyin_transcribe_local(
    url: str,
    *,
    cookie_args: list[str],
    yt_dlp_command: Sequence[str],
    ffmpeg_bin: str,
    runtime_path: str,
    model_path: str,
    vad_path: str,
    timeout_sec: int = 600,
    cache_enabled: bool = True,
    cache_dir: Optional[str] = None,
    logger=None,
) -> str:
    """Download one Douyin audio track and transcribe it locally."""
    runtime = Path(runtime_path).expanduser().resolve()
    model = Path(model_path).expanduser().resolve()
    vad = Path(vad_path).expanduser().resolve()
    missing = [str(path) for path in (runtime, model, vad) if not path.is_file()]
    if missing:
        raise LocalASRError(f"本地 ASR 资源不存在: {', '.join(missing)}")

    resolved_cache_dir = Path(cache_dir or Path.cwd() / "data" / "asr_cache" / "summary_plus")
    url_digest = hashlib.sha256((url or "").encode("utf-8")).hexdigest()[:20]
    transcript_cache = (
        resolved_cache_dir / f"douyin_{url_digest}_{_model_cache_token(model)}.txt"
    )
    if cache_enabled and transcript_cache.is_file():
        cached = transcript_cache.read_text(encoding="utf-8").strip()
        if cached:
            _log(logger, "info", f"[+] 命中本地 ASR 缓存: {transcript_cache.name}")
            return cached

    work_dir = Path.cwd() / "tmp" / "videos"
    work_dir.mkdir(parents=True, exist_ok=True)
    output_base = work_dir / f"douyin_asr_{uuid.uuid4().hex[:10]}"
    wav_path = output_base.with_suffix(".wav")

    try:
        source_path = _download_douyin_audio(
            url,
            output_base=output_base,
            cookie_args=list(cookie_args or []),
            yt_dlp_command=yt_dlp_command,
            ffmpeg_bin=ffmpeg_bin,
            logger=logger,
        )
        _convert_to_wav(
            source_path,
            wav_path=wav_path,
            ffmpeg_bin=ffmpeg_bin,
            logger=logger,
        )
        transcript = _transcribe_wav(
            wav_path,
            runtime_path=runtime,
            model_path=model,
            vad_path=vad,
            timeout_sec=max(30, int(timeout_sec)),
            logger=logger,
        )
        if cache_enabled:
            _write_cache(transcript_cache, transcript)
        return transcript
    finally:
        for artifact in work_dir.glob(f"{output_base.name}*"):
            try:
                artifact.unlink()
            except OSError:
                _log(logger, "warning", f"[!] 无法清理本地 ASR 临时文件: {artifact}")
