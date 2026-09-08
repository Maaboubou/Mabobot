"""通过全局默认官方 Codex Profile，在独立进程中编辑或生成图片。"""

import asyncio
import base64
import hashlib
import time
import threading
import logging
import uuid
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

from app.core.event_bus import Event, EventType
from app.utils.plugin_config import get_config
from app.services.codex_profile_service import (
    CodexProfileError, get_codex_profile_service, _managed_profile_permission_roots,
)
from app.services.codex_proxy.client import CodexCliClient, CodexCliTimeoutError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@dataclass
class ImageDownload:
    """图片下载任务"""
    message_id: str
    status: str = "pending"  # pending, downloading, completed, failed
    file_path: Optional[str] = None
    error: Optional[str] = None

@dataclass
class EditSession:
    """图片编辑会话"""
    user_sender: str
    chat_name: str
    wx_manager: Any  # 保存wx_manager引用用于发送消息
    required_images: int  # 需要收集的图片数量
    target_images: Optional[int] = None  # 本次最多生成图片数量；None表示使用配置默认值
    images: List[str] = None  # 存储base64编码的图片
    text_description: Optional[str] = None  # 存储用户输入的文字描述（只有一条）
    image_downloads: Dict[str, ImageDownload] = None  # 图片下载任务
    status: str = "collecting"  # collecting, processing, completed
    processed_immediately: bool = False  # 是否已被立即处理

    def __post_init__(self):
        if self.images is None:
            self.images = []
        if self.image_downloads is None:
            self.image_downloads = {}

    def is_complete(self) -> bool:
        """检查是否收集完成"""
        return (len(self.images) >= self.required_images and
                self.text_description is not None)

    def can_add_image(self) -> bool:
        """是否可以添加更多图片"""
        return len(self.images) < self.required_images


class ImageEditorPlugin:
    """图片编辑器插件主类"""

    def _parse_trigger_word(self, message: str) -> tuple[int, Optional[int], Optional[str], Optional[str]]:
        """解析触发词。

        返回(参考图片数量, 生成目标数量, 触发词, 剩余文字)，不匹配则返回
        (0, None, None, None)。

        生成目标数量使用“+数字”表达，例如：
        - "P图0 +5"  => 参考图0张，最多生成5张
        - "P图 2+5" => 参考图2张，最多生成5张
        “+数字”会从剩余文字里移除，避免进入图片提示词。
        """
        import re

        # 匹配模式：触发词 + 可选参考图数量 + 可选剩余文字
        pattern = rf"^{re.escape(self.trigger_keyword)}\s*(\d*)\s*(.*)$"
        match = re.match(pattern, message.strip(), re.DOTALL)

        if match:
            count = 1  # 默认1张参考图
            target_images = None
            number_str = match.group(1).strip()
            remaining_text = match.group(2).strip()

            # 可选生成数量：识别任意位置的“+数字”，如 “+5”、“ +5”、“描述 +5”
            target_match = re.search(r"\+(\d+)", remaining_text)
            if target_match:
                try:
                    target_images = max(1, int(target_match.group(1)))
                except ValueError:
                    target_images = None
                # 从提示词中移除第一个 +数字 参数，并整理空白
                remaining_text = (
                    remaining_text[:target_match.start()] + " " + remaining_text[target_match.end():]
                )
                remaining_text = re.sub(r"\s+", " ", remaining_text).strip()

            if number_str:
                try:
                    count = int(number_str)
                    # 限制在0到max_images张参考图之间
                    if count < 0:
                        count = 0
                    elif count > self.max_images:  # 使用配置的最大参考图数
                        count = self.max_images
                except ValueError:
                    pass  # 如果转换失败，则使用默认值1
            return count, target_images, self.trigger_keyword, (remaining_text or None)

        # 如果不匹配，则返回 None
        return 0, None, None, None

    def __init__(self, context=None):
        self.context = context
        plugin_name = "image_editor"
        self.trigger_keyword = get_config("trigger_keyword", "P图", plugin_name=plugin_name)
        self.collect_timeout = int(get_config("collect_timeout", 120, plugin_name=plugin_name))
        self.max_images = int(get_config("max_images", 5, plugin_name=plugin_name))
        self.processing_timeout = max(1, int(get_config("processing_timeout", 300, plugin_name=plugin_name)))
        self.target_images = max(1, int(get_config("target_images", 1, plugin_name=plugin_name)))
        self.output_dir = (
            context.storage.generated_root / "images" if context is not None
            else Path("data/plugins/image_editor/generated/images")
        ).resolve()
        self._sessions: Dict[tuple[str, str], EditSession] = {}
        self._session_lock = threading.RLock()
        logger.info("🖼️ 图片编辑插件初始化完成，使用全局默认官方 Codex Profile")

    def _resolve_profile(self) -> Dict[str, Any]:
        service = get_codex_profile_service()
        profile_id = service.resolve_assistant_profile_id("")
        profile = service.get_profile(profile_id)
        if not profile:
            raise CodexProfileError("全局默认 Codex Profile 不存在")
        if profile.get("auth_type") != "chatgpt":
            raise CodexProfileError("图片编辑仅支持官方 ChatGPT 登录的 Codex Profile，请更换全局默认 Profile")
        if (profile.get("auth_source") == "local_cache"
                and profile.get("auth_sync_status") in {"missing", "outdated", "invalid"}):
            synced = service.sync_local_auth_if_needed(profile_id)
            profile = synced.get("profile") or service.get_profile(profile_id)
        if not profile or not profile.get("available"):
            raise CodexProfileError("全局默认 Codex Profile 尚未完成登录，请先完成官方 ChatGPT 认证")
        return profile

    def _session_key(self, chat_name: str, user_sender: str) -> tuple[str, str]:
        return chat_name, user_sender

    def _get_session(self, chat_name: str, user_sender: str) -> Optional[EditSession]:
        return self._sessions.get(self._session_key(chat_name, user_sender))

    def _remove_session(self, session: EditSession):
        key = self._session_key(session.chat_name, session.user_sender)
        if self._sessions.get(key) is session:
            self._sessions.pop(key, None)

    def _start_edit_session(
        self,
        user_sender: str,
        chat_name: str,
        wx_manager,
        required_images: int,
        target_images: Optional[int] = None,
    ) -> bool:
        """开始图片编辑会话"""
        with self._session_lock:
            target_log = target_images if target_images is not None else self.target_images
            logger.info(
                f"🖼️ 尝试开始新会话 - 用户: {user_sender}, 聊天: {chat_name}, "
                f"需要图片: {required_images}张, 最多生成: {target_log}张"
            )
            existing_session = self._get_session(chat_name, user_sender)
            if existing_session and existing_session.status == "collecting":
                logger.info(f"🖼️ 用户已有收集中会话，继续使用 - 用户: {user_sender}, 聊天: {chat_name}")
                return True

            # 创建新会话
            session = EditSession(
                user_sender=user_sender,
                chat_name=chat_name,
                wx_manager=wx_manager,
                required_images=required_images,
                target_images=target_images,
            )
            self._sessions[self._session_key(chat_name, user_sender)] = session

            logger.info(f"🖼️ 新会话创建成功 - 用户: {user_sender}, 配置超时: {self.collect_timeout}秒")

            # 立即开始倒计时，如果超时则取消会话
            self.context.workers.start(
                f"session-timeout-{chat_name}-{user_sender}-{time.time_ns()}",
                self._start_timeout_check,
                args=(chat_name, user_sender),
            )

            logger.info(f"🖼️ 开始图片编辑会话 - 用户: {user_sender}, 聊天: {chat_name}, 需要图片: {required_images}张")
            return True

    def _start_timeout_check(self, chat_name: str, user_sender: str):
        """开始倒计时检查，如果超时则取消会话"""
        logger.info(f"🖼️ 开始超时检查 - 用户: {user_sender}, 超时时间: {self.collect_timeout}秒")

        # 使用循环检查而不是阻塞sleep，可以被中断
        start_time = time.time()
        check_interval = 1.0  # 每秒检查一次

        while True:
            time.sleep(check_interval)
            elapsed = time.time() - start_time

            # 检查是否超时
            if elapsed >= self.collect_timeout:
                break

            # 检查会话状态，如果已被处理或取消，则提前退出
            with self._session_lock:
                session = self._get_session(chat_name, user_sender)
                if (not session or
                    session.status != "collecting" or
                    session.processed_immediately):
                    logger.info(f"🖼️ 超时检查提前结束 - 用户: {user_sender}, 原因: 会话已处理或取消")
                    return

        # 超时处理
        with self._session_lock:
            session = self._get_session(chat_name, user_sender)
            if (session and
                session.status == "collecting" and
                not session.processed_immediately):

                logger.info(f"🖼️ 收集时间结束 - 用户: {user_sender}, 会话将被取消")

                # 发送超时提示
                if session.wx_manager:
                    collected_images = len(session.images)
                    has_description = 1 if session.text_description else 0

                    if collected_images < session.required_images and not has_description:
                        session.wx_manager.send_message(
                            session.chat_name,
                            f"⏰ 收集时间结束，您只发送了{collected_images}张图片，缺少文字描述"
                        )
                    elif collected_images < session.required_images:
                        session.wx_manager.send_message(
                            session.chat_name,
                            f"⏰ 收集时间结束，您只发送了{collected_images}张图片，还需要{session.required_images - collected_images}张图片"
                        )
                    elif not has_description:
                        session.wx_manager.send_message(
                            session.chat_name,
                            f"⏰ 收集时间结束，您发送了{collected_images}张图片但缺少文字描述"
                        )

                # 清理会话
                self._remove_session(session)
            else:
                logger.info(f"🖼️ 超时检查完成但会话已处理 - 用户: {user_sender}")

    def _start_processing_immediately(self, session: EditSession):
        """立即开始处理（当收集完成时调用）"""
        if not session:
            return

        with self._session_lock:
            session.status = "processing"
            session.processed_immediately = True  # 标记为已立即处理

            logger.info(f"🖼️ 收集完成，立即开始处理 - 图片: {len(session.images)}张, 描述: {session.text_description[:50] if session.text_description else 'None'}...")

            if session.wx_manager:
                session.wx_manager.send_message(
                    session.chat_name,
                    f"🎨 已收集完成，开始编辑处理..."
                )

            # 启动处理线程
            self.context.workers.start(
                f"process-{session.chat_name}-{session.user_sender}-{time.time_ns()}",
                self._process_images,
                args=(session,),
            )

    def _detect_image_format(self, image_data: bytes) -> str:
        """检测图片格式"""
        if image_data.startswith(b'\xff\xd8\xff'):
            return 'JPEG'
        elif image_data.startswith(b'\x89PNG'):
            return 'PNG'
        elif image_data.startswith(b'GIF8'):
            return 'GIF'
        elif image_data.startswith(b'BM'):
            return 'BMP'
        elif image_data.startswith(b'RIFF') and image_data[8:12] == b'WEBP':
            return 'WEBP'
        else:
            return 'UNKNOWN'

    def _add_image_to_session(self, image_base64: str, chat_name: str, user_sender: str) -> bool:
        """向会话添加图片（静默处理）"""
        with self._session_lock:
            session = self._get_session(chat_name, user_sender)
            if not session or session.status != "collecting":
                return False

            if len(session.images) >= self.max_images:
                logger.info(f"🖼️ 已达到最大图片数量: {self.max_images}")
                return False

            session.images.append(image_base64)
            logger.info(f"🖼️ 静默添加图片 - 用户: {user_sender}, 当前图片数: {len(session.images)}")
            return True



    def _process_images(self, session: Optional[EditSession] = None):
        """每个任务只调用一次独立 Codex CLI，不使用聊天进程池或续接会话。"""
        if session is None:
            return
        sent_hashes = set()
        try:
            profile = self._resolve_profile()
            target = session.target_images or self.target_images
            task_dir = self.output_dir / uuid.uuid4().hex
            task_dir.mkdir(parents=True, exist_ok=False)
            content = [{"type": "text", "text": (
                f"请根据以下要求编辑或生成图片，生成 {target} 张独立图片文件。"
                f"全部 {len(session.images)} 张附件都是本次任务的参考图，请一起使用。\n"
                f"用户要求：{session.text_description or ''}"
            )}]
            mime_by_format = {
                "JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif",
                "BMP": "image/bmp", "WEBP": "image/webp",
            }
            for encoded in session.images:
                data = base64.b64decode(encoded, validate=True)
                mime = mime_by_format.get(self._detect_image_format(data))
                if not mime:
                    raise ValueError("参考图片格式无法识别，请重新发送图片")
                content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
            client = CodexCliClient(
                codex_bin=profile["wrapper_path"],
                workdir=str(task_dir),
                timeout_seconds=self.processing_timeout,
                permission_read_roots=_managed_profile_permission_roots(profile),
                codex_home=profile.get("codex_home"),
            )
            def send_image(path):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if digest in sent_hashes:
                    return
                if not self._send_image_file(session, path):
                    raise RuntimeError("图片已生成但发送失败，文件已保留")
                sent_hashes.add(digest)

            response = asyncio.run(client.chat({
                "model": profile["model"],
                "reasoning_effort": profile.get("reasoning_effort") or "high",
                "timeout": self.processing_timeout,
                "codex_runtime_profile": profile["name"],
                "codex_config_policy": "inherit",
                "codex_permission_profile": "mabobot-chat-isolated",
                "codex_approval_policy": "never",
                "codex_workdir": str(task_dir),
                "codex_artifact_root": str(task_dir / "artifacts"),
                "codex_source_chat_name": session.chat_name,
                "mabobot_allow_image_input": True,
                "messages": [
                    {"role": "system", "content": (
                        "你是图片编辑器。使用真实的图片生成/编辑能力完成任务，"
                        "保留用户要求的主体和细节。不要追问，不要启动子代理或额外 Codex 进程。"
                        "保留生图工具产生的全部完整版本，不要删除或覆盖。"
                        "宿主负责收集和发送图片，你无需查找、检查或搬运生成文件；完成生成后直接结束。"
                        "能力不可用时如实说明失败，禁止用代码绘制替代图片。"
                    )},
                    {"role": "user", "content": content},
                ],
            }, on_image=send_image))
            paths = []
            for attachment in response.get("attachments") or []:
                if attachment.get("type") != "image" or not attachment.get("path"):
                    continue
                path = Path(attachment["path"]).resolve()
                if not path.is_relative_to(task_dir) or not path.is_file() or path.stat().st_size <= 0:
                    continue
                if path not in paths:
                    paths.append(path)
            if not paths and not sent_hashes:
                raise RuntimeError("Codex 未返回生成图片，请检查该官方 Profile 的生图能力或任务记录")
            for path in paths:
                send_image(path)
        except Exception as exc:
            if sent_hashes and isinstance(exc, (CodexCliTimeoutError, TimeoutError)):
                logger.warning(
                    "🖼️ Codex 图片已交付，后续执行超时 - 聊天: %s, 用户: %s, 已发送: %d, 原因: %s",
                    session.chat_name, session.user_sender, len(sent_hashes), exc,
                )
            else:
                logger.exception("🖼️ Codex 图片编辑失败")
                self._send_error_message(session, str(exc))
        finally:
            with self._session_lock:
                session.status = "completed"
                self._remove_session(session)

    def _send_image_file(self, session: EditSession, path: Path) -> bool:
        """保留生成文件，避免微信发送 API 尚未读完文件。"""
        if not session.wx_manager:
            return False
        for attempt in range(1, 4):
            if session.wx_manager.send_files(session.chat_name, [str(path)]):
                return True
            if attempt < 3:
                time.sleep(attempt * 2)
        return False

    def _send_error_message(self, session: EditSession, error_msg: str, context_info: str = None):
        """发送错误消息"""
        try:
            logger.error(f"🖼️ 处理失败 - 用户: {session.user_sender}, 错误: {error_msg}")
            if session.wx_manager:
                if context_info:
                    full_msg = f"⚠️ 图片编辑失败：{error_msg}\n💡 上下文：{context_info}"
                else:
                    full_msg = f"⚠️ 图片编辑失败：{error_msg}"
                session.wx_manager.send_message(
                    session.chat_name,
                    full_msg
                )
        except Exception as e:
            logger.error(f"发送错误消息失败: {e}")

    def handle_text_message(self, event: Event) -> bool:
        """处理文本消息事件"""
        try:
            message = event.data.get("message", "").strip()
            sender = event.data.get("sender", "")
            chat_name = event.data.get("chat_name", "")
            wx_manager = event.context.get("wx")

            # 注意：权限检查已移至 EventBus 统一管理，此处不再检查 enabled_chats

                        # 检查是否在图片编辑会话中
            with self._session_lock:
                session = self._get_session(chat_name, sender)
                if session and session.status == "collecting":

                    # 在会话中，收集第一条文字描述
                    if session.text_description is None:
                        session.text_description = message.strip()
                        logger.info(f"🖼️ 收集到文字描述 - 用户: {sender}, 描述: {message[:50]}...")

                        # 检查是否可以开始处理
                        if session.is_complete():
                            self._start_processing_immediately(session)
                        return True  # 静默处理，不回复
                    else:
                        # 已经有了描述，忽略后续描述
                        return True
            # 解析触发词
            required_images, target_images, clean_trigger, immediate_text = self._parse_trigger_word(message)

            # 如果 clean_trigger 是 None，说明没有匹配到触发词
            if not clean_trigger:
                return False
            target_log = target_images if target_images is not None else self.target_images
            logger.info(
                f"🖼️ 检测到触发词 - 用户: {sender}, 聊天: {chat_name}, "
                f"需要图片: {required_images}张, 最多生成: {target_log}张"
            )

            # 开始新会话
            if self._start_edit_session(sender, chat_name, wx_manager, required_images, target_images=target_images):
                # 处理即时包含在触发消息中的文本
                if immediate_text:
                    with self._session_lock:
                        session = self._get_session(chat_name, sender)
                        if session:
                            session.text_description = immediate_text
                            logger.info(f"🖼️ 从触发消息中提取描述: {immediate_text[:50]}...")
                            if session.is_complete():
                                self._start_processing_immediately(session)
                                return True

                if wx_manager:
                    if required_images == 0:
                        wx_manager.send_message(
                            chat_name,
                            f"🎨 请输入您想要生成的图片描述\n"
                        )
                    elif required_images == 1:
                        wx_manager.send_message(
                            chat_name,
                            f"🎨 请发送需要编辑的图片和修改描述\n"
                        )
                    else:
                        wx_manager.send_message(
                            chat_name,
                            f"🎨 请发送{required_images}张需要编辑的图片和修改描述\n"
                        )
                return True

            return False

        except Exception as e:
            logger.error(f"🖼️ 处理文本消息失败: {e}")
            return False

    def handle_image_message(self, event: Event) -> bool:
        """处理图片消息事件"""
        try:
            sender = event.data.get("sender", "")
            chat_name = event.data.get("chat_name", "")

            logger.info(f"🖼️ handle_image_message called - sender: {sender}, chat: {chat_name}")

            # 检查是否有活跃会话且是同一用户
            with self._session_lock:
                session = self._get_session(chat_name, sender)
                logger.info(f"🖼️ 检查session - exists: {session is not None}, status: {session.status if session else 'None'}")

                if not session or session.status != "collecting":
                    logger.info(f"🖼️ Session check failed - sender: {sender}, status: {session.status if session else 'None'}")
                    return False

            # 获取图片消息ID
            message_id = event.data.get("message_id")

            if not message_id:
                logger.warning(f"🖼️ 图片消息缺少message_id")
                return False

            # 检查是否还可以添加图片
            with self._session_lock:
                session = self._get_session(chat_name, sender)
                if not session or not session.can_add_image():
                    logger.info(f"🖼️ 已达到所需图片数量或会话不存在，忽略此图片")
                    return True  # 静默忽略，不回复

                # 立即确认收到图片，不等待下载完成
                logger.info(f"🖼️ 收到图片消息 - 用户: {sender}, 消息ID: {message_id}")

                # 创建下载任务
                download_task = ImageDownload(message_id=message_id, status="pending")
                session.image_downloads[message_id] = download_task

            # 启动后台下载线程
            self.context.workers.start(
                f"download-{chat_name}-{message_id}-{time.time_ns()}",
                self._download_image_async,
                args=(chat_name, message_id, sender),
            )

            return True  # 静默处理，不回复

        except Exception as e:
            logger.error(f"🖼️ 处理图片消息失败: {e}")
            return False

    def handle_quote_image_message(self, event: Event) -> bool:
        """处理引用图片消息事件"""
        try:
            data = event.data
            context = event.context

            chat_name = data.get("chat_name", "")
            content = data.get("message", "").strip()
            quote_content = data.get("quote_content", "")
            sender = data.get("sender", "")
            wx_manager = context.get("wx")

            # 注意：权限检查已移至 EventBus 统一管理，此处不再检查 enabled_chats

            # 仅当引用里含图片时触发
            if "[图片]" not in quote_content:
                return False

            # 检查是否在图片编辑会话中
            with self._session_lock:
                session = self._get_session(chat_name, sender)
                if session and session.status == "collecting":

                    # 在会话中，处理引用图片和描述
                    if session.text_description is None:
                        # 设置描述
                        session.text_description = content.strip()
                        logger.info(f"🖼️ 收集到引用图片和文字描述 - 用户: {sender}, 描述: {content[:50]}...")

                        # 下载引用图片
                        message_id = data.get("message_id")
                        if message_id:
                            self.context.workers.start(
                                f"quote-download-{chat_name}-{message_id}-{time.time_ns()}",
                                self._download_quote_image_async,
                                args=(chat_name, message_id, sender),
                            )

                        # 检查是否可以开始处理
                        if session.is_complete():
                            self._start_processing_immediately(session)
                        return True  # 静默处理，不回复
                    else:
                        # 已经有了描述，忽略
                        return True
                else:
                    # 不在会话中，忽略
                    return False

        except Exception as e:
            logger.error(f"🖼️ 处理引用图片消息失败: {e}")
            return False

    def _download_quote_image_async(self, chat_name: str, message_id: str, sender: str):
        """异步下载引用图片"""
        try:
            logger.info(f"🖼️ 开始后台下载引用图片 - 消息ID: {message_id}")

            # 获取WeChat管理器
            wx_manager = None
            with self._session_lock:
                session = self._get_session(chat_name, sender)
                if session:
                    wx_manager = session.wx_manager

            if wx_manager:
                # 下载引用图片
                image_path = wx_manager.download_quote_image(chat_name, message_id=message_id)
                if image_path and Path(image_path).exists():
                    # 读取并编码图片
                    try:
                        with open(image_path, "rb") as f:
                            image_data = f.read()

                        # 检查图片大小
                        image_size_mb = len(image_data) / (1024 * 1024)
                        logger.info(f"🖼️ 引用图片大小: {image_size_mb:.2f} MB")

                        # 如果图片太大，可能需要压缩或拒绝处理
                        if image_size_mb > 10:  # 10MB限制
                            logger.warning(f"🖼️ 引用图片过大 ({image_size_mb:.2f} MB)，可能导致API调用失败")
                            return

                        # 检测图片格式
                        image_format = self._detect_image_format(image_data)
                        logger.info(f"🖼️ 检测到引用图片格式: {image_format}")

                        image_base64 = base64.b64encode(image_data).decode("utf-8")

                        # 添加图片到会话
                        with self._session_lock:
                            session = self._get_session(chat_name, sender)
                            if session and session.can_add_image():
                                session.images.append(image_base64)
                                logger.info(f"🖼️ 引用图片已添加到session - 当前图片数: {len(session.images)}")

                                # 检查是否可以开始处理
                                if session.is_complete():
                                    self._start_processing_immediately(session)

                    except Exception as e:
                        logger.error(f"🖼️ 读取下载的引用图片失败: {e}")
                else:
                    logger.warning(f"🖼️ 下载引用图片失败或文件不存在 - 消息ID: {message_id}")
            else:
                logger.warning(f"🖼️ WeChat管理器不可用，无法下载引用图片 - 消息ID: {message_id}")

        except Exception as e:
            logger.error(f"🖼️ 异步下载引用图片失败 - 消息ID: {message_id}, 错误: {e}")

    def _download_image_async(self, chat_name: str, message_id: str, sender: str):
        """异步下载图片"""
        try:
            logger.info(f"🖼️ 开始后台下载图片 - 消息ID: {message_id}")

            # 更新下载状态
            with self._session_lock:
                session = self._get_session(chat_name, sender)
                if session and message_id in session.image_downloads:
                    session.image_downloads[message_id].status = "downloading"

            # 调用下载API
            wx_manager = None
            with self._session_lock:
                session = self._get_session(chat_name, sender)
                if session:
                    wx_manager = session.wx_manager

            if wx_manager:
                image_path = wx_manager.download_image_message(chat_name, message_id)
                if image_path and Path(image_path).exists():
                    # 读取并编码图片
                    try:
                        with open(image_path, "rb") as f:
                            image_data = f.read()

                        # 检查图片大小
                        image_size_mb = len(image_data) / (1024 * 1024)
                        logger.info(f"🖼️ 图片大小: {image_size_mb:.2f} MB")

                        # 如果图片太大，可能需要压缩或拒绝处理
                        if image_size_mb > 10:  # 10MB限制
                            logger.warning(f"🖼️ 图片过大 ({image_size_mb:.2f} MB)，可能导致API调用失败")
                            with self._session_lock:
                                session = self._get_session(chat_name, sender)
                                if session and message_id in session.image_downloads:
                                    session.image_downloads[message_id].status = "failed"
                                    session.image_downloads[message_id].error = f"图片过大 ({image_size_mb:.2f} MB)"
                            return True

                        # 检测图片格式
                        image_format = self._detect_image_format(image_data)
                        logger.info(f"🖼️ 检测到图片格式: {image_format}")

                        image_base64 = base64.b64encode(image_data).decode("utf-8")

                        # 更新下载状态和添加图片
                        with self._session_lock:
                            session = self._get_session(chat_name, sender)
                            if session and message_id in session.image_downloads:
                                session.image_downloads[message_id].status = "completed"
                                session.image_downloads[message_id].file_path = image_path

                                # 再次检查是否可以添加图片（防止并发问题）
                                if session.can_add_image():
                                    session.images.append(image_base64)
                                    logger.info(f"🖼️ 图片已添加到session - 当前图片数: {len(session.images)}")
                                else:
                                    logger.info(f"🖼️ 图片数量已达上限，忽略此图片 - 消息ID: {message_id}")
                                    # 标记为失败但不添加到images列表
                                    session.image_downloads[message_id].status = "ignored"
                                    return

                        # 检查是否可以开始处理
                        with self._session_lock:
                            session = self._get_session(chat_name, sender)
                            if session and session.is_complete():
                                logger.info(f"🖼️ 图片下载完成 - 消息ID: {message_id}, 大小: {len(image_data)} bytes, Base64长度: {len(image_base64)}")
                                self._start_processing_immediately(session)
                            else:
                                logger.info(f"🖼️ 图片下载完成 - 消息ID: {message_id}, 大小: {len(image_data)} bytes, Base64长度: {len(image_base64)}")

                    except Exception as e:
                        logger.error(f"🖼️ 读取下载的图片失败: {e}")
                        with self._session_lock:
                            session = self._get_session(chat_name, sender)
                            if session and message_id in session.image_downloads:
                                session.image_downloads[message_id].status = "failed"
                                session.image_downloads[message_id].error = str(e)
                else:
                    logger.warning(f"🖼️ 下载图片失败或文件不存在 - 消息ID: {message_id}")
                    with self._session_lock:
                        session = self._get_session(chat_name, sender)
                        if session and message_id in session.image_downloads:
                            session.image_downloads[message_id].status = "failed"
                            session.image_downloads[message_id].error = "下载失败或文件不存在"
            else:
                logger.warning(f"🖼️ WeChat管理器不可用，无法下载图片 - 消息ID: {message_id}")
                with self._session_lock:
                    session = self._get_session(chat_name, sender)
                    if session and message_id in session.image_downloads:
                        session.image_downloads[message_id].status = "failed"
                        session.image_downloads[message_id].error = "WeChat管理器不可用"

        except Exception as e:
            logger.error(f"🖼️ 异步下载图片失败 - 消息ID: {message_id}, 错误: {e}")
            with self._session_lock:
                session = self._get_session(chat_name, sender)
                if session and message_id in session.image_downloads:
                    session.image_downloads[message_id].status = "failed"
                    session.image_downloads[message_id].error = str(e)

# 全局实例
image_editor_plugin = None


def handle_text_message(event: Event) -> bool:
    """处理文本消息事件"""
    global image_editor_plugin
    if image_editor_plugin:
        return image_editor_plugin.handle_text_message(event)
    return False


def handle_image_message(event: Event) -> bool:
    """处理图片消息事件"""
    global image_editor_plugin
    if image_editor_plugin:
        return image_editor_plugin.handle_image_message(event)
    return False


def handle_quote_image_message(event: Event) -> bool:
    """处理引用图片消息事件"""
    global image_editor_plugin
    if image_editor_plugin:
        return image_editor_plugin.handle_quote_image_message(event)
    return False


def register(event_bus, subscribe, context):
    """插件注册函数"""
    global image_editor_plugin

    logger.info("🖼️ 注册图片编辑插件...")

    # 初始化插件
    image_editor_plugin = ImageEditorPlugin(context)
    context.health.register(lambda: {
        "status": "healthy" if image_editor_plugin is not None else "unhealthy",
        "message": "图片编辑会话服务已就绪" if image_editor_plugin is not None else "图片编辑服务未初始化",
        "active_sessions": len(image_editor_plugin._sessions) if image_editor_plugin is not None else 0,
    })
    context.register_cleanup(unregister)

        # 订阅事件
    subscribe(
        event_type=EventType.TEXT_MESSAGE_RECEIVED,
        handler=handle_text_message
    )

    subscribe(
        event_type=EventType.IMAGE_MESSAGE_RECEIVED,
        handler=handle_image_message
    )

    subscribe(
        event_type=EventType.QUOTE_IMAGE_MESSAGE_RECEIVED,
        handler=handle_quote_image_message
    )

    logger.info("✅ 图片编辑插件注册成功")


def unregister():
    """取消注册插件"""
    global image_editor_plugin

    logger.info("🖼️ 取消注册图片编辑插件...")
    image_editor_plugin = None
    logger.info("✅ 图片编辑插件已取消注册")
