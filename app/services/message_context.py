"""Distinguish framework facts from the current human request."""
FRAMEWORK_CONTEXT_NAMES = frozenset({
    "search_context", "history_context", "shared_work_context",
    "background_task_context", "background_task_result",
})


def is_framework_context(message):
    return message.get("name") in FRAMEWORK_CONTEXT_NAMES


def latest_user_message(messages):
    return next((message for message in reversed(list(messages))
                 if str(message.get("role") or "user") == "user"
                 and not is_framework_context(message)), {})


def insert_before_latest_user(messages, context):
    result = list(messages)
    for index in range(len(result) - 1, -1, -1):
        message = result[index]
        if str(message.get("role") or "user") == "user" and not is_framework_context(message):
            result.insert(index, context)
            return result
    result.append(context)
    return result
