"""Normalize visible WeChat time separators for history callback cutoffs."""
from datetime import datetime, timedelta
import re


def history_timestamp(value: str, now: datetime | None = None) -> str:
    value = str(value or '').strip()
    now = now or datetime.now()
    for pattern in ('%Y-%m-%d %H:%M', '%Y/%m/%d %H:%M', '%Y年%m月%d日 %H:%M'):
        try:
            return datetime.strptime(value, pattern).strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            pass
    match = re.fullmatch(r'(?:(今天|昨天|前天|星期[一二三四五六日天]|\d{1,2}月\d{1,2}日)\s+)?(\d{1,2}):(\d{2})', value)
    if not match:
        return value
    label, hour, minute = match.groups()
    date = now
    try:
        if label in ('昨天', '前天'):
            date -= timedelta(days=1 if label == '昨天' else 2)
        elif label and label.startswith('星期'):
            weekday = '一二三四五六日'.find(label[-1].replace('天', '日'))
            days = (now.weekday() - weekday) % 7
            date -= timedelta(days=days or 7)
        elif label and '月' in label:
            month, day = map(int, re.findall(r'\d+', label))
            date = now.replace(month=month, day=day)
            if date.date() > now.date():
                date = date.replace(year=date.year - 1)
        return date.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0).strftime('%Y-%m-%d %H:%M:%S')
    except ValueError:
        return value
