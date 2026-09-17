"""
中国银行外汇牌价插件（boc_rate）
- 完整还原 legacy/boc_exchange_service.py 的功能与表现
- 保持缓存目录与文件格式一致，以便直接复用既有缓存
"""

import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Set

from app.core.event_bus import Event, EventType
from app.plugins.boc_rate.boc_exchange_service import BOCExchangeService
from app.services.plugin_config_context import ScopedConfigAttribute
from app.utils.plugin_config import get_config
import pytz


logger = logging.getLogger(__name__)


class BOCExchangePlugin:
    @ScopedConfigAttribute
    def trigger_keywords(self):
        return get_config('trigger_keywords', ['汇率', '中行', '牌价'], plugin_name='boc_rate') or []

    def __init__(self, context):
        self.context = context
        # 与 legacy 一致的初始化（无需特殊配置时传入空dict）
        migration_notes = context.storage.migrate_legacy_directory(
            Path(__file__).parent / "exchange_rate_cache",
            storage_class="cache",
            relative="exchange_rates",
        )
        cache_dir = context.storage.cache_root / "exchange_rates"
        self.service = BOCExchangeService(
            config={}, cache_dir=cache_dir, workers=context.workers
        )
        if migration_notes:
            context.audit.record(
                "storage_migration",
                summary="中行汇率缓存已迁移到插件标准存储目录",
                details={"moved_files": len(migration_notes)},
            )
        self.trigger_keywords = get_config(
            "trigger_keywords", ["汇率", "中行", "牌价"], plugin_name="boc_rate"
        ) or []

    def _build_chart_callback(self, wx, chat_name: str):
        def _callback(path: str, currency: str):
            try:
                if wx and chat_name and path:
                    wx.send_files(chat_name, [path])
                    logger.info(f"✅ 成功发送 {currency} 走势图")
            except Exception as e:
                logger.error(f"❌ 发送图表失败: {e}")
        return _callback

    def _should_trigger(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        # 触发方式：命中配置关键词或任何币种别名
        if any(str(keyword) in t for keyword in self.trigger_keywords if str(keyword)):
            return True
        # 检测是否包含已知币种别名
        try:
            for _, aliases in self.service.CURRENCY_MAPPING.items():
                for alias in aliases:
                    if alias in t:
                        return True
        except Exception:
            pass
        return False

    def handle_text(self, event: Event):
        try:
            message = event.data.get("message", "")
            chat_name = event.data.get("chat_name", "")
            wx = event.context.get("wx")

            if not self._should_trigger(message):
                # 未触发：不消费，让后续插件继续处理
                return False

            # 为当前会话绑定专属图表回调（避免并发串聊）
            try:
                self.service._chart_callback = self._build_chart_callback(wx, chat_name)
            except Exception:
                pass

            text_reply, _ = self.service.query_exchange_rate(message, chat_name)

            if wx and text_reply:
                wx.send_message(chat_name, text_reply)
                return True
            return False
        except Exception as e:
            logger.error(f"boc_rate 处理失败: {e}")
            return False


# 全局实例
plugin: Optional[BOCExchangePlugin] = None

# 定时任务控制
_scheduler_started = False
_scheduler_lock = threading.Lock()
_scheduler_thread = None
_execution_lock = threading.Lock()


def handle_text(event: Event):
    if plugin:
        return plugin.handle_text(event)
    return False


def _parse_hhmm(s: str) -> Tuple[int, int]:
    try:
        parts = (s or "10:00").strip().split(":")
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
        hh = max(0, min(23, hh))
        mm = max(0, min(59, mm))
        return hh, mm
    except Exception:
        return 10, 0


def _seconds_until_beijing_time(target_hh: int, target_mm: int) -> float:
    tz = pytz.timezone("Asia/Shanghai")
    now_bj = datetime.now(tz)
    target = now_bj.replace(hour=target_hh, minute=target_mm, second=0, microsecond=0)
    if now_bj >= target:
        target = target + timedelta(days=1)
    return (target - now_bj).total_seconds()


def _get_push_enabled_users_for_boc_rate() -> Set[str]:
    """查询拥有 boc_rate#push 权限的用户/群组名称集合"""
    try:
        from app.models.base import SessionLocal
        from app.models.user_permission import WeChatUser
        db = SessionLocal()
        users = db.query(WeChatUser).all()
        enabled: Set[str] = set()
        for u in users:
            try:
                plugin_keys = {p.plugin_name for p in u.permissions}
                if "boc_rate#push" in plugin_keys:
                    if u.chat_name:
                        enabled.add(u.chat_name)
                    continue
                # 末级名匹配（如 some/path/boc_rate#push）
                for k in plugin_keys:
                    base = k.rsplit('/', 1)[-1]
                    if base == "boc_rate#push" and u.chat_name:
                        enabled.add(u.chat_name)
                        break
            except Exception:
                continue
        return enabled
    except Exception:
        return set()


def _compute_eur_change_from_cache(service: BOCExchangeService) -> Optional[Dict]:
    """基于缓存数据计算昨日均价、今日10点前最新价与涨跌幅。
    返回 { previous_avg, current_rate, change_rate } 或 None
    """
    currency = "欧元"
    bj_now = datetime.now(pytz.timezone('Asia/Shanghai'))
    end_date_str = bj_now.strftime("%Y-%m-%d")
    start_date_str = (bj_now - timedelta(days=30)).strftime("%Y-%m-%d")

    all_contents, cached_dates, today_latest_time = service._load_cached_data(
        currency, start_date_str, end_date_str
    )
    missing_ranges = service._get_missing_date_ranges(start_date_str, end_date_str, cached_dates)

    # 即使今天已有缓存文件，也要在异常判断前确认数据是否足够新；否则用户
    # 早晨查询过一次后，10点任务可能一直使用早晨的旧牌价。
    refresh_today = service._should_update_today_data(
        today_latest_time, min_interval_minutes=10
    )
    today_str = bj_now.strftime("%Y-%m-%d")
    if refresh_today and today_str in cached_dates:
        missing_ranges.append((today_str, today_str))

    if missing_ranges:
        try:
            new_data = service._fetch_missing_data_sync(currency, missing_ranges)
            if new_data:
                service._save_data_to_cache(currency, new_data)
                if refresh_today:
                    today_dot = bj_now.strftime("%Y.%m.%d")
                    all_contents = [
                        item for item in all_contents
                        if not item.get("发布时间", "").startswith(today_dot)
                    ]
                all_contents.extend(new_data)
        except Exception:
            pass

    if not all_contents:
        return None

    by_date: Dict[str, List[Dict]] = {}
    for item in all_contents:
        try:
            date_part = item["发布时间"].split(' ')[0]
            by_date.setdefault(date_part, []).append(item)
        except Exception:
            continue

    today_key = bj_now.strftime("%Y.%m.%d")
    yesterday_key = (bj_now - timedelta(days=1)).strftime("%Y.%m.%d")
    if yesterday_key not in by_date or today_key not in by_date:
        return None

    try:
        prev_rates = [float(r.get("现汇卖出价", "") or 0) for r in by_date[yesterday_key] if r.get("现汇卖出价")]
        if not prev_rates:
            return None
        previous_avg = sum(prev_rates) / len(prev_rates)
    except Exception:
        return None

    try:
        ten_am = bj_now.replace(hour=10, minute=0, second=0, microsecond=0)
        today_records = []
        for r in by_date[today_key]:
            try:
                ts = datetime.strptime(r["发布时间"], "%Y.%m.%d %H:%M:%S")
                if ts < ten_am.replace(tzinfo=None):
                    today_records.append((ts, float(r["现汇卖出价"])) )
            except Exception:
                continue
        if not today_records:
            return None
        today_records.sort(key=lambda x: x[0])
        current_rate = today_records[-1][1]
    except Exception:
        return None

    if previous_avg == 0:
        return None

    change_rate = (current_rate - previous_avg) / previous_avg * 100.0
    return {
        "previous_avg": previous_avg,
        "current_rate": current_rate,
        "change_rate": change_rate,
    }


def _check_trend_3day_signal(service: BOCExchangeService) -> Optional[Dict]:
    """趋势型规则：连续3天价格同向移动，且 |3日累计涨跌幅| > 1.2%。
    以“北京时间10点前最新价”作为当日代表价。
    返回 {"triggered": bool, "cum_change": float} 或 None（数据不足）
    """
    currency = "欧元"
    tz = pytz.timezone('Asia/Shanghai')
    bj_now = datetime.now(tz)
    # 最近数天窗口，覆盖 D-2、D-1、D
    end_date_str = bj_now.strftime("%Y-%m-%d")
    start_date_str = (bj_now - timedelta(days=5)).strftime("%Y-%m-%d")

    all_contents, _, _ = service._load_cached_data(currency, start_date_str, end_date_str)
    if not all_contents:
        return None

    # 按日期分组（YYYY.MM.DD）
    by_date: Dict[str, List[Dict]] = {}
    for item in all_contents:
        try:
            date_part = item["发布时间"].split(' ')[0]
            by_date.setdefault(date_part, []).append(item)
        except Exception:
            continue

    def _latest_before_10(date_key: str) -> Optional[float]:
        recs = by_date.get(date_key)
        if not recs:
            return None
        try:
            day = datetime.strptime(date_key, "%Y.%m.%d").date()
            ten_am = datetime(year=day.year, month=day.month, day=day.day, hour=10, minute=0, second=0)
            candidates: List[Tuple[datetime, float]] = []
            for r in recs:
                try:
                    ts = datetime.strptime(r["发布时间"], "%Y.%m.%d %H:%M:%S")
                    if ts < ten_am:
                        candidates.append((ts, float(r["现汇卖出价"])))
                except Exception:
                    continue
            if not candidates:
                return None
            candidates.sort(key=lambda x: x[0])
            return candidates[-1][1]
        except Exception:
            return None

    d0 = bj_now.strftime("%Y.%m.%d")
    d1 = (bj_now - timedelta(days=1)).strftime("%Y.%m.%d")
    d2 = (bj_now - timedelta(days=2)).strftime("%Y.%m.%d")

    p0 = _latest_before_10(d0)
    p1 = _latest_before_10(d1)
    p2 = _latest_before_10(d2)
    if p0 is None or p1 is None or p2 is None:
        return None

    diff1 = p1 - p2
    diff2 = p0 - p1
    same_direction = (diff1 > 0 and diff2 > 0) or (diff1 < 0 and diff2 < 0)
    if not same_direction or p2 == 0:
        return {"triggered": False, "cum_change": 0.0}

    cum_change = (p0 - p2) / p2 * 100.0
    triggered = abs(cum_change) > 1.2
    return {"triggered": triggered, "cum_change": cum_change}


def _execute_daily_task(event_bus, target_chat=None):
    if not plugin:
        return
    service = plugin.service
    try:
        result = _compute_eur_change_from_cache(service)
        if not result:
            logger.info("boc_rate: 数据不足或10点前无数据，跳过推送")
            return

        threshold = get_config("ALERT_THRESHOLD_PERCENT", plugin_name="boc_rate")
        try:
            threshold = float(threshold) if threshold is not None else 0.5
        except Exception:
            threshold = 0.5

        previous_avg = result["previous_avg"]
        current_rate = result["current_rate"]
        change_rate = result["change_rate"]

        # 规则A：单日异动
        trigger_single = abs(change_rate) > threshold

        # 规则B：3日趋势
        trend = _check_trend_3day_signal(service)
        trigger_trend = bool(trend and trend.get("triggered"))

        if not (trigger_single or trigger_trend):
            logger.info(
                f"boc_rate: 未触发。单日 {change_rate:.2f}% 阈值 {threshold:.2f}%；3日趋势触发={trigger_trend}"
            )
            return

        sign = "+" if change_rate >= 0 else ""
        text_out = (
            "欧元汇率异动\n"
            f"昨天均价: {previous_avg:.2f}\n"
            f"当前最新: {current_rate:.2f}\n"
            f"涨跌幅：{sign}{change_rate:.2f}%"
        )
        # 若触发了3日趋势规则，追加3日累计涨跌幅
        if trigger_trend and trend is not None:
            try:
                cum = float(trend.get("cum_change", 0.0))
                sign_trend = "+" if cum >= 0 else ""
                text_out += f"\n3日累计涨跌幅：{sign_trend}{cum:.2f}%"
            except Exception:
                pass

        chart_path = None
        try:
            bj_now = datetime.now(pytz.timezone('Asia/Shanghai'))
            end_date_str = bj_now.strftime("%Y-%m-%d")
            start_date_str = (bj_now - timedelta(days=30)).strftime("%Y-%m-%d")
            all_contents, _, _ = service._load_cached_data("欧元", start_date_str, end_date_str)
            if all_contents:
                chart_path = service._create_trend_chart(all_contents, "欧元")
        except Exception as e:
            logger.warning("boc_rate: 生成走势图失败: %s", e)

        enabled_users = _get_push_enabled_users_for_boc_rate()
        if target_chat is not None:
            enabled_users &= {target_chat}
        try:
            wx = event_bus.context.get("wx")
        except Exception:
            wx = None

        for chat_name in enabled_users:
            try:
                if wx:
                    wx.send_message(chat_name, text_out[:3500])
                    if chart_path:
                        wx.send_files(chat_name, [chart_path])
            except Exception as e:
                logger.warning("boc_rate: 推送到 %s 失败: %s", chat_name, e)
    except Exception as e:
        logger.warning("boc_rate: 每日任务执行出错: %s", e)


def _start_daily_scheduler(event_bus, context):
    global _scheduler_started, _scheduler_thread
    from app.services.plugin_chat_schedule import start_chat_schedule

    if not _scheduler_started:
        _scheduler_started = True
        _scheduler_thread = start_chat_schedule(
            context, event_bus, time_key="DAILY_PUSH_TIME",
            execution_lock=_execution_lock,
            callback=lambda chat_name, operation: _execute_daily_task(event_bus, target_chat=chat_name),
        )

def register(event_bus, subscribe, context):
    global plugin
    logger.info("📈 注册 boc_rate 插件...")
    plugin = BOCExchangePlugin(context)
    subscribe(
        event_type=EventType.TEXT_MESSAGE_RECEIVED,
        handler=handle_text
    )
    _start_daily_scheduler(event_bus, context)
    context.health.register(lambda: {
        "status": "healthy" if plugin is not None and _scheduler_thread is not None and _scheduler_thread.is_alive() else "degraded",
        "message": "汇率调度器运行正常" if plugin is not None and _scheduler_thread is not None and _scheduler_thread.is_alive() else "汇率调度器未运行",
        "scheduler_alive": bool(_scheduler_thread and _scheduler_thread.is_alive()),
    })
    context.register_cleanup(unregister)
    logger.info("✅ boc_rate 插件注册成功")


def unregister():
    global plugin
    global _scheduler_started
    plugin = None
    _scheduler_started = False
    logger.info("✅ boc_rate 插件卸载完成")


