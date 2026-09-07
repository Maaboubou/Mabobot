"""Platform extraction/routing helpers for summary_plus."""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.core.event_bus import Event
from app.services.runtime_operations import OperationContext

from .runtime_support import DispatchDecision, ProcessingResult

__all__ = [
    "handle_link_message",
]


@dataclass(frozen=True)
class PlatformRoute:
    name: str
    extract_share_url: Callable[[Any, str, str], Optional[str]]
    async_handler: Callable[..., Optional[bool]]


def _send_files(
    wx: Any,
    chat_name: str,
    file_path: str,
    logger: logging.Logger,
    svc: Any = None,
) -> None:
    artifacts = getattr(svc, "artifacts", None)
    if artifacts is not None:
        artifacts.validate_file(file_path)
    if hasattr(wx, "send_files"):
        wx.send_files(chat_name, [file_path])
        return
    if hasattr(wx, "SendFiles"):
        wx.SendFiles(file_path, chat_name)
        return
    logger.error("❌ 无法发送文件，未找到发送方法")


def _send_summary_reply(
    wx: Any,
    chat_name: str,
    message_id: Optional[str],
    summary: str,
    logger: logging.Logger,
) -> bool:
    """优先引用原链接消息回复；不可用或失败时回退普通发送。"""
    quote_message = getattr(wx, "quote_message", None)
    if message_id and callable(quote_message):
        try:
            if quote_message(chat_name, message_id, summary):
                logger.info("✅ 网页摘要已引用原消息发送: %s:%s", chat_name, message_id)
                return True
            logger.warning("⚠️ 引用原消息发送摘要失败，回退普通发送: %s:%s", chat_name, message_id)
        except Exception as e:
            logger.warning("⚠️ 引用原消息发送摘要异常，回退普通发送: %s", e)

    send_message = getattr(wx, "send_message", None)
    if callable(send_message):
        return bool(send_message(chat_name, summary))

    logger.error("❌ 无法发送网页摘要，未找到 send_message 方法")
    return False


def _get_url_from_message(event: Event, message: str) -> Optional[str]:
    url = event.data.get("url")
    if url:
        return url
    url_match = re.search(r'(https?://[^\s<>\"]+)', message or "")
    return url_match.group(1) if url_match else None


def _resolve_pending_link_url(event: Event, wx: Any, chat_name: str, logger: logging.Logger) -> Optional[str]:
    """当 wx_bot 只缓存了链接卡片时，按需请求真实 URL。"""
    if not wx or not hasattr(wx, "resolve_link_url"):
        return None

    message_id = event.data.get("message_id")
    if not message_id:
        return None

    try:
        logger.info(f"🔗 链接事件未携带URL，按需解析链接卡片: {chat_name}:{message_id}")
        url = wx.resolve_link_url(chat_name, message_id)
        if url:
            event.data["url"] = url
            return url
    except Exception as e:
        logger.warning(f"⚠️ 按需解析链接卡片URL失败: {e}")
    return None


def _handle_douyin_async(
    svc: Any,
    wx: Any,
    chat_name: str,
    share_url: str,
    logger: logging.Logger,
    message_id: Optional[str] = None,
) -> None:
    try:
        if not wx:
            logger.info("ℹ️ 未找到 wx 上下文，跳过抖音视频下载")
            return

        video_info = {}
        duration = svc._check_douyin_duration(share_url, metadata_capture=video_info)
        max_download_duration = max(
            1,
            int(getattr(svc, "douyin_max_download_duration", 300)),
        )
        if duration is not None and duration > max_download_duration:
            logger.info(
                "ℹ️ 抖音视频 %s 时长 %ss > %ss，进入本地 ASR 脑图制作逻辑。",
                share_url,
                duration,
                max_download_duration,
            )
            if not getattr(svc, "local_asr_enabled", True):
                logger.info("ℹ️ 抖音长视频本地 ASR 已关闭，跳过脑图制作。")
                return

            max_asr_minutes = max(
                1,
                int(getattr(svc, "local_asr_max_duration_minutes", 35)),
            )
            if duration > max_asr_minutes * 60:
                logger.info(
                    "ℹ️ 抖音视频 %s 时长 %ss，超过本地 ASR 上限 %s 分钟，直接忽略。",
                    share_url,
                    duration,
                    max_asr_minutes,
                )
                return

            article_text = svc._douyin_transcribe_local(share_url)
            if article_text:
                asyncio.run(
                    svc._generate_douyin_mindmap_async(
                        share_url,
                        wx,
                        chat_name,
                        article_text=article_text,
                    )
                )
            return

        if duration is None:
            logger.info("ℹ️ 抖音视频时长获取失败，兼容回退为直接下载。")
        else:
            logger.info(
                "ℹ️ 抖音视频 %s 时长 %ss <= %ss，继续下载视频。",
                share_url,
                duration,
                max_download_duration,
            )

        video_path = svc._download_douyin_with_ytdlp(
            share_url, timeout_sec=180, video_info=video_info or None,
        )
        if video_path:
            _send_files(wx, chat_name, video_path, logger, svc)
            return

        logger.info("🔄 抖音 yt-dlp 下载失败，回退 TikHub")
        video_url_list = svc.parse_douyin_video(share_url)
        if video_url_list:
            video_path = svc._download_video(video_url_list)
            if video_path:
                _send_files(wx, chat_name, video_path, logger, svc)
                return
            logger.info("❌ 视频下载失败")
    except Exception as e:
        logger.error(f"❌ 异步处理抖音视频失败: {e}", exc_info=True)
        raise


def _handle_tiktok_async(
    svc: Any,
    wx: Any,
    chat_name: str,
    share_url: str,
    logger: logging.Logger,
    message_id: Optional[str] = None,
) -> None:
    try:
        video_url_list = svc.parse_tiktok_video(share_url)
        if video_url_list and wx:
            video_path = svc._download_video(video_url_list)
            if video_path:
                _send_files(wx, chat_name, video_path, logger, svc)
                return
            logger.error(f"❌ TikTok 视频下载失败: {share_url}")
    except Exception as e:
        logger.error(f"❌ 异步处理 TikTok 视频失败: {e}", exc_info=True)
        raise


def _handle_weibo_async(
    svc: Any,
    wx: Any,
    chat_name: str,
    share_url: str,
    logger: logging.Logger,
    message_id: Optional[str] = None,
) -> None:
    try:
        logger.info(f"📺 检测到微博链接，开始使用 yt-dlp 下载: {share_url}")
        if not wx:
            logger.info("ℹ️ 未找到 wx 上下文，跳过微博视频下载")
            return

        video_path = svc._download_weibo_video(share_url, timeout_sec=180)
        if video_path:
            _send_files(wx, chat_name, video_path, logger, svc)
            logger.info(f"✅ 微博视频发送完成: {share_url}")
            return
        logger.error(f"❌ 微博视频下载失败: {share_url}")
    except Exception as e:
        logger.error(f"❌ 异步处理微博视频失败: {e}", exc_info=True)
        raise


def _handle_bilibili_async(
    svc: Any,
    wx: Any,
    chat_name: str,
    share_url: str,
    logger: logging.Logger,
    message_id: Optional[str] = None,
) -> None:
    try:
        if not svc._ensure_bili_cookie_login_ready(wx=wx, chat_name=chat_name):
            logger.warning("⚠️ B站 Cookie 未就绪，将继续尝试后续字幕/API/ASR流程。")

        duration = svc._check_bilibili_duration(share_url)
        if duration is None:
            logger.info(f"ℹ️ Bilibili 视频 {share_url} 获取时长失败，尝试直接获取字幕进行脑图制作。")
            article_text = svc._bili_get_subtitles(share_url)
            if article_text:
                asyncio.run(svc._generate_bilibili_mindmap_async(share_url, wx, chat_name, article_text=article_text))
            return

        if duration <= 60:
            logger.info(f"ℹ️ Bilibili 视频 {share_url} 时长 {duration}s <= 1 分钟，下载 1080p。")
            if wx:
                video_path = svc._download_bilibili_video(share_url, max_720p=False)
                if video_path:
                    video_path = svc._process_bilibili_video(video_path, source_url=share_url)
                    _send_files(wx, chat_name, video_path, logger, svc)
                else:
                    logger.error(f"❌ Bilibili 视频下载失败: {share_url}")
            return

        max_download_duration = getattr(svc, "bilibili_max_download_duration", 300)
        if duration <= max_download_duration:
            logger.info(f"ℹ️ Bilibili 视频 {share_url} 时长 {duration}s <= {max_download_duration}s，下载 720p。")
            if wx:
                video_path = svc._download_bilibili_video(share_url, max_720p=True)
                if video_path:
                    video_path = svc._process_bilibili_video(video_path, source_url=share_url)
                    _send_files(wx, chat_name, video_path, logger, svc)
                else:
                    logger.error(f"❌ Bilibili 视频下载失败: {share_url}")
            return

        logger.info(
            "ℹ️ Bilibili 视频 %s 时长 %ss > %ss，进入脑图制作逻辑。",
            share_url,
            duration,
            max_download_duration,
        )
        article_text = svc._bili_get_subtitles(share_url)
        if article_text:
            if duration < 10800:
                logger.info(f"✅ 成功获取字幕，时长 {duration}s < 3 小时，利用字幕制作脑图。")
                asyncio.run(svc._generate_bilibili_mindmap_async(share_url, wx, chat_name, article_text=article_text))
            else:
                logger.info(f"ℹ️ Bilibili 视频 {share_url} 时长 {duration}s >= 180 分钟，直接忽略。")
            return

        if not getattr(svc, "local_asr_enabled", True):
            logger.info(f"ℹ️ Bilibili 视频 {share_url} 无字幕且本地 ASR 已关闭，直接忽略。")
            return

        max_asr_minutes = max(1, int(getattr(svc, "local_asr_max_duration_minutes", 35)))
        max_asr_seconds = max_asr_minutes * 60
        if duration > max_asr_seconds:
            logger.info(
                "ℹ️ Bilibili 视频 %s 时长 %ss，超过本地 ASR 上限 %s 分钟且无字幕，直接忽略。",
                share_url,
                duration,
                max_asr_minutes,
            )
            return

        logger.info(
            "ℹ️ Bilibili 视频 %s 时长 %ss，无字幕，使用本地 SenseVoice ASR 制作脑图。",
            share_url,
            duration,
        )
        article_text = svc._bili_transcribe_local(share_url)
        if article_text:
            asyncio.run(
                svc._generate_bilibili_mindmap_async(
                    share_url,
                    wx,
                    chat_name,
                    article_text=article_text,
                )
            )
    except Exception as e:
        logger.error(f"❌ 异步处理 Bilibili 视频失败: {e}", exc_info=True)
        raise


def _handle_xhs_async(
    svc: Any,
    wx: Any,
    chat_name: str,
    share_url: str,
    logger: logging.Logger,
    message_id: Optional[str] = None,
) -> bool:
    try:
        resolved_url = svc._xhs_resolve_share_url(share_url)
        file_path = svc.process_xhs_note(resolved_url)
        if file_path and wx:
            _send_files(wx, chat_name, file_path, logger, svc)
            return True

        summary = svc.summarize_xhs_note(resolved_url, chat_name=chat_name)
        if summary and wx:
            sent = _send_summary_reply(wx, chat_name, message_id, summary, logger)
            if sent and chat_name in getattr(svc, "special_translation_groups", set()):
                translated = svc.translate_text_for_special_group(summary)
                if translated:
                    wx.send_message(chat_name, translated)
            return sent

        logger.info("ℹ️ 小红书笔记处理完成，但没有可发送的媒体或摘要: %s", resolved_url)
        return False
    except Exception as e:
        logger.error(f"❌ 异步处理小红书视频/图片失败: {e}", exc_info=True)
        raise


def _handle_youtube_async(
    svc: Any,
    wx: Any,
    chat_name: str,
    youtube_url: str,
    logger: logging.Logger,
    message_id: Optional[str] = None,
) -> None:
    loop = None
    try:
        logger.info(f"🎥 检测到 YouTube 链接，启动脑图制作流程: {youtube_url}")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(svc._generate_youtube_mindmap_async(youtube_url, wx, chat_name))
    except Exception as e:
        logger.error(f"❌ YouTube 异步处理线程报错: {e}", exc_info=True)
        raise
    finally:
        if loop is not None:
            loop.close()


def _extract_douyin_url(svc: Any, url: str, message: str) -> Optional[str]:
    return svc._extract_douyin_share_url(url or "") or svc._extract_douyin_share_url(message)


def _extract_tiktok_url(svc: Any, url: str, message: str) -> Optional[str]:
    return svc._extract_tiktok_share_url(url or "") or svc._extract_tiktok_share_url(message)


def _extract_weibo_url(svc: Any, url: str, message: str) -> Optional[str]:
    return svc._extract_weibo_url(url or "") or svc._extract_weibo_url(message)


def _extract_bilibili_url(svc: Any, url: str, message: str) -> Optional[str]:
    return svc._extract_bilibili_share_url(url or "") or svc._extract_bilibili_share_url(message)


def _extract_xhs_url(svc: Any, url: str, message: str) -> Optional[str]:
    return svc._extract_xhs_share_url(url or "") or svc._extract_xhs_share_url(message)


def _extract_youtube_url(svc: Any, url: str, message: str) -> Optional[str]:
    return svc._extract_youtube_url(url or "") or svc._extract_youtube_url(message)


PLATFORM_ROUTES = (
    PlatformRoute("douyin", _extract_douyin_url, _handle_douyin_async),
    PlatformRoute("tiktok", _extract_tiktok_url, _handle_tiktok_async),
    PlatformRoute("weibo", _extract_weibo_url, _handle_weibo_async),
    PlatformRoute("bilibili", _extract_bilibili_url, _handle_bilibili_async),
    PlatformRoute("xiaohongshu", _extract_xhs_url, _handle_xhs_async),
    PlatformRoute("youtube", _extract_youtube_url, _handle_youtube_async),
)


def _dispatch_platform_route(
    route: PlatformRoute,
    svc: Any,
    wx: Any,
    chat_name: str,
    share_url: str,
    logger: logging.Logger,
    message_id: Optional[str] = None,
) -> DispatchDecision:
    """Submit a platform route to Runtime API v2's bounded dispatcher."""

    def worker(operation: OperationContext) -> ProcessingResult:
        operation.progress(10, f"正在处理 {route.name}")
        handled = route.async_handler(
            svc,
            wx,
            chat_name,
            share_url,
            logger,
            message_id,
        )
        if handled is False:
            return ProcessingResult.skipped(f"{route.name} 没有可发送内容")
        return ProcessingResult.completed(f"{route.name} 处理完成")

    dispatch = getattr(svc, "dispatch_operation", None)
    if not callable(dispatch):
        # Compatibility path for isolated unit tests and Runtime API v1.
        route.async_handler(svc, wx, chat_name, share_url, logger, message_id)
        return DispatchDecision(True, "compatibility")
    decision = dispatch(
        route.name,
        chat_name,
        share_url,
        worker,
        title=f"Summary Plus · {route.name}",
    )
    if not decision.accepted:
        logger.info("ℹ️ %s 任务未重复提交: %s", route.name, decision.reason)
    return decision


def _dispatch_hupu(
    svc: Any,
    wx: Any,
    chat_name: str,
    sender: str,
    share_url: str,
    message_id: Optional[str],
    logger: logging.Logger,
) -> DispatchDecision:
    def worker(operation: OperationContext) -> ProcessingResult:
        operation.progress(10, "正在下载虎扑视频")
        video_path = svc._download_hupu_video(share_url, timeout_sec=60)
        if video_path:
            _send_files(wx, chat_name, video_path, logger, svc)
            logger.info("✅ 虎扑视频发送完成: %s", share_url)
            return ProcessingResult.completed("虎扑视频发送完成", video_path)
        logger.info("ℹ️ 虎扑视频下载失败，回退常规摘要流程: %s", share_url)
        return _run_browser_summary(
            svc,
            wx,
            chat_name,
            sender,
            share_url,
            message_id,
            False,
            logger,
        )

    dispatch = getattr(svc, "dispatch_operation", None)
    if not callable(dispatch):
        worker(_CompatibilityOperation())
        return DispatchDecision(True, "compatibility")
    return dispatch("hupu", chat_name, share_url, worker, title="Summary Plus · 虎扑")


class _CompatibilityOperation:
    def progress(self, _percent: int, _message: str, **_details: Any) -> None:
        return None


def _run_browser_summary(
    svc: Any,
    wx: Any,
    chat_name: str,
    sender: str,
    url: str,
    message_id: Optional[str],
    is_link_message: bool,
    logger: logging.Logger,
) -> ProcessingResult:
    summary = svc.summarize_url(
        url,
        is_link_message=is_link_message,
        chat_name=chat_name,
        sender=sender,
    )
    logger.info("🧾 常规网页摘要流程返回: has_summary=%s", bool(summary))
    if not wx:
        return ProcessingResult.completed("摘要处理完成" if summary else "页面无可用摘要")
    if not summary:
        return ProcessingResult.skipped("页面无可用摘要")
    _send_summary_reply(wx, chat_name, message_id, summary, logger)
    if chat_name in svc.special_translation_groups:
        translated = svc.translate_text_for_special_group(summary)
        if translated:
            wx.send_message(chat_name, translated)
    return ProcessingResult.completed("网页摘要已发送")


def handle_link_message(event: Event, svc: Any, logger: logging.Logger) -> bool:
    """处理链接消息事件（平台直链下载/脑图 + 普通 URL 摘要）"""
    try:
        chat_name = event.data.get("chat_name", "")
        message = event.data.get("message", "") or ""
        url = _get_url_from_message(event, message)
        sender = event.data.get("sender", "")
        if svc._is_sender_blacklisted(sender):
            logger.info(f"🚫 Sender 黑名单命中：{sender}，跳过处理")
            return False

        wx = event.context.get("wx")

        if not url:
            url = _resolve_pending_link_url(event, wx, chat_name, logger)

        if url:
            logger.info(f"🔗 summary_plus 准备处理URL: chat={chat_name}, url={url[:180]}")

        if url and svc._is_blacklisted(url):
            logger.info(f"🚫 域名黑名单命中：{url}，跳过处理")
            return False

        if url and url != "LINK_MESSAGE_CLICKED":
            hupu_url = svc._extract_hupu_url(url) or svc._extract_hupu_url(message)
            if hupu_url:
                logger.info(f"🏀 检测到虎扑链接，优先尝试直返视频: {hupu_url}")
                if wx:
                    decision = _dispatch_hupu(
                        svc,
                        wx,
                        chat_name,
                        sender,
                        hupu_url,
                        event.data.get("message_id"),
                        logger,
                    )
                    if not decision.accepted:
                        logger.info("ℹ️ 虎扑任务未重复提交: %s", decision.reason)
                    return True
                else:
                    logger.info("ℹ️ 未找到 wx 上下文，跳过虎扑视频直返并继续摘要流程")

        for route in PLATFORM_ROUTES:
            share_url = route.extract_share_url(svc, url or "", message)
            if not share_url:
                continue
            _dispatch_platform_route(
                route,
                svc,
                wx,
                chat_name,
                share_url,
                logger,
                event.data.get("message_id"),
            )
            return True

        if not url:
            return False

        is_link_message = url == "LINK_MESSAGE_CLICKED"
        logger.info(
            "🧭 进入常规网页摘要流程: "
            f"is_link_message={is_link_message}"
        )
        dispatch = getattr(svc, "dispatch_operation", None)
        if callable(dispatch):
            decision = dispatch(
                "browser",
                chat_name,
                url,
                lambda operation: (
                    operation.progress(10, "正在提取网页内容"),
                    _run_browser_summary(
                        svc,
                        wx,
                        chat_name,
                        sender,
                        url,
                        event.data.get("message_id"),
                        is_link_message,
                        logger,
                    ),
                )[1],
                title="Summary Plus · 网页摘要",
            )
            if not decision.accepted:
                logger.info("ℹ️ 网页摘要任务未重复提交: %s", decision.reason)
            return True

        _run_browser_summary(
            svc,
            wx,
            chat_name,
            sender,
            url,
            event.data.get("message_id"),
            is_link_message,
            logger,
        )
        return True
    except Exception as e:
        logger.error(f"❌ Error handling link message: {e}", exc_info=True)
        return False
