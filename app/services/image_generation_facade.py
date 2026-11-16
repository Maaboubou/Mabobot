"""Task-owned pure image generation, policy pins, job identity and reconciliation."""
from __future__ import annotations

import copy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import time

from app.services.execution_contracts import ExecutionError, canonical, digest
from app.services.image_generation_service import (
    ImageGenerationError, ImageGenerationCancelled, ImageRequest, ImageResult, generate_images, inspect_image,
)


def image_input_reference(image):
    return {"name": image.name, "sha256": hashlib.sha256(image.data).hexdigest(), "size": len(image.data)}


def freeze_image_policy(policy, *, native_profile_id=""):
    """A snapshot contains channel version references, never channel secrets."""
    binding = {}
    if native_profile_id:
        from app.services.codex_profile_service import get_codex_profile_service
        profile = get_codex_profile_service().get_profile(native_profile_id)
        if profile:
            binding = {key: profile.get(key) for key in ("name", "model", "reasoning_effort", "created_at", "auth_type", "base_url")}
    from app.services.execution_service import get_execution_service
    configured = get_execution_service().config.read()[1]["profiles"].get(native_profile_id, {}) if native_profile_id else {}
    return {"execution_version": 1, "mode": policy["mode"], "version": policy["version"],
        "allow_request_override": policy.get("allow_request_override", False),
        "native_profile_id": native_profile_id, "native_binding": binding, "unknown_policy": "pause",
        "native_runtime_generation": str(configured.get("generation") or digest(binding)),
        "native_auth_ref": configured.get("auth_ref", ""), "native_account_key": configured.get("account_key", ""),
        "channels": [{"id": c["id"], "model": c["model"], "name": c.get("name", c["id"]),
                      "enabled": c.get("enabled", True), "parameters": copy.deepcopy(c.get("parameters", {})),
                      "binding_digest": digest(c)} for c in policy["channels"]]}


def thaw_image_policy(snapshot, *, db=None):
    from app.services.image_generation_config import KEY, read_settings
    from app.models.base import SessionLocal
    from app.models.setting import Setting
    if db is None:
        with SessionLocal() as session:
            return thaw_image_policy(snapshot, db=session)
    settings = read_settings(db)
    if settings["version"] != snapshot["version"]:
        row = db.query(Setting).filter_by(key=KEY + "_VERSION_" + str(snapshot["version"])).first()
        if not row:
            raise ExecutionError("image_policy_unavailable", "Pinned image configuration is unavailable")
        settings = json.loads(row.value)
    channels = {channel["id"]: channel for channel in settings["channels"]}
    resolved = []
    for reference in snapshot["channels"]:
        channel = channels.get(reference["id"])
        if not channel or digest(channel) != reference["binding_digest"]:
            raise ExecutionError("image_account_changed", "Pinned image channel binding changed")
        resolved.append(copy.deepcopy(channel))
    return {**snapshot, "channels": resolved}


class ImageGenerationFacade:
    def __init__(self, store, *, authorize, native_generate=None, policy_resolver=thaw_image_policy):
        self.store, self.authorize = store, authorize
        self.native_generate = native_generate or self._native_generate
        self.policy_resolver = policy_resolver

    def generate(self, context, job_id, policy, request: ImageRequest, output_dir, *, asset_request_id="", parents=(), source_attachment_ids=()):
        if not self.authorize(context):
            raise ExecutionError("permission_denied", "Image job authorization is no longer valid")
        request.check()
        input_summary = {"prompt_hash": hashlib.sha256(request.prompt.encode()).hexdigest(),
            "images": [image_input_reference(image) for image in request.images],
            "mask": image_input_reference(request.mask) if request.mask else None,
            "markup": image_input_reference(request.markup) if request.markup else None,
            "parameters": request.parameters, "count": request.count, "policy": policy}
        operation, created = self.store.claim_operation(context.run_id, "image_job", job_id, input_summary)
        if not created:
            if operation.get("result_json"):
                return {**json.loads(operation["result_json"]), "status": operation["status"]}
            return {"operation_id": operation["operation_id"], "job_id": job_id,
                    "status": operation["status"], "assets": [], "missing": request.count or 1}
        cancelled = request.cancelled
        def stopped():
            return bool(cancelled and cancelled()) or bool(self.store.get(context.run_id)["cancel_requested"]) or not self.authorize(context)
        bounded = replace(request, timeout=min(request.timeout or 3600, max(0.1, context.deadline - time.time())), cancelled=stopped)
        paths, attempts = [], []
        status, error = "completed", ""
        try:
            if stopped() or time.time() >= context.deadline:
                raise ImageGenerationCancelled("图片任务已取消或截止")
            actual = self.policy_resolver(policy)
            if actual["mode"] == "native":
                generated = self.native_generate(context, job_id, policy, bounded, Path(output_dir))
            else:
                generated = generate_images(actual, bounded, Path(output_dir))
            paths, attempts = generated.paths, generated.attempts
        except ImageGenerationError as exc:
            paths, attempts = exc.paths, exc.attempts
            status = "cancelled" if isinstance(exc, ImageGenerationCancelled) else "unknown" if (
                getattr(exc, "outcome_unknown", False) or any(a.get("outcome_unknown") for a in attempts)) else "partial" if paths else "failed"
            error = str(exc)
        except Exception:
            self.store.finish_operation(operation["operation_id"], "unknown", {"error": "Image submission requires reconciliation"})
            raise
        from app.services.execution_authority import chat_name
        from app.services.image_generation_assets import register_images
        assets = register_images(chat_name(context), paths, request_id=asset_request_id or operation["operation_id"],
            source="engine_image_job", parameters=request.parameters, parent_asset_ids=parents,
            source_attachment_ids=source_attachment_ids) if paths else []
        result = {"operation_id": operation["operation_id"], "job_id": job_id, "status": status,
                  "assets": assets, "paths": [str(p) for p in paths], "attempts": attempts,
                  "missing": max(0, (request.count or 1) - len(paths)), "error": error}
        self.store.finish_operation(operation["operation_id"], status, result)
        return result

    @staticmethod
    def _native_generate(context, job_id, policy, request, output_dir):
        from app.services.execution_service import get_execution_service
        from app.services.execution_contracts import RouteCandidate, RouteSnapshot, InputEnvelope, ExecutionResult, run_handle_scope, execution_context
        from app.services.execution_runtime import RunHandle
        from app.services.engine_adapters import CodexAdapter
        import uuid
        service = get_execution_service()
        binding = policy.get("native_binding") or {}
        profile_id = policy.get("native_profile_id")
        _, config = service.config.read()
        configured = config["profiles"].get(profile_id, {})
        if not configured or configured.get("engine") != "codex" or configured.get("revoked") or configured.get("auth_ref") != policy.get("native_auth_ref"):
            raise ExecutionError("image_profile_unavailable", "Register and retain the original Codex image Profile/account binding before using this path")
        candidate = RouteCandidate("image:" + str(profile_id), "codex", str(profile_id or ""),
            policy["native_runtime_generation"], str(binding.get("model") or ""),
            policy["native_account_key"], policy["native_auth_ref"])
        if service.scheduler.is_running(context.run_id):
            child = context.child(component=context.component, task_type="image_generation")
        else:
            child = replace(context, task_id=uuid.uuid4().hex, run_id=uuid.uuid4().hex,
                request_id=context.request_id + ":image:" + job_id, task_type="image_generation", delivery_chat_id="", delivery_owner="")
        route = RouteSnapshot("image-" + str(policy["version"]), "legacy", (candidate,), image_policy_json=canonical(policy), max_attempts=1)
        run_id, _ = service.admit(child, route)
        service.store.bind_input(run_id, InputEnvelope("Generate images", canonical({"job_id": job_id})))
        attempt_id = uuid.uuid5(uuid.NAMESPACE_URL, run_id + ":image").hex
        claimed = service.store.dispatch(run_id, 0, attempt_id)
        if not claimed["new"]:
            raise ExecutionError("reconciliation_required", "Native image attempt was already submitted")
        handle = RunHandle(child, attempt_id, candidate.generation, persist_cancel=service.store.cancel, cancel=CodexAdapter.cancel)
        def run():
            with service._lock:
                service._handles[child.run_id] = handle
            try:
                with execution_context(child), run_handle_scope(handle):
                    return ImageGenerationFacade._call_native(child, job_id, policy, request, output_dir)
            finally:
                handle.complete(None)
                with service._lock:
                    service._handles.pop(child.run_id, None)
        try:
            generated = service.scheduler.submit(child, candidate.account_key, run).result()
        except Exception:
            service.store.finish_attempt(ExecutionResult(child.task_id, child.run_id, attempt_id, "interrupted_unknown",
                engine="codex", profile_id=candidate.profile_id, error_code="image_outcome_unknown"))
            raise
        service.store.finish_attempt(ExecutionResult(child.task_id, child.run_id, attempt_id, "completed",
            engine="codex", profile_id=candidate.profile_id, assets_json=canonical([str(p) for p in generated.paths])))
        return generated

    @staticmethod
    def _call_native(context, job_id, policy, request, output_dir):
        from app.services.codex_profile_service import get_codex_runtime_registry
        profile_id = policy.get("native_profile_id")
        if not profile_id:
            raise ExecutionError("image_profile_missing", "No Codex image Profile was frozen for this job")
        runtime, profile = get_codex_runtime_registry().resolve(profile_id)
        binding = policy.get("native_binding") or {}
        if any(profile.get(key) != binding.get(key) for key in ("created_at", "auth_type", "base_url")):
            raise ExecutionError("image_account_changed", "The pinned image account binding changed")
        from app.services.execution_authority import chat_name
        output_dir.mkdir(parents=True, exist_ok=True)
        reference_dir = output_dir.parent / "image-inputs" / digest(job_id)[:16]
        reference_dir.mkdir(parents=True, exist_ok=True)
        references = []
        for index, image in enumerate(request.images):
            reference = reference_dir / f"reference-{index}.png"
            reference.write_bytes(image.data)
            references.append({"path": str(reference), "name": reference.name})
        for label, image in (("mask", request.mask), ("markup", request.markup)):
            if image is not None:
                reference = reference_dir / (label + ".png")
                reference.write_bytes(image.data)
                references.append({"path": str(reference), "name": reference.name})
        prompt = request.prompt + f"\n请生成 {request.count or 1} 张独立成品。使用全部参考图；mask 是蒙版，markup 是标注。\n图片参数：" + canonical(request.parameters)
        response = runtime.run({"model": binding.get("model") or profile["model"], "messages": [
            {"role": "developer", "content": "仅按请求使用原生 imagegen 生成图片。禁止委派子代理、调用外部图片 API 或发送微信。只返回本次生成的原始图片附件；未知结果不重试。"},
            {"role": "user", "content": prompt}],
            "timeout": max(1, int(request.timeout or 300)), "codex_source_chat_name": chat_name(context),
            "codex_runtime_profile": profile_id, "mabobot_native_image_generation": True,
            "mabobot_image_plugin": "engine_image_job", "mabobot_input_files": references,
            "mabobot_allow_image_input": bool(references), "reasoning_effort": binding.get("reasoning_effort") or "high",
        }, profile_name="child" if context.depth else "batch", allow_exec_fallback=False)
        from app.services.assistant_reply_gateway import CodexReplyGateway
        attachments = CodexReplyGateway._extract_attachments(response)
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for item in attachments:
            source = Path(item["path"])
            if source.is_symlink() or not source.is_file():
                raise ImageGenerationError("原生产物路径无效", paths=paths)
            data = source.read_bytes()
            kind, _ = inspect_image(data)
            target = output_dir / (digest([job_id, len(paths)])[:24] + "." + kind)
            target.write_bytes(data)
            paths.append(target)
        if len(paths) < (request.count or 1):
            raise ImageGenerationError("原生生图返回数量不足", paths=paths)
        return ImageResult(paths, [])
