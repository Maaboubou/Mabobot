"""Explicit tombstones for removed model tasks; unrelated routes stay intact."""


def is_retired_model_route(owner: str, task: str) -> bool:
    return (
        owner == "hermes_mode"
        or (owner in {"assistant", "builtin_chatbot"} and task.startswith("memory_"))
        or (owner == "summary_plus" and task == "detail")
    )


def prune_retired_model_routes(mappings: dict) -> bool:
    changed = False
    for owner, tasks in list(mappings.items()):
        if owner == "hermes_mode":
            del mappings[owner]
            changed = True
        elif isinstance(tasks, dict):
            for task in list(tasks):
                if is_retired_model_route(owner, task):
                    del tasks[task]
                    changed = True
    return changed
