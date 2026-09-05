"""微信管理器：通过 HTTP 与使用 mabowx 的 wx_bot 桥接进程交互。"""

import logging
import os
import time
import threading
import uuid
from contextlib import contextmanager
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from urllib.parse import urlparse
import requests

from .event_bus import EventBus, get_event_bus, Event, EventType
from ..utils.health_state import ConsecutiveHealthGate, should_resync_connection

# --- wx_bot.py 的地址 ---
WX_BOT_PORT = os.getenv("WX_BOT_PORT", "5555").strip() or "5555"
WX_BOT_URL = os.getenv("WX_BOT_URL", "").strip() or f"http://127.0.0.1:{WX_BOT_PORT}"
LISTENER_API_TIMEOUT_SEC = 75
CONNECTION_MONITOR_INTERVAL_SEC = 5
CONNECTION_FAILURE_THRESHOLD = 3
CONNECTION_RECOVERY_THRESHOLD = 2
TEXT_SEND_API_TIMEOUT_SEC = 30

@dataclass
class MessageInfo:
    """消息信息"""
    content: str
    sender: str
    chat_name: str
    chat_type: str  # user, group
    message_type: str  # text, image, link, quote, video, file, voice
    timestamp: float
    raw_message: Any = None
    sender_remark: Optional[str] = None


class WeChatManager:
    """
    微信管理器 - API客户端模式
    - 通过HTTP API与独立的wx_bot.py进程通信
    """
    _outbound_send_lock = threading.RLock()

    def __init__(self, event_bus: Optional[EventBus] = None):
        self.event_bus = event_bus or get_event_bus()
        self.logger = logging.getLogger(__name__)
        self._http = requests.Session()
        bridge_host = (urlparse(WX_BOT_URL).hostname or "").lower()
        if bridge_host in {"127.0.0.1", "localhost", "::1"}:
            # 本机桥接流量不能被系统 HTTP(S)_PROXY 送到外部代理。
            self._http.trust_env = False
        self._running = False
        self._listened_chats = {}
        self._stats = {
            'messages_received': 0,
            'messages_processed': 0,
            'events_published': 0,
            'last_message_time': None,
            'listened_chats_count': 0
        }
        # 连接监控
        self._connection_monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop_flag = False
        self._last_health: Dict[str, Any] = {
            'wechat_connected': False,
            'wechat_online': False,
            'timestamp': 0
        }
        self._last_reconnected_ts: float = 0.0
        self._last_connection_id: Optional[str] = None
        self._last_health_error_signature: Optional[str] = None
        self._last_health_error_logged_at: float = 0.0

    def _post_outbound(self, endpoint: str, payload: Dict[str, Any], timeout: int) -> requests.Response:
        """
        应用层串行多段回复，保证同一业务回复不被其他会话插入。
        单次 UI 事务、目标校验和重试均由 mabowx 内部负责。
        """
        with self._outbound_send_lock:
            self.logger.debug(
                "Acquired WeChat outbound send lock: endpoint=%s chat=%s",
                endpoint,
                payload.get("who") or payload.get("chat_name")
            )
            return self._http.post(
                f"{WX_BOT_URL}{endpoint}",
                json=payload,
                timeout=timeout
            )

    @contextmanager
    def outbound_send_session(self):
        """把多段回复视为一次连续出站发送，避免中途被其他群切走窗口。"""
        with self._outbound_send_lock:
            yield

    def start(self) -> bool:
        """启动微信管理器并检查与wx_bot的连接"""
        self.logger.info("Starting WeChat manager (API client mode)...")
        self._last_health = self._get_health()
        self._last_connection_id = self._last_health.get("connection_id") or None
        if self._last_health.get('wechat_connected'):
            self.logger.info("✅ Successfully connected to wx_bot service.")
            self._running = True
            # 启动连接状态监控线程（无论是否连接都开启，便于后续自动恢复）
            self._start_connection_monitor()
            return True
        else:
            self.logger.error("❌ Failed to connect to wx_bot service. Please ensure wx_bot.py is running.")
            self._running = False
            # 即使当前未连接，也启动监控线程以便后续自动恢复
            self._start_connection_monitor()
            return False

    def stop(self) -> None:
        """停止微信管理器"""
        self.logger.info("Stopping WeChat manager (API client mode)...")
        self._running = False
        self._monitor_stop_flag = True
        if self._connection_monitor_thread and self._connection_monitor_thread.is_alive():
            self._connection_monitor_thread.join(timeout=5)
        self.logger.info("WeChat manager stopped.")

    def _check_wechat_connection(self) -> bool:
        """通过API检查与wx_bot的连接状态"""
        health = self._get_health()
        return bool(health.get("bridge_reachable") and health.get("wechat_connected"))

    def _health_failure_sample(self, reason: str, error: object, latency_ms: float) -> Dict[str, Any]:
        now = time.time()
        signature = f"{reason}:{type(error).__name__}:{error}"
        if signature != self._last_health_error_signature or now - self._last_health_error_logged_at >= 60:
            self._last_health_error_signature = signature
            self._last_health_error_logged_at = now
            self.logger.warning(
                "wx_bot health request failed: reason=%s latency_ms=%.1f error=%s",
                reason,
                latency_ms,
                error,
            )
        return {
            'bridge_reachable': False,
            'wechat_connected': False,
            'wechat_online': False,
            'health_status': 'unavailable',
            'failure_reason': reason,
            'failure_error': str(error),
            'response_latency_ms': round(latency_ms, 1),
            'online_probe': {},
            'connection_id': None,
            'listeners': {},
            'timestamp': now,
        }

    def _get_health(self) -> Dict[str, Any]:
        """获取 wx_bot 健康状态详情（包含 online/connected）"""
        started = time.perf_counter()
        try:
            response = self._http.get(f"{WX_BOT_URL}/health", timeout=3)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("wx_bot health response is not an object")
            latency_ms = (time.perf_counter() - started) * 1000
            listeners = data.get('listeners') or {}
            self._sync_listened_chats_from_listener_status(listeners)
            self._last_health_error_signature = None
            return {
                'bridge_reachable': True,
                'wechat_connected': bool(data.get('wechat_connected')),
                'wechat_online': bool(data.get('wechat_online')),
                'health_status': data.get('health_status', 'ok'),
                'failure_reason': None,
                'failure_error': None,
                'response_latency_ms': round(latency_ms, 1),
                'online_probe': data.get('online_probe') or {},
                'service_instance_id': data.get('service_instance_id'),
                'connection_generation': data.get('connection_generation'),
                'connection_id': data.get('connection_id'),
                'listeners': listeners,
                'timestamp': data.get('timestamp', time.time())
            }
        except requests.Timeout as e:
            latency_ms = (time.perf_counter() - started) * 1000
            return self._health_failure_sample("bridge_timeout", e, latency_ms)
        except ValueError as e:
            latency_ms = (time.perf_counter() - started) * 1000
            return self._health_failure_sample("invalid_response", e, latency_ms)
        except requests.RequestException as e:
            latency_ms = (time.perf_counter() - started) * 1000
            return self._health_failure_sample("bridge_request_error", e, latency_ms)
        except Exception as e:
            latency_ms = (time.perf_counter() - started) * 1000
            return self._health_failure_sample("health_processing_error", e, latency_ms)

    def _sync_listened_chats_from_listener_status(self, listeners: Dict[str, Any]) -> None:
        """用 wx_bot 的 desired/actual 快照刷新本进程里的活跃监听视图。"""
        if not isinstance(listeners, dict):
            return
        if listeners.get('probe_skipped'):
            return

        desired = listeners.get('desired') or []
        actual = listeners.get('actual') or []
        if not isinstance(desired, list) or not isinstance(actual, list):
            return

        desired_set = set(str(name) for name in desired if name)
        actual_set = set(str(name) for name in actual if name)
        active_set = desired_set & actual_set

        now_ts = time.time()
        for chat_name in active_set:
            self._listened_chats.setdefault(chat_name, {
                'added_time': now_ts,
                'message_count': 0
            })

        for chat_name in list(self._listened_chats.keys()):
            if chat_name not in active_set:
                self._listened_chats.pop(chat_name, None)

        self._stats['listened_chats_count'] = len(self._listened_chats)

    def _start_connection_monitor(self) -> None:
        """启动后台连接监控；确认连续失败后才发布一次重连事件。"""
        if self._connection_monitor_thread and self._connection_monitor_thread.is_alive():
            return

        self._monitor_stop_flag = False

        def _loop():
            self.logger.info("Starting WeChat connection monitor thread...")
            self._last_health = self._get_health()
            health_gate = ConsecutiveHealthGate(
                self._last_health,
                failure_threshold=CONNECTION_FAILURE_THRESHOLD,
                recovery_threshold=CONNECTION_RECOVERY_THRESHOLD,
            )
            while not self._monitor_stop_flag:
                try:
                    current = self._get_health()
                    transition = health_gate.observe(current)
                    if not transition.accepted:
                        if current.get("wechat_connected") and current.get("wechat_online"):
                            self.logger.debug(
                                "Waiting for stable wx_bot recovery %s/%s",
                                transition.consecutive_successes,
                                CONNECTION_RECOVERY_THRESHOLD,
                            )
                        else:
                            self.logger.debug(
                                "Ignoring transient wx_bot health failure %s/%s reason=%s",
                                transition.consecutive_failures,
                                CONNECTION_FAILURE_THRESHOLD,
                                current.get("failure_reason")
                                or (current.get("online_probe") or {}).get("state"),
                            )
                        time.sleep(CONNECTION_MONITOR_INTERVAL_SEC)
                        continue

                    self._last_health = transition.confirmed
                    self._running = transition.healthy
                    if not transition.healthy:
                        if transition.became_unhealthy:
                            self.logger.warning(
                                "wx_bot health confirmed unavailable after %s failures: "
                                "reason=%s latency_ms=%s probe_state=%s",
                                transition.consecutive_failures,
                                transition.confirmed.get("failure_reason") or "wechat_offline",
                                transition.confirmed.get("response_latency_ms"),
                                (transition.confirmed.get("online_probe") or {}).get("state"),
                            )
                        time.sleep(CONNECTION_MONITOR_INTERVAL_SEC)
                        continue

                    connection_id = transition.confirmed.get("connection_id") or None
                    connection_changed = bool(
                        connection_id and connection_id != self._last_connection_id
                    )
                    if should_resync_connection(
                        self._last_connection_id,
                        connection_id,
                        recovered=transition.reconnected,
                    ):
                        now_ts = time.time()
                        should_publish = connection_changed or now_ts - self._last_reconnected_ts > 60
                        if should_publish:
                            self._last_reconnected_ts = now_ts
                            if connection_id:
                                self._last_connection_id = connection_id
                            self.logger.info(
                                "🔄 检测到新的微信连接实例，发布一次监听器同步事件: connection_id=%s",
                                connection_id or "legacy",
                            )
                            try:
                                if self.event_bus:
                                    self.event_bus.publish(
                                        Event(
                                            type=EventType.WECHAT_RECONNECTED,
                                            source="wechat_manager",
                                            data={
                                                "timestamp": now_ts,
                                                "connection_id": connection_id,
                                            }
                                        )
                                    )
                            except Exception as e:
                                self.logger.error(f"发布微信恢复事件失败: {e}")

                    time.sleep(CONNECTION_MONITOR_INTERVAL_SEC)
                except Exception:
                    # 避免监控线程崩溃
                    time.sleep(CONNECTION_MONITOR_INTERVAL_SEC)

        self._connection_monitor_thread = threading.Thread(target=_loop, name="wechat_connection_monitor", daemon=True)
        self._connection_monitor_thread.start()

    def add_listen_chat(self, chat_name: str, exact: bool = False) -> bool:
        """添加监听聊天 - 通过API"""
        try:
            response = self._http.post(
                f"{WX_BOT_URL}/api/add_listener",
                json={"who": chat_name},
                timeout=LISTENER_API_TIMEOUT_SEC
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                self._listened_chats[chat_name] = {
                    'added_time': time.time(),
                    'message_count': 0
                }
                self._stats['listened_chats_count'] = len(self._listened_chats)
                self.logger.debug(f"✅ Successfully added listen chat via API: {chat_name}")
                return True
            else:
                self.logger.error(f"❌ Failed to add listen chat via API: {data.get('message')}")
                return False
        except requests.RequestException as e:
            self.logger.error(f"❌ Failed to call add_listener API: {e}")
            return False

    def remove_listen_chat(self, chat_name: str) -> bool:
        """移除监听聊天 - 通过API"""
        try:
            response = self._http.post(
                f"{WX_BOT_URL}/api/remove_listener",
                json={"who": chat_name},
                timeout=5
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                self.logger.debug(f"✅ Successfully removed listen chat via API: {chat_name}")
                return True
            else:
                self.logger.error(f"❌ Failed to remove listen chat via API: {data.get('message')}")
                return False
        except requests.RequestException as e:
            self.logger.error(f"❌ Failed to call remove_listener API: {e}")
            return False
        finally:
            # 本地视图立即服从用户的停止意图；桥接残留会在重连同步时再次清理。
            self._listened_chats.pop(chat_name, None)
            self._stats['listened_chats_count'] = len(self._listened_chats)

    def send_message(self, chat_name: str, message: str, at_users: List[str] = None) -> bool:
        """发送消息 - 通过API"""
        normalized_at_users = [
            str(user).strip().lstrip("@").strip()
            for user in (at_users or [])
            if str(user).strip().lstrip("@").strip()
        ]

        request_id = uuid.uuid4().hex
        try:
            response = self._post_outbound(
                "/api/send_message",
                {
                    "who": chat_name,
                    "message": message,
                    "at_users": normalized_at_users,
                    "request_id": request_id,
                },
                timeout=TEXT_SEND_API_TIMEOUT_SEC,
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                self.logger.debug(
                    "Sent message to %s via API: request_id=%s route=%s attempts=%s message=%s...",
                    chat_name,
                    request_id,
                    data.get("route"),
                    data.get("attempt_count"),
                    message[:50],
                )
                # 增加处理消息计数：只有成功发送回复时才计数
                self._stats['messages_processed'] += 1
                return True
            else:
                self.logger.error(f"Failed to send message via API: {data.get('message')}")
                return False
        except requests.HTTPError as e:
            response_body = ""
            if e.response is not None:
                response_body = (e.response.text or "").strip()
                if len(response_body) > 1000:
                    response_body = response_body[:1000] + "...<truncated>"
            self.logger.error(
                "Failed to call send_message API: request_id=%s error=%s | response_body=%s",
                request_id,
                e,
                response_body or "<empty>"
            )
            return False
        except requests.ReadTimeout as e:
            # The Flask worker may still be running after the HTTP client stops
            # waiting. Do not retry here; request_id lets the server deduplicate
            # a deliberate status/retry workflow in the future.
            self.logger.error(
                "send_message API read timed out; delivery status unknown and client retry suppressed: "
                "request_id=%s chat=%s timeout=%ss error=%s",
                request_id,
                chat_name,
                TEXT_SEND_API_TIMEOUT_SEC,
                e,
            )
            return False
        except requests.RequestException as e:
            self.logger.error(
                "Failed to call send_message API: request_id=%s error=%s",
                request_id,
                e,
            )
            return False

    def quote_message(self, chat_name: str, message_id: str, message: str) -> bool:
        """引用指定的已缓存微信消息并回复。"""
        if not message_id:
            return False

        try:
            response = self._post_outbound(
                "/api/quote_message",
                {
                    "chat_name": chat_name,
                    "message_id": message_id,
                    "message": message,
                },
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                self.logger.debug(
                    "Quoted message in %s via API: message_id=%s",
                    chat_name,
                    message_id,
                )
                self._stats['messages_processed'] += 1
                return True

            self.logger.warning(
                "Failed to quote message via API; caller may fall back to plain send: %s",
                data.get("message"),
            )
            return False
        except requests.RequestException as e:
            response_body = ""
            if getattr(e, "response", None) is not None:
                response_body = (e.response.text or "").strip()
                if len(response_body) > 1000:
                    response_body = response_body[:1000] + "...<truncated>"
            self.logger.warning(
                "Failed to call quote_message API; caller may fall back to plain send: %s | response_body=%s",
                e,
                response_body or "<empty>",
            )
            return False

    def tickle(self, chat_name: str, sender: str) -> bool:
        """拍一拍指定聊天中发起互动的成员。"""
        chat_name = str(chat_name or "").strip()
        sender = str(sender or "").strip()
        if not chat_name or not sender:
            return False

        try:
            response = self._post_outbound(
                "/api/tickle_message",
                {"chat_name": chat_name, "sender": sender},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("status") == "success":
                self.logger.info("👋 Successfully tickled %s in %s", sender, chat_name)
                self._stats['messages_processed'] += 1
                return True

            self.logger.warning(
                "Failed to tickle %s in %s: %s",
                sender,
                chat_name,
                data.get("message"),
            )
            return False
        except requests.RequestException as e:
            response_body = ""
            if getattr(e, "response", None) is not None:
                response_body = (e.response.text or "").strip()
                if len(response_body) > 1000:
                    response_body = response_body[:1000] + "...<truncated>"
            self.logger.warning(
                "Failed to call tickle_message API: chat=%s sender=%s error=%s | response_body=%s",
                chat_name,
                sender,
                e,
                response_body or "<empty>",
            )
            return False

    # === 历史桥接脚本使用的方法 ===

    def send_files(self, chat_name: str, file_paths: List[str]) -> bool:
        """发送文件 - 通过API"""
        try:
            # 支持单个文件路径的兼容性
            if isinstance(file_paths, str):
                file_paths = [file_paths]

            response = self._post_outbound(
                "/api/send_files",
                {"who": chat_name, "file_paths": file_paths},
                timeout=600  # 周报/PDF 文件发送可能触发微信 UI 操作，保留长超时
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                self.logger.info(f"✅ Successfully sent {len(file_paths)} files to {chat_name}")
                return True
            else:
                self.logger.error(f"❌ Failed to send files via API: {data.get('message')}")
                return False
        except requests.RequestException as e:
            self.logger.error(f"❌ Failed to call send_files API: {e}")
            return False

    def send_url_card(self, chat_name: str, url: str, timeout: int = 30) -> Dict[str, Any]:
        """发送URL卡片 - 通过API"""
        try:
            response = self._post_outbound(
                "/api/send_url_card",
                {"who": chat_name, "url": url},
                timeout=timeout
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                self.logger.info(f"✅ Successfully sent URL card to {chat_name}")
                return {"success": True, "message": "URL card sent successfully"}
            else:
                error_msg = data.get('message', 'Unknown error')
                self.logger.error(f"❌ Failed to send URL card via API: {error_msg}")
                return {"success": False, "message": error_msg}
        except requests.RequestException as e:
            error_msg = f"API request failed: {e}"
            self.logger.error(f"❌ Failed to call send_url_card API: {e}")
            return {"success": False, "message": error_msg}



    def resolve_link_url(self, chat_name: str, message_id: str, timeout: int = 60) -> Optional[str]:
        """按需解析微信链接卡片真实URL。"""
        if not message_id:
            return None

        try:
            response = self._http.post(
                f"{WX_BOT_URL}/api/resolve_link_url",
                json={"chat_name": chat_name, "message_id": message_id, "timeout": timeout},
                timeout=max(timeout + 15, 20)
            )
            response.raise_for_status()
            data = response.json()
            if data.get("status") == "success":
                url = data.get("url")
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    self.logger.info(f"✅ 成功解析链接卡片URL: {url}")
                    return url

            self.logger.error(f"❌ 链接卡片URL解析失败: {data.get('message')}")
            return None
        except requests.RequestException as e:
            self.logger.error(f"❌ Failed to call resolve_link_url API: {e}")
            return None

    def _download_quote_media(
        self,
        chat_name: str,
        message_id: Optional[str],
        *,
        media_kind: str,
    ) -> Optional[str]:
        """请求 wx_bot 下载与引用消息精确绑定的媒体。"""
        is_video = media_kind == "video"
        media_label = "视频" if is_video else "图片"
        endpoint = (
            "/api/download_quote_video_on_demand"
            if is_video
            else "/api/download_quote_image_on_demand"
        )
        # wx_bot 会合并相同 message_id 的并发请求。首次超时留足正常 UI
        # 下载时间，避免 5~10 秒的短超时制造仍在后台执行的重复请求。
        retry_configs = (
            [
                {"attempt": 1, "timeout": 45, "description": "首次尝试"},
                {"attempt": 2, "timeout": 135, "description": "第二次重试"},
            ]
            if is_video
            else [
                {"attempt": 1, "timeout": 30, "description": "首次尝试"},
                {"attempt": 2, "timeout": 90, "description": "第二次重试"},
            ]
        )

        last_exception = None

        for config in retry_configs:
            attempt = config["attempt"]
            timeout = config["timeout"]
            description = config["description"]

            try:
                self.logger.info(
                    "%s %s - 下载引用%s (消息ID: %s, 超时: %s秒)",
                    "🎬" if is_video else "🖼️",
                    description,
                    media_label,
                    message_id,
                    timeout,
                )

                response = self._http.post(
                    f"{WX_BOT_URL}{endpoint}",
                    json={
                        "chat_name": chat_name,
                        "message_id": message_id,
                        "timeout": 120 if is_video else 30,
                    },
                    timeout=timeout
                )
                response.raise_for_status()
                data = response.json()

                if data.get("status") == "success":
                    file_path = data.get("file_path")
                    if file_path:
                        self.logger.info(f"✅ 成功下载引用{media_label} (第{attempt}次尝试): {file_path}")
                        return file_path
                    else:
                        self.logger.error(f"❌ 第{attempt}次尝试失败: API未返回文件路径")
                        last_exception = Exception("API未返回文件路径")
                        continue
                else:
                    error_msg = data.get('message', '未知错误')
                    self.logger.error(f"❌ 第{attempt}次尝试失败: {error_msg}")
                    last_exception = Exception(f"API返回错误: {error_msg}")

                    # 如果是服务器错误，等待后重试
                    if response.status_code >= 500:
                        if attempt < len(retry_configs):
                            time.sleep(1)
                    else:
                        # 客户端错误（如400、404）直接返回失败
                        break

            except requests.exceptions.Timeout as e:
                self.logger.warning(f"⏰ 第{attempt}次尝试超时 ({timeout}秒): {e}")
                last_exception = e
                # 超时后立即重试，因为可能是网络波动

            except requests.exceptions.ConnectionError as e:
                self.logger.warning(f"🔗 第{attempt}次尝试连接失败: {e}")
                last_exception = e
                if attempt < len(retry_configs):
                    time.sleep(1)

            except requests.RequestException as e:
                self.logger.error(f"❌ 第{attempt}次尝试网络请求失败: {e}")
                last_exception = e
                if attempt < len(retry_configs):
                    time.sleep(1)

        # 所有重试都失败了
        self.logger.error(f"❌ 引用{media_label}下载失败 - 消息ID: {message_id}，已尝试{len(retry_configs)}次")
        if last_exception:
            self.logger.error(f"最后一次错误: {last_exception}")
        return None

    def download_quote_image(self, chat_name: str, message_id: str = None) -> Optional[str]:
        """按需下载引用图片（带重试机制）。"""
        return self._download_quote_media(
            chat_name,
            message_id,
            media_kind="image",
        )

    def download_quote_video(self, chat_name: str, message_id: str = None) -> Optional[str]:
        """按需打开微信预览并下载引用视频。"""
        return self._download_quote_media(
            chat_name,
            message_id,
            media_kind="video",
        )

    def download_image_message(self, chat_name: str, message_id: str) -> Optional[str]:
        """下载指定消息ID的图片消息（带重试机制）"""
        # 服务端会合并相同 message_id 的并发请求；这里只保留一次宽松重试。
        retry_configs = [
            {"attempt": 1, "timeout": 30, "description": "首次尝试"},
            {"attempt": 2, "timeout": 90, "description": "第二次重试"},
        ]

        last_exception = None

        for config in retry_configs:
            attempt = config["attempt"]
            timeout = config["timeout"]
            description = config["description"]

            try:
                self.logger.info(f"🖼️ {description} - 下载图片 (消息ID: {message_id}, 超时: {timeout}秒)")

                response = self._http.post(
                    f"{WX_BOT_URL}/api/download_image_message",
                    json={"chat_name": chat_name, "message_id": message_id},
                    timeout=timeout
                )
                try:
                    data = response.json()
                except ValueError:
                    data = {}

                if not response.ok:
                    error_code = str(data.get("error_code") or "http_error")
                    error_msg = str(data.get("message") or response.reason or "未知错误")
                    last_exception = Exception(f"{error_code}: {error_msg}")

                    if error_code == "image_identity_mismatch":
                        self.logger.warning(
                            "🖼️ 图片身份校验拒绝，不再重复下载: "
                            f"message_id={message_id} reason={error_msg}"
                        )
                        return None

                    retryable = response.status_code in {408, 429, 500, 502, 503, 504}
                    if retryable and attempt < len(retry_configs):
                        self.logger.warning(
                            "🔄 图片下载暂时失败，准备重试: "
                            f"message_id={message_id} attempt={attempt} "
                            f"status={response.status_code} code={error_code}"
                        )
                        time.sleep(1)
                        continue

                    self.logger.error(
                        "❌ 图片下载接口拒绝: "
                        f"message_id={message_id} status={response.status_code} "
                        f"code={error_code} reason={error_msg}"
                    )
                    return None

                if data.get("status") == "success":
                    file_path = data.get("file_path")
                    if file_path:
                        self.logger.info(f"✅ 成功下载图片消息 (第{attempt}次尝试): {file_path}")
                        return file_path
                    else:
                        self.logger.error(f"❌ 第{attempt}次尝试失败: API未返回文件路径")
                        last_exception = Exception("API未返回文件路径")
                        continue
                else:
                    error_msg = data.get('message', '未知错误')
                    self.logger.error(f"❌ 第{attempt}次尝试失败: {error_msg}")
                    last_exception = Exception(f"API返回错误: {error_msg}")
                    break

            except requests.exceptions.Timeout as e:
                self.logger.warning(f"⏰ 第{attempt}次尝试超时 ({timeout}秒): {e}")
                last_exception = e
                if attempt < len(retry_configs):
                    time.sleep(0.5)  # 超时后短暂等待再重试

            except requests.exceptions.ConnectionError as e:
                self.logger.warning(f"🔗 第{attempt}次尝试连接失败: {e}")
                last_exception = e
                if attempt < len(retry_configs):
                    time.sleep(1)  # 连接失败后等待1秒再重试

            except requests.RequestException as e:
                self.logger.error(f"❌ 第{attempt}次尝试网络请求失败: {e}")
                last_exception = e
                if attempt < len(retry_configs):
                    time.sleep(1)  # 网络错误后等待1秒再重试

        # 所有重试都失败了
        self.logger.error(
            "❌ 图片下载失败: "
            f"message_id={message_id} attempts={len(retry_configs)} "
            f"reason={last_exception or '未知错误'}"
        )
        return None





    def get_chat_info(self, chat_name: str) -> Dict[str, Any]:
        """获取聊天信息 - 通过API（模拟chat.ChatInfo()）"""
        try:
            response = self._http.post(
                f"{WX_BOT_URL}/api/get_chat_info",
                json={"who": chat_name},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                chat_info = data.get("chat_info", {})
                self.logger.debug(f"Successfully got chat info for {chat_name}")
                return chat_info
            else:
                self.logger.error(f"Failed to get chat info via API: {data.get('message')}")
                return {}
        except requests.RequestException as e:
            self.logger.error(f"Failed to call get_chat_info API: {e}")
            return {}

    def keep_running(self) -> None:
        """保持运行 - 通过API（模拟wx.KeepRunning()）"""
        try:
            response = self._http.post(
                f"{WX_BOT_URL}/api/keep_running",
                json={},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                self.logger.info("✅ Successfully started KeepRunning mode")
            else:
                self.logger.error(f"Failed to start KeepRunning via API: {data.get('message')}")
        except requests.RequestException as e:
            self.logger.error(f"Failed to call keep_running API: {e}")

    def restart_wechat(self) -> bool:
        """重启微信连接 - 通过API"""
        try:
            self.logger.info("🔄 Requesting WeChat restart...")
            response = self._http.post(
                f"{WX_BOT_URL}/api/restart_wechat",
                json={},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                self.logger.info("✅ WeChat restart request submitted successfully")
                return True
            else:
                self.logger.error(f"Failed to restart WeChat via API: {data.get('message')}")
                return False
        except requests.RequestException as e:
            self.logger.error(f"Failed to call restart_wechat API: {e}")
            return False

    # === 原有方法保持不变 ===

    def get_all_friends(self, keywords: str = None) -> List[Dict[str, Any]]:
        """获取所有好友 - 通过API"""
        try:
            params = {"keywords": keywords} if keywords else {}
            response = self._http.get(f"{WX_BOT_URL}/api/get_friends", params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                return data.get("friends", [])
            else:
                self.logger.error(f"Failed to get friends list via API: {data.get('message')}")
                return []
        except requests.RequestException as e:
            self.logger.error(f"Failed to call get_friends API: {e}")
            return []

    def get_recent_groups(self) -> List[Dict[str, Any]]:
        """获取最近群聊列表 - 通过API"""
        try:
            response = self._http.get(f"{WX_BOT_URL}/api/get_groups", timeout=10)
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                return data.get("groups", [])
            else:
                self.logger.error(f"Failed to get groups list via API: {data.get('message')}")
                return []
        except requests.RequestException as e:
            self.logger.error(f"Failed to call get_groups API: {e}")
            return []

    def get_current_chat_info(self) -> Dict[str, Any]:
        """获取当前聊天信息 - 通过API"""
        try:
            response = self._http.get(f"{WX_BOT_URL}/api/get_current_chat", timeout=10)
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                return data.get("chat_info", {})
            else:
                self.logger.error(f"Failed to get current chat info via API: {data.get('message')}")
                return {}
        except requests.RequestException as e:
            self.logger.error(f"Failed to call get_current_chat API: {e}")
            return {}

    def get_my_info(self) -> Dict[str, Any]:
        """获取当前用户信息 - 通过API"""
        try:
            response = self._http.get(f"{WX_BOT_URL}/api/get_my_info", timeout=10)
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                return data.get("info", {})
            else:
                self.logger.error(f"Failed to get my info via API: {data.get('message')}")
                return {}
        except requests.RequestException as e:
            self.logger.error(f"Failed to call get_my_info API: {e}")
            return {}

    def get_listened_chats(self) -> Dict[str, Dict[str, Any]]:
        """获取正在监听的聊天列表"""
        self.get_listener_status()
        return self._listened_chats.copy()

    def get_listener_status(self) -> Dict[str, Any]:
        """获取 wx_bot 侧期望监听、实际监听窗口和缺失项。"""
        try:
            response = self._http.get(f"{WX_BOT_URL}/api/listeners/status", timeout=5)
            response.raise_for_status()
            data = response.json()
            if data.get("status") == "success":
                self._sync_listened_chats_from_listener_status(data)
            return data
        except requests.RequestException as e:
            self.logger.warning(f"Failed to call listeners status API: {e}")
            return {
                "status": "error",
                "message": str(e),
                "desired": [],
                "actual": [],
                "missing": []
            }

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息；状态页使用监控线程缓存，不额外请求 wx_bot。"""
        cached_health = self.get_cached_health()
        listener_status = cached_health.get('listeners') or {}
        return {
            **self._stats,
            'connected': bool(cached_health.get('wechat_connected')),
            'running': self._running,
            'bridge_reachable': bool(cached_health.get('bridge_reachable')),
            'health_status': cached_health.get('health_status'),
            'connection_id': cached_health.get('connection_id'),
            'online_probe': cached_health.get('online_probe') or {},
            'listened_chats': list(self._listened_chats.keys()),
            'listener_status': listener_status
        }

    def is_connected(self) -> bool:
        """检查是否连接"""
        return self._check_wechat_connection()

    def get_cached_health(self) -> Dict[str, Any]:
        """返回监控线程最近确认的健康状态，不发起新的桥接请求。"""
        return dict(self._last_health)

    def is_connected_cached(self) -> bool:
        """供健康检查等只读路径使用，避免探针反过来阻塞服务。"""
        return bool(self._last_health.get('wechat_connected'))

    def is_online(self) -> bool:
        """检查微信是否在线 - 通过API（模拟wx.IsOnline()）"""
        try:
            response = self._http.get(
                f"{WX_BOT_URL}/api/is_online",
                timeout=5
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success":
                online = data.get("online", False)
                self.logger.debug(f"WeChat online status: {online}")
                return online
            else:
                self.logger.warning(f"Failed to check online status via API: {data.get('message')}")
                return False
        except requests.RequestException as e:
            self.logger.warning(f"Failed to call is_online API: {e}")
            return False

    # === 兼容性方法 - 保持现有桥接接口 ===

    def SendFiles(self, filepath: str, who: str) -> bool:
        """兼容性方法 - 发送文件（保持原有接口风格）"""
        return self.send_files(who, [filepath])

    def SendUrlCard(self, url: str, friends: str, timeout: int = 30) -> Dict[str, Any]:
        """兼容性方法 - 发送URL卡片（保持原有接口风格）"""
        return self.send_url_card(friends, url, timeout)

    def AddListenChat(self, user_name: str, callback=None) -> bool:
        """兼容性方法 - 添加监听聊天（保持原有接口风格）"""
        # 注意：callback参数在API模式下不适用，消息通过event_bus处理
        if callback:
            self.logger.warning("callback parameter is ignored in API mode. Messages are handled via event_bus.")
        return self.add_listen_chat(user_name)

    def KeepRunning(self) -> None:
        """兼容性方法 - 保持运行（保持原有接口风格）"""
        self.keep_running()
