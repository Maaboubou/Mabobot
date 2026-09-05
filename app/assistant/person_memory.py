"""Evidence-ledger Assistant person memory for high-volume group chats.

The event memory answers "what happened".  This module independently answers
"who is this person over time" from raw messages.  Generated profile text is a
materialized view; observations remain the immutable source of truth.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from app.assistant.memory_store import MemoryStore

logger = logging.getLogger(__name__)


PERSON_MEMORY_SCHEMA_VERSION = 3
LIVE_PERSON_SOURCE_NAMESPACE = "live_chat_log_sequence"
OBSERVATION_FIELDS = {
    "identity",
    "group_role",
    "occupation",
    "employer",
    "education",
    "location",
    "family",
    "relationship",
    "health",
    "preference",
    "interest",
    "skill",
    "asset",
    "experience",
    "habit",
    "plan",
    "current_status",
    "other",
}
OBSERVATION_TYPES = {
    "objective_fact",
    "experience",
    "preference",
    "interest",
    "skill",
    "habit",
    "group_role",
    "relationship",
    "status",
    "plan",
}
VOLATILE_FACT_FIELDS = {
    "occupation",
    "employer",
    "location",
    "group_role",
    "plan",
    "current_status",
}
DIRECT_BEHAVIOR_REPETITION_FIELDS = {
    "occupation",
    "employer",
    "location",
    "group_role",
    "relationship",
    "preference",
    "interest",
    "habit",
    "current_status",
}
BOT_PROMPT_FACT_FIELDS = {
    "identity",
    "group_role",
    "occupation",
    "employer",
    "education",
    "location",
    "family",
    "relationship",
    "health",
    "asset",
    "experience",
    "plan",
    "current_status",
}
STABLE_ATTRIBUTE_FIELDS = {
    "preference",
    "interest",
    "habit",
    "group_role",
}
SOURCE_RELATIONS = {
    "self_report",
    "direct_action",
    "attributed_statement",
    "group_interaction",
    "manual_admin",
}
EPISTEMIC_STATUSES = {
    "asserted",
    "uncertain",
    "joke",
    "sarcasm",
    "roleplay",
    "hypothetical",
    "denied",
}
FACT_STATUSES = {
    "current",
    "historical",
    "planned",
    "uncertain",
    "disputed",
}
PATTERN_TYPES = {
    "trait",
    "interest",
    "preference",
    "habit",
    "skill",
    "group_role",
    "communication_style",
}
PATTERN_STATES = {
    "candidate",
    "confirmed",
    "declining",
    "disputed",
}
RELATIONSHIP_TYPES = {
    "family",
    "friend",
    "colleague",
    "group_affinity",
    "group_friction",
    "mentor",
    "collaboration",
    "other",
}
SENSITIVITY_LEVELS = {"low", "medium", "high"}
SENSITIVITY_RANK = {"low": 0, "medium": 1, "high": 2}
SNAPSHOT_SECTIONS = (
    "current_snapshot",
    "timeline",
    "stable_traits",
    "group_relationships",
    "uncertain",
)


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_load(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _clean_text(value: Any, limit: int = 1000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _normalize_text(value: Any) -> str:
    text = _clean_text(value, 2000).casefold()
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def _contains_vague_inference(value: Any) -> bool:
    """Reject conclusions that hedge or infer a durable state from weak cues."""
    return bool(
        re.search(
            r"(?:可能|疑似|推断|暗示|未否认|没有否认|"
            r"居住或工作|工作或居住|常居或工作|工作或常居|"
            r"常住或工作|工作或常住|居住或任职|任职或居住|"
            r"默认其)",
            str(value or ""),
        )
    )


def _is_incidental_location(
    observations: Sequence[Dict[str, Any]],
) -> bool:
    """A delivery/contact address is not evidence of current residence."""
    location_observations = [
        observation
        for observation in observations
        if str(observation.get("field_name") or "") == "location"
    ]
    if not location_observations:
        return False
    return all(
        re.search(
            r"(?:收货地址|快递地址|配送地址|寄件地址|邮寄地址|"
            r"作为收货|用于收货|"
            r"(?:我|本人)?在[一二三四五六七八九十百0-9]+楼|"
            r"地震时|出差|旅游|路过|临时到|开会地点|培训地点|"
            r"参加.{0,20}培训|培训期间|期间住在|入住|酒店|宾馆|旅馆)",
            " ".join(
                [
                    str(observation.get("statement") or ""),
                    *[
                        str(item.get("content") or "")
                        for item in observation.get("evidence_excerpt") or []
                        if isinstance(item, dict)
                    ],
                ]
            ),
        )
        is not None
        for observation in location_observations
    )


def _is_suitable_current_volatile_observation(
    field_name: str,
    observation: Dict[str, Any],
) -> bool:
    """Keep situational work/location remarks from replacing identity facts."""
    statement = str(observation.get("statement") or "")
    # The extracted statement is the atomic claim about this person.  The
    # surrounding excerpt may contain another speaker's occupation/location
    # and must not make an otherwise situational claim look like a durable
    # current state.
    text = statement
    if field_name == "occupation":
        role_signal = re.search(
            r"(?:职业|岗位|任职|入职|从事|专技岗|公务员|事业编|"
            r"教师|老师|医生|护士|律师|工程师|设计师|程序员|会计|"
            r"警察|辅警|销售|司机|厨师|老板|个体户|自由职业|体制内|"
            r"(?:在|到|转到|调至|调到|调入|转入|派驻|去了?).{1,20}工作|"
            r"工作(?:于|在|涉及|内容))",
            text,
        )
        return role_signal is not None
    if field_name == "employer":
        return re.search(
            r"(?:单位|公司|学校|医院|局|镇政府|街道办|委员会|中心|"
            r"事务所|工作室|集团)",
            text,
        ) is not None
    if field_name == "location":
        if _is_incidental_location([observation]):
            return False
        if re.search(
            r"(?:发生|事件|事故|跳楼|曾在|以前在|之前在|"
            r"并提及|以及.+等地)",
            text,
        ):
            return False
        return re.search(
            r"(?:居住|常住|住在|住喺|住系|工作地点|在.{1,16}工作|"
            r"驻点|派驻|单位在|返.{1,12}(?:上班|工作))",
            text,
        ) is not None
    return True


def _materialized_volatile_value(
    field_name: str,
    observation: Dict[str, Any],
) -> str:
    statement = _clean_text(observation.get("statement"), 600)
    if field_name == "occupation":
        for role in (
            "公务员",
            "事业单位工作人员",
            "教师",
            "医生",
            "护士",
            "律师",
            "工程师",
            "设计师",
            "程序员",
            "会计",
            "警察",
            "辅警",
            "销售",
            "司机",
            "厨师",
            "自由职业者",
        ):
            if role in statement:
                return role
    if field_name == "location":
        for clause in re.split(r"[，,；;。]|(?:但是|但|不过|然而)", statement):
            candidate = _clean_text(clause, 240)
            if (
                candidate
                and re.search(
                    r"(?:居住|常住|住在|住喺|住系|"
                    r"在.{1,16}工作|工作地点|驻点|派驻|单位在)",
                    candidate,
                )
                and not _is_incidental_location(
                    [
                        {
                            "statement": candidate,
                            "evidence_excerpt": [],
                        }
                    ]
                )
            ):
                return candidate
    return statement


def _projection_adds_unsupported_identity_term(
    text: Any,
    observations: Sequence[Dict[str, Any]],
) -> bool:
    """Catch high-impact roles/organizations invented during consolidation."""
    value = str(text or "")
    evidence = " ".join(
        [
            str(observation.get("statement") or "")
            for observation in observations
        ]
        + [
            str(item.get("content") or "")
            for observation in observations
            for item in observation.get("evidence_excerpt") or []
            if isinstance(item, dict)
        ]
    )
    guarded_terms = (
        "公务员",
        "事业编",
        "水务局",
        "公安局",
        "教育局",
        "财政局",
        "税务局",
        "政府",
        "委员会",
        "街道办",
        "镇政府",
        "公司",
        "集团",
        "学院",
        "学校",
        "医院",
        "党校",
        "事务所",
        "工作室",
    )
    return any(
        term in value and term not in evidence
        for term in guarded_terms
    )


def _observation_adds_unsupported_specificity(
    field_name: str,
    statement: Any,
    evidence_messages: Sequence[Dict[str, Any]],
) -> bool:
    """Reject invented high-impact identity details before ledger insertion."""
    if field_name in {"education", "skill"} and "证" in str(statement or ""):
        evidence_text = " ".join(
            str(message.get("content") or "")
            for message in evidence_messages
        )
        if "证" not in evidence_text:
            return True
    if field_name not in {
        "occupation",
        "employer",
        "education",
        "location",
        "skill",
    }:
        return False
    return _projection_adds_unsupported_identity_term(
        statement,
        [
            {
                "statement": "",
                "evidence_excerpt": [
                    {"content": str(message.get("content") or "")}
                    for message in evidence_messages
                ],
            }
        ],
    )


def _has_explicit_stable_attribute(
    field_name: str,
    observations: Sequence[Dict[str, Any]],
) -> bool:
    """Return whether a durable attribute was literally self-declared."""
    if any(
        observation.get("source_relation") == "manual_admin"
        for observation in observations
    ):
        return True
    contents = "\n".join(
        str(item.get("content") or "")
        for observation in observations
        if observation.get("source_relation") == "self_report"
        for item in observation.get("evidence_excerpt") or []
        if isinstance(item, dict)
    )
    if field_name == "group_role":
        return bool(
            re.search(
                r"(?:我|本人).{0,12}(?:是群主|是管理员|负责|担任|管理这个群)",
                contents,
            )
        )
    return bool(
        re.search(
            r"(?:我|本人|老子).{0,16}(?:喜欢|钟意|爱好|感兴趣|"
            r"兴趣是|习惯|一向|通常|经常)",
            contents,
        )
    )


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _max_sensitivity(
    requested: Any,
    observations: Sequence[Dict[str, Any]],
) -> str:
    values = [str(requested or "low").strip().lower()]
    values.extend(
        str(observation.get("sensitivity") or "low").strip().lower()
        for observation in observations
    )
    valid = [value for value in values if value in SENSITIVITY_LEVELS]
    return max(valid or ["low"], key=lambda value: SENSITIVITY_RANK[value])


def _is_stale_lifecycle(
    observations: Sequence[Dict[str, Any]],
    *,
    reference_time: Optional[datetime] = None,
    max_age_days: int = 180,
) -> bool:
    if not observations:
        return False
    durabilities = [
        str((observation.get("context") or {}).get("durability") or "")
        .strip()
        .lower()
        for observation in observations
    ]
    if not durabilities or any(
        durability != "lifecycle" for durability in durabilities
    ):
        return False
    observed_times = [
        parsed
        for parsed in (
            _parse_time(
                observation.get("observed_at")
                or observation.get("valid_from")
            )
            for observation in observations
        )
        if parsed is not None
    ]
    if not observed_times:
        return False
    now = reference_time or datetime.now()
    return (now - max(observed_times)).days > max(1, int(max_age_days))


def _is_one_time_fact_value(field_name: str, value: Any) -> bool:
    if field_name not in {
        "experience",
        "education",
        "asset",
        "family",
        "health",
        "other",
        "plan",
        "current_status",
    }:
        return False
    return bool(
        re.search(
            r"(?:收到.{0,20}录取|完成.{0,20}(?:考试|培训|公示|调整)|"
            r"通过.{0,20}(?:考试|评审|政审|体检)|"
            r"(?:将|已将).{0,30}调整|"
            r"(?:提供|乘坐|参加|购买|卖出|获得|考取|入选|宣布).{0,30})",
            str(value or ""),
        )
    )


def _is_semantically_valid_fact_projection(
    field_name: str,
    slot_key: str,
    value: Any,
) -> bool:
    text = str(value or "")
    prefix = str(slot_key or "").split(".", 1)[0]
    if prefix in OBSERVATION_FIELDS and prefix != field_name:
        return False
    if slot_key == "occupation.primary" and not re.search(
        r"(?:公务员|事业编|教师|医生|护士|药师|工程师|律师|会计|"
        r"司机|工人|职员|员工|经理|主管|个体|自由职业|待业|"
        r"退休|学生|从事|任职|工作)",
        text,
    ):
        return False
    if slot_key.startswith("location.") and (
        re.search(
            r"(?:发生|事件|事故|跳楼|提及.+等地|以及.+等地)",
            text,
        )
        or "并提及" in text
    ):
        return False
    return True


def _embedded_volatile_field(field_name: str, value: Any) -> str:
    if field_name == "family" and re.search(
        r"(?:妻子|丈夫|老婆|老公|父亲|母亲|配偶).{0,24}"
        r"(?:在.{0,16}工作|任职|从事|单位|医院|公司)",
        str(value or ""),
    ):
        return "occupation"
    return field_name


def _parse_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        try:
            return datetime.fromisoformat(text[:10]).replace(tzinfo=None)
        except ValueError:
            return None


def _iso_day(value: Any) -> str:
    parsed = _parse_time(value)
    return parsed.date().isoformat() if parsed is not None else ""


class PersonMemoryStore:
    """Persistence and deterministic validation for person memory."""

    SCHEMA_VERSION = 3

    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.ensure_schema()

    @staticmethod
    def now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def ensure_schema(self) -> None:
        with self.store._schema_lock, self.store._connection() as connection:
            schema_row = connection.execute(
                """
                SELECT version FROM memory_schema_meta
                WHERE component = 'person_memory'
                """
            ).fetchone()
            if (
                schema_row is not None
                and int(schema_row["version"] or 0) >= self.SCHEMA_VERSION
            ):
                return
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_person_state (
                    chat_name TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL DEFAULT 3,
                    mode TEXT NOT NULL DEFAULT 'building',
                    source_namespace TEXT NOT NULL DEFAULT 'live_chat_log',
                    observation_source_cursor INTEGER NOT NULL DEFAULT 0,
                    ingestion_cursor INTEGER NOT NULL DEFAULT 0,
                    ingestion_message_count INTEGER NOT NULL DEFAULT 0,
                    active_snapshot_generation INTEGER NOT NULL DEFAULT 0,
                    last_observation_at TEXT,
                    last_consolidation_at TEXT,
                    activated_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_person_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    person_id INTEGER NOT NULL,
                    observation_type TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    normalized_statement TEXT NOT NULL,
                    source_relation TEXT NOT NULL,
                    epistemic_status TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    valid_from TEXT,
                    valid_to TEXT,
                    observed_at TEXT,
                    source_namespace TEXT NOT NULL DEFAULT 'live_chat_log',
                    source_start_cursor INTEGER NOT NULL DEFAULT 0,
                    source_end_cursor INTEGER NOT NULL DEFAULT 0,
                    evidence_cursors_json TEXT NOT NULL DEFAULT '[]',
                    subject_evidence_cursors_json TEXT NOT NULL DEFAULT '[]',
                    evidence_source_ids_json TEXT NOT NULL DEFAULT '[]',
                    evidence_senders_json TEXT NOT NULL DEFAULT '[]',
                    evidence_excerpt_json TEXT NOT NULL DEFAULT '[]',
                    context_json TEXT NOT NULL DEFAULT '{}',
                    sensitivity TEXT NOT NULL DEFAULT 'low',
                    quality_status TEXT NOT NULL DEFAULT 'active',
                    rejection_reason TEXT NOT NULL DEFAULT '',
                    extractor_version TEXT NOT NULL DEFAULT 'person-memory',
                    batch_key TEXT NOT NULL DEFAULT '',
                    fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(chat_name, fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_person_observations_person
                    ON memory_person_observations(
                        chat_name, person_id, quality_status, id
                    );
                CREATE INDEX IF NOT EXISTS idx_person_observations_source
                    ON memory_person_observations(
                        chat_name, source_namespace, source_end_cursor
                    );

                CREATE TABLE IF NOT EXISTS memory_person_fact_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    person_id INTEGER NOT NULL,
                    slot_key TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    value TEXT NOT NULL,
                    normalized_value TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'uncertain',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    valid_from TEXT,
                    valid_to TEXT,
                    observed_at TEXT,
                    evidence_observation_ids_json TEXT NOT NULL DEFAULT '[]',
                    support_count INTEGER NOT NULL DEFAULT 1,
                    independent_day_count INTEGER NOT NULL DEFAULT 1,
                    evidence_span_days INTEGER NOT NULL DEFAULT 0,
                    priority REAL NOT NULL DEFAULT 0.5,
                    sensitivity TEXT NOT NULL DEFAULT 'low',
                    revision INTEGER NOT NULL DEFAULT 1,
                    supersedes_fact_id INTEGER NOT NULL DEFAULT 0,
                    superseded_by_fact_id INTEGER NOT NULL DEFAULT 0,
                    is_current_version INTEGER NOT NULL DEFAULT 1,
                    manual_lock INTEGER NOT NULL DEFAULT 0,
                    deleted_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(chat_name, person_id, slot_key, revision)
                );
                CREATE INDEX IF NOT EXISTS idx_person_fact_versions_person
                    ON memory_person_fact_versions(
                        chat_name, person_id, is_current_version,
                        deleted_at, status, priority
                    );

                CREATE TABLE IF NOT EXISTS memory_person_patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    person_id INTEGER NOT NULL,
                    pattern_type TEXT NOT NULL,
                    label TEXT NOT NULL,
                    normalized_label TEXT NOT NULL,
                    description TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'candidate',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    evidence_observation_ids_json TEXT NOT NULL DEFAULT '[]',
                    support_count INTEGER NOT NULL DEFAULT 1,
                    independent_day_count INTEGER NOT NULL DEFAULT 1,
                    evidence_span_days INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT,
                    last_seen_at TEXT,
                    sensitivity TEXT NOT NULL DEFAULT 'low',
                    manual_lock INTEGER NOT NULL DEFAULT 0,
                    deleted_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(chat_name, person_id, pattern_type, normalized_label)
                );
                CREATE INDEX IF NOT EXISTS idx_person_patterns_person
                    ON memory_person_patterns(
                        chat_name, person_id, state, deleted_at, confidence
                    );

                CREATE TABLE IF NOT EXISTS memory_person_relationships (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    person_id INTEGER NOT NULL,
                    target_person_id INTEGER NOT NULL DEFAULT 0,
                    target_name TEXT NOT NULL,
                    relationship_type TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'uncertain',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    evidence_observation_ids_json TEXT NOT NULL DEFAULT '[]',
                    support_count INTEGER NOT NULL DEFAULT 1,
                    independent_day_count INTEGER NOT NULL DEFAULT 1,
                    evidence_span_days INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT,
                    last_seen_at TEXT,
                    sensitivity TEXT NOT NULL DEFAULT 'low',
                    manual_lock INTEGER NOT NULL DEFAULT 0,
                    deleted_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_person_relationships_person
                    ON memory_person_relationships(
                        chat_name, person_id, status, deleted_at, confidence
                    );

                CREATE TABLE IF NOT EXISTS memory_person_period_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    person_id INTEGER NOT NULL,
                    period_key TEXT NOT NULL,
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    rendered_text TEXT NOT NULL DEFAULT '',
                    evidence_observation_ids_json TEXT NOT NULL DEFAULT '[]',
                    source_observation_max_id INTEGER NOT NULL DEFAULT 0,
                    generator_version TEXT NOT NULL DEFAULT 'person-memory',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(chat_name, person_id, period_key)
                );
                CREATE INDEX IF NOT EXISTS idx_person_period_summaries_person
                    ON memory_person_period_summaries(chat_name, person_id, period_key);

                CREATE TABLE IF NOT EXISTS memory_person_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    person_id INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    sections_json TEXT NOT NULL DEFAULT '{}',
                    rendered_text TEXT NOT NULL DEFAULT '',
                    evidence_observation_ids_json TEXT NOT NULL DEFAULT '[]',
                    source_observation_max_id INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    generator_version TEXT NOT NULL DEFAULT 'person-memory',
                    created_at TEXT NOT NULL,
                    UNIQUE(chat_name, person_id, generation)
                );
                CREATE INDEX IF NOT EXISTS idx_person_snapshots_active
                    ON memory_person_snapshots(chat_name, person_id, is_active);

                CREATE TABLE IF NOT EXISTS memory_person_refresh_state (
                    chat_name TEXT NOT NULL,
                    person_id INTEGER NOT NULL,
                    consolidated_observation_id INTEGER NOT NULL DEFAULT 0,
                    pending_observation_count INTEGER NOT NULL DEFAULT 0,
                    last_observation_at TEXT,
                    last_consolidated_at TEXT,
                    last_period_refresh_at TEXT,
                    last_snapshot_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(chat_name, person_id)
                );

                CREATE TABLE IF NOT EXISTS memory_person_projection_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    person_id INTEGER NOT NULL DEFAULT 0,
                    action TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL,
                    before_json TEXT NOT NULL DEFAULT '{}',
                    after_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    reverted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_person_audit_chat
                    ON memory_person_projection_audit(chat_name, status, id);

                CREATE TABLE IF NOT EXISTS memory_person_suppressions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    person_id INTEGER NOT NULL,
                    target_type TEXT NOT NULL,
                    target_key TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    reverted_at TEXT,
                    UNIQUE(
                        chat_name, person_id, target_type,
                        target_key, status
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_person_suppressions_person
                    ON memory_person_suppressions(
                        chat_name, person_id, target_type, status
                    );

                CREATE TABLE IF NOT EXISTS memory_person_source_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    source_namespace TEXT NOT NULL,
                    source_cursor INTEGER NOT NULL,
                    source_id TEXT NOT NULL DEFAULT '',
                    message_time TEXT NOT NULL DEFAULT '',
                    sender_name TEXT NOT NULL DEFAULT '',
                    sender_external_id TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(chat_name, source_namespace, source_cursor)
                );
                CREATE INDEX IF NOT EXISTS idx_person_source_messages_cursor
                    ON memory_person_source_messages(
                        chat_name, source_namespace, source_cursor
                    );

                CREATE TABLE IF NOT EXISTS memory_person_message_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    person_id INTEGER NOT NULL,
                    source_namespace TEXT NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    relation TEXT NOT NULL,
                    matched_alias TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(
                        chat_name, person_id, source_message_id, relation
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_person_message_links_pending
                    ON memory_person_message_links(
                        chat_name, person_id, source_namespace, id
                    );
                CREATE INDEX IF NOT EXISTS idx_person_message_links_source
                    ON memory_person_message_links(source_message_id);

                CREATE TABLE IF NOT EXISTS memory_person_pipeline_state (
                    chat_name TEXT NOT NULL,
                    person_id INTEGER NOT NULL,
                    source_namespace TEXT NOT NULL,
                    processed_link_id INTEGER NOT NULL DEFAULT 0,
                    pending_link_count INTEGER NOT NULL DEFAULT 0,
                    last_indexed_at TEXT,
                    last_processed_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(chat_name, person_id, source_namespace)
                );
                CREATE INDEX IF NOT EXISTS idx_person_pipeline_due
                    ON memory_person_pipeline_state(
                        chat_name, pending_link_count, last_indexed_at
                    );

                CREATE TABLE IF NOT EXISTS memory_person_claim_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_name TEXT NOT NULL,
                    person_id INTEGER NOT NULL,
                    source_namespace TEXT NOT NULL,
                    batch_key TEXT NOT NULL,
                    candidate_key TEXT NOT NULL,
                    statement TEXT NOT NULL DEFAULT '',
                    candidate_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    verifier_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(chat_name, batch_key, candidate_key)
                );
                CREATE INDEX IF NOT EXISTS idx_person_claim_candidates_status
                    ON memory_person_claim_candidates(
                        chat_name, person_id, status, id
                    );
                """
            )
            self._upgrade_versioned_table_names(connection)
            state_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(memory_person_state)"
                ).fetchall()
            }
            for column, definition in (
                ("ingestion_cursor", "INTEGER NOT NULL DEFAULT 0"),
                ("ingestion_message_count", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in state_columns:
                    connection.execute(
                        f"ALTER TABLE memory_person_state "
                        f"ADD COLUMN {column} {definition}"
                    )
            self.store.set_schema_component_version(
                "person_memory",
                self.SCHEMA_VERSION,
                connection=connection,
            )
            connection.execute(
                "DELETE FROM memory_schema_meta WHERE component = ?",
                ("person_v3",),
            )

    @staticmethod
    def _upgrade_versioned_table_names(connection: Any) -> None:
        """Copy the former version-labelled tables into stable names once."""
        table_names = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        for source, target in (
            ("memory_person_v3_state", "memory_person_state"),
            ("memory_person_v3_audit", "memory_person_projection_audit"),
            ("memory_person_v3_suppressions", "memory_person_suppressions"),
        ):
            if source not in table_names or target not in table_names:
                continue
            source_columns = {
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({source})")
            }
            target_columns = [
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({target})")
            ]
            columns = [column for column in target_columns if column in source_columns]
            if not columns:
                continue
            projection = ",".join(columns)
            connection.execute(
                f"INSERT OR IGNORE INTO {target}({projection}) "
                f"SELECT {projection} FROM {source}"
            )

    def ensure_chat_state(
        self,
        chat_name: str,
        *,
        mode: str = "building",
        source_namespace: str = "live_chat_log",
    ) -> Dict[str, Any]:
        now = self.now()
        normalized_mode = mode if mode in {"building", "active", "disabled"} else "building"
        with self.store._connection() as connection:
            connection.execute(
                """
                INSERT INTO memory_person_state(
                    chat_name, schema_version, mode, source_namespace, updated_at
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(chat_name) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    source_namespace = CASE
                        WHEN memory_person_state.source_namespace = ''
                        THEN excluded.source_namespace
                        ELSE memory_person_state.source_namespace
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_name,
                    PERSON_MEMORY_SCHEMA_VERSION,
                    normalized_mode,
                    source_namespace,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM memory_person_state WHERE chat_name = ?",
                (chat_name,),
            ).fetchone()
        return dict(row) if row is not None else {}

    def get_chat_state(self, chat_name: str) -> Dict[str, Any]:
        with self.store._connection() as connection:
            row = connection.execute(
                "SELECT * FROM memory_person_state WHERE chat_name = ?",
                (chat_name,),
            ).fetchone()
        return dict(row) if row is not None else {}

    def set_ingestion_cursor(
        self,
        chat_name: str,
        *,
        source_cursor: int,
        source_message_count: int,
        monotonic: bool = True,
        source_namespace: Optional[str] = None,
    ) -> Dict[str, Any]:
        namespace = _clean_text(source_namespace, 100)
        self.ensure_chat_state(
            chat_name,
            source_namespace=namespace or "live_chat_log",
        )
        now = self.now()
        assignment = (
            "ingestion_cursor = MAX(ingestion_cursor, ?), "
            "ingestion_message_count = MAX(ingestion_message_count, ?)"
            if monotonic
            else "ingestion_cursor = ?, ingestion_message_count = ?"
        )
        namespace_assignment = (
            ", source_namespace = ?" if namespace else ""
        )
        parameters: List[Any] = [
            max(0, int(source_cursor)),
            max(0, int(source_message_count)),
        ]
        if namespace:
            parameters.append(namespace)
        parameters.extend((now, chat_name))
        with self.store._connection() as connection:
            connection.execute(
                f"""
                UPDATE memory_person_state SET
                    {assignment}{namespace_assignment}, updated_at = ?
                WHERE chat_name = ?
                """,
                parameters,
            )
        return self.get_chat_state(chat_name)

    def set_chat_mode(self, chat_name: str, mode: str) -> Dict[str, Any]:
        if mode not in {"building", "active", "disabled"}:
            raise ValueError(f"invalid person-memory mode: {mode}")
        now = self.now()
        self.ensure_chat_state(chat_name, mode=mode)
        with self.store._connection() as connection:
            connection.execute(
                """
                UPDATE memory_person_state SET
                    mode = ?,
                    activated_at = CASE
                        WHEN ? = 'active' THEN COALESCE(activated_at, ?)
                        ELSE activated_at
                    END,
                    updated_at = ?
                WHERE chat_name = ?
                """,
                (mode, mode, now, now, chat_name),
            )
        return self.get_chat_state(chat_name)

    @staticmethod
    def _mention_alias_matches(
        content: str,
        alias: str,
        *,
        require_address_context: bool = False,
    ) -> bool:
        """Return whether an alias is explicitly present in message text."""
        text = str(content or "")
        value = str(alias or "").strip()
        if not text or len(value) < 2:
            return False
        if require_address_context:
            escaped = re.escape(value)
            patterns = (
                rf"@{escaped}(?=$|[\s\u2005,，。.!！?？:：;；])",
                rf"{escaped}(?:哥|姐|总|老师|桑|酱|王)"
                rf"(?=$|[\s\u2005,，。.!！?？:：;；"
                rf"呢啊呀在又是要会说来去买送发])",
                rf"(?:叫|找|问|喊|请|让|给|等|谢谢)\s*{escaped}"
                rf"(?=$|[\s\u2005,，。.!！?？:：;；])",
                rf"(?:^|[\n。！？!?])\s*{escaped}"
                rf"(?:呢|啊|呀|在|又|是|要|会|说|来|去|买|送|发)",
            )
            return any(
                re.search(pattern, text, flags=re.IGNORECASE)
                is not None
                for pattern in patterns
            )
        if re.fullmatch(r"[\u3400-\u9fff]{2,}", value):
            return value in text
        if len(value) <= 2:
            return (
                f"@{value}" in text
                or re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(value)}"
                    rf"(?![A-Za-z0-9_])",
                    text,
                    flags=re.IGNORECASE,
                )
                is not None
            )
        return value.casefold() in text.casefold()

    def index_person_messages(
        self,
        chat_name: str,
        messages: Sequence[Dict[str, Any]],
        *,
        source_namespace: str,
        core_cursors: Optional[Iterable[int]] = None,
        excluded_sender_names: Optional[Iterable[str]] = None,
        excluded_sender_ids: Optional[Iterable[str]] = None,
        included_person_ids: Optional[Iterable[int]] = None,
    ) -> Dict[str, Any]:
        """Index raw messages once and link authored/mentioned people.

        The source-message table is deliberately separate from observations:
        a low-value decision made today must not permanently erase raw material
        that a later person-centric rebuild may need.
        """

        namespace = _clean_text(source_namespace, 100) or "live_chat_log"
        excluded_names = {
            str(value or "").strip().casefold()
            for value in (excluded_sender_names or [])
            if str(value or "").strip()
        }
        excluded_ids = {
            str(value or "").strip()
            for value in (excluded_sender_ids or [])
            if str(value or "").strip()
        }
        included_ids = (
            {
                _safe_int(value)
                for value in included_person_ids
                if _safe_int(value) > 0
            }
            if included_person_ids is not None
            else None
        )
        ordered = [
            dict(message)
            for message in messages
            if _safe_int(message.get("_log_cursor")) > 0
        ]
        if not ordered:
            return {
                "source_messages": 0,
                "links": 0,
                "authored_links": 0,
                "mention_links": 0,
                "people_touched": [],
            }
        allowed_core = (
            {
                _safe_int(value)
                for value in core_cursors or []
                if _safe_int(value) > 0
            }
            if core_cursors is not None
            else {
                _safe_int(message.get("_log_cursor"))
                for message in ordered
            }
        )
        now = self.now()
        inserted_sources = 0
        authored_links = 0
        mention_links = 0
        touched: set[Tuple[int, str]] = set()
        with self.store._connection() as connection:
            identity_rows = connection.execute(
                """
                SELECT
                    identity.id AS person_id,
                    identity.canonical_name,
                    alias.alias_name,
                    alias.external_id,
                    alias.status AS alias_status,
                    alias.source AS alias_source
                FROM memory_person_identities AS identity
                LEFT JOIN memory_person_aliases AS alias
                  ON alias.person_id = identity.id
                WHERE identity.chat_name = ?
                  AND identity.status = 'active'
                  AND EXISTS(
                    SELECT 1
                    FROM memory_person_aliases AS sender_alias
                    WHERE sender_alias.chat_name = identity.chat_name
                      AND sender_alias.person_id = identity.id
                      AND sender_alias.status = 'confirmed'
                      AND (
                        (
                          sender_alias.external_id != ''
                          AND LOWER(sender_alias.external_id)
                              NOT LIKE '%@chatroom'
                        )
                        OR sender_alias.source IN(
                          'message',
                          'live_message',
                          'historical_message',
                          'historical_sender_id'
                        )
                      )
                  )
                ORDER BY identity.id, alias.id
                """,
                (chat_name,),
            ).fetchall()
            confirmed_names: Dict[str, set[int]] = defaultdict(set)
            external_ids: Dict[str, set[int]] = defaultdict(set)
            mention_names: Dict[str, set[int]] = defaultdict(set)
            display_aliases: Dict[Tuple[str, int], str] = {}
            alias_sources: Dict[Tuple[str, int], str] = {}
            for row in identity_rows:
                person_id = int(row["person_id"])
                canonical = str(row["canonical_name"] or "").strip()
                if canonical:
                    confirmed_names[canonical.casefold()].add(person_id)
                    mention_names[canonical.casefold()].add(person_id)
                    display_aliases[(canonical.casefold(), person_id)] = canonical
                alias_name = str(row["alias_name"] or "").strip()
                alias_status = str(row["alias_status"] or "")
                if alias_name and alias_status in {"confirmed", "suggested"}:
                    mention_names[alias_name.casefold()].add(person_id)
                    display_aliases[
                        (alias_name.casefold(), person_id)
                    ] = alias_name
                    alias_sources[
                        (alias_name.casefold(), person_id)
                    ] = str(row["alias_source"] or "")
                if alias_name and alias_status == "confirmed":
                    confirmed_names[alias_name.casefold()].add(person_id)
                external_id = str(row["external_id"] or "").strip()
                if external_id and alias_status == "confirmed":
                    external_ids[external_id].add(person_id)

            unique_mentions = {
                alias_key: next(iter(person_ids))
                for alias_key, person_ids in mention_names.items()
                if len(person_ids) == 1
                and alias_key
                and alias_key != str(chat_name or "").strip().casefold()
            }

            source_rows: Dict[int, int] = {}
            for message in ordered:
                cursor_value = _safe_int(message.get("_log_cursor"))
                insert = connection.execute(
                    """
                    INSERT OR IGNORE INTO memory_person_source_messages(
                        chat_name, source_namespace, source_cursor, source_id,
                        message_time, sender_name, sender_external_id, content,
                        created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chat_name,
                        namespace,
                        cursor_value,
                        _clean_text(message.get("source_id"), 300),
                        _clean_text(message.get("time"), 60),
                        _clean_text(message.get("sender"), 120),
                        _clean_text(message.get("sender_id"), 200),
                        str(message.get("content") or "")[:12000],
                        now,
                        now,
                    ),
                )
                inserted_sources += max(0, int(insert.rowcount or 0))
                connection.execute(
                    """
                    UPDATE memory_person_source_messages SET
                        source_id = CASE
                            WHEN source_id = '' THEN ? ELSE source_id
                        END,
                        message_time = CASE
                            WHEN message_time = '' THEN ? ELSE message_time
                        END,
                        sender_name = CASE
                            WHEN sender_name = '' THEN ? ELSE sender_name
                        END,
                        sender_external_id = CASE
                            WHEN sender_external_id = '' THEN ?
                            ELSE sender_external_id
                        END,
                        content = CASE
                            WHEN content = '' THEN ? ELSE content
                        END,
                        updated_at = ?
                    WHERE chat_name = ? AND source_namespace = ?
                      AND source_cursor = ?
                    """,
                    (
                        _clean_text(message.get("source_id"), 300),
                        _clean_text(message.get("time"), 60),
                        _clean_text(message.get("sender"), 120),
                        _clean_text(message.get("sender_id"), 200),
                        str(message.get("content") or "")[:12000],
                        now,
                        chat_name,
                        namespace,
                        cursor_value,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT id FROM memory_person_source_messages
                    WHERE chat_name = ? AND source_namespace = ?
                      AND source_cursor = ?
                    """,
                    (chat_name, namespace, cursor_value),
                ).fetchone()
                if row is not None:
                    source_rows[cursor_value] = int(row["id"])

            for message in ordered:
                cursor_value = _safe_int(message.get("_log_cursor"))
                if cursor_value not in allowed_core:
                    continue
                source_message_id = source_rows.get(cursor_value, 0)
                if not source_message_id:
                    continue
                sender_name = str(message.get("sender") or "").strip()
                sender_id = str(message.get("sender_id") or "").strip()
                if (
                    sender_name.casefold() in excluded_names
                    or sender_id in excluded_ids
                    or self.store.is_non_person_sender(
                        chat_name,
                        sender_name,
                        sender_id,
                    )
                ):
                    continue
                author_matches = (
                    external_ids.get(sender_id, set())
                    if sender_id
                    else set()
                )
                if len(author_matches) != 1:
                    author_matches = confirmed_names.get(
                        sender_name.casefold(),
                        set(),
                    )
                author_id = (
                    next(iter(author_matches))
                    if len(author_matches) == 1
                    else 0
                )

                def add_link(
                    person_id: int,
                    relation: str,
                    matched_alias: str,
                ) -> bool:
                    if (
                        included_ids is not None
                        and int(person_id) not in included_ids
                    ):
                        return False
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO memory_person_message_links(
                            chat_name, person_id, source_namespace,
                            source_message_id, relation, matched_alias,
                            created_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chat_name,
                            int(person_id),
                            namespace,
                            source_message_id,
                            relation,
                            matched_alias,
                            now,
                        ),
                    )
                    if int(cursor.rowcount or 0) <= 0:
                        return False
                    touched.add((int(person_id), namespace))
                    connection.execute(
                        """
                        INSERT INTO memory_person_pipeline_state(
                            chat_name, person_id, source_namespace,
                            pending_link_count, last_indexed_at, updated_at
                        ) VALUES(?, ?, ?, 1, ?, ?)
                        ON CONFLICT(
                            chat_name, person_id, source_namespace
                        ) DO UPDATE SET
                            pending_link_count =
                                memory_person_pipeline_state.pending_link_count
                                + 1,
                            last_indexed_at = excluded.last_indexed_at,
                            updated_at = excluded.updated_at
                        """,
                        (
                            chat_name,
                            int(person_id),
                            namespace,
                            now,
                            now,
                        ),
                    )
                    return True

                if author_id and add_link(
                    author_id,
                    "authored",
                    sender_name,
                ):
                    authored_links += 1

                content = str(message.get("content") or "")
                mentioned_people: set[int] = set()
                matched_mentions = []
                for alias_key, person_id in unique_mentions.items():
                    if person_id == author_id:
                        continue
                    alias = display_aliases.get(
                        (alias_key, person_id),
                        alias_key,
                    )
                    if self._mention_alias_matches(
                        content,
                        alias,
                        require_address_context=(
                            alias_sources.get(
                                (alias_key, person_id),
                                "",
                            )
                            == "manual_alias_audit_contextual"
                        ),
                    ):
                        spans = [
                            (match.start(), match.end())
                            for match in re.finditer(
                                re.escape(alias),
                                content,
                                flags=re.IGNORECASE,
                            )
                        ]
                        if spans:
                            matched_mentions.append(
                                {
                                    "person_id": int(person_id),
                                    "alias": alias,
                                    "spans": spans,
                                }
                            )
                # A complete WeChat @ display name is stronger than a short
                # alias embedded inside it.  For example,
                # ``@AAA 专业炒粉画图黄工`` must not also link ``黄工`` to a
                # different nearby speaker.  Prefer the longest owner for
                # each overlapping text span, while still allowing multiple
                # independent mentions in one message.
                matched_mentions.sort(
                    key=lambda item: len(str(item["alias"])),
                    reverse=True,
                )
                accepted_spans: List[Tuple[int, int, int]] = []
                for item in matched_mentions:
                    person_id = int(item["person_id"])
                    if person_id in mentioned_people:
                        continue
                    unshadowed = []
                    for start, end in item["spans"]:
                        shadowed = any(
                            owner_id != person_id
                            and owner_start <= start
                            and owner_end >= end
                            and (owner_end - owner_start) > (end - start)
                            for owner_start, owner_end, owner_id
                            in accepted_spans
                        )
                        if not shadowed:
                            unshadowed.append((start, end))
                    if not unshadowed:
                        continue
                    mentioned_people.add(person_id)
                    accepted_spans.extend(
                        (start, end, person_id)
                        for start, end in unshadowed
                    )
                    if add_link(
                        person_id,
                        "mention",
                        str(item["alias"]),
                    ):
                        mention_links += 1
        return {
            "source_messages": inserted_sources,
            "links": authored_links + mention_links,
            "authored_links": authored_links,
            "mention_links": mention_links,
            "people_touched": sorted(
                {person_id for person_id, _ in touched}
            ),
        }

    def due_indexed_people(
        self,
        chat_name: str,
        *,
        threshold: int = 30,
        stale_after_days: int = 0,
        stale_min_pending: int = 0,
        limit: int = 8,
        force: bool = False,
    ) -> List[Dict[str, Any]]:
        clauses = ["state.chat_name = ?", "state.pending_link_count > 0"]
        params: List[Any] = [chat_name]
        if not force:
            normal_threshold = max(1, int(threshold))
            stale_days = max(0, int(stale_after_days))
            stale_minimum = max(1, int(stale_min_pending or normal_threshold))
            if stale_days > 0:
                stale_cutoff = (
                    datetime.now().astimezone() - timedelta(days=stale_days)
                ).isoformat(timespec="seconds")
                clauses.append(
                    """
                    (
                        state.pending_link_count >= ?
                        OR (
                            state.pending_link_count >= ?
                            AND EXISTS (
                                SELECT 1
                                FROM memory_person_message_links AS pending_link
                                WHERE pending_link.chat_name = state.chat_name
                                  AND pending_link.person_id = state.person_id
                                  AND pending_link.source_namespace = state.source_namespace
                                  AND pending_link.id > state.processed_link_id
                                  AND julianday(pending_link.created_at) <= julianday(?)
                            )
                        )
                    )
                    """
                )
                params.extend(
                    [normal_threshold, stale_minimum, stale_cutoff]
                )
            else:
                clauses.append("state.pending_link_count >= ?")
                params.append(normal_threshold)
        params.append(max(1, min(1000, int(limit))))
        with self.store._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT state.*, identity.canonical_name
                FROM memory_person_pipeline_state AS state
                JOIN memory_person_identities AS identity
                  ON identity.id = state.person_id
                 AND identity.chat_name = state.chat_name
                 AND identity.status = 'active'
                WHERE {' AND '.join(clauses)}
                ORDER BY state.pending_link_count DESC,
                         state.last_indexed_at ASC,
                         state.person_id
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def next_indexed_person_batch(
        self,
        chat_name: str,
        person_id: int,
        source_namespace: str,
        *,
        limit: int = 80,
        context_radius: int = 2,
        after_link_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        namespace = _clean_text(source_namespace, 100)
        with self.store._connection() as connection:
            state = connection.execute(
                """
                SELECT * FROM memory_person_pipeline_state
                WHERE chat_name = ? AND person_id = ?
                  AND source_namespace = ?
                """,
                (chat_name, int(person_id), namespace),
            ).fetchone()
            processed_link_id = (
                max(0, int(after_link_id))
                if after_link_id is not None
                else int(state["processed_link_id"] if state is not None else 0)
            )
            links = connection.execute(
                """
                SELECT
                    link.id AS link_id,
                    link.relation,
                    link.matched_alias,
                    identity.canonical_name,
                    source.*
                FROM memory_person_message_links AS link
                JOIN memory_person_source_messages AS source
                  ON source.id = link.source_message_id
                JOIN memory_person_identities AS identity
                  ON identity.id = link.person_id
                 AND identity.chat_name = link.chat_name
                WHERE link.chat_name = ? AND link.person_id = ?
                  AND link.source_namespace = ? AND link.id > ?
                ORDER BY link.id
                LIMIT ?
                """,
                (
                    chat_name,
                    int(person_id),
                    namespace,
                    processed_link_id,
                    max(1, min(500, int(limit))),
                ),
            ).fetchall()
            if not links:
                return {
                    "person_id": int(person_id),
                    "source_namespace": namespace,
                    "messages": [],
                    "core_cursors": [],
                    "link_ids": [],
                }
            core_cursors = sorted(
                {int(row["source_cursor"]) for row in links}
            )
            radius = max(0, min(8, int(context_radius)))
            context_cursors = sorted(
                {
                    cursor
                    for core_cursor in core_cursors
                    for cursor in range(
                        max(1, core_cursor - radius),
                        core_cursor + radius + 1,
                    )
                }
            )
            source_rows: List[Any] = []
            for start in range(0, len(context_cursors), 800):
                values = context_cursors[start : start + 800]
                placeholders = ",".join("?" for _ in values)
                source_rows.extend(
                    connection.execute(
                        f"""
                        SELECT * FROM memory_person_source_messages
                        WHERE chat_name = ? AND source_namespace = ?
                          AND source_cursor IN({placeholders})
                        ORDER BY source_cursor
                        """,
                        (chat_name, namespace, *values),
                    ).fetchall()
                )
        messages = [
            {
                "_log_cursor": int(row["source_cursor"]),
                "source_id": str(row["source_id"] or ""),
                "time": str(row["message_time"] or ""),
                "sender": str(row["sender_name"] or ""),
                "sender_id": str(row["sender_external_id"] or ""),
                "content": str(row["content"] or ""),
            }
            for row in sorted(
                source_rows,
                key=lambda row: int(row["source_cursor"]),
            )
        ]
        return {
            "person_id": int(person_id),
            "person_name": str(links[0]["canonical_name"] or "")
            if "canonical_name" in links[0].keys()
            else "",
            "source_namespace": namespace,
            "messages": messages,
            "core_cursors": core_cursors,
            "link_ids": [int(row["link_id"]) for row in links],
            "relations": [
                {
                    "link_id": int(row["link_id"]),
                    "cursor": int(row["source_cursor"]),
                    "relation": str(row["relation"] or ""),
                    "matched_alias": str(row["matched_alias"] or ""),
                }
                for row in links
            ],
        }

    def mark_indexed_person_batch_processed(
        self,
        chat_name: str,
        person_id: int,
        source_namespace: str,
        link_ids: Iterable[int],
    ) -> None:
        ids = sorted(
            {
                _safe_int(value)
                for value in link_ids
                if _safe_int(value) > 0
            }
        )
        if not ids:
            return
        namespace = _clean_text(source_namespace, 100)
        now = self.now()
        max_link_id = max(ids)
        with self.store._connection() as connection:
            remaining = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS value
                    FROM memory_person_message_links
                    WHERE chat_name = ? AND person_id = ?
                      AND source_namespace = ? AND id > ?
                    """,
                    (
                        chat_name,
                        int(person_id),
                        namespace,
                        max_link_id,
                    ),
                ).fetchone()["value"]
            )
            connection.execute(
                """
                INSERT INTO memory_person_pipeline_state(
                    chat_name, person_id, source_namespace,
                    processed_link_id, pending_link_count,
                    last_processed_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    chat_name, person_id, source_namespace
                ) DO UPDATE SET
                    processed_link_id = MAX(
                        memory_person_pipeline_state.processed_link_id,
                        excluded.processed_link_id
                    ),
                    pending_link_count = excluded.pending_link_count,
                    last_processed_at = excluded.last_processed_at,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_name,
                    int(person_id),
                    namespace,
                    max_link_id,
                    remaining,
                    now,
                    now,
                ),
            )

    def record_claim_candidates(
        self,
        chat_name: str,
        person_id: int,
        source_namespace: str,
        batch_key: str,
        candidates: Sequence[Dict[str, Any]],
    ) -> None:
        if not batch_key or not candidates:
            return
        now = self.now()
        with self.store._connection() as connection:
            for index, candidate in enumerate(candidates, start=1):
                if not isinstance(candidate, dict):
                    continue
                candidate_key = str(
                    candidate.get("candidate_id") or f"c{index}"
                )
                connection.execute(
                    """
                    INSERT INTO memory_person_claim_candidates(
                        chat_name, person_id, source_namespace, batch_key,
                        candidate_key, statement, candidate_json, status,
                        verifier_json, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', '{}', ?, ?)
                    ON CONFLICT(chat_name, batch_key, candidate_key)
                    DO UPDATE SET
                        person_id = excluded.person_id,
                        source_namespace = excluded.source_namespace,
                        statement = excluded.statement,
                        candidate_json = excluded.candidate_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        chat_name,
                        int(person_id),
                        _clean_text(source_namespace, 100),
                        _clean_text(batch_key, 240),
                        _clean_text(candidate_key, 80),
                        _clean_text(candidate.get("statement"), 1000),
                        _json_dump(candidate),
                        now,
                        now,
                    ),
                )

    def update_claim_candidate_results(
        self,
        chat_name: str,
        batch_key: str,
        results: Dict[str, Dict[str, Any]],
    ) -> None:
        if not batch_key:
            return
        now = self.now()
        with self.store._connection() as connection:
            rows = connection.execute(
                """
                SELECT candidate_key
                FROM memory_person_claim_candidates
                WHERE chat_name = ? AND batch_key = ?
                """,
                (chat_name, batch_key),
            ).fetchall()
            for row in rows:
                candidate_key = str(row["candidate_key"])
                result = dict(results.get(candidate_key) or {})
                status = str(result.get("status") or "rejected")
                if status not in {
                    "pending",
                    "verified",
                    "quarantined",
                    "rejected",
                }:
                    status = "rejected"
                connection.execute(
                    """
                    UPDATE memory_person_claim_candidates SET
                        status = ?, verifier_json = ?, updated_at = ?
                    WHERE chat_name = ? AND batch_key = ?
                      AND candidate_key = ?
                    """,
                    (
                        status,
                        _json_dump(result),
                        now,
                        chat_name,
                        batch_key,
                        candidate_key,
                    ),
                )

    def candidate_stats(self, chat_name: str) -> Dict[str, int]:
        with self.store._connection() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS value
                FROM memory_person_claim_candidates
                WHERE chat_name = ?
                GROUP BY status
                """,
                (chat_name,),
            ).fetchall()
        values = {
            str(row["status"]): int(row["value"])
            for row in rows
        }
        return {
            "total": sum(values.values()),
            "pending": values.get("pending", 0),
            "verified": values.get("verified", 0),
            "quarantined": values.get("quarantined", 0),
            "rejected": values.get("rejected", 0),
        }

    def pipeline_stats(self, chat_name: str) -> Dict[str, int]:
        with self.store._connection() as connection:
            source_messages = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS value
                    FROM memory_person_source_messages
                    WHERE chat_name = ?
                    """,
                    (chat_name,),
                ).fetchone()["value"]
            )
            links = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS value
                    FROM memory_person_message_links
                    WHERE chat_name = ?
                    """,
                    (chat_name,),
                ).fetchone()["value"]
            )
            state = connection.execute(
                """
                SELECT
                    COALESCE(SUM(pending_link_count), 0) AS pending,
                    SUM(CASE WHEN pending_link_count > 0 THEN 1 ELSE 0 END)
                        AS pending_people
                FROM memory_person_pipeline_state
                WHERE chat_name = ?
                """,
                (chat_name,),
            ).fetchone()
        return {
            "source_messages": source_messages,
            "links": links,
            "pending_links": int(state["pending"] or 0),
            "pending_people": int(state["pending_people"] or 0),
        }

    def _observation_from_row(self, row: Any) -> Dict[str, Any]:
        value = dict(row)
        for key in (
            "evidence_cursors",
            "subject_evidence_cursors",
            "evidence_source_ids",
            "evidence_senders",
            "evidence_excerpt",
        ):
            value[key] = _json_load(value.pop(f"{key}_json", "[]"), [])
        value["context"] = _json_load(value.pop("context_json", "{}"), {})
        return value

    def add_observations(
        self,
        chat_name: str,
        observations: Iterable[Dict[str, Any]],
        *,
        source_namespace: str = "live_chat_log",
        batch_key: str = "",
    ) -> Dict[str, Any]:
        now = self.now()
        inserted_ids: List[int] = []
        person_counts: Dict[int, int] = defaultdict(int)
        max_cursor = 0
        with self.store._connection() as connection:
            for raw in observations:
                person_id = _safe_int(raw.get("person_id"))
                statement = _clean_text(raw.get("statement"), 1000)
                normalized = _normalize_text(statement)
                evidence_cursors = sorted(
                    {
                        _safe_int(value)
                        for value in raw.get("evidence_cursors") or []
                        if _safe_int(value) > 0
                    }
                )
                if not person_id or not normalized or not evidence_cursors:
                    continue
                subject_cursors = sorted(
                    {
                        _safe_int(value)
                        for value in raw.get("subject_evidence_cursors") or []
                        if _safe_int(value) in evidence_cursors
                    }
                )
                fingerprint_material = "|".join(
                    (
                        str(person_id),
                        str(raw.get("observation_type") or "objective_fact"),
                        str(raw.get("field_name") or "other"),
                        normalized,
                        str(source_namespace),
                        ",".join(str(value) for value in evidence_cursors),
                    )
                )
                fingerprint = hashlib.sha256(
                    fingerprint_material.encode("utf-8")
                ).hexdigest()
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO memory_person_observations(
                        chat_name, person_id, observation_type, field_name,
                        statement, normalized_statement, source_relation,
                        epistemic_status, confidence, valid_from, valid_to,
                        observed_at, source_namespace, source_start_cursor,
                        source_end_cursor, evidence_cursors_json,
                        subject_evidence_cursors_json, evidence_source_ids_json,
                        evidence_senders_json, evidence_excerpt_json,
                        context_json, sensitivity, quality_status,
                        rejection_reason, extractor_version, batch_key,
                        fingerprint, created_at, updated_at
                    ) VALUES(
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        chat_name,
                        person_id,
                        raw.get("observation_type") or "objective_fact",
                        raw.get("field_name") or "other",
                        statement,
                        normalized,
                        raw.get("source_relation") or "attributed_statement",
                        raw.get("epistemic_status") or "uncertain",
                        _safe_float(raw.get("confidence")),
                        str(raw.get("valid_from") or "") or None,
                        str(raw.get("valid_to") or "") or None,
                        str(raw.get("observed_at") or "") or None,
                        source_namespace,
                        min(evidence_cursors),
                        max(evidence_cursors),
                        _json_dump(evidence_cursors),
                        _json_dump(subject_cursors),
                        _json_dump(raw.get("evidence_source_ids") or []),
                        _json_dump(raw.get("evidence_senders") or []),
                        _json_dump(raw.get("evidence_excerpt") or []),
                        _json_dump(raw.get("context") or {}),
                        raw.get("sensitivity") or "low",
                        raw.get("quality_status") or "active",
                        _clean_text(raw.get("rejection_reason"), 500),
                        raw.get("extractor_version") or "person-memory",
                        batch_key or str(raw.get("batch_key") or ""),
                        fingerprint,
                        now,
                        now,
                    ),
                )
                if int(cursor.rowcount or 0) <= 0:
                    continue
                observation_id = int(cursor.lastrowid)
                inserted_ids.append(observation_id)
                person_counts[person_id] += 1
                max_cursor = max(max_cursor, max(evidence_cursors))
                connection.execute(
                    """
                    INSERT INTO memory_person_refresh_state(
                        chat_name, person_id, pending_observation_count,
                        last_observation_at, updated_at
                    ) VALUES(?, ?, 1, ?, ?)
                    ON CONFLICT(chat_name, person_id) DO UPDATE SET
                        pending_observation_count =
                            memory_person_refresh_state.pending_observation_count + 1,
                        last_observation_at = excluded.last_observation_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        chat_name,
                        person_id,
                        str(raw.get("observed_at") or now),
                        now,
                    ),
                )
            if max_cursor:
                connection.execute(
                    """
                    INSERT INTO memory_person_state(
                        chat_name, schema_version, mode, source_namespace,
                        observation_source_cursor, last_observation_at, updated_at
                    ) VALUES(?, ?, 'building', ?, ?, ?, ?)
                    ON CONFLICT(chat_name) DO UPDATE SET
                        observation_source_cursor = MAX(
                            memory_person_state.observation_source_cursor,
                            excluded.observation_source_cursor
                        ),
                        last_observation_at = excluded.last_observation_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        chat_name,
                        PERSON_MEMORY_SCHEMA_VERSION,
                        source_namespace,
                        max_cursor,
                        now,
                        now,
                    ),
                )
        return {
            "inserted": len(inserted_ids),
            "observation_ids": inserted_ids,
            "person_counts": dict(person_counts),
            "max_cursor": max_cursor,
        }

    def list_observations(
        self,
        chat_name: str,
        *,
        person_id: int = 0,
        after_id: int = 0,
        quality_status: str = "active",
        limit: int = 500,
        descending: bool = False,
    ) -> List[Dict[str, Any]]:
        clauses = ["chat_name = ?", "id > ?"]
        params: List[Any] = [chat_name, max(0, int(after_id))]
        if person_id:
            clauses.append("person_id = ?")
            params.append(int(person_id))
        if quality_status:
            clauses.append("quality_status = ?")
            params.append(quality_status)
        order = "DESC" if descending else "ASC"
        params.append(max(1, min(10000, int(limit))))
        with self.store._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM memory_person_observations
                WHERE {' AND '.join(clauses)}
                ORDER BY id {order} LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._observation_from_row(row) for row in rows]

    def get_observation(
        self,
        chat_name: str,
        observation_id: int,
    ) -> Optional[Dict[str, Any]]:
        with self.store._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_person_observations
                WHERE chat_name = ? AND id = ?
                """,
                (chat_name, int(observation_id)),
            ).fetchone()
        return self._observation_from_row(row) if row is not None else None

    def observation_stats(self, chat_name: str) -> Dict[str, int]:
        with self.store._connection() as connection:
            rows = connection.execute(
                """
                SELECT quality_status, COUNT(*) AS value
                FROM memory_person_observations
                WHERE chat_name = ?
                GROUP BY quality_status
                """,
                (chat_name,),
            ).fetchall()
        values = {str(row["quality_status"]): int(row["value"]) for row in rows}
        return {
            "total": sum(values.values()),
            "active": values.get("active", 0),
            "quarantined": values.get("quarantined", 0),
            "rejected": values.get("rejected", 0),
        }

    def due_people(
        self,
        chat_name: str,
        *,
        threshold: int = 10,
        stale_after_days: int = 0,
        force: bool = False,
        limit: int = 8,
    ) -> List[Dict[str, Any]]:
        clauses = ["state.chat_name = ?", "state.pending_observation_count > 0"]
        params: List[Any] = [chat_name]
        if not force:
            normal_threshold = max(1, int(threshold))
            stale_days = max(0, int(stale_after_days))
            if stale_days > 0:
                stale_cutoff = (
                    datetime.now().astimezone() - timedelta(days=stale_days)
                ).isoformat(timespec="seconds")
                clauses.append(
                    """
                    (
                        state.pending_observation_count >= ?
                        OR julianday(state.last_observation_at) <= julianday(?)
                    )
                    """
                )
                params.extend([normal_threshold, stale_cutoff])
            else:
                clauses.append("state.pending_observation_count >= ?")
                params.append(normal_threshold)
        params.append(max(1, min(1000, int(limit))))
        with self.store._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT state.*, identity.canonical_name
                FROM memory_person_refresh_state AS state
                JOIN memory_person_identities AS identity
                  ON identity.id = state.person_id
                 AND identity.chat_name = state.chat_name
                 AND identity.status = 'active'
                WHERE {' AND '.join(clauses)}
                ORDER BY state.pending_observation_count DESC,
                         state.last_observation_at ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_current_facts(
        self,
        chat_name: str,
        person_id: int,
        *,
        include_uncertain: bool = True,
    ) -> List[Dict[str, Any]]:
        uncertain_clause = "" if include_uncertain else (
            "AND status NOT IN('uncertain', 'disputed')"
        )
        with self.store._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM memory_person_fact_versions
                WHERE chat_name = ? AND person_id = ?
                  AND is_current_version = 1 AND deleted_at IS NULL
                  {uncertain_clause}
                ORDER BY priority DESC, observed_at DESC, id DESC
                """,
                (chat_name, int(person_id)),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["evidence_observation_ids"] = _json_load(
                value.pop("evidence_observation_ids_json", "[]"),
                [],
            )
            result.append(value)
        return result

    def list_patterns(
        self,
        chat_name: str,
        person_id: int,
        *,
        include_candidates: bool = True,
    ) -> List[Dict[str, Any]]:
        state_clause = "" if include_candidates else "AND state = 'confirmed'"
        with self.store._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM memory_person_patterns
                WHERE chat_name = ? AND person_id = ? AND deleted_at IS NULL
                  {state_clause}
                ORDER BY CASE state WHEN 'confirmed' THEN 0 ELSE 1 END,
                         confidence DESC, support_count DESC, id DESC
                """,
                (chat_name, int(person_id)),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["evidence_observation_ids"] = _json_load(
                value.pop("evidence_observation_ids_json", "[]"),
                [],
            )
            result.append(value)
        return result

    def list_relationships(
        self,
        chat_name: str,
        person_id: int,
        *,
        include_uncertain: bool = True,
    ) -> List[Dict[str, Any]]:
        status_clause = "" if include_uncertain else (
            "AND status NOT IN('uncertain', 'disputed')"
        )
        with self.store._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM memory_person_relationships
                WHERE chat_name = ? AND person_id = ? AND deleted_at IS NULL
                  {status_clause}
                ORDER BY confidence DESC, support_count DESC, id DESC
                """,
                (chat_name, int(person_id)),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["evidence_observation_ids"] = _json_load(
                value.pop("evidence_observation_ids_json", "[]"),
                [],
            )
            result.append(value)
        return result

    def upsert_period_summary(
        self,
        chat_name: str,
        person_id: int,
        period_key: str,
        summary: Dict[str, Any],
        *,
        evidence_observation_ids: Iterable[int],
        source_observation_max_id: int,
        generator_version: str = "person-memory",
    ) -> int:
        ids = sorted(
            {
                _safe_int(value)
                for value in evidence_observation_ids
                if _safe_int(value) > 0
            }
        )
        with self.store._connection() as connection:
            observations = self._verified_observations(
                connection,
                chat_name,
                person_id,
                ids,
            )
            verified_ids = [int(item["id"]) for item in observations]
            if not verified_ids:
                raise ValueError("period summary has no verified observations")
            rendered = _clean_text(
                summary.get("summary") if isinstance(summary, dict) else "",
                5000,
            )
            if not rendered:
                rendered = _json_dump(summary)
            now = self.now()
            cursor = connection.execute(
                """
                INSERT INTO memory_person_period_summaries(
                    chat_name, person_id, period_key, summary_json,
                    rendered_text, evidence_observation_ids_json,
                    source_observation_max_id, generator_version,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_name, person_id, period_key) DO UPDATE SET
                    summary_json = excluded.summary_json,
                    rendered_text = excluded.rendered_text,
                    evidence_observation_ids_json =
                        excluded.evidence_observation_ids_json,
                    source_observation_max_id =
                        excluded.source_observation_max_id,
                    generator_version = excluded.generator_version,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_name,
                    int(person_id),
                    _clean_text(period_key, 80),
                    _json_dump(summary),
                    rendered,
                    _json_dump(verified_ids),
                    int(source_observation_max_id),
                    generator_version,
                    now,
                    now,
                ),
            )
            if int(cursor.lastrowid or 0):
                return int(cursor.lastrowid)
            row = connection.execute(
                """
                SELECT id FROM memory_person_period_summaries
                WHERE chat_name = ? AND person_id = ? AND period_key = ?
                """,
                (chat_name, int(person_id), _clean_text(period_key, 80)),
            ).fetchone()
        return int(row["id"]) if row is not None else 0

    def list_period_summaries(
        self,
        chat_name: str,
        person_id: int,
    ) -> List[Dict[str, Any]]:
        with self.store._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM memory_person_period_summaries
                WHERE chat_name = ? AND person_id = ?
                ORDER BY period_key, id
                """,
                (chat_name, int(person_id)),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["summary"] = _json_load(value.pop("summary_json", "{}"), {})
            value["evidence_observation_ids"] = _json_load(
                value.pop("evidence_observation_ids_json", "[]"),
                [],
            )
            result.append(value)
        return result

    def _verified_observations(
        self,
        connection: Any,
        chat_name: str,
        person_id: int,
        observation_ids: Iterable[Any],
    ) -> List[Dict[str, Any]]:
        ids = sorted({_safe_int(value) for value in observation_ids if _safe_int(value) > 0})
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"""
            SELECT * FROM memory_person_observations
            WHERE chat_name = ? AND person_id = ?
              AND quality_status = 'active' AND id IN({placeholders})
            ORDER BY id
            """,
            (chat_name, int(person_id), *ids),
        ).fetchall()
        return [self._observation_from_row(row) for row in rows]

    @staticmethod
    def _support_metrics(
        observations: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        days = sorted(
            {
                day
                for observation in observations
                for day in (
                    _iso_day(
                        observation.get("observed_at")
                        or observation.get("valid_from")
                    ),
                )
                if day
            }
        )
        span_days = 0
        if len(days) >= 2:
            first = _parse_time(days[0])
            last = _parse_time(days[-1])
            if first is not None and last is not None:
                span_days = max(0, (last - first).days)
        return {
            "support_count": len(observations),
            "independent_day_count": max(1, len(days)),
            "evidence_span_days": span_days,
            "first_seen_at": days[0] if days else "",
            "last_seen_at": days[-1] if days else "",
        }

    def _has_newer_field_observation(
        self,
        connection: Any,
        chat_name: str,
        person_id: int,
        field_name: str,
        observations: Sequence[Dict[str, Any]],
    ) -> bool:
        if field_name not in VOLATILE_FACT_FIELDS or not observations:
            return False
        evidence_times = [
            parsed
            for parsed in (
                _parse_time(
                    observation.get("observed_at")
                    or observation.get("valid_from")
                )
                for observation in observations
                if observation.get("field_name") == field_name
            )
            if parsed is not None
        ]
        if not evidence_times:
            return False
        rows = connection.execute(
            """
            SELECT *
            FROM memory_person_observations
            WHERE chat_name = ? AND person_id = ?
              AND field_name = ? AND quality_status = 'active'
            ORDER BY COALESCE(observed_at, valid_from, '') DESC, id DESC
            """,
            (chat_name, int(person_id), field_name),
        ).fetchall()
        latest_observation = next(
            (
                observation
                for observation in (
                    self._observation_from_row(row)
                    for row in rows
                )
                if _is_suitable_current_volatile_observation(
                    field_name,
                    observation,
                )
            ),
            None,
        )
        if latest_observation is None:
            return False
        latest = _parse_time(
            latest_observation.get("observed_at")
            or latest_observation.get("valid_from")
        )
        return latest is not None and latest > max(evidence_times)

    @staticmethod
    def _is_stale_volatile_field(
        connection: Any,
        chat_name: str,
        field_name: str,
        observations: Sequence[Dict[str, Any]],
        *,
        max_age_days: int = 365,
    ) -> bool:
        if field_name not in VOLATILE_FACT_FIELDS or not observations:
            return False
        if any(
            observation.get("source_relation") == "manual_admin"
            for observation in observations
        ):
            return False
        evidence_times = [
            parsed
            for parsed in (
                _parse_time(
                    observation.get("observed_at")
                    or observation.get("valid_from")
                )
                for observation in observations
            )
            if parsed is not None
        ]
        if not evidence_times:
            return False
        rows = connection.execute(
            """
            SELECT observed_at, valid_from
            FROM memory_person_observations
            WHERE chat_name = ? AND quality_status = 'active'
            """,
            (chat_name,),
        ).fetchall()
        reference_times = [
            parsed
            for parsed in (
                _parse_time(row["observed_at"] or row["valid_from"])
                for row in rows
            )
            if parsed is not None
        ]
        reference_time = max(reference_times or [datetime.now()])
        return (
            reference_time - max(evidence_times)
        ).days > max(1, int(max_age_days))

    def _upsert_fact_projection(
        self,
        connection: Any,
        chat_name: str,
        person_id: int,
        raw: Dict[str, Any],
        now: str,
    ) -> Optional[int]:
        value = _clean_text(raw.get("value"), 600)
        normalized = _normalize_text(value)
        field_name = str(raw.get("field") or raw.get("field_name") or "other").strip().lower()
        if field_name not in OBSERVATION_FIELDS:
            field_name = "other"
        slot_key = _clean_text(raw.get("slot_key"), 120).casefold()
        if not slot_key:
            slot_key = f"{field_name}:{normalized[:80]}"
        if not _is_semantically_valid_fact_projection(
            field_name,
            slot_key,
            value,
        ):
            return None
        observations = self._verified_observations(
            connection,
            chat_name,
            person_id,
            raw.get("evidence_observation_ids") or [],
        )
        if not value or not normalized or not observations:
            return None
        suppressed = connection.execute(
            """
            SELECT 1 FROM memory_person_suppressions
            WHERE chat_name = ? AND person_id = ?
              AND target_type = 'fact' AND target_key = ?
              AND status = 'active'
            LIMIT 1
            """,
            (
                chat_name,
                int(person_id),
                f"{slot_key}|{normalized}",
            ),
        ).fetchone()
        if suppressed is not None:
            return None
        metrics = self._support_metrics(observations)
        status = str(raw.get("status") or "uncertain").strip().lower()
        if status not in FACT_STATUSES:
            status = "uncertain"
        if status == "current" and field_name in {"experience", "plan"}:
            status = "historical"
        if (
            status in {"current", "planned"}
            and (
                field_name not in VOLATILE_FACT_FIELDS
                or field_name == "current_status"
            )
            and _is_stale_lifecycle(observations)
        ):
            status = "historical"
        if status in {"current", "planned"} and self._has_newer_field_observation(
            connection,
            chat_name,
            person_id,
            field_name,
            observations,
        ):
            status = "historical"
        if (
            status in {"current", "planned"}
            and self._is_stale_volatile_field(
                connection,
                chat_name,
                _embedded_volatile_field(field_name, value),
                observations,
            )
        ):
            status = "historical"
        if (
            status in {"current", "planned"}
            and field_name == "location"
            and _is_incidental_location(observations)
        ):
            status = "historical"
        if status == "current" and _is_one_time_fact_value(
            field_name,
            value,
        ):
            status = "historical"
        if (
            status == "current"
            and field_name in STABLE_ATTRIBUTE_FIELDS
            and (
                metrics["independent_day_count"] < 3
                or metrics["evidence_span_days"] < 30
            )
            and not _has_explicit_stable_attribute(
                field_name,
                observations,
            )
        ):
            status = "uncertain"
        confidence = _safe_float(raw.get("confidence"))
        if any(
            observation.get("epistemic_status") != "asserted"
            for observation in observations
        ):
            status = "uncertain" if status not in {"historical", "disputed"} else status
            confidence = min(confidence, 0.65)
        existing = connection.execute(
            """
            SELECT * FROM memory_person_fact_versions
            WHERE chat_name = ? AND person_id = ? AND slot_key = ?
              AND is_current_version = 1 AND deleted_at IS NULL
            ORDER BY revision DESC LIMIT 1
            """,
            (chat_name, int(person_id), slot_key),
        ).fetchone()
        evidence_ids = [int(item["id"]) for item in observations]
        sensitivity = _max_sensitivity(raw.get("sensitivity"), observations)
        if existing is not None and str(existing["normalized_value"]) == normalized:
            merged_ids = sorted(
                set(_json_load(existing["evidence_observation_ids_json"], []))
                | set(evidence_ids)
            )
            merged_observations = self._verified_observations(
                connection,
                chat_name,
                person_id,
                merged_ids,
            )
            merged_metrics = self._support_metrics(merged_observations)
            connection.execute(
                """
                UPDATE memory_person_fact_versions SET
                    status = ?, confidence = MAX(confidence, ?),
                    valid_from = COALESCE(?, valid_from),
                    valid_to = COALESCE(?, valid_to),
                    observed_at = COALESCE(?, observed_at),
                    evidence_observation_ids_json = ?,
                    support_count = ?, independent_day_count = ?,
                    evidence_span_days = ?, priority = MAX(priority, ?),
                    sensitivity = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    confidence,
                    str(raw.get("valid_from") or "") or None,
                    str(raw.get("valid_to") or "") or None,
                    str(raw.get("observed_at") or "") or None,
                    _json_dump(merged_ids),
                    merged_metrics["support_count"],
                    merged_metrics["independent_day_count"],
                    merged_metrics["evidence_span_days"],
                    _safe_float(raw.get("priority"), 0.5),
                    sensitivity,
                    now,
                    int(existing["id"]),
                ),
            )
            return int(existing["id"])

        revision = int(existing["revision"] or 0) + 1 if existing is not None else 1
        cursor = connection.execute(
            """
            INSERT INTO memory_person_fact_versions(
                chat_name, person_id, slot_key, field_name, value,
                normalized_value, status, confidence, valid_from, valid_to,
                observed_at, evidence_observation_ids_json, support_count,
                independent_day_count, evidence_span_days, priority,
                sensitivity, revision, supersedes_fact_id, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_name,
                int(person_id),
                slot_key,
                field_name,
                value,
                normalized,
                status,
                confidence,
                str(raw.get("valid_from") or "") or None,
                str(raw.get("valid_to") or "") or None,
                str(raw.get("observed_at") or "") or None,
                _json_dump(evidence_ids),
                metrics["support_count"],
                metrics["independent_day_count"],
                metrics["evidence_span_days"],
                _safe_float(raw.get("priority"), 0.5),
                sensitivity,
                revision,
                int(existing["id"]) if existing is not None else 0,
                now,
                now,
            ),
        )
        fact_id = int(cursor.lastrowid)
        if existing is not None and not int(existing["manual_lock"] or 0):
            connection.execute(
                """
                UPDATE memory_person_fact_versions SET
                    is_current_version = 0, superseded_by_fact_id = ?,
                    status = CASE
                        WHEN status IN('current', 'planned') THEN 'historical'
                        ELSE status
                    END,
                    valid_to = COALESCE(valid_to, ?), updated_at = ?
                WHERE id = ?
                """,
                (
                    fact_id,
                    str(raw.get("valid_from") or raw.get("observed_at") or "") or None,
                    now,
                    int(existing["id"]),
                ),
            )
        return fact_id

    def _materialize_latest_volatile_facts(
        self,
        connection: Any,
        chat_name: str,
        person_id: int,
        now: str,
    ) -> List[int]:
        """Make current volatile state deterministic from the newest evidence."""
        slot_keys = {
            "occupation": "occupation.primary",
            "employer": "employer.primary",
            "location": "location.current",
            "group_role": "group_role.primary",
            "current_status": "current_status.primary",
        }
        fact_ids: List[int] = []
        for field_name, slot_key in slot_keys.items():
            rows = connection.execute(
                """
                SELECT *
                FROM memory_person_observations
                WHERE chat_name = ? AND person_id = ?
                  AND field_name = ? AND quality_status = 'active'
                  AND epistemic_status = 'asserted'
                """,
                (chat_name, int(person_id), field_name),
            ).fetchall()
            observations = [
                self._observation_from_row(row) for row in rows
            ]
            observations.sort(
                key=lambda observation: (
                    _parse_time(
                        observation.get("observed_at")
                        or observation.get("valid_from")
                    )
                    or datetime.min,
                    int(observation.get("id") or 0),
                ),
                reverse=True,
            )
            if not observations:
                continue
            latest = next(
                (
                    observation
                    for observation in observations
                    if _is_suitable_current_volatile_observation(
                        field_name,
                        observation,
                    )
                    and _is_semantically_valid_fact_projection(
                        field_name,
                        slot_key,
                        _materialized_volatile_value(
                            field_name,
                            observation,
                        ),
                    )
                ),
                None,
            )
            if latest is None:
                continue
            # Model-written volatile facts may still be useful history, but
            # cannot remain current when the ledger provides a latest value.
            connection.execute(
                """
                UPDATE memory_person_fact_versions
                SET status = CASE
                        WHEN status IN('current', 'planned') THEN 'historical'
                        ELSE status
                    END,
                    updated_at = ?
                WHERE chat_name = ? AND person_id = ? AND field_name = ?
                  AND manual_lock = 0 AND deleted_at IS NULL
                """,
                (now, chat_name, int(person_id), field_name),
            )
            if (
                _contains_vague_inference(latest.get("statement"))
                or (
                    field_name == "current_status"
                    and _is_stale_lifecycle([latest])
                )
                or _is_incidental_location([latest])
                or self._is_stale_volatile_field(
                    connection,
                    chat_name,
                    field_name,
                    [latest],
                )
            ):
                continue
            fact_id = self._upsert_fact_projection(
                connection,
                chat_name,
                person_id,
                {
                    "slot_key": slot_key,
                    "field": field_name,
                    "value": _materialized_volatile_value(
                        field_name,
                        latest,
                    ),
                    "status": "current",
                    "confidence": latest.get("confidence"),
                    "valid_from": latest.get("valid_from"),
                    "valid_to": latest.get("valid_to"),
                    "observed_at": latest.get("observed_at"),
                    "evidence_observation_ids": [latest["id"]],
                    "priority": 1.0,
                    "sensitivity": latest.get("sensitivity"),
                },
                now,
            )
            if fact_id:
                fact_ids.append(fact_id)
        return fact_ids

    def _upsert_pattern_projection(
        self,
        connection: Any,
        chat_name: str,
        person_id: int,
        raw: Dict[str, Any],
        now: str,
        *,
        minimum_days: int,
        minimum_span_days: int,
    ) -> Optional[int]:
        pattern_type = str(raw.get("type") or raw.get("pattern_type") or "trait").strip().lower()
        if pattern_type not in PATTERN_TYPES:
            pattern_type = "trait"
        label = _clean_text(raw.get("label"), 160)
        normalized = _normalize_text(label)
        description = _clean_text(raw.get("description") or label, 600)
        observations = self._verified_observations(
            connection,
            chat_name,
            person_id,
            raw.get("evidence_observation_ids") or [],
        )
        if not label or not normalized or not observations:
            return None
        metrics = self._support_metrics(observations)
        requested_state = str(raw.get("state") or "candidate").strip().lower()
        state = requested_state if requested_state in PATTERN_STATES else "candidate"
        if state == "confirmed" and (
            metrics["independent_day_count"] < max(3, minimum_days)
            or metrics["evidence_span_days"] < minimum_span_days
        ):
            state = "candidate"
        sensitivity = _max_sensitivity(raw.get("sensitivity"), observations)
        evidence_ids = [int(item["id"]) for item in observations]
        cursor = connection.execute(
            """
            INSERT INTO memory_person_patterns(
                chat_name, person_id, pattern_type, label, normalized_label,
                description, state, confidence, evidence_observation_ids_json,
                support_count, independent_day_count, evidence_span_days,
                first_seen_at, last_seen_at, sensitivity, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_name, person_id, pattern_type, normalized_label)
            DO UPDATE SET
                description = excluded.description,
                state = CASE
                    WHEN memory_person_patterns.manual_lock = 1
                    THEN memory_person_patterns.state
                    ELSE excluded.state
                END,
                confidence = MAX(
                    memory_person_patterns.confidence, excluded.confidence
                ),
                evidence_observation_ids_json =
                    excluded.evidence_observation_ids_json,
                support_count = excluded.support_count,
                independent_day_count = excluded.independent_day_count,
                evidence_span_days = excluded.evidence_span_days,
                first_seen_at = excluded.first_seen_at,
                last_seen_at = excluded.last_seen_at,
                sensitivity = excluded.sensitivity,
                deleted_at = NULL,
                updated_at = excluded.updated_at
            """,
            (
                chat_name,
                int(person_id),
                pattern_type,
                label,
                normalized,
                description,
                state,
                _safe_float(raw.get("confidence")),
                _json_dump(evidence_ids),
                metrics["support_count"],
                metrics["independent_day_count"],
                metrics["evidence_span_days"],
                metrics["first_seen_at"] or None,
                metrics["last_seen_at"] or None,
                sensitivity,
                now,
                now,
            ),
        )
        if int(cursor.lastrowid or 0):
            return int(cursor.lastrowid)
        row = connection.execute(
            """
            SELECT id FROM memory_person_patterns
            WHERE chat_name = ? AND person_id = ? AND pattern_type = ?
              AND normalized_label = ?
            """,
            (chat_name, int(person_id), pattern_type, normalized),
        ).fetchone()
        return int(row["id"]) if row is not None else None

    def _insert_relationship_projection(
        self,
        connection: Any,
        chat_name: str,
        person_id: int,
        raw: Dict[str, Any],
        now: str,
    ) -> Optional[int]:
        target_name = _clean_text(raw.get("target_name"), 120)
        relationship_type = str(
            raw.get("type") or raw.get("relationship_type") or "other"
        ).strip().lower()
        if relationship_type not in RELATIONSHIP_TYPES:
            relationship_type = "other"
        description = _clean_text(raw.get("description"), 600)
        observations = self._verified_observations(
            connection,
            chat_name,
            person_id,
            raw.get("evidence_observation_ids") or [],
        )
        if not target_name or not description or not observations:
            return None
        metrics = self._support_metrics(observations)
        status = str(raw.get("status") or "uncertain").strip().lower()
        if status not in FACT_STATUSES:
            status = "uncertain"
        # Friendship and group affinity are inferred patterns.  A couple of
        # interactions in one short period cannot establish a current tie.
        if (
            status == "current"
            and relationship_type
            in {"friend", "group_affinity", "group_friction"}
            and (
                metrics["independent_day_count"] < 3
                or metrics["evidence_span_days"] < 30
            )
        ):
            status = "uncertain"
        target_identity = self.store.resolve_person(chat_name, target_name)
        target_person_id = (
            int(target_identity["id"]) if target_identity is not None else 0
        )
        sensitivity = _max_sensitivity(raw.get("sensitivity"), observations)
        cursor = connection.execute(
            """
            INSERT INTO memory_person_relationships(
                chat_name, person_id, target_person_id, target_name,
                relationship_type, description, status, confidence,
                evidence_observation_ids_json, support_count,
                independent_day_count, evidence_span_days, first_seen_at,
                last_seen_at, sensitivity, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_name,
                int(person_id),
                target_person_id,
                target_name,
                relationship_type,
                description,
                status,
                _safe_float(raw.get("confidence")),
                _json_dump([int(item["id"]) for item in observations]),
                metrics["support_count"],
                metrics["independent_day_count"],
                metrics["evidence_span_days"],
                metrics["first_seen_at"] or None,
                metrics["last_seen_at"] or None,
                sensitivity,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def _validated_snapshot_sections(
        self,
        connection: Any,
        chat_name: str,
        person_id: int,
        raw_sections: Any,
    ) -> Dict[str, List[Dict[str, Any]]]:
        source = raw_sections if isinstance(raw_sections, dict) else {}
        result: Dict[str, List[Dict[str, Any]]] = {
            section: [] for section in SNAPSHOT_SECTIONS
        }
        seen_text: set[str] = set()
        suppressed_terms = [
            str(row["target_key"]).split("|", 1)[-1]
            for row in connection.execute(
                """
                SELECT target_key FROM memory_person_suppressions
                WHERE chat_name = ? AND person_id = ?
                  AND status = 'active' AND target_type IN('fact', 'snapshot')
                """,
                (chat_name, int(person_id)),
            ).fetchall()
        ]
        limits = {
            "current_snapshot": 12,
            "timeline": 15,
            "stable_traits": 8,
            "group_relationships": 6,
            "uncertain": 8,
        }
        current_fact_rows = connection.execute(
            """
            SELECT *
            FROM memory_person_fact_versions
            WHERE chat_name = ? AND person_id = ?
              AND is_current_version = 1 AND deleted_at IS NULL
              AND status IN('current', 'planned')
            ORDER BY priority DESC,
                     COALESCE(observed_at, valid_from, created_at) DESC,
                     id DESC
            LIMIT 24
            """,
            (chat_name, int(person_id)),
        ).fetchall()
        confirmed_pattern_rows = connection.execute(
            """
            SELECT pattern_type, evidence_observation_ids_json
            FROM memory_person_patterns
            WHERE chat_name = ? AND person_id = ?
              AND state = 'confirmed' AND deleted_at IS NULL
            """,
            (chat_name, int(person_id)),
        ).fetchall()
        confirmed_pattern_evidence = [
            (
                str(row["pattern_type"] or ""),
                {
                    _safe_int(value)
                    for value in _json_load(
                        row["evidence_observation_ids_json"],
                        [],
                    )
                    if _safe_int(value) > 0
                },
            )
            for row in confirmed_pattern_rows
        ]
        relationship_rows = connection.execute(
            """
            SELECT target_name, status, evidence_observation_ids_json
            FROM memory_person_relationships
            WHERE chat_name = ? AND person_id = ?
              AND deleted_at IS NULL
            """,
            (chat_name, int(person_id)),
        ).fetchall()
        known_relationship_targets = {
            _normalize_text(row["target_name"])
            for row in relationship_rows
            if _normalize_text(row["target_name"])
        }
        current_relationship_evidence = [
            (
                _normalize_text(row["target_name"]),
                {
                    _safe_int(value)
                    for value in _json_load(
                        row["evidence_observation_ids_json"],
                        [],
                    )
                    if _safe_int(value) > 0
                },
            )
            for row in relationship_rows
            if str(row["status"] or "") == "current"
        ]
        for row in current_fact_rows:
            text = _clean_text(row["value"], 360)
            normalized = _normalize_text(text)
            observations = self._verified_observations(
                connection,
                chat_name,
                person_id,
                _json_load(row["evidence_observation_ids_json"], []),
            )
            if (
                not text
                or not normalized
                or not observations
                or normalized in seen_text
                or _contains_vague_inference(text)
                or (
                    (
                        str(row["field_name"] or "")
                        not in VOLATILE_FACT_FIELDS
                        or str(row["field_name"] or "")
                        == "current_status"
                    )
                    and _is_stale_lifecycle(observations)
                )
                or _is_incidental_location(observations)
            ):
                continue
            if any(
                term
                and (term in normalized or normalized in term)
                for term in suppressed_terms
            ):
                continue
            seen_text.add(normalized)
            result["current_snapshot"].append(
                {
                    "text": text,
                    "evidence_observation_ids": [
                        int(observation["id"]) for observation in observations
                    ],
                    "valid_from": _clean_text(row["valid_from"], 40),
                    "valid_to": _clean_text(row["valid_to"], 40),
                    "confidence": _safe_float(row["confidence"]),
                    "sensitivity": _max_sensitivity(
                        row["sensitivity"],
                        observations,
                    ),
                }
            )
            if len(result["current_snapshot"]) >= limits["current_snapshot"]:
                break
        for section in SNAPSHOT_SECTIONS:
            # Current overview is a deterministic materialization of accepted
            # current fact versions above.  The model cannot freely paraphrase
            # several historical states into one "current" sentence.
            if section == "current_snapshot":
                continue
            values = source.get(section)
            if not isinstance(values, list):
                continue
            for raw in values[: limits[section] * 2]:
                if not isinstance(raw, dict):
                    continue
                text = _clean_text(raw.get("text"), 360)
                normalized = _normalize_text(text)
                observations = self._verified_observations(
                    connection,
                    chat_name,
                    person_id,
                    raw.get("evidence_observation_ids") or [],
                )
                if not text or not normalized or not observations or normalized in seen_text:
                    continue
                if any(
                    term
                    and (
                        term in normalized
                        or normalized in term
                    )
                    for term in suppressed_terms
                ):
                    continue
                if section != "uncertain" and any(
                    observation.get("epistemic_status") != "asserted"
                    for observation in observations
                ):
                    continue
                if section != "uncertain" and _contains_vague_inference(text):
                    continue
                if section == "stable_traits":
                    metrics = self._support_metrics(observations)
                    if (
                        metrics["independent_day_count"] < 3
                        or metrics["evidence_span_days"] < 30
                    ):
                        continue
                    evidence_ids = {
                        int(observation["id"])
                        for observation in observations
                    }
                    if not any(
                        len(evidence_ids & pattern_ids)
                        >= max(
                            2,
                            (
                                min(len(evidence_ids), len(pattern_ids))
                                + 1
                            )
                            // 2,
                        )
                        for _, pattern_ids in confirmed_pattern_evidence
                    ):
                        continue
                if section == "group_relationships":
                    evidence_ids = {
                        int(observation["id"])
                        for observation in observations
                    }
                    supported_relationship = any(
                        target
                        and target in normalized
                        and bool(evidence_ids & relationship_ids)
                        for target, relationship_ids
                        in current_relationship_evidence
                    )
                    supported_group_role = any(
                        pattern_type == "group_role"
                        and bool(evidence_ids & pattern_ids)
                        for pattern_type, pattern_ids
                        in confirmed_pattern_evidence
                    )
                    mentions_known_relationship = any(
                        target and target in normalized
                        for target in known_relationship_targets
                    )
                    if not (
                        supported_relationship
                        or (
                            not mentions_known_relationship
                            and supported_group_role
                        )
                    ):
                        continue
                seen_text.add(normalized)
                result[section].append(
                    {
                        "text": text,
                        "evidence_observation_ids": [
                            int(observation["id"]) for observation in observations
                        ],
                        "valid_from": _clean_text(raw.get("valid_from"), 40),
                        "valid_to": _clean_text(raw.get("valid_to"), 40),
                        "confidence": _safe_float(raw.get("confidence")),
                        "sensitivity": _max_sensitivity(
                            raw.get("sensitivity"),
                            observations,
                        ),
                    }
                )
                if len(result[section]) >= limits[section]:
                    break
        return result

    @staticmethod
    def render_snapshot(sections: Dict[str, List[Dict[str, Any]]]) -> str:
        labels = {
            "current_snapshot": "当前概况",
            "timeline": "关键时间线",
            "stable_traits": "稳定特点",
            "group_relationships": "群内角色与关系",
            "uncertain": "待确认",
        }
        blocks = []
        for key in SNAPSHOT_SECTIONS:
            items = sections.get(key) or []
            if not items:
                continue
            lines = [f"【{labels[key]}】"]
            for item in items:
                time_text = item.get("valid_from") or ""
                prefix = f"{time_text} " if time_text and key == "timeline" else ""
                lines.append(f"- {prefix}{item.get('text') or ''}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def apply_projection(
        self,
        chat_name: str,
        person_id: int,
        projection: Dict[str, Any],
        *,
        source_observation_max_id: int,
        minimum_pattern_days: int = 3,
        minimum_pattern_span_days: int = 30,
        generator_version: str = "person-memory",
    ) -> Dict[str, Any]:
        now = self.now()
        fact_ids: List[int] = []
        pattern_ids: List[int] = []
        relationship_ids: List[int] = []
        with self.store._connection() as connection:
            raw_facts = [
                raw
                for raw in (projection.get("facts") or [])[:18]
                if isinstance(raw, dict)
            ]
            raw_facts.sort(
                key=lambda raw: (
                    _clean_text(
                        raw.get("slot_key")
                        or raw.get("field")
                        or raw.get("field_name"),
                        120,
                    ).casefold(),
                    _parse_time(
                        raw.get("observed_at") or raw.get("valid_from")
                    )
                    or datetime.min,
                    _normalize_text(raw.get("value")),
                )
            )
            for raw in raw_facts:
                fact_id = self._upsert_fact_projection(
                    connection,
                    chat_name,
                    person_id,
                    raw,
                    now,
                )
                if fact_id:
                    fact_ids.append(fact_id)
            fact_ids.extend(
                self._materialize_latest_volatile_facts(
                    connection,
                    chat_name,
                    person_id,
                    now,
                )
            )
            for raw in (projection.get("patterns") or [])[:8]:
                if not isinstance(raw, dict):
                    continue
                pattern_id = self._upsert_pattern_projection(
                    connection,
                    chat_name,
                    person_id,
                    raw,
                    now,
                    minimum_days=minimum_pattern_days,
                    minimum_span_days=minimum_pattern_span_days,
                )
                if pattern_id:
                    pattern_ids.append(pattern_id)
            # Rebuild automatic relationship projections each consolidation;
            # manual rows remain untouched.
            connection.execute(
                """
                UPDATE memory_person_relationships
                SET deleted_at = ?, updated_at = ?
                WHERE chat_name = ? AND person_id = ?
                  AND manual_lock = 0 AND deleted_at IS NULL
                """,
                (now, now, chat_name, int(person_id)),
            )
            for raw in (projection.get("relationships") or [])[:6]:
                if not isinstance(raw, dict):
                    continue
                relationship_id = self._insert_relationship_projection(
                    connection,
                    chat_name,
                    person_id,
                    raw,
                    now,
                )
                if relationship_id:
                    relationship_ids.append(relationship_id)

            sections = self._validated_snapshot_sections(
                connection,
                chat_name,
                person_id,
                projection.get("snapshot"),
            )
            anchor_specs = {
                "current_snapshot": (
                    """
                    SELECT evidence_observation_ids_json
                    FROM memory_person_fact_versions
                    WHERE chat_name = ? AND person_id = ?
                      AND is_current_version = 1
                      AND deleted_at IS NULL AND status = 'current'
                    """,
                    (),
                ),
                "stable_traits": (
                    """
                    SELECT evidence_observation_ids_json
                    FROM memory_person_patterns
                    WHERE chat_name = ? AND person_id = ?
                      AND deleted_at IS NULL
                    """,
                    (),
                ),
                "group_relationships": (
                    """
                    SELECT evidence_observation_ids_json
                    FROM memory_person_relationships
                    WHERE chat_name = ? AND person_id = ?
                      AND deleted_at IS NULL
                    """,
                    (),
                ),
            }
            for section, (query, extra_params) in anchor_specs.items():
                anchor_ids = {
                    _safe_int(value)
                    for row in connection.execute(
                        query,
                        (chat_name, int(person_id), *extra_params),
                    ).fetchall()
                    for value in _json_load(
                        row["evidence_observation_ids_json"],
                        [],
                    )
                    if _safe_int(value) > 0
                }
                sections[section] = [
                    item
                    for item in sections[section]
                    if anchor_ids.intersection(
                        {
                            _safe_int(value)
                            for value in item.get(
                                "evidence_observation_ids"
                            )
                            or []
                        }
                    )
                ]
            current_fact_rows = connection.execute(
                """
                SELECT field_name, value, evidence_observation_ids_json
                FROM memory_person_fact_versions
                WHERE chat_name = ? AND person_id = ?
                  AND is_current_version = 1
                  AND deleted_at IS NULL AND status = 'current'
                """,
                (chat_name, int(person_id)),
            ).fetchall()
            current_snapshot_fields = {
                "identity",
                "occupation",
                "employer",
                "location",
                "family",
                "asset",
                "education",
                "current_status",
                "group_role",
            }
            current_snapshot_ids: set[int] = set()
            for row in current_fact_rows:
                evidence_ids = {
                    _safe_int(value)
                    for value in _json_load(
                        row["evidence_observation_ids_json"],
                        [],
                    )
                    if _safe_int(value) > 0
                }
                if not evidence_ids:
                    continue
                field_name = str(row["field_name"] or "")
                include = field_name in current_snapshot_fields
                if not include:
                    placeholders = ",".join("?" for _ in evidence_ids)
                    include = (
                        connection.execute(
                            f"""
                            SELECT 1
                            FROM memory_person_observations
                            WHERE chat_name = ? AND person_id = ?
                              AND id IN({placeholders})
                              AND source_relation = 'manual_admin'
                            LIMIT 1
                            """,
                            (
                                chat_name,
                                int(person_id),
                                *sorted(evidence_ids),
                            ),
                        ).fetchone()
                        is not None
                    )
                if include:
                    current_snapshot_ids.update(evidence_ids)
            sections["current_snapshot"] = [
                item
                for item in sections["current_snapshot"]
                if current_snapshot_ids.intersection(
                    {
                        _safe_int(value)
                        for value in item.get(
                            "evidence_observation_ids"
                        )
                        or []
                    }
                )
                and not re.search(
                    r"(?:父亲|母亲|爸爸|妈妈|伯父|伯母|叔叔|阿姨)"
                    r"(?:是|叫|名为)",
                    str(item.get("text") or ""),
                )
            ]
            # A malformed or over-cautious model response must not erase a
            # previously useful snapshot.
            rendered = self.render_snapshot(sections)
            snapshot_id = 0
            generation = 0
            if rendered:
                row = connection.execute(
                    """
                    SELECT COALESCE(MAX(generation), 0) AS value
                    FROM memory_person_snapshots
                    WHERE chat_name = ? AND person_id = ?
                    """,
                    (chat_name, int(person_id)),
                ).fetchone()
                generation = int(row["value"] or 0) + 1
                connection.execute(
                    """
                    UPDATE memory_person_snapshots SET is_active = 0
                    WHERE chat_name = ? AND person_id = ? AND is_active = 1
                    """,
                    (chat_name, int(person_id)),
                )
                evidence_ids = sorted(
                    {
                        int(value)
                        for items in sections.values()
                        for item in items
                        for value in item.get("evidence_observation_ids") or []
                    }
                )
                cursor = connection.execute(
                    """
                    INSERT INTO memory_person_snapshots(
                        chat_name, person_id, generation, sections_json,
                        rendered_text, evidence_observation_ids_json,
                        source_observation_max_id, is_active,
                        generator_version, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        chat_name,
                        int(person_id),
                        generation,
                        _json_dump(sections),
                        rendered,
                        _json_dump(evidence_ids),
                        int(source_observation_max_id),
                        generator_version,
                        now,
                    ),
                )
                snapshot_id = int(cursor.lastrowid)

            connection.execute(
                """
                INSERT INTO memory_person_refresh_state(
                    chat_name, person_id, consolidated_observation_id,
                    pending_observation_count, last_consolidated_at,
                    last_snapshot_at, updated_at
                ) VALUES(?, ?, ?, 0, ?, ?, ?)
                ON CONFLICT(chat_name, person_id) DO UPDATE SET
                    consolidated_observation_id = MAX(
                        memory_person_refresh_state.consolidated_observation_id,
                        excluded.consolidated_observation_id
                    ),
                    pending_observation_count = (
                        SELECT COUNT(*)
                        FROM memory_person_observations AS observation
                        WHERE observation.chat_name = excluded.chat_name
                          AND observation.person_id = excluded.person_id
                          AND observation.quality_status = 'active'
                          AND observation.id >
                              excluded.consolidated_observation_id
                    ),
                    last_consolidated_at = excluded.last_consolidated_at,
                    last_snapshot_at = CASE
                        WHEN ? > 0 THEN excluded.last_snapshot_at
                        ELSE memory_person_refresh_state.last_snapshot_at
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_name,
                    int(person_id),
                    int(source_observation_max_id),
                    now,
                    now if snapshot_id else None,
                    now,
                    snapshot_id,
                ),
            )
            connection.execute(
                """
                UPDATE memory_person_state SET
                    active_snapshot_generation = MAX(
                        active_snapshot_generation, ?
                    ),
                    last_consolidation_at = ?,
                    updated_at = ?
                WHERE chat_name = ?
                """,
                (generation, now, now, chat_name),
            )
        return {
            "facts": len(fact_ids),
            "patterns": len(pattern_ids),
            "relationships": len(relationship_ids),
            "snapshot_id": snapshot_id,
            "snapshot_generation": generation,
        }

    def get_active_snapshot(
        self,
        chat_name: str,
        person_id: int,
    ) -> Optional[Dict[str, Any]]:
        with self.store._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_person_snapshots
                WHERE chat_name = ? AND person_id = ? AND is_active = 1
                ORDER BY generation DESC LIMIT 1
                """,
                (chat_name, int(person_id)),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["sections"] = _json_load(value.pop("sections_json", "{}"), {})
        value["evidence_observation_ids"] = _json_load(
            value.pop("evidence_observation_ids_json", "[]"),
            [],
        )
        return value

    def _replace_snapshot_after_filter(
        self,
        connection: Any,
        chat_name: str,
        person_id: int,
        *,
        remove_observation_id: int = 0,
        remove_normalized_text: str = "",
        now: str,
        generator_version: str = "person-memory-manual",
    ) -> int:
        row = connection.execute(
            """
            SELECT * FROM memory_person_snapshots
            WHERE chat_name = ? AND person_id = ? AND is_active = 1
            ORDER BY generation DESC LIMIT 1
            """,
            (chat_name, int(person_id)),
        ).fetchone()
        if row is None:
            return 0
        sections = _json_load(row["sections_json"], {})
        changed = False
        filtered: Dict[str, List[Dict[str, Any]]] = {}
        for section in SNAPSHOT_SECTIONS:
            filtered[section] = []
            for item in sections.get(section) or []:
                ids = {
                    _safe_int(value)
                    for value in item.get("evidence_observation_ids") or []
                }
                normalized = _normalize_text(item.get("text"))
                remove = bool(
                    (remove_observation_id and remove_observation_id in ids)
                    or (
                        remove_normalized_text
                        and normalized
                        and (
                            remove_normalized_text in normalized
                            or normalized in remove_normalized_text
                        )
                    )
                )
                if remove:
                    changed = True
                    continue
                filtered[section].append(dict(item))
        if not changed:
            return int(row["id"])
        connection.execute(
            "UPDATE memory_person_snapshots SET is_active = 0 WHERE id = ?",
            (int(row["id"]),),
        )
        rendered = self.render_snapshot(filtered)
        if not rendered:
            return 0
        generation = int(row["generation"] or 0) + 1
        evidence_ids = sorted(
            {
                _safe_int(value)
                for items in filtered.values()
                for item in items
                for value in item.get("evidence_observation_ids") or []
                if _safe_int(value) > 0
            }
        )
        cursor = connection.execute(
            """
            INSERT INTO memory_person_snapshots(
                chat_name, person_id, generation, sections_json,
                rendered_text, evidence_observation_ids_json,
                source_observation_max_id, is_active,
                generator_version, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                chat_name,
                int(person_id),
                generation,
                _json_dump(filtered),
                rendered,
                _json_dump(evidence_ids),
                int(row["source_observation_max_id"] or 0),
                generator_version,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def review_observation(
        self,
        chat_name: str,
        observation_id: int,
        *,
        quality_status: str,
        reason: str,
    ) -> Dict[str, Any]:
        if quality_status not in {"active", "quarantined", "rejected"}:
            raise ValueError("invalid observation quality status")
        reason_text = _clean_text(reason, 1000)
        if len(reason_text) < 2:
            raise ValueError("review reason is required")
        now = self.now()
        with self.store._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_person_observations
                WHERE chat_name = ? AND id = ?
                """,
                (chat_name, int(observation_id)),
            ).fetchone()
            if row is None:
                raise ValueError("observation does not exist")
            before = self._observation_from_row(row)
            person_id = int(row["person_id"])
            connection.execute(
                """
                UPDATE memory_person_observations SET
                    quality_status = ?,
                    rejection_reason = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    quality_status,
                    "" if quality_status == "active" else reason_text,
                    now,
                    int(observation_id),
                ),
            )
            if quality_status != "active":
                for table in (
                    "memory_person_fact_versions",
                    "memory_person_patterns",
                    "memory_person_relationships",
                ):
                    rows = connection.execute(
                        f"""
                        SELECT id, evidence_observation_ids_json
                        FROM {table}
                        WHERE chat_name = ? AND person_id = ?
                          AND deleted_at IS NULL
                        """,
                        (chat_name, person_id),
                    ).fetchall()
                    affected_ids = [
                        int(item["id"])
                        for item in rows
                        if int(observation_id)
                        in {
                            _safe_int(value)
                            for value in _json_load(
                                item["evidence_observation_ids_json"],
                                [],
                            )
                        }
                    ]
                    if affected_ids:
                        placeholders = ",".join("?" for _ in affected_ids)
                        connection.execute(
                            f"""
                            UPDATE {table}
                            SET deleted_at = ?, updated_at = ?
                            WHERE id IN({placeholders})
                            """,
                            (now, now, *affected_ids),
                        )
                self._replace_snapshot_after_filter(
                    connection,
                    chat_name,
                    person_id,
                    remove_observation_id=int(observation_id),
                    now=now,
                )
            connection.execute(
                """
                INSERT INTO memory_person_projection_audit(
                    chat_name, person_id, action, target_type, target_id,
                    reason, before_json, after_json, created_at
                ) VALUES(?, ?, 'review_observation', 'observation', ?, ?, ?, ?, ?)
                """,
                (
                    chat_name,
                    person_id,
                    int(observation_id),
                    reason_text,
                    _json_dump(before),
                    _json_dump(
                        {
                            **before,
                            "quality_status": quality_status,
                            "rejection_reason": (
                                ""
                                if quality_status == "active"
                                else reason_text
                            ),
                        }
                    ),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE memory_person_refresh_state SET
                    pending_observation_count = CASE
                        WHEN ? = 'active'
                        THEN pending_observation_count + 1
                        ELSE pending_observation_count
                    END,
                    updated_at = ?
                WHERE chat_name = ? AND person_id = ?
                """,
                (quality_status, now, chat_name, person_id),
            )
            audit_id = int(
                connection.execute(
                    "SELECT last_insert_rowid() AS value"
                ).fetchone()["value"]
            )
        return {
            "audit_id": audit_id,
            "person_id": person_id,
            "observation_id": int(observation_id),
            "quality_status": quality_status,
        }

    def add_manual_fact(
        self,
        chat_name: str,
        person_id: int,
        fact: Dict[str, Any],
        *,
        reason: str,
    ) -> Dict[str, Any]:
        reason_text = _clean_text(reason, 1000)
        value = _clean_text(fact.get("value"), 600)
        if len(reason_text) < 2 or not value:
            raise ValueError("manual fact value and reason are required")
        field_name = str(
            fact.get("field") or fact.get("field_name") or "other"
        ).strip().lower()
        if field_name not in OBSERVATION_FIELDS:
            field_name = "other"
        status = str(fact.get("status") or "current").strip().lower()
        if status not in FACT_STATUSES:
            status = "uncertain"
        slot_key = _clean_text(fact.get("slot_key"), 120).casefold()
        if not slot_key:
            slot_key = f"manual.{field_name}.{_normalize_text(value)[:60]}"
        now = self.now()
        fingerprint = hashlib.sha256(
            f"manual|{chat_name}|{person_id}|{value}|{now}|{uuid.uuid4().hex}".encode(
                "utf-8"
            )
        ).hexdigest()
        with self.store._connection() as connection:
            identity = connection.execute(
                """
                SELECT canonical_name FROM memory_person_identities
                WHERE chat_name = ? AND id = ? AND status = 'active'
                """,
                (chat_name, int(person_id)),
            ).fetchone()
            if identity is None:
                raise ValueError("person does not exist")
            cursor = connection.execute(
                """
                INSERT INTO memory_person_observations(
                    chat_name, person_id, observation_type, field_name,
                    statement, normalized_statement, source_relation,
                    epistemic_status, confidence, valid_from, valid_to,
                    observed_at, source_namespace, evidence_cursors_json,
                    subject_evidence_cursors_json,
                    evidence_source_ids_json, evidence_senders_json,
                    evidence_excerpt_json, context_json, sensitivity,
                    quality_status, extractor_version, batch_key,
                    fingerprint, created_at, updated_at
                ) VALUES(
                    ?, ?, 'objective_fact', ?, ?, ?, 'manual_admin',
                    'asserted', 1.0, ?, ?, ?, 'manual', '[]', '[]',
                    '[]', ?, ?, ?, ?, 'active', 'manual-admin',
                    'manual', ?, ?, ?
                )
                """,
                (
                    chat_name,
                    int(person_id),
                    field_name,
                    value,
                    _normalize_text(value),
                    str(fact.get("valid_from") or "") or None,
                    str(fact.get("valid_to") or "") or None,
                    str(fact.get("observed_at") or now),
                    _json_dump([str(identity["canonical_name"])]),
                    _json_dump(
                        [
                            {
                                "source": "manual_admin",
                                "reason": reason_text,
                            }
                        ]
                    ),
                    _json_dump({"reason": reason_text}),
                    (
                        str(fact.get("sensitivity") or "low")
                        if str(fact.get("sensitivity") or "low")
                        in SENSITIVITY_LEVELS
                        else "low"
                    ),
                    fingerprint,
                    now,
                    now,
                ),
            )
            observation_id = int(cursor.lastrowid)
        section = (
            "timeline"
            if status == "historical"
            else "uncertain"
            if status in {"uncertain", "disputed"}
            else "current_snapshot"
        )
        existing_snapshot = self.get_active_snapshot(chat_name, person_id)
        merged_sections = {
            key: [
                dict(item)
                for item in (
                    (existing_snapshot or {}).get("sections", {}).get(key)
                    or []
                )
            ]
            for key in SNAPSHOT_SECTIONS
        }
        merged_sections[section].append(
            {
                "text": value,
                "evidence_observation_ids": [observation_id],
                "valid_from": str(fact.get("valid_from") or ""),
                "valid_to": str(fact.get("valid_to") or ""),
                "confidence": 1.0,
                "sensitivity": str(
                    fact.get("sensitivity") or "low"
                ),
            }
        )
        projection = {
            "facts": [
                {
                    "slot_key": slot_key,
                    "field": field_name,
                    "value": value,
                    "status": status,
                    "confidence": 1.0,
                    "valid_from": str(fact.get("valid_from") or ""),
                    "valid_to": str(fact.get("valid_to") or ""),
                    "observed_at": str(fact.get("observed_at") or now),
                    "evidence_observation_ids": [observation_id],
                    "priority": 1.0,
                    "sensitivity": str(
                        fact.get("sensitivity") or "low"
                    ),
                }
            ],
            "patterns": [],
            "relationships": [],
            "snapshot": merged_sections,
        }
        applied = self.apply_projection(
            chat_name,
            person_id,
            projection,
            source_observation_max_id=observation_id,
            generator_version="person-memory-manual",
        )
        with self.store._connection() as connection:
            fact_row = connection.execute(
                """
                SELECT * FROM memory_person_fact_versions
                WHERE chat_name = ? AND person_id = ?
                  AND slot_key = ? AND is_current_version = 1
                ORDER BY revision DESC LIMIT 1
                """,
                (chat_name, int(person_id), slot_key),
            ).fetchone()
            if fact_row is None:
                raise RuntimeError("manual fact projection failed")
            fact_id = int(fact_row["id"])
            connection.execute(
                """
                UPDATE memory_person_fact_versions
                SET manual_lock = 1, priority = 1.0, updated_at = ?
                WHERE id = ?
                """,
                (now, fact_id),
            )
            cursor = connection.execute(
                """
                INSERT INTO memory_person_projection_audit(
                    chat_name, person_id, action, target_type, target_id,
                    reason, before_json, after_json, created_at
                ) VALUES(?, ?, 'add_manual_fact', 'fact', ?, ?, '{}', ?, ?)
                """,
                (
                    chat_name,
                    int(person_id),
                    fact_id,
                    reason_text,
                    _json_dump(dict(fact_row)),
                    now,
                ),
            )
            audit_id = int(cursor.lastrowid)
        return {
            "audit_id": audit_id,
            "person_id": int(person_id),
            "fact_id": fact_id,
            "observation_id": observation_id,
            **applied,
        }

    def delete_fact(
        self,
        chat_name: str,
        person_id: int,
        fact_id: int,
        *,
        reason: str,
    ) -> Dict[str, Any]:
        reason_text = _clean_text(reason, 1000)
        if len(reason_text) < 2:
            raise ValueError("delete reason is required")
        now = self.now()
        with self.store._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_person_fact_versions
                WHERE chat_name = ? AND person_id = ? AND id = ?
                  AND deleted_at IS NULL
                """,
                (chat_name, int(person_id), int(fact_id)),
            ).fetchone()
            if row is None:
                raise ValueError("fact does not exist")
            before = dict(row)
            target_key = (
                f"{row['slot_key']}|{row['normalized_value']}"
            )
            connection.execute(
                """
                UPDATE memory_person_fact_versions
                SET deleted_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, int(fact_id)),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO memory_person_suppressions(
                    chat_name, person_id, target_type, target_key,
                    reason, status, created_at
                ) VALUES(?, ?, 'fact', ?, ?, 'active', ?)
                """,
                (
                    chat_name,
                    int(person_id),
                    target_key,
                    reason_text,
                    now,
                ),
            )
            self._replace_snapshot_after_filter(
                connection,
                chat_name,
                person_id,
                remove_normalized_text=str(row["normalized_value"]),
                now=now,
            )
            cursor = connection.execute(
                """
                INSERT INTO memory_person_projection_audit(
                    chat_name, person_id, action, target_type, target_id,
                    reason, before_json, after_json, created_at
                ) VALUES(?, ?, 'delete_fact', 'fact', ?, ?, ?, ?, ?)
                """,
                (
                    chat_name,
                    int(person_id),
                    int(fact_id),
                    reason_text,
                    _json_dump(before),
                    _json_dump({**before, "deleted_at": now}),
                    now,
                ),
            )
            audit_id = int(cursor.lastrowid)
        return {
            "audit_id": audit_id,
            "person_id": int(person_id),
            "fact_id": int(fact_id),
        }

    def list_audits(
        self,
        chat_name: str,
        *,
        limit: int = 100,
        include_snapshots: bool = True,
    ) -> List[Dict[str, Any]]:
        projection = "*" if include_snapshots else """
            id, chat_name, person_id, action, target_type, target_id,
            reason, status, created_at, reverted_at
        """
        with self.store._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT {projection} FROM memory_person_projection_audit
                WHERE chat_name = ?
                ORDER BY id DESC LIMIT ?
                """,
                (chat_name, max(1, min(1000, int(limit)))),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            if include_snapshots:
                value["before"] = _json_load(value.pop("before_json", "{}"), {})
                value["after"] = _json_load(value.pop("after_json", "{}"), {})
            result.append(value)
        return result

    def list_profiles(
        self,
        chat_name: str,
        *,
        include_building: bool = True,
    ) -> List[Dict[str, Any]]:
        state = self.get_chat_state(chat_name)
        if not include_building and state.get("mode") != "active":
            return []
        with self.store._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    identity.id AS person_id,
                    identity.canonical_name,
                    refresh.pending_observation_count,
                    refresh.last_observation_at,
                    refresh.last_consolidated_at,
                    snapshot.id AS snapshot_id,
                    snapshot.generation,
                    snapshot.sections_json,
                    snapshot.rendered_text,
                    snapshot.source_observation_max_id,
                    snapshot.created_at AS snapshot_created_at,
                    (
                        SELECT COUNT(*)
                        FROM memory_person_observations AS observation
                        WHERE observation.chat_name = identity.chat_name
                          AND observation.person_id = identity.id
                          AND observation.quality_status = 'active'
                    ) AS observation_count
                FROM memory_person_identities AS identity
                LEFT JOIN memory_person_refresh_state AS refresh
                  ON refresh.chat_name = identity.chat_name
                 AND refresh.person_id = identity.id
                LEFT JOIN memory_person_snapshots AS snapshot
                  ON snapshot.chat_name = identity.chat_name
                 AND snapshot.person_id = identity.id
                 AND snapshot.is_active = 1
                WHERE identity.chat_name = ? AND identity.status = 'active'
                  AND (
                    snapshot.id IS NOT NULL
                    OR EXISTS(
                        SELECT 1
                        FROM memory_person_observations AS observation
                        WHERE observation.chat_name = identity.chat_name
                          AND observation.person_id = identity.id
                    )
                  )
                ORDER BY observation_count DESC, identity.canonical_name
                """,
                (chat_name,),
            ).fetchall()
        profiles = []
        for row in rows:
            value = dict(row)
            value["person_name"] = value["canonical_name"]
            value["sections"] = _json_load(value.pop("sections_json", "{}"), {})
            identity = self.store.resolve_person(
                chat_name,
                str(value["canonical_name"]),
            )
            value["aliases"] = (
                list(identity.get("aliases") or []) if identity is not None else []
            )
            value["facts"] = self.list_current_facts(
                chat_name,
                int(value["person_id"]),
            )
            value["patterns"] = self.list_patterns(
                chat_name,
                int(value["person_id"]),
            )
            value["relationships"] = self.list_relationships(
                chat_name,
                int(value["person_id"]),
            )
            profiles.append(value)
        return profiles

    def count_profiles(self, chat_name: str) -> int:
        """Count materialized profiles without loading their nested details."""
        with self.store._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(DISTINCT identity.id) AS value
                FROM memory_person_identities AS identity
                JOIN memory_person_snapshots AS snapshot
                  ON snapshot.chat_name = identity.chat_name
                 AND snapshot.person_id = identity.id
                 AND snapshot.is_active = 1
                WHERE identity.chat_name = ? AND identity.status = 'active'
                """,
                (chat_name,),
            ).fetchone()
        return int(row["value"] if row else 0)

    def browse_profiles(
        self,
        chat_name: str,
        *,
        query: str = "",
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[List[Dict[str, Any]], int]:
        """Return paginated person summaries; nested evidence stays in detail."""
        normalized_query = str(query or "").strip()
        clauses = ["identity.chat_name = ?", "identity.status = 'active'"]
        params: List[Any] = [chat_name]
        if normalized_query:
            clauses.append(
                """
                (
                    identity.canonical_name LIKE ? ESCAPE '\\'
                    OR COALESCE(snapshot.rendered_text, '') LIKE ? ESCAPE '\\'
                    OR EXISTS(
                        SELECT 1 FROM memory_person_aliases AS matched_alias
                        WHERE matched_alias.chat_name = identity.chat_name
                          AND matched_alias.person_id = identity.id
                          AND matched_alias.alias_name LIKE ? ESCAPE '\\'
                    )
                )
                """
            )
            escaped = (
                normalized_query.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            pattern = f"%{escaped}%"
            params.extend((pattern, pattern, pattern))
        where_sql = " AND ".join(clauses)
        safe_offset = max(0, int(offset))
        safe_limit = max(1, min(100, int(limit)))
        with self.store._connection() as connection:
            total_row = connection.execute(
                f"""
                SELECT COUNT(*) AS value
                FROM memory_person_identities AS identity
                LEFT JOIN memory_person_snapshots AS snapshot
                  ON snapshot.chat_name = identity.chat_name
                 AND snapshot.person_id = identity.id
                 AND snapshot.is_active = 1
                WHERE {where_sql}
                  AND (
                    snapshot.id IS NOT NULL
                    OR EXISTS(
                        SELECT 1 FROM memory_person_observations AS observation
                        WHERE observation.chat_name = identity.chat_name
                          AND observation.person_id = identity.id
                    )
                  )
                """,
                params,
            ).fetchone()
            rows = connection.execute(
                f"""
                SELECT
                    identity.id AS person_id,
                    identity.canonical_name AS person_name,
                    identity.manual_lock,
                    refresh.pending_observation_count,
                    refresh.last_observation_at,
                    refresh.last_consolidated_at,
                    snapshot.id AS snapshot_id,
                    snapshot.generation,
                    snapshot.created_at AS snapshot_created_at,
                    SUBSTR(COALESCE(snapshot.rendered_text, ''), 1, 360)
                        AS summary,
                    (
                        SELECT COUNT(*)
                        FROM memory_person_observations AS observation
                        WHERE observation.chat_name = identity.chat_name
                          AND observation.person_id = identity.id
                          AND observation.quality_status = 'active'
                    ) AS observation_count,
                    (
                        SELECT COUNT(*)
                        FROM memory_person_fact_versions AS fact
                        WHERE fact.chat_name = identity.chat_name
                          AND fact.person_id = identity.id
                          AND fact.is_current_version = 1
                          AND fact.deleted_at IS NULL
                    ) AS fact_count,
                    (
                        SELECT COUNT(*)
                        FROM memory_person_patterns AS pattern
                        WHERE pattern.chat_name = identity.chat_name
                          AND pattern.person_id = identity.id
                          AND pattern.deleted_at IS NULL
                    ) AS pattern_count
                FROM memory_person_identities AS identity
                LEFT JOIN memory_person_refresh_state AS refresh
                  ON refresh.chat_name = identity.chat_name
                 AND refresh.person_id = identity.id
                LEFT JOIN memory_person_snapshots AS snapshot
                  ON snapshot.chat_name = identity.chat_name
                 AND snapshot.person_id = identity.id
                 AND snapshot.is_active = 1
                WHERE {where_sql}
                  AND (
                    snapshot.id IS NOT NULL
                    OR EXISTS(
                        SELECT 1 FROM memory_person_observations AS observation
                        WHERE observation.chat_name = identity.chat_name
                          AND observation.person_id = identity.id
                    )
                  )
                ORDER BY observation_count DESC, identity.canonical_name
                LIMIT ? OFFSET ?
                """,
                (*params, safe_limit, safe_offset),
            ).fetchall()
            person_ids = [int(row["person_id"]) for row in rows]
            aliases_by_person: Dict[int, List[str]] = defaultdict(list)
            if person_ids:
                placeholders = ",".join("?" for _ in person_ids)
                alias_rows = connection.execute(
                    f"""
                    SELECT person_id, alias_name
                    FROM memory_person_aliases
                    WHERE chat_name = ? AND status = 'confirmed'
                      AND person_id IN({placeholders})
                    ORDER BY person_id, id
                    """,
                    (chat_name, *person_ids),
                ).fetchall()
                for alias in alias_rows:
                    aliases_by_person[int(alias["person_id"])].append(
                        str(alias["alias_name"] or "")
                    )
        items = []
        for row in rows:
            value = dict(row)
            value["aliases"] = aliases_by_person.get(
                int(value["person_id"]),
                [],
            )
            value["status"] = "ready" if value.get("snapshot_id") else "building"
            items.append(value)
        return items, int(total_row["value"] if total_row else 0)

    def get_profile(
        self,
        chat_name: str,
        person_id: int,
    ) -> Optional[Dict[str, Any]]:
        with self.store._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    identity.id AS person_id,
                    identity.canonical_name,
                    identity.manual_lock,
                    refresh.pending_observation_count,
                    refresh.last_observation_at,
                    refresh.last_consolidated_at,
                    snapshot.id AS snapshot_id,
                    snapshot.generation,
                    snapshot.sections_json,
                    snapshot.rendered_text,
                    snapshot.source_observation_max_id,
                    snapshot.created_at AS snapshot_created_at
                FROM memory_person_identities AS identity
                LEFT JOIN memory_person_refresh_state AS refresh
                  ON refresh.chat_name = identity.chat_name
                 AND refresh.person_id = identity.id
                LEFT JOIN memory_person_snapshots AS snapshot
                  ON snapshot.chat_name = identity.chat_name
                 AND snapshot.person_id = identity.id
                 AND snapshot.is_active = 1
                WHERE identity.chat_name = ? AND identity.id = ?
                  AND identity.status = 'active'
                """,
                (chat_name, int(person_id)),
            ).fetchone()
        if row is None:
            return None
        profile = dict(row)
        profile["person_name"] = profile["canonical_name"]
        profile["sections"] = _json_load(profile.pop("sections_json", "{}"), {})
        identity = self.store.resolve_person(
            chat_name,
            str(profile["canonical_name"]),
        )
        profile["aliases"] = list(identity.get("aliases") or []) if identity else []
        profile["facts"] = self.list_current_facts(chat_name, int(person_id))
        profile["patterns"] = self.list_patterns(chat_name, int(person_id))
        profile["relationships"] = self.list_relationships(chat_name, int(person_id))
        profile["recent_observations"] = self.list_observations(
            chat_name,
            person_id=int(person_id),
            limit=100,
            descending=True,
        )
        return profile


class PersonMemoryEngine:
    """Extract raw-message observations and maintain evidence-backed profiles."""

    def __init__(
        self,
        store: MemoryStore,
        context_manager: Any,
        call_json: Callable[..., Dict[str, Any]],
        *,
        excluded_person_names: Optional[Iterable[str]] = None,
    ) -> None:
        self.store = store
        self.ledger = PersonMemoryStore(store)
        self.context_manager = context_manager
        self.call_json = call_json
        self.excluded_person_names = {
            str(value or "").strip().casefold()
            for value in (excluded_person_names or [])
            if str(value or "").strip()
        }

    @staticmethod
    def _format_messages(messages: Sequence[Dict[str, Any]]) -> str:
        lines = []
        for message in messages:
            cursor = _safe_int(message.get("_log_cursor"))
            time_text = str(message.get("time") or "")
            sender = _clean_text(message.get("sender"), 100)
            sender_id = _clean_text(message.get("sender_id"), 180)
            content = str(message.get("content") or "").replace("\x00", "")
            lines.append(
                f"[#{cursor}] [{time_text}] [sender={sender}] "
                f"[sender_id={sender_id or '-'}] {content}"
            )
        return "\n".join(lines)

    def extract_observations(
        self,
        chat_name: str,
        messages: Sequence[Dict[str, Any]],
        *,
        core_start_cursor: int,
        core_end_cursor: int,
        core_cursors: Optional[Iterable[int]] = None,
        source_namespace: str = "live_chat_log",
        batch_key: str = "",
        trace_id: str = "",
        excluded_sender_names: Optional[Iterable[str]] = None,
        excluded_sender_ids: Optional[Iterable[str]] = None,
        target_person_id: int = 0,
        max_observations: int = 6,
        minimum_memory_value: float = 0.78,
    ) -> Optional[Dict[str, Any]]:
        if not messages:
            return {"inserted": 0, "observations": []}
        excluded_names = {
            str(value or "").strip().casefold()
            for value in (excluded_sender_names or [])
            if str(value or "").strip()
        }
        excluded_ids = {
            str(value or "").strip()
            for value in (excluded_sender_ids or [])
            if str(value or "").strip()
        }
        self.excluded_person_names.update(excluded_names)
        messages = [
            dict(message)
            for message in messages
            if str(message.get("sender") or "").strip().casefold()
            not in excluded_names
            and str(message.get("sender_id") or "").strip()
            not in excluded_ids
        ]
        if not messages:
            return {
                "inserted": 0,
                "quarantined": 0,
                "filtered_low_value": 0,
                "filtered_weak_evidence": 0,
                "filtered_verification": 0,
                "observations": [],
            }
        by_cursor = {
            _safe_int(message.get("_log_cursor")): dict(message)
            for message in messages
            if _safe_int(message.get("_log_cursor")) > 0
        }
        if not by_cursor:
            return {"inserted": 0, "observations": []}
        core_cursor_set = (
            {
                _safe_int(value)
                for value in (core_cursors or [])
                if _safe_int(value) in by_cursor
            }
            if core_cursors is not None
            else {
                cursor
                for cursor in by_cursor
                if core_start_cursor <= cursor <= core_end_cursor
            }
        )
        if not core_cursor_set:
            return {"inserted": 0, "observations": []}
        observation_limit = max(1, min(30, int(max_observations or 6)))
        value_floor = max(
            0.35,
            min(0.95, float(minimum_memory_value or 0.78)),
        )
        target_identity = next(
            (
                person
                for person in self.store.list_person_directory(chat_name)
                if int(person.get("person_id") or 0)
                == int(target_person_id or 0)
            ),
            None,
        )
        if target_identity is None:
            return {"inserted": 0, "observations": []}
        observed_sender_ids = sorted(
            {
                str(message.get("sender_id") or "").strip()
                for message in messages
                if str(message.get("sender_id") or "").strip()
            }
        )
        observed_names = sorted(
            {
                str(message.get("sender") or "").strip()
                for message in messages
                if str(message.get("sender") or "").strip()
            }
        )
        prompt = [
            {
                "role": "system",
                "content": (
                    "你是人物长期记忆的“观察记录员”，直接阅读原始群聊，不写人物简介。"
                    "本批消息已经按一个目标人物聚合。高召回提取该人物的身份、经历、"
                    "职业与地点变化、明确计划、长期兴趣信号、技能、群内角色和关系"
                    "信号；不要因为尚未形成完整人物简介而丢弃有明确原文支持的候选。"
                    f"最多输出{observation_limit}条，按长期价值排序，不要求凑满。"
                    + "普通寒暄、一次性观点、下注/赔率/接龙流水、临时输赢、"
                    "商品或新闻链接、临时游戏邀约/段位、一次性技术排障、当天上班休息、"
                    "下载网速、天气、纯玩笑、角色扮演和无法确定主语的内容一律不提取。"
                    "不要从支持某支球队推断籍贯，不要从一次下载速度推断网络状况，"
                    "不要从一次休息推断工作排班，也不要从分享一个产品推断长期兴趣。"
                    "按memory_value从高到低输出。若候选过多，优先身份/职业/"
                    "教育/家庭/长期地点变化、重要经历、明确技能与反复出现的兴趣证据，"
                    "舍弃低价值内容。"
                    "每条观察必须绑定真实群员的 sender_id，第三方公众人物或群外亲友不能"
                    "新建人物。self_report 只允许该 sender_id 本人说自己的情况；别人评价"
                    "他只能是 attributed_statement，且默认 uncertain。direct_action 只描述"
                    "消息中可直接观察到的群内行为，不推断性格；兴趣/偏好/习惯/技能/"
                    "群角色若不是本人明确陈述，至少需要同一人的两条独立行为证据。"
                    "长期性格、习惯、关系亲疏"
                    "不要在本步下结论，只记录可复核观察，后续由跨时间证据聚合。"
                    "evidence_cursors 是支撑内容的原消息；subject_evidence_cursors 必须明确"
                    "支撑主语绑定。至少一条事实证据必须位于核心游标，前后重叠只用于消歧。"
                    "相对时间必须保留 observed_at/valid_from，不得改写为永久属性。"
                    "敏感健康、财务、家庭内容只有当事人明确自述且确有长期价值时才记录。"
                    "只输出JSON对象，不要代码块。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"群聊：{chat_name}\n"
                    + "目标人物："
                    f"{target_identity.get('person_name')}\n"
                    "目标人物别名："
                    f"{_json_dump([alias.get('alias_name') for alias in target_identity.get('aliases', [])])}\n"
                    "目标人物稳定 sender_id："
                    f"{_json_dump(sorted({str(alias.get('external_id') or '') for alias in target_identity.get('aliases', []) if str(alias.get('external_id') or '')}))}\n"
                    "subject_sender_id 只能填写上述目标ID；若本批未出现"
                    "目标本人消息且无法确认ID则填空，绝不能填写第三方说话人的ID。\n"
                    f"核心人物消息游标：{_json_dump(sorted(core_cursor_set))}\n"
                    +
                    f"本批出现的 sender_id：{_json_dump(observed_sender_ids)}\n"
                    f"本批出现的昵称：{_json_dump(observed_names)}\n\n"
                    "原始消息（核心外消息只作重叠证据）：\n"
                    f"{self._format_messages(messages)}\n\n"
                    "严格输出："
                    '{"observations":[{"subject_name":"群员昵称",'
                    '"subject_sender_id":"原消息中的sender_id",'
                    '"observation_type":"objective_fact|experience|preference|'
                    'interest|skill|habit|group_role|relationship|status|plan",'
                    '"field":"identity|group_role|occupation|employer|education|'
                    'location|family|relationship|health|preference|interest|'
                    'skill|asset|experience|habit|plan|current_status|other",'
                    '"statement":"一条原子观察，保留归因与时间语义",'
                    '"source_relation":"self_report|direct_action|'
                    'attributed_statement|group_interaction",'
                    '"epistemic_status":"asserted|uncertain|joke|sarcasm|'
                    'roleplay|hypothetical|denied","confidence":0.0,'
                    '"valid_from":"ISO时间或空","valid_to":"ISO时间或空",'
                    '"observed_at":"ISO时间","evidence_cursors":[1],'
                    '"subject_evidence_cursors":[1],'
                    '"sensitivity":"low|medium|high",'
                    '"durability":"stable|lifecycle|ephemeral",'
                    '"evidence_strength":"explicit|repeated_behavior|weak_inference",'
                    '"memory_value":0.0,'
                    '"future_value_reason":"半年后仍值得记住的具体原因"}]}'
                ),
            },
        ]
        try:
            payload = self.call_json(
                call_type="memory_person_extract",
                messages=prompt,
                schema_hint='根对象必须是 {"observations":[...]}',
                chat_name=chat_name,
                trace_id=trace_id,
            )
        except Exception as exc:
            logger.warning(
                "⚠️ Person observation extraction failed for %s: %s",
                chat_name,
                exc,
            )
            return None
        raw_observations = payload.get("observations")
        if not isinstance(raw_observations, list):
            return None
        raw_candidates: List[Dict[str, Any]] = []
        for index, raw in enumerate(
            raw_observations[:observation_limit],
            start=1,
        ):
            if not isinstance(raw, dict):
                continue
            candidate = dict(raw)
            candidate["candidate_id"] = f"c{index}"
            raw_candidates.append(candidate)
        if int(target_person_id or 0) > 0 and batch_key:
            self.ledger.record_claim_candidates(
                chat_name,
                int(target_person_id),
                source_namespace,
                batch_key,
                raw_candidates,
            )

        normalized: List[Dict[str, Any]] = []
        filtered_low_value = 0
        filtered_weak_evidence = 0
        allowed_cursors = set(by_cursor)
        for raw in raw_candidates:
            memory_value = _safe_float(raw.get("memory_value"))
            durability = str(
                raw.get("durability") or "ephemeral"
            ).strip().lower()
            evidence_strength = str(
                raw.get("evidence_strength") or "weak_inference"
            ).strip().lower()
            if durability not in {"stable", "lifecycle", "ephemeral"}:
                durability = "ephemeral"
            if evidence_strength not in {
                "explicit",
                "repeated_behavior",
                "weak_inference",
            }:
                evidence_strength = "weak_inference"
            if memory_value < value_floor or durability == "ephemeral":
                filtered_low_value += 1
                continue
            if evidence_strength == "weak_inference":
                filtered_weak_evidence += 1
                continue
            sender_id = _clean_text(raw.get("subject_sender_id"), 180)
            subject_name = _clean_text(raw.get("subject_name"), 100)
            identity = self.store.resolve_person(
                chat_name,
                subject_name,
                external_id=sender_id,
            )
            if identity is None:
                # Never let an extracted name create a new group member.
                continue
            person_id = int(identity["id"])
            if (
                int(target_person_id or 0) > 0
                and person_id != int(target_person_id)
            ):
                continue
            evidence_cursors = sorted(
                {
                    _safe_int(value)
                    for value in raw.get("evidence_cursors") or []
                    if _safe_int(value) in allowed_cursors
                }
            )
            subject_cursors = sorted(
                {
                    _safe_int(value)
                    for value in raw.get("subject_evidence_cursors") or []
                    if _safe_int(value) in evidence_cursors
                }
            )
            if (
                not evidence_cursors
                or not subject_cursors
                or not any(
                    value in core_cursor_set
                    for value in evidence_cursors
                )
            ):
                continue
            source_relation = str(
                raw.get("source_relation") or "attributed_statement"
            ).strip().lower()
            if source_relation not in SOURCE_RELATIONS:
                source_relation = "attributed_statement"
            epistemic_status = str(
                raw.get("epistemic_status") or "uncertain"
            ).strip().lower()
            if epistemic_status not in EPISTEMIC_STATUSES:
                epistemic_status = "uncertain"
            evidence_messages = [by_cursor[value] for value in evidence_cursors]
            def is_subject_message(message: Dict[str, Any]) -> bool:
                message_sender_id = str(
                    message.get("sender_id") or ""
                ).strip()
                if sender_id and message_sender_id:
                    return message_sender_id == sender_id
                return (
                    str(message.get("sender") or "").strip().casefold()
                    == subject_name.casefold()
                )

            if source_relation == "self_report":
                authored_subject_cursors = [
                    cursor
                    for cursor in subject_cursors
                    if is_subject_message(by_cursor[cursor])
                ]
                if not authored_subject_cursors:
                    # A self-report must contain the subject's own message.
                    filtered_weak_evidence += 1
                    continue
                last_subject_cursor = max(authored_subject_cursors)
                if any(
                    _safe_int(message.get("_log_cursor"))
                    > last_subject_cursor
                    and not is_subject_message(message)
                    for message in evidence_messages
                ):
                    # Never append a third party's later claim to a self-report.
                    filtered_weak_evidence += 1
                    continue
                subject_cursors = authored_subject_cursors
            observation_type = str(
                raw.get("observation_type") or "objective_fact"
            ).strip().lower()
            if observation_type not in OBSERVATION_TYPES:
                observation_type = "objective_fact"
            field_name = str(raw.get("field") or "other").strip().lower()
            if field_name not in OBSERVATION_FIELDS:
                field_name = "other"
            if (
                excluded_names
                and field_name in BOT_PROMPT_FACT_FIELDS
                and any(
                    any(
                        re.search(
                            rf"@\s*{re.escape(excluded_name)}"
                            rf"(?:\s|$|[，。！？；：、,.!?])",
                            str(by_cursor[cursor].get("content") or ""),
                            flags=re.IGNORECASE,
                        )
                        for excluded_name in excluded_names
                    )
                    for cursor in subject_cursors
                )
            ):
                # Bot-directed prompts often invent relatives, jobs or assets
                # for a joke. Keep them as chat context, not durable identity
                # evidence.
                filtered_weak_evidence += 1
                continue
            subject_message_count = sum(
                1
                for message in evidence_messages
                if str(message.get("sender_id") or "").strip() == sender_id
            )
            behavior_requires_repetition = (
                source_relation in {"direct_action", "group_interaction"}
                and (
                    observation_type
                    in {
                        "preference",
                        "interest",
                        "skill",
                        "habit",
                        "group_role",
                        "relationship",
                    }
                    or field_name
                    in {
                        "preference",
                        "interest",
                        "skill",
                        "habit",
                        "group_role",
                        "relationship",
                    }
                )
            )
            if behavior_requires_repetition and (
                evidence_strength != "repeated_behavior"
                or subject_message_count < 2
            ):
                filtered_weak_evidence += 1
                continue
            if field_name in {"other", "current_status"} and (
                memory_value
                < max(value_floor + 0.12, 0.72)
                or durability != "lifecycle"
            ):
                filtered_low_value += 1
                continue
            statement = _clean_text(raw.get("statement"), 1000)
            if not statement:
                continue
            if _observation_adds_unsupported_specificity(
                field_name,
                statement,
                evidence_messages,
            ):
                filtered_weak_evidence += 1
                continue
            if (
                field_name == "health"
                and re.search(
                    r"(?:父亲|母亲|爸爸|妈妈|老豆|老母|父母|"
                    r"(?:大|二|三|四|阿)?姐|(?:大|二|三|四|阿)?妹|"
                    r"(?:大|二|三|四|阿)?哥|(?:大|二|三|四|阿)?弟|"
                    r"家姐|细佬|老婆|妻子|丈夫|女友|男友|"
                    r"孩子|儿子|女儿|亲友)",
                    statement,
                )
                and re.search(
                    r"(?:确诊|生病|患|感染|咳血|住院|手术|痛风|"
                    r"高血压|糖尿病|癌|去世|死亡)",
                    statement,
                )
            ):
                # This is health data about a third party, not the target
                # person's own health.  Do not attach it to the target profile.
                filtered_weak_evidence += 1
                continue
            if (
                source_relation == "self_report"
                and field_name
                in {"family", "relationship", "health", "asset"}
            ):
                subject_evidence_text = "\n".join(
                    str(by_cursor[cursor].get("content") or "")
                    for cursor in subject_cursors
                )
                if not re.search(
                    r"(?:^|[\s，。！？；：、,@])(?:我|本人|老子|自己|我家)",
                    subject_evidence_text,
                ):
                    # High-impact personal facts require an explicit first-person
                    # anchor.  An omitted Cantonese/Chinese subject can easily be
                    # a comment about the previous speaker ("听朝就做人老豆了").
                    filtered_weak_evidence += 1
                    continue
            if (
                epistemic_status == "asserted"
                and _contains_vague_inference(statement)
            ):
                filtered_weak_evidence += 1
                continue
            confidence = _safe_float(raw.get("confidence"))
            quality_status = "active"
            rejection_reason = ""
            if epistemic_status in {
                "joke",
                "sarcasm",
                "roleplay",
                "hypothetical",
            }:
                quality_status = "quarantined"
                rejection_reason = f"epistemic_status={epistemic_status}"
                confidence = min(confidence, 0.3)
            elif (
                source_relation == "attributed_statement"
                and epistemic_status == "asserted"
            ):
                epistemic_status = "uncertain"
                confidence = min(confidence, 0.65)
            evidence_source_ids = [
                str(message.get("source_id") or "")
                for message in evidence_messages
                if str(message.get("source_id") or "")
            ]
            evidence_senders = [
                str(message.get("sender") or "")
                for message in evidence_messages
            ]
            evidence_excerpt = [
                {
                    "cursor": _safe_int(message.get("_log_cursor")),
                    "time": str(message.get("time") or ""),
                    "sender": str(message.get("sender") or ""),
                    "sender_id": str(message.get("sender_id") or ""),
                    "content": str(message.get("content") or "")[:1500],
                }
                for message in evidence_messages
            ]
            observed_at = _clean_text(raw.get("observed_at"), 40)
            if not observed_at:
                observed_at = str(evidence_messages[-1].get("time") or "")
            sensitivity = str(raw.get("sensitivity") or "low").strip().lower()
            if sensitivity not in SENSITIVITY_LEVELS:
                sensitivity = "low"
            if field_name in {"family", "asset"}:
                sensitivity = _max_sensitivity(
                    "medium",
                    [{"sensitivity": sensitivity}],
                )
            elif field_name == "health":
                sensitivity = "high"
            elif field_name == "location":
                sensitivity = _max_sensitivity(
                    "medium",
                    [{"sensitivity": sensitivity}],
                )
                if re.search(
                    r"(?:路|街|巷|栋|室|号|工业区|小区|公寓|办公室)",
                    statement,
                ):
                    sensitivity = "high"
            normalized.append(
                {
                    "person_id": person_id,
                    "observation_type": observation_type,
                    "field_name": field_name,
                    "statement": statement,
                    "source_relation": source_relation,
                    "epistemic_status": epistemic_status,
                    "confidence": confidence,
                    "valid_from": _clean_text(raw.get("valid_from"), 40),
                    "valid_to": _clean_text(raw.get("valid_to"), 40),
                    "observed_at": observed_at,
                    "evidence_cursors": evidence_cursors,
                    "subject_evidence_cursors": subject_cursors,
                    "evidence_source_ids": evidence_source_ids,
                    "evidence_senders": evidence_senders,
                    "evidence_excerpt": evidence_excerpt,
                    "context": {
                        "subject_name": subject_name,
                        "subject_sender_id": sender_id,
                        "candidate_id": str(
                            raw.get("candidate_id") or ""
                        ),
                        "core_start_cursor": core_start_cursor,
                        "core_end_cursor": core_end_cursor,
                        "memory_value": memory_value,
                        "durability": durability,
                        "evidence_strength": evidence_strength,
                        "future_value_reason": _clean_text(
                            raw.get("future_value_reason"),
                            500,
                        ),
                    },
                    "sensitivity": sensitivity,
                    "quality_status": quality_status,
                    "rejection_reason": rejection_reason,
                    "extractor_version": "person-memory.1",
                }
            )
        asserted = [
            observation
            for observation in normalized
            if observation.get("quality_status") == "active"
        ]
        pre_quarantined = [
            observation
            for observation in normalized
            if observation.get("quality_status") != "active"
        ]
        verified: List[Dict[str, Any]] = []
        filtered_verification = 0
        verification_quarantined = 0
        if asserted:
            candidates = [
                {
                    "candidate_id": (
                        observation["context"].get("candidate_id")
                        or f"c{index}"
                    ),
                    "subject_name": observation["context"]["subject_name"],
                    "subject_sender_id": observation["context"][
                        "subject_sender_id"
                    ],
                    "statement": observation["statement"],
                    "source_relation": observation["source_relation"],
                    "field": observation["field_name"],
                    "evidence_cursors": observation["evidence_cursors"],
                    "subject_evidence_cursors": observation[
                        "subject_evidence_cursors"
                    ],
            }
            for index, observation in enumerate(asserted, start=1)
            ]
            verification_prompt = [
                {
                    "role": "system",
                    "content": (
                        "你是人物记忆的独立证据核验员。你的目标是宁缺毋滥，判断候选"
                        "观察是否被原始群聊明确支持。不要因为候选写得通顺就认可。"
                        "supported 必须同时满足：当事人的原话或可直接观察行为明确支持"
                        "完整主语、谓语和关键对象；不是靠另一人的名词加当事人的短句拼接；"
                        "不是玩笑、夸张、吹牛、反讽、角色扮演、假设、向AI设定人设或"
                        "讨论第三人的情况。比如本人只说“买了”、别人说“918”，不能证明"
                        "本人买了918；只说“5个月相当于8个月”不能证明其妻子怀双胞胎；"
                        "讨论婚礼流程不能证明是自己的婚礼；“当事人没有否认”永远不能"
                        "作为支持；在@机器人请求评价时临时声称"
                        "一个身份而没有上下文支撑，应判 uncertain 或 reject。"
                        "subject_binding=explicit 要求至少一条当事人消息本身足以绑定事实，"
                        "不能仅靠发言者身份或别人点名。对 direct_action 可用"
                        "literalness=direct_behavior，但不得由一次行为推断长期属性。"
                        "只输出JSON对象。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"原始消息：\n{self._format_messages(messages)}\n\n"
                        f"候选观察：{_json_dump(candidates)}\n\n"
                        "逐项严格输出："
                        '{"verifications":[{"candidate_id":"c1",'
                        '"verdict":"supported|uncertain|reject",'
                        '"subject_binding":"explicit|contextual|unsupported",'
                        '"literalness":"literal|direct_behavior|joke|sarcasm|'
                        'boast|roleplay|hypothetical|third_party",'
                        '"evidence_completeness":"complete|partial|missing",'
                        '"atomicity":"atomic|compound",'
                        '"confidence":0.0,"reason":"简明原因"}]}'
                    ),
                },
            ]
            try:
                verification_payload = self.call_json(
                    call_type="memory_person_review",
                    messages=verification_prompt,
                    schema_hint='根对象必须是 {"verifications":[...]}',
                    chat_name=chat_name,
                    trace_id=trace_id,
                )
            except Exception as exc:
                logger.warning(
                    "⚠️ Person observation verification failed for %s: %s",
                    chat_name,
                    exc,
                )
                return None
            verification_rows = verification_payload.get("verifications")
            if not isinstance(verification_rows, list):
                return None
            decisions = {
                str(row.get("candidate_id") or ""): row
                for row in verification_rows
                if isinstance(row, dict)
            }
            for index, observation in enumerate(asserted, start=1):
                candidate_id = str(
                    observation["context"].get("candidate_id")
                    or f"c{index}"
                )
                decision = decisions.get(candidate_id) or {}
                verdict = str(
                    decision.get("verdict") or "reject"
                ).strip().lower()
                binding = str(
                    decision.get("subject_binding") or "unsupported"
                ).strip().lower()
                literalness = str(
                    decision.get("literalness") or "third_party"
                ).strip().lower()
                completeness = str(
                    decision.get("evidence_completeness") or "missing"
                ).strip().lower()
                atomicity = str(
                    decision.get("atomicity") or "compound"
                ).strip().lower()
                verification_confidence = _safe_float(
                    decision.get("confidence")
                )
                observation["context"]["verification"] = {
                    "verdict": verdict,
                    "subject_binding": binding,
                    "literalness": literalness,
                    "evidence_completeness": completeness,
                    "atomicity": atomicity,
                    "confidence": verification_confidence,
                    "reason": _clean_text(decision.get("reason"), 500),
                }
                unsupported_single_behavior = (
                    literalness == "direct_behavior"
                    and observation.get("field_name")
                    in DIRECT_BEHAVIOR_REPETITION_FIELDS
                    and str(
                        (observation.get("context") or {}).get(
                            "evidence_strength"
                        )
                        or ""
                    ).strip().lower()
                    != "repeated_behavior"
                )
                if (
                    verdict == "supported"
                    and binding == "explicit"
                    and literalness in {"literal", "direct_behavior"}
                    and not unsupported_single_behavior
                    and completeness == "complete"
                    and atomicity == "atomic"
                    and verification_confidence >= 0.8
                ):
                    observation["confidence"] = min(
                        _safe_float(observation.get("confidence")),
                        verification_confidence,
                    )
                    verified.append(observation)
                elif verdict == "uncertain" and verification_confidence >= 0.7:
                    observation["quality_status"] = "quarantined"
                    observation["rejection_reason"] = (
                        "verification_uncertain: "
                        + _clean_text(decision.get("reason"), 500)
                    )
                    observation["confidence"] = min(
                        _safe_float(observation.get("confidence")),
                        0.5,
                    )
                    verified.append(observation)
                    verification_quarantined += 1
                else:
                    filtered_verification += 1

        accepted_object_ids = {
            id(observation)
            for observation in (pre_quarantined + verified)
        }
        accepted = [
            observation
            for observation in normalized
            if id(observation) in accepted_object_ids
        ]
        result = self.ledger.add_observations(
            chat_name,
            accepted,
            source_namespace=source_namespace,
            batch_key=batch_key,
        )
        result["observations"] = accepted
        result["quarantined"] = sum(
            1
            for observation in accepted
            if observation.get("quality_status") == "quarantined"
        )
        result["filtered_low_value"] = filtered_low_value
        result["filtered_weak_evidence"] = filtered_weak_evidence
        result["filtered_verification"] = filtered_verification
        result["verification_quarantined"] = verification_quarantined
        if int(target_person_id or 0) > 0 and batch_key:
            accepted_ids = {
                id(observation)
                for observation in accepted
            }
            candidate_results: Dict[str, Dict[str, Any]] = {
                str(candidate.get("candidate_id") or ""): {
                    "status": "rejected",
                    "reason": "未通过结构、长期价值或证据门槛",
                }
                for candidate in raw_candidates
                if str(candidate.get("candidate_id") or "")
            }
            for observation in normalized:
                candidate_id = str(
                    (observation.get("context") or {}).get(
                        "candidate_id"
                    )
                    or ""
                )
                if not candidate_id:
                    continue
                verification = dict(
                    (observation.get("context") or {}).get(
                        "verification"
                    )
                    or {}
                )
                if id(observation) in accepted_ids:
                    status = (
                        "verified"
                        if observation.get("quality_status") == "active"
                        else "quarantined"
                    )
                    reason = (
                        verification.get("reason")
                        or observation.get("rejection_reason")
                        or "通过人物观察流水线"
                    )
                else:
                    status = "rejected"
                    reason = (
                        verification.get("reason")
                        or "未通过独立证据核验"
                    )
                candidate_results[candidate_id] = {
                    "status": status,
                    "reason": reason,
                    "verification": verification,
                }
            self.ledger.update_claim_candidate_results(
                chat_name,
                batch_key,
                candidate_results,
            )
        return result

    def _fit_indexed_person_batch(
        self,
        batch: Dict[str, Any],
        *,
        input_token_budget: int,
    ) -> Dict[str, Any]:
        """Keep a contiguous prefix of person links plus nearby context."""

        messages_by_cursor = {
            _safe_int(message.get("_log_cursor")): dict(message)
            for message in batch.get("messages") or []
            if _safe_int(message.get("_log_cursor")) > 0
        }
        core_cursors = [
            _safe_int(value)
            for value in batch.get("core_cursors") or []
            if _safe_int(value) in messages_by_cursor
        ]
        link_ids = [
            _safe_int(value)
            for value in batch.get("link_ids") or []
            if _safe_int(value) > 0
        ]
        if not core_cursors or not link_ids:
            return {**batch, "messages": [], "core_cursors": [], "link_ids": []}
        budget = max(2000, int(input_token_budget))
        selected_core: List[int] = []
        selected_cursors: set[int] = set()
        used = 0
        for core_cursor in core_cursors:
            candidate_cursors = {
                cursor
                for cursor in range(
                    max(1, core_cursor - 2),
                    core_cursor + 3,
                )
                if cursor in messages_by_cursor
            }
            new_cursors = candidate_cursors - selected_cursors
            cost = sum(
                self.context_manager.estimate_message_tokens(
                    messages_by_cursor[cursor]
                )
                for cursor in new_cursors
            )
            if selected_core and used + cost > budget:
                break
            selected_core.append(core_cursor)
            selected_cursors.update(candidate_cursors)
            used += cost
            if used >= budget:
                break
        if not selected_core:
            selected_core = [core_cursors[0]]
            selected_cursors = {
                cursor
                for cursor in range(
                    max(1, core_cursors[0] - 2),
                    core_cursors[0] + 3,
                )
                if cursor in messages_by_cursor
            }
        selected_core_set = set(selected_core)
        selected_relations = [
            relation
            for relation in batch.get("relations") or []
            if _safe_int(relation.get("cursor")) in selected_core_set
        ]
        selected_link_ids = [
            _safe_int(relation.get("link_id"))
            for relation in selected_relations
            if _safe_int(relation.get("link_id")) > 0
        ]
        if not selected_link_ids:
            selected_link_ids = link_ids[: len(selected_core)]
        return {
            **batch,
            "messages": [
                messages_by_cursor[cursor]
                for cursor in sorted(selected_cursors)
            ],
            "core_cursors": selected_core,
            "link_ids": selected_link_ids,
            "relations": selected_relations,
        }

    def process_due_person_batches(
        self,
        chat_name: str,
        *,
        threshold: int = 30,
        stale_after_days: int = 0,
        stale_min_pending: int = 0,
        batch_size: int = 80,
        max_people: int = 4,
        input_token_budget: int = 24000,
        max_observations: int = 16,
        minimum_memory_value: float = 0.58,
        force: bool = False,
        excluded_sender_names: Optional[Iterable[str]] = None,
        excluded_sender_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        """Process independent person queues without a global group quota."""

        due = self.ledger.due_indexed_people(
            chat_name,
            threshold=threshold,
            stale_after_days=stale_after_days,
            stale_min_pending=stale_min_pending,
            limit=max_people,
            force=force,
        )
        results: List[Dict[str, Any]] = []
        totals = {
            "people_due": len(due),
            "people_processed": 0,
            "links_processed": 0,
            "inserted": 0,
            "quarantined": 0,
            "filtered_low_value": 0,
            "filtered_weak_evidence": 0,
            "filtered_verification": 0,
        }
        for state in due:
            person_id = int(state.get("person_id") or 0)
            namespace = str(state.get("source_namespace") or "")
            batch = self.ledger.next_indexed_person_batch(
                chat_name,
                person_id,
                namespace,
                limit=max(8, min(500, int(batch_size))),
                context_radius=2,
            )
            batch = self._fit_indexed_person_batch(
                batch,
                input_token_budget=input_token_budget,
            )
            core_cursors = list(batch.get("core_cursors") or [])
            link_ids = list(batch.get("link_ids") or [])
            messages = list(batch.get("messages") or [])
            if not core_cursors or not link_ids or not messages:
                continue
            batch_key = (
                f"person-centric:{namespace}:{person_id}:"
                f"{min(core_cursors)}:{max(core_cursors)}"
            )
            result = self.extract_observations(
                chat_name,
                messages,
                core_start_cursor=min(core_cursors),
                core_end_cursor=max(core_cursors),
                core_cursors=core_cursors,
                source_namespace=namespace,
                batch_key=batch_key,
                excluded_sender_names=excluded_sender_names,
                excluded_sender_ids=excluded_sender_ids,
                target_person_id=person_id,
                max_observations=max_observations,
                minimum_memory_value=minimum_memory_value,
            )
            if result is None:
                continue
            self.ledger.mark_indexed_person_batch_processed(
                chat_name,
                person_id,
                namespace,
                link_ids,
            )
            item = {
                **result,
                "person_id": person_id,
                "person_name": state.get("canonical_name"),
                "source_namespace": namespace,
                "links_processed": len(link_ids),
                "core_start_cursor": min(core_cursors),
                "core_end_cursor": max(core_cursors),
            }
            results.append(item)
            totals["people_processed"] += 1
            totals["links_processed"] += len(link_ids)
            for key in (
                "inserted",
                "quarantined",
                "filtered_low_value",
                "filtered_weak_evidence",
                "filtered_verification",
            ):
                totals[key] += int(result.get(key) or 0)
        totals["results"] = results
        return totals

    @staticmethod
    def _observation_prompt_item(observation: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": int(observation.get("id") or 0),
            "type": observation.get("observation_type"),
            "field": observation.get("field_name"),
            "statement": observation.get("statement"),
            "source_relation": observation.get("source_relation"),
            "epistemic_status": observation.get("epistemic_status"),
            "confidence": observation.get("confidence"),
            "valid_from": observation.get("valid_from"),
            "valid_to": observation.get("valid_to"),
            "observed_at": observation.get("observed_at"),
            "evidence_excerpt": observation.get("evidence_excerpt"),
            "sensitivity": observation.get("sensitivity"),
        }

    def _reject_unsupported_active_observations(
        self,
        chat_name: str,
        observations: Sequence[Dict[str, Any]],
    ) -> int:
        """Re-audit deterministic errors produced by an older extractor."""
        rejected = 0
        relative_pattern = re.compile(
            r"(?:父亲|母亲|爸爸|妈妈|老豆|老母|父母|"
            r"(?:大|二|三|四|阿)?姐|(?:大|二|三|四|阿)?妹|"
            r"(?:大|二|三|四|阿)?哥|(?:大|二|三|四|阿)?弟|"
            r"家姐|细佬|老婆|妻子|丈夫|女友|男友|"
            r"孩子|儿子|女儿|亲友)"
        )
        for observation in observations:
            if observation.get("source_relation") == "manual_admin":
                continue
            field_name = str(observation.get("field_name") or "")
            statement = str(observation.get("statement") or "")
            evidence_excerpt = [
                item
                for item in observation.get("evidence_excerpt") or []
                if isinstance(item, dict)
            ]
            reason = ""
            if _observation_adds_unsupported_specificity(
                field_name,
                statement,
                evidence_excerpt,
            ):
                reason = (
                    "自动复核：职业、单位、地点或教育细节未在引用原文中出现"
                )
            elif (
                field_name == "health"
                and relative_pattern.search(statement)
            ):
                reason = "自动复核：这是亲友健康信息，不属于目标人物本人"
            if not reason:
                continue
            try:
                self.ledger.review_observation(
                    chat_name,
                    int(observation["id"]),
                    quality_status="rejected",
                    reason=reason,
                )
                rejected += 1
            except Exception as exc:
                logger.warning(
                    "⚠️ Failed deterministic observation review %s/%s: %s",
                    chat_name,
                    observation.get("id"),
                    exc,
                )
        return rejected

    def _verify_projection(
        self,
        chat_name: str,
        person_id: int,
        projection: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Independently verify every derived profile item against observations."""
        candidates: List[Dict[str, Any]] = []
        source_items: Dict[str, Dict[str, Any]] = {}

        def add_candidate(
            item_id: str,
            kind: str,
            raw: Dict[str, Any],
            text: Any,
            *,
            section: str = "",
        ) -> None:
            value = dict(raw)
            evidence_ids = sorted(
                {
                    _safe_int(item)
                    for item in value.get("evidence_observation_ids") or []
                    if _safe_int(item) > 0
                }
            )
            normalized_text = _clean_text(text, 1000)
            if not normalized_text or not evidence_ids:
                return
            value["evidence_observation_ids"] = evidence_ids
            source_items[item_id] = value
            candidates.append(
                {
                    "item_id": item_id,
                    "kind": kind,
                    "section": section,
                    "field": value.get("field")
                    or value.get("field_name")
                    or value.get("type")
                    or value.get("pattern_type")
                    or "",
                    "slot_key": value.get("slot_key") or "",
                    "status": value.get("status")
                    or value.get("state")
                    or "",
                    "text": normalized_text,
                    "evidence_observation_ids": evidence_ids,
                }
            )

        for index, raw in enumerate(
            (projection.get("facts") or [])[:12],
            start=1,
        ):
            if isinstance(raw, dict):
                add_candidate(f"fact:{index}", "fact", raw, raw.get("value"))
        for index, raw in enumerate(
            (projection.get("patterns") or [])[:6],
            start=1,
        ):
            if isinstance(raw, dict):
                add_candidate(
                    f"pattern:{index}",
                    "pattern",
                    raw,
                    "；".join(
                        part
                        for part in (
                            _clean_text(raw.get("label"), 160),
                            _clean_text(raw.get("description"), 600),
                        )
                        if part
                    ),
                )
        for index, raw in enumerate(
            (projection.get("relationships") or [])[:5],
            start=1,
        ):
            if isinstance(raw, dict):
                add_candidate(
                    f"relationship:{index}",
                    "relationship",
                    raw,
                    "；".join(
                        part
                        for part in (
                            _clean_text(raw.get("target_name"), 120),
                            _clean_text(raw.get("description"), 600),
                        )
                        if part
                    ),
                )
        snapshot = (
            projection.get("snapshot")
            if isinstance(projection.get("snapshot"), dict)
            else {}
        )
        for section in SNAPSHOT_SECTIONS:
            for index, raw in enumerate(
                (snapshot.get(section) or [])[:20],
                start=1,
            ):
                if isinstance(raw, dict):
                    add_candidate(
                        f"snapshot:{section}:{index}",
                        "snapshot",
                        raw,
                        raw.get("text"),
                        section=section,
                    )
        if not candidates:
            return {
                "facts": [],
                "patterns": [],
                "relationships": [],
                "snapshot": {section: [] for section in SNAPSHOT_SECTIONS},
            }

        cited_ids = sorted(
            {
                observation_id
                for candidate in candidates
                for observation_id in candidate["evidence_observation_ids"]
            }
        )
        with self.store._connection() as connection:
            observations = self.ledger._verified_observations(
                connection,
                chat_name,
                person_id,
                cited_ids,
            )
        allowed_ids = {
            int(observation.get("id") or 0)
            for observation in observations
        }
        observation_by_id = {
            int(observation.get("id") or 0): observation
            for observation in observations
        }
        candidates = [
            candidate
            for candidate in candidates
            if set(candidate["evidence_observation_ids"]).issubset(allowed_ids)
        ]
        deterministic_rejections: set[str] = set()
        for candidate in candidates:
            item_id = str(candidate["item_id"])
            kind = str(candidate.get("kind") or "")
            raw_item = source_items.get(item_id) or {}
            field_name = str(candidate.get("field") or "").strip().lower()
            slot_key = str(candidate.get("slot_key") or "").strip().lower()
            status = str(candidate.get("status") or "").strip().lower()
            text_value = str(candidate.get("text") or "")
            if kind == "fact":
                if not _is_semantically_valid_fact_projection(
                    field_name,
                    slot_key,
                    text_value,
                ):
                    deterministic_rejections.add(item_id)
                if (
                    status == "current"
                    and field_name in {"experience", "plan"}
                ):
                    deterministic_rejections.add(item_id)
            elif kind == "relationship":
                target_name = _clean_text(
                    raw_item.get("target_name"),
                    120,
                )
                target = self.store.resolve_person(
                    chat_name,
                    target_name,
                )
                if (
                    target is None
                    or int(target.get("id") or 0) == int(person_id)
                    or target_name
                    != str(target.get("canonical_name") or "").strip()
                    or target_name.casefold()
                    in self.excluded_person_names
                ):
                    deterministic_rejections.add(item_id)
            cited = [
                observation_by_id[observation_id]
                for observation_id in candidate[
                    "evidence_observation_ids"
                ]
            ]
            high_impact_projection = (
                candidate.get("kind") == "fact"
                or (
                    candidate.get("kind") == "snapshot"
                    and candidate.get("section")
                    in {"current_snapshot", "timeline"}
                )
            )
            if (
                high_impact_projection
                and _projection_adds_unsupported_identity_term(
                    candidate.get("text"),
                    cited,
                )
            ) or any(
                observation.get("field_name") == "health"
                and re.search(
                    r"(?:父亲|母亲|爸爸|妈妈|老豆|老母|父母|"
                    r"(?:大|二|三|四|阿)?姐|(?:大|二|三|四|阿)?妹|"
                    r"(?:大|二|三|四|阿)?哥|(?:大|二|三|四|阿)?弟|"
                    r"家姐|细佬|老婆|妻子|丈夫|女友|男友|"
                    r"孩子|儿子|女儿|亲友)",
                    str(observation.get("statement") or ""),
                )
                for observation in cited
            ):
                deterministic_rejections.add(str(candidate["item_id"]))
        if not candidates:
            return {
                "facts": [],
                "patterns": [],
                "relationships": [],
                "snapshot": {section: [] for section in SNAPSHOT_SECTIONS},
            }
        auditable_candidates = [
            candidate
            for candidate in candidates
            if str(candidate["item_id"]) not in deterministic_rejections
        ]
        if not auditable_candidates:
            return {
                "facts": [],
                "patterns": [],
                "relationships": [],
                "snapshot": {section: [] for section in SNAPSHOT_SECTIONS},
            }
        prompt = [
            {
                "role": "system",
                "content": (
                    "你是人物画像的最终独立审计员。逐项检查派生结论是否被它引用的"
                    "observation 完整支持。不得用常识、相邻候选或没有被该项引用的观察"
                    "补全。以下任一情况必须 reject：增加了观察中没有的单位、地点、"
                    "身份、时间、因果或关系亲疏；把值班/培训/办公楼层当成职业或长期"
                    "地点；把一次动作写成长期兴趣；把历史/计划写成当前；把两个可独立"
                    "变化的事实塞进一个 fact；slot_key 或 field 与内容语义不匹配。"
                    "pattern 可以概括多条跨时间证据，relationship 可以概括多次互动，"
                    "它们不要求像原子事实一样逐字复述；若引用的多条已核验 observation"
                    "共同支持该模式或关系，应判 supported，只拒绝其中新增的具体事实、"
                    "过度亲疏或无证据因果。只输出JSON对象。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "已核验观察："
                    + _json_dump(
                        [
                            self._observation_prompt_item(observation)
                            for observation in observations
                        ]
                    )
                    + "\n\n待审计项目："
                    + _json_dump(
                        auditable_candidates
                    )
                    + "\n\n严格输出："
                    '{"verifications":[{"item_id":"fact:1",'
                    '"verdict":"supported|reject",'
                    '"field_alignment":"correct|wrong",'
                    '"temporal_alignment":"correct|wrong",'
                    '"atomicity":"atomic|compound|not_applicable",'
                    '"confidence":0.0,"reason":"简明原因"}]}'
                ),
            },
        ]
        try:
            payload = self.call_json(
                call_type="memory_person_projection_review",
                messages=prompt,
                schema_hint='根对象必须是 {"verifications":[...]}',
                chat_name=chat_name,
            )
        except Exception as exc:
            logger.warning(
                "⚠️ Person projection verification failed for %s/%s: %s",
                chat_name,
                person_id,
                exc,
            )
            return None
        rows = payload.get("verifications")
        if not isinstance(rows, list):
            return None
        decisions = {
            str(row.get("item_id") or ""): row
            for row in rows
            if isinstance(row, dict)
        }
        logger.info(
            "Person projection audit %s/%s: candidates=%s "
            "deterministic_rejected=%s decisions=%s",
            chat_name,
            person_id,
            len(candidates),
            len(deterministic_rejections),
            len(decisions),
        )

        def supported(item_id: str, kind: str) -> bool:
            if item_id in deterministic_rejections:
                return False
            decision = decisions.get(item_id) or {}
            atomicity = str(
                decision.get("atomicity") or "compound"
            ).strip().lower()
            return bool(
                str(decision.get("verdict") or "").strip().lower()
                == "supported"
                and str(
                    decision.get("field_alignment") or ""
                ).strip().lower()
                == "correct"
                and str(
                    decision.get("temporal_alignment") or ""
                ).strip().lower()
                == "correct"
                and _safe_float(decision.get("confidence")) >= 0.8
                and (
                    kind != "fact"
                    or atomicity == "atomic"
                )
            )

        result: Dict[str, Any] = {
            "facts": [],
            "patterns": [],
            "relationships": [],
            "snapshot": {section: [] for section in SNAPSHOT_SECTIONS},
        }
        kind_targets = {
            "fact": "facts",
            "pattern": "patterns",
            "relationship": "relationships",
        }
        for candidate in candidates:
            item_id = str(candidate["item_id"])
            kind = str(candidate["kind"])
            if not supported(item_id, kind):
                continue
            raw = source_items[item_id]
            if kind == "snapshot":
                result["snapshot"][str(candidate["section"])].append(raw)
            else:
                result[kind_targets[kind]].append(raw)
        current_fact_ids = {
            _safe_int(observation_id)
            for fact in result["facts"]
            if str(fact.get("status") or "").strip().lower() == "current"
            for observation_id in fact.get("evidence_observation_ids") or []
        }
        pattern_ids = {
            _safe_int(observation_id)
            for pattern in result["patterns"]
            for observation_id in pattern.get("evidence_observation_ids") or []
        }
        relationship_ids = {
            _safe_int(observation_id)
            for relationship in result["relationships"]
            for observation_id in relationship.get(
                "evidence_observation_ids"
            )
            or []
        }
        section_requirements = {
            "current_snapshot": current_fact_ids,
            "stable_traits": pattern_ids,
            "group_relationships": relationship_ids,
        }
        section_limits = {
            "current_snapshot": 6,
            "timeline": 12,
            "stable_traits": 6,
            "group_relationships": 4,
            "uncertain": 3,
        }
        for section, limit in section_limits.items():
            items = result["snapshot"][section]
            required_ids = section_requirements.get(section)
            if required_ids is not None:
                items = [
                    item
                    for item in items
                    if required_ids.intersection(
                        {
                            _safe_int(value)
                            for value in item.get(
                                "evidence_observation_ids"
                            )
                            or []
                        }
                    )
                ]
            result["snapshot"][section] = items[:limit]
        return result

    def consolidate_person(
        self,
        chat_name: str,
        person_id: int,
        *,
        force: bool = False,
        observation_limit: int = 400,
        minimum_pattern_span_days: int = 30,
    ) -> Optional[Dict[str, Any]]:
        identity = None
        for person in self.store.list_person_directory(chat_name):
            if int(person.get("person_id") or 0) == int(person_id):
                identity = person
                break
        if identity is None:
            return None
        observations = self.ledger.list_observations(
            chat_name,
            person_id=person_id,
            quality_status="active",
            limit=max(20, min(2000, int(observation_limit))),
            descending=True,
        )
        rejected_count = self._reject_unsupported_active_observations(
            chat_name,
            observations,
        )
        if rejected_count:
            logger.info(
                "Person observation re-audit %s/%s rejected %s item(s)",
                chat_name,
                person_id,
                rejected_count,
            )
            observations = self.ledger.list_observations(
                chat_name,
                person_id=person_id,
                quality_status="active",
                limit=max(20, min(2000, int(observation_limit))),
                descending=True,
            )
        observations.reverse()
        if not observations:
            return None
        existing_facts = self.ledger.list_current_facts(chat_name, person_id)
        existing_patterns = self.ledger.list_patterns(chat_name, person_id)
        existing_relationships = self.ledger.list_relationships(chat_name, person_id)
        period_summaries = self.ledger.list_period_summaries(chat_name, person_id)
        existing_snapshot = self.ledger.get_active_snapshot(
            chat_name,
            person_id,
        )
        material = {
            "as_of": datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
            "person": {
                "person_id": person_id,
                "name": identity.get("person_name"),
                "aliases": [
                    alias.get("alias_name")
                    for alias in identity.get("aliases") or []
                    if alias.get("status") == "confirmed"
                ],
            },
            "known_group_people": [
                {
                    "canonical_name": person.get("person_name"),
                    "aliases": [
                        alias.get("alias_name")
                        for alias in person.get("aliases") or []
                        if alias.get("status") == "confirmed"
                    ],
                }
                for person in self.store.list_person_directory(chat_name)
                if int(person.get("person_id") or 0) != int(person_id)
                and str(person.get("person_name") or "").strip().casefold()
                not in self.excluded_person_names
            ],
            "existing_facts": [
                {
                    "id": fact.get("id"),
                    "slot_key": fact.get("slot_key"),
                    "field": fact.get("field_name"),
                    "value": fact.get("value"),
                    "status": fact.get("status"),
                    "valid_from": fact.get("valid_from"),
                    "valid_to": fact.get("valid_to"),
                    "evidence_observation_ids": fact.get(
                        "evidence_observation_ids"
                    ),
                }
                for fact in existing_facts
            ],
            "existing_patterns": [
                {
                    "id": pattern.get("id"),
                    "type": pattern.get("pattern_type"),
                    "label": pattern.get("label"),
                    "state": pattern.get("state"),
                    "evidence_observation_ids": pattern.get(
                        "evidence_observation_ids"
                    ),
                }
                for pattern in existing_patterns
            ],
            "existing_relationships": [
                {
                    "id": relationship.get("id"),
                    "target_name": relationship.get("target_name"),
                    "type": relationship.get("relationship_type"),
                    "description": relationship.get("description"),
                    "status": relationship.get("status"),
                    "evidence_observation_ids": relationship.get(
                        "evidence_observation_ids"
                    ),
                }
                for relationship in existing_relationships
            ],
            "existing_snapshot": (
                {
                    "generation": existing_snapshot.get("generation"),
                    "sections": existing_snapshot.get("sections") or {},
                    "evidence_observation_ids": existing_snapshot.get(
                        "evidence_observation_ids"
                    )
                    or [],
                }
                if existing_snapshot is not None
                else {}
            ),
            "historical_period_summaries": [
                {
                    "period_key": item.get("period_key"),
                    "summary": item.get("summary"),
                    "evidence_observation_ids": item.get(
                        "evidence_observation_ids"
                    ),
                }
                for item in period_summaries
            ],
            "observations": [
                self._observation_prompt_item(observation)
                for observation in observations
            ],
        }
        prompt = [
            {
                "role": "system",
                "content": (
                    "你是人物长期记忆的证据聚合器。输入是已核对原消息的人物观察账本。"
                    "输出事实版本、跨时间稳定模式、明确关系和一个像长期相处后形成但仍可"
                    "审计的人物视图。任何输出项必须引用 observation ID；不得添加观察中"
                    "没有的信息。区分“现在是什么”“过去发生过什么”“计划”“不确定说法”。"
                    "这是增量合并，不是只看最近观察重新写人：existing_snapshot、"
                    "existing_facts、existing_patterns 和 existing_relationships 中仍被"
                    "证据支持且未被新观察取代的高价值内容应保留；只有出现更新、冲突、"
                    "过期或更强证据时才修订或降级。不得仅因旧观察没有进入本轮最近观察"
                    "窗口就删除既有结论。"
                    "输入中的as_of是快照基准时间；怀孕、待产、短期计划、临时状态等"
                    "lifecycle观察若距as_of超过180天且没有后续确认，不得放入当前概况，"
                    "只能进入历史时间线或待确认。"
                    "职业、单位、地点、当前状态等变化时使用同一 slot_key，使新值形成版本；"
                    "不得把多年历史并列成多个当前状态。性格、习惯、兴趣、关系亲疏只有至少"
                    "3个不同日期且跨度至少30天的独立观察才可 confirmed，否则 candidate。"
                    "提供收货/快递地址只证明当时使用过该地址，不等于当前居住地或工作地，"
                    "只能放入历史时间线。一次集中讨论或交易行为不等于长期兴趣；除非当事人"
                    "明确自述喜好，否则必须满足跨3个日期、30天的稳定性门槛。"
                    "收到录取、通过考试、完成调整、购买或乘坐等一次性动作必须进入历史"
                    "时间线，不能作为当前状态；职业、单位、地点等可变信息超过一年没有"
                    "后续确认时也只能作为最后已知历史。"
                    "优先保留能回答“他是谁、现在做什么、经历过哪些关键转折、长期喜欢"
                    "什么、在群里扮演什么角色”的信息。一次事故、处罚、投资输赢、普通"
                    "值班、短期待遇、单次消费和生活插曲通常只会制造噪声；除非它构成"
                    "明确人生转折，否则不要占用 facts 或快照名额。家庭、资产、健康等"
                    "高影响事实若只来自一条向机器人发出的夸张设定或称呼玩笑，必须排除。"
                    "current_snapshot 只能复述本次 facts 中 status=current 的原子事实，"
                    "不得把普通事件包装成当前身份。stable_traits 必须对应本次 patterns，"
                    "group_relationships 必须对应本次 relationships。relationship 的"
                    "target_name 必须严格使用 known_group_people 中的 canonical_name，"
                    "不得把别名拼接到姓名后，也不得把AI机器人列为人物关系。"
                    "一次性观点不等于稳定偏好，正常聊天互动不等于亲密关系。玩笑、反讽、"
                    "角色扮演和第三方未证实评价不得进入确定区。"
                    "快照不是流水账：当前概况最多6条、关键时间线最多12条、稳定特点"
                    "最多6条、群内角色与关系最多4条、不确定项最多3条；材料不足可以很少。"
                    "敏感信息必须保持原敏感级别。facts最多12条、patterns最多6条、"
                    "relationships最多5条；同一事实"
                    "不要在不同slot重复输出。"
                    "只输出JSON对象，不要代码块。"
                ),
            },
            {
                "role": "user",
                "content": (
                    _json_dump(material)
                    + "\n\n严格输出："
                    '{"facts":[{"slot_key":"例如occupation.primary",'
                    '"field":"固定字段","value":"原子事实",'
                    '"status":"current|historical|planned|uncertain|disputed",'
                    '"confidence":0.0,"valid_from":"","valid_to":"",'
                    '"observed_at":"","evidence_observation_ids":[1],'
                    '"priority":0.0,"sensitivity":"low|medium|high"}],'
                    '"patterns":[{"type":"trait|interest|preference|habit|skill|'
                    'group_role|communication_style","label":"短标签",'
                    '"description":"证据支持的描述",'
                    '"state":"candidate|confirmed|declining|disputed",'
                    '"confidence":0.0,"evidence_observation_ids":[1],'
                    '"sensitivity":"low|medium|high"}],'
                    '"relationships":[{"target_name":"对象",'
                    '"type":"family|friend|colleague|group_affinity|group_friction|'
                    'mentor|collaboration|other","description":"关系描述",'
                    '"status":"current|historical|uncertain|disputed",'
                    '"confidence":0.0,"evidence_observation_ids":[1],'
                    '"sensitivity":"low|medium|high"}],'
                    '"snapshot":{"current_snapshot":[{"text":"当前事实",'
                    '"evidence_observation_ids":[1],"valid_from":"",'
                    '"valid_to":"","confidence":0.0,"sensitivity":"low"}],'
                    '"timeline":[],"stable_traits":[],"group_relationships":[],'
                    '"uncertain":[]}}'
                ),
            },
        ]
        try:
            projection = self.call_json(
                call_type="memory_person_consolidate",
                messages=prompt,
                schema_hint=(
                    "根对象必须包含 facts、patterns、relationships、snapshot"
                ),
                chat_name=chat_name,
            )
        except Exception as exc:
            logger.warning(
                "⚠️ Person consolidation failed for %s/%s: %s",
                chat_name,
                person_id,
                exc,
            )
            return None
        projection = self._verify_projection(
            chat_name,
            person_id,
            projection,
        )
        if projection is None:
            return None
        source_max = max(int(observation["id"]) for observation in observations)
        applied = self.ledger.apply_projection(
            chat_name,
            person_id,
            projection,
            source_observation_max_id=source_max,
            minimum_pattern_days=3,
            minimum_pattern_span_days=minimum_pattern_span_days,
            generator_version="person-memory.1",
        )
        return {
            **applied,
            "person_id": person_id,
            "person_name": identity.get("person_name"),
            "source_observation_max_id": source_max,
        }

    def refresh_due_people(
        self,
        chat_name: str,
        *,
        threshold: int = 10,
        stale_after_days: int = 0,
        force: bool = False,
        limit: int = 8,
    ) -> Dict[str, Any]:
        due = self.ledger.due_people(
            chat_name,
            threshold=threshold,
            stale_after_days=stale_after_days,
            force=force,
            limit=limit,
        )
        results = []
        for person in due:
            result = self.consolidate_person(
                chat_name,
                int(person["person_id"]),
                force=force,
            )
            if result is not None:
                results.append(result)
        return {
            "people_due": len(due),
            "people_refreshed": len(results),
            "results": results,
        }

    def select_profiles_for_query(
        self,
        chat_name: str,
        *,
        sender: str,
        content: str,
        event_participants: Iterable[str] = (),
        maximum_people: int = 3,
    ) -> List[Dict[str, Any]]:
        state = self.ledger.get_chat_state(chat_name)
        if state.get("mode") != "active":
            return []
        query = str(content or "")
        self_referential = bool(
            re.search(
                r"(?:^|[\s，。！？；：、,.!?;:])"
                r"(?:我|我家|我的|本人|俺|咱)(?:$|[\s，。！？；：、,.!?;:]|们|家|的|在|是|有|想|要|会|曾|现)",
                query,
            )
        )
        participants = {str(value or "").strip() for value in event_participants}
        selected = []
        for profile in self.ledger.list_profiles(chat_name, include_building=False):
            aliases = {
                str(alias.get("alias_name") or "").strip()
                for alias in profile.get("aliases") or []
                if alias.get("status") == "confirmed"
            }
            aliases.add(str(profile.get("person_name") or ""))
            reasons = []
            matched = sorted(
                {alias for alias in aliases if alias and alias in query},
                key=len,
                reverse=True,
            )
            if matched:
                reasons.append("问题中提及")
            if aliases & participants:
                reasons.append("相关事件参与者")
            if sender in aliases and self_referential:
                reasons.append("本轮发送者")
            if not reasons:
                continue
            value = dict(profile)
            value["selection_reasons"] = reasons
            value["matched_aliases"] = matched
            selected.append(value)
        selected.sort(
            key=lambda item: (
                "问题中提及" in item.get("selection_reasons", []),
                "相关事件参与者" in item.get("selection_reasons", []),
                "本轮发送者" in item.get("selection_reasons", []),
                int(item.get("observation_count") or 0),
            ),
            reverse=True,
        )
        return selected[: max(1, min(6, int(maximum_people)))]

    @staticmethod
    def render_profile_for_query(
        profile: Dict[str, Any],
        query_text: str,
        *,
        maximum_items: int = 12,
        include_high_sensitivity: bool = False,
    ) -> str:
        sections = profile.get("sections") or {}
        query_tokens: set[str] = set()
        stop_terms = {
            "什么",
            "怎么",
            "怎样",
            "如何",
            "这个",
            "那个",
            "现在",
            "以前",
            "一下",
            "知道",
            "说说",
        }
        for token in re.split(
            r"[\s，。！？；：、,.!?;:（）()\[\]【】]+",
            query_text,
        ):
            value = token.strip()
            if len(value) < 2:
                continue
            query_tokens.add(value)
            if re.search(r"[\u3400-\u9fff]", value):
                for size in (2, 3, 4):
                    query_tokens.update(
                        value[index : index + size]
                        for index in range(len(value) - size + 1)
                    )
        query_tokens.difference_update(stop_terms)
        semantic_groups = (
            {
                "学校",
                "大学",
                "学院",
                "高中",
                "学历",
                "毕业",
                "读书",
                "教育",
                "证书",
            },
            {"工作", "职业", "单位", "上班", "任职", "公务员", "事业编"},
            {"住址", "居住", "住在", "地点", "哪里", "哪儿", "工作地"},
            {"兴趣", "爱好", "喜欢", "偏好", "习惯"},
            {"朋友", "关系", "认识", "熟悉", "同事", "亲友"},
            {"投资", "股票", "炒股", "持仓", "亏损", "盈利"},
            {"游戏", "电竞", "王者荣耀", "英雄联盟", "开黑", "段位"},
        )
        for group in semantic_groups:
            if any(term in query_text for term in group):
                query_tokens.update(group)
        candidates: List[tuple[float, str, Dict[str, Any]]] = []

        def relevance_for(text: str) -> float:
            return sum(
                min(2.0, len(token) / 2.0)
                for token in query_tokens
                if token in text
            )

        section_weights = {
            "current_snapshot": 4.0,
            "stable_traits": 3.0,
            "group_relationships": 2.5,
            "timeline": 2.0,
            "uncertain": 0.5,
        }
        for section, weight in section_weights.items():
            if section == "stable_traits" and profile.get("patterns"):
                continue
            if (
                section == "group_relationships"
                and profile.get("relationships")
            ):
                continue
            for index, item in enumerate(sections.get(section) or []):
                if (
                    item.get("sensitivity") == "high"
                    and not include_high_sensitivity
                ):
                    continue
                text = str(item.get("text") or "")
                relevance = relevance_for(text)
                if section == "timeline" and query_tokens and relevance <= 0:
                    continue
                if section == "uncertain" and relevance <= 0:
                    continue
                candidates.append(
                    (
                        weight + relevance * 3.0 - index * 0.02,
                        section,
                        item,
                    )
                )

        for index, fact in enumerate(profile.get("facts") or []):
            if not isinstance(fact, dict) or not fact.get("value"):
                continue
            if (
                fact.get("sensitivity") == "high"
                and not include_high_sensitivity
            ):
                continue
            status = str(fact.get("status") or "uncertain")
            text = str(fact.get("value") or "")
            relevance = relevance_for(
                " ".join(
                    [
                        str(fact.get("field_name") or ""),
                        str(fact.get("slot_key") or ""),
                        text,
                    ]
                )
            )
            if status in {"historical", "uncertain", "disputed"}:
                if query_tokens and relevance <= 0:
                    continue
            weight = {
                "current": 4.2,
                "planned": 3.4,
                "historical": 1.9,
                "uncertain": 0.6,
                "disputed": 0.4,
            }.get(status, 0.6)
            label = {
                "current": "当前",
                "planned": "计划",
                "historical": "经历",
                "uncertain": "待确认",
                "disputed": "有争议",
            }.get(status, "事实")
            candidates.append(
                (
                    weight + relevance * 3.0 - index * 0.01,
                    label,
                    {
                        "text": text,
                        "valid_from": fact.get("valid_from")
                        or fact.get("observed_at")
                        or "",
                    },
                )
            )

        for index, pattern in enumerate(profile.get("patterns") or []):
            if not isinstance(pattern, dict) or not pattern.get("label"):
                continue
            if (
                pattern.get("sensitivity") == "high"
                and not include_high_sensitivity
            ):
                continue
            text = "：".join(
                part
                for part in (
                    str(pattern.get("label") or ""),
                    str(pattern.get("description") or ""),
                )
                if part
            )
            relevance = relevance_for(text)
            state = str(pattern.get("state") or "candidate")
            weight = 3.1 if state == "confirmed" else 1.2
            candidates.append(
                (
                    weight + relevance * 3.0 - index * 0.01,
                    "长期" if state == "confirmed" else "待观察",
                    {"text": text},
                )
            )

        for index, relationship in enumerate(
            profile.get("relationships") or []
        ):
            if (
                not isinstance(relationship, dict)
                or not relationship.get("target_name")
            ):
                continue
            if (
                relationship.get("sensitivity") == "high"
                and not include_high_sensitivity
            ):
                continue
            text = "：".join(
                part
                for part in (
                    str(relationship.get("target_name") or ""),
                    str(relationship.get("description") or ""),
                )
                if part
            )
            relevance = relevance_for(text)
            status = str(relationship.get("status") or "uncertain")
            if status in {"historical", "uncertain", "disputed"}:
                if query_tokens and relevance <= 0:
                    continue
            weight = 2.6 if status == "current" else 1.0
            candidates.append(
                (
                    weight + relevance * 3.0 - index * 0.01,
                    "群内" if status == "current" else "历史关系",
                    {"text": text},
                )
            )

        candidates.sort(key=lambda value: value[0], reverse=True)
        selected = []
        seen_texts: List[str] = []
        for score, label, item in candidates:
            normalized = _normalize_text(item.get("text"))
            if not normalized or any(
                normalized == seen
                or (
                    len(normalized) >= 8
                    and len(seen) >= 8
                    and (normalized in seen or seen in normalized)
                )
                for seen in seen_texts
            ):
                continue
            selected.append((score, label, item))
            seen_texts.append(normalized)
            if len(selected) >= max(1, min(20, int(maximum_items))):
                break
        if not selected:
            return ""
        aliases = [
            alias.get("alias_name")
            for alias in profile.get("aliases") or []
            if alias.get("status") == "confirmed"
            and alias.get("alias_name") != profile.get("person_name")
        ][:5]
        lines = [
            f"- {profile.get('person_name') or '未知人物'}"
            + (f"（别名：{'、'.join(aliases)}）" if aliases else "")
        ]
        labels = {
            "current_snapshot": "当前",
            "timeline": "经历",
            "stable_traits": "长期",
            "group_relationships": "群内",
            "uncertain": "待确认",
        }
        for _, section, item in selected:
            time_text = item.get("valid_from") or ""
            suffix = f"；{time_text}" if time_text else ""
            lines.append(
                f"  · [{labels.get(section, section)}] "
                f"{item.get('text') or ''}{suffix}"
            )
        return "\n".join(lines)
