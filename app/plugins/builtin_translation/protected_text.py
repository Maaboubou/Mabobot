"""Lossless WeChat emoji transport and conservative output cleanup."""

import re
import secrets


WECHAT_EMOJI_NAMES = frozenset(['微笑', '撇嘴', '色', '发呆', '得意', '流泪', '害羞', '闭嘴', '睡', '大哭', '尴尬', '发怒', '调皮', '呲牙', '惊讶', '难过', '囧', '抓狂', '吐', '偷笑', '愉快', '白眼', '傲慢', '困', '惊恐', '憨笑', '悠闲', '咒骂', '疑问', '嘘', '晕', '衰', '骷髅', '敲打', '再见', '擦汗', '抠鼻', '鼓掌', '坏笑', '右哼哼', '鄙视', '委屈', '快哭了', '阴险', '亲亲', '可怜', '笑脸', '生病', '脸红', '破涕为笑', '恐惧', '失望', '无语', '嘿哈', '捂脸', '奸笑', '机智', '皱眉', '耶', '吃瓜', '加油', '汗', '天啊', 'Emm', '社会社会', '旺柴', '好的', '打脸', '哇', '翻白眼', '666', '让我看看', '叹气', '苦涩', '裂开', '嘴唇', '爱心', '心碎', '拥抱', '强', '弱', '握手', '胜利', '抱拳', '勾引', '拳头', 'OK', '合十', '啤酒', '咖啡', '蛋糕', '玫瑰', '凋谢', '菜刀', '炸弹', '便便', '月亮', '太阳', '庆祝', '礼物', '红包', '發', '福', '烟花', '爆竹', '猪头', '跳跳', '发抖', '转圈', '喜极而泣'])
WECHAT_EMOJI_PATTERN = re.compile(r'\[([^\[\]\n]{1,8})\]')

# Only remove generated trailing whitespace entities, never decode arbitrary
# HTML (which may be literal code, a URL, or the actual subject of translation).
_TRAILING_SPACE_ENTITIES = re.compile(r"(?:\s*(?:&#(?:x0*20|0*32|x0*a0|0*160);|&nbsp;))+\s*$", re.I)


def clean_translation_text(text, source=""):
    if not _TRAILING_SPACE_ENTITIES.search(source):
        text = _TRAILING_SPACE_ENTITIES.sub("", text)
    return text.strip()


class ProtectedWeChatText:
    def __init__(self, source):
        self.source = source
        self.prefix = "__WX_EMOJI_" + secrets.token_hex(8) + "_"
        while self.prefix in source:
            self.prefix = "__WX_EMOJI_" + secrets.token_hex(8) + "_"
        self.tokens = []
        self.originals = []

        def replace(match):
            if match[1] not in WECHAT_EMOJI_NAMES:
                return match[0]
            token = f"{self.prefix}{len(self.tokens)}__"
            self.tokens.append(token)
            self.originals.append(match[0])
            return token

        self.masked = WECHAT_EMOJI_PATTERN.sub(replace, source)

    def restore(self, text):
        observed = re.findall(re.escape(self.prefix) + r"\d+__", text)
        if observed != self.tokens:
            raise ValueError("微信表情占位符缺失、重复或顺序改变")
        # The masked input contains no literal recognized emoji labels. A model
        # inventing any here would otherwise duplicate the restored original.
        if self.tokens and any(match[1] in WECHAT_EMOJI_NAMES for match in WECHAT_EMOJI_PATTERN.finditer(text)):
            raise ValueError("译文额外生成了微信表情")
        for token, original in zip(self.tokens, self.originals):
            text = text.replace(token, original)
        return clean_translation_text(text, self.source)
