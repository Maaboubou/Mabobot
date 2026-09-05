"""Event-based, retrieval-first memory for the core Assistant."""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from app.assistant.context_manager import ChatContextManager
from app.assistant.embedding_service import LocalEmbeddingService
from app.assistant.memory_store import MemoryStore
from app.assistant.memory_output_schemas import (
    codex_memory_output_schema,
)
from app.assistant.memory_scheduler import MemoryBackgroundScheduler
from app.assistant.person_memory import (
    LIVE_PERSON_SOURCE_NAMESPACE,
    PersonMemoryEngine,
)
from app.services.llm_manager import get_llm_manager

logger = logging.getLogger(__name__)


class ChatMemoryService:
    """Create event cards asynchronously and retrieve only relevant memory."""

    def __init__(
        self,
        chat_log_manager,
        context_manager: ChatContextManager,
        *,
        store: Optional[MemoryStore] = None,
        embedding_service: Optional[LocalEmbeddingService] = None,
        llm_manager: Any = None,
        llm_history_chat_name: str = "",
        llm_history_mode: str = "full",
        llm_usage_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.chat_log_manager = chat_log_manager
        self.context_manager = context_manager
        self.store = store or MemoryStore()
        self.embedding_service = embedding_service or LocalEmbeddingService()
        self.llm_manager = llm_manager
        self.llm_history_chat_name = str(llm_history_chat_name or "").strip()
        self.llm_history_mode = str(llm_history_mode or "full").strip().lower()
        self.llm_usage_callback = llm_usage_callback
        self._scheduler = MemoryBackgroundScheduler(max_workers=2)
        self._chat_locks_guard = threading.Lock()
        self._chat_locks: Dict[str, threading.RLock] = {}
        self._retrieval_cache_lock = threading.Lock()
        self._retrieval_cache: OrderedDict[
            str,
            Tuple[
                int,
                int,
                List[Dict[str, Any]],
                Dict[int, Tuple[np.ndarray, List[int]]],
            ],
        ] = OrderedDict()
        self._retrieval_cache_max_chats = 16
        self._automation_stop = threading.Event()
        self._automation_thread: Optional[threading.Thread] = None
        self._automation_lock = threading.Lock()
        self.person_memory = PersonMemoryEngine(
            self.store,
            self.context_manager,
            self._call_memory_json,
        )
        self._closed = False

    def _lock_for(self, chat_name: str) -> threading.RLock:
        with self._chat_locks_guard:
            lock = self._chat_locks.get(chat_name)
            if lock is None:
                lock = threading.RLock()
                self._chat_locks[chat_name] = lock
            return lock

    def close(self) -> None:
        self._closed = True
        self._automation_stop.set()
        thread = self._automation_thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)
        self._scheduler.close()

    def start_automatic_maintenance(
        self,
        chat_names_provider: Callable[[], Iterable[str]],
        config_provider: Callable[[str], Dict[str, Any]],
        *,
        poll_minutes: int = 15,
        initial_delay_seconds: float = 30.0,
    ) -> bool:
        """Continuously resume durable person queues without user activity.

        Event and stage thresholds remain owned by their normal due checks.  A
        periodic scan only makes already-due work independent of a new message.
        """
        with self._automation_lock:
            if self._closed:
                return False
            if self._automation_thread is not None:
                return False
            base_wait = max(60.0, min(86400.0, float(poll_minutes) * 60.0))

            def worker() -> None:
                wait_seconds = max(0.0, float(initial_delay_seconds))
                while not self._automation_stop.wait(wait_seconds):
                    next_wait = base_wait
                    try:
                        chat_names = list(
                            dict.fromkeys(
                                str(name or "").strip()
                                for name in chat_names_provider()
                                if str(name or "").strip()
                            )
                        )
                    except Exception:
                        logger.exception(
                            "⚠️ Failed to enumerate chats for memory automation"
                        )
                        chat_names = []
                    for chat_name in chat_names:
                        if self._automation_stop.is_set() or self._closed:
                            break
                        try:
                            config = dict(config_provider(chat_name) or {})
                            configured_wait = max(
                                60.0,
                                min(
                                    86400.0,
                                    float(
                                        config.get(
                                            "memory_automation_poll_minutes",
                                            poll_minutes,
                                        )
                                        or poll_minutes
                                    )
                                    * 60.0,
                                ),
                            )
                            next_wait = min(next_wait, configured_wait)
                            self.schedule(chat_name, config)
                        except Exception:
                            logger.exception(
                                "⚠️ Automatic memory scan failed for %s",
                                chat_name,
                            )
                    wait_seconds = next_wait

            thread = threading.Thread(
                target=worker,
                name="chat-memory-automation",
                daemon=True,
            )
            self._automation_thread = thread
            thread.start()
            return True

    def _configure_embedding(self, config: Dict[str, Any]) -> None:
        model_name = str(
            config.get("memory_embedding_model")
            or "BAAI/bge-small-zh-v1.5"
        )
        threads = max(1, min(8, int(config.get("memory_embedding_threads") or 4)))
        if (
            self.embedding_service.model_name == model_name
            and self.embedding_service.threads == threads
        ):
            return
        if self.embedding_service.ready:
            logger.warning(
                "Embedding configuration changed while the old model is resident; "
                "the new model will be used after plugin reload"
            )
            return
        self.embedding_service = LocalEmbeddingService(
            model_name=model_name,
            threads=threads,
        )

    def schedule(self, chat_name: str, config: Dict[str, Any]) -> bool:
        """Schedule bounded background ingestion without blocking a reply."""
        if (
            self._closed
            or not chat_name
            or not config.get("memory_enabled", True)
        ):
            return False
        self._configure_embedding(config)
        event_due = (
            config.get("memory_background_enabled", True)
            and self._event_ingestion_is_due(chat_name, config)
        )
        person_due = (
            config.get("memory_background_enabled", True)
            and config.get("memory_person_enabled", True)
            and self._person_ingestion_is_due(chat_name, config)
        )
        embedding_due = (
            config.get("memory_embedding_enabled", True)
            and self.embedding_service.can_attempt_load
        )
        maintenance_due = self.store.maintenance_is_due(
            chat_name,
            interval_hours=max(
                1,
                int(config.get("memory_maintenance_interval_hours") or 24),
            ),
        )
        if not any((event_due, person_due, embedding_due, maintenance_due)):
            return False

        config_copy = dict(config)

        def worker() -> None:
            if event_due or person_due or maintenance_due:
                self.process_pending(chat_name, config_copy)
            elif self.embedding_service.warmup():
                self._embed_missing_events(
                    chat_name,
                    limit=max(
                        1,
                        int(config_copy.get("memory_embedding_batch_size") or 8) * 2,
                    ),
                )
                self.invalidate(chat_name)

        return self._scheduler.submit(chat_name, worker, logger=logger)

    def _initialize_state_if_needed(
        self,
        chat_name: str,
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        state = self.store.get_state(chat_name)
        physical_count = int(self.chat_log_manager.count_log_messages(chat_name) or 0)
        cumulative_count = int(
            self.chat_log_manager.count_messages(chat_name) or physical_count
        )
        retained_floor = max(0, cumulative_count - physical_count)
        if (
            state.get("source_cursor", 0) == 0
            and state.get("source_message_count", 0) == 0
            and physical_count > 0
        ):
            backfill = max(
                0,
                int(config.get("memory_initial_backfill_messages") or 0),
            )
            kept = min(physical_count, backfill)
            state = self.store.initialize_cursor(
                chat_name,
                source_cursor=max(0, cumulative_count - kept),
                source_message_count=max(0, cumulative_count - kept),
            )
        else:
            # Older versions persisted a physical JSONL line number in
            # source_cursor and a cumulative count in source_message_count.
            # The cumulative value is the only one that survives truncation.
            durable_cursor = int(state.get("source_message_count") or 0)
            if durable_cursor < retained_floor or durable_cursor > cumulative_count:
                durable_cursor = retained_floor
            if (
                int(state.get("source_cursor") or 0) != durable_cursor
                or int(state.get("source_message_count") or 0) != durable_cursor
            ):
                state = self.store.set_ingestion_cursor(
                    chat_name,
                    source_cursor=durable_cursor,
                    source_message_count=durable_cursor,
                )
        return state

    def _available_messages(
        self,
        chat_name: str,
        state: Dict[str, Any],
    ) -> Tuple[int, int, int, int]:
        physical_count = int(self.chat_log_manager.count_log_messages(chat_name) or 0)
        cumulative_count = int(
            self.chat_log_manager.count_messages(chat_name) or physical_count
        )
        retained_floor = max(0, cumulative_count - physical_count)
        state_cursor = int(state.get("source_cursor") or 0)
        start_cursor = min(cumulative_count, max(retained_floor, state_cursor))
        available = min(physical_count, max(0, cumulative_count - start_cursor))
        return physical_count, cumulative_count, available, start_cursor

    def _event_ingestion_is_due(self, chat_name: str, config: Dict[str, Any]) -> bool:
        state = self._initialize_state_if_needed(chat_name, config)
        _, _, available, _ = self._available_messages(chat_name, state)
        target = max(
            int(config.get("memory_event_min_messages") or 20),
            int(config.get("memory_event_target_messages") or 40),
        )
        lookahead = max(
            0,
            int(config.get("memory_event_context_after_messages") or 12),
        )
        return available >= target + lookahead

    def _initialize_person_state_if_needed(
        self,
        chat_name: str,
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        state = self.person_memory.ledger.ensure_chat_state(
            chat_name,
            source_namespace=LIVE_PERSON_SOURCE_NAMESPACE,
        )
        physical_count = int(self.chat_log_manager.count_log_messages(chat_name) or 0)
        cumulative_count = int(
            self.chat_log_manager.count_messages(chat_name) or physical_count
        )
        retained_floor = max(0, cumulative_count - physical_count)
        if state.get("source_namespace") != LIVE_PERSON_SOURCE_NAMESPACE:
            # Historical imports and the old physical-cursor live stream used
            # incompatible cursor spaces. Start the durable live namespace at
            # the oldest retained row so no available live message is lost.
            state = self.person_memory.ledger.set_ingestion_cursor(
                chat_name,
                source_cursor=retained_floor,
                source_message_count=retained_floor,
                monotonic=False,
                source_namespace=LIVE_PERSON_SOURCE_NAMESPACE,
            )
        elif (
            int(state.get("ingestion_cursor") or 0) == 0
            and int(state.get("ingestion_message_count") or 0) == 0
            and physical_count > 0
        ):
            backfill = max(
                0,
                int(config.get("memory_initial_backfill_messages") or 0),
            )
            kept = min(physical_count, backfill)
            state = self.person_memory.ledger.set_ingestion_cursor(
                chat_name,
                source_cursor=max(0, cumulative_count - kept),
                source_message_count=max(0, cumulative_count - kept),
                monotonic=False,
                source_namespace=LIVE_PERSON_SOURCE_NAMESPACE,
            )
        else:
            durable_cursor = int(state.get("ingestion_message_count") or 0)
            if durable_cursor < retained_floor or durable_cursor > cumulative_count:
                durable_cursor = retained_floor
            if (
                int(state.get("ingestion_cursor") or 0) != durable_cursor
                or int(state.get("ingestion_message_count") or 0) != durable_cursor
            ):
                state = self.person_memory.ledger.set_ingestion_cursor(
                    chat_name,
                    source_cursor=durable_cursor,
                    source_message_count=durable_cursor,
                    monotonic=False,
                    source_namespace=LIVE_PERSON_SOURCE_NAMESPACE,
                )
        return state

    def _person_available_messages(
        self,
        chat_name: str,
        state: Dict[str, Any],
    ) -> Tuple[int, int, int, int]:
        physical_count = int(self.chat_log_manager.count_log_messages(chat_name) or 0)
        cumulative_count = int(
            self.chat_log_manager.count_messages(chat_name) or physical_count
        )
        retained_floor = max(0, cumulative_count - physical_count)
        state_cursor = int(state.get("ingestion_cursor") or 0)
        start_cursor = min(cumulative_count, max(retained_floor, state_cursor))
        available = min(physical_count, max(0, cumulative_count - start_cursor))
        return physical_count, cumulative_count, available, start_cursor

    def _person_ingestion_is_due(
        self,
        chat_name: str,
        config: Dict[str, Any],
    ) -> bool:
        state = self._initialize_person_state_if_needed(chat_name, config)
        _, _, available, _ = self._person_available_messages(chat_name, state)
        target = max(
            5,
            int(config.get("memory_person_index_target_messages") or 20),
        )
        if available >= target:
            return True

        # Person extraction and projection have their own durable queues.  They
        # must keep moving even when the group has not produced another full
        # raw-message indexing batch.
        pending_people = self.person_memory.ledger.due_indexed_people(
            chat_name,
            threshold=max(
                1,
                int(config.get("memory_person_min_pending_messages") or 30),
            ),
            stale_after_days=max(
                1,
                int(config.get("memory_person_stale_pending_days") or 14),
            ),
            stale_min_pending=max(
                1,
                int(
                    config.get("memory_person_stale_pending_min_messages")
                    or 8
                ),
            ),
            limit=1,
        )
        if pending_people:
            return True
        return bool(
            self.person_memory.ledger.due_people(
                chat_name,
                threshold=max(
                    1,
                    int(config.get("memory_person_refresh_threshold") or 10),
                ),
                stale_after_days=max(
                    1,
                    int(
                        config.get("memory_person_refresh_max_age_days")
                        or 7
                    ),
                ),
                limit=1,
            )
        )

    def _index_person_pending_messages(
        self,
        chat_name: str,
        config: Dict[str, Any],
        *,
        force_tail: bool,
    ) -> Dict[str, int]:
        if not config.get("memory_person_enabled", True):
            return {"chunks": 0, "messages": 0, "links": 0}

        target = max(
            5,
            int(config.get("memory_person_index_target_messages") or 20),
        )
        batch_limit = max(
            target,
            min(
                500,
                int(config.get("memory_person_batch_related_messages") or 80) * 2,
            ),
        )
        max_chunks = max(
            1,
            min(20, int(config.get("memory_max_chunks_per_run") or 3)),
        )
        totals = {"chunks": 0, "messages": 0, "links": 0}
        for _ in range(max_chunks):
            state = self._initialize_person_state_if_needed(chat_name, config)
            _, cumulative_count, available, start_cursor = (
                self._person_available_messages(chat_name, state)
            )
            if available <= 0 or (available < target and not force_tail):
                break
            requested = min(available, batch_limit)
            end_cursor = start_cursor + requested
            messages = self.chat_log_manager.get_messages_after_sequence(
                chat_name,
                after_sequence=start_cursor,
                through_sequence=end_cursor,
                limit=max(1, requested),
            )
            selected = [
                dict(message)
                for message in messages
                if start_cursor < int(message.get("_log_cursor") or 0) <= end_cursor
            ]
            if not selected:
                break
            selected_end = max(
                int(message.get("_log_cursor") or 0) for message in selected
            )
            self.store.observe_message_identities(
                chat_name,
                selected,
                source=(
                    "historical_message"
                    if any(message.get("sender_id") for message in selected)
                    else "live_message"
                ),
            )
            indexed = self.person_memory.ledger.index_person_messages(
                chat_name,
                selected,
                source_namespace=LIVE_PERSON_SOURCE_NAMESPACE,
                core_cursors=[
                    int(message.get("_log_cursor") or 0) for message in selected
                ],
                excluded_sender_names=(
                    config.get("memory_person_excluded_sender_names") or []
                ),
                excluded_sender_ids=(
                    config.get("memory_person_excluded_sender_ids") or []
                ),
            )
            self.person_memory.ledger.set_ingestion_cursor(
                chat_name,
                source_cursor=selected_end,
                source_message_count=selected_end,
                source_namespace=LIVE_PERSON_SOURCE_NAMESPACE,
            )
            totals["chunks"] += 1
            totals["messages"] += len(selected)
            totals["links"] += int(indexed.get("links") or 0)
        return totals

    def process_pending(
        self,
        chat_name: str,
        config: Dict[str, Any],
        *,
        force_tail: bool = False,
    ) -> Dict[str, int]:
        """Process a bounded number of chunks. Public for admin/tests."""
        if not config.get("memory_enabled", True):
            return {"chunks": 0, "events": 0, "embedded": 0, "stage": 0}

        with self._lock_for(chat_name):
            max_chunks = max(1, min(20, int(config.get("memory_max_chunks_per_run") or 3)))
            chunks = 0
            created_events = 0
            person_observations = 0
            person_quarantined = 0
            for _ in range(max_chunks):
                result = self._process_one_chunk(
                    chat_name,
                    config,
                    force_tail=force_tail,
                )
                if result is None:
                    break
                chunks += 1
                created_events += result

            person_index = self._index_person_pending_messages(
                chat_name,
                config,
                force_tail=force_tail,
            )
            person_links_indexed = int(person_index.get("links") or 0)
            identity_merge = {
                "candidates": 0,
                "merged": 0,
                "items": [],
                "skipped": [],
            }
            if (
                config.get("memory_person_enabled", True)
                and config.get(
                    "memory_person_auto_merge_stable_identities",
                    True,
                )
            ):
                identity_merge = (
                    self.store.auto_merge_stable_identity_duplicates(
                        chat_name,
                        limit=2,
                    )
                )

            person_batches = {
                "people_due": 0,
                "people_processed": 0,
                "links_processed": 0,
                "inserted": 0,
                "quarantined": 0,
                "results": [],
            }
            if config.get("memory_person_enabled", True):
                person_batches = (
                    self.person_memory.process_due_person_batches(
                        chat_name,
                        threshold=max(
                            1,
                            int(
                                config.get(
                                    "memory_person_min_pending_messages",
                                    30,
                                )
                                or 30
                            ),
                        ),
                        stale_after_days=max(
                            1,
                            int(
                                config.get(
                                    "memory_person_stale_pending_days",
                                    14,
                                )
                                or 14
                            ),
                        ),
                        stale_min_pending=max(
                            1,
                            int(
                                config.get(
                                    "memory_person_stale_pending_min_messages",
                                    8,
                                )
                                or 8
                            ),
                        ),
                        batch_size=max(
                            8,
                            int(
                                config.get(
                                    "memory_person_batch_related_messages",
                                    80,
                                )
                                or 80
                            ),
                        ),
                        max_people=max(
                            1,
                            min(
                                20,
                                int(
                                    config.get(
                                        "memory_person_max_batch_people",
                                        4,
                                    )
                                    or 4
                                ),
                            ),
                        ),
                        input_token_budget=max(
                            4000,
                            int(
                                config.get(
                                    "memory_person_input_token_budget",
                                    24000,
                                )
                                or 24000
                            ),
                        ),
                        max_observations=max(
                            4,
                            min(
                                30,
                                int(
                                    config.get(
                                        "memory_person_max_observations_per_batch",
                                        16,
                                    )
                                    or 16
                                ),
                            ),
                        ),
                        minimum_memory_value=max(
                            0.35,
                            min(
                                0.95,
                                float(
                                    config.get(
                                        "memory_person_candidate_memory_value",
                                        0.58,
                                    )
                                    or 0.58
                                ),
                            ),
                        ),
                        force=force_tail,
                        excluded_sender_names=(
                            config.get(
                                "memory_person_excluded_sender_names"
                            )
                            or []
                        ),
                        excluded_sender_ids=(
                            config.get(
                                "memory_person_excluded_sender_ids"
                            )
                            or []
                        ),
                    )
                )
                person_observations += int(
                    person_batches.get("inserted") or 0
                )
                person_quarantined += int(
                    person_batches.get("quarantined") or 0
                )

            embedded = 0
            if config.get("memory_embedding_enabled", True):
                embedded = self._embed_missing_events(
                    chat_name,
                    limit=max(
                        1,
                        int(config.get("memory_embedding_batch_size") or 8) * 2,
                    ),
                )
            stage_updated = int(self._refresh_stage_if_due(chat_name, config))
            person_refresh = {
                "people_due": 0,
                "people_refreshed": 0,
                "results": [],
            }
            if config.get("memory_person_enabled", False):
                person_refresh = self.person_memory.refresh_due_people(
                    chat_name,
                    threshold=max(
                        1,
                        int(
                            config.get(
                                "memory_person_refresh_threshold",
                                10,
                            )
                            or 10
                        ),
                    ),
                    stale_after_days=max(
                        1,
                        int(
                            config.get(
                                "memory_person_refresh_max_age_days",
                                7,
                            )
                            or 7
                        ),
                    ),
                    limit=max(
                        1,
                        min(
                            20,
                            int(
                                config.get(
                                    "memory_person_max_refresh_people",
                                    4,
                                )
                                or 4
                            ),
                        ),
                    ),
                )
                if int(person_refresh.get("people_refreshed") or 0) > 0:
                    person_state = self.person_memory.ledger.get_chat_state(chat_name)
                    if (
                        person_state.get("mode") == "building"
                        and person_state.get("source_namespace")
                        == LIVE_PERSON_SOURCE_NAMESPACE
                    ):
                        self.person_memory.ledger.set_chat_mode(chat_name, "active")
            maintenance = self.store.maybe_prune_transient_candidates(
                chat_name,
                rejected_older_than_days=max(
                    7,
                    int(config.get("memory_candidate_retention_days") or 90),
                ),
                interval_hours=max(
                    1,
                    int(config.get("memory_maintenance_interval_hours") or 24),
                ),
            )
            integrity = {"ok": True, "checks": {}}
            if not maintenance.get("skipped"):
                integrity = self.store.integrity_report(chat_name)
                if not integrity.get("ok"):
                    logger.error(
                        "⚠️ Automatic memory integrity check failed for %s: %s",
                        chat_name,
                        integrity.get("checks"),
                    )
            return {
                "chunks": chunks,
                "events": created_events,
                "embedded": embedded,
                "stage": stage_updated,
                "person_observations": person_observations,
                "person_quarantined": person_quarantined,
                "person_links_indexed": person_links_indexed,
                "person_identities_merged": int(
                    identity_merge.get("merged") or 0
                ),
                "person_messages_indexed": int(person_index.get("messages") or 0),
                "person_links_processed": int(
                    person_batches.get("links_processed") or 0
                ),
                "person_batches_processed": int(
                    person_batches.get("people_processed") or 0
                ),
                "person_profiles_refreshed": int(
                    person_refresh.get("people_refreshed") or 0
                ),
                "maintenance_candidates_deleted": int(
                    maintenance.get("deleted") or 0
                ),
                "maintenance_integrity_ok": bool(integrity.get("ok", True)),
            }

    def _process_one_chunk(
        self,
        chat_name: str,
        config: Dict[str, Any],
        *,
        force_tail: bool,
    ) -> Optional[int]:
        state = self._initialize_state_if_needed(chat_name, config)
        _, cumulative_count, available, start_cursor = self._available_messages(
            chat_name,
            state,
        )
        minimum = max(5, int(config.get("memory_event_min_messages") or 20))
        target = max(minimum, int(config.get("memory_event_target_messages") or 40))
        maximum = max(target, int(config.get("memory_event_max_messages") or 60))
        before_count = max(
            0,
            int(config.get("memory_event_context_before_messages") or 12),
        )
        after_count = max(
            0,
            int(config.get("memory_event_context_after_messages") or 12),
        )
        processable = available if force_tail else max(0, available - after_count)
        if processable < minimum or (processable < target and not force_tail):
            return None

        requested = min(
            processable,
            maximum if processable >= maximum else target,
        )
        core_start_cursor = start_cursor + 1
        requested_core_end_cursor = start_cursor + requested
        context_start = max(0, start_cursor - before_count)
        context_end = min(
            cumulative_count,
            requested_core_end_cursor + (0 if force_tail else after_count),
        )
        raw_messages = self.chat_log_manager.get_messages_after_sequence(
            chat_name,
            after_sequence=context_start,
            through_sequence=context_end,
            limit=max(1, context_end - context_start),
        )
        selected, selected_core_end_cursor = self._select_event_window_messages(
            raw_messages,
            core_start_cursor=core_start_cursor,
            requested_core_end_cursor=requested_core_end_cursor,
            token_budget=max(
                1024,
                int(
                    config.get("memory_event_input_token_budget")
                    or 16000
                ),
            ),
        )
        selected_core_messages = [
            message
            for message in selected
            if core_start_cursor
            <= int(message.get("_log_cursor") or 0)
            <= selected_core_end_cursor
        ]
        if len(selected_core_messages) < minimum:
            return None

        cards = self._extract_event_cards(
            chat_name,
            selected,
            core_start_cursor=core_start_cursor,
            core_end_cursor=selected_core_end_cursor,
            max_cards=max(
                1,
                min(
                    12,
                    int(config.get("memory_event_max_cards") or 6),
                ),
            ),
        )
        if cards is None:
            return None
        for card in cards:
            card_start = int(
                card.get("source_start_cursor") or core_start_cursor
            )
            card_end = int(
                card.get("source_end_cursor") or selected_core_end_cursor
            )
            card["source_messages"] = [
                dict(message)
                for message in selected
                if card_start
                <= int(message.get("_log_cursor") or 0)
                <= card_end
            ]

        if config.get("memory_verification_enabled", True) and cards:
            cards = self._verify_high_risk_event_cards(
                chat_name,
                cards,
                selected,
            )
        eligible_cards = [
            card
            for card in cards
            if card.get("verification_status") != "quarantined"
        ]
        quarantined_cards = [
            card
            for card in cards
            if card.get("verification_status") == "quarantined"
        ]

        vectors: List[Optional[np.ndarray]] = []
        if config.get("memory_embedding_enabled", True) and eligible_cards:
            vectors = self.embedding_service.embed_passages(
                card["search_text"] for card in eligible_cards
            )
        for index, card in enumerate(eligible_cards):
            card["embedding"] = vectors[index] if index < len(vectors) else None

        dedup_stats = {"skipped": 0, "updates": 0}
        if config.get("memory_dedup_enabled", True) and eligible_cards:
            eligible_cards, dedup_stats = self._apply_event_deduplication(
                chat_name,
                eligible_cards,
                config,
            )

        cards = [*eligible_cards, *quarantined_cards]
        created_ids = self.store.add_events(chat_name, cards)
        self.store.advance_cursor(
            chat_name,
            source_cursor=selected_core_end_cursor,
            source_message_count=selected_core_end_cursor,
        )
        self.invalidate(chat_name)
        logger.info(
            "🧠 Event memory ingested for %s: core=%s-%s context=%s-%s "
            "core_messages=%s context_messages=%s "
            "events=%s duplicates=%s updates=%s",
            chat_name,
            core_start_cursor,
            selected_core_end_cursor,
            int(selected[0].get("_log_cursor") or core_start_cursor),
            int(selected[-1].get("_log_cursor") or selected_core_end_cursor),
            len(selected_core_messages),
            len(selected),
            len(created_ids),
            dedup_stats["skipped"],
            dedup_stats["updates"],
        )
        return len(created_ids)

    def _select_event_window_messages(
        self,
        messages: Sequence[Dict[str, Any]],
        *,
        core_start_cursor: int,
        requested_core_end_cursor: int,
        token_budget: int,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Keep a contiguous core and spend spare budget on nearby evidence."""
        ordered = sorted(
            (
                dict(message)
                for message in messages
                if int(message.get("_log_cursor") or 0) > 0
            ),
            key=lambda message: int(message.get("_log_cursor") or 0),
        )
        core = [
            message
            for message in ordered
            if core_start_cursor
            <= int(message.get("_log_cursor") or 0)
            <= requested_core_end_cursor
        ]
        if not core:
            return [], 0

        budget = max(1, int(token_budget))
        selected_core: List[Dict[str, Any]] = []
        used = 0
        for message in core:
            cost = self.context_manager.estimate_message_tokens(message)
            if selected_core and used + cost > budget:
                break
            selected_core.append(message)
            used += cost
            if used >= budget:
                break
        if not selected_core:
            return [], 0

        actual_core_end = int(
            selected_core[-1].get("_log_cursor") or core_start_cursor
        )
        before = [
            message
            for message in ordered
            if int(message.get("_log_cursor") or 0) < core_start_cursor
        ]
        after = [
            message
            for message in ordered
            if int(message.get("_log_cursor") or 0) > actual_core_end
        ]
        # Nearest after-context is considered first because it resolves
        # batch-tail pronouns and outcomes; then alternate with lookbehind.
        evidence: List[Dict[str, Any]] = []
        before_index = len(before) - 1
        after_index = 0
        prefer_after = True
        while before_index >= 0 or after_index < len(after):
            message: Optional[Dict[str, Any]] = None
            if prefer_after and after_index < len(after):
                message = after[after_index]
                after_index += 1
            elif before_index >= 0:
                message = before[before_index]
                before_index -= 1
            elif after_index < len(after):
                message = after[after_index]
                after_index += 1
            prefer_after = not prefer_after
            if message is None:
                break
            cost = self.context_manager.estimate_message_tokens(message)
            if used + cost > budget:
                continue
            evidence.append(message)
            used += cost

        selected = sorted(
            [*selected_core, *evidence],
            key=lambda message: int(message.get("_log_cursor") or 0),
        )
        return selected, actual_core_end

    def _extract_event_cards(
        self,
        chat_name: str,
        messages: Sequence[Dict[str, Any]],
        *,
        trace_id: str = "",
        core_start_cursor: int = 0,
        core_end_cursor: int = 0,
        max_cards: int = 4,
    ) -> Optional[List[Dict[str, Any]]]:
        start_cursor = int(messages[0].get("_log_cursor") or 1)
        end_cursor = int(messages[-1].get("_log_cursor") or start_cursor)
        core_start = max(
            start_cursor,
            int(core_start_cursor or start_cursor),
        )
        core_end = min(
            end_cursor,
            max(core_start, int(core_end_cursor or end_cursor)),
        )
        card_limit = max(1, min(12, int(max_cards or 4)))
        formatted = self.context_manager.format_messages(list(messages))
        active_corrections: Sequence[Dict[str, Any]] = []
        if getattr(self, "store", None) is not None:
            active_corrections = self.store.list_corrections(
                chat_name,
                active_only=True,
                include_snapshots=False,
            )[:20]
        correction_text = self._format_manual_corrections(active_corrections)
        prompt = [
            {
                "role": "system",
                "content": (
                    f"你是高流量群聊的事件记忆抽取器。把核心消息拆成0到{card_limit}个未来可能值得检索的"
                    "独立事件。忽略问候、复读、纯表情和无后续价值的流水账；不要捏造。"
                    "输出事件之间必须主题互斥；同一段连续讨论只能合并为一张事件卡，不能按"
                    "不同说法或不同参与者重复拆卡。"
                    "同一事件可跨多条消息，观点要标明说话人。转发新闻、截图、第三方说法和"
                    "群友推测只代表‘群内有人分享/声称’，不得写成已核实的外部事实；夸张、"
                    "反讽和玩笑不得升级为当事人的真实意图。前后重叠消息只用于消歧、确认主语"
                    "和观察结果，不能单独触发事件；每张卡的 anchor_cursor 必须落在核心消息"
                    "范围内。尤其当核心末尾出现省略主语、代词、转述或未完成句时，必须结合后"
                    "文判断；仍无法确定当事人时，不得写为 personal_update 或"
                    " self_report，应降低确定性或不生成该卡。"
                    "每一项事实都必须放进 claims，并分别给出事实内容、主语、说话人、"
                    "支撑该事实的 claim_evidence_cursors，以及明确支撑主语绑定的"
                    " subject_evidence_cursors。证据游标只能引用聊天记录中的真实消息；"
                    "至少一个事实证据必须落在核心范围，anchor_cursor 也必须属于该卡的"
                    "证据游标。不要用相邻话题的消息替另一主语补事实。"
                    "certainty 使用 self_report 表示主语本人自述，attributed_claim 表示"
                    "群友对他人或外部对象的归因说法。只输出一个JSON对象，不要代码块。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"群聊：{chat_name}\n"
                    f"全部证据游标：{start_cursor} 到 {end_cursor}\n"
                    f"可触发新事件的核心游标：{core_start} 到 {core_end}\n\n"
                    f"生效中的人工纠错（不得重新生成冲突说法）：\n{correction_text}\n\n"
                    "聊天记录（含只作证据的前后重叠消息）：\n"
                    f"{formatted}\n\n"
                    "严格输出格式：\n"
                    '{"events":[{"title":"简短标题","summary":"100到300字的事实摘要",'
                    '"anchor_cursor":1,"source_start_cursor":1,"source_end_cursor":2,'
                    '"participants":["姓名"],"keywords":["关键词"],'
                    '"opinions":[{"person":"姓名","view":"观点"}],'
                    '"claims":[{"text":"单项事实","subject":"该事实主语或对象",'
                    '"speaker":"消息中的说话人",'
                    '"claim_evidence_cursors":[1],'
                    '"subject_evidence_cursors":[1]}],'
                    '"decisions":["明确结论"],"open_items":["待办或悬而未决问题"],'
                    '"event_type":"personal_update|group_decision|question|debate|'
                    'shared_info|joke|other",'
                    '"certainty":"confirmed_in_chat|self_report|attributed_claim|'
                    'unverified_external|rumor_or_joke",'
                    '"source_note":"信息由谁提供、是否为转发/截图/推测",'
                    '"importance":0.0}]}\n'
                    "importance范围0到1。若没有值得保留的内容，输出 {\"events\":[]}。"
                ),
            },
        ]
        try:
            payload = self._call_memory_json(
                call_type="memory_event_extract",
                messages=prompt,
                schema_hint=(
                    '根对象必须是 {"events":[...]}；events 是数组，'
                    "数组项保留原字段和值。"
                ),
                chat_name=chat_name,
                trace_id=trace_id,
            )
        except Exception as exc:
            logger.warning("⚠️ Event extraction failed for %s: %s", chat_name, exc)
            return None

        raw_events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(raw_events, list):
            logger.warning("⚠️ Event extraction returned invalid schema for %s", chat_name)
            return None

        by_cursor = {
            int(message.get("_log_cursor") or 0): message
            for message in messages
        }
        cards: List[Dict[str, Any]] = []
        for raw in raw_events[:card_limit]:
            if not isinstance(raw, dict):
                continue
            title = self._clean_text(raw.get("title"), 120)
            summary = self._clean_text(raw.get("summary"), 1200)
            if not title or not summary:
                continue
            anchor_cursor = self._bounded_int(
                raw.get("anchor_cursor"),
                start_cursor,
                end_cursor,
                start_cursor,
            )
            if not core_start <= anchor_cursor <= core_end:
                logger.info(
                    "Dropping overlap-only memory event for %s: anchor=%s "
                    "core=%s-%s title=%s",
                    chat_name,
                    anchor_cursor,
                    core_start,
                    core_end,
                    title,
                )
                continue
            claims = self._normalize_event_claims(
                raw.get("claims"),
                available_cursors=set(by_cursor),
                messages_by_cursor=by_cursor,
            )
            if not claims:
                logger.info(
                    "Dropping memory event without claim evidence for %s: %s",
                    chat_name,
                    title,
                )
                continue
            claim_evidence = {
                cursor
                for claim in claims
                for cursor in claim["claim_evidence_cursors"]
            }
            subject_evidence = {
                cursor
                for claim in claims
                for cursor in claim["subject_evidence_cursors"]
            }
            evidence_cursors = sorted(
                claim_evidence | subject_evidence | {anchor_cursor}
            )
            if (
                anchor_cursor not in claim_evidence | subject_evidence
                or not any(
                    core_start <= cursor <= core_end
                    for cursor in claim_evidence
                )
            ):
                logger.info(
                    "Dropping memory event with unanchored evidence for %s: "
                    "anchor=%s core=%s-%s title=%s",
                    chat_name,
                    anchor_cursor,
                    core_start,
                    core_end,
                    title,
                )
                continue
            source_start = min(evidence_cursors)
            source_end = max(evidence_cursors)
            participants = self._string_list(raw.get("participants"), 30, 80)
            keywords = self._string_list(raw.get("keywords"), 20, 80)
            opinions = self._dict_list(raw.get("opinions"), 20)
            decisions = self._string_list(raw.get("decisions"), 20, 240)
            open_items = self._string_list(raw.get("open_items"), 20, 240)
            event_type = self._enum_value(
                raw.get("event_type"),
                {
                    "personal_update",
                    "group_decision",
                    "question",
                    "debate",
                    "shared_info",
                    "joke",
                    "other",
                },
                "other",
            )
            certainty = self._enum_value(
                raw.get("certainty"),
                {
                    "confirmed_in_chat",
                    "self_report",
                    "attributed_claim",
                    "participant_report",
                    "unverified_external",
                    "rumor_or_joke",
                },
                "unverified_external",
            )
            source_note = self._clean_text(raw.get("source_note"), 300)
            try:
                importance = max(0.0, min(1.0, float(raw.get("importance", 0.5))))
            except (TypeError, ValueError):
                importance = 0.5
            source_messages = [
                by_cursor[cursor]
                for cursor in range(source_start, source_end + 1)
                if cursor in by_cursor
            ]
            start_time = (
                str(source_messages[0].get("time") or "")
                if source_messages
                else str(messages[0].get("time") or "")
            )
            end_time = (
                str(source_messages[-1].get("time") or "")
                if source_messages
                else str(messages[-1].get("time") or "")
            )
            card = {
                "title": title,
                "summary": summary,
                "anchor_cursor": anchor_cursor,
                "source_start_cursor": source_start,
                "source_end_cursor": source_end,
                "start_time": start_time,
                "end_time": end_time,
                "participants": participants,
                "keywords": keywords,
                "opinions": opinions,
                "claims": claims,
                "evidence_cursors": evidence_cursors,
                "decisions": decisions,
                "open_items": open_items,
                "event_type": event_type,
                "certainty": certainty,
                "source_note": source_note,
                "importance": importance,
                "verification_status": "not_required",
                "verification_note": "",
            }
            card["card"] = dict(card)
            card["search_text"] = self._event_search_text(card)
            cards.append(card)
        return cards

    @classmethod
    def _normalize_event_claims(
        cls,
        value: Any,
        *,
        available_cursors: set[int],
        messages_by_cursor: Optional[Dict[int, Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            return []
        claims: List[Dict[str, Any]] = []
        for item in value[:20]:
            if not isinstance(item, dict):
                continue
            text = cls._clean_text(item.get("text"), 500)
            subject = cls._clean_text(item.get("subject"), 120)
            speaker = cls._clean_text(item.get("speaker"), 120)
            claim_evidence = sorted(
                {
                    cls._safe_int(cursor)
                    for cursor in (
                        item.get("claim_evidence_cursors") or []
                    )
                    if cls._safe_int(cursor) in available_cursors
                }
            )
            subject_evidence = sorted(
                {
                    cls._safe_int(cursor)
                    for cursor in (
                        item.get("subject_evidence_cursors") or []
                    )
                    if cls._safe_int(cursor) in available_cursors
                }
            )
            if (
                not text
                or not claim_evidence
                or (subject and not subject_evidence)
            ):
                continue
            evidence_union = set(claim_evidence) | set(subject_evidence)
            if messages_by_cursor and speaker and not any(
                str(
                    messages_by_cursor.get(cursor, {}).get("sender") or ""
                ).strip()
                == speaker
                for cursor in evidence_union
            ):
                continue
            if (
                messages_by_cursor
                and subject
                and subject != speaker
                and not any(
                    subject
                    in str(
                        messages_by_cursor.get(cursor, {}).get("content")
                        or ""
                    )
                    for cursor in subject_evidence
                )
            ):
                continue
            claims.append(
                {
                    "text": text,
                    "subject": subject,
                    "speaker": speaker,
                    "claim_evidence_cursors": claim_evidence,
                    "subject_evidence_cursors": subject_evidence,
                }
            )
        return claims

    @staticmethod
    def _event_requires_verification(card: Dict[str, Any]) -> bool:
        nested = card.get("card") if isinstance(card.get("card"), dict) else card
        certainty = str(nested.get("certainty") or "")
        event_type = str(nested.get("event_type") or "")
        claims = nested.get("claims") or card.get("claims") or []
        if event_type == "personal_update" or certainty in {
            "self_report",
            "attributed_claim",
            "participant_report",
        }:
            return True
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            subject = str(claim.get("subject") or "").strip()
            speaker = str(claim.get("speaker") or "").strip()
            if subject and speaker and subject != speaker:
                return True
        high_risk_text = " ".join(
            [
                str(card.get("title") or ""),
                str(card.get("summary") or ""),
                *[
                    str(claim.get("text") or "")
                    for claim in claims
                    if isinstance(claim, dict)
                ],
            ]
        )
        return bool(
            re.search(
                r"手术|疾病|医疗|怀孕|生育|父亲|母亲|去世|死亡|"
                r"借款|欠款|收入|资产|婚姻|离婚|犯罪|违法",
                high_risk_text,
            )
        )

    @staticmethod
    def _set_event_verification(
        card: Dict[str, Any],
        status: str,
        note: str,
    ) -> None:
        card["verification_status"] = status
        card["verification_note"] = note
        nested = card.get("card")
        if isinstance(nested, dict):
            nested["verification_status"] = status
            nested["verification_note"] = note

    def _verify_high_risk_event_cards(
        self,
        chat_name: str,
        cards: Sequence[Dict[str, Any]],
        messages: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Fail closed only for cards where a wrong entity binding is costly."""
        result = [dict(card) for card in cards]
        risky_indexes = [
            index
            for index, card in enumerate(result)
            if self._event_requires_verification(card)
        ]
        if not risky_indexes:
            return result

        ordered = sorted(
            (
                dict(message)
                for message in messages
                if int(message.get("_log_cursor") or 0) > 0
            ),
            key=lambda message: int(message.get("_log_cursor") or 0),
        )
        positions = {
            int(message.get("_log_cursor") or 0): index
            for index, message in enumerate(ordered)
        }
        selected_positions: set[int] = set()
        for index in risky_indexes:
            for cursor in result[index].get("evidence_cursors") or []:
                position = positions.get(int(cursor))
                if position is None:
                    continue
                selected_positions.update(
                    range(
                        max(0, position - 3),
                        min(len(ordered), position + 4),
                    )
                )
        verification_messages = [
            ordered[position]
            for position in sorted(selected_positions)
        ]
        payload = {
            "events": [
                {
                    "candidate_index": index + 1,
                    "title": result[index].get("title") or "",
                    "summary": result[index].get("summary") or "",
                    "event_type": (
                        result[index].get("card", {}).get("event_type")
                        if isinstance(result[index].get("card"), dict)
                        else ""
                    ),
                    "certainty": (
                        result[index].get("card", {}).get("certainty")
                        if isinstance(result[index].get("card"), dict)
                        else ""
                    ),
                    "claims": result[index].get("claims") or [],
                    "anchor_cursor": result[index].get("anchor_cursor"),
                }
                for index in risky_indexes
            ]
        }
        prompt = [
            {
                "role": "system",
                "content": (
                    "你是事件记忆证据复核器。逐项核对每张卡的 claims：事实内容、主语、"
                    "说话人和证据游标是否与原消息一致。高流量群聊常有话题交错；相邻出现"
                    "的人名不能自动成为前一段经历的主语，转述他人也不能变成说话人自述。"
                    "只要一项核心事实缺少直接证据、主语绑定冲突、把两个话题拼接，或"
                    "self_report 并非主语本人自述，就 quarantine。完全受证据支持才 accept。"
                    "不要补充外部知识，只输出JSON对象。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"群聊：{chat_name}\n"
                    f"待复核事件：{json.dumps(payload, ensure_ascii=False)}\n\n"
                    "事件附近原消息：\n"
                    f"{self.context_manager.format_messages(verification_messages)}\n\n"
                    "严格输出："
                    '{"decisions":[{"candidate_index":1,'
                    '"action":"accept|quarantine",'
                    '"reason":"简短、具体地说明证据是否支持主语和事实"}]}'
                ),
            },
        ]
        decisions: Dict[int, Dict[str, Any]] = {}
        failure_note = ""
        try:
            parsed = self._call_memory_json(
                call_type="memory_event_review",
                messages=prompt,
                schema_hint='根对象必须是 {"decisions":[...]}。',
                chat_name=chat_name,
            )
            for item in (
                parsed.get("decisions")
                if isinstance(parsed, dict)
                and isinstance(parsed.get("decisions"), list)
                else []
            ):
                if not isinstance(item, dict):
                    continue
                candidate_index = (
                    self._safe_int(item.get("candidate_index"), default=0) - 1
                )
                if candidate_index in risky_indexes:
                    decisions[candidate_index] = item
        except Exception as exc:
            failure_note = f"证据复核调用失败：{self._clean_text(exc, 240)}"
            logger.warning(
                "⚠️ High-risk memory verification failed for %s: %s",
                chat_name,
                exc,
            )

        for index in risky_indexes:
            decision = decisions.get(index) or {}
            action = str(decision.get("action") or "").strip().lower()
            note = self._clean_text(decision.get("reason"), 500)
            if action == "accept":
                self._set_event_verification(
                    result[index],
                    "passed",
                    note or "高风险事实已通过逐项证据复核",
                )
            else:
                self._set_event_verification(
                    result[index],
                    "quarantined",
                    note
                    or failure_note
                    or "复核结果缺失，已按高风险策略隔离",
                )
        return result

    def _apply_event_deduplication(
        self,
        chat_name: str,
        cards: Sequence[Dict[str, Any]],
        config: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        """Classify only plausible semantic collisions; fail open on uncertainty."""
        candidate_threshold = max(
            0.5,
            min(
                0.99,
                float(config.get("memory_dedup_candidate_threshold") or 0.78),
            ),
        )
        duplicate_threshold = max(
            candidate_threshold,
            min(
                0.999,
                float(
                    config.get("memory_duplicate_similarity_threshold")
                    or 0.90
                ),
            ),
        )
        lookback_days = max(
            1,
            min(3650, int(config.get("memory_dedup_lookback_days") or 30)),
        )
        existing_events, _ = self._cached_events(chat_name)
        cutoff, upper_bound = self._event_dedup_window(
            cards,
            lookback_days=lookback_days,
        )
        existing_events = [
            event
            for event in existing_events
            if cutoff
            <= self._parse_time(
                event.get("end_time")
                or event.get("start_time")
                or event.get("created_at")
            )
            <= upper_bound
        ]

        normalized_new = [
            self._normalized_vector(card.get("embedding"))
            for card in cards
        ]
        match_map: Dict[int, List[Tuple[float, Dict[str, Any]]]] = {}
        forced_existing_pairs: set[Tuple[int, int]] = set()
        for index, vector in enumerate(normalized_new):
            scored = []
            for event in existing_events:
                existing_vector = self._normalized_vector(event.get("embedding"))
                similarity = -1.0
                if (
                    vector is not None
                    and existing_vector is not None
                    and existing_vector.size == vector.size
                ):
                    similarity = float(existing_vector @ vector)
                forced = self._event_source_ranges_adjacent(
                    card=cards[index],
                    event=event,
                )
                if forced:
                    forced_existing_pairs.add((index, int(event["id"])))
                if forced or similarity >= candidate_threshold:
                    scored.append((similarity, event))
            scored.sort(key=lambda item: (item[0], item[1]["id"]), reverse=True)
            if scored:
                forced_matches = [
                    item
                    for item in scored
                    if (index, int(item[1]["id"])) in forced_existing_pairs
                ][:3]
                semantic_matches = [
                    item
                    for item in scored
                    if (index, int(item[1]["id"])) not in forced_existing_pairs
                ][:3]
                match_map[index] = [*forced_matches, *semantic_matches]

        new_pairs: Dict[Tuple[int, int], float] = {}
        forced_new_pairs: set[Tuple[int, int]] = set()
        for left in range(len(cards)):
            left_vector = normalized_new[left]
            for right in range(left + 1, len(cards)):
                right_vector = normalized_new[right]
                similarity = -1.0
                if (
                    left_vector is not None
                    and right_vector is not None
                    and right_vector.size == left_vector.size
                ):
                    similarity = float(left_vector @ right_vector)
                forced = self._event_source_ranges_adjacent(
                    card=cards[left],
                    event=cards[right],
                )
                if forced:
                    forced_new_pairs.add((left, right))
                if forced or similarity >= candidate_threshold:
                    new_pairs[(left, right)] = similarity

        exact_existing: Dict[str, int] = {}
        for event in existing_events:
            fingerprint = self._event_content_fingerprint(event)
            if fingerprint:
                exact_existing[fingerprint] = int(event["id"])
        exact_seen: Dict[str, int] = {}
        exact_skips: Dict[int, Tuple[str, int]] = {}
        for index, card in enumerate(cards):
            fingerprint = self._event_content_fingerprint(card)
            if not fingerprint:
                continue
            if fingerprint in exact_existing:
                exact_skips[index] = ("existing", exact_existing[fingerprint])
            elif fingerprint in exact_seen:
                exact_skips[index] = ("new", exact_seen[fingerprint])
            else:
                exact_seen[fingerprint] = index

        if not match_map and not new_pairs and not exact_skips:
            return list(cards), {"skipped": 0, "updates": 0}

        decisions: Dict[int, Dict[str, Any]] = {}
        if match_map or new_pairs:
            try:
                decisions = self._classify_event_relations(
                    chat_name,
                    cards,
                    match_map,
                    new_pairs,
                    forced_existing_pairs=forced_existing_pairs,
                    forced_new_pairs=forced_new_pairs,
                    compact_output=bool(
                        config.get("memory_dedup_compact_output", False)
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "⚠️ Event dedup classification failed for %s; keeping candidates: %s",
                    chat_name,
                    exc,
                )

        skipped_indexes = set(exact_skips)
        superseded_new_indexes = set()
        update_count = 0
        for index, decision in decisions.items():
            if index < 0 or index >= len(cards) or index in skipped_indexes:
                continue
            action = str(decision.get("action") or "keep").strip().lower()
            related_event_id = max(
                0,
                self._safe_int(decision.get("related_event_id")),
            )
            related_candidate_number = self._safe_int(
                decision.get("related_candidate_index"),
                default=0,
            )
            related_candidate_index = (
                related_candidate_number - 1
                if related_candidate_number > 0
                else -1
            )
            matched_existing = {
                int(event["id"]): similarity
                for similarity, event in match_map.get(index, [])
            }
            reason = self._clean_text(decision.get("reason"), 300)
            forced_existing = (
                related_event_id > 0
                and (index, related_event_id) in forced_existing_pairs
            )
            forced_new = (
                0 <= related_candidate_index < index
                and (related_candidate_index, index) in forced_new_pairs
            )

            if action == "quarantine_conflict":
                if forced_existing or forced_new:
                    self._set_event_verification(
                        cards[index],
                        "quarantined",
                        reason or "相邻事件复核发现主语或事实冲突",
                    )
                continue

            if action == "skip_duplicate":
                if not related_event_id and matched_existing:
                    related_event_id = max(
                        matched_existing,
                        key=matched_existing.get,
                    )
                    forced_existing = (
                        index,
                        related_event_id,
                    ) in forced_existing_pairs
                existing_similarity = matched_existing.get(related_event_id, -1.0)
                new_similarity = new_pairs.get(
                    (related_candidate_index, index),
                    -1.0,
                )
                if (
                    related_event_id
                    and (
                        existing_similarity >= duplicate_threshold
                        or forced_existing
                    )
                ) or (
                    0 <= related_candidate_index < index
                    and (
                        new_similarity >= duplicate_threshold
                        or forced_new
                    )
                ):
                    skipped_indexes.add(index)
                continue

            if action != "keep_update":
                continue

            if related_event_id:
                similarity = matched_existing.get(related_event_id, -1.0)
                if similarity < candidate_threshold and not forced_existing:
                    continue
                cards[index]["supersedes_event_id"] = related_event_id
                cards[index]["relation_reason"] = reason or (
                    f"同话题新进展，语义相似度 {similarity:.3f}"
                )
                self._apply_consolidated_event_text(cards[index], decision)
                update_count += 1
            elif 0 <= related_candidate_index < index:
                similarity = new_pairs.get(
                    (related_candidate_index, index),
                    -1.0,
                )
                if similarity < candidate_threshold and not forced_new:
                    continue
                superseded_new_indexes.add(related_candidate_index)
                cards[index]["relation_reason"] = reason or (
                    f"合并同批次事件，语义相似度 {similarity:.3f}"
                )
                self._apply_consolidated_event_text(cards[index], decision)

        skipped_indexes.update(superseded_new_indexes)
        kept = [
            card
            for index, card in enumerate(cards)
            if index not in skipped_indexes
        ]
        return kept, {
            "skipped": len(skipped_indexes),
            "updates": update_count,
        }

    @staticmethod
    def _event_source_ranges_adjacent(
        *,
        card: Dict[str, Any],
        event: Dict[str, Any],
        maximum_gap: int = 3,
    ) -> bool:
        card_namespace = str(
            card.get("source_namespace") or "live_chat_log"
        )
        event_namespace = str(
            event.get("source_namespace") or "live_chat_log"
        )
        if card_namespace != event_namespace:
            return False
        card_start = ChatMemoryService._safe_int(
            card.get("source_start_cursor"),
        )
        card_end = ChatMemoryService._safe_int(
            card.get("source_end_cursor"),
        )
        event_start = ChatMemoryService._safe_int(
            event.get("source_start_cursor"),
        )
        event_end = ChatMemoryService._safe_int(
            event.get("source_end_cursor"),
        )
        if min(card_start, card_end, event_start, event_end) <= 0:
            return False
        if card_end < event_start:
            gap = event_start - card_end - 1
        elif event_end < card_start:
            gap = card_start - event_end - 1
        else:
            gap = 0
        return gap <= max(0, int(maximum_gap))

    @classmethod
    def _event_dedup_window(
        cls,
        cards: Sequence[Dict[str, Any]],
        *,
        lookback_days: int,
    ) -> Tuple[datetime, datetime]:
        """Anchor deduplication to event time so chronological replays work."""
        event_times = [
            cls._parse_time(card.get("end_time") or card.get("start_time"))
            for card in cards
        ]
        event_times = [value for value in event_times if value != datetime.min]
        if event_times:
            earliest_time = min(event_times)
            latest_time = max(event_times)
        else:
            earliest_time = latest_time = datetime.now()
        return (
            earliest_time - timedelta(days=max(1, int(lookback_days))),
            latest_time + timedelta(days=1),
        )

    def _classify_event_relations(
        self,
        chat_name: str,
        cards: Sequence[Dict[str, Any]],
        match_map: Dict[int, List[Tuple[float, Dict[str, Any]]]],
        new_pairs: Dict[Tuple[int, int], float],
        *,
        forced_existing_pairs: Optional[set[Tuple[int, int]]] = None,
        forced_new_pairs: Optional[set[Tuple[int, int]]] = None,
        compact_output: bool = False,
    ) -> Dict[int, Dict[str, Any]]:
        forced_existing_pairs = forced_existing_pairs or set()
        forced_new_pairs = forced_new_pairs or set()
        existing_by_id: Dict[int, Dict[str, Any]] = {}
        possible_matches = []
        for candidate_index, matches in match_map.items():
            for similarity, event in matches:
                event_id = int(event["id"])
                existing_by_id[event_id] = event
                possible_matches.append(
                    {
                        "candidate_index": candidate_index + 1,
                        "existing_event_id": event_id,
                        "similarity": round(similarity, 4),
                        "relation_basis": (
                            "adjacent_source"
                            if (candidate_index, event_id)
                            in forced_existing_pairs
                            else "semantic_similarity"
                        ),
                    }
                )

        payload = {
            "new_events": [
                {
                    "candidate_index": index + 1,
                    "title": card.get("title") or "",
                    "summary": card.get("summary") or "",
                    "participants": card.get("participants") or [],
                    "keywords": card.get("keywords") or [],
                    "claims": card.get("claims") or [],
                    "event_type": (
                        card.get("card", {}).get("event_type")
                        if isinstance(card.get("card"), dict)
                        else ""
                    ),
                    "start_time": card.get("start_time") or "",
                    "end_time": card.get("end_time") or "",
                    "source_excerpt": [
                        {
                            "cursor": int(
                                message.get("_log_cursor") or 0
                            ),
                            "sender": self._clean_text(
                                message.get("sender"),
                                100,
                            ),
                            "content": self._clean_text(
                                message.get("content"),
                                300,
                            ),
                        }
                        for message in card.get("source_messages") or []
                        if isinstance(message, dict)
                    ][:16],
                }
                for index, card in enumerate(cards)
            ],
            "existing_events": [
                {
                    "event_id": event_id,
                    "title": event.get("title") or "",
                    "summary": self._clean_text(event.get("summary"), 600),
                    "participants": event.get("participants") or [],
                    "keywords": event.get("keywords") or [],
                    "end_time": event.get("end_time") or "",
                    "claims": (
                        event.get("card", {}).get("claims") or []
                        if isinstance(event.get("card"), dict)
                        else []
                    ),
                    "source_excerpt": self._event_source_excerpt(event),
                }
                for event_id, event in sorted(existing_by_id.items())
            ],
            "possible_matches": possible_matches,
            "new_event_similarities": [
                {
                    "left_candidate_index": left + 1,
                    "right_candidate_index": right + 1,
                    "similarity": round(similarity, 4),
                    "relation_basis": (
                        "adjacent_source"
                        if (left, right) in forced_new_pairs
                        else "semantic_similarity"
                    ),
                }
                for (left, right), similarity in sorted(new_pairs.items())
            ],
        }
        output_instruction = (
            "只输出动作是 skip_duplicate、keep_update 或"
            " quarantine_conflict 的项；"
            "keep 项必须省略，未输出的新事件按 keep 处理。"
            if compact_output
            else "每张新事件都要输出一个动作。"
        )
        action_schema = (
            "skip_duplicate|keep_update|quarantine_conflict"
            if compact_output
            else "keep|skip_duplicate|keep_update|quarantine_conflict"
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "你是事件记忆去重器。只判断给出的候选关系，不补充外部事实。"
                    "keep 表示独立事件；skip_duplicate 表示"
                    "与已有事件或更早的新候选语义相同且没有任何新增事实、纠正、结果或状态变化；"
                    "keep_update 表示同一持续事件出现实质新进展、纠正或结果，并且新卡应成为"
                    "当前有效版本。相同人物或相同大话题不等于重复。拿不准时必须 keep。"
                    "keep_update 时给出能独立理解当前状态的 consolidated_title 和"
                    " consolidated_summary。"
                    "relation_basis=adjacent_source 的候选即使向量相似度低也必须检查："
                    "若新卡把相邻话题的事实绑定到错误主语、与证据或相邻事件出现实体冲突，"
                    "使用 quarantine_conflict；不要为了消除冲突而合并两个独立话题。"
                    + output_instruction
                    + "只输出 JSON 对象。"
                ),
            },
            {
                "role": "user",
                "content": (
                    json.dumps(payload, ensure_ascii=False)
                    + "\n严格输出："
                    '{"decisions":[{"candidate_index":1,'
                    + f'"action":"{action_schema}",'
                    +
                    '"related_event_id":0,'
                    '"related_candidate_index":0,'
                    '"consolidated_title":"",'
                    '"consolidated_summary":"",'
                    '"reason":"简短理由"}]}。'
                    "related_candidate_index 只能引用编号更小的新候选；没有关联时填0。"
                ),
            },
        ]
        parsed = self._call_memory_json(
            call_type="memory_event_relation",
            messages=messages,
            schema_hint='根对象必须是 {"decisions":[...]}。',
            chat_name=chat_name,
        )
        raw_decisions = parsed.get("decisions") if isinstance(parsed, dict) else None
        if not isinstance(raw_decisions, list):
            return {}
        result = {}
        for item in raw_decisions:
            if not isinstance(item, dict):
                continue
            index = self._safe_int(item.get("candidate_index"), default=0) - 1
            if 0 <= index < len(cards):
                result[index] = dict(item)
        return result

    def _event_source_excerpt(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        store = getattr(self, "store", None)
        event_id = self._safe_int(event.get("id"))
        if store is None or event_id <= 0:
            return []
        try:
            messages = store.list_event_messages(event_id)
        except Exception:
            return []
        return [
            {
                "cursor": int(message.get("_log_cursor") or 0),
                "sender": self._clean_text(message.get("sender"), 100),
                "content": self._clean_text(message.get("content"), 300),
            }
            for message in messages[:16]
        ]

    def _apply_consolidated_event_text(
        self,
        card: Dict[str, Any],
        decision: Dict[str, Any],
    ) -> None:
        title = self._clean_text(decision.get("consolidated_title"), 120)
        summary = self._clean_text(decision.get("consolidated_summary"), 1200)
        if title:
            card["title"] = title
        if summary:
            card["summary"] = summary
        nested = card.get("card")
        if isinstance(nested, dict):
            nested["title"] = card.get("title") or ""
            nested["summary"] = card.get("summary") or ""
        card["search_text"] = self._event_search_text(card)
        if self.embedding_service.ready:
            vectors = self.embedding_service.embed_passages([card["search_text"]])
            if vectors and vectors[0] is not None:
                card["embedding"] = vectors[0]

    @staticmethod
    def _normalized_vector(value: Any) -> Optional[np.ndarray]:
        if not isinstance(value, np.ndarray) or value.size <= 0:
            return None
        vector = np.asarray(value, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vector))
        if norm <= 1e-9:
            return None
        return vector / norm

    @classmethod
    def _event_content_fingerprint(cls, event: Dict[str, Any]) -> str:
        text = " ".join(
            (
                str(event.get("title") or ""),
                str(event.get("summary") or ""),
            )
        )
        return re.sub(r"[\W_]+", "", text, flags=re.UNICODE).casefold()

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_json_object(value: Any) -> Dict[str, Any]:
        text = str(value or "").strip()
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("model returned no JSON object")
            parsed = json.loads(text[start : end + 1])
        if not isinstance(parsed, dict):
            raise ValueError("model JSON root must be an object")
        return parsed

    def _call_memory_json(
        self,
        *,
        call_type: str,
        messages: List[Dict[str, str]],
        schema_hint: str,
        chat_name: str = "",
        trace_id: str = "",
    ) -> Dict[str, Any]:
        """Parse strict JSON and repair only the small output when necessary."""
        manager = self.llm_manager or get_llm_manager()
        history_chat_name = self.llm_history_chat_name or str(chat_name or "")
        codex_output_schema = codex_memory_output_schema(
            call_type,
            schema_hint,
            messages,
        )

        def call_model(request_messages: List[Dict[str, str]]) -> str:
            usage_capture: List[Dict[str, Any]] = []
            try:
                return manager.call(
                    plugin_name="assistant",
                    call_type=call_type,
                    messages=request_messages,
                    _mabobot_chat_name=history_chat_name,
                    _mabobot_memory_trace=(
                        {"trace_id": trace_id}
                        if trace_id
                        else None
                    ),
                    _mabobot_history_mode=self.llm_history_mode,
                    _mabobot_usage_capture=usage_capture,
                    _mabobot_codex_output_schema=codex_output_schema,
                )
            finally:
                if self.llm_usage_callback is not None:
                    for usage in usage_capture:
                        self.llm_usage_callback(usage)

        result = call_model(messages)
        try:
            return self._parse_json_object(result)
        except (ValueError, json.JSONDecodeError) as first_error:
            logger.warning(
                "⚠️ %s returned malformed JSON; requesting one output-only repair: %s",
                call_type,
                first_error,
            )
            repaired = call_model(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是JSON语法修复器。只修复下面文本的JSON语法，不增删事实，"
                            "不解释，不使用代码块，只输出一个合法JSON对象。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"结构要求：{schema_hint}\n\n待修复文本：\n{result}",
                    },
                ]
            )
            return self._parse_json_object(repaired)

    @staticmethod
    def _clean_text(value: Any, limit: int) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]

    @classmethod
    def _string_list(
        cls,
        value: Any,
        max_items: int,
        item_limit: int,
    ) -> List[str]:
        if not isinstance(value, list):
            return []
        result: List[str] = []
        seen = set()
        for item in value:
            text = cls._clean_text(item, item_limit)
            if text and text not in seen:
                result.append(text)
                seen.add(text)
            if len(result) >= max_items:
                break
        return result

    @staticmethod
    def _dict_list(value: Any, max_items: int) -> List[Dict[str, str]]:
        if not isinstance(value, list):
            return []
        result = []
        for item in value:
            if not isinstance(item, dict):
                continue
            person = re.sub(r"\s+", " ", str(item.get("person") or "")).strip()[:80]
            view = re.sub(r"\s+", " ", str(item.get("view") or "")).strip()[:300]
            if person and view:
                result.append({"person": person, "view": view})
            if len(result) >= max_items:
                break
        return result

    @staticmethod
    def _bounded_int(
        value: Any,
        lower: int,
        upper: int,
        default: int,
    ) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(lower, min(upper, parsed))

    @staticmethod
    def _enum_value(value: Any, allowed: set[str], default: str) -> str:
        normalized = str(value or "").strip().lower()
        return normalized if normalized in allowed else default

    @staticmethod
    def _event_search_text(event: Dict[str, Any]) -> str:
        opinions = "；".join(
            f"{item.get('person')}：{item.get('view')}"
            for item in event.get("opinions") or []
        )
        claims = "；".join(
            "｜".join(
                part
                for part in (
                    str(item.get("subject") or ""),
                    str(item.get("speaker") or ""),
                    str(item.get("text") or ""),
                )
                if part
            )
            for item in event.get("claims") or []
            if isinstance(item, dict)
        )
        return "\n".join(
            part
            for part in (
                f"标题：{event.get('title', '')}",
                f"摘要：{event.get('summary', '')}",
                f"参与者：{'、'.join(event.get('participants') or [])}",
                f"关键词：{'、'.join(event.get('keywords') or [])}",
                f"观点：{opinions}" if opinions else "",
                f"逐项事实：{claims}" if claims else "",
                f"结论：{'；'.join(event.get('decisions') or [])}",
                f"待办：{'；'.join(event.get('open_items') or [])}",
                f"事件性质：{event.get('event_type') or 'other'}",
                f"可信状态：{event.get('certainty') or 'unverified_external'}",
                f"来源说明：{event.get('source_note') or ''}",
            )
            if part
        )

    def _embed_missing_events(self, chat_name: str, limit: int) -> int:
        if not self.embedding_service.ready:
            return 0
        events = self.store.list_missing_embeddings(chat_name, limit=limit)
        if not events:
            return 0
        vectors = self.embedding_service.embed_passages(
            event.get("search_text", "") for event in events
        )
        count = 0
        for event, vector in zip(events, vectors):
            if vector is None:
                continue
            self.store.update_event_embedding(event["id"], vector)
            count += 1
        if count:
            self.invalidate(chat_name)
        return count

    def _refresh_stage_if_due(
        self,
        chat_name: str,
        config: Dict[str, Any],
        *,
        force: bool = False,
    ) -> bool:
        state = self.store.get_state(chat_name)
        if str(state.get("stage_mode") or "auto") == "manual":
            return False
        after_id = int(state.get("stage_source_event_id") or 0)
        threshold = max(5, int(config.get("memory_stage_event_threshold") or 40))
        recent_events = self.store.list_events(
            chat_name,
            after_id=after_id,
            limit=max(
                threshold,
                int(config.get("memory_stage_input_event_limit") or 80),
            ),
            active_only=True,
        )
        recent_events = [
            event
            for event in recent_events
            if int(event.get("superseded_by_event_id") or 0) == 0
            and int(event.get("is_invalidated") or 0) == 0
        ]
        has_version_update = any(
            int(event.get("supersedes_event_id") or 0) > 0
            for event in recent_events
        )
        immediate_version_refresh = bool(
            config.get("memory_stage_immediate_version_update", True)
        )
        if (
            not force
            and len(recent_events) < threshold
            and not (has_version_update and immediate_version_refresh)
        ):
            return False
        if not recent_events:
            return False

        stage_input_budget = max(
            2048,
            int(config.get("memory_stage_input_token_budget") or 24000),
        )
        previous = self.context_manager.truncate_text_to_budget(
            str(state.get("stage_summary") or "（暂无阶段记忆）"),
            max(512, int(stage_input_budget * 0.20)),
            notice="旧阶段记忆达到输入预算上限",
        )
        material_overhead = (
            self.context_manager.estimate_tokens(previous)
            + 650
        )
        event_input_budget = max(1024, stage_input_budget - material_overhead)
        selected_events: List[Dict[str, Any]] = []
        event_blocks: List[str] = []
        used_event_tokens = 0
        for event in recent_events:
            block = self._format_event_for_stage(event)
            block_tokens = self.context_manager.estimate_tokens(block) + 8
            if selected_events and used_event_tokens + block_tokens > event_input_budget:
                break
            selected_events.append(event)
            event_blocks.append(block)
            used_event_tokens += block_tokens
            if used_event_tokens >= event_input_budget:
                break
        if not selected_events:
            return False
        event_text = "\n\n".join(event_blocks)
        active_corrections = self.store.list_corrections(
            chat_name,
            active_only=True,
            include_snapshots=False,
        )
        correction_text = self._format_manual_corrections(
            active_corrections
        )
        prompt = [
            {
                "role": "system",
                "content": (
                    "你是群聊阶段记忆管理员。用旧阶段记忆和新增事件维护一份紧凑、可靠、"
                    "面向未来回复的当前状态。保留稳定事实、群体关系、持续话题、明确结论和"
                    "未完成事项；删除流水账和已失效状态；不确定就标注。‘稳定事实’严格只允许"
                    "群内已发生的决定、反复确认的关系与偏好、或当事人自己的经历/设备/计划，"
                    "并保留归因。若新增事件标记为替代旧事件，必须以新版本修正旧阶段记忆，"
                    "不要把互相冲突的新旧版本同时保留为当前事实。任何外部新闻、企业动态、"
                    "产品数据、社会事件、截图或第三方说法，"
                    "即便看似来自官网，也一律放入shared_claims，句子必须以‘某人分享/称/转发’"
                    "开头，不得写成已核实事实。rumor_or_joke 不得写成人物真实意图。"
                    "本任务只维护群级阶段状态，不生成或改写人物资料；人物长期记忆由直接读取"
                    "原消息的独立证据链路维护。人工纠错"
                    "是最高优先级的确定信息，任何旧"
                    "阶段或事件卡与人工纠错冲突时必须删除旧说法，且不得在后续更新"
                    "中恢复。只输出JSON对象，不要代码块。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"群聊：{chat_name}\n\n"
                    f"## 旧阶段记忆\n{previous}\n\n"
                    f"## 生效中的人工纠错\n{correction_text}\n\n"
                    f"## 新增事件\n{event_text}\n\n"
                    "严格输出："
                    '{"summary":"不超过2500字的阶段总览",'
                    '"stable_facts":["仅限群内决定、偏好、自述经历"],'
                    '"shared_claims":["带分享者归因的外部消息或未核实说法"],'
                    '"active_topics":["持续话题"],'
                    '"group_dynamics":["关系或氛围变化"],"open_items":["未完成事项"],'
                    '"stale_or_uncertain":["可能过时或不确定内容"]}'
                ),
            },
        ]
        try:
            payload = self._call_memory_json(
                call_type="memory_stage_summarize",
                messages=prompt,
                schema_hint=(
                    "根对象必须包含 summary、stable_facts、active_topics、"
                    "shared_claims、group_dynamics、open_items、stale_or_uncertain。"
                ),
                chat_name=chat_name,
            )
        except Exception as exc:
            logger.warning("⚠️ Stage memory refresh failed for %s: %s", chat_name, exc)
            return False

        payload = self._normalize_stage_categories(payload)
        payload = self._apply_manual_correction_constraints(
            payload,
            active_corrections,
        )
        rendered = self._render_stage(payload)
        if not rendered:
            return False
        char_limit = max(1000, int(config.get("memory_stage_char_limit") or 6000))
        rendered = rendered[:char_limit].rstrip()
        source_event_id = int(selected_events[-1]["id"])
        self.store.update_stage(
            chat_name,
            summary=rendered,
            stage_json=payload,
            source_event_id=source_event_id,
        )
        logger.info(
            "🧠 Stage memory refreshed for %s: events=%s",
            chat_name,
            len(selected_events),
        )
        return True

    @staticmethod
    def _format_event_for_stage(event: Dict[str, Any]) -> str:
        card = event.get("card") if isinstance(event.get("card"), dict) else {}
        opinions = "；".join(
            f"{item.get('person') or '?'}：{item.get('view') or ''}"
            for item in (event.get("opinions") or [])[:8]
            if isinstance(item, dict)
        )
        claims = "；".join(
            "｜".join(
                part
                for part in (
                    str(item.get("subject") or ""),
                    str(item.get("speaker") or ""),
                    str(item.get("text") or ""),
                )
                if part
            )
            for item in (card.get("claims") or [])[:12]
            if isinstance(item, dict)
        )
        return "\n".join(
            (
                f"### 事件 {event['id']}｜{event.get('start_time') or ''}"
                f"～{event.get('end_time') or ''}",
                f"标题：{event.get('title') or ''}",
                f"摘要：{event.get('summary') or ''}",
                f"参与者：{'、'.join(event.get('participants') or [])}",
                f"关键词：{'、'.join(event.get('keywords') or [])}",
                f"代表观点：{opinions}",
                f"逐项事实：{claims}",
                f"结论：{'；'.join(event.get('decisions') or [])}",
                f"未完成：{'；'.join(event.get('open_items') or [])}",
                f"事件性质：{card.get('event_type') or 'unknown'}",
                f"可信状态：{card.get('certainty') or 'unknown'}",
                f"来源说明：{card.get('source_note') or '未记录'}",
                (
                    f"版本关系：替代事件 #{int(event.get('supersedes_event_id') or 0)}；"
                    f"{event.get('relation_reason') or '同话题新进展'}"
                    if int(event.get("supersedes_event_id") or 0) > 0
                    else "版本关系：独立事件"
                ),
                f"重要度：{float(event.get('importance') or 0.5):.2f}",
            )
        )

    @classmethod
    def _render_stage(cls, payload: Dict[str, Any]) -> str:
        summary = cls._clean_text(payload.get("summary"), 3000)
        if not summary:
            return ""
        sections = [f"## 阶段总览\n{summary}"]
        for key, title in (
            ("stable_facts", "稳定事实"),
            ("shared_claims", "群内转发与未核实外部说法"),
            ("active_topics", "持续话题"),
            ("group_dynamics", "群体关系与氛围"),
            ("open_items", "未完成事项"),
            ("stale_or_uncertain", "可能过时或不确定"),
        ):
            values = cls._string_list(payload.get(key), 30, 300)
            if values:
                sections.append(f"## {title}\n" + "\n".join(f"- {item}" for item in values))
        return "\n\n".join(sections)

    @classmethod
    def _normalize_stage_categories(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Keep obvious third-party shares out of durable stable facts."""
        normalized = dict(payload or {})
        stable = cls._string_list(normalized.get("stable_facts"), 40, 400)
        shared = cls._string_list(normalized.get("shared_claims"), 60, 400)
        external_markers = re.compile(r"分享|转发|报道|截图|官网|新闻|消息称")
        personal_markers = re.compile(
            r"自述|自己的|自家|亲历|确认自己|决定|选择|使用|拥有|遇到|其单位|自己单位"
        )
        kept = []
        seen_shared = set(shared)
        for item in stable:
            if external_markers.search(item) and not personal_markers.search(item):
                if item not in seen_shared:
                    shared.append(item)
                    seen_shared.add(item)
            else:
                kept.append(item)
        normalized["stable_facts"] = kept
        normalized["shared_claims"] = shared[:60]
        return normalized

    @classmethod
    def _format_manual_corrections(
        cls,
        corrections: Sequence[Dict[str, Any]],
    ) -> str:
        if not corrections:
            return "（暂无）"
        blocks = []
        for correction in corrections[:40]:
            if correction.get("action") == "approve_review":
                continue
            false_claims = "；".join(
                cls._string_list(
                    correction.get("false_claims"),
                    20,
                    300,
                )
            ) or "未单列"
            corrected_claim = cls._clean_text(
                correction.get("corrected_claim"),
                600,
            ) or "仅撤销错误说法，不新增事实"
            blocks.append(
                f"- 纠错 #{int(correction.get('id') or 0)}："
                f"错误说法＝{false_claims}；"
                f"正确信息＝{corrected_claim}；"
                f"原因＝{cls._clean_text(correction.get('reason'), 400)}"
            )
        return "\n".join(blocks)

    @staticmethod
    def _normalized_claim_text(value: Any) -> str:
        return re.sub(
            r"[\s，。！？；：、,.!?;:'\"“”‘’（）()【】\[\]-]+",
            "",
            str(value or "").casefold(),
        )

    @classmethod
    def _contains_false_claim(
        cls,
        text: Any,
        false_claims: Sequence[str],
    ) -> bool:
        normalized = cls._normalized_claim_text(text)
        if not normalized:
            return False
        return any(
            claim
            and (
                claim in normalized
                or (
                    len(normalized) >= 12
                    and normalized in claim
                )
            )
            for claim in (
                cls._normalized_claim_text(value)
                for value in false_claims
            )
        )

    @classmethod
    def _remove_false_claim_sentences(
        cls,
        text: Any,
        false_claims: Sequence[str],
    ) -> str:
        value = str(text or "").strip()
        if not value or not false_claims:
            return value
        pieces = re.split(r"(?<=[。！？；\n])", value)
        kept = [
            piece
            for piece in pieces
            if piece.strip()
            and not cls._contains_false_claim(piece, false_claims)
        ]
        return "".join(kept).strip()

    @classmethod
    def _apply_manual_correction_constraints(
        cls,
        payload: Dict[str, Any],
        corrections: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        normalized = dict(payload or {})
        false_claims = [
            str(claim or "").strip()
            for correction in corrections
            for claim in correction.get("false_claims") or []
            if str(claim or "").strip()
        ]
        if not false_claims:
            return normalized
        normalized["summary"] = cls._remove_false_claim_sentences(
            normalized.get("summary"),
            false_claims,
        )
        for key in (
            "stable_facts",
            "shared_claims",
            "active_topics",
            "group_dynamics",
            "open_items",
            "stale_or_uncertain",
        ):
            normalized[key] = [
                value
                for value in cls._string_list(
                    normalized.get(key),
                    80,
                    500,
                )
                if not cls._contains_false_claim(value, false_claims)
            ]
        normalized.pop("people_updates", None)
        return normalized

    def build_retrieval_context(
        self,
        chat_name: str,
        *,
        sender: str,
        content: str,
        recent_messages: Sequence[Dict[str, Any]],
        config: Dict[str, Any],
    ) -> Tuple[str, Dict[str, Any]]:
        """Build bounded stage + people + event context for the current turn."""
        started_at = time.perf_counter()
        trace_id = uuid.uuid4().hex
        trace_base = {
            "trace_id": trace_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "chat_name": chat_name,
            "enabled": bool(config.get("memory_enabled", True)),
            "query_sender": str(sender or ""),
        }
        if not config.get("memory_enabled", True):
            trace = {
                **trace_base,
                "token_budget": 0,
                "tokens": 0,
                "retrieval_ms": round((time.perf_counter() - started_at) * 1000, 1),
                "vector_ready": False,
                "stage": {"included": False},
                "events": [],
                "people": [],
                "dropped_events": [],
                "dropped_people": [],
                "candidate_event_count": 0,
                "candidate_people_count": 0,
            }
            return "", {
                "event_count": 0,
                "people_count": 0,
                "vector_ready": False,
                "tokens": 0,
                "trace": trace,
            }

        query_messages = list(recent_messages)[
            -max(1, int(config.get("memory_query_recent_messages") or 12)) :
        ]
        recent_query_text = self.context_manager.format_messages(query_messages)
        query_text = str(content or "").strip()
        if query_messages:
            query_text += "\n" + recent_query_text
        use_embedding = bool(config.get("memory_embedding_enabled", True))
        events = self._retrieve_events(
            chat_name,
            query_text=query_text,
            sender=sender,
            top_k=max(1, min(20, int(config.get("memory_retrieval_top_k") or 6))),
            retention_days=max(0, int(config.get("memory_retention_days") or 0)),
            use_embedding=use_embedding,
            diversity_lambda=max(
                0.1,
                min(
                    1.0,
                    float(config.get("memory_retrieval_mmr_lambda") or 0.72),
                ),
            ),
            diversity_threshold=max(
                0.6,
                min(
                    0.999,
                    float(
                        config.get("memory_retrieval_diversity_threshold")
                        or 0.92
                    ),
                ),
            ),
        )
        event_participants = {
            str(name)
            for event in events
            for name in event.get("participants") or []
            if str(name).strip()
        }
        person_state = self.person_memory.ledger.get_chat_state(chat_name)
        people: List[Dict[str, Any]] = []
        if (
            config.get("memory_person_enabled", False)
            and person_state.get("mode") == "active"
        ):
            people = self.person_memory.select_profiles_for_query(
                chat_name,
                sender=sender,
                content=content,
                event_participants=event_participants,
                maximum_people=max(
                    1,
                    min(
                        6,
                        int(
                            config.get(
                                "memory_person_retrieval_max_people",
                                3,
                            )
                            or 3
                        ),
                    ),
                ),
            )
            for person in people:
                person["_retrieval_query"] = query_text
                person["_retrieval_max_items"] = max(
                    3,
                    min(
                        20,
                        int(
                            config.get(
                                "memory_person_retrieval_max_items",
                                12,
                            )
                            or 12
                        ),
                    ),
                )
                person["_include_high_sensitivity"] = bool(
                    config.get(
                        "memory_person_include_high_sensitivity",
                        False,
                    )
                )
        state = self.store.get_state(chat_name)
        stage_candidate = str(state.get("stage_summary") or "").strip()
        stage = (
            stage_candidate
            if self._stage_is_relevant_for_query(query_text, stage_candidate)
            else ""
        )

        if not any((stage, people, events)):
            vector_ready = use_embedding and self.embedding_service.ready
            trace = {
                **trace_base,
                "token_budget": max(
                    512,
                    int(config.get("memory_context_max_tokens") or 6000),
                ),
                "tokens": 0,
                "retrieval_ms": round((time.perf_counter() - started_at) * 1000, 1),
                "vector_ready": vector_ready,
                "stage": {"included": False},
                "events": [],
                "people": [],
                "dropped_events": [],
                "dropped_people": [],
                "candidate_event_count": 0,
                "candidate_people_count": 0,
            }
            return "", {
                "event_count": 0,
                "people_count": 0,
                "vector_ready": vector_ready,
                "tokens": 0,
                "trace": trace,
            }

        budget = max(512, int(config.get("memory_context_max_tokens") or 6000))
        text, composition = self._compose_retrieval_context(
            stage=stage,
            people=people,
            events=events,
            token_budget=budget,
        )
        vector_ready = use_embedding and self.embedding_service.ready
        tokens = self.context_manager.estimate_tokens(text)
        injected_events = [
            self._event_trace_item(item["event"], item["prompt_text"])
            for item in composition["events"]
        ]
        injected_people = [
            self._person_trace_item(item["person"], item["prompt_text"])
            for item in composition["people"]
        ]
        dropped_events = [
            self._event_trace_item(item["event"], "", drop_reason="token_budget")
            for item in composition["dropped_events"]
        ]
        dropped_people = [
            self._person_trace_item(item["person"], "", drop_reason="token_budget")
            for item in composition["dropped_people"]
        ]
        stage_prompt_text = composition.get("stage_text") or ""
        trace = {
            **trace_base,
            "token_budget": budget,
            "tokens": tokens,
            "retrieval_ms": round((time.perf_counter() - started_at) * 1000, 1),
            "vector_ready": vector_ready,
            "stage": {
                "included": bool(stage_prompt_text),
                "text": self._strip_section_heading(
                    stage_prompt_text,
                    "## 当前阶段记忆",
                ),
                "prompt_text": stage_prompt_text,
                "truncated": bool(composition.get("stage_truncated")),
                "source_event_id": int(state.get("stage_source_event_id") or 0),
                "updated_at": state.get("stage_updated_at"),
            },
            "events": injected_events,
            "people": injected_people,
            "dropped_events": dropped_events,
            "dropped_people": dropped_people,
            "candidate_event_count": len(events),
            "candidate_people_count": len(people),
        }
        return text, {
            "event_count": len(injected_events),
            "people_count": len(injected_people),
            "candidate_event_count": len(events),
            "candidate_people_count": len(people),
            "vector_ready": vector_ready,
            "tokens": tokens,
            "trace": trace,
        }

    @classmethod
    def _stage_is_relevant_for_query(
        cls,
        query_text: str,
        stage_text: str,
    ) -> bool:
        """Gate the broad stage overview with a local, deterministic check."""
        if not str(stage_text or "").strip():
            return False
        query = str(query_text or "").strip()
        if re.search(
            r"最近群里|群里最近|这段时间|呢排|近期情况|当前情况|"
            r"最近进展|整体情况|大家最近|最近在聊|群里在聊|发生了什么",
            query,
            flags=re.IGNORECASE,
        ):
            return True
        query_tokens = cls._lexical_tokens(query)
        stage_tokens = cls._lexical_tokens(stage_text)
        if not query_tokens or not stage_tokens:
            return False
        overlap = query_tokens & stage_tokens
        return len(overlap) >= 2 or (
            len(overlap) == 1 and len(query_tokens) <= 3
        )

    def _compose_retrieval_context(
        self,
        *,
        stage: str,
        people: Sequence[Dict[str, Any]],
        events: Sequence[Dict[str, Any]],
        token_budget: int,
    ) -> Tuple[str, Dict[str, Any]]:
        """Compose memory while keeping event and person entries atomic."""
        intro = (
            "以下是系统按当前话题检索出的群聊记忆。它可能不完整；最近原始消息优先。"
            "仅在直接相关时自然使用，不要提及检索、事件卡、向量或记忆系统。"
        )
        raw_stage = "## 当前阶段记忆\n" + stage if stage else ""
        people_items = [
            (person, self._render_person_for_prompt(person))
            for person in people
        ]
        event_items = [
            (event, self._render_event_for_prompt(event))
            for event in events
        ]

        weights: Dict[str, float] = {}
        if stage:
            weights["stage"] = 0.35
        if people:
            weights["people"] = 0.25
        if events:
            weights["events"] = 0.50

        # Reserve separators so the final join cannot split an event card.
        intro_tokens = self.context_manager.estimate_tokens(intro)
        usable = max(128, int(token_budget) - intro_tokens - 24)
        total_weight = sum(weights.values()) or 1.0
        caps = {
            key: max(0, int(usable * weight / total_weight))
            for key, weight in weights.items()
        }

        # A retrieved event is more useful than a long stage preamble. Ensure
        # the first event can be included whole whenever it fits the turn.
        if event_items:
            first_event_tokens = self.context_manager.estimate_tokens(
                "## 检索到的相关历史事件\n" + event_items[0][1]
            )
            if first_event_tokens <= usable and first_event_tokens > caps.get("events", 0):
                extra = first_event_tokens - caps.get("events", 0)
                caps["events"] = first_event_tokens
                for key in ("stage", "people"):
                    reduction = min(extra, caps.get(key, 0))
                    caps[key] = caps.get(key, 0) - reduction
                    extra -= reduction
                    if extra <= 0:
                        break

        rendered: Dict[str, str] = {}
        selected_people: List[Tuple[Dict[str, Any], str]] = []
        selected_events: List[Tuple[Dict[str, Any], str]] = []

        if raw_stage and caps.get("stage", 0) > 0:
            rendered["stage"] = self._truncate_section_to_budget(
                raw_stage,
                caps["stage"],
                "stage部分达到预算上限",
            )
        if people_items and caps.get("people", 0) > 0:
            rendered["people"], selected_people = self._render_atomic_section(
                "## 本轮相关人物资料",
                people_items,
                caps["people"],
                separator="\n",
            )
        if event_items and caps.get("events", 0) > 0:
            rendered["events"], selected_events = self._render_atomic_section(
                "## 检索到的相关历史事件",
                event_items,
                caps["events"],
                separator="\n\n",
            )

        used = sum(
            self.context_manager.estimate_tokens(value)
            for value in rendered.values()
        )
        leftover = max(0, usable - used)
        for key in ("events", "stage", "people"):
            if key not in weights or leftover <= 0:
                continue
            current_tokens = self.context_manager.estimate_tokens(
                rendered.get(key, "")
            )
            expanded_cap = current_tokens + leftover
            if key == "events":
                expanded, expanded_items = self._render_atomic_section(
                    "## 检索到的相关历史事件",
                    event_items,
                    expanded_cap,
                    separator="\n\n",
                )
                selected_events = expanded_items
            elif key == "people":
                expanded, expanded_items = self._render_atomic_section(
                    "## 本轮相关人物资料",
                    people_items,
                    expanded_cap,
                    separator="\n",
                )
                selected_people = expanded_items
            else:
                expanded = self._truncate_section_to_budget(
                    raw_stage,
                    expanded_cap,
                    "stage部分达到预算上限",
                )
            rendered[key] = expanded
            expanded_tokens = self.context_manager.estimate_tokens(expanded)
            leftover = max(0, leftover - max(0, expanded_tokens - current_tokens))

        ordered = [
            rendered[key]
            for key in ("stage", "people", "events")
            if rendered.get(key)
        ]
        text = intro + "\n\n" + "\n\n".join(ordered) if ordered else ""

        selected_people_ids = {id(person) for person, _ in selected_people}
        selected_event_ids = {int(event.get("id") or 0) for event, _ in selected_events}
        composition = {
            "stage_text": rendered.get("stage", ""),
            "stage_truncated": bool(
                raw_stage
                and rendered.get("stage")
                and rendered.get("stage") != raw_stage
            ),
            "people": [
                {"person": person, "prompt_text": prompt_text}
                for person, prompt_text in selected_people
            ],
            "events": [
                {"event": event, "prompt_text": prompt_text}
                for event, prompt_text in selected_events
            ],
            "dropped_people": [
                {"person": person}
                for person, _ in people_items
                if id(person) not in selected_people_ids
            ],
            "dropped_events": [
                {"event": event}
                for event, _ in event_items
                if int(event.get("id") or 0) not in selected_event_ids
            ],
        }
        return text, composition

    @staticmethod
    def _render_person_for_prompt(person: Dict[str, Any]) -> str:
        return PersonMemoryEngine.render_profile_for_query(
            person,
            str(person.get("_retrieval_query") or ""),
            maximum_items=int(person.get("_retrieval_max_items") or 12),
            include_high_sensitivity=bool(
                person.get("_include_high_sensitivity", False)
            ),
        )

    def _truncate_section_to_budget(
        self,
        text: str,
        token_budget: int,
        notice: str,
    ) -> str:
        """Truncate one free-form section while keeping its notice in budget."""
        if not text or token_budget <= 0:
            return ""
        if self.context_manager.estimate_tokens(text) <= token_budget:
            return text
        suffix = f"\n\n（{notice}）"
        suffix_tokens = self.context_manager.estimate_tokens(suffix)
        prefix_budget = max(0, token_budget - suffix_tokens)
        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if self.context_manager.estimate_tokens(text[:mid]) <= prefix_budget:
                low = mid
            else:
                high = mid - 1
        prefix = text[:low].rstrip()
        return (prefix + suffix) if prefix else suffix.strip()

    def _render_atomic_section(
        self,
        heading: str,
        items: Sequence[Tuple[Dict[str, Any], str]],
        token_budget: int,
        *,
        separator: str,
    ) -> Tuple[str, List[Tuple[Dict[str, Any], str]]]:
        """Include only complete items so the audit matches the actual prompt."""
        if not items or token_budget <= 0:
            return "", []
        selected: List[Tuple[Dict[str, Any], str]] = []
        for item, prompt_text in items:
            candidate_items = [value for _, value in selected] + [prompt_text]
            candidate = heading + "\n" + separator.join(candidate_items)
            if self.context_manager.estimate_tokens(candidate) > token_budget:
                break
            selected.append((item, prompt_text))
        if not selected:
            return "", []
        return (
            heading + "\n" + separator.join(value for _, value in selected),
            selected,
        )

    @staticmethod
    def _strip_section_heading(text: str, heading: str) -> str:
        value = str(text or "").strip()
        if value.startswith(heading):
            value = value[len(heading) :].lstrip()
        return value

    @staticmethod
    def _event_trace_item(
        event: Dict[str, Any],
        prompt_text: str,
        *,
        drop_reason: str = "",
    ) -> Dict[str, Any]:
        card = event.get("card") if isinstance(event.get("card"), dict) else {}
        return {
            "id": int(event.get("id") or 0),
            "title": event.get("title") or "未命名事件",
            "summary": event.get("summary") or "",
            "start_time": event.get("start_time") or "",
            "end_time": event.get("end_time") or "",
            "source_start_cursor": int(event.get("source_start_cursor") or 0),
            "source_end_cursor": int(event.get("source_end_cursor") or 0),
            "supersedes_event_id": int(event.get("supersedes_event_id") or 0),
            "superseded_by_event_id": int(event.get("superseded_by_event_id") or 0),
            "relation_reason": event.get("relation_reason") or "",
            "participants": list(event.get("participants") or []),
            "keywords": list(event.get("keywords") or []),
            "decisions": list(event.get("decisions") or []),
            "open_items": list(event.get("open_items") or []),
            "importance": round(float(event.get("importance") or 0.0), 4),
            "event_type": card.get("event_type") or "",
            "certainty": card.get("certainty") or "",
            "source_note": card.get("source_note") or "",
            "claims": list(card.get("claims") or []),
            "evidence_cursors": list(card.get("evidence_cursors") or []),
            "verification_status": event.get("verification_status")
            or card.get("verification_status")
            or "not_required",
            "verification_note": event.get("verification_note")
            or card.get("verification_note")
            or "",
            "retrieval_score": round(float(event.get("retrieval_score") or 0.0), 4),
            "score_breakdown": dict(event.get("score_breakdown") or {}),
            "match_reasons": list(event.get("match_reasons") or []),
            "matched_keywords": list(event.get("matched_keywords") or []),
            "matched_participants": list(event.get("matched_participants") or []),
            "prompt_text": prompt_text,
            "drop_reason": drop_reason,
        }

    @staticmethod
    def _person_trace_item(
        person: Dict[str, Any],
        prompt_text: str,
        *,
        drop_reason: str = "",
    ) -> Dict[str, Any]:
        return {
            "person_id": int(person.get("person_id") or 0),
            "name": person.get("person_name") or "",
            "aliases": [
                alias.get("alias_name")
                for alias in person.get("aliases") or []
                if alias.get("status") == "confirmed"
            ],
            "facts": list(person.get("facts") or []),
            "patterns": list(person.get("patterns") or []),
            "relationships": list(person.get("relationships") or []),
            "snapshot_id": int(person.get("snapshot_id") or 0),
            "snapshot_generation": int(person.get("generation") or 0),
            "observation_count": int(person.get("observation_count") or 0),
            "profile_text": person.get("rendered_text") or "",
            "updated_at": person.get("updated_at"),
            "selection_reasons": list(person.get("selection_reasons") or []),
            "prompt_text": prompt_text,
            "drop_reason": drop_reason,
        }

    def _cached_events(
        self,
        chat_name: str,
    ) -> Tuple[
        List[Dict[str, Any]],
        Dict[int, Tuple[np.ndarray, List[int]]],
    ]:
        latest_id = self.store.latest_event_id(chat_name)
        count = self.store.count_events(chat_name, active_only=True)
        with self._retrieval_cache_lock:
            cached = self._retrieval_cache.get(chat_name)
            if cached and cached[0] == latest_id and cached[1] == count:
                self._retrieval_cache.move_to_end(chat_name)
                return cached[2], cached[3]
        events = self.store.list_events(chat_name, active_only=True)
        grouped: Dict[int, List[Tuple[int, np.ndarray]]] = {}
        for index, event in enumerate(events):
            event["_lexical_tokens"] = self._lexical_tokens(
                event.get("search_text") or ""
            )
            vector = self._normalized_vector(event.get("embedding"))
            if vector is not None:
                event["embedding"] = vector
                grouped.setdefault(int(vector.size), []).append((index, vector))
        vector_indexes = {
            dimension: (
                np.vstack([vector for _, vector in values]),
                [index for index, _ in values],
            )
            for dimension, values in grouped.items()
        }
        with self._retrieval_cache_lock:
            self._retrieval_cache[chat_name] = (
                latest_id,
                count,
                events,
                vector_indexes,
            )
            self._retrieval_cache.move_to_end(chat_name)
            while len(self._retrieval_cache) > self._retrieval_cache_max_chats:
                self._retrieval_cache.popitem(last=False)
        return events, vector_indexes

    def _retrieve_events(
        self,
        chat_name: str,
        *,
        query_text: str,
        sender: str,
        top_k: int,
        retention_days: int,
        use_embedding: bool,
        diversity_lambda: float,
        diversity_threshold: float,
    ) -> List[Dict[str, Any]]:
        events, vector_indexes = self._cached_events(chat_name)
        all_events = events
        if retention_days > 0:
            cutoff = datetime.now() - timedelta(days=retention_days)
            events = [
                event
                for event in events
                if self._parse_time(event.get("end_time") or event.get("created_at"))
                >= cutoff
            ]
        if not events:
            return []

        query_vector = self._normalized_vector(
            self.embedding_service.embed_query(query_text)
            if use_embedding
            else None
        )
        semantic_by_id: Dict[int, float] = {}
        if query_vector is not None:
            cached_vectors = vector_indexes.get(int(query_vector.size))
            if cached_vectors:
                matrix, all_event_indexes = cached_vectors
                values = matrix @ query_vector
                semantic_by_id = {
                    int(all_events[index]["id"]): float(score)
                    for index, score in zip(all_event_indexes, values)
                }

        query_tokens = self._lexical_tokens(query_text)
        now = datetime.now()
        ranked = []
        for event in events:
            event_tokens = event.get("_lexical_tokens") or set()
            lexical = (
                len(query_tokens & event_tokens) / max(1, len(query_tokens))
                if query_tokens
                else 0.0
            )
            participants = [str(item) for item in event.get("participants") or []]
            keywords = [str(item) for item in event.get("keywords") or []]
            participant_match = 1.0 if any(
                name and (name == sender or name in query_text)
                for name in participants
            ) else 0.0
            keyword_match = (
                sum(1 for keyword in keywords if keyword and keyword in query_text)
                / max(1, min(4, len(keywords)))
            )
            age_days = max(
                0.0,
                (now - self._parse_time(event.get("end_time") or event.get("created_at")))
                .total_seconds()
                / 86400,
            )
            recency = math.exp(-age_days / 90.0)
            importance = max(0.0, min(1.0, float(event.get("importance") or 0.5)))
            semantic = semantic_by_id.get(int(event["id"]), 0.0)
            if query_vector is not None:
                score = (
                    semantic * 0.60
                    + lexical * 0.15
                    + keyword_match * 0.10
                    + participant_match * 0.08
                    + recency * 0.04
                    + importance * 0.03
                )
                relevant = (
                    semantic >= 0.45
                    or lexical >= 0.15
                    or keyword_match > 0
                    or participant_match > 0
                )
            else:
                score = (
                    lexical * 0.55
                    + keyword_match * 0.20
                    + participant_match * 0.15
                    + recency * 0.05
                    + importance * 0.05
                )
                relevant = lexical > 0 or keyword_match > 0 or participant_match > 0
            if relevant:
                ranked.append((score, event))

        ranked.sort(key=lambda item: (item[0], item[1]["id"]), reverse=True)
        diversified = self._diversify_ranked_events(
            ranked,
            top_k=top_k,
            diversity_lambda=diversity_lambda,
            diversity_threshold=diversity_threshold,
        )
        result = []
        for score, event, mmr_score, redundancy in diversified:
            event_tokens = event.get("_lexical_tokens") or set()
            lexical = (
                len(query_tokens & event_tokens) / max(1, len(query_tokens))
                if query_tokens
                else 0.0
            )
            participants = [str(item) for item in event.get("participants") or []]
            keywords = [str(item) for item in event.get("keywords") or []]
            matched_participants = [
                name
                for name in participants
                if name and (name == sender or name in query_text)
            ]
            matched_keywords = [
                keyword
                for keyword in keywords
                if keyword and keyword in query_text
            ]
            participant_match = 1.0 if matched_participants else 0.0
            keyword_match = len(matched_keywords) / max(1, min(4, len(keywords)))
            age_days = max(
                0.0,
                (
                    now
                    - self._parse_time(
                        event.get("end_time") or event.get("created_at")
                    )
                ).total_seconds()
                / 86400,
            )
            recency = math.exp(-age_days / 90.0)
            importance = max(
                0.0,
                min(1.0, float(event.get("importance") or 0.5)),
            )
            semantic = semantic_by_id.get(int(event["id"]), 0.0)
            match_reasons = []
            if semantic >= 0.45:
                match_reasons.append("语义相似")
            if lexical >= 0.15:
                match_reasons.append("文字重合")
            if matched_keywords:
                match_reasons.append("关键词命中")
            if matched_participants:
                match_reasons.append("人物关联")
            if recency >= 0.85:
                match_reasons.append("近期事件")
            if importance >= 0.75:
                match_reasons.append("高重要度")

            value = dict(event)
            value.pop("embedding", None)
            value.pop("_lexical_tokens", None)
            value["retrieval_score"] = round(float(score), 4)
            value["score_breakdown"] = {
                "semantic": round(float(semantic), 4),
                "lexical": round(float(lexical), 4),
                "keyword": round(float(keyword_match), 4),
                "participant": round(float(participant_match), 4),
                "recency": round(float(recency), 4),
                "importance": round(float(importance), 4),
                "mmr": round(float(mmr_score), 4),
                "redundancy": round(float(redundancy), 4),
            }
            value["match_reasons"] = match_reasons
            value["matched_keywords"] = matched_keywords
            value["matched_participants"] = matched_participants
            result.append(value)
        return result

    def _diversify_ranked_events(
        self,
        ranked: Sequence[Tuple[float, Dict[str, Any]]],
        *,
        top_k: int,
        diversity_lambda: float,
        diversity_threshold: float,
    ) -> List[Tuple[float, Dict[str, Any], float, float]]:
        """Apply bounded MMR so near-identical cards cannot fill the prompt."""
        if not ranked or top_k <= 0:
            return []
        pool = list(ranked[: max(40, top_k * 8)])
        selected: List[Tuple[float, Dict[str, Any], float, float]] = []
        while pool and len(selected) < top_k:
            best_index = -1
            best_key: Optional[Tuple[float, float, int]] = None
            best_mmr = 0.0
            best_redundancy = 0.0
            for index, (base_score, event) in enumerate(pool):
                redundancy = 0.0
                if selected:
                    redundancy = max(
                        self._event_similarity(event, selected_event)
                        for _, selected_event, _, _ in selected
                    )
                    if redundancy >= diversity_threshold:
                        continue
                mmr_score = (
                    diversity_lambda * float(base_score)
                    - (1.0 - diversity_lambda) * max(0.0, redundancy)
                )
                key = (mmr_score, float(base_score), int(event.get("id") or 0))
                if best_key is None or key > best_key:
                    best_index = index
                    best_key = key
                    best_mmr = mmr_score
                    best_redundancy = redundancy
            if best_index < 0:
                break
            base_score, event = pool.pop(best_index)
            selected.append(
                (base_score, event, best_mmr, best_redundancy)
            )
        return selected

    def _event_similarity(
        self,
        left: Dict[str, Any],
        right: Dict[str, Any],
    ) -> float:
        left_vector = self._normalized_vector(left.get("embedding"))
        right_vector = self._normalized_vector(right.get("embedding"))
        if (
            left_vector is not None
            and right_vector is not None
            and left_vector.size == right_vector.size
        ):
            return float(left_vector @ right_vector)
        left_tokens = self._lexical_tokens(left.get("search_text") or "")
        right_tokens = self._lexical_tokens(right.get("search_text") or "")
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / max(
            1,
            len(left_tokens | right_tokens),
        )

    @staticmethod
    def _lexical_tokens(text: Any) -> set[str]:
        value = str(text or "").lower()
        tokens = set(re.findall(r"[a-z0-9_][a-z0-9_.-]{1,}", value))
        for block in re.findall(r"[\u4e00-\u9fff]{2,}", value):
            if len(block) <= 4:
                tokens.add(block)
            tokens.update(block[index : index + 2] for index in range(len(block) - 1))
        tokens.difference_update(
            {
                "什么",
                "怎么",
                "如何",
                "这个",
                "那个",
                "是否",
                "有没有",
                "当前",
                "消息",
                "最近",
                "上下",
                "问题",
                "一下",
            }
        )
        return tokens

    @staticmethod
    def _parse_time(value: Any) -> datetime:
        text = str(value or "").strip()
        if not text:
            return datetime.min
        try:
            parsed = datetime.fromisoformat(text)
            return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
        except ValueError:
            return datetime.min

    @staticmethod
    def _render_event_for_prompt(event: Dict[str, Any]) -> str:
        card = event.get("card") if isinstance(event.get("card"), dict) else {}
        parts = [
            f"### {event.get('title') or '未命名事件'}",
            f"时间：{event.get('start_time') or '?'} ～ {event.get('end_time') or '?'}",
            f"摘要：{event.get('summary') or ''}",
        ]
        if card.get("event_type") or card.get("certainty"):
            parts.append(
                "性质："
                f"{card.get('event_type') or 'unknown'}；"
                f"可信状态：{card.get('certainty') or 'unknown'}"
            )
        if card.get("source_note"):
            parts.append(f"来源说明：{card['source_note']}")
        if int(event.get("supersedes_event_id") or 0) > 0:
            parts.append(
                f"版本：替代事件 #{int(event['supersedes_event_id'])}；"
                f"{event.get('relation_reason') or '同话题新进展'}"
            )
        if event.get("participants"):
            parts.append(f"参与者：{'、'.join(event['participants'])}")
        if event.get("decisions"):
            parts.append(f"明确结论：{'；'.join(event['decisions'])}")
        if event.get("open_items"):
            parts.append(f"未完成事项：{'；'.join(event['open_items'])}")
        return "\n".join(parts)

    def get_checkpoint_text(
        self,
        chat_name: str,
        *,
        token_budget: int,
    ) -> Tuple[str, int]:
        """Return a stable stage snapshot for a newly rotated anchored thread."""
        stage = str(self.store.get_state(chat_name).get("stage_summary") or "").strip()
        if not stage:
            return "", 0
        text = self.context_manager.truncate_text_to_budget(
            "## 群聊阶段记忆\n" + stage,
            max(256, int(token_budget)),
            notice="阶段记忆检查点达到预算上限",
        )
        return text, self.context_manager.estimate_tokens(text)

    def get_memory_document(self, chat_name: str) -> Dict[str, Any]:
        state = self.store.get_state(chat_name)
        event_count = self.store.count_events(chat_name)
        active_event_count = self.store.count_events(
            chat_name,
            active_only=True,
        )
        _, superseded_event_count = self.store.browse_events(
            chat_name,
            status="superseded",
            limit=1,
        )
        _, invalidated_event_count = self.store.browse_events(
            chat_name,
            status="invalidated",
            limit=1,
        )
        _, quarantined_event_count = self.store.browse_events(
            chat_name,
            status="quarantined",
            limit=1,
        )
        corrections = self.store.list_corrections(
            chat_name,
            include_snapshots=False,
        )
        person_state = self.person_memory.ledger.get_chat_state(chat_name)
        person_observations = (
            self.person_memory.ledger.observation_stats(chat_name)
            if person_state
            else {
                "total": 0,
                "active": 0,
                "quarantined": 0,
                "rejected": 0,
            }
        )
        person_profile_count = (
            self.person_memory.ledger.count_profiles(chat_name)
            if person_state
            else 0
        )
        people_count = person_profile_count
        return {
            "chat_name": chat_name,
            "stage_memory": {
                "summary": state.get("stage_summary") or "",
                "structured": state.get("stage_json") or {},
                "mode": state.get("stage_mode") or "auto",
                "manual_note": state.get("stage_manual_note") or "",
                "manual_updated_at": state.get("stage_manual_updated_at"),
                "updated_at": state.get("stage_updated_at"),
                "source_event_id": int(state.get("stage_source_event_id") or 0),
            },
            "event_count": event_count,
            "active_event_count": active_event_count,
            "superseded_event_count": superseded_event_count,
            "invalidated_event_count": invalidated_event_count,
            "quarantined_event_count": quarantined_event_count,
            "correction_count": len(corrections),
            "active_correction_count": sum(
                1
                for correction in corrections
                if correction.get("status") == "active"
            ),
            "people_count": people_count,
            "person_memory": {
                "schema_version": int(
                    person_state.get("schema_version") or 3
                ),
                "mode": person_state.get("mode") or "not_initialized",
                "observation_source_cursor": int(
                    person_state.get("observation_source_cursor") or 0
                ),
                "last_observation_at": person_state.get(
                    "last_observation_at"
                ),
                "last_consolidation_at": person_state.get(
                    "last_consolidation_at"
                ),
                "activated_at": person_state.get("activated_at"),
                "profile_count": person_profile_count,
                "observations": person_observations,
            },
            "source_cursor": int(state.get("source_cursor") or 0),
            "source_message_count": int(state.get("source_message_count") or 0),
            "recent_events": [
                {
                    "id": event["id"],
                    "title": event.get("title"),
                    "start_time": event.get("start_time"),
                    "end_time": event.get("end_time"),
                    "importance": event.get("importance"),
                }
                for event in self.store.list_events(
                    chat_name,
                    newest_first=True,
                    limit=10,
                    active_only=True,
                )
            ],
        }

    def _prepare_correction_derived_repair(
        self,
        chat_name: str,
        *,
        target_event: Dict[str, Any],
        reason: str,
        false_claims: Sequence[str],
        corrected_claim: str,
        affected_people: Sequence[str],
    ) -> Dict[str, Any]:
        state = self.store.get_state(chat_name)
        stage_payload = (
            dict(state.get("stage_json") or {})
            if isinstance(state.get("stage_json"), dict)
            else {}
        )
        if not stage_payload:
            stage_payload = {
                "summary": str(state.get("stage_summary") or ""),
                "stable_facts": [],
                "shared_claims": [],
                "active_topics": [],
                "group_dynamics": [],
                "open_items": [],
                "stale_or_uncertain": [],
            }
        stage_payload.pop("people_updates", None)
        correction = {
            "id": 0,
            "reason": str(reason or "").strip(),
            "false_claims": list(false_claims),
            "corrected_claim": str(corrected_claim or "").strip(),
            "affected_people": [
                str(value or "").strip()
                for value in affected_people
                if str(value or "").strip()
            ],
        }
        prompt = [
            {
                "role": "system",
                "content": (
                    "你是记忆库人工纠错执行器。管理员已经核对原始聊天，给出的纠错结论"
                    "是最高优先级事实，不得质疑。请只删除或修正与纠错冲突的阶段记忆，"
                    "其他内容尽量原样保留。人物证据账本由独立流程处理，不要输出人物资料。"
                    "不得从错误事件继续推断。输出一个JSON对象。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"群聊：{chat_name}\n"
                    f"错误事件：{json.dumps(self._json_safe_event(target_event), ensure_ascii=False)}\n"
                    f"人工纠错：{json.dumps(correction, ensure_ascii=False)}\n"
                    "当前阶段结构："
                    f"{json.dumps(stage_payload, ensure_ascii=False)}\n\n"
                    "严格输出当前完整阶段结构："
                    '{"summary":"","stable_facts":[],"shared_claims":[],'
                    '"active_topics":[],"group_dynamics":[],"open_items":[],'
                    '"stale_or_uncertain":[]}'
                ),
            },
        ]
        try:
            repaired = self._call_memory_json(
                call_type="memory_stage_summarize",
                messages=prompt,
                schema_hint="根对象必须包含阶段记忆各数组。",
                chat_name=chat_name,
            )
        except Exception as exc:
            logger.warning(
                "⚠️ Manual correction derived repair fell back to "
                "deterministic filtering for %s: %s",
                chat_name,
                exc,
            )
            repaired = dict(stage_payload)
        repaired.pop("people_updates", None)
        repaired = self._normalize_stage_categories(repaired)
        repaired = self._apply_manual_correction_constraints(
            repaired,
            [correction],
        )
        repaired.pop("people_updates", None)
        rendered = self._render_stage(repaired)
        if not rendered:
            rendered = self._remove_false_claim_sentences(
                state.get("stage_summary") or "",
                false_claims,
            )
        return {
            "stage": {
                "summary": rendered,
                "structured": repaired,
            },
        }

    @staticmethod
    def _json_safe_event(event: Dict[str, Any]) -> Dict[str, Any]:
        result = {}
        for key, value in event.items():
            if key == "embedding":
                continue
            if isinstance(value, np.ndarray):
                continue
            result[key] = value
        return result

    def correct_event_manual(
        self,
        chat_name: str,
        *,
        event_id: int,
        action: str,
        reason: str,
        false_claims: Sequence[str],
        corrected_claim: str = "",
        affected_people: Sequence[str] = (),
        existing_replacement_event_id: int = 0,
        corrected_event_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        target = self.store.get_event(chat_name, int(event_id))
        if not target:
            raise ValueError("memory event does not exist")
        people_names = list(
            dict.fromkeys(
                [
                    *(
                        str(value or "").strip()
                        for value in affected_people
                    ),
                    *(
                        str(value or "").strip()
                        for value in target.get("participants") or []
                    ),
                ]
            )
        )
        people_names = [value for value in people_names if value][:30]
        claims = [
            str(value or "").strip()
            for value in false_claims
            if str(value or "").strip()
        ]
        if not claims:
            raise ValueError("at least one false claim is required")

        derived = self._prepare_correction_derived_repair(
            chat_name,
            target_event=target,
            reason=reason,
            false_claims=claims,
            corrected_claim=corrected_claim,
            affected_people=people_names,
        )
        corrected_event = None
        if str(action or "").strip().lower() == "create_revision":
            fields = dict(corrected_event_fields or {})
            source_start = max(
                1,
                int(
                    fields.get("source_start_cursor")
                    or target.get("source_start_cursor")
                    or 1
                ),
            )
            source_end = max(
                source_start,
                int(
                    fields.get("source_end_cursor")
                    or target.get("source_end_cursor")
                    or source_start
                ),
            )
            from app.assistant.memory_source import (
                read_event_source,
            )

            source_event = {
                **target,
                "id": 0,
                "source_start_cursor": source_start,
                "source_end_cursor": source_end,
            }
            source_messages = read_event_source(
                self.store,
                source_event,
                limit=min(200, source_end - source_start + 1),
            )
            participants = self._string_list(
                fields.get("participants")
                or target.get("participants"),
                30,
                80,
            )
            keywords = self._string_list(
                fields.get("keywords") or target.get("keywords"),
                20,
                80,
            )
            event_type = self._enum_value(
                fields.get("event_type"),
                {
                    "personal_update",
                    "group_decision",
                    "question",
                    "debate",
                    "shared_info",
                    "joke",
                    "other",
                },
                "other",
            )
            certainty = self._enum_value(
                fields.get("certainty"),
                {
                    "confirmed_in_chat",
                    "self_report",
                    "attributed_claim",
                    "participant_report",
                    "unverified_external",
                    "rumor_or_joke",
                },
                "confirmed_in_chat",
            )
            corrected_event = {
                "source_namespace": target.get("source_namespace")
                or "live_chat_log",
                "source_start_cursor": source_start,
                "source_end_cursor": source_end,
                "start_time": (
                    str(source_messages[0].get("time") or "")
                    if source_messages
                    else str(target.get("start_time") or "")
                ),
                "end_time": (
                    str(source_messages[-1].get("time") or "")
                    if source_messages
                    else str(target.get("end_time") or "")
                ),
                "title": self._clean_text(
                    fields.get("title"),
                    120,
                )
                or f"修正：{target.get('title') or '事件'}",
                "summary": self._clean_text(
                    fields.get("summary") or corrected_claim,
                    1200,
                ),
                "participants": participants,
                "keywords": keywords,
                "opinions": self._dict_list(
                    fields.get("opinions"),
                    20,
                ),
                "decisions": self._string_list(
                    fields.get("decisions"),
                    20,
                    240,
                ),
                "open_items": self._string_list(
                    fields.get("open_items"),
                    20,
                    240,
                ),
                "importance": max(
                    0.0,
                    min(
                        1.0,
                        float(
                            fields.get("importance")
                            or target.get("importance")
                            or 0.5
                        ),
                    ),
                ),
                "source_messages": source_messages,
            }
            card = {
                key: corrected_event[key]
                for key in (
                    "title",
                    "summary",
                    "source_start_cursor",
                    "source_end_cursor",
                    "start_time",
                    "end_time",
                    "participants",
                    "keywords",
                    "opinions",
                    "decisions",
                    "open_items",
                    "importance",
                )
            }
            card.update(
                {
                    "event_type": event_type,
                    "certainty": certainty,
                    "source_note": self._clean_text(
                        fields.get("source_note"),
                        300,
                    )
                    or f"人工核对并修正事件 #{int(event_id)}",
                    "manually_verified": True,
                }
            )
            corrected_event["card"] = card
            corrected_event["search_text"] = self._event_search_text(card)
            if self.embedding_service.warmup():
                vectors = self.embedding_service.embed_passages(
                    [corrected_event["search_text"]]
                )
                corrected_event["embedding"] = (
                    vectors[0] if vectors else None
                )

        correction = self.store.apply_event_correction(
            chat_name,
            target_event_id=int(event_id),
            action=action,
            reason=reason,
            false_claims=claims,
            corrected_claim=corrected_claim,
            affected_people=people_names,
            corrected_event=corrected_event,
            existing_replacement_event_id=int(
                existing_replacement_event_id or 0
            ),
            stage_after=derived["stage"],
        )
        self.invalidate(chat_name)
        return {
            "correction": correction,
            "memory": self.get_memory_document(chat_name),
        }

    def revert_manual_correction(
        self,
        chat_name: str,
        correction_id: int,
    ) -> Dict[str, Any]:
        correction = self.store.revert_correction(
            chat_name,
            int(correction_id),
        )
        self.invalidate(chat_name)
        return {
            "correction": correction,
            "memory": self.get_memory_document(chat_name),
        }

    def delete_event_manual(
        self,
        chat_name: str,
        *,
        event_id: int,
        reason: str,
    ) -> Dict[str, Any]:
        target = self.store.get_event(chat_name, int(event_id))
        if not target:
            raise ValueError("memory event does not exist")
        false_claims = [
            value
            for value in (
                self._clean_text(target.get("title"), 300),
                self._clean_text(target.get("summary"), 1200),
            )
            if value
        ]
        if not false_claims:
            false_claims = [f"已删除事件 #{int(event_id)}"]
        return self.correct_event_manual(
            chat_name,
            event_id=int(event_id),
            action="delete",
            reason=reason,
            false_claims=false_claims,
            corrected_claim="",
            affected_people=target.get("participants") or [],
        )

    def update_stage_manual(
        self,
        chat_name: str,
        summary: str,
        *,
        mode: str = "manual",
        reason: str = "管理员编辑阶段记忆",
    ) -> Dict[str, Any]:
        if str(mode or "manual").strip().lower() == "auto":
            self.store.enable_automatic_stage(chat_name, reason=reason)
        else:
            self.store.update_stage_manual(
                chat_name,
                summary=str(summary or "").strip(),
                reason=reason,
            )
        self.invalidate(chat_name)
        return self.get_memory_document(chat_name)

    def clear_memory(self, chat_name: str, scope: str = "all") -> Dict[str, Any]:
        self.store.clear_chat(
            chat_name,
            scope=scope,
            reset_cursor=int(self.chat_log_manager.count_messages(chat_name) or 0),
            source_message_count=int(self.chat_log_manager.count_messages(chat_name) or 0),
        )
        self.invalidate(chat_name)
        return self.get_memory_document(chat_name)

    def invalidate(self, chat_name: str) -> None:
        with self._retrieval_cache_lock:
            self._retrieval_cache.pop(chat_name, None)
