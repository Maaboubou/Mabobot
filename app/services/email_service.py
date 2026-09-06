#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
邮件通知服务 - 适合新架构
基于legacy/Email_notify.py，但使用数据库配置管理
支持独立运行（如 Ping_notify.py），此时从环境变量读取配置。
"""

import html
import logging
import mimetypes
import os
import smtplib
import ssl
import time
import threading
from email import encoders
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from .email_settings_service import load_config, normalize, read_setting, record_delivery

_get_setting_from_db = read_setting

logger = logging.getLogger(__name__)


class EmailService:
    """邮件服务类"""

    def __init__(self, config=None):
        self._config_override = config
        self._local = threading.local()
        self._attempts = {}
        self._send_lock = threading.Lock()

    @property
    def last_message(self):
        return getattr(self._local, "message", "")

    @property
    def last_status(self):
        return getattr(self._local, "status", "")

    def _config(self):
        return self._config_override or load_config(_get_setting_from_db)

    def event_enabled(self, event: str) -> bool:
        try:
            config = self._config()
            return bool(config["enabled"] and config["events"].get(event, True)
                        and config["address"] and config["password"])
        except Exception:
            return False

    @property
    def has_saved_preferences(self) -> bool:
        return self._config().get("revision") != "legacy"

    def _get_email_address(self) -> Optional[str]:
        return self._config()["address"] or None

    def _get_auth_code(self) -> Optional[str]:
        return self._config()["password"] or None

    def _result(self, event, status, message, config, *, record=True):
        self._local.status = status
        self._local.message = message
        if record:
            try:
                record_delivery(event, status, message, config.get("revision", ""))
            except Exception:
                logger.warning("邮件发送记录未能保存")
        if status == "failed":
            logger.warning("邮件提醒：%s", message)
        return status == "sent"

    def send_email(
        self,
        body_text: str,
        subject: str = "微信助手通知",
        attachment_paths: Optional[Iterable[str | os.PathLike[str]]] = None,
        body_html: Optional[str] = None,
        inline_image_paths: Optional[
            Iterable[tuple[str, str | os.PathLike[str]]]
        ] = None,
        *,
        event: str = "general",
    ) -> bool:
        """
        发送邮件

        Args:
            body_text: 正文内容
            subject: 邮件标题，默认为 "微信助手通知"
            attachment_paths: 可选附件路径列表
            body_html: 可选 HTML 正文；提供时与纯文本正文组成兼容内容
            inline_image_paths: 可选的 ``(Content-ID, 图片路径)`` 列表

        Returns:
            bool: 发送成功返回True，失败返回False
        """
        try:
            config = normalize(self._config())
        except Exception:
            return self._result(event, "failed", "邮箱配置不可用，请重新保存配置", {}, record=False)
        if event != "test" and (not config["enabled"] or not config["events"].get(event, True)):
            return self._result(event, "skipped", "邮件提醒未开启或未订阅此事件", config, record=False)
        if not all(config.get(key) for key in ("address", "host", "password")):
            return self._result(event, "skipped" if event != "test" else "failed", "请先配置邮箱和授权码", config, record=event == "test")
        # Failed deliveries back off independently of the business alert state.
        attempt_key = (event, config.get("revision"))
        with self._send_lock:
            if time.monotonic() < self._attempts.get(attempt_key, 0):
                return self._result(event, "skipped", "发送冷却中，请稍后再试", config, record=False)
            self._attempts[attempt_key] = time.monotonic() + (10 if event == "test" else 300)
        qq_email = config["address"]
        auth_code = config["password"]
        server = None

        try:
            msg = MIMEMultipart("mixed")
            msg['From'] = qq_email
            msg['To'] = config['recipient'] or qq_email
            msg['Subject'] = subject

            inline_images = tuple(inline_image_paths or ())
            if body_html or inline_images:
                related = MIMEMultipart("related")
                alternatives = MIMEMultipart("alternative")
                alternatives.attach(MIMEText(body_text, 'plain', 'utf-8'))
                if body_html:
                    alternatives.attach(MIMEText(body_html, 'html', 'utf-8'))
                related.attach(alternatives)

                for content_id, inline_image_path in inline_images:
                    path = Path(inline_image_path)
                    content_type, _encoding = mimetypes.guess_type(path.name)
                    if not content_type or not content_type.startswith("image/"):
                        raise ValueError(f"内嵌内容不是受支持的图片: {path.name}")
                    _maintype, subtype = content_type.split("/", 1)
                    part = MIMEImage(path.read_bytes(), _subtype=subtype)
                    part.add_header("Content-ID", f"<{content_id}>")
                    part.add_header(
                        "Content-Disposition",
                        "inline",
                        filename=path.name,
                    )
                    part.add_header("Content-Location", path.name)
                    related.attach(part)

                msg.attach(related)
            else:
                msg.attach(MIMEText(body_text, 'plain', 'utf-8'))

            for attachment_path in attachment_paths or ():
                path = Path(attachment_path)
                content_type, _encoding = mimetypes.guess_type(path.name)
                if not content_type:
                    content_type = "application/octet-stream"
                maintype, subtype = content_type.split("/", 1)
                part = MIMEBase(maintype, subtype)
                part.set_payload(path.read_bytes())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    "attachment",
                    filename=path.name,
                )
                msg.attach(part)

            context = ssl.create_default_context()
            if config["security"] == "ssl":
                server = smtplib.SMTP_SSL(config["host"], config["port"], timeout=15, context=context)
            else:
                server = smtplib.SMTP(config["host"], config["port"], timeout=15)
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
            server.login(config["username"] or qq_email, auth_code)
            refused = server.send_message(msg)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
            with self._send_lock:
                if event != "test":
                    self._attempts.pop(attempt_key, None)
            return self._result(event, "sent", "已提交发送，请检查收件箱及垃圾邮件", config)
        except smtplib.SMTPAuthenticationError:
            message = "邮箱认证失败，请检查账号、授权码及 SMTP 是否开启"
        except smtplib.SMTPRecipientsRefused:
            message = "收件地址被拒绝，请检查收件邮箱"
        except ssl.SSLError:
            message = "邮箱服务的加密连接或证书验证失败，请检查 SMTP 配置"
        except (TimeoutError, OSError):
            message = "连接邮箱服务失败，请检查服务器、端口和网络后重试"
        except smtplib.SMTPNotSupportedError:
            message = "邮箱服务不支持所选加密方式，请检查 SMTP 配置"
        except Exception:
            message = "邮件发送失败，请检查邮箱服务设置后重试"
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    try:
                        server.close()
                    except Exception:
                        pass
        return self._result(event, "failed", message, config)

    def send_offline_notification(self, bot_name: str = "微信助手") -> bool:
        """发送掉线通知邮件"""
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        email_body = f"""
{bot_name} 掉线通知

机器人名称: {bot_name}
时间: {current_time}
状态: 微信掉线 (wx.IsOnline() 返回False或异常)

请检查微信客户端状态，可能需要重新登录。

此消息由微信掉线监控系统自动发送。
        """.strip()

        return self.send_email(email_body, f"🚨 {bot_name} 掉线通知", event="wechat_offline")

    def send_login_qr_notification(
        self,
        qr_image_path: str | os.PathLike[str],
        bot_name: str = "微信助手",
        reason: str = "微信要求重新扫码登录",
    ) -> bool:
        """发送正文内嵌当前登录二维码的掉线通知。"""
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        host = os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "unknown"
        email_body = f"""
{bot_name} 已掉线，自动化已进入微信扫码登录页。

机器人名称: {bot_name}
主机: {host}
时间: {current_time}
状态: {reason}

请尽快使用微信扫描正文中的登录二维码。二维码可能失效；如果扫码失败，请远程查看电脑上的微信窗口。
请勿转发本邮件或其中的二维码。

此消息由微信掉线监控系统自动发送。
        """.strip()

        escaped_bot_name = html.escape(str(bot_name))
        escaped_host = html.escape(str(host))
        escaped_time = html.escape(current_time)
        escaped_reason = html.escape(str(reason))
        email_html = f"""<!doctype html>
<html lang="zh-CN">
<body style="margin:0;padding:24px;background:#f5f5f5;color:#222;font-family:'Microsoft YaHei',Arial,sans-serif;">
  <div style="max-width:620px;margin:0 auto;padding:28px;background:#fff;border-radius:12px;">
    <h2 style="margin:0 0 18px;font-size:20px;">{escaped_bot_name} 需要扫码登录</h2>
    <p style="margin:0 0 16px;line-height:1.7;">微信已掉线，自动化已进入扫码登录页。</p>
    <p style="margin:0 0 20px;line-height:1.7;">
      机器人名称：{escaped_bot_name}<br>
      主机：{escaped_host}<br>
      时间：{escaped_time}<br>
      状态：{escaped_reason}
    </p>
    <div style="margin:20px 0;text-align:center;">
      <img src="cid:wechat-login-qr" alt="微信登录二维码" width="544"
           style="display:inline-block;width:100%;max-width:544px;height:auto;border:0;image-rendering:pixelated;">
    </div>
    <p style="margin:18px 0 0;line-height:1.7;">请尽快使用微信扫描上方二维码。二维码可能失效；如果扫码失败，请远程查看电脑上的微信窗口。</p>
    <p style="margin:12px 0 0;color:#b42318;line-height:1.7;">请勿转发本邮件或其中的二维码。</p>
  </div>
</body>
</html>"""

        return self.send_email(
            email_body,
            f"🔐 {bot_name} 需要扫码登录",
            body_html=email_html,
            event="wechat_offline",
            inline_image_paths=[("wechat-login-qr", qr_image_path)],
        )

    def send_recovery_notification(self, bot_name: str = "微信助手") -> bool:
        """发送恢复通知邮件"""
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        email_body = f"""
{bot_name} 状态已恢复正常！

机器人名称: {bot_name}
时间: {current_time}
状态: 微信在线 (wx.IsOnline() 返回True)

{bot_name} 已重新正常运行，所有功能恢复正常。

此消息由微信掉线监控系统自动发送。
        """.strip()

        return self.send_email(email_body, f"✅ {bot_name} 恢复在线", event="wechat_recovery")


# 全局实例
_email_service = None

def get_email_service() -> EmailService:
    """获取邮件服务实例（单例模式）"""
    global _email_service
    if _email_service is None:
        _email_service = EmailService()
    return _email_service


# 便捷函数，保持与legacy代码兼容
def send_email(body_text: str, subject: str = "微信助手通知") -> bool:
    """
    便捷函数：发送邮件
    保持与legacy/Email_notify.py的接口兼容
    """
    service = get_email_service()
    return service.send_email(body_text, subject)


def send_alert_email(subject: str, body: str) -> bool:
    """
    独立运行专用便捷函数（供 Ping_notify.py 等脚本调用）。
    直接从环境变量 QQEMAIL_ADDR / QQEMAIL_CODE 读取配置，无需数据库。

    Args:
        subject: 邮件主题
        body:    邮件正文

    Returns:
        bool: 发送成功返回 True，失败返回 False
    """
    qq_email = str(os.getenv("QQEMAIL_ADDR") or "").strip()
    auth_code = os.getenv("QQEMAIL_CODE")

    if not qq_email:
        logger.error("❌ 邮箱地址未配置，请设置环境变量 QQEMAIL_ADDR")
        print("❌ 邮箱地址未配置，请设置环境变量 QQEMAIL_ADDR")
        return False

    if not auth_code:
        logger.error("❌ 邮件授权码未配置，请设置环境变量 QQEMAIL_CODE")
        print("❌ 邮件授权码未配置，请设置环境变量 QQEMAIL_CODE")
        return False

    try:
        msg = MIMEMultipart()
        msg["From"] = qq_email
        msg["To"] = qq_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        server = smtplib.SMTP_SSL("smtp.qq.com", 465)
        server.login(qq_email, auth_code)
        server.send_message(msg)
        server.quit()

        logger.info(f"✅ 警报邮件发送成功: {subject}")
        print(f"✅ 邮件发送成功: {subject}")
        return True

    except Exception as e:
        logger.error(f"❌ 警报邮件发送失败: {e}")
        print(f"❌ 邮件发送失败: {e}")
        return False


if __name__ == "__main__":
    # 测试邮件发送
    service = get_email_service()

    test_body = f"""
这是一封测试邮件。

发送时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
测试内容: 新架构邮件功能正常

如果您收到这封邮件，说明邮件配置正确。
    """.strip()

    if service.send_email(test_body, "📧 新架构邮件功能测试"):
        print("测试邮件发送成功")
    else:
        print("测试邮件发送失败")
