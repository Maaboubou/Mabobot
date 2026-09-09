"""左侧导航栏。"""

from __future__ import annotations

from mabowx.core import uia
from mabowx.core.selectors import runtime_selector
from mabowx.core.locks import uilock

from .base import BaseUISubWnd, BaseUIWnd


class NavigationBox(BaseUISubWnd):
    """主窗口左侧导航栏。"""

    _ui_cls_name = "mmui::MainTabBar"

    def __init__(self, root: BaseUIWnd) -> None:
        self.root = root
        self.parent = None
        self.control = uia.find_descendant(
            root.control,
            **runtime_selector(root, "navigation.tab_bar"),
            timeout=2.0,
        )

    def _buttons(self):
        if self.control is None or not self.control.Exists(0):
            return []
        try:
            return list(self.control.GetChildren())
        except Exception:
            return []

    def _find_button(self, name: str):
        for button in self._buttons():
            try:
                if button.ControlTypeName == "ButtonControl" and button.Name == name:
                    return button
            except Exception:
                continue
        return None

    @uilock
    def switch_to(self, name: str) -> bool:
        """切换到指定导航项，如 微信/通讯录/朋友圈/收藏。"""
        button = self._find_button(name)
        if button is None:
            return False
        button.Click(simulateMove=False, waitTime=0.6)
        return True

    @uilock
    def switch_to_chat_page(self) -> bool:
        return self.switch_to("微信")

    @uilock
    def switch_to_contact_page(self) -> bool:
        return self.switch_to("通讯录")

    @uilock
    def switch_to_favorites_page(self) -> bool:
        return self.switch_to("收藏")

    @uilock
    def switch_to_files_page(self) -> bool:
        return self.switch_to("聊天文件")

    @uilock
    def switch_to_moments_page(self) -> bool:
        return self.switch_to("朋友圈")

    def has_new_message(self) -> bool:
        """判断导航栏是否显示新消息红点/徽标。"""
        for button in self._buttons():
            try:
                for child in uia.iter_descendants(button, max_nodes=30):
                    if child.ClassName == "mmui::XBadge":
                        name = child.Name
                        if name and name.strip():
                            return True
            except Exception:
                continue
        return False
