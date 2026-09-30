"""Summary-specific browser behavior; lifecycle lives in the shared service."""

from urllib.parse import urlparse

from app.services.shared_chrome_runtime import SharedChromeRuntimeMixin


class BrowserRuntimeMixin(SharedChromeRuntimeMixin):
    """Summary page access and filtering."""

    def _open_background_page(self, url):
        from .background_page import BackgroundPage
        # BackgroundPage connects through the common runtime and owns one tab.
        return BackgroundPage(
            self.chrome_debug_port, url,
            command_timeout=max(3, int(self.webdriver_command_timeout_sec or 8)),
        )

    def _is_blacklisted(self, url: str) -> bool:
        """检查 URL 是否在域名黑名单中。
        支持精确域名（如 example.com）和通配符后缀（如 *.xyz 或直接填 .xyz）。
        """
        if not url or not self.domain_blacklist:
            return False
        try:
            netloc = urlparse(url).netloc.lower()
            # 去掉端口号
            netloc = netloc.split(":")[0]
            if not netloc:
                return False

            for entry in self.domain_blacklist:
                entry = entry.lstrip("*")  # 兼容 *.xyz 写法，统一为 .xyz
                if entry.startswith("."):
                    # 后缀匹配：命中 foo.xyz / bar.baz.xyz 等，但不命中 xyz 本身
                    if netloc.endswith(entry):
                        return True
                else:
                    # 精确匹配或子域名匹配
                    if netloc == entry or netloc.endswith("." + entry):
                        return True
        except Exception as e:
            self.logger.warning(f"⚠️ 检查黑名单时解析 URL 出错: {e}")
        return False

    def _is_sender_blacklisted(self, sender: str) -> bool:
        """检查消息发送者是否在 sender 黑名单中（大小写不敏感）"""
        if not sender or not self.sender_blacklist:
            return False
        return sender.lower().strip() in self.sender_blacklist
