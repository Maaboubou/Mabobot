"""Deterministic wall-clock schedules. Stored instants are UTC epoch seconds."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def instant(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        return result.timestamp()
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("时间必须包含时区，例如 2026-10-28T09:00:00+08:00") from exc


def _clock(value, *, midnight=False):
    if midnight and value == "24:00":
        return 1440
    try:
        hour, minute = map(int, value.split(":"))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59 or value != f"{hour:02}:{minute:02}":
            raise ValueError()
        return hour * 60 + minute
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("触发时间必须为 HH:MM；结束时刻可用 24:00") from exc


def validate_schedule(value, now):
    if not isinstance(value, dict):
        raise ValueError("缺少时间规则")
    kind = value.get("kind")
    keys = {"once": {"at"}, "daily": {"time", "times"}, "weekly": {"time", "times", "weekdays"},
            "interval": {"seconds", "start_at"},
            "window": {"start_time", "end_time", "seconds", "weekdays", "include_end"}}
    common = {"kind", "timezone"} | ({"start_date", "end_date"} if kind != "once" else set())
    if kind not in keys or set(value) - keys[kind] - common:
        raise ValueError("无效的时间规则或不适用于此触发方式的字段")
    zone = str(value.get("timezone") or "Asia/Shanghai")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("无效的 IANA 时区") from exc
    result = {**value, "timezone": zone}
    for key in ("start_date", "end_date"):
        if key in value:
            try:
                if datetime.strptime(value[key], "%Y-%m-%d").strftime("%Y-%m-%d") != value[key]:
                    raise ValueError()
            except (TypeError, ValueError) as exc:
                raise ValueError("起止日期须为 YYYY-MM-DD") from exc
    if value.get("start_date", "") > value.get("end_date", "9999-12-31"):
        raise ValueError("结束日期不能早于开始日期")
    if kind == "once":
        if instant(value.get("at")) <= now:
            raise ValueError("一次性任务的触发时间必须在未来")
    elif kind in {"interval", "window"}:
        seconds = value.get("seconds")
        if type(seconds) is not int or not 60 <= seconds <= 366 * 86400:
            raise ValueError("执行间隔须为 60 秒至 366 天之间的整数秒")
        if kind == "interval":
            result["start_at"] = value.get("start_at") or datetime.fromtimestamp(now + seconds, timezone.utc).isoformat()
            instant(result["start_at"])
        else:
            start = _clock(value.get("start_time"))
            end = _clock(value.get("end_time"), midnight=True)
            if start == end or seconds % 60 or seconds > 86400:
                raise ValueError("时段起止不能相同，间隔须为整分钟且不超过一天")
            if type(value.get("include_end", False)) is not bool:
                raise ValueError("include_end 必须为布尔值")
            result["include_end"] = value.get("include_end", False)
    else:
        if ("time" in value) == ("times" in value):
            raise ValueError("time 和 times 必须且只能提供一个")
        times = value.get("times", [value.get("time")])
        if not isinstance(times, list) or not 1 <= len(times) <= 48:
            raise ValueError("每天须为 1–48 个触发时刻")
        for clock in times:
            _clock(clock)
        if "times" in value:
            result["times"] = sorted(set(times))
    if kind == "weekly" or (kind == "window" and "weekdays" in value):
        days = value.get("weekdays")
        if not isinstance(days, list) or not days or any(type(d) is not int or not 1 <= d <= 7 for d in days):
            raise ValueError("weekdays 必须为 1（周一）到 7（周日）的数组")
        result["weekdays"] = sorted(set(days))
    return result


def _date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def _wall_slots(rule, after, *, forward):
    zone = ZoneInfo(rule["timezone"])
    date = datetime.fromtimestamp(after, zone).date()
    # Jump directly to distant bounds; searching a week suffices for weekly rules.
    if forward and rule.get("start_date"):
        date = max(date, _date(rule["start_date"]))
    if not forward and rule.get("end_date"):
        date = min(date, _date(rule["end_date"]) + timedelta(days=1))
    if rule["kind"] == "window":
        start, end = _clock(rule["start_time"]), _clock(rule["end_time"], midnight=True)
        if end < start:
            end += 1440
        minutes = range(start, end + int(rule.get("include_end", False)), rule["seconds"] // 60)
    else:
        minutes = [_clock(t) for t in rule.get("times", [rule.get("time")])]
    for offset in (range(-1, 9) if forward else range(0, -10, -1)):
        day = date + timedelta(days=offset)
        if rule.get("start_date") and day < _date(rule["start_date"]):
            continue
        if rule.get("end_date") and day > _date(rule["end_date"]):
            continue
        if rule.get("weekdays") and day.isoweekday() not in rule["weekdays"]:
            continue
        for minute in (minutes if forward else reversed(minutes)):
            naive = datetime(day.year, day.month, day.day) + timedelta(minutes=minute)
            local = naive.replace(tzinfo=zone, fold=0)
            stamp = local.timestamp()
            # Nonexistent wall times are skipped; repeated wall times fire once.
            if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == naive:
                yield stamp


def _base_fire(rule, when, *, forward):
    kind = rule["kind"]
    if kind == "once":
        stamp = instant(rule["at"])
        return stamp if (stamp > when if forward else stamp <= when) else None
    if kind == "interval":
        start, seconds = instant(rule["start_at"]), rule["seconds"]
        zone = ZoneInfo(rule["timezone"])
        lower = datetime.combine(_date(rule["start_date"]), datetime.min.time(), zone).timestamp() if rule.get("start_date") else start
        upper = datetime.combine(_date(rule["end_date"]) + timedelta(days=1), datetime.min.time(), zone).timestamp() if rule.get("end_date") else float("inf")
        if forward:
            import math
            n = max(0, math.floor((when - start) / seconds) + 1, math.ceil((lower - start) / seconds))
        else:
            import math
            n = math.floor((min(when, upper - 0.000001) - start) / seconds)
        stamp = start + n * seconds
        return stamp if n >= 0 and lower <= stamp < upper else None
    slots = (t for t in _wall_slots(rule, when, forward=forward) if (t > when if forward else t <= when))
    return next(slots, None)


def next_fire(rule, after, exception=None):
    stamp = _base_fire(rule, after, forward=True)
    if exception and stamp == exception["original_at"]:
        stamp = _base_fire(rule, stamp, forward=True)
    replacement = (exception or {}).get("replacement_at")
    if replacement is not None and replacement > after:
        stamp = min(stamp, replacement) if stamp is not None else replacement
    return stamp


def latest_fire(rule, now, exception=None):
    stamp = _base_fire(rule, now, forward=False)
    if exception and stamp == exception["original_at"]:
        stamp = _base_fire(rule, stamp - 0.000001, forward=False)
    replacement = (exception or {}).get("replacement_at")
    if replacement is not None and replacement <= now:
        stamp = max(stamp, replacement) if stamp is not None else replacement
    return stamp


def preview(rule, now, count=3, exception=None):
    times = []
    for _ in range(count):
        now = next_fire(rule, now, exception)
        if now is None:
            break
        times.append(now)
    return times


def display_time(stamp, zone="Asia/Shanghai"):
    return datetime.fromtimestamp(stamp, ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M:%S") if stamp is not None else "无"


def describe(rule):
    zone = "北京时间" if rule["timezone"] == "Asia/Shanghai" else rule["timezone"]
    kind = rule["kind"]
    if kind == "once":
        text = "仅执行一次"
    elif kind == "interval":
        seconds = rule["seconds"]
        text = f"每隔 {seconds // 3600} 小时" if seconds % 3600 == 0 else f"每隔 {seconds // 60} 分钟" if seconds % 60 == 0 else f"每隔 {seconds} 秒"
    elif kind == "daily":
        text = "每天 " + "、".join(rule.get("times", [rule.get("time")]))
    elif kind == "window":
        days = "每周" + "、".join("一二三四五六日"[d - 1] for d in rule["weekdays"]) if rule.get("weekdays") else "每天"
        cross = "次日 " if _clock(rule["end_time"], midnight=True) < _clock(rule["start_time"]) else ""
        text = f"{days} {rule['start_time']}–{cross}{rule['end_time']}，每 {rule['seconds'] // 60} 分钟"
        text += "（包含结束时刻）" if rule.get("include_end") else "（不含结束时刻）"
    else:
        text = "每周" + "、".join("一二三四五六日"[d - 1] for d in rule["weekdays"]) + " " + "、".join(rule.get("times", [rule.get("time")]))
    if rule.get("start_date") or rule.get("end_date"):
        text += f" · {rule.get('start_date', '即日起')} 至 {rule.get('end_date', '长期')}"
    return f"{text}（{zone}）"
