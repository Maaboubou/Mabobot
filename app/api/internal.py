#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
内部API - 用于服务间通信
"""

import logging
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from app.core.event_bus import EventBus, Event, EventType
from app.dependencies import get_event_bus_instance, get_wechat_manager_instance
from app.core.wechat_manager import WeChatManager
from app.models.base import SessionLocal
from app.models.user_permission import WeChatUser

router = APIRouter()
logger = logging.getLogger(__name__)

def check_summary_permission(chat_name: str) -> bool:
    """
    检查聊天是否启用了摘要功能

    Args:
        chat_name: 聊天名称

    Returns:
        是否启用摘要功能
    """
    try:
        # 从数据库查询用户权限
        db = SessionLocal()
        try:
            user = db.query(WeChatUser).filter(WeChatUser.chat_name == chat_name).first()
            if user:
                # 检查用户是否有摘要插件权限
                allowed_plugins = {p.plugin_name for p in user.permissions}
                summary_plugins = {"builtin_summary", "summary_plus"}
                # 支持多级目录插件名匹配（完整键）
                if allowed_plugins.intersection(summary_plugins):
                    return True
                # 检查末级简名匹配
                base_plugins = {p.rsplit('/', 1)[-1] for p in allowed_plugins}
                if base_plugins.intersection(summary_plugins):
                    return True
                return False
            else:
                logger.info("聊天 '%s' 未纳入管理，拒绝摘要能力", chat_name)
                return False
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"⚠️ 检查摘要权限失败: {e}")
        # Authorization lookup failures must fail closed.
        return False

class WeChatMessage(BaseModel):
    content: str
    sender: str
    sender_id: Optional[str] = None
    sender_remark: Optional[str] = None
    chat_name: str
    is_group: bool
    type: str
    mtype: str
    message_id: Optional[str] = None
    url: Optional[str] = None
    quote_image_path: Optional[str] = None
    quote_nickname: Optional[str] = None
    quote_content: Optional[str] = None
    has_quote_image: Optional[bool] = False
    quote_video_path: Optional[str] = None
    has_quote_video: Optional[bool] = False
    file_id: Optional[str] = None
    file_name: Optional[str] = None
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    file_sha256: Optional[str] = None
    file_status: Optional[str] = None
    file_error: Optional[str] = None
    has_quote_file: Optional[bool] = False
    quoted_file_id: Optional[str] = None
    quoted_file_name: Optional[str] = None
    quoted_file_path: Optional[str] = None
    quoted_file_size: Optional[int] = None
    quoted_file_sha256: Optional[str] = None
    quoted_file_status: Optional[str] = None
    quoted_file_candidate_count: Optional[int] = 0
    quoted_file_error: Optional[str] = None
    is_tickle: bool = False
    tickle_from: Optional[str] = None
    tickle_to: Optional[str] = None
    tickle_suffix: Optional[str] = None
    timestamp: float

@router.post("/wechat_message")
async def receive_wechat_message(
    message: WeChatMessage,
    event_bus: EventBus = Depends(get_event_bus_instance),
    wechat_manager: WeChatManager = Depends(get_wechat_manager_instance)
):
    """接收来自wx_bot的消息并发布到事件总线"""
    logger.debug(f"Received internal message from wx_bot: {message.content[:50]}")

    if not event_bus or not wechat_manager:
        logger.error("Event bus or WeChat manager not available")
        return {"status": "error", "message": "Core components not available"}

    # 根据消息类型确定事件类型
    # 优先根据显式URL判断为链接事件；其次检查mtype
    is_link_message = (message.url and isinstance(message.url, str) and message.url.startswith('http')) \
        or message.mtype == 'link' \
        or 'http' in message.content \
        or (message.type == 'other' and (message.content.strip().startswith('[链接]')))

    if message.is_tickle:
        event_type = EventType.TICKLE_MESSAGE_RECEIVED
    elif is_link_message:
        # 对于链接消息，先检查用户是否启用了摘要功能
        if check_summary_permission(message.chat_name):
            event_type = EventType.LINK_MESSAGE_RECEIVED
            logger.info(f"🔗 检测到URL内容，用户已启用摘要功能，发布LINK_MESSAGE_RECEIVED事件")
        else:
            logger.info(f"🔗 检测到URL内容，但聊天 '{message.chat_name}' 未启用摘要功能，跳过链接事件发布")
            # 将链接消息当作普通文本消息处理
            event_type = EventType.TEXT_MESSAGE_RECEIVED
    elif message.mtype == 'text':
        event_type = EventType.TEXT_MESSAGE_RECEIVED
    elif message.mtype == 'image':
        event_type = EventType.IMAGE_MESSAGE_RECEIVED
    elif message.mtype == 'quote':
        # 细分引用消息类型
        quote_content = message.quote_content or ""
        if message.has_quote_video or "[视频]" in quote_content:
            event_type = EventType.QUOTE_VIDEO_MESSAGE_RECEIVED
        elif "[图片]" in quote_content:
            event_type = EventType.QUOTE_IMAGE_MESSAGE_RECEIVED
        else:
            event_type = EventType.QUOTE_TEXT_MESSAGE_RECEIVED

        # 同时发送通用的QUOTE事件以保持向后兼容
        general_quote_event = Event(
            type=EventType.QUOTE_MESSAGE_RECEIVED,
            source="wx_bot_internal",
            data={
                "message": message.content,
                "sender": message.sender,
                "sender_id": message.sender_id,
                "sender_remark": message.sender_remark,
                "chat_name": message.chat_name,
                "chat_type": "group" if message.is_group else "user",
                "message_type": message.mtype,
                "message_id": message.message_id,
                "url": message.url,
                "quote_image_path": message.quote_image_path,
                "quote_nickname": message.quote_nickname,
                "quote_content": message.quote_content,
                "has_quote_image": message.has_quote_image,
                "quote_video_path": message.quote_video_path,
                "has_quote_video": message.has_quote_video,
                "has_quote_file": message.has_quote_file,
                "quoted_file_id": message.quoted_file_id,
                "quoted_file_name": message.quoted_file_name,
                "quoted_file_path": message.quoted_file_path,
                "quoted_file_size": message.quoted_file_size,
                "quoted_file_sha256": message.quoted_file_sha256,
                "quoted_file_status": message.quoted_file_status,
                "quoted_file_candidate_count": message.quoted_file_candidate_count,
                "quoted_file_error": message.quoted_file_error,
                "timestamp": message.timestamp
            },
            context={
                "wx": wechat_manager
            }
        )
        await event_bus.publish_async(general_quote_event)
    elif message.mtype == 'emotion':
        event_type = EventType.EMOTION_MESSAGE_RECEIVED
    elif message.mtype == 'voice':
        event_type = EventType.VOICE_MESSAGE_RECEIVED
    elif message.mtype == 'video':
        event_type = EventType.VIDEO_MESSAGE_RECEIVED
    elif message.mtype == 'file':
        event_type = EventType.FILE_MESSAGE_RECEIVED
    elif message.mtype == 'location':
        event_type = EventType.LOCATION_MESSAGE_RECEIVED
    elif message.mtype == 'merge':
        event_type = EventType.MERGE_MESSAGE_RECEIVED
    elif message.mtype == 'personal_card':
        event_type = EventType.PERSONAL_CARD_MESSAGE_RECEIVED
    elif message.mtype == 'note':
        event_type = EventType.NOTE_MESSAGE_RECEIVED
    elif message.mtype in {'other', 'miniapp', 'official', 'time', 'system'}:
        # mabowx 的公开类型集合比业务事件集合更细。当前没有这些类型的
        # 专用插件事件，显式归一化为 OTHER，避免把“已识别但暂无专用消费者”误报为
        # 未知类型；原始 message_type 仍保留在 event.data 中供后续升级使用。
        event_type = EventType.OTHER_MESSAGE_RECEIVED
    else:
        # 对于未知类型，发布通用OTHER事件而不是默认为TEXT
        logger.warning(f"未知消息类型 '{message.mtype}', 发布为OTHER_MESSAGE_RECEIVED事件")
        event_type = EventType.OTHER_MESSAGE_RECEIVED

    event = Event(
        type=event_type,
        source="wx_bot_internal",
        data={
            "message": message.content,
            "sender": message.sender,
            "sender_id": message.sender_id,
            "sender_remark": message.sender_remark,
            "chat_name": message.chat_name,
            "chat_type": "group" if message.is_group else "user",
            "message_type": message.mtype,
            "message_id": message.message_id,
            "url": message.url,
            "quote_image_path": message.quote_image_path,
            "quote_nickname": message.quote_nickname,
            "quote_content": message.quote_content,
            "has_quote_image": message.has_quote_image,
            "quote_video_path": message.quote_video_path,
            "has_quote_video": message.has_quote_video,
            "file_id": message.file_id,
            "file_name": message.file_name,
            "file_path": message.file_path,
            "file_size": message.file_size,
            "file_sha256": message.file_sha256,
            "file_status": message.file_status,
            "file_error": message.file_error,
            "has_quote_file": message.has_quote_file,
            "quoted_file_id": message.quoted_file_id,
            "quoted_file_name": message.quoted_file_name,
            "quoted_file_path": message.quoted_file_path,
            "quoted_file_size": message.quoted_file_size,
            "quoted_file_sha256": message.quoted_file_sha256,
            "quoted_file_status": message.quoted_file_status,
            "quoted_file_candidate_count": message.quoted_file_candidate_count,
            "quoted_file_error": message.quoted_file_error,
            "is_tickle": message.is_tickle,
            "tickle_from": message.tickle_from,
            "tickle_to": message.tickle_to,
            "tickle_suffix": message.tickle_suffix,
            "timestamp": message.timestamp
        },
        context={
            "wx": wechat_manager
        }
    )

    # 异步发布事件，避免阻塞请求
    await event_bus.publish_async(event)

    # 更新WeChatManager统计信息
    if wechat_manager:
        wechat_manager._stats['messages_received'] += 1
        wechat_manager._stats['last_message_time'] = message.timestamp

        # 更新聊天统计
        if message.chat_name in wechat_manager._listened_chats:
            wechat_manager._listened_chats[message.chat_name]['message_count'] += 1

    logger.info(f"Published {event.type.value} event for chat: {message.chat_name}")

    # 添加调试信息：检查用户权限
    from app.models.user_permission import WeChatUser
    from app.models.base import SessionLocal

    try:
        db = SessionLocal()
        user = db.query(WeChatUser).filter(WeChatUser.chat_name == message.chat_name).first()
        if user:
            plugin_permissions = [p.plugin_name for p in user.permissions]
            logger.info(f"🔧 User '{message.chat_name}' has permissions: {plugin_permissions}")
        else:
            logger.warning(f"⚠️ User '{message.chat_name}' not found in permissions table!")
    except Exception as e:
        logger.error(f"Error checking user permissions: {e}")
    finally:
        db.close()

    return {"status": "success"}

@router.get("/connectivity/websearch")
async def test_websearch_connectivity():
    """测试内置 DDGS 搜索工具，不再依赖独立搜索模型映射。"""
    import asyncio
    import time

    from app.services.local_web_search import LocalWebSearchService

    service = LocalWebSearchService(
        timeout_seconds=10,
        max_results=1,
        fetch_max_pages=0,
    )
    started_at = time.perf_counter()
    try:
        results = await asyncio.to_thread(
            service.search,
            ["DDGS connectivity test"],
            fetch_pages=False,
        )
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        return {
            "status": "success",
            "provider": "ddgs",
            "ok": bool(results),
            "latency_ms": latency_ms,
            "result_count": len(results),
            "message": "Success" if results else "DDGS 未返回结果",
        }
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        return {
            "status": "error",
            "provider": "ddgs",
            "ok": False,
            "latency_ms": latency_ms,
            "message": f"{type(exc).__name__}: {str(exc)[:160]}",
        }
