"""Admission for image-only business entry points, separate from their delivery."""
from __future__ import annotations

from app.services.execution_contracts import RouteCandidate, RouteSnapshot, digest, canonical


def images_enabled(component):
    from app.services.engine_calls import routing_enabled
    if not routing_enabled():
        return False
    from app.services.execution_service import get_execution_service
    return get_execution_service().config.read()[1]["routes"].get(component + ".images", {}).get("mode", "legacy") != "legacy"


def begin_image_task(component, policy, *, native_profile_id="", timeout=600, source=None):
    if not images_enabled(component):
        return None
    from app.services.execution_authority import make_context
    from app.services.image_generation_facade import freeze_image_policy
    from app.services.execution_service import get_execution_service
    from app.services.codex_profile_service import CodexProfileService
    service = get_execution_service()
    context = make_context(component, "images", timeout=timeout, source=source)
    native_profile_id = native_profile_id or CodexProfileService.default_profile_id()
    frozen = freeze_image_policy(policy, native_profile_id=native_profile_id)
    binding = frozen["native_binding"]
    revision, config = service.config.read()
    if policy["mode"] == "native":
        profile = config["profiles"].get(native_profile_id, {})
        candidate = RouteCandidate("images:" + native_profile_id, "codex", native_profile_id,
            str(profile.get("generation") or digest(binding)), binding.get("model") or "native-image",
            profile.get("account_key") or "codex:" + native_profile_id, profile.get("auth_ref") or "codex:" + native_profile_id)
    else:
        channel = frozen["channels"][0] if frozen["channels"] else {"id": "unavailable", "model": "unavailable"}
        candidate = RouteCandidate("images:" + channel["id"], "api", "", str(policy["version"]), channel["model"],
            "image:" + channel["id"], "image-channel:" + channel["id"])
    route = RouteSnapshot(str(revision), "legacy", (candidate,), max_attempts=1, image_policy_json=canonical(frozen))
    return service.admit(context, route)[0]
