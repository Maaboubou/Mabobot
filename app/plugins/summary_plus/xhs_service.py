"""Xiaohongshu extraction, download and fallback pipeline."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from PIL import Image, ImageDraw, ImageFont

from app.utils.subprocess_utils import hidden_process_kwargs

from .browser_service import strip_summary_tag_section
from .content_cleaner import strip_markdown
from .runtime_support import ArtifactLimitError
from .ytdlp_cookie_service import ytdlp_browser_cookie_args


TIKHUB_ENDPOINT_XHS_IMAGE_NOTE = "https://api.tikhub.io/api/v1/xiaohongshu/app_v2/get_image_note_detail"
TIKHUB_ENDPOINT_XHS_VIDEO_NOTE = "https://api.tikhub.io/api/v1/xiaohongshu/app_v2/get_video_note_detail"


class XiaohongshuMixin:
    """Platform-specific Xiaohongshu workflow mixed into SummaryService."""

    def _xhs_h5_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Mobile Safari/537.36"
            ),
            "Referer": "https://www.xiaohongshu.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

    def _xhs_note_id(self, note: Any) -> Optional[str]:
        if not isinstance(note, dict):
            return None
        value = note.get("note_id") or note.get("noteId") or note.get("id")
        value = str(value or "").strip()
        return value or None

    def _xhs_resolve_share_url(self, share_url: str) -> str:
        """Resolve WeChat's ``/explore?share_id=...`` landing URL to a note URL.

        WeChat cards can expose an ID-less Xiaohongshu landing URL. Xiaohongshu
        redirects that request to the home page, but preserves the canonical note
        URL in ``window.__INITIAL_STATE__.global.shareContext``.
        """
        normalized_share_url = self._normalize_xhs_share_url(share_url)
        if self._extract_xhs_note_id(normalized_share_url):
            return normalized_share_url

        try:
            response = requests.get(
                normalized_share_url,
                headers=self._xhs_h5_headers(),
                timeout=30,
                allow_redirects=True,
            )
            response.raise_for_status()

            final_url = self._normalize_xhs_share_url(str(getattr(response, "url", "") or ""))
            if self._extract_xhs_note_id(final_url):
                self.logger.info(
                    "✅ 小红书分享链接经 HTTP 跳转恢复笔记 ID: %s",
                    self._extract_xhs_note_id(final_url),
                )
                return final_url

            state = self._xhs_extract_initial_state_json(response.text)
            global_state = state.get("global") if isinstance(state, dict) else None
            share_context = (
                global_state.get("shareContext")
                if isinstance(global_state, dict)
                else None
            )
            if not isinstance(share_context, dict):
                return normalized_share_url

            requested_share_id = (
                parse_qs(urlparse(normalized_share_url).query).get("share_id") or [""]
            )[0]
            context_share_id = str(share_context.get("shareId") or "").strip()
            if requested_share_id and context_share_id and requested_share_id != context_share_id:
                self.logger.warning(
                    "⚠️ 小红书分享上下文与请求 share_id 不一致，拒绝使用页面候选链接"
                )
                return normalized_share_url

            candidate = str(share_context.get("shareLink") or "").strip()
            content_id = str(share_context.get("shareContentId") or "").strip()
            if not candidate and re.fullmatch(r"[0-9a-fA-F]{16,32}", content_id):
                candidate = f"https://www.xiaohongshu.com/discovery/item/{content_id}"
            if not candidate:
                return normalized_share_url

            candidate = self._normalize_xhs_share_url(
                urljoin(final_url or normalized_share_url, candidate)
            )
            parsed_candidate = urlparse(candidate)
            hostname = (parsed_candidate.hostname or "").lower()
            note_id = self._extract_xhs_note_id(candidate)
            if (
                parsed_candidate.scheme not in {"http", "https"}
                or not (hostname == "xiaohongshu.com" or hostname.endswith(".xiaohongshu.com"))
                or not note_id
            ):
                self.logger.warning("⚠️ 小红书分享上下文未提供有效的站内笔记链接")
                return normalized_share_url
            if content_id and content_id.casefold() != note_id.casefold():
                self.logger.warning("⚠️ 小红书分享上下文中的笔记 ID 不一致，拒绝候选链接")
                return normalized_share_url

            self.logger.info("✅ 小红书长链接已恢复真实笔记 ID: %s", note_id)
            return candidate
        except Exception as exc:
            self.logger.warning("⚠️ 小红书长链接解析失败，保留原链接: %s", exc)
            return normalized_share_url

    def _xhs_extract_note_from_state(
        self,
        state: Any,
        target_note_id: Optional[str] = None,
    ) -> Optional[dict]:
        if not isinstance(state, dict):
            return None
        note_data = state.get("noteData")
        data = note_data.get("data") if isinstance(note_data, dict) else None
        note = data.get("noteData") if isinstance(data, dict) else None
        if not isinstance(note, dict) or not note:
            return None
        note_id = self._xhs_note_id(note)
        if target_note_id and note_id != target_note_id:
            self.logger.warning(
                "⚠️ 小红书 H5 页面笔记不匹配: target=%s, actual=%s",
                target_note_id,
                note_id or "<unknown>",
            )
            return None
        return note

    def _xhs_fetch_note_from_h5(self, share_url: str) -> Optional[dict]:
        """Fetch complete note metadata directly from Xiaohongshu's H5 state."""
        resolved_share_url = self._xhs_resolve_share_url(share_url)
        target_note_id = self._extract_xhs_note_id(resolved_share_url)
        if not target_note_id:
            self.logger.warning("⚠️ 小红书 H5 请求前仍未恢复笔记 ID")
            return None
        try:
            self.logger.info("正在直连小红书 H5 页面提取笔记: note_id=%s", target_note_id)
            response = requests.get(
                resolved_share_url,
                headers=self._xhs_h5_headers(),
                timeout=30,
                allow_redirects=True,
            )
            response.raise_for_status()
            state = self._xhs_extract_initial_state_json(response.text)
            note = self._xhs_extract_note_from_state(state, target_note_id)
            if not note:
                self.logger.warning("⚠️ 小红书 H5 页面中未找到目标笔记数据")
                return None
            self.logger.info(
                "✅ 小红书 H5 笔记提取成功: note_id=%s, type=%s",
                target_note_id,
                note.get("type") or note.get("note_type") or "unknown",
            )
            return note
        except Exception as exc:
            self.logger.warning("⚠️ 直连小红书 H5 笔记提取失败: %s", exc)
            return None

    def _xhs_extract_subtitle_urls(self, note: Any) -> List[str]:
        """Extract subtitle URLs, preferring source-language and Chinese tracks."""
        tracks: Dict[str, List[str]] = {}
        seen_nodes = set()

        def add_track(language: str, value: Any) -> None:
            values = value if isinstance(value, list) else [value]
            for item in values:
                url = self._xhs_pick_url(item)
                if not url:
                    continue
                bucket = tracks.setdefault(language.casefold(), [])
                if url not in bucket:
                    bucket.append(url)

        def visit(value: Any) -> None:
            if isinstance(value, str):
                candidate = value.strip()
                if candidate.startswith(("{", "[")):
                    try:
                        visit(json.loads(candidate))
                    except (TypeError, ValueError):
                        pass
                return
            if not isinstance(value, (dict, list)):
                return
            node_id = id(value)
            if node_id in seen_nodes:
                return
            seen_nodes.add(node_id)
            if isinstance(value, list):
                for item in value:
                    visit(item)
                return
            for key, child in value.items():
                if str(key).casefold() == "subtitles" and isinstance(child, dict):
                    for language, track in child.items():
                        add_track(str(language), track)
                visit(child)

        visit(note)
        ordered_languages = ["source", "zh-cn", "zh_cn", "zh", "zh-hans"]
        result: List[str] = []
        for language in ordered_languages:
            result.extend(url for url in tracks.pop(language, []) if url not in result)
        for urls in tracks.values():
            result.extend(url for url in urls if url not in result)
        return result

    def _xhs_subtitle_to_text(self, subtitle_text: str) -> str:
        lines: List[str] = []
        for raw_line in (subtitle_text or "").lstrip("\ufeff").splitlines():
            line = raw_line.strip()
            if not line or line.upper() == "WEBVTT" or re.fullmatch(r"\d+", line):
                continue
            if "-->" in line:
                continue
            line = re.sub(r"<[^>]+>", "", line).strip()
            if line and (not lines or line != lines[-1]):
                lines.append(line)
        return "\n".join(lines)

    def _xhs_fetch_subtitle_text(self, note: Any) -> Optional[str]:
        for subtitle_url in self._xhs_extract_subtitle_urls(note):
            try:
                response = requests.get(
                    subtitle_url,
                    headers=self._xhs_h5_headers(),
                    timeout=20,
                )
                response.raise_for_status()
                text = self._xhs_subtitle_to_text(response.text)
                if text:
                    self.logger.info("✅ 小红书字幕提取成功: %s 字", len(text))
                    return text
            except Exception as exc:
                self.logger.warning("⚠️ 小红书字幕轨道读取失败，尝试下一轨: %s", exc)
        return None

    def summarize_xhs_note(self, share_url: str, chat_name: str = "") -> Optional[str]:
        """Summarize H5 note text/subtitles without downloading an oversized video."""
        resolved_share_url = self._xhs_resolve_share_url(share_url)
        note = self._xhs_fetch_note_from_h5(resolved_share_url)
        if not note:
            return None

        title = str(note.get("title") or "").strip()
        description = str(note.get("desc") or note.get("description") or "").strip()
        user = note.get("user") if isinstance(note.get("user"), dict) else {}
        author = str(user.get("nickName") or user.get("nickname") or "").strip()
        subtitle_text = self._xhs_fetch_subtitle_text(note)

        sections = []
        if title:
            sections.append(f"标题：{title}")
        if author:
            sections.append(f"作者：{author}")
        if description:
            sections.append(f"笔记正文：{description}")
        if subtitle_text:
            sections.append(f"视频字幕：\n{subtitle_text}")
        source_text = "\n\n".join(sections).strip()
        if not source_text:
            return None

        max_content_length = max(1, int(getattr(self, "MAX_CONTENT_LENGTH", 20000)))
        messages = [
            {
                "role": "system",
                "content": str(
                    getattr(self, "prompt_summary", "请忠实、简洁地总结用户提供的内容。")
                ),
            },
            {"role": "user", "content": source_text[:max_content_length]},
        ]
        call_kwargs = {"_mabobot_chat_name": chat_name} if chat_name else {}
        try:
            response = self.llm_manager.call(
                plugin_name="summary_plus",
                call_type="summary",
                messages=messages,
                **call_kwargs,
            )
        except Exception as exc:
            self.logger.error("❌ 小红书摘要模型调用失败: %s", exc, exc_info=True)
            return None
        summary = strip_summary_tag_section(strip_markdown(str(response or "").strip()))
        return summary or None

    def _xhs_ytdlp_info(
        self,
        share_url: str,
        timeout_sec: int = 60,
        cookie_args: Optional[List[str]] = None,
    ) -> Optional[dict]:
        try:
            result = self._run_platform_ytdlp(
                "xiaohongshu",
                [
                    "--ignore-no-formats-error",
                    "--dump-single-json",
                    share_url,
                ],
                timeout_sec=timeout_sec,
                cookie_args=cookie_args,
            )
            if result.returncode != 0:
                self._log_ytdlp_failure("小红书元数据", result)
                return None
            payload = json.loads((result.stdout or "").strip())
            return payload if isinstance(payload, dict) else None
        except (json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            self.logger.warning("⚠️ 小红书 yt-dlp 元数据解析失败: %s", exc)
            return None
        except Exception as exc:
            self.logger.warning("⚠️ 小红书 yt-dlp 元数据获取异常: %s", exc)
            return None

    def _xhs_ytdlp_image_urls(self, info: Any) -> List[str]:
        """Deduplicate urlDefault/urlPre thumbnail pairs while preserving note order."""
        if not isinstance(info, dict):
            return []
        selected: Dict[str, Tuple[int, str]] = {}
        for thumbnail in info.get("thumbnails") or []:
            if not isinstance(thumbnail, dict):
                continue
            url = str(thumbnail.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            parsed = urlparse(url)
            variant = re.search(r"^(.*)!nd_(dft|prv)_", parsed.path, re.IGNORECASE)
            if variant:
                key = f"{parsed.netloc.casefold()}{variant.group(1)}"
                score = 2 if variant.group(2).casefold() == "dft" else 1
            else:
                key = url
                score = 0
            current = selected.get(key)
            if current is None or score > current[0]:
                selected[key] = (score, url)
        return [url for _score, url in selected.values()]

    def _xhs_note_description(self, note: dict) -> str:
        for key in ("description", "desc"):
            value = note.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _xhs_text_font(self, size: int, *, bold: bool = False):
        windows_fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
        filename = "msyhbd.ttc" if bold else "msyh.ttc"
        candidates = [
            os.path.join(windows_fonts, filename),
            f"/mnt/c/Windows/Fonts/{filename}",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/System/Library/Fonts/PingFang.ttc",
        ]
        for path in candidates:
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
        raise RuntimeError("小红书正文制图需要中文字体（微软雅黑、Noto Sans CJK 或文泉驿）")

    def _xhs_render_description(self, description: str, author: str, output_path: str):
        """Render the approved 1000px body card for the end of a long image."""
        text = re.sub(r"#([^#]+?)\[话题\]#", r"#\1 ", description).strip()
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        font = self._xhs_text_font(36)
        bold = self._xhs_text_font(46, bold=True)
        small = self._xhs_text_font(26)
        lines = []
        for paragraph in text.split("\n"):
            line = ""
            for char in paragraph:
                if line and font.getlength(line + char) > 876:
                    if char in "，。！？；：、）】》”’":
                        lines.append(line + char)
                        line = ""
                    else:
                        lines.append(line)
                        line = char
                else:
                    line += char
            lines.append(line)
        with Image.new("RGB", (1000, 220 + len(lines) * 62), "white") as card:
            draw = ImageDraw.Draw(card)
            draw.line((48, 0, 952, 0), fill="#dddddd", width=2)
            draw.text((48, 32), "笔记正文", font=bold, fill="#222222")
            # Keep the author on one line even when upstream metadata is unusually long.
            author = " ".join(author.split())
            while author and small.getlength(author) > 876:
                author = author[:-2] + "…"
            draw.text((48, 102), author, font=small, fill="#888888")
            for index, line in enumerate(lines):
                draw.text((48, 166 + index * 62), line, font=font, fill="#292929")
            card.save(output_path, "PNG")

    def _process_xhs_image_urls(
        self, image_urls: List[str], uid: str, *, note: Optional[dict] = None,
    ) -> Optional[str]:
        image_urls = image_urls[:self.xhs_max_images]
        if not image_urls:
            return None

        note = note or {}
        description = self._xhs_note_description(note)
        tmp_dir = self._temp_dir("images")
        if len(image_urls) == 1 and not description:
            raw_path = os.path.join(tmp_dir, f"temp_{uid}_ytdlp_raw")
            output_path = os.path.join(tmp_dir, f"xhs_img_{uid}.jpg")
            try:
                self._xhs_download_file(image_urls[0], raw_path)
                self._xhs_convert_to_jpg(raw_path, output_path)
                return output_path
            finally:
                self._remove_path_quietly(raw_path)

        self.logger.info(
            "检测到小红书图文（%s 张），准备合并为长图",
            len(image_urls),
        )
        temp_files: List[str] = []
        converted_images: List[str] = []
        try:
            for index, image_url in enumerate(image_urls):
                raw_path = os.path.join(tmp_dir, f"temp_{uid}_ytdlp_{index}_raw")
                jpg_path = os.path.join(tmp_dir, f"temp_{uid}_ytdlp_{index}.jpg")
                self._xhs_download_file(image_url, raw_path)
                temp_files.append(raw_path)
                self._xhs_convert_to_jpg(raw_path, jpg_path)
                temp_files.append(jpg_path)
                converted_images.append(jpg_path)
            if not converted_images:
                return None
            if description:
                body_path = os.path.join(tmp_dir, f"temp_{uid}_description.png")
                temp_files.append(body_path)
                user = note.get("user")
                user = user if isinstance(user, dict) else {}
                author = str(user.get("nickname") or user.get("name") or note.get("uploader") or note.get("creator") or "")
                self._xhs_render_description(description, author, body_path)
                converted_images.append(body_path)
            output_path = os.path.join(tmp_dir, f"xhs_long_img_{uid}.jpg")
            self._merge_images_vertically(converted_images, output_path)
            return output_path
        except Exception as exc:
            self.logger.warning("⚠️ 小红书图片处理失败: %s", exc)
            return None
        finally:
            for temp_path in temp_files:
                self._remove_path_quietly(temp_path)

    def _download_xhs_video_with_ytdlp(
        self,
        share_url: str,
        uid: str,
        info: Optional[dict] = None,
        timeout_sec: int = 240,
        cookie_args: Optional[List[str]] = None,
    ) -> Optional[str]:
        tmp_dir = self._temp_dir("videos")
        output_template = os.path.join(tmp_dir, f"xhs_ytdlp_{uid}_{uuid.uuid4().hex[:8]}.%(ext)s")
        info_path = ""
        try:
            input_args: List[str]
            if info:
                handle, info_path = tempfile.mkstemp(
                    prefix="summary_plus_xhs_",
                    suffix=".info.json",
                )
                os.close(handle)
                with open(info_path, "w", encoding="utf-8") as info_file:
                    json.dump(info, info_file, ensure_ascii=False)
                try:
                    os.chmod(info_path, 0o600)
                except OSError:
                    pass
                input_args = ["--load-info-json", info_path]
            else:
                input_args = [share_url]
            result = self._run_platform_ytdlp(
                "xiaohongshu",
                [
                    "--ffmpeg-location",
                    self.ffmpeg_bin,
                    "--format",
                    "b[vcodec=EF4]/b[vcodec=h264]/b[vcodec^=avc]/b[ext=mp4]/b",
                    "--merge-output-format",
                    "mp4",
                    "--remux-video",
                    "mp4",
                    "--no-write-thumbnail",
                    "--no-progress",
                    "-o",
                    output_template,
                    *input_args,
                ],
                timeout_sec=timeout_sec,
                cookie_args=cookie_args,
            )
            if result.returncode != 0:
                self._log_ytdlp_failure("小红书视频", result)
                return None
            video_path = self._find_ytdlp_output(output_template)
            if not video_path:
                return None
            codec = self._probe_video_codec(video_path)
            if codec and codec not in {"h264", "avc1"}:
                self.logger.info("🔄 小红书 yt-dlp 输出编码为 %s，转换为微信兼容 H.264", codec)
                video_path = self._convert_to_wechat_compatible(video_path) or video_path
            return video_path
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            self.logger.warning("⚠️ 小红书 yt-dlp 视频下载失败: %s", exc)
            return None
        except Exception as exc:
            self.logger.warning("⚠️ 小红书 yt-dlp 视频下载异常: %s", exc)
            return None
        finally:
            if info_path:
                self._remove_path_quietly(info_path)

    def _process_xhs_note_with_ytdlp(self, share_url: str) -> Tuple[bool, Optional[str]]:
        self.logger.info("📥 小红书优先使用 yt-dlp 处理: %s", share_url)
        try:
            with ytdlp_browser_cookie_args(
                platform="xiaohongshu",
                debug_port=self.chrome_debug_port,
                user_data_dir=self.chrome_user_data_dir,
                profile_dir=self.chrome_profile_dir,
                logger=self.logger,
            ) as cookie_args:
                info = self._xhs_ytdlp_info(share_url, cookie_args=cookie_args)
                if not info:
                    return False, None

                uid = str(
                    info.get("id")
                    or self._extract_xhs_note_id(share_url)
                    or uuid.uuid4().hex[:8]
                )
                formats = info.get("formats") or []
                if isinstance(formats, list) and formats:
                    duration = self._xhs_video_duration_seconds(info)
                    if not duration:
                        duration = max(
                            (self._xhs_video_duration_seconds(item) for item in formats),
                            default=0,
                        )
                    if duration > self.xhs_max_download_duration:
                        self.logger.info(
                            "跳过处理(yt-dlp): 视频时长为 %ss，超过 %ss",
                            duration,
                            self.xhs_max_download_duration,
                        )
                        return True, None
                    video_path = self._download_xhs_video_with_ytdlp(
                        share_url,
                        uid,
                        info=info,
                        cookie_args=cookie_args,
                    )
                    if video_path:
                        self.logger.info("✅ 小红书 yt-dlp 视频下载成功: %s", video_path)
                        return True, video_path
                    return False, None

                image_urls = self._xhs_ytdlp_image_urls(info)
                if image_urls:
                    self.logger.info(
                        "🖼️ 小红书 yt-dlp 识别到 %s 张去重后的正文图片",
                        len(image_urls),
                    )
                    image_path = self._process_xhs_image_urls(image_urls, uid, note=info)
                    if image_path:
                        self.logger.info("✅ 小红书 yt-dlp 图片处理成功: %s", image_path)
                        return True, image_path
                return False, None
        except Exception as exc:
            self.logger.warning("⚠️ 小红书 yt-dlp 处理异常: %s", exc)
            return False, None

    def _xhs_download_file(self, url: str, save_path: str):
        """下载文件并保存 (带 Headers 以防止 405)"""
        self.logger.info(f"正在下载: {url}")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.xiaohongshu.com/"
        }
        last_err: Optional[Exception] = None
        max_bytes = max(1, int(getattr(self, "max_artifact_size_mb", 512))) * 1024 * 1024
        for attempt in range(2):
            try:
                response = requests.get(url, headers=headers, stream=True, timeout=(15, 90))
                response.raise_for_status()
                content_length = int(response.headers.get("Content-Length") or 0)
                artifacts = getattr(self, "artifacts", None)
                if artifacts is not None:
                    artifacts.assert_capacity(content_length)
                    max_bytes = artifacts.max_artifact_bytes
                elif content_length > max_bytes:
                    raise ArtifactLimitError("小红书文件超过单文件大小限制")
                downloaded = 0
                with open(save_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            downloaded += len(chunk)
                            if downloaded > max_bytes:
                                raise ArtifactLimitError("小红书文件下载超过单文件大小限制")
                            f.write(chunk)
                self.logger.info(f"保存成功: {save_path}")
                return
            except Exception as e:
                self._remove_path_quietly(save_path)
                last_err = e
                self.logger.warning(f"⚠️ 下载失败 (requests 第 {attempt + 1}/2 次): {e}")
                time.sleep(1 + attempt)

        curl_bin = "curl.exe" if self.is_wsl else "curl"
        try:
            subprocess.run(
                [
                    curl_bin,
                    "--max-time",
                    "180",
                    "--max-filesize",
                    str(max_bytes),
                    "-L",
                    "-A",
                    headers["User-Agent"],
                    "-e",
                    headers["Referer"],
                    url,
                    "-o",
                    save_path,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=210,
                **hidden_process_kwargs(),
            )
            if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                if os.path.getsize(save_path) > max_bytes:
                    self._remove_path_quietly(save_path)
                    raise ArtifactLimitError("小红书文件下载超过单文件大小限制")
                self.logger.info(f"保存成功(curl fallback): {save_path}")
                return
            raise RuntimeError("curl fallback produced empty file")
        except Exception as e:
            self.logger.error(f"❌ 下载失败(curl fallback): {e}")
            if last_err:
                raise last_err
            raise

    def _merge_images_vertically(self, image_paths: list, output_path: str, target_width: int = 1080, margin: int = 40):
        """将多张图片缩放到相同宽度（算上边框）后垂直拼接，不裁剪，添加等宽边框"""
        self.logger.info(f"正在合并 {len(image_paths)} 张图片并添加边框...")
        images = []

        # 有效图片宽度 = 总宽度 - 左右边框
        inner_width = target_width - 2 * margin

        try:
            total_height = margin # 初始顶部边距
            for p in image_paths:
                img = Image.open(p)
                w, h = img.size
                ratio = inner_width / w
                new_h = int(h * ratio)

                img_resized = img.resize((inner_width, new_h), Image.Resampling.LANCZOS)
                images.append(img_resized)

                # 图片高度 + 图间/底部边距
                total_height += new_h + margin

            # 创建空白画布
            result = Image.new('RGB', (target_width, total_height), (255, 255, 255))

            current_y = margin
            for img in images:
                result.paste(img, (margin, current_y))
                current_y += img.height + margin

            result.save(output_path, "JPEG", quality=95)
            self.logger.info(f"等距长图已保存: {output_path}")
        finally:
            for img in images:
                img.close()

    def _xhs_convert_to_jpg(self, input_path: str, output_path: str):
        """使用 ffmpeg 将图片转换为 jpg"""
        self.logger.info(f"正在进行格式转换: {input_path} -> {output_path}")
        try:
            # -y 表示覆盖输出文件
            subprocess.run(
                [self.ffmpeg_bin, "-y", "-i", input_path, output_path],
                check=True,
                capture_output=True,
                **hidden_process_kwargs(),
            )
            self.logger.info("转换完成")
        except subprocess.CalledProcessError as e:
            self.logger.error(f"转换失败: {e.stderr.decode()}")
            raise

    def _xhs_pick_url(self, value: Any) -> Optional[str]:
        """从 TikHub 新旧结构里提取可下载 URL。"""
        if isinstance(value, str):
            value = value.strip()
            return value if value.startswith(("http://", "https://")) else None
        if isinstance(value, list):
            for item in value:
                url = self._xhs_pick_url(item)
                if url:
                    return url
            return None
        if isinstance(value, dict):
            url_keys = (
                "url",
                "url_default",
                "url_pre",
                "url_size_large",
                "original",
                "origin",
                "master_url",
                "masterUrl",
                "backup_url",
                "backupUrl",
                "backup_urls",
                "backupUrls",
                "main_url",
                "mainUrl",
                "h265_url",
                "h265Url",
                "h264_url",
                "h264Url",
            )
            for key in url_keys:
                url = self._xhs_pick_url(value.get(key))
                if url:
                    return url
            nested_keys = (
                "url_list",
                "url_info_list",
                "info_list",
                "play_addr",
                "download_addr",
                "stream",
                "media",
                "video",
            )
            for key in nested_keys:
                url = self._xhs_pick_url(value.get(key))
                if url:
                    return url
        return None

    def _xhs_extract_notes_from_response(self, res_json: Any) -> list:
        """兼容 TikHub V1 新结构、旧结构和 App V2 结构。"""
        notes = []
        seen = set()

        def looks_like_note(item: dict) -> bool:
            note_keys = {
                "note_id",
                "noteId",
                "id",
                "type",
                "image_list",
                "imageList",
                "images_list",
                "video_info",
                "video_info_v2",
                "video",
            }
            return bool(note_keys.intersection(item.keys()))

        def visit(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    visit(item)
                return
            if not isinstance(value, dict):
                return

            if looks_like_note(value):
                marker = (
                    value.get("note_id")
                    or value.get("noteId")
                    or value.get("id")
                    or id(value)
                )
                if marker not in seen:
                    seen.add(marker)
                    notes.append(value)

            for key in (
                "data",
                "note",
                "note_detail",
                "note_info",
                "note_list",
                "items",
                "list",
                "result",
            ):
                if key in value:
                    visit(value.get(key))

        visit(res_json.get("data") if isinstance(res_json, dict) else res_json)
        return notes

    def _xhs_has_usable_media(self, note: dict) -> bool:
        images = (
            note.get("image_list")
            or note.get("imageList")
            or note.get("images_list")
            or note.get("images")
            or []
        )
        if isinstance(images, dict):
            images = [images]
        video_info = self._xhs_get_video_info(note)
        has_image_url = any(self._xhs_pick_url(img) for img in images)
        return bool(has_image_url or self._xhs_pick_url(video_info))

    def _xhs_get_video_info(self, note: dict) -> dict:
        """兼容 TikHub App V2 的 video_info_v2 和旧结构。"""
        if not isinstance(note, dict):
            return {}
        video_info = note.get("video_info_v2") or note.get("video_info") or note.get("video") or {}
        return video_info if isinstance(video_info, dict) else {}

    def _xhs_target_note_id(self, share_url: str) -> Optional[str]:
        return self._extract_xhs_note_id(self._normalize_xhs_share_url(share_url))

    def _xhs_select_target_notes(self, notes: list, target_note_id: Optional[str]) -> list:
        """有目标 ID 时严格只保留目标笔记，绝不使用接口返回的关联笔记代替。"""
        valid_notes = [note for note in notes if isinstance(note, dict)]
        if not target_note_id:
            return valid_notes

        target_notes = [
            note
            for note in valid_notes
            if self._xhs_note_id(note) == target_note_id
        ]
        ignored_ids = [
            str(self._xhs_note_id(note) or "<unknown>")
            for note in valid_notes
            if self._xhs_note_id(note) != target_note_id
        ]
        if ignored_ids:
            self.logger.info(
                "小红书严格目标匹配: target=%s, ignored_related=%s",
                target_note_id,
                ignored_ids,
            )
        if not target_notes:
            self.logger.warning(
                "⚠️ TikHub 响应未包含目标小红书笔记: target=%s, candidates=%s",
                target_note_id,
                ignored_ids,
            )
        return target_notes

    def _xhs_static_image_url(self, image_info: Any) -> Optional[str]:
        """只提取静态图 URL，避免多图 live_photo 场景误取动态视频。"""
        if isinstance(image_info, str):
            image_info = image_info.strip()
            return image_info if image_info.startswith(("http://", "https://")) else None
        if not isinstance(image_info, dict):
            return None
        for key in ("url_size_large", "original", "url", "url_default", "url_pre", "origin"):
            url = self._xhs_pick_url(image_info.get(key))
            if url:
                return url
        image_node = image_info.get("image")
        if isinstance(image_node, dict):
            for key in ("url_size_large", "original", "url", "url_default", "url_pre", "origin"):
                url = self._xhs_pick_url(image_node.get(key))
                if url:
                    return url
        return None

    def _xhs_extract_live_photo_video_url(self, image_info: Any) -> Optional[str]:
        """单张 live 图优先返回动态视频；多图场景不调用。"""
        if not isinstance(image_info, dict):
            return None
        live_photo = image_info.get("live_photo") or image_info.get("livePhoto")
        if not isinstance(live_photo, dict):
            stream = image_info.get("stream")
            return self._xhs_extract_video_url(stream) if isinstance(stream, dict) else None
        return self._xhs_extract_video_url(live_photo)

    def _xhs_request_note_endpoint(
        self,
        token: str,
        endpoint_name: str,
        api_url: str,
        params: dict,
        target_note_id: Optional[str],
    ) -> Optional[dict]:
        """请求一个 TikHub App V2 笔记端点，并过滤不可用/非目标响应。"""
        headers = {"Authorization": f"Bearer {token}"}
        retry_count = 3
        retry_delay = 2

        for attempt in range(retry_count):
            try:
                self.logger.info(
                    f"正在请求 TikHub 小红书{endpoint_name}接口 "
                    f"(尝试 {attempt + 1}/{retry_count}): params={params}"
                )
                response = requests.get(api_url, headers=headers, params=params, timeout=30)
                if response.status_code == 200:
                    res_json = response.json()
                    notes = self._xhs_extract_notes_from_response(res_json)
                    notes = self._xhs_select_target_notes(notes, target_note_id)
                    if notes:
                        return res_json
                    self.logger.warning(
                        f"⚠️ TikHub 小红书{endpoint_name}接口返回成功但未找到目标笔记"
                    )
                    # TikHub 会对这种 HTTP 200 的上游空结果正常计费；相同参数
                    # 立即重试只会重复扣费，不会改变结果。
                    return None
                else:
                    self.logger.warning(
                        f"⚠️ TikHub 小红书{endpoint_name}接口请求失败: "
                        f"{response.status_code} - {response.text}"
                    )
                    # 参数或路由错误重试不会改善结果，也会拖长摘要链路。
                    if response.status_code in (400, 401, 403, 404, 422):
                        break
            except Exception as e:
                self.logger.warning(f"⚠️ TikHub 小红书{endpoint_name}接口请求异常: {e}")

            if attempt < retry_count - 1:
                time.sleep(retry_delay)

        return None

    def _xhs_fetch_note_response(self, token: str, share_url: str) -> Optional[dict]:
        """按 TikHub App V2 文档流程获取笔记；视频用专用详情接口补全播放地址。"""
        normalized_share_url = self._normalize_xhs_share_url(share_url)
        note_id = self._extract_xhs_note_id(normalized_share_url)

        parsed = urlparse(normalized_share_url)
        is_xhs_long_url = parsed.netloc.lower().endswith("xiaohongshu.com")
        if is_xhs_long_url and not note_id:
            self.logger.warning(
                "⚠️ 小红书长链接缺少笔记 ID，跳过 TikHub 请求以避免无效调用: %s",
                normalized_share_url,
            )
            return None

        if note_id:
            # 文档要求二选一并优先 note_id；不要同时携带过期的分享参数。
            params = {"note_id": note_id}
        else:
            params = {"share_text": normalized_share_url}

        # 2026-08-14 文档推荐：先用图文详情识别类型；视频再用同一 note_id
        # 请求视频详情。已下线并持续返回 404 的 App V1 get_note_info 不再兜底。
        image_response = self._xhs_request_note_endpoint(
            token,
            "App V2 图文",
            TIKHUB_ENDPOINT_XHS_IMAGE_NOTE,
            params,
            note_id,
        )
        if not image_response:
            return None

        image_notes = self._xhs_extract_notes_from_response(image_response)
        image_notes = self._xhs_select_target_notes(image_notes, note_id)
        primary_note = image_notes[0] if image_notes else {}
        note_type = str(primary_note.get("type") or primary_note.get("note_type") or "").lower()

        if note_type != "video":
            if any(self._xhs_has_usable_media(note) for note in image_notes):
                return image_response
            self.logger.warning("⚠️ TikHub 小红书App V2 图文接口未返回可用媒体字段")
            return None

        video_note_id = str(self._xhs_note_id(primary_note) or note_id or "").strip()
        video_params = {"note_id": video_note_id} if video_note_id else params
        video_response = self._xhs_request_note_endpoint(
            token,
            "App V2 视频",
            TIKHUB_ENDPOINT_XHS_VIDEO_NOTE,
            video_params,
            video_note_id or note_id,
        )
        if not video_response:
            return None

        video_notes = self._xhs_extract_notes_from_response(video_response)
        video_notes = self._xhs_select_target_notes(video_notes, video_note_id or note_id)
        if any(self._xhs_extract_video_url(self._xhs_get_video_info(note)) for note in video_notes):
            return video_response

        self.logger.warning("⚠️ TikHub 小红书App V2 视频接口未返回可用视频播放地址")

        return None

    def _xhs_video_duration_seconds(self, video_info: Any) -> float:
        """兼容 TikHub 和小红书 H5 __INITIAL_STATE__ 的视频时长字段。"""
        if not isinstance(video_info, dict):
            return 0

        def normalize_duration(value: Any) -> Optional[float]:
            if value is None:
                return None
            try:
                duration = float(value)
                return duration / 1000 if duration > 1000 else duration
            except (TypeError, ValueError):
                return None

        for key in ("duration", "duration_sec", "duration_seconds"):
            duration = normalize_duration(video_info.get(key))
            if duration:
                return duration

        for path in (
            ("capa", "duration"),
            ("media", "video", "duration"),
        ):
            node: Any = video_info
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
            duration = normalize_duration(node)
            if duration:
                return duration
        return 0

    def _xhs_extract_video_url(self, video_info: Any) -> Optional[str]:
        """提取视频 URL；优先 H.264 MP4，避免 H.265 在微信/部分播放器兼容性差。"""
        if not isinstance(video_info, dict):
            return None

        candidates = []

        def collect(value: Any, meta: str = "") -> None:
            if isinstance(value, list):
                for item in value:
                    collect(item, meta)
                return
            url = self._xhs_pick_url(value)
            if url:
                candidates.append((url, meta or str(value)))

        streams = [video_info.get("stream")]
        media = video_info.get("media")
        if isinstance(media, dict):
            streams.append(media.get("stream"))
            # TikHub 2026-08 新结构：video_info_v2.media.video.stream.h264。
            media_video = media.get("video")
            if isinstance(media_video, dict):
                streams.append(media_video.get("stream"))

        for stream in streams:
            if not isinstance(stream, dict):
                continue
            for codec_key in ("h264", "h265", "h266", "av1"):
                collect(stream.get(codec_key), codec_key)

        for key in (
            "url_info_list",
            "url_list",
            "video_list",
            "stream_list",
            "media",
            "play_addr",
            "download_addr",
            "video",
            "url",
        ):
            collect(video_info.get(key), key)

        def is_mp4(url: str) -> bool:
            return ".mp4" in urlparse(url).path.lower()

        for url, meta in candidates:
            if is_mp4(url) and ("h264" in meta.lower() or "_259.mp4" in url or "streamtype': 259" in meta.lower()):
                return url
        for url, _meta in candidates:
            if is_mp4(url):
                return url
        return candidates[0][0] if candidates else None

    def _xhs_extract_initial_state_json(self, page_html: str) -> Optional[dict]:
        """从小红书 H5 页面提取 window.__INITIAL_STATE__（自动修复 JS 残留如 undefined）。"""
        marker = "window.__INITIAL_STATE__="
        start = page_html.find(marker)
        if start < 0:
            return None
        start += len(marker)
        end = page_html.find("</script>", start)
        if end < 0:
            return None
        raw = page_html[start:end].strip().rstrip(";")
        # JavaScript literal → valid JSON: replace undefined with null
        raw = re.sub(r"\bundefined\b", "null", raw)
        try:
            return json.loads(raw)
        except Exception as e:
            self.logger.warning(f"⚠️ 解析小红书 H5 INITIAL_STATE 失败: {e}")
            return None

    def _xhs_find_video_info_in_state(self, state: Any) -> Optional[dict]:
        """递归扫描 INITIAL_STATE，找到包含真实 stream/masterUrl 的 video 对象。"""
        seen = set()

        def has_video_url(value: Any) -> bool:
            if isinstance(value, dict):
                url = self._xhs_extract_video_url(value)
                if url and (".mp4" in urlparse(url).path.lower() or "sns-video" in url):
                    return True
                return any(has_video_url(v) for v in value.values())
            if isinstance(value, list):
                return any(has_video_url(v) for v in value)
            return False

        def visit(value: Any) -> Optional[dict]:
            obj_id = id(value)
            if obj_id in seen:
                return None
            seen.add(obj_id)

            if isinstance(value, dict):
                video = value.get("video") or value.get("video_info")
                if isinstance(video, dict) and has_video_url(video):
                    return video
                if has_video_url(value) and ("stream" in value or "media" in value):
                    return value
                for child in value.values():
                    found = visit(child)
                    if found:
                        return found
            elif isinstance(value, list):
                for child in value:
                    found = visit(child)
                    if found:
                        return found
            return None

        return visit(state)

    def _xhs_fetch_video_note_from_h5(self, share_url: str) -> Optional[dict]:
        """直连小红书 H5 页面，绕过第三方 API 提取视频流；仅用于视频兜底。"""
        note = self._xhs_fetch_note_from_h5(share_url)
        if not note:
            return None
        video_info = self._xhs_get_video_info(note)
        if not video_info or not self._xhs_extract_video_url(video_info):
            self.logger.warning("⚠️ 小红书 H5 页面中未找到可用视频流")
            return None
        return {
            "type": "video",
            "note_id": self._xhs_note_id(note) or uuid.uuid4().hex[:8],
            "video_info": video_info,
        }

    def _xhs_url_looks_video(self, share_url: str) -> bool:
        try:
            parsed = urlparse(self._normalize_xhs_share_url(share_url))
            qs = parse_qs(parsed.query)
            return (qs.get("type") or [""])[0].lower() == "video"
        except Exception:
            return "type=video" in (share_url or "").lower()

    def _xhs_process_note_payload(
        self,
        note: Any,
        *,
        source: str,
    ) -> Tuple[bool, Optional[str]]:
        """Convert one H5/TikHub note payload into a WeChat-sendable artifact."""
        if not isinstance(note, dict):
            return False, None
        note_type = str(note.get("type") or note.get("note_type") or "").lower()
        images_list = (
            note.get("image_list")
            or note.get("imageList")
            or note.get("images_list")
            or note.get("images")
            or []
        )
        if isinstance(images_list, dict):
            images_list = [images_list]
        video_info = self._xhs_get_video_info(note)
        uid = self._xhs_note_id(note) or uuid.uuid4().hex[:8]

        try:
            if video_info or note_type == "video":
                duration = self._xhs_video_duration_seconds(video_info)
                if duration > self.xhs_max_download_duration:
                    self.logger.info(
                        "跳过视频下载(%s): 目标视频 %s 时长为 %ss，超过 %ss；将尝试字幕摘要",
                        source,
                        uid,
                        duration,
                        self.xhs_max_download_duration,
                    )
                    return True, None
                selected_url = self._xhs_extract_video_url(video_info)
                if not selected_url:
                    self.logger.warning("⚠️ %s 小红书笔记未找到有效视频 URL", source)
                    return False, None
                tmp_dir = self._temp_dir("videos")
                filepath = os.path.join(tmp_dir, f"xhs_video_{uid}.mp4")
                self._xhs_download_file(selected_url, filepath)
                self.logger.info("✅ %s 小红书视频下载成功: %s", source, filepath)
                return True, filepath

            if images_list or note_type in ("normal", "image", "images"):
                if len(images_list) == 1:
                    live_video_url = self._xhs_extract_live_photo_video_url(images_list[0])
                    if live_video_url:
                        tmp_dir = self._temp_dir("videos")
                        filepath = os.path.join(tmp_dir, f"xhs_live_{uid}.mp4")
                        self._xhs_download_file(live_video_url, filepath)
                        self.logger.info("✅ %s 小红书 Live Photo 下载成功: %s", source, filepath)
                        return True, filepath

                image_urls = [
                    image_url
                    for image_url in (
                        self._xhs_static_image_url(image_info)
                        for image_info in images_list
                    )
                    if image_url
                ]
                if not image_urls:
                    self.logger.warning("⚠️ %s 小红书笔记未找到图片原图 URL", source)
                    return False, None
                image_path = self._process_xhs_image_urls(image_urls, uid, note=note)
                return (True, image_path) if image_path else (False, None)

            self.logger.info(
                "跳过处理(%s): 类型为 %s, 图片数量为 %s",
                source,
                note_type or "unknown",
                len(images_list),
            )
            return False, None
        except Exception as exc:
            self.logger.warning("⚠️ %s 小红书媒体处理失败: %s", source, exc, exc_info=True)
            return False, None

    def _xhs_process_note_payloads(
        self,
        notes: List[dict],
        *,
        source: str,
    ) -> Tuple[bool, Optional[str]]:
        for note in notes:
            handled, file_path = self._xhs_process_note_payload(note, source=source)
            if handled:
                return True, file_path
        return False, None

    def process_xhs_note(self, share_url: str) -> Optional[str]:
        """Resolve and process a Xiaohongshu note, returning an artifact path."""
        resolved_share_url = self._xhs_resolve_share_url(share_url)
        handled, ytdlp_path = self._process_xhs_note_with_ytdlp(resolved_share_url)
        if handled:
            return ytdlp_path
        self.logger.info("🔄 小红书 yt-dlp 处理失败，优先回退免费 H5，再回退 TikHub")

        h5_note = self._xhs_fetch_note_from_h5(resolved_share_url)
        if h5_note:
            handled, h5_path = self._xhs_process_note_payload(h5_note, source="H5")
            if handled:
                return h5_path

        token = (os.getenv("TIKHUB_API_TOKEN") or "").strip()
        if not token:
            self.logger.warning("⚠️ 小红书 H5 未获取到可用媒体，且未配置 TikHub Token")
            return None

        target_note_id = self._xhs_target_note_id(resolved_share_url)
        res_json = self._xhs_fetch_note_response(token, resolved_share_url)
        notes = self._xhs_extract_notes_from_response(res_json) if res_json else []
        notes = self._xhs_select_target_notes(notes, target_note_id)
        if not notes:
            self.logger.warning("未获取到有效小红书数据")
            return None

        _handled, file_path = self._xhs_process_note_payloads(notes, source="TikHub")
        return file_path
