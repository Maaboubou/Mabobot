"""
Weekly 周报插件
- 每周定时读取开启 Weekly#push 的群过去 7 天聊天记录
- 由 Codex 先做多页周报规划，再调用 Codex 原生子代理/批处理生成 4:3 图片
- 将每页图片压缩合并为多页 PDF，并推送 PDF 文件
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pytz
from PIL import Image, JpegImagePlugin  # noqa: F401 - registers JPEG PDF encoder

from app.core.event_bus import Event, EventType
from app.services.codex_profile_service import get_codex_runtime_registry
from app.utils.plugin_config import get_config

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class WeeklyGenerationError(RuntimeError):
    """周报生成失败时抛出的业务异常。"""


class WeeklyPlugin:
    def __init__(self, context):
        self.context = context
        self.log_dir = Path("data/chat_logs")
        migration_notes = context.storage.migrate_legacy_directory(
            Path("data/weekly_reports"), storage_class="generated", relative="reports"
        )
        self.output_root = context.storage.generated_root / "reports"
        self.state_path = context.storage.persistent_path("weekly_state.json")
        migrated_state = self.output_root / "weekly_state.json"
        if migrated_state.exists() and not self.state_path.exists():
            os.replace(migrated_state, self.state_path)
            migration_notes.append(f"{migrated_state} -> {self.state_path}")
        # 任务可能跨越插件热重载继续运行。临时目录会在运行时上下文关闭时被
        # 自动清理，锁文件放在那里会让新插件实例误以为没有任务在执行。
        # machine_bound 存储不会随热重载清空，同时也不会进入可迁移备份。
        self.task_lock_path = context.storage.machine_bound_path("weekly_task.lock")
        self.output_root.mkdir(parents=True, exist_ok=True)
        if migration_notes:
            context.audit.record(
                "storage_migration",
                summary="Weekly 已迁移到插件标准存储目录",
                details={"moved_files": len(migration_notes)},
            )
        logger.info("🗞️ Weekly 插件初始化完成")

    def handle_text(self, event: Event):
        return _handle_admin_text(event)


plugin: Optional[WeeklyPlugin] = None
_scheduler_started = False
_scheduler_lock = threading.Lock()
_scheduler_thread = None
_execution_lock = threading.Lock()
_state_lock = threading.Lock()


def _cfg_str(key: str, default: str) -> str:
    value = get_config(key, default, plugin_name="Weekly")
    return str(value if value is not None else default).strip()


def _cfg_int(key: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        value = int(get_config(key, default, plugin_name="Weekly"))
    except Exception:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _cfg_bool(key: str, default: bool) -> bool:
    value = get_config(key, default, plugin_name="Weekly")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "y"}:
            return True
        if normalized in {"0", "false", "no", "off", "n"}:
            return False
    return default


def _parse_hhmm(value: str) -> tuple[int, int]:
    try:
        parts = (value or "09:00").strip().split(":")
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
        return max(0, min(23, hh)), max(0, min(59, mm))
    except Exception:
        return 9, 0


def _parse_weekday(value: str) -> int:
    text = str(value or "MON").strip().upper()
    aliases = {
        "MON": 0,
        "MONDAY": 0,
        "周一": 0,
        "TUE": 1,
        "TUESDAY": 1,
        "周二": 1,
        "WED": 2,
        "WEDNESDAY": 2,
        "周三": 2,
        "THU": 3,
        "THURSDAY": 3,
        "周四": 3,
        "FRI": 4,
        "FRIDAY": 4,
        "周五": 4,
        "SAT": 5,
        "SATURDAY": 5,
        "周六": 5,
        "SUN": 6,
        "SUNDAY": 6,
        "周日": 6,
        "周天": 6,
    }
    if text in aliases:
        return aliases[text]
    try:
        number = int(text)
        if 1 <= number <= 7:
            return number - 1
        if 0 <= number <= 6:
            return number
    except Exception:
        pass
    return 0


def _seconds_until_beijing_weekly(target_weekday: int, target_hh: int, target_mm: int) -> float:
    target = _next_beijing_weekly_run(target_weekday, target_hh, target_mm)
    now_bj = datetime.now(pytz.timezone("Asia/Shanghai"))
    return (target - now_bj).total_seconds()


def _next_beijing_weekly_run(target_weekday: int, target_hh: int, target_mm: int) -> datetime:
    tz = pytz.timezone("Asia/Shanghai")
    now_bj = datetime.now(tz)
    days_ahead = (target_weekday - now_bj.weekday()) % 7
    target = (now_bj + timedelta(days=days_ahead)).replace(
        hour=target_hh,
        minute=target_mm,
        second=0,
        microsecond=0,
    )
    if target <= now_bj:
        target += timedelta(days=7)
    return target


def _safe_filename(value: str) -> str:
    text = re.sub(r'[\\/:*?"<>|\r\n]+', "_", value or "").strip(" ._")
    return text[:80] or "chat"


def _get_push_enabled_chats() -> Set[str]:
    try:
        from app.models.base import SessionLocal
        from app.models.user_permission import WeChatUser

        db = SessionLocal()
        try:
            users = db.query(WeChatUser).all()
            enabled: Set[str] = set()
            for user in users:
                try:
                    plugin_keys = {p.plugin_name for p in user.permissions}
                    if "Weekly#push" in plugin_keys:
                        if user.chat_name:
                            enabled.add(user.chat_name)
                        continue
                    for key in plugin_keys:
                        if key.rsplit("/", 1)[-1] == "Weekly#push" and user.chat_name:
                            enabled.add(user.chat_name)
                            break
                except Exception:
                    continue
            return enabled
        finally:
            db.close()
    except Exception as e:
        logger.error("🗞️ Weekly: 获取启用列表失败: %s", e, exc_info=True)
        return set()


_PLACEHOLDER_VALUES = {
    "[图片]",
    "[表情]",
    "[视频]",
    "[语音]",
    "[文件]",
}


def _is_placeholder_content(content: str) -> bool:
    text = (content or "").strip()
    if not text:
        return True
    if text in _PLACEHOLDER_VALUES:
        return True
    return bool(re.fullmatch(r"(?:\[[^\]]+\]\s*)+", text))


def _extract_last_7d_logs(chat_name: str) -> tuple[list[dict[str, str]], dict[str, Any]]:
    log_path = Path("data/chat_logs") / f"{chat_name}.jsonl"
    if not log_path.exists():
        return [], {"reason": "log_not_found", "path": str(log_path)}

    tz = pytz.timezone("Asia/Shanghai")
    end_time = datetime.now(tz).replace(tzinfo=None)
    start_time = end_time - timedelta(days=7)

    excluded: Dict[str, int] = {}
    records: list[dict[str, str]] = []
    raw_count = 0
    unique_senders: set[str] = set()

    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)
                log_time = datetime.strptime(str(item.get("time")), "%Y-%m-%d %H:%M:%S")
            except Exception:
                excluded["parse_error"] = excluded.get("parse_error", 0) + 1
                continue
            if not (start_time <= log_time <= end_time):
                continue
            raw_count += 1
            sender = str(item.get("sender") or "未知").strip()
            content = str(item.get("content") or "").strip()
            if sender == "OCR":
                excluded["OCR"] = excluded.get("OCR", 0) + 1
                continue
            if sender == "链接摘要":
                excluded["链接摘要"] = excluded.get("链接摘要", 0) + 1
                continue
            if _is_placeholder_content(content):
                excluded["placeholder"] = excluded.get("placeholder", 0) + 1
                continue
            unique_senders.add(sender)
            records.append(
                {
                    "time": log_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "sender": sender,
                    "content": content,
                }
            )

    meta = {
        "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S"),
        "raw_count": raw_count,
        "kept_count": len(records),
        "unique_senders": len(unique_senders),
        "excluded": excluded,
    }
    return records, meta


def _format_logs_for_codex(records: list[dict[str, str]], max_chars: int) -> str:
    lines: list[str] = []
    total = 0
    truncated = 0
    for item in records:
        content = re.sub(r"\s+", " ", item["content"]).strip()
        if len(content) > 500:
            content = content[:500] + "..."
        line = f"{item['time']} | {item['sender']} | {content}"
        if total + len(line) + 1 > max_chars:
            truncated += 1
            continue
        lines.append(line)
        total += len(line) + 1
    if truncated:
        lines.append(f"[系统裁剪说明] 由于 WEEKLY_MAX_LOG_CHARS 限制，后续 {truncated} 条记录未传入。")
    return "\n".join(lines)


def _load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {"chats": {}}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("chats", {})
            return data
    except Exception as e:
        logger.warning("🗞️ Weekly: 读取期数状态失败，将使用默认状态: %s", e)
    return {"chats": {}}


def _save_state(state_path: Path, data: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(state_path)


def _next_issue_for_chat(state_path: Path, chat_name: str) -> int:
    with _state_lock:
        state = _load_state(state_path)
        chat_state = state.get("chats", {}).get(chat_name)
        initial_issue = _cfg_int("WEEKLY_INITIAL_ISSUE", 1, minimum=1)
        if isinstance(chat_state, dict) and isinstance(chat_state.get("last_issue"), int):
            return int(chat_state["last_issue"]) + 1
        return initial_issue


def _mark_issue_success(state_path: Path, chat_name: str, issue: int, pdf_path: Path) -> None:
    with _state_lock:
        state = _load_state(state_path)
        chats = state.setdefault("chats", {})
        chats[chat_name] = {
            "last_issue": issue,
            "last_pdf": str(pdf_path),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        failures = state.get("failures")
        if isinstance(failures, dict):
            failures.pop(chat_name, None)
        _save_state(state_path, state)


def _record_weekly_failure(
    state_path: Path,
    chat_name: str,
    issue: int,
    stage: str,
    error: str,
    run_dir: str | None = None,
    triggered_by: str = "schedule",
) -> dict[str, Any]:
    failure = {
        "issue": issue,
        "stage": stage,
        "error": error[:2000],
        "run_dir": run_dir or "",
        "triggered_by": triggered_by,
        "failed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _state_lock:
        state = _load_state(state_path)
        failures = state.setdefault("failures", {})
        failures[chat_name] = failure
        _save_state(state_path, state)
    return failure


def _load_failures(state_path: Path) -> dict[str, dict[str, Any]]:
    with _state_lock:
        state = _load_state(state_path)
        failures = state.get("failures")
        if not isinstance(failures, dict):
            return {}
        return {
            str(chat_name): failure
            for chat_name, failure in failures.items()
            if isinstance(failure, dict)
        }


def _latest_failure_chat(state_path: Path) -> str | None:
    failures = _load_failures(state_path)
    if not failures:
        return None
    return max(
        failures.items(),
        key=lambda item: str(item[1].get("failed_at") or ""),
    )[0]


def _scheduler_run_key_text(run_key: tuple[str, int, int, int]) -> str:
    date_text, weekday, hh, mm = run_key
    return f"{date_text}|{weekday}|{hh:02d}:{mm:02d}"


def _has_scheduler_run(state_path: Path, run_key: tuple[str, int, int, int]) -> bool:
    with _state_lock:
        state = _load_state(state_path)
        runs = state.get("scheduler_runs")
        return isinstance(runs, dict) and _scheduler_run_key_text(run_key) in runs


def _claim_scheduler_run(state_path: Path, run_key: tuple[str, int, int, int]) -> bool:
    """在提交托管任务前登记本次调度，避免等待任务结束期间重复提交。"""

    with _state_lock:
        state = _load_state(state_path)
        runs = state.setdefault("scheduler_runs", {})
        key = _scheduler_run_key_text(run_key)
        if key in runs:
            return False
        runs[key] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if len(runs) > 50:
            for old_key in sorted(runs.keys())[:-50]:
                runs.pop(old_key, None)
        _save_state(state_path, state)
        return True


def _acquire_task_file_lock(lock_path: Path) -> bool:
    stale_seconds = _cfg_int("WEEKLY_TASK_LOCK_STALE_SECONDS", 7200, minimum=600)
    now = time.time()
    try:
        if lock_path.exists():
            try:
                payload = json.loads(lock_path.read_text(encoding="utf-8"))
                started_at = float(payload.get("started_at") or 0)
            except Exception:
                started_at = 0
            age = now - started_at if started_at else stale_seconds + 1
            if age < stale_seconds:
                logger.warning(
                    "🗞️ Weekly: 检测到已有任务锁，跳过本次执行 lock=%s age=%.1fs stale_seconds=%s",
                    lock_path,
                    age,
                    stale_seconds,
                )
                return False
            logger.warning("🗞️ Weekly: 清理过期任务锁 lock=%s age=%.1fs", lock_path, age)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "pid": os.getpid(),
                    "started_at": now,
                    "started_at_text": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        return True
    except FileExistsError:
        logger.warning("🗞️ Weekly: 任务锁竞争失败，跳过本次执行 lock=%s", lock_path)
        return False
    except Exception as e:
        logger.error("🗞️ Weekly: 创建任务锁失败 lock=%s error=%s", lock_path, e, exc_info=True)
        return False


def _release_task_file_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
        logger.info("🗞️ Weekly: 已释放任务锁 lock=%s", lock_path)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("🗞️ Weekly: 释放任务锁失败 lock=%s error=%s", lock_path, e, exc_info=True)


def _build_plan_prompt(
    chat_name: str,
    issue: int,
    page_count: int,
    meta: dict[str, Any],
    logs_text: str,
) -> str:
    issue_label = f"第{issue}期"
    now_bj = datetime.now(pytz.timezone("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S %Z")
    return f"""
你是刘局，也是一本群聊杂志的主编。现在要为群聊「{chat_name}」主编一期只属于这 7 天的群聊杂志。

它不是工作周报、会议纪要、数据简报，也不是把聊天记录换一种顺序复述。你要从真实聊天里找到本周独有的编辑主题、戏剧线索、人物关系、意外转折和余味，把零散聊天编成一期让群友愿意翻完、看完会笑、还能想起当时情景的杂志。
最终会生成固定 {page_count} 张横版 4:3 图片，然后每张作为 PDF 的一页。
当前北京时间：{now_bj}。如果页面中出现年份、日期、统计周期或“本周/今年”的判断，必须以这个当前时间和下面的统计周期为准；不要沿用旧年份、旧截图或历史 run 的日期。

你不是在直接生成图片，而是在担任总编、特稿编辑、设计师和分镜导演。

先在内部完成选题判断，不要输出分析过程：
1. 通读整周，而不是只抓消息最多的一天；找出一个能够解释“这周为什么值得做成一期”的编辑主题。
2. 判断本周的群聊气质和故事走向：它可能热闹、荒诞、松弛、温暖、嘴硬心软、持续跑题，也可能围绕一个小事件不断长出支线。
3. 找出跨天呼应、话题变形、突然反转、被忽略的好问题、无人接住的妙语，以及后来被重新提起的伏笔。
4. 根据素材浓度决定取舍：活跃周要大胆编辑，不做流水账；安静周要放大少数真正有意思的细节，不注水、不假装热闹。
5. 先定本期独有的杂志概念，再决定每页的内容形式、叙事顺序和视觉语言。每一页都要推进这一期，而不是重复首页或换标题复述同一批热门话题。

标题原则：
1. 总标题必须从本周具体内容、反差、悬念、共同情绪或一句有代表性的表达中长出来，每期重新创作。
2. 标题要像真正的杂志封面标题：具体、有画面、有一点好奇心或幽默感；可以机灵，但不要硬凑网络梗和廉价标题党。
3. 禁止把“群聊周报”“本周回顾”“一周总结”“群聊那些事”“本周热点”这类通用词直接当总标题或页面标题。
4. 不要把群名、用户名、昵称机械拼进标题，也不要仅仅因为某个人发言多就把整期做成人物专刊。

内容组织原则：
1. 只有首页承担封面功能；其余页面没有固定栏目。不要默认套用“热点榜、群友金句、活跃人物、数据统计、未完待续”等栏目，只有本周素材真的需要时才使用。
2. 可以采用但不限于：一条话题的漂流史、群聊小剧场、梗的考古、跨天暗线、观点碰撞、人物群像、冷门细节特写、一个问题的多种答案、剧情反转、平行宇宙式编辑想象、下周悬念。它们只是灵感，不是栏目清单，更不能每期照搬。
3. 页面形式可以混合变化，例如封面故事、短特稿、对话切片、视觉时间线、关系图、编辑手记、迷你漫画感场景、趣味预测；具体用什么完全由本周内容决定。
4. 事实信息、群体气氛和趣味表达要自然混合。优先做“共同记忆”，不要做排行榜，不公开比较谁最水、谁最能聊，也不要给群友贴永久标签。
5. 所有页合起来要有开场、展开、变化和收束，但不强迫套用传统起承转合；最后一页也不必固定预测下周，可以用最适合本期主题的方式留下余味。

语言与气质：
1. 整体轻松、鲜活、有熟人感，不要严肃风，不要政务宣传口吻，不要企业汇报腔，不要“凝心聚力、成果丰硕、展望未来”式套话。
2. 可以幽默、毒舌、调侃和轻微荒诞，但笑点必须来自真实语境；不要强行造梗，不恶意评价个人，不替群友虚构动机或心理活动。
3. 编辑文案要短、准、有节奏，像一个了解这个群的朋友在主编杂志，而不是 AI 在做摘要。

硬性规则：
1. 必须生成 exactly {page_count} 页，不多不少。
2. 不要使用固定模板，不要预设固定栏目，不要预设固定视觉风格；本期各页可以因内容而变化，但整期需要有可感知的编辑概念和呼应关系。
3. 不要把用户名、昵称、头像、群名里的词自动当成本周主题。
4. OCR、链接摘要、机器人辅助消息、系统工具消息不是实际聊天内容，除非真人围绕它们展开讨论，否则忽略。
5. 不得编造聊天中没有发生的事件、结论、关系或人物态度；创意表达只能建立在真实素材之上。
6. 如果使用“用户名：聊天内容”的直接引用，聊天内容必须逐字来自原始记录。
7. 如果不是逐字原话，就必须写成编辑口吻，不要伪装成聊天截图。
8. 每页都要适合单独生成一张 4:3 横版图片。
9. 周报期数「{issue_label}」只允许出现在首页。
10. 「编辑：刘局」只允许出现在首页。
11. 第 2 页到第 {page_count} 页不要出现期数、不要出现“编辑：刘局”。
12. 图片模型中文能力有限，所以每页 visible_text 要短而准：每页核心可见中文尽量控制在 60-140 字，不要塞满小字。
13. 不要让 image_prompt 像“本地画图脚本说明”或网页排版模板；它应该是面向真实图像模型的开放式创意 brief，允许模型自由选择画面语言。
14. image_prompt 里不要要求把“Prompt:”“Title:”“Guard:”“可见文字:”等任务标签画到图上；这些标签只是给模型理解任务，不是画面内容。

统计口径：
- 统计周期：{meta.get("start_time")} 至 {meta.get("end_time")}
- 原始记录：{meta.get("raw_count")}
- 入选真人文本：{meta.get("kept_count")}
- 发言人数：{meta.get("unique_senders")}
- 已过滤：{json.dumps(meta.get("excluded", {}), ensure_ascii=False)}

输出严格 JSON，不要 Markdown，不要解释。格式如下：
{{
  "title": "从本周具体内容中生长出来的杂志总标题，禁止通用周报标题",
  "issue": {issue},
  "issue_label": "{issue_label}",
  "period": "统计周期",
  "page_count": {page_count},
  "editorial_concept": {{
    "theme": "本期唯一的编辑主题",
    "cover_hook": "让群友想翻开的封面钩子",
    "tone_keywords": ["根据本周内容确定的气质词"],
    "story_arc": "本期内容如何从开场走向收束",
    "selection_reason": "为什么这一主题只属于本周"
  }},
  "overall_design_intent": "与本期编辑主题一致、但不做正式汇报的整体视觉设计意图",
  "pages": [
    {{
      "page_no": 1,
      "page_role": "这一页在整期杂志里的作用",
      "content_form": "根据本周内容选择的页面形式，不得按固定栏目填写",
      "editorial_angle": "本页相对其他页面独有的观察角度",
      "title": "本页标题",
      "subtitle": "可选的短副标题，没有必要就留空",
      "visible_text": ["本页可见文案"],
      "source_basis": ["支撑本页内容的真实聊天事实或逐字短引，不会直接画到图片上"],
      "layout_intent": "本页排版设计思路",
      "image_prompt": "用于生成这一页图片的完整提示词。必须包含 title、非空 subtitle、visible_text 中全部需要画出的文字，以及本页视觉意图、构图方向和 4:3 横版要求。source_basis 只用于事实校验，不得画出。保持开放式、轻松、有杂志感，不要固化成模板或正式汇报；首页必须包含 {issue_label} 和 编辑：刘局；其他页不得包含期数和编辑署名；不要把提示词标签/任务说明画进图片。"
    }}
  ]
}}

聊天记录：
{logs_text}
""".strip()


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start : end + 1])
        raise


async def _codex_chat_async(prompt: str, *, timeout: int) -> dict[str, Any]:
    return await asyncio.to_thread(_codex_chat, prompt, timeout=timeout)


def _codex_chat(prompt: str, *, timeout: int) -> dict[str, Any]:
    runtime, profile = get_codex_runtime_registry().resolve()
    model = str((profile or {}).get("model") or "").strip()
    if not model:
        raise WeeklyGenerationError("默认 Codex Profile 未配置模型")
    return runtime.run(
        {
            "model": model,
            "timeout": timeout,
            "messages": [{"role": "user", "content": prompt}],
            "extra_body": {
                "reasoning_effort": "high",
                "web_search": True,
            },
        },
        profile_name="weekly",
    )


def _create_weekly_plan(
    chat_name: str,
    issue: int,
    page_count: int,
    records: list[dict[str, str]],
    meta: dict[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    max_log_chars = _cfg_int("WEEKLY_MAX_LOG_CHARS", 360000, minimum=20000)
    prompt = _build_plan_prompt(
        chat_name=chat_name,
        issue=issue,
        page_count=page_count,
        meta=meta,
        logs_text=_format_logs_for_codex(records, max_chars=max_log_chars),
    )
    prompt_path = run_dir / "plan_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    logger.info(
        "🗞️ Weekly: 调用 Codex 生成页计划 chat=%s issue=%s page_count=%s prompt_chars=%s",
        chat_name,
        issue,
        page_count,
        len(prompt),
    )
    timeout = _cfg_int("WEEKLY_CODEX_TIMEOUT_SECONDS", 900, minimum=60)
    response = _codex_chat(prompt, timeout=timeout)
    content = response["choices"][0]["message"].get("content") or ""
    (run_dir / "plan_response.txt").write_text(content, encoding="utf-8")
    plan = _extract_json_object(content)
    plan["page_count"] = page_count
    plan["issue"] = issue
    plan["issue_label"] = f"第{issue}期"
    pages = plan.get("pages")
    if not isinstance(pages, list) or len(pages) != page_count:
        raise WeeklyGenerationError(f"Codex 计划页数异常: expected={page_count}, actual={len(pages) if isinstance(pages, list) else 'N/A'}")
    _normalize_plan_pages(plan, page_count)
    (run_dir / "weekly_page_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("🗞️ Weekly: 页计划生成完成 chat=%s issue=%s pages=%s", chat_name, issue, page_count)
    return plan


def _normalize_plan_pages(plan: dict[str, Any], page_count: int) -> None:
    issue_label = str(plan.get("issue_label") or "")
    for idx, page in enumerate(plan.get("pages") or [], start=1):
        page["page_no"] = idx
        title = str(page.get("title") or f"第{idx}页")
        page["title"] = title
        visible = page.get("visible_text")
        if not isinstance(visible, list):
            visible = [str(visible or title)]
        clean_visible = []
        for item in visible:
            text = str(item or "").strip()
            if not text:
                continue
            if idx != 1 and ("编辑：刘局" in text or "编辑:刘局" in text or issue_label in text):
                continue
            clean_visible.append(text)
        if idx == 1:
            if issue_label and not any(issue_label in item for item in clean_visible):
                clean_visible.insert(0, issue_label)
            if not any("编辑：刘局" in item or "编辑:刘局" in item for item in clean_visible):
                clean_visible.append("编辑：刘局")
        page["visible_text"] = clean_visible

        prompt = str(page.get("image_prompt") or "")
        if idx != 1:
            prompt = prompt.replace("编辑：刘局", "").replace("编辑:刘局", "")
            if issue_label:
                prompt = prompt.replace(issue_label, "")
            prompt += "\n本页不是首页，不要显示周报期数，不要显示“编辑：刘局”。"
        else:
            prompt += f"\n首页必须清晰显示：{issue_label}；编辑：刘局。"
        page["image_prompt"] = prompt.strip()


def _extract_image_attachments(response: dict[str, Any]) -> list[dict[str, Any]]:
    message = response.get("choices", [{}])[0].get("message", {}) if isinstance(response.get("choices"), list) else {}
    attachments = response.get("attachments") or message.get("attachments") or []
    return [
        item for item in attachments
        if isinstance(item, dict) and item.get("type") == "image" and item.get("path")
    ]


def _copy_image_attachment(src: Path, dst: Path) -> Path:
    if not src.exists():
        raise WeeklyGenerationError(f"图片附件不存在: {src}")
    dst = dst.with_suffix(src.suffix.lower() if src.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} else ".png")
    shutil.copy2(src, dst)
    return dst


def _build_batch_image_prompt(plan: dict[str, Any]) -> str:
    pages = sorted(plan["pages"], key=lambda item: int(item.get("page_no") or 0))
    now_bj = datetime.now(pytz.timezone("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S %Z")
    page_blocks = []
    for page in pages:
        page_no = int(page.get("page_no") or 0)
        guard = "首页必须显示周报期数和“编辑：刘局”。" if page_no == 1 else "本页不是首页，不要显示周报期数，不要显示“编辑：刘局”。"
        page_blocks.append(
            f"""
PAGE {page_no:02d}
Title: {page.get("title") or f"第{page_no}页"}
Filename: page_{page_no:02d}.png
Guard: {guard}
Prompt:
{page.get("image_prompt") or ""}
""".strip()
        )
    return f"""
你是刘局，现在要把一期根据本周聊天动态选题的群聊杂志生成成图片。

当前北京时间：{now_bj}。涉及年份、日期或“本周”的画面文字必须以当前时间和计划中的统计周期为准，不要生成 2025 或其他旧年份。

请生成 exactly {len(pages)} 张横版 4:3 周报页面图片，并把最终图片作为本次调用的文件附件输出。

执行方式：
1. 请显式使用 Codex 子代理/并行代理工作流：按页拆分任务，尽量 one subagent per page；如果当前环境的子代理线程上限不足，就按线程上限分批处理。
2. 每个子代理只负责一页图片，最终由主 Codex 等待所有页面完成后统一收齐。
3. 每张图必须是真实图像模型生成的高质量 raster image 文件；不要用 PIL/Canvas/SVG/HTML 截图等本地工具临时拼字排版来冒充成品。如果当前 Codex 环境没有可用的真实图片生成能力，必须明确失败，不要交付占位图或脚本生成图。
4. 严禁复制、重命名、复用项目目录、历史 run、tmp 或 mabowx文件下载中的任何既有图片、微信截图、聊天截图或上次失败产物。
5. 最终附件必须保存为这些精确文件名：{", ".join(f"page_{int(page.get('page_no') or 0):02d}.png" for page in pages)}。
6. 不要额外生成封面、索引、说明图、草稿图或中间图。
7. 如果某一页生成失败，请在本次调用内重试该页；最终回复前必须确认所有 page_XX.png 都存在。
8. 每页保持横版 4:3；中文要尽量清晰，字号大、对比高、不要密密麻麻的小字。
9. 不要添加二维码、水印、无关话题。
10. 只能使用下面每页 prompt 指定的内容，不要自行补充不存在的聊天记录。
11. 保持开放式创作：不要把所有页面做成同一套固定模板；可以根据每页主题自由选择构图、质感、镜头、色彩和视觉语言。
12. 画面里只应出现每页需要读者看到的标题/短句，不要把任务标签或提示词文本画进去，例如不要出现“Prompt:”“Title:”“Guard:”“visible_text”“生成 4:3 横版图片”等字样。
13. 每个 page_XX.png 必须是独立生成的不同页面；不要复用同一张图改名，不要让两页内容或构图重复。
14. 整期应像一本轻松、有个性、会讲故事的独立杂志或 zine，不要做成政务宣传册、企业工作汇报、会议纪要、数据大屏或整齐刻板的信息图模板。
15. 允许各页视觉语言随内容变化；用少量色彩、字体气质或视觉小元素形成整期呼应即可，不要牺牲每页的独特性去追求完全统一。
16. 幽默感应来自页面 prompt 中的真实内容和反差，不要擅自添加网络热梗、鸡汤口号、宏大结论或不存在的人物评价。

本期杂志标题：{plan.get("title") or "本期群聊杂志"}
本期编辑概念：{json.dumps(plan.get("editorial_concept") or {}, ensure_ascii=False)}
整体设计意图：{plan.get("overall_design_intent") or ""}

页面任务：

{chr(10).join(page_blocks)}

完成后只用一句话说明已生成，不要列文件路径。
""".strip()


def _map_batch_attachments_to_pages(image_attachments: list[dict[str, Any]], pages: list[dict[str, Any]], run_dir: Path) -> dict[int, Path]:
    by_page: dict[int, dict[str, Any]] = {}
    unused: list[dict[str, Any]] = []
    for attachment in image_attachments:
        name = str(attachment.get("name") or Path(str(attachment.get("path"))).name)
        match = re.search(r"page[_-]?(\d{1,2})", name, flags=re.IGNORECASE)
        if match:
            page_no = int(match.group(1))
            by_page.setdefault(page_no, attachment)
        else:
            unused.append(attachment)

    results: dict[int, Path] = {}
    sorted_pages = sorted(pages, key=lambda item: int(item.get("page_no") or 0))
    sorted_unused = iter(unused)
    for page in sorted_pages:
        page_no = int(page["page_no"])
        attachment = by_page.get(page_no)
        if attachment is None:
            try:
                attachment = next(sorted_unused)
            except StopIteration:
                continue
        src = Path(str(attachment["path"]))
        dst = _copy_image_attachment(src, run_dir / f"page_{page_no:02d}.png")
        results[page_no] = dst
        logger.info(
            "🗞️ Weekly: 批量生图收集 page=%s file=%s size=%s source=%s",
            page_no,
            dst,
            dst.stat().st_size,
            attachment.get("name") or src.name,
        )
    return results


def _generate_pages_with_codex_subagents(plan: dict[str, Any], run_dir: Path) -> list[Path]:
    pages = sorted(plan["pages"], key=lambda item: int(item.get("page_no") or 0))
    prompt = _build_batch_image_prompt(plan)
    (run_dir / "batch_image_prompt.txt").write_text(prompt, encoding="utf-8")
    timeout = _cfg_int("WEEKLY_IMAGE_BATCH_TIMEOUT_SECONDS", 3600, minimum=300)
    logger.info(
        "🗞️ Weekly: 启动 Codex 子代理批量生图 pages=%s prompt_chars=%s timeout=%ss",
        len(pages),
        len(prompt),
        timeout,
    )
    response = _codex_chat(prompt, timeout=timeout)
    text = response["choices"][0]["message"].get("content") or ""
    (run_dir / "batch_image_response.txt").write_text(text, encoding="utf-8")
    image_attachments = _extract_image_attachments(response)
    logger.info("🗞️ Weekly: Codex 批量生图返回附件 count=%s", len(image_attachments))
    results = _map_batch_attachments_to_pages(image_attachments, pages, run_dir)

    missing_pages = [int(page["page_no"]) for page in pages if int(page["page_no"]) not in results]
    if missing_pages:
        raise WeeklyGenerationError(f"Codex 子代理批量生图缺页: missing={missing_pages}")

    return [results[int(page["page_no"])] for page in pages]


def _validate_generated_pages(image_paths: list[Path], expected_count: int) -> None:
    import hashlib

    if len(image_paths) != expected_count:
        raise WeeklyGenerationError(f"图片数量不匹配: got={len(image_paths)} expected={expected_count}")
    hashes: list[str] = []
    for path in image_paths:
        data = path.read_bytes()
        if len(data) < 20_000:
            raise WeeklyGenerationError(f"图片疑似异常过小: {path} size={len(data)}")
        hashes.append(hashlib.sha256(data).hexdigest())
    if len(set(hashes)) != expected_count:
        duplicate_count = expected_count - len(set(hashes))
        raise WeeklyGenerationError(f"图片存在完全重复文件，拒绝生成 PDF: duplicates={duplicate_count}")


def _generate_pages(plan: dict[str, Any], run_dir: Path) -> list[Path]:
    pages = sorted(plan["pages"], key=lambda item: int(item.get("page_no") or 0))
    logger.info("🗞️ Weekly: 使用 Codex 子代理批量生图模式")
    image_paths = _generate_pages_with_codex_subagents(plan, run_dir)
    _validate_generated_pages(image_paths, len(pages))
    return image_paths


def _fit_to_4_3(im: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    target_w, target_h = target_size
    src = im.convert("RGB")
    src_w, src_h = src.size
    scale = min(target_w / src_w, target_h / src_h)
    new_w = max(1, round(src_w * scale))
    new_h = max(1, round(src_h * scale))
    resized = src.resize((new_w, new_h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (target_w, target_h), (18, 18, 18))
    canvas.paste(resized, ((target_w - new_w) // 2, (target_h - new_h) // 2))
    return canvas


def _make_pdf_under_size(image_paths: list[Path], pdf_path: Path) -> Path:
    max_mb = _cfg_int("WEEKLY_PDF_MAX_MB", 10, minimum=1)
    initial_quality = _cfg_int("WEEKLY_PDF_JPEG_QUALITY", 72, minimum=35, maximum=95)
    opened = [Image.open(path).convert("RGB") for path in image_paths]
    try:
        max_width = max(image.width for image in opened)
    finally:
        for image in opened:
            image.close()

    candidates: list[tuple[tuple[int, int], int]] = []
    base_widths = [max_width, 1448, 1200, 1100, 1024, 900]
    qualities = [initial_quality, 68, 62, 56, 48]
    seen: set[tuple[int, int, int]] = set()
    for width in base_widths:
        width = min(max_width, int(width))
        height = round(width * 3 / 4)
        for quality in qualities:
            key = (width, height, quality)
            if key not in seen:
                seen.add(key)
                candidates.append(((width, height), quality))

    last_size_mb = 0.0
    for size, quality in candidates:
        pages: list[Image.Image] = []
        try:
            for path in image_paths:
                with Image.open(path) as im:
                    pages.append(_fit_to_4_3(im, size))
            first, *rest = pages
            first.save(pdf_path, save_all=True, append_images=rest, quality=quality, resolution=144.0)
        finally:
            for page in pages:
                page.close()
        last_size_mb = pdf_path.stat().st_size / 1024 / 1024
        logger.info(
            "🗞️ Weekly: PDF 压缩尝试 file=%s pages=%s size=%s quality=%s size_mb=%.2f",
            pdf_path,
            len(image_paths),
            size,
            quality,
            last_size_mb,
        )
        if last_size_mb <= max_mb:
            return pdf_path

    raise WeeklyGenerationError(f"PDF 体积仍超过限制: {last_size_mb:.2f}MB > {max_mb}MB")


def _generate_weekly_pdf_for_chat(chat_name: str, active_plugin: WeeklyPlugin | None = None) -> tuple[Path, int]:
    active_plugin = active_plugin or plugin
    if active_plugin is None:
        raise WeeklyGenerationError("Weekly plugin 尚未初始化")

    stage = "prepare"
    run_dir: Path | None = None
    try:
        issue = _next_issue_for_chat(active_plugin.state_path, chat_name)
        issue_label = f"第{issue}期"
        page_count = _cfg_int("WEEKLY_PAGE_COUNT", 8, minimum=1, maximum=20)
        if page_count < 6 or page_count > 10:
            logger.warning("🗞️ Weekly: WEEKLY_PAGE_COUNT=%s 不在建议范围 6-10 内，将按配置继续执行", page_count)

        stage = "extract_logs"
        records, meta = _extract_last_7d_logs(chat_name)
        logger.info(
            "🗞️ Weekly: 日志提取完成 chat=%s issue=%s raw=%s kept=%s senders=%s excluded=%s range=%s~%s",
            chat_name,
            issue,
            meta.get("raw_count"),
            meta.get("kept_count"),
            meta.get("unique_senders"),
            meta.get("excluded"),
            meta.get("start_time"),
            meta.get("end_time"),
        )
        if not records:
            raise WeeklyGenerationError(f"{chat_name} 过去 7 天无可用真人文本记录")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        chat_dir = active_plugin.output_root / _safe_filename(chat_name)
        run_dir = chat_dir / f"issue_{issue:04d}_{timestamp}"
        run_dir.mkdir(parents=True, exist_ok=True)

        stage = "plan"
        plan = _create_weekly_plan(chat_name, issue, page_count, records, meta, run_dir)
        stage = "image_batch"
        image_paths = _generate_pages(plan, run_dir)

        stage = "pdf"
        pdf_name = f"群聊周报_{issue_label}.pdf"
        pdf_path = chat_dir / pdf_name
        _make_pdf_under_size(image_paths, pdf_path)
        logger.info(
            "🗞️ Weekly: PDF 生成完成 chat=%s issue=%s pdf=%s size_mb=%.2f pages=%s",
            chat_name,
            issue,
            pdf_path,
            pdf_path.stat().st_size / 1024 / 1024,
            len(image_paths),
        )
        return pdf_path, issue
    except Exception as e:
        setattr(e, "weekly_stage", stage)
        if run_dir is not None:
            setattr(e, "weekly_run_dir", str(run_dir))
        raise


def _send_file(wx: Any, chat_name: str, file_path: Path) -> bool:
    if hasattr(wx, "send_files"):
        return bool(wx.send_files(chat_name, [str(file_path)]))
    if hasattr(wx, "SendFiles"):
        return bool(wx.SendFiles(str(file_path), chat_name))
    logger.error("🗞️ Weekly: wx 实例缺少 send_files/SendFiles，无法发送 PDF")
    return False


def _send_text(wx: Any, chat_name: str, message: str) -> bool:
    if wx is None:
        return False
    if hasattr(wx, "send_message"):
        try:
            return bool(wx.send_message(chat_name, message, silent=True))
        except TypeError:
            return bool(wx.send_message(chat_name, message))
    if hasattr(wx, "SendMsg"):
        return bool(wx.SendMsg(message, chat_name))
    logger.error("🗞️ Weekly: wx 实例缺少 send_message/SendMsg，无法发送文本")
    return False


def _failure_stage_label(stage: str) -> str:
    labels = {
        "prepare": "准备任务",
        "extract_logs": "读取聊天记录",
        "plan": "生成页计划",
        "image_batch": "Codex 子代理批量生图",
        "pdf": "生成 PDF",
        "send": "发送 PDF",
        "admin_retry": "手动重试",
    }
    return labels.get(stage or "", stage or "未知阶段")


def _admin_chat_name() -> str:
    return _cfg_str("WEEKLY_ADMIN_CHAT", "Gerry") or "Gerry"


def _notify_failure(wx: Any, chat_name: str, issue: int, failure: dict[str, Any]) -> None:
    if not _cfg_bool("WEEKLY_NOTIFY_FAILURE", True):
        return
    admin_chat = _admin_chat_name()
    message = (
        "Weekly 周报生成失败\n\n"
        f"群聊：{chat_name}\n"
        f"期数：第{issue}期\n"
        f"阶段：{_failure_stage_label(str(failure.get('stage') or ''))}\n"
        f"错误：{failure.get('error') or ''}\n"
        f"时间：{failure.get('failed_at') or ''}\n"
        f"目录：{failure.get('run_dir') or '无'}\n\n"
        "可回复：\n"
        "周报重试\n"
        f"周报重试 {chat_name}\n"
        "周报失败\n"
        "周报状态"
    )
    ok = _send_text(wx, admin_chat, message[:3500])
    logger.info("🗞️ Weekly: 失败通知发送 admin=%s chat=%s ok=%s", admin_chat, chat_name, ok)


def _format_failures_for_admin(state_path: Path) -> str:
    failures = _load_failures(state_path)
    if not failures:
        return "当前没有 Weekly 失败记录。"
    lines = ["Weekly 失败记录："]
    for chat_name, failure in sorted(failures.items(), key=lambda item: str(item[1].get("failed_at") or ""), reverse=True):
        issue = failure.get("issue")
        stage = _failure_stage_label(str(failure.get("stage") or ""))
        failed_at = failure.get("failed_at") or ""
        error = str(failure.get("error") or "").replace("\n", " ")
        if len(error) > 180:
            error = error[:180] + "..."
        lines.append(f"- {chat_name}｜第{issue}期｜{stage}｜{failed_at}｜{error}")
    lines.append("")
    lines.append("回复：周报重试 群名")
    return "\n".join(lines)[:3500]


def _format_status_for_admin(active_plugin: WeeklyPlugin) -> str:
    state = _load_state(active_plugin.state_path)
    failures = state.get("failures") if isinstance(state.get("failures"), dict) else {}
    chats = state.get("chats") if isinstance(state.get("chats"), dict) else {}
    lock_exists = active_plugin.task_lock_path.exists()
    execution_busy = _execution_lock.locked()
    lines = [
        "Weekly 状态：",
        f"执行中：{'是' if execution_busy else '否'}",
        f"任务锁：{'存在' if lock_exists else '无'}",
        f"失败记录：{len(failures)} 条",
        f"成功群数：{len(chats)}",
    ]
    if failures:
        lines.append("")
        lines.append(_format_failures_for_admin(active_plugin.state_path))
    return "\n".join(lines)[:3500]


def _admin_help_text() -> str:
    return (
        "Weekly 管理命令：\n"
        "周报失败 - 查看失败记录\n"
        "周报状态 - 查看任务状态\n"
        "周报重试 - 重试最近一次失败的群\n"
        "周报重试 群名 - 重试指定失败群\n"
        "周报帮助 - 查看命令"
    )


def _execute_one_chat_task(
    chat_name: str,
    wx: Any,
    active_plugin: WeeklyPlugin,
    *,
    triggered_by: str,
    notify_admin: bool = True,
) -> bool:
    state_path = active_plugin.state_path
    issue_for_log = _next_issue_for_chat(state_path, chat_name)
    try:
        logger.info("🗞️ Weekly: 开始处理 chat=%s issue=%s triggered_by=%s", chat_name, issue_for_log, triggered_by)
        pdf_path, issue = _generate_weekly_pdf_for_chat(chat_name, active_plugin)
        if wx is None:
            error = WeeklyGenerationError("wx 实例不可用，PDF 已生成但未发送")
            setattr(error, "weekly_stage", "send")
            setattr(error, "weekly_run_dir", str(pdf_path))
            raise error
        if _send_file(wx, chat_name, pdf_path):
            _mark_issue_success(state_path, chat_name, issue, pdf_path)
            logger.info("🗞️ Weekly: 成功推送并更新期数 chat=%s issue=%s", chat_name, issue)
            if triggered_by == "admin_retry":
                _send_text(wx, _admin_chat_name(), f"Weekly 重试成功：{chat_name} 第{issue}期已发送。")
            return True

        stage = "send"
        error = f"PDF 发送失败: {pdf_path}"
        failure = _record_weekly_failure(state_path, chat_name, issue, stage, error, str(pdf_path), triggered_by)
        logger.error("🗞️ Weekly: PDF 发送失败，不更新期数 chat=%s issue=%s pdf=%s", chat_name, issue, pdf_path)
        if notify_admin:
            _notify_failure(wx, chat_name, issue, failure)
        return False
    except Exception as e:
        stage = str(getattr(e, "weekly_stage", "") or "unknown")
        run_dir = str(getattr(e, "weekly_run_dir", "") or "")
        failure = _record_weekly_failure(state_path, chat_name, issue_for_log, stage, str(e), run_dir, triggered_by)
        logger.error("🗞️ Weekly: 处理失败 chat=%s issue=%s error=%s", chat_name, issue_for_log, e, exc_info=True)
        if notify_admin:
            _notify_failure(wx, chat_name, issue_for_log, failure)
        return False


def _parse_admin_command(message: str) -> tuple[str, str | None] | None:
    text = re.sub(r"\s+", " ", (message or "").strip())
    if not text.startswith("周报"):
        return None
    if text in {"周报帮助", "周报 help", "周报 ?"}:
        return "help", None
    if text == "周报失败":
        return "failures", None
    if text == "周报状态":
        return "status", None
    if text == "周报重试":
        return "retry", None
    prefix = "周报重试 "
    if text.startswith(prefix):
        chat_name = text[len(prefix):].strip()
        return ("retry", chat_name) if chat_name else ("retry", None)
    return None


def _start_admin_retry(chat_name: str, wx: Any, active_plugin: WeeklyPlugin) -> bool:
    if _execution_lock.locked():
        _send_text(wx, _admin_chat_name(), "当前已有 Weekly 任务在执行，稍后再试。")
        logger.warning("🗞️ Weekly: 管理员重试被拒绝，已有任务执行 chat=%s", chat_name)
        return False

    task_lock_path = active_plugin.task_lock_path

    def _runner(operation) -> Dict[str, Any]:
        if not _execution_lock.acquire(blocking=False):
            raise WeeklyGenerationError("当前已有 Weekly 任务在执行")
        lock_acquired = False
        try:
            operation.progress(5, f"准备重试 {chat_name}")
            lock_acquired = _acquire_task_file_lock(task_lock_path)
            if not lock_acquired:
                _send_text(wx, _admin_chat_name(), "当前已有 Weekly 任务锁，无法开始重试。")
                return
            _send_text(wx, _admin_chat_name(), f"已开始重试 Weekly：{chat_name}")
            succeeded = _execute_one_chat_task(
                chat_name,
                wx,
                active_plugin,
                triggered_by="admin_retry",
                notify_admin=True,
            )
            if not succeeded:
                raise WeeklyGenerationError(f"{chat_name} 周报重试失败")
            operation.progress(100, "周报重试完成")
            return {"chat_name": chat_name, "succeeded": bool(succeeded)}
        finally:
            if lock_acquired:
                _release_task_file_lock(task_lock_path)
            try:
                _execution_lock.release()
            except Exception:
                pass

    active_plugin.context.tasks.submit(
        "weekly_retry",
        f"Weekly · 重试 {chat_name}",
        _runner,
        details={"chat_name": chat_name, "triggered_by": "admin_retry"},
    )
    logger.info("🗞️ Weekly: 管理员重试托管任务已提交 chat=%s", chat_name)
    return True


def _handle_admin_text(event: Event) -> bool:
    active_plugin = plugin
    if active_plugin is None:
        return False

    admin_chat = _admin_chat_name()
    chat_name = str(event.data.get("chat_name") or "").strip()
    chat_type = str(event.data.get("chat_type") or "").strip()
    message = str(event.data.get("message") or event.data.get("content") or "").strip()
    if chat_name != admin_chat:
        return False
    if chat_type and chat_type != "user":
        return False

    parsed = _parse_admin_command(message)
    if parsed is None:
        return False

    wx = event.context.get("wx")
    command, arg = parsed
    logger.info("🗞️ Weekly: 收到管理员命令 command=%s arg=%s chat=%s", command, arg, chat_name)

    if command == "help":
        _send_text(wx, admin_chat, _admin_help_text())
        return True
    if command == "failures":
        _send_text(wx, admin_chat, _format_failures_for_admin(active_plugin.state_path))
        return True
    if command == "status":
        _send_text(wx, admin_chat, _format_status_for_admin(active_plugin))
        return True
    if command == "retry":
        failures = _load_failures(active_plugin.state_path)
        retry_chat = arg or _latest_failure_chat(active_plugin.state_path)
        if not retry_chat:
            _send_text(wx, admin_chat, "当前没有可重试的 Weekly 失败记录。")
            return True
        if retry_chat not in failures:
            _send_text(wx, admin_chat, f"没有找到该群的失败记录：{retry_chat}")
            return True
        _start_admin_retry(retry_chat, wx, active_plugin)
        return True

    return False


def _execute_weekly_task(event_bus) -> None:
    active_plugin = plugin
    if not active_plugin:
        return
    task_lock_path = active_plugin.task_lock_path
    if not _acquire_task_file_lock(task_lock_path):
        return
    logger.info("🗞️ Weekly: 开始执行每周推送任务")
    try:
        enabled_chats = _get_push_enabled_chats()
        if not enabled_chats:
            logger.info("🗞️ Weekly: 没有群组开启此功能，跳过")
            return

        try:
            wx = event_bus.context.get("wx")
        except Exception:
            wx = None
        if wx is None:
            logger.warning("🗞️ Weekly: wx 实例不可用，本次只会生成文件但无法推送")

        for chat_name in sorted(enabled_chats):
            _execute_one_chat_task(
                chat_name,
                wx,
                active_plugin,
                triggered_by="schedule",
                notify_admin=True,
            )
    finally:
        _release_task_file_lock(task_lock_path)


def _start_weekly_scheduler(event_bus, context) -> None:
    def _runner():
        while _scheduler_started and not context.workers.stop_event.is_set():
            try:
                weekday = _parse_weekday(_cfg_str("WEEKLY_PUSH_WEEKDAY", "MON"))
                hh, mm = _parse_hhmm(_cfg_str("WEEKLY_PUSH_TIME", "09:00"))
                schedule = (weekday, hh, mm)
                target = _next_beijing_weekly_run(weekday, hh, mm)
                now_bj = datetime.now(pytz.timezone("Asia/Shanghai"))
                missed_target = now_bj.replace(hour=hh, minute=mm, second=0, microsecond=0)
                missed_run_key = (missed_target.strftime("%Y-%m-%d"), weekday, hh, mm)
                missed_seconds = (now_bj - missed_target).total_seconds()
                if (
                    now_bj.weekday() == weekday
                    and 0 < missed_seconds <= 300
                    and plugin is not None
                    and not _has_scheduler_run(plugin.state_path, missed_run_key)
                ):
                    target = now_bj
                    logger.info(
                        "🗞️ Weekly: 配置时间刚错过 %.1fs，按测试友好策略立即执行一次 run_key=%s",
                        missed_seconds,
                        missed_run_key,
                    )
                wait = (target - now_bj).total_seconds()
                logger.info(
                    "🗞️ Weekly: 下一次执行时间 target=%s weekday=%s %02d:%02d wait=%.1fs",
                    target.strftime("%Y-%m-%d %H:%M:%S"),
                    weekday + 1,
                    hh,
                    mm,
                    wait,
                )

                while _scheduler_started:
                    new_weekday = _parse_weekday(_cfg_str("WEEKLY_PUSH_WEEKDAY", "MON"))
                    new_hh, new_mm = _parse_hhmm(_cfg_str("WEEKLY_PUSH_TIME", "09:00"))
                    new_schedule = (new_weekday, new_hh, new_mm)
                    if new_schedule != schedule:
                        logger.info(
                            "🗞️ Weekly: 检测到定时配置变化 old=%s new=%s，重新计算下一次执行时间",
                            schedule,
                            new_schedule,
                        )
                        break

                    now_bj = datetime.now(pytz.timezone("Asia/Shanghai"))
                    remaining = (target - now_bj).total_seconds()
                    if remaining <= 0:
                        break
                    sleep_time = min(30.0, max(1.0, remaining))
                    time.sleep(sleep_time)

                if not _scheduler_started:
                    break
                if new_schedule != schedule:
                    continue
                execution_run_key = (target.strftime("%Y-%m-%d"), weekday, hh, mm)
                active_plugin = plugin
                if active_plugin is None:
                    continue
                if not _claim_scheduler_run(active_plugin.state_path, execution_run_key):
                    logger.info("🗞️ Weekly: 本次调度已登记，跳过重复提交 run_key=%s", execution_run_key)
                    continue

                def _managed_push(operation):
                    if not _execution_lock.acquire(blocking=False):
                        logger.warning("🗞️ Weekly: 已有任务执行，跳过重复的托管调度 run_key=%s", execution_run_key)
                        return {"run_key": execution_run_key[0], "skipped": True}
                    try:
                        operation.progress(5, "读取周报推送范围")
                        _execute_weekly_task(event_bus)
                        operation.progress(100, "每周周报推送完成")
                        return {"run_key": execution_run_key[0]}
                    finally:
                        _execution_lock.release()

                active_plugin.context.tasks.submit(
                    "weekly_push",
                    "Weekly · 每周周报推送",
                    _managed_push,
                    details={"run_key": execution_run_key[0]},
                )
            except Exception as e:
                logger.warning("🗞️ Weekly: 定时任务循环异常: %s", e, exc_info=True)

    global _scheduler_started, _scheduler_thread
    if not _scheduler_started:
        with _scheduler_lock:
            if not _scheduler_started:
                _scheduler_started = True
                _scheduler_thread = context.workers.start("weekly-scheduler", _runner)
                logger.info("🗞️ Weekly: scheduler thread started")


def register(event_bus, subscribe, context):
    global plugin
    logger.info("🗞️ 注册 Weekly 插件...")
    plugin = WeeklyPlugin(context)
    subscribe(
        event_type=EventType.TEXT_MESSAGE_RECEIVED,
        handler=plugin.handle_text,
    )
    _start_weekly_scheduler(event_bus, context)
    context.health.register(lambda: {
        "status": "healthy" if plugin is not None and _scheduler_thread is not None and _scheduler_thread.is_alive() else "degraded",
        "message": "周报调度器运行正常" if plugin is not None and _scheduler_thread is not None and _scheduler_thread.is_alive() else "周报调度器未运行",
        "scheduler_alive": bool(_scheduler_thread and _scheduler_thread.is_alive()),
    })
    context.register_cleanup(unregister)
    logger.info("✅ Weekly 插件注册成功")


def unregister():
    global plugin, _scheduler_started
    plugin = None
    _scheduler_started = False
    logger.info("✅ Weekly 插件卸载完成")
