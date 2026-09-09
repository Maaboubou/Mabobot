#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
微信掉线监控服务
定期检测微信在线状态，掉线时发送邮件通知
"""

import logging
import threading
import time
from pathlib import Path
from typing import Optional

from wechat_auto_login import prepare_relogin_qr

from .config_service import get_setting
from .email_service import get_email_service

logger = logging.getLogger(__name__)


class WeChatMonitorService:
    """
    微信掉线监控服务
    - 定期检查微信是否在线
    - 监听窗口缺失时自动调用聊天恢复入口
    - 掉线时自动推进重新登录，并通过邮件发送二维码
    - 恢复时可选发送恢复通知
    """

    def __init__(self, wechat_manager=None):
        self.wechat_manager = wechat_manager
        self.email_service = get_email_service()
        self.logger = logging.getLogger(__name__)

        # 监控配置
        self.is_monitoring = False
        self.monitor_thread = None
        self.check_interval = 30  # 30秒检查一次

        # 防止重复发邮件；连续离线检查达到阈值后才发送掉线通知，避免瞬时超时误报
        self.offline_email_sent = False
        self.offline_detected = False
        self.offline_failure_count = 0
        self.offline_alert_threshold = 3
        self.last_online_status = None
        self.auto_relogin_enabled = True
        self.relogin_in_progress = False
        self.last_relogin_result = None
        self._relogin_lock = threading.Lock()
        self.last_listener_status = None
        self.last_blocked_listeners = []
        self.last_missing_listeners = []

        # 缺失监听使用与管理页“恢复监听”相同的 add_listen_chat 入口恢复。
        # 单个聊天失败后独立退避，避免持续故障时每 30 秒反复抢占微信 UI。
        self.listener_auto_recovery_enabled = True
        self.listener_recovery_base_cooldown = 60
        self.listener_recovery_max_cooldown = 300
        self.listener_recovery_state = {}
        self.last_listener_recovery = None
        self._listener_recovery_lock = threading.RLock()

        # 从配置获取机器人名称
        self.bot_name = get_setting("WECHAT_BOT_NAME", "微信助手")

        self.logger.info(f"🔧 微信掉线监控服务初始化完成 (机器人: {self.bot_name})")

    def set_wechat_manager(self, wechat_manager):
        """设置微信管理器实例"""
        self.wechat_manager = wechat_manager
        self.logger.info("微信管理器已设置到监控服务")

    def start_monitoring(self) -> bool:
        """启动监控"""
        if self.is_monitoring:
            self.logger.warning("⚠️ 监控服务已在运行")
            return False

        if not self.wechat_manager:
            self.logger.error("❌ 微信管理器未设置，无法启动监控")
            return False

        self.is_monitoring = True
        self.monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="wechat_monitor_service",
            daemon=True
        )
        self.monitor_thread.start()
        self.logger.info("🔍 微信掉线监控已启动")
        return True

    def stop_monitoring(self) -> None:
        """停止监控"""
        if not self.is_monitoring:
            return

        self.is_monitoring = False
        self.logger.info("🛑 微信掉线监控已停止")

        # 等待监控线程结束
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=5)

    def _monitor_loop(self) -> None:
        """监控循环"""
        self.logger.info("监控循环已启动")
        consecutive_failures = 0
        max_consecutive_failures = 5

        while self.is_monitoring:
            try:
                # 检查微信在线状态
                is_online = self._check_wechat_online()

                # 重置连续失败计数
                consecutive_failures = 0

                if is_online:
                    listener_status = self._check_listener_status()
                    if listener_status:
                        self._handle_listener_status(listener_status)

                    # 微信在线：清零连续离线计数
                    if self.offline_failure_count:
                        self.logger.info(
                            "✅ 微信在线检查恢复，清零连续失败计数 (%s/%s)",
                            self.offline_failure_count,
                            self.offline_alert_threshold,
                        )
                        self.offline_failure_count = 0

                    if self.offline_email_sent or self.offline_detected:
                        # 恢复提醒有独立订阅，即使关闭掉线邮件也可以接收。
                        self.logger.info("✅ 微信状态恢复正常，发送恢复通知...")
                        if self.email_service.send_recovery_notification(self.bot_name):
                            self.offline_email_sent = False
                            self.offline_detected = False
                        elif getattr(self.email_service, "last_status", "") == "skipped":
                            if not self.email_service.event_enabled("wechat_recovery"):
                                self.offline_email_sent = False
                                self.offline_detected = False
                        else:
                            self.logger.error("恢复通知邮件发送失败")

                    # 更新状态
                    if self.last_online_status != True:
                        self.logger.info("✅ 微信在线")
                        self.last_online_status = True
                else:
                    # 微信掉线/健康检查超时：连续失败达到阈值后才告警，避免单次 /health 超时误报
                    self.offline_failure_count += 1
                    if self.offline_failure_count < self.offline_alert_threshold:
                        self.logger.warning(
                            "⚠️ 微信在线检查失败 %s/%s，暂不发送掉线通知",
                            self.offline_failure_count,
                            self.offline_alert_threshold,
                        )
                    else:
                        self.offline_detected = True
                        if not self.offline_email_sent:
                            self.logger.warning(
                                "❌ 连续 %s 次检测到微信离线，尝试恢复登录并检查通知设置...",
                                self.offline_failure_count,
                            )
                            if self._handle_offline_alert():
                                self.offline_email_sent = True
                            elif getattr(self.email_service, "last_status", "") != "skipped":
                                self.logger.error("掉线通知邮件发送失败")

                        # 更新状态
                        if self.last_online_status != False:
                            self.logger.warning("⚠️ 微信离线")
                            self.last_online_status = False

                # 等待下次检查
                time.sleep(self.check_interval)

            except Exception as e:
                consecutive_failures += 1
                self.logger.error(f"监控循环异常 (连续失败 {consecutive_failures}/{max_consecutive_failures}): {e}")

                if consecutive_failures >= max_consecutive_failures:
                    self.logger.error(f"监控服务连续失败 {max_consecutive_failures} 次，延长检查间隔")
                    time.sleep(300)  # 5分钟
                    consecutive_failures = 0  # 重置计数
                else:
                    time.sleep(60)  # 异常时等待1分钟

        self.logger.info("监控循环已结束")

    def _check_wechat_online(self) -> bool:
        """读取连接监控缓存，避免重复探测微信桥接端口。"""
        try:
            if not self.wechat_manager:
                self.logger.warning("微信管理器未设置")
                return False

            health = self.wechat_manager.get_cached_health()
            is_online = bool(
                health.get("wechat_connected")
                and health.get("wechat_online")
            )
            self.logger.debug(f"微信在线状态: {is_online}")
            return is_online

        except Exception as e:
            self.logger.debug(f"检查微信在线状态异常: {e}")
            return False

    def _handle_offline_alert(self) -> bool:
        """尝试推进重新登录；能拿到二维码时随邮件发送附件。"""
        if not self.auto_relogin_enabled:
            return self.email_service.send_offline_notification(self.bot_name)

        if not self._relogin_lock.acquire(blocking=False):
            self.logger.info("微信重新登录自动化已在执行，本次跳过")
            return False

        qr_path: Path | None = None
        self.relogin_in_progress = True
        try:
            self.logger.warning("🔐 正在自动确认微信掉线提示并准备登录二维码...")
            result = prepare_relogin_qr(timeout=20, poll_interval=0.5)
            qr_path = result.qr_image_path
            self.last_relogin_result = {
                "status": result.status,
                "reason": result.reason,
                "timestamp": time.time(),
                "qr_captured": bool(qr_path),
            }

            if result.status == "online":
                self.logger.warning(
                    "微信客户端界面已在线，但桥接健康检查尚未恢复，发送普通掉线通知"
                )
                return self.email_service.send_offline_notification(self.bot_name)

            if qr_path:
                self.logger.warning("已生成微信登录二维码，正在发送邮件附件")
                return self.email_service.send_login_qr_notification(
                    qr_path,
                    bot_name=self.bot_name,
                    reason=result.reason,
                )

            self.logger.warning(
                "未能获取微信登录二维码，退回普通掉线通知: %s",
                result.reason,
            )
            return self.email_service.send_offline_notification(self.bot_name)
        except Exception as exc:
            self.logger.exception("微信重新登录自动化异常: %s", exc)
            self.last_relogin_result = {
                "status": "error",
                "reason": str(exc),
                "timestamp": time.time(),
                "qr_captured": False,
            }
            return self.email_service.send_offline_notification(self.bot_name)
        finally:
            if qr_path:
                try:
                    qr_path.unlink(missing_ok=True)
                except OSError as exc:
                    self.logger.warning("清理临时微信二维码失败: %s", exc)
            self.relogin_in_progress = False
            self._relogin_lock.release()

    def _check_listener_status(self) -> Optional[dict]:
        """检查 wx_bot 监听窗口健康状态。"""
        try:
            if not self.wechat_manager or not hasattr(self.wechat_manager, "get_listener_status"):
                return None

            status = self.wechat_manager.get_listener_status()
            if status.get("status") != "success":
                self.logger.debug("监听健康检查跳过: %s", status.get("message"))
                return status

            missing = status.get("missing") or []
            if missing:
                self.logger.warning(
                    "监听健康检查异常: missing=%s desired=%s actual=%s",
                    missing,
                    status.get("desired"),
                    status.get("actual"),
                )
            return status
        except Exception as e:
            self.logger.debug(f"监听健康检查异常: {e}")
            return None

    @staticmethod
    def _normalized_listener_names(names) -> list[str]:
        """保持桥接返回顺序并过滤空值、重复值。"""
        normalized = []
        seen = set()
        for value in names or []:
            name = str(value or "").strip()
            if not name or name in seen:
                continue
            normalized.append(name)
            seen.add(name)
        return normalized

    def _listener_recovery_delay(self, failure_count: int) -> float:
        exponent = max(0, int(failure_count) - 1)
        return float(min(
            self.listener_recovery_max_cooldown,
            self.listener_recovery_base_cooldown * (2 ** exponent),
        ))

    def _recover_missing_listeners(self, missing: list[str]) -> dict:
        """通过管理页同款入口恢复缺失监听，并对单个聊天独立退避。"""
        result = {"recovered": [], "failed": [], "deferred": []}
        if not self.listener_auto_recovery_enabled or not self.wechat_manager:
            return result

        for chat_name in self._normalized_listener_names(missing):
            now = time.time()
            with self._listener_recovery_lock:
                state = dict(self.listener_recovery_state.get(chat_name) or {})
                next_attempt_at = float(state.get("next_attempt_at") or 0)
                if next_attempt_at > now:
                    result["deferred"].append(chat_name)
                    continue
                attempt_count = int(state.get("attempt_count") or 0) + 1
                state.update({
                    "chat_name": chat_name,
                    "status": "recovering",
                    "attempt_count": attempt_count,
                    "last_attempt_at": now,
                    "last_error": None,
                })
                self.listener_recovery_state[chat_name] = state

            self.logger.warning(
                "🔧 检测到监听窗口缺失，正在自动恢复: chat=%s attempt=%s",
                chat_name,
                attempt_count,
            )
            error = None
            try:
                recovered = bool(self.wechat_manager.add_listen_chat(chat_name))
                if not recovered:
                    error = "add_listen_chat returned false"
            except Exception as exc:
                recovered = False
                error = str(exc)

            completed_at = time.time()
            with self._listener_recovery_lock:
                state = dict(self.listener_recovery_state.get(chat_name) or state)
                if recovered:
                    state.update({
                        "status": "recovered",
                        "failure_count": 0,
                        "last_success_at": completed_at,
                        "last_error": None,
                        "next_attempt_at": completed_at + self.listener_recovery_base_cooldown,
                    })
                else:
                    failure_count = int(state.get("failure_count") or 0) + 1
                    state.update({
                        "status": "failed",
                        "failure_count": failure_count,
                        "last_failed_at": completed_at,
                        "last_error": error,
                        "next_attempt_at": completed_at + self._listener_recovery_delay(failure_count),
                    })
                self.listener_recovery_state[chat_name] = state
                self.last_listener_recovery = dict(state)

            if recovered:
                result["recovered"].append(chat_name)
                self.logger.info("✅ 已自动恢复监听: chat=%s", chat_name)
            else:
                result["failed"].append(chat_name)
                self.logger.error(
                    "❌ 自动恢复监听失败，将在退避后重试: chat=%s error=%s next_attempt_at=%s",
                    chat_name,
                    error,
                    state.get("next_attempt_at"),
                )

        return result

    def _handle_listener_status(self, status: dict) -> None:
        """消费一次可信监听快照，并自动修复其中的缺失监听。"""
        if status.get("status") != "success" or status.get("probe_skipped"):
            return

        missing = self._normalized_listener_names(status.get("missing"))
        missing_set = set(missing)
        with self._listener_recovery_lock:
            for chat_name in list(self.listener_recovery_state):
                if chat_name not in missing_set:
                    self.listener_recovery_state.pop(chat_name, None)

        recovery = self._recover_missing_listeners(missing) if missing else {
            "recovered": [],
            "failed": [],
            "deferred": [],
        }
        recovered = set(recovery["recovered"])
        unresolved = [chat_name for chat_name in missing if chat_name not in recovered]
        self.last_missing_listeners = unresolved
        blocked = self._normalized_listener_names(status.get("blocked"))
        if blocked != self.last_blocked_listeners:
            if blocked:
                diagnostics = status.get("message_delivery") or {}
                self.logger.warning("⚠️ 监听线程存活但消息交付受阻: %s; recovery=%s",
                                    blocked, {name: diagnostics.get(name) for name in blocked})
            elif self.last_blocked_listeners:
                self.logger.info("✅ 消息交付恢复: %s", self.last_blocked_listeners)
        self.last_blocked_listeners = blocked
        current_listener_status = "degraded" if unresolved or blocked else "healthy"

        if self.last_listener_status != current_listener_status:
            if unresolved:
                self.logger.warning("⚠️ 监听健康检查发现缺失监听: %s", unresolved)
            elif not blocked:
                self.logger.info("✅ 监听健康检查正常")
            self.last_listener_status = current_listener_status

    def get_status(self) -> dict:
        """获取监控服务状态"""
        with self._listener_recovery_lock:
            recovery_state = {
                name: dict(state)
                for name, state in self.listener_recovery_state.items()
            }
            last_listener_recovery = (
                dict(self.last_listener_recovery)
                if self.last_listener_recovery
                else None
            )
        return {
            "monitoring": self.is_monitoring,
            "last_blocked_listeners": list(self.last_blocked_listeners),
            "bot_name": self.bot_name,
            "check_interval": self.check_interval,
            "offline_email_sent": self.offline_email_sent,
            "offline_failure_count": self.offline_failure_count,
            "offline_alert_threshold": self.offline_alert_threshold,
            "last_online_status": self.last_online_status,
            "auto_relogin_enabled": self.auto_relogin_enabled,
            "relogin_in_progress": self.relogin_in_progress,
            "last_relogin_result": (
                dict(self.last_relogin_result) if self.last_relogin_result else None
            ),
            "last_listener_status": self.last_listener_status,
            "last_missing_listeners": self.last_missing_listeners,
            "listener_auto_recovery_enabled": self.listener_auto_recovery_enabled,
            "listener_recovery_base_cooldown": self.listener_recovery_base_cooldown,
            "listener_recovery_max_cooldown": self.listener_recovery_max_cooldown,
            "listener_recovery_state": recovery_state,
            "last_listener_recovery": last_listener_recovery,
            "wechat_manager_available": self.wechat_manager is not None
        }

    def force_check(self) -> dict:
        """强制执行一次检查（用于测试）"""
        if not self.wechat_manager:
            return {"success": False, "message": "微信管理器未设置"}

        try:
            is_connected = self.wechat_manager.is_connected()
            is_online = self.wechat_manager.is_online() if is_connected else False
            listener_status = self.wechat_manager.get_listener_status() if is_connected else {}

            return {
                "success": True,
                "wx_bot_connected": is_connected,
                "wechat_online": is_online,
                "listener_status": listener_status,
                "listeners_healthy": not bool((listener_status or {}).get("missing")),
                "timestamp": time.time()
            }
        except Exception as e:
            return {"success": False, "message": str(e)}


# 全局实例
_monitor_service: Optional[WeChatMonitorService] = None

def get_monitor_service() -> WeChatMonitorService:
    """获取监控服务实例（单例模式）"""
    global _monitor_service
    if _monitor_service is None:
        _monitor_service = WeChatMonitorService()
    return _monitor_service

def start_wechat_monitoring(wechat_manager) -> WeChatMonitorService:
    """
    启动微信掉线监控的便捷函数

    Args:
        wechat_manager: 微信管理器实例

    Returns:
        WeChatMonitorService: 监控服务实例
    """
    monitor_service = get_monitor_service()
    monitor_service.set_wechat_manager(wechat_manager)
    monitor_service.start_monitoring()
    return monitor_service


if __name__ == "__main__":
    # 测试代码
    print("微信掉线监控服务模块")
    print("使用方法:")
    print("from app.services.wechat_monitor_service import start_wechat_monitoring")
    print("monitor = start_wechat_monitoring(wechat_manager)")
