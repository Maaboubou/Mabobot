"""URL parsing, platform downloads and media processing for Summary Plus."""

from __future__ import annotations

import base64
import gzip
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import requests

from app.utils.plugin_config import get_config
from app.utils.subprocess_utils import hidden_process_kwargs

from .runtime_support import ArtifactLimitError
from .ytdlp_cookie_service import ytdlp_browser_cookie_args


TIKHUB_ENDPOINT_FETCH_ONE = "https://api.tikhub.io/api/v1/douyin/app/v3/fetch_one_video_by_share_url"
TIKHUB_ENDPOINT_FETCH_ONE_WEB = "https://api.tikhub.io/api/v1/douyin/web/fetch_one_video_by_share_url"
TIKHUB_ENDPOINT_TIKTOK_FETCH_ONE = "https://api.tikhub.io/api/v1/tiktok/app/v3/fetch_one_video_by_share_url"


class MediaPipelineMixin:
    """Platform URL, downloader, FFmpeg and Bilibili media primitives."""

    def _extract_douyin_share_url(self, text: str) -> Optional[str]:
        """提取抖音短链、douyin.com 长链和 iesdouyin.com 分享链接。"""
        if not text:
            return None

        # 匹配 v.douyin.com 短链（支持字母、数字、连字符和下划线）
        # 边界：空格或字符串结尾
        m = re.search(r"(https?://v\.douyin\.com/[0-9A-Za-z\-_]+/?)(?:\s|$)", text)
        if m:
            return m.group(1)

        # 保留分享参数，供 yt-dlp 跟随跳转及 TikHub 解析原始分享链接。
        m = re.search(
            r"(https?://(?:www\.)?(?:douyin|iesdouyin)\.com/[^\s<>\"]+)",
            text,
            re.IGNORECASE,
        )
        return m.group(1) if m else None

    def _extract_tiktok_share_url(self, text: str) -> Optional[str]:
        """提取 TikTok 分享链接（支持 vt/vm.tiktok.com 短链和 www.tiktok.com 长链）"""
        if not text:
            return None

        # 匹配 vt.tiktok.com / vm.tiktok.com 短链
        m = re.search(r"(https?://(?:vt|vm)\.tiktok\.com/[0-9A-Za-z\-_]+/?)", text)
        if m:
            return m.group(1)

        # 兼容 www.tiktok.com 长链接
        m = re.search(r"(https?://(?:www\.)?tiktok\.com/[^\s<>\"]+)", text)
        return m.group(1) if m else None

    def _extract_bilibili_share_url(self, text: str) -> Optional[str]:
        """提取 Bilibili 分享链接并去除多余参数"""
        if not text:
            return None

        m = re.search(r"(https?://(?:www\.)?bilibili\.com/video/[a-zA-Z0-9]+)/?", text)
        return m.group(1) if m else None

    def _extract_xhs_share_url(self, text: str) -> Optional[str]:
        """提取小红书分享链接"""
        if not text:
            return None
        # TikHub App V2 同时支持 xhslink.com、xhslink.cn 和 xiaohongshu.com。
        m = re.search(
            r"(https?://(?:www\.)?(?:xiaohongshu\.com|xhslink\.(?:com|cn))/[^\s<>\"]+)",
            text,
        )
        return m.group(1) if m else None

    def _extract_xhs_note_id(self, text: str) -> Optional[str]:
        """从小红书长链接中提取 note_id，作为 TikHub share_text 解析失败时的稳定参数。"""
        if not text:
            return None
        normalized = self._normalize_xhs_share_url(text)
        candidates = [text]
        if normalized != text:
            candidates.append(normalized)
        candidates.append(unquote(text))
        for candidate in candidates:
            m = re.search(r"xiaohongshu\.com/(?:(?:discovery/)?item|explore)/([0-9a-fA-F]+)", candidate)
            if m:
                return m.group(1)
        return None

    def _normalize_xhs_share_url(self, url: str) -> str:
        """把小红书 login redirectPath 链接还原成真实笔记链接。"""
        if not url:
            return url
        try:
            parsed = urlparse(url)
            if parsed.netloc.endswith("xiaohongshu.com") and parsed.path.rstrip("/") == "/login":
                redirect_path = (parse_qs(parsed.query).get("redirectPath") or [""])[0]
                if redirect_path:
                    return unquote(redirect_path)
        except Exception as e:
            self.logger.warning(f"⚠️ 解析小红书跳转链接失败: {e}")
        m = re.search(r"redirectPath=([^&\s]+)", url)
        return unquote(m.group(1)) if m else url

    def _extract_youtube_url(self, text: str) -> Optional[str]:
        """提取 YouTube 链接"""
        if not text:
            return None
        # 支持 youtube.com, youtu.be, youtube.com/shorts/ 等
        patterns = [
            r"(https?://(?:www\.)?youtube\.com/watch\?v=[-_a-zA-Z0-9]{11})",
            r"(https?://youtu\.be/[-_a-zA-Z0-9]{11})",
            r"(https?://(?:www\.)?youtube\.com/shorts/[-_a-zA-Z0-9]{11})",
            r"(https?://(?:www\.)?youtube\.com/embed/[-_a-zA-Z0-9]{11})",
        ]
        for p in patterns:
            m = re.search(p, text)
            if m:
                return m.group(1)
        return None

    def _extract_weibo_url(self, text: str) -> Optional[str]:
        """提取微博链接（支持 weibo.com、weibo.cn 及其子域名）"""
        if not text:
            return None
        m = re.search(
            r"(https?://(?:[a-zA-Z0-9-]+\.)*weibo\.(?:com|cn)/"
            r"[^\s<>\"'，。！？；：、）】》]+)",
            text,
            re.IGNORECASE,
        )
        return m.group(1) if m else None

    def _extract_hupu_url(self, text: str) -> Optional[str]:
        """提取虎扑链接（支持 hupu.com 主域及子域）"""
        if not text:
            return None
        m = re.search(r"(https?://(?:[a-zA-Z0-9-]+\.)*hupu\.com/[^\s<>\"]*)", text)
        return m.group(1) if m else None

    def _get_tikhub_token(self) -> str:
        return (os.getenv("TIKHUB_API_TOKEN") or "").strip()

    def _json_loads_if_needed(self, v: Any) -> Any:
        if isinstance(v, str):
            s = v.strip()
            if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
                try:
                    return json.loads(s)
                except Exception:
                    return v
        return v

    def _get_tikhub_json(
        self,
        endpoint: str,
        *,
        headers: dict,
        params: dict,
        label: str,
    ) -> Tuple[Optional[dict], bool]:
        """Call a paid TikHub endpoint without retrying deterministic failures."""
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            error: Optional[Exception] = None
            try:
                response = requests.get(
                    endpoint,
                    headers=headers,
                    params=params,
                    timeout=20,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    self.logger.warning("⚠️ TikHub %s 返回非对象 JSON", label)
                    return None, True
                return payload, False
            except (requests.Timeout, requests.ConnectionError) as exc:
                error = exc
                transient = True
            except requests.HTTPError as exc:
                error = exc
                status_code = getattr(exc.response, "status_code", 0) or 0
                transient = status_code >= 500
            except (requests.RequestException, ValueError) as exc:
                error = exc
                transient = False
            except Exception as exc:
                error = exc
                transient = False

            self.logger.warning(
                "⚠️ TikHub %s 请求失败 (尝试 %s/%s): %s",
                label,
                attempt,
                max_attempts,
                error,
            )
            if not transient or attempt >= max_attempts:
                return None, not transient
            time.sleep(2)
        return None, False

    def _extract_first_play_url_from_aweme_detail(self, aweme_detail: Any) -> Optional[List[str]]:
        """
        从 TikHub 的 aweme_detail 中提取视频直链列表。
        规则：
        1) 强制优先：aweme_detail.video.download_addr.url_list (返回整个列表)
        """

        def _pick_url_list(obj: Any, path: List[str]) -> Optional[List[str]]:
            cur: Any = obj
            for key in path:
                if not isinstance(cur, dict):
                    return None
                cur = cur.get(key)
            if not isinstance(cur, list) or not cur:
                return None
            # 过滤出有效的 URL
            valid_urls = []
            for item in cur:
                if isinstance(item, str):
                    s = item.strip()
                    if s.startswith(("http://", "https://")):
                        valid_urls.append(s)
            return valid_urls if valid_urls else None

        try:
            url_list = _pick_url_list(aweme_detail, ["video", "download_addr", "url_list"])
            if url_list:
                return url_list
        except Exception:
            return None

    def _extract_play_url_from_tiktok_aweme(self, aweme_detail: Any) -> Optional[List[str]]:
        """
        从 TikHub 的 TikTok aweme_detail 中提取视频直链列表。
        TikTok App V3 接口使用 video.play_addr.url_list 路径。
        """

        def _pick_url_list(obj: Any, path: List[str]) -> Optional[List[str]]:
            cur: Any = obj
            for key in path:
                if not isinstance(cur, dict):
                    return None
                cur = cur.get(key)
            if not isinstance(cur, list) or not cur:
                return None
            valid_urls = [url for url in cur if isinstance(url, str) and url.strip().startswith(("http://", "https://"))]
            return valid_urls if valid_urls else None

        try:
            url_list = _pick_url_list(aweme_detail, ["video", "play_addr", "url_list"])
            if url_list:
                return url_list
        except Exception:
            return None

    def parse_douyin_video(self, text: str) -> Optional[List[str]]:
        """使用 TikHub 解析抖音分享链接，返回直链列表（url_list）"""
        share_url = self._extract_douyin_share_url(text)
        if not share_url:
            return None

        token = self._get_tikhub_token()
        if not token:
            self.logger.warning("⚠️ 缺少 TikHub Token：请在 .env 设置 TIKHUB_API_TOKEN")
            return None

        headers = {"Authorization": f"Bearer {token}"}
        params = {"share_url": share_url}
        payload, app_permanent_failure = self._get_tikhub_json(
            TIKHUB_ENDPOINT_FETCH_ONE,
            headers=headers,
            params=params,
            label="抖音 App V3",
        )
        data = self._json_loads_if_needed(payload.get("data")) if payload else None
        if isinstance(data, dict):
            aweme_detail = self._json_loads_if_needed(data.get("aweme_detail"))
            if isinstance(aweme_detail, dict):
                url_list = self._extract_first_play_url_from_aweme_detail(aweme_detail)
                if url_list:
                    return url_list

        if app_permanent_failure:
            self.logger.error("❌ TikHub 抖音 App V3 请求不可重试，停止解析")
            return None

        # App V3 成功但无视频直链时，官方建议尝试 Web 接口。
        self.logger.info("🔄 抖音 App V3 未返回视频直链，尝试 Web 接口")
        payload_web, _ = self._get_tikhub_json(
            TIKHUB_ENDPOINT_FETCH_ONE_WEB,
            headers=headers,
            params=params,
            label="抖音 Web",
        )
        data_web = self._json_loads_if_needed(payload_web.get("data")) if payload_web else None
        if not isinstance(data_web, dict):
            self.logger.error("❌ TikHub 抖音解析失败")
            return None

        aweme_detail_web = self._json_loads_if_needed(data_web.get("aweme_detail"))
        if not isinstance(aweme_detail_web, dict):
            self.logger.error("❌ TikHub 抖音 Web 接口未返回作品数据")
            return None

        url_list = self._extract_first_play_url_from_aweme_detail(aweme_detail_web)
        if url_list:
            return url_list

        # 图文视频使用 images[0].video.play_addr.url_list。
        images = aweme_detail_web.get("images")
        if isinstance(images, list) and images and isinstance(images[0], dict):
            video = images[0].get("video")
            play_addr = video.get("play_addr") if isinstance(video, dict) else None
            raw_urls = play_addr.get("url_list") if isinstance(play_addr, dict) else None
            if isinstance(raw_urls, list):
                valid_urls = [
                    url
                    for url in raw_urls
                    if isinstance(url, str)
                    and url.strip().startswith(("http://", "https://"))
                ]
                if valid_urls:
                    self.logger.info(
                        "✅ 从抖音 Web images 路径提取到 %s 个视频直链",
                        len(valid_urls),
                    )
                    return valid_urls
        self.logger.error("❌ TikHub 抖音 Web 接口未返回视频直链")
        return None

    def parse_tiktok_video(self, text: str) -> Optional[List[str]]:
        """使用 TikHub 解析 TikTok 分享链接，返回直链列表（url_list）"""
        share_url = self._extract_tiktok_share_url(text)
        if not share_url:
            return None

        token = self._get_tikhub_token()
        if not token:
            self.logger.warning("⚠️ 缺少 TikHub Token：请在 .env 设置 TIKHUB_API_TOKEN")
            return None

        headers = {"Authorization": f"Bearer {token}"}
        params = {"share_url": share_url}
        payload, _ = self._get_tikhub_json(
            TIKHUB_ENDPOINT_TIKTOK_FETCH_ONE,
            headers=headers,
            params=params,
            label="TikTok App V3",
        )
        data = self._json_loads_if_needed(payload.get("data")) if payload else None
        if not isinstance(data, dict):
            return None

        aweme_detail = self._json_loads_if_needed(data.get("aweme_detail"))
        if not isinstance(aweme_detail, dict):
            return None
        return self._extract_play_url_from_tiktok_aweme(aweme_detail)

    def _rank_video_download_url(self, url: str) -> int:
        """给视频直链排序：优先稳定 CDN，降低 experiment 节点优先级。"""
        u = (url or "").lower()
        score = 0
        if "experiment" in u:
            score += 20
        if "ov-experiment" in u:
            score += 20
        if "v5-hl" in u:
            score -= 5
        return score

    def _download_douyin_with_ytdlp(
        self,
        share_url: str,
        timeout_sec: int = 180,
    ) -> Optional[str]:
        """Download Douyin with yt-dlp before the paid TikHub fallback."""
        share_url = self._extract_douyin_share_url(share_url or "") or ""
        if not share_url:
            return None

        tmp_dir = self._temp_dir("videos")
        output_template = os.path.join(
            tmp_dir,
            f"douyin_ytdlp_{int(time.time())}_{uuid.uuid4().hex[:8]}.%(ext)s",
        )
        self.logger.info("📥 抖音优先使用 yt-dlp 下载: %s", share_url)
        try:
            result = self._run_platform_ytdlp(
                "douyin",
                [
                    "--ffmpeg-location",
                    self.ffmpeg_bin,
                    "--format",
                    "b[format_id^=h264_]/b[vcodec=h264]/b[vcodec^=avc]/b[ext=mp4]/b",
                    "--merge-output-format",
                    "mp4",
                    "--remux-video",
                    "mp4",
                    "--no-write-thumbnail",
                    "--no-progress",
                    "-o",
                    output_template,
                    share_url,
                ],
                timeout_sec=timeout_sec,
            )
            if result.returncode != 0:
                self._log_ytdlp_failure("抖音", result)
                return None
            video_path = self._find_ytdlp_output(output_template)
            if not video_path:
                self.logger.warning("⚠️ 抖音 yt-dlp 未生成视频文件")
                return None

            codec = self._probe_video_codec(video_path)
            if codec and codec not in {"h264", "avc1"}:
                self.logger.info("🔄 抖音 yt-dlp 输出编码为 %s，转换为微信兼容 H.264", codec)
                video_path = self._convert_to_wechat_compatible(video_path) or video_path
            self.logger.info("✅ 抖音 yt-dlp 下载成功: %s", video_path)
            return video_path
        except subprocess.TimeoutExpired:
            self.logger.warning("⚠️ 抖音 yt-dlp 下载超时（>%ss）", timeout_sec)
            return None
        except FileNotFoundError:
            self.logger.warning("⚠️ 未找到 yt-dlp，抖音将回退 TikHub")
            return None
        except Exception as exc:
            self.logger.warning("⚠️ 抖音 yt-dlp 下载异常，将回退 TikHub: %s", exc)
            return None

    def _check_douyin_duration(self, share_url: str) -> Optional[int]:
        """Use yt-dlp metadata to return a Douyin video's duration in seconds."""
        share_url = self._extract_douyin_share_url(share_url or "") or ""
        if not share_url:
            return None

        self.logger.info("⏱️ 正在通过 yt-dlp 检查抖音视频时长: %s", share_url)
        try:
            result = self._run_platform_ytdlp(
                "douyin",
                [
                    "--skip-download",
                    "--print",
                    "%(duration)s",
                    share_url,
                ],
                timeout_sec=30,
            )
        except subprocess.TimeoutExpired:
            self.logger.warning("⚠️ 抖音 yt-dlp 获取时长超时（>30s）: %s", share_url)
            return None
        except FileNotFoundError:
            self.logger.warning("⚠️ 未找到 yt-dlp，无法获取抖音视频时长")
            return None
        except Exception as exc:
            self.logger.warning("⚠️ 抖音 yt-dlp 获取时长异常: %s", exc)
            return None

        if result.returncode != 0:
            self._log_ytdlp_failure("抖音时长检测", result)
            return None

        for line in reversed((result.stdout or "").splitlines()):
            value = line.strip()
            if not value:
                continue
            try:
                duration = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(duration) and duration > 0:
                total_seconds = int(math.ceil(duration))
                self.logger.info("✅ 抖音视频时长检测: %s秒", total_seconds)
                return total_seconds

        self.logger.warning(
            "⚠️ 抖音 yt-dlp 未返回有效时长: %r",
            (result.stdout or "").strip()[-200:],
        )
        return None

    def _download_video(self, url_list: List[str]) -> Optional[str]:
        """下载视频到临时文件，支持 fallback 机制遍历 url_list"""
        if not url_list:
            self.logger.error("❌ URL 列表为空")
            return None

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://www.douyin.com/',
        }
        connect_timeout = 5
        read_timeout = 12
        chunk_size = 256 * 1024
        url_list = sorted(url_list, key=self._rank_video_download_url)

        # 创建临时目录
        tmp_dir = self._temp_dir("videos")

        # 遍历 url_list，尝试每个 URL 直到成功
        for idx, url in enumerate(url_list, 1):
            filepath: Optional[str] = None
            try:
                self.logger.info(
                    f"⬇️ 尝试下载视频 ({idx}/{len(url_list)}): {url[:200]}... "
                    f"timeout=({connect_timeout}s connect, {read_timeout}s read)"
                )
                resp = requests.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=(connect_timeout, read_timeout),
                )
                resp.raise_for_status()
                content_length = int(resp.headers.get("Content-Length") or 0)
                max_bytes = max(1, int(getattr(self, "max_artifact_size_mb", 512))) * 1024 * 1024
                artifacts = getattr(self, "artifacts", None)
                if artifacts is not None:
                    artifacts.assert_capacity(content_length)
                    max_bytes = artifacts.max_artifact_bytes
                elif content_length > max_bytes:
                    raise ArtifactLimitError("远端视频超过单文件大小限制")

                # 检查内容类型
                content_type = resp.headers.get('Content-Type', '')
                if 'video' not in content_type and 'octet-stream' not in content_type:
                    self.logger.warning(f"⚠️ 警告：返回的内容类型可能不是视频: {content_type}")

                filename = f"douyin_{int(time.time())}_{uuid.uuid4().hex[:8]}.mp4"
                filepath = os.path.join(tmp_dir, filename)

                downloaded = 0
                with open(filepath, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        if chunk:
                            downloaded += len(chunk)
                            if downloaded > max_bytes:
                                raise ArtifactLimitError("视频下载超过单文件大小限制")
                            f.write(chunk)

                self.logger.info(f"✅ 下载成功！文件已保存至: {filepath}")

                # 简单校验文件头 (MP4 常见的 ftyp 标记)
                try:
                    with open(filepath, 'rb') as f:
                        header = f.read(12)
                        if b'ftyp' in header:
                            self.logger.info("✅ 验证通过：文件头部包含有效的 MP4 标识。")
                        else:
                            self.logger.warning("⚠️ 警告：文件头部未检测到标准 MP4 标识，请确认文件是否可播放。")
                except Exception as e:
                    self.logger.warning(f"⚠️ 校验视频文件头失败: {e}")

                return filepath
            except Exception as e:
                if filepath:
                    self._remove_path_quietly(filepath)
                self.logger.warning(f"⚠️ URL {idx}/{len(url_list)} 下载失败: {e}")
                if idx == len(url_list):
                    self.logger.error(f"❌ 所有 URL 均下载失败，共尝试 {len(url_list)} 个")
                continue

        return None

    def _download_hupu_video(self, url: str, timeout_sec: int = 60) -> Optional[str]:
        """使用 yt-dlp 下载虎扑视频，成功后转为微信兼容格式"""
        if not url:
            return None

        tmp_dir = self._temp_dir("videos")

        output_tpl = os.path.join(
            tmp_dir, f"hupu_{int(time.time())}_{uuid.uuid4().hex[:8]}.%(ext)s"
        )
        self.logger.info(f"🏀 命中虎扑链接，开始尝试 yt-dlp 下载: {url}")

        try:
            cmd = [
                *self.yt_dlp_command,
                "--ignore-config",
                "--no-playlist",
                "--max-filesize",
                f"{getattr(self, 'max_artifact_size_mb', 512)}M",
                "-o",
                output_tpl,
                url,
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                encoding="utf-8",
                errors="replace",
                **hidden_process_kwargs(),
            )
            if result.returncode != 0:
                self.logger.warning(
                    f"⚠️ 虎扑 yt-dlp 下载失败，返回码 {result.returncode}: {url}"
                )
                self._log_ytdlp_failure("虎扑下载", result)
                return None

            matched_files = []
            prefix = output_tpl.replace("%(ext)s", "")
            for fn in os.listdir(tmp_dir):
                full_path = os.path.join(tmp_dir, fn)
                if os.path.isfile(full_path) and full_path.startswith(prefix):
                    matched_files.append(full_path)

            if not matched_files:
                self.logger.warning(f"⚠️ 虎扑下载未找到输出文件: {url}")
                return None

            matched_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            downloaded_path = matched_files[0]
            self.logger.info(f"✅ 虎扑视频下载成功: {downloaded_path}")

            converted_path = self._convert_to_wechat_compatible(downloaded_path)
            if converted_path and os.path.exists(converted_path):
                self.logger.info(f"✅ 虎扑视频已转换为微信兼容格式: {converted_path}")
                return converted_path

            self.logger.warning("⚠️ 虎扑视频转码失败，回退使用原始下载文件")
            return downloaded_path if os.path.exists(downloaded_path) else None
        except subprocess.TimeoutExpired:
            self.logger.warning(f"⚠️ 虎扑 yt-dlp 下载超时（>{timeout_sec}s）: {url}")
            return None
        except FileNotFoundError:
            self.logger.error("❌ 未找到 yt-dlp 命令，虎扑视频下载已跳过")
            return None
        except Exception as e:
            self.logger.error(f"❌ 虎扑视频下载异常: {e}", exc_info=True)
            return None

    def _download_weibo_video(self, url: str, timeout_sec: int = 180) -> Optional[str]:
        """直接使用 yt-dlp 下载微博视频并返回本地文件路径。"""
        if not url:
            return None

        tmp_dir = self._temp_dir("videos")

        output_tpl = os.path.join(
            tmp_dir, f"weibo_{int(time.time())}_{uuid.uuid4().hex[:8]}.%(ext)s"
        )
        self.logger.info(f"📺 命中微博链接，开始使用 yt-dlp 下载: {url}")

        try:
            cmd = [
                *self.yt_dlp_command,
                "--no-playlist",
                "--max-filesize",
                f"{getattr(self, 'max_artifact_size_mb', 512)}M",
                "--ffmpeg-location",
                self.ffmpeg_bin,
                "--merge-output-format",
                "mp4",
                "--remux-video",
                "mp4",
                "-o",
                output_tpl,
                url,
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                encoding="utf-8",
                errors="replace",
                **hidden_process_kwargs(),
            )
            if result.returncode != 0:
                self.logger.warning(
                    f"⚠️ 微博 yt-dlp 下载失败，返回码 {result.returncode}: {url}"
                )
                self._log_ytdlp_failure("微博下载", result)
                return None

            prefix = output_tpl.replace("%(ext)s", "")
            matched_files = [
                os.path.join(tmp_dir, filename)
                for filename in os.listdir(tmp_dir)
                if os.path.isfile(os.path.join(tmp_dir, filename))
                and os.path.join(tmp_dir, filename).startswith(prefix)
                and not filename.endswith((".part", ".temp", ".ytdl"))
            ]
            if not matched_files:
                self.logger.warning(f"⚠️ 微博下载未找到输出文件: {url}")
                return None

            matched_files.sort(key=os.path.getmtime, reverse=True)
            downloaded_path = matched_files[0]
            self.logger.info(f"✅ 微博视频下载成功: {downloaded_path}")
            return downloaded_path
        except subprocess.TimeoutExpired:
            self.logger.warning(f"⚠️ 微博 yt-dlp 下载超时（>{timeout_sec}s）: {url}")
            return None
        except FileNotFoundError:
            self.logger.error("❌ 未找到 yt-dlp 命令，微博视频下载已跳过")
            return None
        except Exception as e:
            self.logger.error(f"❌ 微博视频下载异常: {e}", exc_info=True)
            return None

    def _check_bilibili_duration(self, url: str) -> Optional[int]:
        """检查B站视频时长，返回秒数。若失败则返回 None"""
        yt_dlp_timeout_sec = 20
        try:
            self.logger.info(f"⏱️ 正在检查视频时长: {url}")
            bvid_match = re.search(r"BV[0-9A-Za-z]+", url)
            if bvid_match:
                bvid = bvid_match.group(0)
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Referer": "https://www.bilibili.com/",
                }
                try:
                    session = requests.Session()
                    session.trust_env = False
                    api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
                    resp = session.get(api_url, headers=headers, timeout=8)
                    payload = resp.json() if resp.ok else {}
                    data = payload.get("data", {}) if isinstance(payload, dict) else {}
                    duration = data.get("duration")
                    if isinstance(duration, (int, float)) and duration > 0:
                        total_seconds = int(duration)
                        self.logger.info(f"✅ B站 API 视频时长检测: {total_seconds}秒")
                        return total_seconds
                    if isinstance(duration, str) and duration.isdigit():
                        total_seconds = int(duration)
                        self.logger.info(f"✅ B站 API 视频时长检测: {total_seconds}秒")
                        return total_seconds
                    pages = data.get("pages", [])
                    if isinstance(pages, list) and pages:
                        page_duration = pages[0].get("duration") if isinstance(pages[0], dict) else None
                        if isinstance(page_duration, (int, float)) and page_duration > 0:
                            total_seconds = int(page_duration)
                            self.logger.info(f"✅ B站 API 分P时长检测: {total_seconds}秒")
                            return total_seconds
                    self.logger.warning(f"⚠️ B站 API 未返回有效时长: {payload.get('message') if isinstance(payload, dict) else 'unknown'}")
                except Exception as e:
                    self.logger.warning(f"⚠️ B站 API 获取视频时长失败，回退 yt-dlp: {e}")

            # 使用 yt-dlp --get-duration 获取视频时长
            cookies_path = self._get_bili_cookies_path()
            cmd = [*self.yt_dlp_command, '--get-duration', '--no-playlist', '--proxy', '']

            # 添加反爬和身份标识
            cmd.extend(['--add-headers', 'User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'])
            cmd.extend(['--add-headers', 'Referer:https://www.bilibili.com/'])

            if os.path.exists(cookies_path):
                cmd.extend(['--cookies', cookies_path])

            cmd.append(url)
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=yt_dlp_timeout_sec,
                encoding='utf-8', errors='replace',
                **hidden_process_kwargs(),
            )

            if result.returncode != 0:
                self._log_ytdlp_failure("Bilibili 时长检测", result)
                return None

            duration_str = result.stdout.strip()
            if not duration_str:
                self.logger.warning("⚠️ 无法解析视频时长，yt-dlp 返回为空")
                return None

            # 解析时间字符串
            parts = duration_str.split(':')
            total_seconds = 0
            if len(parts) == 3:  # HH:MM:SS
                total_seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            elif len(parts) == 2:  # MM:SS
                total_seconds = int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 1:  # SS
                total_seconds = int(parts[0])
            else:
                self.logger.warning(f"⚠️ 无法识别的时间格式: {duration_str}")
                return None

            self.logger.info(f"✅ 视频时长检测: {duration_str} ({total_seconds}秒)")
            return total_seconds

        except subprocess.TimeoutExpired:
            self.logger.warning(f"⚠️ 获取视频时长超时 (超过{yt_dlp_timeout_sec}秒): {url}")
            return None
        except FileNotFoundError:
            self.logger.error("❌ 未找到 yt-dlp 命令，请确认已安装并加入系统 PATH")
            return None
        except Exception as e:
            self.logger.error(f"❌ 解析视频时长发生异常: {e}", exc_info=True)
            return None

    def _download_bilibili_video(self, url: str, max_720p: bool = False) -> Optional[str]:
        """使用 yt-dlp 下载 Bilibili 视频"""
        # 创建临时目录
        tmp_dir = self._temp_dir("videos")

        filename = f"bilibili_{int(time.time())}_{uuid.uuid4().hex[:8]}.mp4"
        filepath = os.path.join(tmp_dir, filename)

        try:
            self.logger.info(f"⬇️ 开始使用 yt-dlp 下载视频 (Max 720p: {max_720p}): {url}")

            # 使用 yt-dlp 下载视频并合并为 mp4。
            format_str = 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best' if max_720p else 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best'

            cookies_path = self._get_bili_cookies_path()
            cmd = [
                *self.yt_dlp_command,
                '-f', format_str,
                '--ffmpeg-location', self.ffmpeg_bin,
                '--merge-output-format', 'mp4',
                '--no-playlist',
                '--max-filesize', f"{getattr(self, 'max_artifact_size_mb', 512)}M",
                '--proxy', '',
                '-o', filepath
            ]

            if self.bilibili_burn_danmu:
                cmd.extend([
                    '--write-subs', '--sub-langs', 'danmaku',
                    '--use-postprocessor', f"danmaku:font_size={self.danmaku_font_size};line_spacing={self.danmaku_line_spacing};display_region_ratio={self.danmaku_display_region_ratio}"
                ])

            cmd.extend([
                '--add-headers', 'User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                '--add-headers', 'Referer:https://www.bilibili.com/'
            ])

            if os.path.exists(cookies_path):
                cmd.extend(['--cookies', cookies_path])

            cmd.append(url)

            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=600, # 增加超时到10分钟
                encoding='utf-8', errors='replace',
                **hidden_process_kwargs(),
            )

            if result.returncode == 0 and os.path.exists(filepath):
                self.logger.info(f"✅ Bilibili 视频下载成功！文件已保存至: {filepath}")
                return filepath
            else:
                self.logger.error(f"❌ yt-dlp 下载失败，返回码: {result.returncode}")
                self._log_ytdlp_failure("Bilibili 下载", result)
                return None

        except subprocess.TimeoutExpired:
            self.logger.error(f"❌ 下载 Bilibili 视频超时 (超过600秒): {url}")
            return None

    def _parse_ass_time_to_seconds(self, value: str) -> Optional[float]:
        """Parse ASS timestamp like H:MM:SS.cc into seconds."""
        parts = value.strip().split(":")
        if len(parts) != 3:
            return None
        try:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
        except ValueError:
            return None
        return hours * 3600 + minutes * 60 + seconds

    def _limit_danmaku_ass_file(self, ass_path: str) -> Tuple[int, int]:
        """Limit danmaku Dialogue lines per time window for mobile-friendly viewing."""
        window_seconds = self.danmaku_limit_window_seconds
        max_per_window = self.danmaku_max_per_window
        if window_seconds <= 0 or max_per_window <= 0:
            return 0, 0

        try:
            with open(ass_path, "r", encoding="utf-8-sig", errors="replace") as f:
                lines = f.readlines()
        except OSError as exc:
            self.logger.warning(f"⚠️ 读取弹幕文件失败，跳过弹幕限流: {exc}")
            return 0, 0

        buckets: Dict[int, List[Tuple[int, float]]] = {}
        always_keep_indices: Set[int] = set()
        dialogue_count = 0
        for idx, line in enumerate(lines):
            if not line.startswith("Dialogue:"):
                continue
            dialogue_count += 1
            payload = line[len("Dialogue:"):].strip()
            fields = payload.split(",", 9)
            if len(fields) < 10:
                always_keep_indices.add(idx)
                continue
            start_seconds = self._parse_ass_time_to_seconds(fields[1])
            if start_seconds is None:
                always_keep_indices.add(idx)
                continue
            bucket_key = int(start_seconds // window_seconds)
            buckets.setdefault(bucket_key, []).append((idx, start_seconds))

        keep_indices: Set[int] = set(always_keep_indices)
        for entries in buckets.values():
            entries.sort(key=lambda item: item[1])
            if len(entries) <= max_per_window:
                keep_indices.update(idx for idx, _ in entries)
                continue

            if max_per_window == 1:
                selected_positions = [len(entries) // 2]
            else:
                selected_positions = [
                    round(i * (len(entries) - 1) / (max_per_window - 1))
                    for i in range(max_per_window)
                ]
            keep_indices.update(entries[pos][0] for pos in selected_positions)

        if dialogue_count == 0:
            return 0, 0

        kept_dialogues = 0
        filtered_lines: List[str] = []
        for idx, line in enumerate(lines):
            if line.startswith("Dialogue:"):
                if idx not in keep_indices:
                    continue
                kept_dialogues += 1
            filtered_lines.append(line)

        if kept_dialogues == dialogue_count:
            return dialogue_count, dialogue_count

        try:
            with open(ass_path, "w", encoding="utf-8", newline="") as f:
                f.writelines(filtered_lines)
        except OSError as exc:
            self.logger.warning(f"⚠️ 写入弹幕限流结果失败，使用原始弹幕: {exc}")
            return dialogue_count, dialogue_count

        return dialogue_count, kept_dialogues

    def _get_bilibili_webmask_info(self, source_url: str) -> Optional[Tuple[str, int]]:
        """Fetch official Bilibili webmask URL and FPS. No local segmentation is used."""
        if not source_url:
            return None

        bvid_match = re.search(r"BV[0-9A-Za-z]+", source_url)
        if not bvid_match:
            return None

        bvid = bvid_match.group(0)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
        }

        try:
            view_resp = requests.get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bvid},
                headers=headers,
                timeout=12,
            )
            view_resp.raise_for_status()
            view_data = view_resp.json()
            pages = ((view_data.get("data") or {}).get("pages") or [])
            if not pages:
                return None
            cid = pages[0].get("cid")
            if not cid:
                return None

            for endpoint in (
                "https://api.bilibili.com/x/player/wbi/v2",
                "https://api.bilibili.com/x/player/v2",
            ):
                try:
                    player_resp = requests.get(
                        endpoint,
                        params={"bvid": bvid, "cid": cid},
                        headers=headers,
                        timeout=12,
                    )
                    player_resp.raise_for_status()
                    player_data = player_resp.json()
                except Exception as exc:
                    self.logger.debug(f"B站 webmask 接口请求失败，继续尝试下一个端点: {endpoint}, {exc}")
                    continue
                dm_mask = ((player_data.get("data") or {}).get("dm_mask") or {})
                mask_url = dm_mask.get("mask_url")
                if not mask_url:
                    continue
                if mask_url.startswith("//"):
                    mask_url = "https:" + mask_url
                elif mask_url.startswith("/"):
                    mask_url = "https://www.bilibili.com" + mask_url
                fps = int(dm_mask.get("fps") or 30)
                self.logger.info(f"✅ 获取到 B站 webmask: cid={cid}, fps={fps}")
                return mask_url, fps
        except Exception as exc:
            self.logger.warning(f"⚠️ 获取 B站 webmask 信息失败，将回退普通弹幕压制: {exc}")

        return None

    def _download_bilibili_webmask(self, mask_url: str, target_path: str) -> bool:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
        }
        try:
            resp = requests.get(mask_url, headers=headers, timeout=30)
            resp.raise_for_status()
            content = resp.content
            if len(content) < 16 or content[:4] != b"MASK":
                self.logger.warning("⚠️ webmask 文件格式无效，将回退普通弹幕压制")
                return False
            with open(target_path, "wb") as f:
                f.write(content)
            self.logger.info(f"✅ webmask 下载完成: {len(content)} bytes")
            return True
        except Exception as exc:
            self.logger.warning(f"⚠️ 下载 webmask 失败，将回退普通弹幕压制: {exc}")
            return False

    def _extract_webmask_svg_frames(self, webmask_path: str, output_dir: str) -> int:
        """Extract official webmask SVG frames from .webmask file."""
        try:
            with open(webmask_path, "rb") as f:
                buf = f.read()

            if len(buf) < 16 or buf[:4] != b"MASK":
                return 0

            segment_count = struct.unpack(">i", buf[12:16])[0]
            if segment_count <= 0:
                return 0

            offsets: List[int] = []
            for idx in range(segment_count):
                pos = 16 + idx * 16
                if pos + 16 > len(buf):
                    return 0
                offsets.append(struct.unpack(">q", buf[pos + 8:pos + 16])[0])

            os.makedirs(output_dir, exist_ok=True)
            frame_count = 0
            for idx, start in enumerate(offsets):
                end = offsets[idx + 1] if idx + 1 < len(offsets) else len(buf)
                if start < 0 or end <= start or end > len(buf):
                    continue
                try:
                    block = gzip.decompress(buf[start:end])
                except OSError:
                    continue

                for raw_frame in block.split(b"data:image/svg+xml;base64,")[1:]:
                    encoded_svg = raw_frame.split(b"\x00", 1)[0].strip()
                    if not encoded_svg:
                        continue
                    try:
                        svg = base64.b64decode(encoded_svg).decode("utf-8", errors="replace")
                    except Exception:
                        continue
                    frame_count += 1
                    frame_path = os.path.join(output_dir, f"mask_{frame_count - 1:06d}.svg")
                    with open(frame_path, "w", encoding="utf-8") as f:
                        f.write(svg)

            return frame_count
        except Exception as exc:
            self.logger.warning(f"⚠️ 解析 webmask 失败，将回退普通弹幕压制: {exc}")
            return 0

    def _probe_video_dimensions(self, video_path: str) -> Optional[Tuple[int, int]]:
        try:
            result = subprocess.run(
                [
                    self.ffprobe_bin, "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height",
                    "-of", "json",
                    video_path,
                ],
                capture_output=True,
                text=True,
                timeout=20,
                encoding="utf-8",
                errors="replace",
                **hidden_process_kwargs(),
            )
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout or "{}")
            stream = (data.get("streams") or [{}])[0]
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            if width > 0 and height > 0:
                return width, height
        except Exception:
            return None
        return None

    def _probe_video_codec(self, video_path: str) -> Optional[str]:
        try:
            result = subprocess.run(
                [
                    self.ffprobe_bin, "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=codec_name",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    video_path,
                ],
                capture_output=True,
                text=True,
                timeout=20,
                encoding="utf-8",
                errors="replace",
                **hidden_process_kwargs(),
            )
            if result.returncode == 0:
                codec = (result.stdout or "").strip().casefold()
                return codec or None
        except Exception:
            return None
        return None

    def _probe_video_duration_seconds(self, video_path: str) -> Optional[float]:
        try:
            result = subprocess.run(
                [
                    self.ffprobe_bin, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    video_path,
                ],
                capture_output=True,
                text=True,
                timeout=20,
                encoding="utf-8",
                errors="replace",
                **hidden_process_kwargs(),
            )
            if result.returncode == 0:
                duration = float((result.stdout or "").strip())
                if duration > 0:
                    return duration
        except Exception:
            return None
        return None

    def _parse_ffprobe_rate(self, value: Any) -> Optional[float]:
        if not value:
            return None
        try:
            text = str(value).strip()
            if "/" in text:
                numerator, denominator = text.split("/", 1)
                denominator_value = float(denominator)
                if denominator_value == 0:
                    return None
                rate = float(numerator) / denominator_value
            else:
                rate = float(text)
            if 1 <= rate <= 240:
                return rate
        except Exception:
            return None
        return None

    def _probe_video_frame_rate(self, video_path: str) -> Optional[float]:
        try:
            result = subprocess.run(
                [
                    self.ffprobe_bin, "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=avg_frame_rate,r_frame_rate",
                    "-of", "json",
                    video_path,
                ],
                capture_output=True,
                text=True,
                timeout=20,
                encoding="utf-8",
                errors="replace",
                **hidden_process_kwargs(),
            )
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout or "{}")
            stream = (data.get("streams") or [{}])[0]
            avg_rate = self._parse_ffprobe_rate(stream.get("avg_frame_rate"))
            real_rate = self._parse_ffprobe_rate(stream.get("r_frame_rate"))
            return avg_rate or real_rate
        except Exception:
            return None

    def _choose_video_output_fps(self, video_path: str, fallback_fps: float) -> int:
        video_fps = self._probe_video_frame_rate(video_path)
        chosen_fps = video_fps or fallback_fps or 30
        if chosen_fps < 24:
            chosen_fps = 30
        return max(24, min(60, int(round(chosen_fps))))

    def _resolve_media_tool(self, tool_name: str, plugin_name: str, configured_path: str = "") -> str:
        if configured_path and os.path.exists(configured_path):
            return configured_path
        if configured_path:
            self.logger.warning(f"⚠️ 配置的 {tool_name} 路径不存在，将尝试自动查找: {configured_path}")

        env_name = f"{tool_name.upper()}_PATH"
        env_path = (os.environ.get(env_name) or "").strip()
        if env_path and os.path.exists(env_path):
            return env_path
        if env_path:
            self.logger.warning(f"⚠️ 环境变量 {env_name} 指向的路径不存在，将尝试自动查找: {env_path}")

        ffmpeg_dir = str(get_config("ffmpeg_dir", plugin_name=plugin_name, default="") or "").strip()
        if ffmpeg_dir:
            candidate = os.path.join(ffmpeg_dir, f"{tool_name}.exe" if os.name == "nt" else tool_name)
            if os.path.exists(candidate):
                return candidate
            self.logger.warning(f"⚠️ 配置的 ffmpeg_dir 未找到 {tool_name}，将尝试自动查找: {candidate}")

        project_candidate = os.path.join(
            os.getcwd(),
            "tools",
            "ffmpeg",
            "bin",
            f"{tool_name}.exe" if os.name == "nt" else tool_name,
        )
        if os.path.exists(project_candidate):
            return project_candidate

        try:
            from static_ffmpeg import run as static_ffmpeg_run

            ffmpeg_path, ffprobe_path = (
                static_ffmpeg_run.get_or_fetch_platform_executables_else_raise()
            )
            bundled_path = ffmpeg_path if tool_name == "ffmpeg" else ffprobe_path
            if os.path.exists(bundled_path):
                return bundled_path
        except Exception as exc:
            self.logger.warning(f"⚠️ 自动准备 {tool_name} 失败，将尝试直接调用系统命令: {exc}")

        if os.name == "nt":
            for tool_dir in (
                "C:\\msys64\\ucrt64\\bin",
                "C:\\msys64\\mingw64\\bin",
            ):
                candidate = os.path.join(tool_dir, f"{tool_name}.exe")
                if os.path.exists(candidate):
                    return candidate

        resolved_path = shutil.which(tool_name)
        if resolved_path:
            return resolved_path

        return tool_name

    def _resolve_ytdlp_command(self) -> List[str]:
        """Use the active interpreter so moving the project cannot stale a pip launcher."""
        return [sys.executable, "-m", "yt_dlp"]

    def _run_platform_ytdlp(
        self,
        platform: str,
        arguments: List[str],
        *,
        timeout_sec: int,
        cookie_args: Optional[List[str]] = None,
    ) -> subprocess.CompletedProcess:
        """Run yt-dlp with cookies from the project's dedicated Chrome profile."""
        if cookie_args is not None:
            return subprocess.run(
                [
                    *self.yt_dlp_command,
                    "--ignore-config",
                    "--no-playlist",
                    "--max-filesize",
                    f"{getattr(self, 'max_artifact_size_mb', 512)}M",
                    *cookie_args,
                    *arguments,
                ],
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                encoding="utf-8",
                errors="replace",
                **hidden_process_kwargs(),
            )
        with ytdlp_browser_cookie_args(
            platform=platform,
            debug_port=self.chrome_debug_port,
            user_data_dir=self.chrome_user_data_dir,
            profile_dir=self.chrome_profile_dir,
            logger=self.logger,
        ) as cookie_args:
            return subprocess.run(
                [
                    *self.yt_dlp_command,
                    "--ignore-config",
                    "--no-playlist",
                    "--max-filesize",
                    f"{getattr(self, 'max_artifact_size_mb', 512)}M",
                    *cookie_args,
                    *arguments,
                ],
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                encoding="utf-8",
                errors="replace",
                **hidden_process_kwargs(),
            )

    def _find_ytdlp_output(self, output_template: str) -> Optional[str]:
        prefix = output_template.replace("%(ext)s", "")
        directory = os.path.dirname(output_template)
        candidates = [
            os.path.join(directory, filename)
            for filename in os.listdir(directory)
            if os.path.isfile(os.path.join(directory, filename))
            and os.path.join(directory, filename).startswith(prefix)
            and not filename.endswith((".part", ".temp", ".ytdl", ".json"))
        ]
        if not candidates:
            return None
        candidates.sort(key=os.path.getmtime, reverse=True)
        return candidates[0]

    def _log_ytdlp_failure(self, platform: str, result: subprocess.CompletedProcess) -> None:
        self.logger.warning(
            "⚠️ %s yt-dlp 失败，返回码 %s",
            platform,
            result.returncode,
        )
        command = getattr(result, "args", None)
        if command:
            rendered = command if isinstance(command, str) else subprocess.list2cmdline(command)
            self.logger.warning("⚠️ %s yt-dlp 调用命令: %s", platform, rendered)

        has_output = False
        for stream_name, output in (("stderr", result.stderr), ("stdout", result.stdout)):
            lines = (output or "").strip().splitlines()
            if not lines:
                continue
            has_output = True
            self.logger.warning(
                "⚠️ %s yt-dlp %s(尾部): %s",
                platform,
                stream_name,
                chr(10).join(lines[-5:]),
            )
        if not has_output:
            self.logger.warning("⚠️ %s yt-dlp 未返回 stdout/stderr", platform)

    def _escape_ffmpeg_filter_path(self, path: str) -> str:
        return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")

    def _remove_path_quietly(self, path: str) -> None:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            elif os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def _burn_bilibili_danmaku_with_webmask(
        self,
        video_path: str,
        danmaku_path: str,
        source_url: str,
        output_path: str,
    ) -> bool:
        """Burn danmaku while respecting Bilibili official webmask."""
        if not self.bilibili_danmaku_webmask_enabled or not source_url:
            return False

        webmask_info = self._get_bilibili_webmask_info(source_url)
        if not webmask_info:
            return False

        dimensions = self._probe_video_dimensions(video_path)
        if not dimensions:
            self.logger.warning("⚠️ 无法探测视频尺寸，将回退普通弹幕压制")
            return False
        width, height = dimensions

        base_path = video_path.rsplit(".", 1)[0]
        webmask_path = base_path + ".webmask"
        mask_svg_dir = base_path + "_webmask_svg"
        mask_url, mask_fps = webmask_info
        if mask_fps <= 0:
            mask_fps = 30

        if not self._download_bilibili_webmask(mask_url, webmask_path):
            return False

        frame_count = self._extract_webmask_svg_frames(webmask_path, mask_svg_dir)
        if frame_count <= 0:
            return False

        video_duration = self._probe_video_duration_seconds(video_path)
        if video_duration and frame_count / mask_fps < video_duration * 0.8:
            self.logger.warning(
                "⚠️ webmask 帧数明显短于视频，将回退普通弹幕压制: "
                f"frames={frame_count}, fps={mask_fps}, video={video_duration:.1f}s"
            )
            return False

        output_fps = self._choose_video_output_fps(video_path, mask_fps)
        self.logger.info(
            "🛡️ 启用 B站 webmask 防遮挡弹幕压制: "
            f"{frame_count} 帧, mask={mask_fps}fps, output={output_fps}fps, {width}x{height}"
        )

        safe_ass_path = self._escape_ffmpeg_filter_path(danmaku_path)
        mask_pattern = os.path.join(mask_svg_dir, "mask_%06d.svg")
        filter_complex = (
            f"[0:v]setpts=PTS-STARTPTS,fps={output_fps},format=gbrp,split=2[base][subbase];"
            f"[subbase]subtitles='{safe_ass_path}',format=gbrp[withsub];"
            f"[1:v]setpts=PTS-STARTPTS,fps={output_fps},format=rgba,"
            f"scale={width}:{height}:flags=bilinear,format=rgba,alphaextract,format=gbrp[mask];"
            f"[base][withsub][mask]maskedmerge=planes=7,format=yuv420p[v]"
        )
        ffmpeg_cmd = [
            self.ffmpeg_bin, "-y",
            "-i", video_path,
            "-thread_queue_size", "512",
            "-framerate", str(mask_fps),
            "-i", mask_pattern,
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-crf", str(self.bilibili_video_crf),
            "-preset", "slow",
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-movflags", "+faststart",
            output_path,
        ]

        try:
            result = subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                text=True,
                timeout=900,
                encoding="utf-8",
                errors="replace",
                **hidden_process_kwargs(),
            )
        except subprocess.TimeoutExpired:
            self.logger.warning("⚠️ webmask 弹幕压制超时，将回退普通弹幕压制")
            return False
        except Exception as exc:
            self.logger.warning(f"⚠️ webmask 弹幕压制异常，将回退普通弹幕压制: {exc}")
            return False

        if result.returncode != 0 or not os.path.exists(output_path):
            self.logger.warning(f"⚠️ webmask 弹幕压制失败，返回码: {result.returncode}")
            err_lines = result.stderr.strip().split("\n")
            if err_lines:
                self.logger.warning(f"webmask FFmpeg 倒数错误: {chr(10).join(err_lines[-5:])}")
            return False

        input_duration = video_duration
        output_duration = self._probe_video_duration_seconds(output_path)
        if input_duration and output_duration and output_duration < input_duration * 0.95:
            self.logger.warning(
                "⚠️ webmask 输出视频疑似被截断，将回退普通弹幕压制: "
                f"input={input_duration:.1f}s, output={output_duration:.1f}s"
            )
            return False

        self.logger.info(f"✅ webmask 防遮挡弹幕压制成功: {output_path}")
        return True

    def _process_bilibili_video(self, video_path: str, source_url: str = "") -> Optional[str]:
        """处理 Bilibili 视频：合并弹幕并确保微信兼容。"""
        if not video_path or not os.path.exists(video_path):
            return None

        if not self.bilibili_burn_danmu:
            self.logger.info("ℹ️ 弹幕压制开关已关闭，直接转换为微信兼容格式")
            return self._convert_to_wechat_compatible(video_path)

        # 弹幕文件探测：尝试多种可能的后缀
        base_path = video_path.rsplit('.', 1)[0]
        possible_danmaku_paths = [
            base_path + ".danmaku.ass",
            base_path + ".ass",
            base_path + ".zh-Hans.ass",
            base_path + ".zh-CN.ass"
        ]

        danmaku_path = None
        for p in possible_danmaku_paths:
            if os.path.exists(p):
                danmaku_path = p
                break

        if not danmaku_path:
            self.logger.info("ℹ️ 未发现压制所需的弹幕 (.ass) 文件，仅执行微信格式转换")
            return self._convert_to_wechat_compatible(video_path)

        original_count, kept_count = self._limit_danmaku_ass_file(danmaku_path)
        if original_count > 0:
            if kept_count < original_count:
                self.logger.info(
                    "📉 弹幕限流完成: "
                    f"{original_count} -> {kept_count} "
                    f"(窗口 {self.danmaku_limit_window_seconds:g}s 最多 {self.danmaku_max_per_window} 条)"
                )
            else:
                self.logger.info(
                    "ℹ️ 弹幕数量未超过限流阈值: "
                    f"{kept_count} 条 (窗口 {self.danmaku_limit_window_seconds:g}s 最多 {self.danmaku_max_per_window} 条)"
                )

        output_path = video_path.replace(".mp4", "_danmaku_wechat.mp4")
        try:
            base_path = video_path.rsplit(".", 1)[0]
            webmask_path = base_path + ".webmask"
            mask_svg_dir = base_path + "_webmask_svg"
            if self._burn_bilibili_danmaku_with_webmask(video_path, danmaku_path, source_url, output_path):
                self._remove_path_quietly(video_path)
                self._remove_path_quietly(danmaku_path)
                self._remove_path_quietly(webmask_path)
                self._remove_path_quietly(mask_svg_dir)
                return output_path
            self._remove_path_quietly(webmask_path)
            self._remove_path_quietly(mask_svg_dir)

            self.logger.info(f"🔥 正在压制弹幕并转换格式: {video_path}")

            # FFmpeg 的 subtitles 滤镜路径处理 (Windows 下)
            safe_ass_path = self._escape_ffmpeg_filter_path(danmaku_path)

            # 重新编码以烧入弹幕，同时应用微信兼容参数。
            ffmpeg_cmd = [
                self.ffmpeg_bin, '-y', '-i', video_path,
                '-vf', f"subtitles='{safe_ass_path}'",
                '-c:v', 'libx264',
                '-crf', str(self.bilibili_video_crf),
                '-preset', 'slow',
                '-pix_fmt', 'yuv420p',
                '-c:a', 'copy',
                '-movflags', '+faststart',
                output_path
            ]

            result = subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                text=True,
                timeout=600,
                encoding="utf-8",
                errors="replace",
                **hidden_process_kwargs(),
            )

            if result.returncode == 0 and os.path.exists(output_path):
                self.logger.info(f"✅ 弹幕压制与转换成功: {output_path}")
                # 清理临时文件
                try: os.remove(video_path)
                except Exception:
                    pass
                try: os.remove(danmaku_path)
                except Exception:
                    pass
                return output_path
            else:
                self.logger.error(f"❌ 弹幕压制失败，返回码: {result.returncode}")
                # 记录最后几行错误输出以供调试
                err_lines = result.stderr.strip().split('\n')
                if err_lines:
                    self.logger.error(f"FFmpeg 倒数错误: {chr(10).join(err_lines[-5:])}")
                return self._convert_to_wechat_compatible(video_path) # 失败则回退
        except Exception as e:
            self.logger.error(f"❌ 弹幕处理异常: {e}")
            return self._convert_to_wechat_compatible(video_path)

    def _convert_to_wechat_compatible(self, input_path: str) -> Optional[str]:
        """将视频转换为微信兼容格式"""
        if not input_path or not os.path.exists(input_path):
            return None

        output_path = input_path.replace(".mp4", "_wechat.mp4")
        try:
            self.logger.info(f"🔄 正在转换视频为微信兼容格式: {input_path}")
            # ffmpeg -i 输入文件名.mp4 -c:v libx264 -pix_fmt yuv420p -c:a copy -movflags +faststart 输出文件名.mp4
            result = subprocess.run(
                [
                    self.ffmpeg_bin,
                    "-y",
                    "-i",
                    input_path,
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "copy",
                    "-movflags",
                    "+faststart",
                    output_path,
                ],
                capture_output=True,
                text=True,
                timeout=600,
                encoding="utf-8",
                errors="replace",
                **hidden_process_kwargs(),
            )

            if result.returncode == 0 and os.path.exists(output_path):
                self.logger.info(f"✅ 微信兼容格式转换成功: {output_path}")
                # 尝试删除原文件以节省空间
                try: os.remove(input_path)
                except Exception:
                    pass
                return output_path
            else:
                self.logger.error(f"❌ 微信格式转换失败，返回码: {result.returncode}")
                return input_path # 失败则回退使用原文件
        except Exception as e:
            self.logger.error(f"❌ 微信格式转换异常: {e}")
            return input_path

    # -------------------------
    # Bilibili: Brainmap / ASR
    # -------------------------
