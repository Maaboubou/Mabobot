"""从文本或引用图片触发的批量生图插件。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from pathlib import Path
from threading import Event as StopEvent

from PIL import Image

from app.core.event_bus import Event, EventType
from app.services import image_generation_assets, image_generation_service as generation
from app.services.codex_profile_service import (
    CodexProfileError, _managed_profile_permission_roots, get_codex_profile_service,
)
from app.services.codex_proxy.client import CodexCliClient
from app.services.engine_image_jobs import begin_image_task
from app.services.image_generation_config import resolve_policy
from app.services.image_generation_history import ImageReceipt
from app.utils.bot_mentions import strip_bot_mentions
from app.utils.plugin_config import get_config
from app.utils.source_message_policy import ActionBlocked, admitted_task_scope, event_is_fresh

logger = logging.getLogger(__name__)
PLUGIN_ID = "image_editor"


@dataclass
class DeliveryWindow:
    """Allow this freshly admitted request to report back to its original chat."""

    chat: str
    deadline: float
    stop_event: StopEvent
    source: object = None

    def require_delivery_allowed(self):
        if self.stop_event.is_set() or time.monotonic() >= self.deadline:
            raise ActionBlocked("生图任务已停止或已超过交付期限")
        return True


class ImageEditorPlugin:
    def __init__(self, context):
        self.context = context
        self.output_dir = (context.storage.generated_root / "images").resolve()

    def handle_text_message(self, event: Event) -> bool:
        data = event.data
        if event.type == EventType.LINK_MESSAGE_RECEIVED and data.get("message_type") not in {"text", "quote"}:
            return False
        keyword = str(get_config("trigger_keyword", "生图", plugin_name=PLUGIN_ID) or "生图").strip() or "生图"
        content = str(data.get("message") or "").strip()
        if not content.startswith(keyword):
            return False
        wx = event.context.get("wx")
        chat = str(data.get("chat_name") or "")
        if wx is None or not chat or not event_is_fresh(event):
            return False
        bot_names = (*event.context.get("bot_mention_names", ()), data.get("bot_mention_name"), "bot", "Bot")
        prompt = strip_bot_mentions(content[len(keyword):], bot_names, preserve_if_empty=False)
        if not prompt or len(prompt) > 32000:
            wx.send_message(chat, f"请在「{keyword}」后填写 1–32000 字的生图描述。")
            return True

        use_reference = event.type == EventType.QUOTE_IMAGE_MESSAGE_RECEIVED or (
            data.get("message_type") == "quote" and (
                data.get("has_quote_image") or data.get("has_quote_emotion")
                or "[图片]" in str(data.get("quote_content") or "")
            )
        )
        self.context.workers.start(
            f"generate-images-{uuid.uuid4().hex[:12]}",
            self._process_command,
            args=(event, wx, prompt, use_reference),
        )
        return True

    def _read_reference(self, event, wx, chat):
        path = event.data.get("quote_image_path")
        prefetch = event.context.get("quote_image_prefetch")
        if not path and prefetch is not None:
            result = prefetch.result(timeout=135)
            if result:
                if result.get("status") != "ready":
                    raise ValueError("引用图片预取失败")
                path = result.get("path")
        message_id = event.data.get("message_id")
        if not path and message_id and not event.data.get("quote_image_prefetch_attempted"):
            path = wx.download_quote_image(chat, message_id=message_id)
        if not path:
            raise ValueError("没有可读取的引用图片")
        path = Path(path)
        with path.open("rb") as source:
            data = source.read(generation.MAX_IMAGE_BYTES + 1)
        return generation.ImageInput.from_bytes(data, path.name)

    def _process_command(self, event, wx, prompt, use_reference):
        chat = str(event.data["chat_name"])
        stop_event = self.context.workers.stop_event
        if stop_event.is_set() or not event_is_fresh(event):
            return
        count = max(1, int(get_config("target_images", 1, plugin_name=PLUGIN_ID)))
        max_batches = max(1, int(get_config("max_batches", 3, plugin_name=PLUGIN_ID)))
        interval = max(0, float(get_config("request_interval", 10, plugin_name=PLUGIN_ID)))
        timeout = max(1, int(get_config("processing_timeout", 300, plugin_name=PLUGIN_ID)))
        budget = max_batches * (timeout + (count - 1) * interval)
        # The business task starts while the source is fresh. Its bounded result
        # delivery can outlive the original event without authorizing new actions.
        window = DeliveryWindow(chat, time.monotonic() + budget + (135 if use_reference else 0) + 120, stop_event)
        with admitted_task_scope(window, event):
            references = []
            if use_reference:
                try:
                    references.append(self._read_reference(event, wx, chat))
                except Exception:
                    logger.warning("生图参考图读取失败 chat=%s message_id=%s",
                                   chat, event.data.get("message_id"), exc_info=True)
                    if not stop_event.is_set():
                        wx.send_message(chat, "无法读取参考图，请重新引用图片后发送生图指令。")
                    return
            if not stop_event.is_set():
                self._process_batches(chat, wx, prompt, references, count, max_batches, interval, timeout, budget)

    def _process_batches(self, chat, wx, prompt, references, count, max_batches, interval, timeout, budget):
        from app.services.execution_contracts import InputEnvelope, canonical
        from app.services.execution_service import get_execution_service
        from app.services.image_generation_facade import ImageGenerationFacade, image_input_reference

        stop_event = self.context.workers.stop_event
        service = run_context = None
        outcome, delivery = "failed", "not_required"
        attempts = 0
        try:
            policy = resolve_policy(plugin_name=PLUGIN_ID)
            run_id = begin_image_task(PLUGIN_ID, policy, timeout=budget + 30)
            task_dir = self.output_dir / (run_id or uuid.uuid4().hex)
            if run_id:
                service = get_execution_service()
                row = service.store.get(run_id)
                if row["status"] not in {"queued", "preparing"} or row["input_json"] is not None:
                    return
                try:
                    task_dir.mkdir(parents=True, exist_ok=False)
                except FileExistsError:
                    return
                run_context, route = service.store.contracts(run_id)
                service.store.bind_input(run_id, InputEnvelope("Generate images", canonical({
                    "prompt": prompt, "inputs": [image_input_reference(image) for image in references],
                    "batch_size": count, "max_batches": max_batches, "interval": interval,
                })))
                snapshot = json.loads(route.image_policy_json)
                facade = ImageGenerationFacade(service.store, authorize=service.authorize)

            def stopping():
                return stop_event.is_set() or bool(
                    run_context and (
                        service.store.get(run_id)["cancel_requested"] or not service.authorize(run_context)
                    )
                )

            def generate_slot(batch, index):
                directory = task_dir / f"batch-{batch}" / f"request-{index}"
                request = generation.ImageRequest(
                    prompt, count=1, images=references, parameters={"output_format": "png"},
                    timeout=timeout, cancelled=stopping, actor=chat,
                )
                try:
                    if stopping():
                        return None
                    directory.mkdir(parents=True, exist_ok=True)
                    if run_context:
                        result = facade.generate(
                            run_context, f"image-editor-{batch}-{index}", snapshot, request, directory,
                            asset_request_id=task_dir.name,
                        )
                        return self._photo_for_wechat(result.get("paths", []), directory)
                    return self._generate_photo(policy, request, directory)
                except Exception as exc:
                    logger.warning("生图请求失败 chat=%s batch=%d request=%d error=%s",
                                   chat, batch, index, type(exc).__name__)
                    return None

            for batch in range(1, max_batches + 1):
                if stopping():
                    outcome, delivery = "cancelled", "blocked"
                    return
                attempts = batch
                with ThreadPoolExecutor(max_workers=count, thread_name_prefix="image-generation") as pool:
                    futures = []
                    for index in range(1, count + 1):
                        if index > 1 and stop_event.wait(interval):
                            break
                        if stopping():
                            break
                        futures.append(pool.submit(copy_context().run, generate_slot, batch, index))
                    paths = [path for future in futures if (path := future.result()) is not None]
                logger.info("生图批次结束 chat=%s batch=%d/%d images=%d/%d", chat, batch, max_batches, len(paths), count)
                if paths:
                    outcome = "completed"
                    delivery = self._deliver_batch(chat, wx, paths, task_dir.name, policy["mode"], stopping,
                                                   store=service.store if run_context else None, run_id=run_id)
                    return
            if stopping():
                outcome, delivery = "cancelled", "blocked"
                return
            message = f"生图失败，已尝试 {attempts} 批，未生成图片。"
        except ActionBlocked:
            delivery = "blocked"
            return
        except Exception:
            logger.exception("生图任务异常 chat=%s attempts=%d", chat, attempts)
            if not stop_event.is_set() and not (run_context and (
                service.store.get(run_context.run_id)["cancel_requested"] or not service.authorize(run_context)
            )):
                message = (f"生图任务未能完成，已尝试 {attempts} 批。" if attempts
                           else "生图任务未能启动，请检查生图插件和渠道配置。")
            else:
                return
        finally:
            if run_context:
                service.store.finish_business_run(run_context.run_id, outcome, delivery_status=delivery)
        wx.send_message(chat, message)

    def _generate_photo(self, policy, request, directory):
        if policy["mode"] == "api":
            receipt = ImageReceipt(request.actor)
            deadline = time.monotonic() + request.timeout
            try:
                result = generation.generate_images(policy, request, directory, progress=receipt.progress)
                receipt.finish("completed")
                return self._photo_for_wechat(result.paths, directory)
            except generation.ImageGenerationError as exc:
                receipt.finish("partial" if exc.paths else "failed")
                photo = self._photo_for_wechat(exc.paths, directory)
                if photo is not None:
                    return photo
                remaining = deadline - time.monotonic()
                if not generation.native_fallback_count(policy, request, exc) or remaining < 1 or request.cancelled():
                    raise
                return self._generate_native(request, directory, remaining)
            except Exception:
                receipt.finish("failed")
                raise
        return self._generate_native(request, directory, request.timeout)

    def _resolve_profile(self):
        service = get_codex_profile_service()
        profile_id = service.resolve_assistant_profile_id("")
        profile = service.get_profile(profile_id)
        if not profile or profile.get("auth_type") != "chatgpt":
            raise CodexProfileError("生图需要官方 ChatGPT 登录的全局默认 Codex Profile")
        if (profile.get("auth_source") == "local_cache"
                and profile.get("auth_sync_status") in {"missing", "outdated", "invalid"}):
            synced = service.sync_local_auth_if_needed(profile_id)
            profile = synced.get("profile") or service.get_profile(profile_id)
        if not profile or not profile.get("available"):
            raise CodexProfileError("全局默认 Codex Profile 尚未完成登录")
        return profile

    def _generate_native(self, request, directory, timeout):
        deadline = time.monotonic() + timeout
        profile = self._resolve_profile()
        timeout = deadline - time.monotonic()
        if timeout < 1 or request.cancelled():
            return None
        client = CodexCliClient(
            codex_bin=profile["wrapper_path"], workdir=str(directory), timeout_seconds=timeout,
            permission_read_roots=_managed_profile_permission_roots(profile), codex_home=profile.get("codex_home"),
        )
        content = [{"type": "text", "text": f"请生成 1 张静态图片。\n用户要求：{request.prompt}"}]
        content.extend({"type": "image_url", "image_url": {
            "url": f"data:{image.mime};base64,{base64.b64encode(image.data).decode()}",
        }} for image in request.images)
        observed = []
        try:
            response = asyncio.run(client.chat({
                "model": profile["model"], "reasoning_effort": profile.get("reasoning_effort") or "high",
                "timeout": timeout, "codex_runtime_profile": profile["name"], "codex_config_policy": "inherit",
                "codex_permission_profile": "mabobot-chat-isolated", "codex_approval_policy": "never",
                "codex_workdir": str(directory), "codex_artifact_root": str(directory / "artifacts"),
                "codex_source_chat_name": request.actor, "mabobot_allow_image_input": True,
                "messages": [
                    {"role": "system", "content": (
                        "使用 Codex 内置图片工具按用户描述生成或编辑一张静态 PNG 图片。"
                        "如有参考图，请使用参考图。宿主负责收集图片，完成生成后结束。"
                        "请勿使用外部生图 API、子代理或代码绘图替代。"
                    )},
                    {"role": "user", "content": content},
                ],
            }, on_image=observed.append))
        except Exception:
            photo = self._photo_for_wechat(reversed(observed), directory)
            if photo is not None:
                return photo
            raise
        paths = [item["path"] for item in response.get("attachments") or []
                 if item.get("type") == "image" and item.get("path")]
        return self._photo_for_wechat([*paths, *reversed(observed)], directory)

    @staticmethod
    def _photo_for_wechat(paths, directory):
        """Return one validated photo, with a PNG copy if WeChat needs it."""
        for raw in paths:
            try:
                path = Path(raw)
                if path.is_symlink() or not path.resolve().is_relative_to(directory):
                    continue
                with path.open("rb") as source:
                    kind, _ = generation.inspect_image(source.read(generation.MAX_IMAGE_BYTES + 1))
                if (kind == "png" and path.suffix.lower() == ".png"
                        or kind == "jpeg" and path.suffix.lower() in {".jpg", ".jpeg"}):
                    return path
                destination = directory / "wechat-photo.png"
                with Image.open(path) as image:
                    image.save(destination, format="PNG")
                return destination
            except (OSError, ValueError, generation.ImageGenerationError):
                logger.warning("生图结果无法作为微信照片交付", exc_info=True)
        return None

    @staticmethod
    def _deliver_batch(chat, wx, paths, request_id, source_mode, stopping, *, store, run_id):
        asset_ids = []
        try:
            assets = image_generation_assets.finalize_images(
                chat, paths, request_id=request_id, source=f"image_editor_{source_mode}",
            )
            asset_ids = [asset["asset_id"] for asset in assets]
        except Exception:
            logger.warning("生图资产登记失败，继续交付已生成照片 chat=%s", chat, exc_info=True)
        status, delivery = "interrupted", "blocked"
        try:
            if stopping():
                return delivery
            if store:
                store.delivery_status(run_id, "sending")
            status, delivery = "failed", "uncertain"
            if wx.send_files(chat, [str(path) for path in paths]):
                status = delivery = "sent"
            else:
                wx.send_message(chat, f"已生成 {len(paths)} 张图片，但微信发送失败，图片已保留。")
        except ActionBlocked:
            status, delivery = "interrupted", "blocked"
        except Exception:
            logger.exception("生图批量交付失败 chat=%s images=%d", chat, len(paths))
        finally:
            if asset_ids:
                try:
                    image_generation_assets.mark_delivery(chat, asset_ids, status)
                except Exception:
                    logger.warning("生图资产交付状态未保存 chat=%s", chat, exc_info=True)
        return delivery


image_editor_plugin = None


def handle_text_message(event: Event) -> bool:
    return image_editor_plugin is not None and image_editor_plugin.handle_text_message(event)


def handle_quote_image_message(event: Event) -> bool:
    return handle_text_message(event)


def register(event_bus, subscribe, context):
    global image_editor_plugin
    image_editor_plugin = ImageEditorPlugin(context)
    context.health.register(lambda: {
        "status": "healthy" if image_editor_plugin is not None else "unhealthy",
        "message": "生图服务已就绪" if image_editor_plugin is not None else "生图服务未初始化",
    })
    context.register_cleanup(unregister)

    for event_type in (
        EventType.TEXT_MESSAGE_RECEIVED, EventType.QUOTE_TEXT_MESSAGE_RECEIVED,
        EventType.QUOTE_VIDEO_MESSAGE_RECEIVED, EventType.LINK_MESSAGE_RECEIVED,
    ):
        subscribe(event_type=event_type, handler=handle_text_message)
    subscribe(event_type=EventType.QUOTE_IMAGE_MESSAGE_RECEIVED, handler=handle_quote_image_message)


def unregister():
    global image_editor_plugin
    image_editor_plugin = None
