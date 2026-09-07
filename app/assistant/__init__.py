"""First-class Codex Assistant domain.

Runtime exports are lazy so archive import utilities
can be imported by the generic model manager without creating an import cycle.
"""

__all__ = ["AssistantRuntime", "get_assistant_handler", "get_assistant_runtime"]


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(name)
    from app.assistant import runtime

    return getattr(runtime, name)
