"""mabowx - 纯 Python 开源的 Windows 微信 4.x UI 自动化库。"""

from importlib import import_module
from typing import Any

__version__ = "0.1.0"

__all__ = [
    "WeChat",
    "Chat",
    "LoginWnd",
    "WxParam",
    "WxResponse",
    "MediaIdentityError",
    "MediaFileMismatchError",
    "media_download_trace",
    "__version__",
]

_LAZY_ATTRIBUTES = {
    "WeChat": (".api.wechat", "WeChat"),
    "Chat": (".api.chat", "Chat"),
    "LoginWnd": (".api.wechat", "LoginWnd"),
    "MediaIdentityError": (".msgs.identity", "MediaIdentityError"),
    "MediaFileMismatchError": (".msgs.identity", "MediaFileMismatchError"),
    "media_download_trace": (".msgs.media_diagnostics", "media_download_trace"),
}

_LAZY_SUBMODULES = {
    "api",
    "core",
    "exceptions",
    "logger",
    "msgs",
    "param",
    "ui",
}


def __getattr__(name: str) -> Any:
    target = _LAZY_ATTRIBUTES.get(name)
    if target is not None:
        module_name, attribute_name = target
        value = getattr(import_module(module_name, __name__), attribute_name)
        globals()[name] = value
        return value

    if name in _LAZY_SUBMODULES:
        value = import_module(f".{name}", __name__)
        globals()[name] = value
        return value

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_ATTRIBUTES) | _LAZY_SUBMODULES)
