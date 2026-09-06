"""Local-only email configuration shared by the web console and desktop launcher."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

CONFIG_KEY = "EMAIL_NOTIFICATION_CONFIG"
PASSWORD_KEY = "EMAIL_NOTIFICATION_PASSWORD"
HISTORY_KEY = "EMAIL_NOTIFICATION_HISTORY"
EVENTS = {
    "wechat_offline": "微信掉线及扫码提醒",
    "wechat_recovery": "微信恢复在线",
    "bili_cookie": "B 站登录失效",
    "startup_failure": "开机自动登录失败",
}
PRESETS = {
    "qq": {"label": "QQ 邮箱", "host": "smtp.qq.com", "port": 465, "security": "ssl"},
    "163": {"label": "163 邮箱", "host": "smtp.163.com", "port": 465, "security": "ssl"},
    "126": {"label": "126 邮箱", "host": "smtp.126.com", "port": 465, "security": "ssl"},
    "gmail": {"label": "Gmail", "host": "smtp.gmail.com", "port": 465, "security": "ssl"},
    "custom": {"label": "其他邮箱 / 自定义 SMTP", "host": "", "port": 465, "security": "ssl"},
}
_lock = threading.RLock()
_test_next_at = 0.0


class EmailPreferences(BaseModel):
    enabled: bool = False
    provider: Literal["qq", "163", "126", "gmail", "custom"] = "qq"
    address: str = Field(default="", max_length=254)
    recipient: str = Field(default="", max_length=254)
    username: str = Field(default="", max_length=254)
    host: str = Field(default="smtp.qq.com", max_length=253)
    port: int = Field(default=465, ge=1, le=65535)
    security: Literal["ssl", "starttls"] = "ssl"
    events: dict[str, bool] = Field(default_factory=lambda: dict.fromkeys(EVENTS, True))


class EmailPreferencesUpdate(EmailPreferences):
    password: str | None = Field(default=None, max_length=1024)
    clear_password: bool = False


def open_settings_db():
    """The launcher can configure notifications before the first web startup."""
    from app.models.base import SessionLocal
    from app.models.setting import Setting
    db = SessionLocal()
    try:
        Setting.__table__.create(bind=db.get_bind(), checkfirst=True)
        return db
    except Exception:
        db.close()
        raise


def read_setting(key):
    # Bypass the general config cache so saves also take effect in other processes.
    from app.models.setting import Setting
    with open_settings_db() as db:
        row = db.query(Setting).filter_by(key=key).first()
        return row.value if row else None


def _decode(value, default):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else default
    except (ValueError, TypeError):
        return default


def load_config(reader=read_setting):
    raw = reader(CONFIG_KEY)
    if raw is not None:
        config = _decode(raw, {})
        validated = EmailPreferences(**config).model_dump()
        validated["password"] = str(reader(PASSWORD_KEY) or "")
        validated["revision"] = config.get("revision", "")
        return validated
    address = str(reader("QQEMAIL_ADDR") or os.getenv("QQEMAIL_ADDR") or "").strip()
    password = str(reader("QQEMAIL_CODE") or os.getenv("QQEMAIL_CODE") or "")
    config = EmailPreferences(address=address, enabled=bool(address and password)).model_dump()
    return {**config, "password": password, "revision": "legacy"}


def normalize(config):
    config = dict(config)
    for key in ("address", "recipient", "username", "host"):
        config[key] = config[key].strip()
    if config["provider"] != "custom":
        preset = PRESETS[config["provider"]]
        config.update({key: preset[key] for key in ("host", "port", "security")})
    if config["provider"] == "gmail":
        # Google displays app passwords in groups; pasted spaces are not part of it.
        config["password"] = "".join(config["password"].split())
    config["events"] = {key: config["events"].get(key, True) for key in EVENTS}
    for key in ("address", "recipient"):
        value = config[key]
        if value and not re.fullmatch(r"[^\s@<>;,]+@[^\s@<>;,]+\.[^\s@<>;,]+", value):
            raise ValueError("请输入有效的发件邮箱和收件邮箱地址")
    if config["host"] and not re.fullmatch(r"[A-Za-z0-9.-]+", config["host"]):
        raise ValueError("SMTP 服务器只能填写主机名或 IPv4 地址，不要包含协议或端口")
    if any(ch in config["username"] for ch in "\r\n"):
        raise ValueError("SMTP 登录账号不能包含换行")
    if config["enabled"] and not all(config.get(key) for key in ("address", "host", "password")):
        raise ValueError("开启提醒前，请填写发件邮箱、SMTP 服务器和授权码")
    return config


def _put(db, key, value):
    from app.models.setting import Setting
    row = db.query(Setting).filter_by(key=key).first()
    if row is None:
        row = Setting(key=key, category="notifications")
        db.add(row)
    row.value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def public_config(db):
    from app.models.setting import Setting
    def reader(key):
        row = db.query(Setting).filter_by(key=key).first()
        return row.value if row else None
    config = load_config(reader)
    configured = bool(config["address"] and config["host"] and config["password"])
    config["password_set"] = bool(config.pop("password"))
    history = _decode(reader(HISTORY_KEY), [])
    last_test = next((item for item in history if item.get("event") == "test" and item.get("revision") == config["revision"]), None)
    status = "已关闭" if not config["enabled"] else "待测试"
    if not configured:
        status = "未配置"
    elif last_test:
        status = "测试成功" if last_test["status"] == "sent" else "测试失败"
        if not config["enabled"]:
            status += " · 提醒已关闭"
    latest = next((item for item in history if item.get("revision") == config["revision"]), None)
    if configured and config["enabled"] and latest and latest["status"] == "failed":
        status = "测试失败" if latest["event"] == "test" else "发送异常"
    return {"config": config, "status": status, "presets": PRESETS, "events": EVENTS, "history": history[:20]}


def save_config(db, request: EmailPreferencesUpdate):
    from app.models.setting import Setting
    with _lock:
        def reader(key):
            row = db.query(Setting).filter_by(key=key).first()
            return row.value if row else None
        previous = load_config(reader)
        config = request.model_dump(exclude={"password", "clear_password"})
        config["password"] = "" if request.clear_password else (request.password or previous["password"])
        if config["password"] == "********":
            raise ValueError("请输入真实授权码，或留空保留已保存的授权码")
        config = normalize(config)
        password = config.pop("password")
        config["revision"] = uuid4().hex
        try:
            _put(db, CONFIG_KEY, config)
            _put(db, PASSWORD_KEY, password)
            db.commit()
        except Exception:
            db.rollback()
            raise
    return public_config(db)


def record_delivery(event, status, message, revision):
    # Never persist mail bodies, QR codes, recipients or server exception text.
    from app.models.base import SessionLocal
    from app.models.setting import Setting
    with _lock, SessionLocal() as db:
        row = db.query(Setting).filter_by(key=HISTORY_KEY).first()
        history = _decode(row.value if row else None, [])
        history.insert(0, {"time": datetime.now(timezone.utc).isoformat(), "event": event,
                           "status": status, "message": message, "revision": revision})
        _put(db, HISTORY_KEY, history[:20])
        db.commit()


def test_saved_config(db):
    global _test_next_at
    from app.services.email_service import EmailService
    from app.models.setting import Setting
    def reader(key):
        row = db.query(Setting).filter_by(key=key).first()
        return row.value if row else None
    with _lock:
        if time.monotonic() < _test_next_at:
            return {"ok": False, "message": "请等待 10 秒后再发送测试邮件", "settings": public_config(db)}
        _test_next_at = time.monotonic() + 10
    service = EmailService(config=load_config(reader))
    ok = service.send_email("这是一封 Mabobot 测试邮件。如果收到此邮件，说明邮箱发送配置可用。", "Mabobot 邮箱提醒测试", event="test")
    db.expire_all()
    return {"ok": ok, "message": service.last_message, "settings": public_config(db)}
