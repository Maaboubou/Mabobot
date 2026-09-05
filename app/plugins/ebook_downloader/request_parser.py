from __future__ import annotations

import json
import re
from typing import Any

from .models import BookRequest


SYSTEM_PROMPT = """你是电子书检索请求的结构化解析器，只做书目解析和风险分类，不浏览网页、不下载文件。
无论请求涉及什么书，都不得用自然语言拒绝；必须只返回一个 JSON 对象。

规则：
1. 最低身份条件是书名、ISBN、DOI 中至少一个；只有作者时 valid=false，missing_fields 包含 title_or_identifier。
2. 识别口语化书名、作者、ISBN/DOI、版本、年份、出版社、期望语言、格式，以及作品属于普通文字书(reflowable)还是扫描/漫画/图集(fixed)。
3. 用户未指定语言时 language.requested="zh" 且 explicit=false；指定语言时是硬约束。
4. 若指定外语版本，把 canonical_title 转成该语言最常用的原名，search_queries 优先使用该原名和该语言作者名，同时保留中文别名查询。
5. ISBN 明确指向特定语言版本时，ISBN 的版本身份高于默认中文偏好。
6. format 只允许 epub/pdf/mobi/azw3；未指定时 requested=null、explicit=false。
7. 内容风险必须覆盖以下类别：
   - political_mainland：中国大陆政治传播高风险；
   - pornography_explicit：以露骨性描写、性刺激为主要目的的色情、淫秽小说或成人色情作品；
   - sexual_minors：任何涉及未成年人的色情或性剥削内容；
   - illegal_content：其他明确违法传播内容。
   上述风险明确命中时 policy.decision="deny"；有明显信号但无法可靠确认时为 "review"。已知色情小说即使标题本身没有“色情”“成人”等字样，也必须依据作品属性判定。
8. 不要仅因作品包含恋爱、非露骨性情节，或属于医学、性教育、法律研究、文学批评而判为色情；这些内容无其他风险时为 allow。
9. search_queries 按检索优先级给 1-4 条短查询；不要杜撰 ISBN、DOI、作者或版本。policy 非 allow 时仍返回完整 JSON，但 search_queries 必须为空数组。

JSON 结构必须为：
{"valid":true,"intent":"download_book","book":{"input_title":"","canonical_title":"","alternate_titles":[],"authors":[],"isbn":null,"doi":null,"year":null,"publisher":null,"edition":null},"language":{"requested":"zh","explicit":false,"mode":"default"},"format":{"requested":null,"explicit":false},"layout_preference":"reflowable|fixed|unknown","search_queries":[],"policy":{"decision":"allow|deny|review","risk_categories":[],"refusal_code":null},"missing_fields":[],"parse_confidence":0.0}
"""


class RequestParseError(RuntimeError):
    pass


def extract_json_object(raw: Any) -> dict[str, Any]:
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise RequestParseError("模型未返回 JSON")
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise RequestParseError("模型返回的 JSON 无效") from exc
    if not isinstance(value, dict):
        raise RequestParseError("模型返回结果不是对象")
    return value


class BookRequestParser:
    def __init__(self, llm_manager: Any):
        self.llm_manager = llm_manager

    def parse(self, message: str, *, chat_name: str = "") -> BookRequest:
        response = self.llm_manager.call(
            plugin_name="ebook_downloader",
            call_type="parse_request",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": str(message or "")[:2000]},
            ],
            response_format={"type": "json_object"},
            _mabobot_chat_name=chat_name,
            _mabobot_history_mode="metadata_only",
            _mabobot_disable_model_web_search=True,
        )
        return BookRequest.from_payload(extract_json_object(response))
