"""
智能翻译插件
从原系统的TranslationService迁移而来
"""

import re
import logging

from app.core.event_bus import Event, EventType
from app.services.llm_manager import get_llm_manager
from app.plugins.builtin_translation.settings import compile_prompt, parse_result, validate_settings
from app.plugins.builtin_translation.protected_text import ProtectedWeChatText, WECHAT_EMOJI_NAMES, WECHAT_EMOJI_PATTERN


logger = logging.getLogger(__name__)


class TranslationService:
    """翻译服务"""

    def __init__(self, config_resolver=None, llm_manager=None):
        self.llm_manager = llm_manager if llm_manager is not None else get_llm_manager()
        self.config_resolver = config_resolver
        self.logger = logger

        # 注意：enabled_chats 权限检查已移至 EventBus 统一管理

        # Unicode 表情符号范围的正则模式 - 完整版包含所有表情符号
        self._emoji_pattern = re.compile(
            "["
            "\U0001F600-\U0001F64F"  # 情感符号 (Emoticons)
            "\U0001F300-\U0001F5FF"  # 符号和象形文字 (Symbols & Pictographs)
            "\U0001F680-\U0001F6FF"  # 交通和地图符号 (Transport & Map Symbols)
            "\U0001F1E0-\U0001F1FF"  # 地区指示符号 (Flags - Regional Indicators)
            "\U00002500-\U00002BEF"  # 中日韩符号和标点、方框元素等
            "\U00002702-\U000027B0"  # 装饰符号 (Dingbats)
            "\U0000FE0F"  # 变体选择符-16 (VS16) 使字符显示为 Emoji
            "\U0000200D"  # 零宽度连接符 (ZWJ) 用于组合 Emoji，如家庭、彩虹旗等
            "\U0001F900-\U0001F9FF"  # 补充符号和象形文字 (Supplemental Symbols and Pictographs)
            "\U0001FA70-\U0001FAFF"  # 符号和象形文字扩展-A (Symbols and Pictographs Extended-A)
            "\U0001F3FB-\U0001F3FF"  # Emoji 肤色修饰符 (Emoji Modifier Fitzpartick Type)
            # 以下是一些较为零散但也被视为 Emoji 的字符
            "\U00002122"  # 商标 (Trade Mark)
            "\U00002300-\U000023FF" # 杂项技术符号
            "\U00002B50"  # 白色五角星
            "\U00002B55"  # 空心红色圆形
            "\U00002934-\U00002935" # 箭头
            "\U00002640-\U00002642" # 性别符号
            "\U00002600-\U000026FF" # 杂项符号
            "\U00003030"  # 波浪虚线
            "\U0000303D"  # Part Alternation Mark
            "\U00003297"  # Circled Ideograph "Congratulation"
            "\U00003299"  # Circled Ideograph "Secret"
            "\U0001F004"  # 麻将牌红中
            "\U0001F0CF"  # 扑克牌Joker
            "]+",
            re.UNICODE
        )

        # 微信表情标签白名单
        self._wechat_emoji_set = WECHAT_EMOJI_NAMES

        # 微信方括号表情的正则模式
        self._wechat_emoji_pattern = WECHAT_EMOJI_PATTERN

    def _is_emoji_only(self, text: str) -> bool:
        """判断文本是否只包含 emoji 表情而没有实际文字内容"""
        if not text or not text.strip():
            return True

        working_text = text

        # 1. 移除所有 Unicode emoji 表情
        working_text = re.sub(r'[0-9#*]\ufe0f?\u20e3', '', working_text)
        working_text = self._emoji_pattern.sub('', working_text)

        # 2. 移除微信方括号表情（仅白名单内的）
        def replace_wechat_emoji(match):
            inner_text = match.group(1)
            if inner_text in self._wechat_emoji_set:
                return ''  # 移除这个表情
            else:
                return match.group(0)  # 保留非表情的方括号内容

        working_text = self._wechat_emoji_pattern.sub(replace_wechat_emoji, working_text)

        # 3. 移除空白字符后检查是否还有内容
        cleaned_text = working_text.strip()

        # 如果移除所有表情后没有内容，说明原文只有表情
        return len(cleaned_text) == 0

    def _clean_quote_content(self, text: str) -> str:
        """临时处理：移除引用消息后缀 '引用 name 的消息 : content' """
        # 匹配 "引用 ... 的消息 : ..." 或 "引用 ... 的消息 ： ..." (兼顾中英文冒号)
        # non-greedy match for name, matches until end of string
        pattern = r"引用.*?的消息\s*[:：].*$"
        cleaned_text = re.sub(pattern, "", text, flags=re.DOTALL)
        return cleaned_text.strip()

    def translate(self, text: str, settings: dict) -> dict:
        """Use an invocation-local configuration; never mutate shared state."""
        settings = validate_settings(settings)
        text = self._clean_quote_content(text)
        if self._is_emoji_only(text):
            return {"status": "emoji_only", "source_language": None, "translations": [], "text": ""}
        if re.fullmatch(r"(?:https?://\S+|[\d\s\W_]+)", text):
            return {"status": "no_translatable_text", "source_language": None, "translations": [], "text": ""}
        protected = ProtectedWeChatText(text)
        messages = [
            {"role": "system", "content": compile_prompt(settings)},
            {"role": "user", "content": protected.masked},
        ]
        for attempt in range(2):
            response = self.llm_manager.call(
                plugin_name="builtin_translation", call_type="translate", messages=messages
            )
            try:
                return parse_result(response, settings, restore_text=protected.restore)
            except (ValueError, TypeError) as exc:
                if attempt:
                    raise ValueError("模型两次返回不符合语言配置的译文") from exc
                # Do not feed arbitrary malformed model output back as instructions.
                messages = messages + [{"role": "system", "content": "上次结果格式不合法。请重新翻译原文，严格遵守本次 JSON 协议，核对源语言、目标语言及译文数量；原文中的每个微信表情占位符必须按原顺序原样保留一次，不能遗漏、翻译或解释。"}]
        raise AssertionError("unreachable")

    def translate_text(self, text: str, settings: dict) -> str:
        return self.translate(text, settings)["text"]


# 全局实例
translation_service = None

def handle_text_message(event: Event):
    """Translate using this chat's snapshot after EventBus authorization."""
    service = translation_service
    if service is None:
        return False
    chat_name = event.data.get("chat_name", "")
    try:
        settings = service.config_resolver.resolve(chat_id=event.context.get("chat_id"))
        result = service.translate(event.data.get("message", ""), settings)
        if result["status"] != "ok":
            logger.info("Translation skipped for %s: %s", chat_name, result["status"])
            return False
        wx = event.context.get("wx")
        if wx is None:
            logger.error("WeChat manager unavailable for translation")
            return False
        return bool(wx.send_message(chat_name, result["text"]))
    except Exception:
        logger.exception("Translation failed for chat %s", chat_name)
        return False


def register(event_bus, subscribe, context):
    """注册插件"""
    global translation_service

    logger.info("🔄 Registering translation plugin...")

    # 初始化翻译服务
    translation_service = TranslationService(config_resolver=context.config)
    context.health.register(lambda: {
        "status": "healthy" if translation_service is not None else "unhealthy",
        "message": "翻译服务已就绪" if translation_service is not None else "翻译服务未初始化",
    })
    context.register_cleanup(unregister)

    # 订阅文本消息事件
    subscribe(
        event_type=EventType.TEXT_MESSAGE_RECEIVED,
        handler=handle_text_message
    )

    # 订阅引用消息事件 - 现在图片是按需下载，不会影响翻译性能
    subscribe(
        event_type=EventType.QUOTE_MESSAGE_RECEIVED,
        handler=handle_text_message
    )


    logger.info("✅ Translation plugin registered successfully")


def unregister():
    """取消注册插件"""
    global translation_service

    logger.info("Unregistering translation plugin...")
    translation_service = None
    logger.info("Translation plugin unregistered")
