"""Chat 公开对象。"""

from __future__ import annotations

from mabowx.core.locks import uilock
from mabowx.ui.main import WeChatSubWnd


class Chat:
    """一个聊天窗口的公开操作对象。

    M2 只实现窗口级能力；消息发送与读取将在 M3/M4 开放。
    """

    def __init__(self, core: WeChatSubWnd | None = None) -> None:
        # Do not use truthiness here: ``WeChatSubWnd.__bool__`` performs a live
        # UIA/Win32 probe. A freshly detached window can need a short moment to
        # become queryable; replacing that valid wrapper with an empty one makes
        # the caller lose the HWND permanently and later report an open timeout.
        self.core = core if core is not None else WeChatSubWnd(None)
        self.who = self.core.who
        if self.core.exists():
            try:
                self.core.get_chatbox().prime_message_cache()
            except Exception:
                pass

    def __repr__(self) -> str:
        return f"<mabowx Chat({self.who!r}) exists={self.core.exists()}>"

    def __bool__(self) -> bool:
        return self.core.exists()

    @uilock
    def Show(self) -> None:
        self.core.show()

    @property
    def ChatBox(self):
        """Mabobot 兼容：返回本窗口的 ChatBox 操作对象。"""
        return self.core.get_chatbox()

    @property
    def _api(self):
        """Mabobot 兼容：部分版本通过 ``chat._api.ChatBox`` 访问。"""
        return self

    @uilock
    def ChatInfo(self) -> dict[str, object]:
        return self.core.chat_info()

    @uilock
    def Close(self) -> None:
        self.core.close()

    def _not_ready(self, feature: str) -> None:
        raise NotImplementedError(f"{feature} 将在后续里程碑实现")

    @uilock
    def SendMsg(
        self,
        msg: str,
        who: str | None = None,
        clear: bool = True,
        at: str | list[str] | None = None,
        exact: bool = False,
    ):
        """向当前独立聊天窗口发送文本消息；who 参数在子窗口模式下无效。"""
        if not self.core.exists():
            from mabowx.param import WxResponse

            return WxResponse.failure("聊天窗口不存在")
        return self.core.get_chatbox().send_text(msg=msg, clear=clear, at=at)

    @uilock
    def GetMyGroupNickname(self) -> str:
        """只读返回当前账号在本群的昵称。"""
        if not self.core.exists():
            raise RuntimeError("聊天窗口不存在")
        info = self.ChatInfo()
        expected_name = str(self.who or "").strip()
        actual_name = str(info.get("chat_name") or "").strip()
        actual_type = str(info.get("chat_type") or "").lower()
        if not expected_name or actual_name != expected_name or actual_type != "group":
            raise RuntimeError(
                "群聊窗口身份校验失败："
                f"期望={expected_name!r}，实际={actual_name!r}，类型={actual_type!r}"
            )
        return self.core.get_chatbox().get_group_my_nickname()

    @uilock
    def SendFiles(self, filepath, who: str | None = None, exact: bool = False):
        """向当前独立聊天窗口发送一个或多个文件。"""
        from mabowx.param import WxResponse

        if isinstance(filepath, (list, tuple)):
            filepaths = list(filepath)
        else:
            filepaths = [filepath]
        if not filepaths:
            return WxResponse.failure("文件列表不能为空")
        if not self.core.exists():
            return WxResponse.failure("聊天窗口不存在")
        return self.core.get_chatbox().send_files(filepaths)

    @uilock
    def GetAllMessage(self):
        if not self.core.exists():
            return []
        return self.core.get_chatbox().get_messages()

    def GetNewMessage(self):
        if not self.core.exists():
            return []
        return self.core.get_chatbox().get_new_messages()

    def GetHistoryMessage(
        self,
        n: int,
        callback=None,
        interval: float = 0.2,
        speed: int = 1,
        goback: bool = True,
        timeout: float | None = None,
    ) -> list:
        if not self.core.exists():
            return []
        return self.core.get_chatbox().get_history_messages(
            n=n,
            callback=callback,
            interval=interval,
            speed=speed,
            goback=goback,
            timeout=timeout,
        )
