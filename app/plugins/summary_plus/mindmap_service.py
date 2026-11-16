"""Mindmap generation/render helpers for summary_plus."""

import json
import logging
import os
import re
import traceback
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import async_playwright

__all__ = [
    "MINDMAP_SYSTEM_PROMPT_DEFAULT",
    "summarize_to_mindmap_json",
    "is_mindmap_skip_response",
    "get_mindmap_skip_reason",
    "resolve_mindmap_layout",
    "mindmap_json_to_markdown",
    "render_mindmap_to_image",
]

MINDMAP_SYSTEM_PROMPT_DEFAULT = """
Role

你是一位顶级知识架构师，擅长将长文提炼为适合手机竖屏阅读的高密度思维导图 JSON。

Input

用户将提供一段长文，你需要基于其内容生成符合以下规则的思维导图 JSON。

Core Principles

    严格忠实原文：所有节点内容必须能在输入长文中找到直接依据，不得添加原文未提及的信息。如果原文某部分信息缺失，则跳过对应分支，绝不编造。

    逻辑优先于形式：不必强行套用固定模板。请根据原文的实际论述顺序和内在脉络组织层次，确保导图能准确反映原文逻辑。

    竖屏阅读优先：输出结果将用于手机竖屏阅读，要求节点便于快速扫读，避免横向依赖、过长标题和同层过度铺开。

    聚合优先：当信息点很多时，优先先聚合为少量中间层主题，再在下层展开细节，不要在同一层平铺过多节点。

    视觉导航清晰：非叶子节点应像“分组标题”或“导航标签”，叶子节点应像“可快速扫读的信息点”。

Output Rules
1. 节点内容要求

    根节点：用一句话概括全文主题，清晰、具体、可直接作为整张图的大标题。

    一级分支：根节点的直接子节点优先控制在 3~5 个，最多不超过 6 个。

    非叶子节点（有子节点）：
        用不超过10个字的标题精准概括该分支的核心。
        标题必须像“分组标签”或“导航锚点”，而不是完整判断句。
        尽量避免使用“背景”“影响”“意义”“问题”“策略”等过于空泛的裸标题，除非前面有明确限定词。

    叶子节点（最终末梢）：
        必须采用“核心点：关键解释”格式。
        冒号前必须是可独立扫读的短关键词，不要写成冗长短句。
        冒号后是简洁解释，说明事实、影响或关系。
        叶子节点整体优先控制在 12~24 个汉字内；若超长，优先压缩关键词和解释，保持可读性。

2. 层次与深度

    整体结构必须控制在 3~5 层以内，根节点为第1层，绝对不能超过5层。

    当原文层次复杂时，优先合并邻近信息或提升抽象层级，避免树过深或同层节点过多。

3. 表达一致性

    同一层级的并列节点应尽量保持表达方式一致，避免有的写对象、有的写结论、有的写时间，导致阅读节奏混乱。

4. 信息完整性

    所有节点必须包含实质性内容，严禁出现仅有两三个字的空洞节点。

    即使是非叶子节点，其标题也必须体现具体内容，而不是空泛概念。

Output Format

    仅返回一个纯 JSON 字符串，严禁包含 Markdown 代码块或任何额外文字。

    JSON 结构：
        根节点字段名为 title。
        子节点数组字段名为 children。
        若某节点无子节点，则省略 children 字段，该节点即为叶子节点。

    每个节点必须包含 title 字段。

    children 数组中的每一项都必须是 JSON 对象，禁止直接放字符串。
    正确：{"children": [{"title": "身材：史上最轻201g"}]}
    错误：{"children": ["身材：史上最轻201g"]}

Example Style

    合格叶子节点示例：
        “海上目标：三艘游轮遭袭”
        “金融撤离：汇丰花旗收缩中东”

    不合格叶子节点示例：
        “伊朗袭击的海上目标包括：三艘游轮遭袭”
        “相关情况说明：多家机构采取撤离措施”

Processing Steps

    通读原文，先识别总主题和一级主干。

    将分散信息优先聚合为少量中间层主题，再逐层展开细节。

    对每个叶子节点，先提炼可高亮扫读的关键词，再补充简短解释。

    复核每个节点是否都能在原文中找到依据，并检查总层级是否超过5层。

Constraint Summary

    不得添加原文不存在的信息。

    不得输出 Markdown 代码块或额外文本。

    叶子节点必须包含冒号。

    非叶子节点标题尽量≤10字。

    根节点到叶子节点总层级不得超过5层。

    一级分支优先控制在3~5个，最多不超过6个。

请根据上述规则，为以下长文生成思维导图JSON：
""".strip()


def _strip_json_fence(content: str) -> str:
    content = content.strip()
    if content.startswith("```json"):
        return content[7:-3].strip() if content.endswith("```") else content[7:].strip()
    if content.startswith("```"):
        return content[3:-3].strip() if content.endswith("```") else content[3:].strip()
    return content


def _unwrap_mindmap_payload(payload: Any):
    if not isinstance(payload, dict):
        return payload

    should_generate = payload.get("should_generate")
    if isinstance(should_generate, bool):
        if not should_generate:
            reason = str(payload.get("skip_reason") or "内容信息密度不足")
            return {"_mindmap_skip": True, "_mindmap_skip_reason": reason}
        nested = payload.get("mindmap")
        if isinstance(nested, dict):
            return nested

    nested = payload.get("mindmap")
    if isinstance(nested, dict):
        return nested

    return payload


def _normalize_mindmap_node(node: Any, path: str, stats: dict[str, int]):
    """Normalize model schema drift while preserving every titled node."""
    if isinstance(node, str):
        title = node.strip()
        if not title:
            return None, f"{path} is an empty string"
        stats["string_nodes"] = stats.get("string_nodes", 0) + 1
        return {"title": title}, ""

    if not isinstance(node, dict):
        return None, f"{path} is {type(node).__name__}, expected object or string"

    title = str(node.get("title") or node.get("topic") or "").strip()
    if not title:
        return None, f"{path}.title is empty"

    has_children = "children" in node or "topics" in node
    children = node.get("children") if "children" in node else node.get("topics")
    if has_children and not isinstance(children, list):
        return None, f"{path}.children is {type(children).__name__}, expected array"

    normalized: dict[str, Any] = {"title": title}
    if isinstance(children, list) and children:
        normalized_children = []
        for index, child in enumerate(children):
            normalized_child, reason = _normalize_mindmap_node(
                child,
                f"{path}.children[{index}]",
                stats,
            )
            if normalized_child is None:
                return None, reason
            normalized_children.append(normalized_child)
        normalized["children"] = normalized_children

    return normalized, ""


def _normalize_mindmap_tree(data: Any):
    if isinstance(data, dict) and data.get("_mindmap_skip"):
        return data, {"string_nodes": 0}, ""
    stats = {"string_nodes": 0}
    normalized, reason = _normalize_mindmap_node(data, "root", stats)
    return normalized, stats, reason


def _validate_mindmap_payload(data: Any) -> tuple[bool, str]:
    if isinstance(data, dict) and data.get("_mindmap_skip"):
        return True, ""
    if not isinstance(data, dict):
        return False, f"root is {type(data).__name__}, expected object"

    title = str(data.get("title") or data.get("topic") or "").strip()
    if not title:
        return False, "root title is empty"

    children = data.get("children")
    if not isinstance(children, list):
        return False, "root children is not an array"
    if not children:
        return False, "root children is empty"

    def validate_node(node: Any, path: str):
        if not isinstance(node, dict):
            return False, f"{path} is {type(node).__name__}, expected object"
        node_title = str(node.get("title") or "").strip()
        if not node_title:
            return False, f"{path}.title is empty"
        if "children" not in node:
            return True, ""
        node_children = node.get("children")
        if not isinstance(node_children, list):
            return False, f"{path}.children is not an array"
        if not node_children:
            return False, f"{path}.children is empty; omit children for a leaf node"
        for index, child in enumerate(node_children):
            valid, reason = validate_node(child, f"{path}.children[{index}]")
            if not valid:
                return valid, reason
        return True, ""

    for index, child in enumerate(children):
        valid, reason = validate_node(child, f"root.children[{index}]")
        if not valid:
            return valid, reason

    return True, ""


def _normalize_and_validate_mindmap_payload(payload: Any, logger: Any, source: str):
    data = _unwrap_mindmap_payload(payload)
    if isinstance(data, dict) and data.get("_mindmap_skip"):
        return data

    data, stats, normalize_reason = _normalize_mindmap_tree(data)
    if data is None:
        if logger is not None:
            logger.warning(
                "⚠️ Mindmap JSON normalization failed after %s: %s; payload preview=%r",
                source,
                normalize_reason,
                repr(payload)[:500],
            )
        return None

    if stats["string_nodes"] and logger is not None:
        logger.warning(
            "⚠️ Mindmap JSON normalized %d string child nodes after %s",
            stats["string_nodes"],
            source,
        )

    valid, reason = _validate_mindmap_payload(data)
    if valid:
        return data

    if logger is not None:
        logger.warning(
            "⚠️ Mindmap JSON structure invalid after %s: %s; payload preview=%r",
            source,
            reason,
            repr(payload)[:500],
        )
    return None


def _parse_mindmap_json_content(content: str, logger: Any):
    try:
        payload = json.loads(content)
        return _normalize_and_validate_mindmap_payload(payload, logger, "json.loads")
    except json.JSONDecodeError as first_err:
        raw_decode_err: Optional[Exception] = None
        try:
            payload, end = json.JSONDecoder().raw_decode(content)
            trailing = content[end:].strip()
            if trailing and logger is not None:
                logger.warning(
                    "⚠️ Mindmap JSON contained trailing garbage after first object: %r",
                    trailing[:300],
                )
            parsed = _normalize_and_validate_mindmap_payload(payload, logger, "raw_decode")
            if parsed is not None:
                return parsed
        except json.JSONDecodeError as err:
            raw_decode_err = err

        try:
            import json_repair

            repaired = json_repair.repair_json(content, return_objects=True)
            if repaired:
                parsed = _normalize_and_validate_mindmap_payload(repaired, logger, "json_repair")
                if parsed is not None:
                    return parsed
        except Exception:
            pass

        fixed_content = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", content)
        try:
            payload = json.loads(fixed_content)
            return _normalize_and_validate_mindmap_payload(payload, logger, "control-char-cleanup")
        except json.JSONDecodeError as err:
            if logger is not None:
                logger.error(
                    "Failed to parse mindmap JSON. content preview: %r, json_error=%r, raw_decode_error=%r, final_error=%r",
                    content[:500],
                    first_err,
                    raw_decode_err,
                    err,
                )
            raise


MINDMAP_OUTPUT_CONTRACT = {
    "skip_field": "should_generate",
    "schema": {
        "$defs": {"node": {"type": "object", "properties": {
            "title": {"type": "string", "minLength": 1},
            "children": {"type": "array", "items": {"$ref": "#/$defs/node"}}},
            "required": ["title"], "additionalProperties": False}},
        "oneOf": [
            {"type": "object", "properties": {"should_generate": {"const": True},
                "mindmap": {"$ref": "#/$defs/node"}}, "required": ["should_generate", "mindmap"], "additionalProperties": False},
            {"type": "object", "properties": {"should_generate": {"const": False},
                "skip_reason": {"type": "string", "minLength": 1}}, "required": ["should_generate", "skip_reason"], "additionalProperties": False},
        ],
    },
}


def summarize_to_mindmap_json(
    llm_manager: Any,
    text: str,
    system_prompt: str,
    call_type: str,
    plugin_name: str = "summary_plus",
):
    rules = (
        "请将以下内容整理为具备深度的脑图 JSON。\n"
        "你必须只返回一个 JSON，并严格使用以下二选一结构之一：\n"
        "1) 允许生成脑图时：\n"
        '{"should_generate": true, "mindmap": {"title": "...", "children": [...]}}\n'
        "2) 不应生成脑图时（如纯歌词、纯语气词、无信息密度文本）：\n"
        '{"should_generate": false, "skip_reason": "不超过30字的原因"}\n'
        "children 数组中的每一项都必须是含 title 的 JSON 对象，禁止直接使用字符串。\n"
        "禁止输出 Markdown 代码块、解释文字或多余字段。\n\n"
    )
    prompt = rules + f"原文内容如下：\n{text}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]
    from app.services.engine_calls import begin_model_task
    run_id = begin_model_task(llm_manager, plugin_name, call_type, output_contract=MINDMAP_OUTPUT_CONTRACT)
    strict = {"_mabobot_run_id": run_id, "_mabobot_instructions": system_prompt + "\n\n" + rules,
              "_mabobot_input": text} if run_id else {}
    response_str = llm_manager.call(
        plugin_name=plugin_name,
        call_type=call_type,
        messages=messages,
        **strict,
    )
    if not response_str:
        return None

    logger = logging.getLogger(__name__)
    content = _strip_json_fence(response_str)
    return _parse_mindmap_json_content(content, logger)


def is_mindmap_skip_response(data: Any) -> bool:
    return isinstance(data, dict) and bool(data.get("_mindmap_skip"))


def get_mindmap_skip_reason(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    return str(data.get("_mindmap_skip_reason") or "").strip()


def resolve_mindmap_layout(layout: str) -> str:
    normalized = (layout or "vertical").strip().lower()
    if normalized in {"horizontal", "landscape", "h", "横向"}:
        return "horizontal"
    return "vertical"


def get_node_children(node):
    if not isinstance(node, dict):
        return []
    children = node.get("children")
    if isinstance(children, list):
        return children
    topics = node.get("topics")
    if isinstance(topics, list):
        return topics
    return []


def mindmap_json_to_markdown(data):
    def to_md(item, level=1):
        if not isinstance(item, dict):
            return ""
        topic = str(item.get("topic", item.get("title", ""))).replace("\n", " ").replace("\r", "")
        if not topic:
            return ""
        md = f"{'  ' * (level - 1)}- {topic}\n"
        for child in get_node_children(item):
            md += to_md(child, level + 1)
        return md

    return to_md(data)


def create_markmap_html(
    md_content: str,
    output_html: str,
    js_d3: str = "",
    js_markmap_lib: str = "",
    js_markmap_view: str = "",
):
    template = f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <style>
            html, body {{ width: 100%; height: 100%; margin: 0; padding: 0; overflow: hidden; background: white; }}
            #mindmap {{ width: 100vw; height: 100vh; display: block; }}

            .markmap-node div {{
                padding: 0 4px;
                line-height: 1.2;
                font-family: "AlibabaPuHuiTi-3-65-Medium", "PingFang SC", "Microsoft YaHei", sans-serif;
                font-size: 16px;
                color: #333;
                white-space: nowrap;
                overflow: visible;
            }}

            .markmap-node line {{
                display: block;
                stroke-width: 2px;
            }}

            svg {{ overflow: visible; }}
            foreignObject {{ overflow: visible; }}

            .markmap-node[data-depth="0"] div {{
                font-weight: bold;
                font-size: 18px;
                padding: 2px 8px;
                background: none;
                border: none;
                box-shadow: none;
            }}

            .markmap-link {{
                stroke-width: 2px;
                opacity: 0.8;
            }}
        </style>
        {f'<script>{js_d3}</script>' if js_d3 else '<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>'}
        {f'<script>{js_markmap_lib}</script>' if js_markmap_lib else '<script src="https://cdn.jsdelivr.net/npm/markmap-lib@0.17.0"></script>'}
        {f'<script>{js_markmap_view}</script>' if js_markmap_view else '<script src="https://cdn.jsdelivr.net/npm/markmap-view@0.17.0"></script>'}
    </head>
    <body>
        <svg id="mindmap"></svg>
        <script>
            (function() {{
                const {{ Markmap, Transformer }} = window.markmap;
                const transformer = new Transformer();
                const md_content = {json.dumps(md_content, ensure_ascii=False)};
                const {{ root }} = transformer.transform(md_content);

                const mm = Markmap.create('#mindmap', {{
                    autoFit: true, duration: 0, paddingX: 20,
                    spacingHorizontal: 80, spacingVertical: 10, maxWidth: 0
                }}, root);

                setTimeout(() => mm.fit(), 200);
                window.addEventListener('resize', () => mm.fit());
            }})();
        </script>
    </body>
    </html>
    """
    with open(output_html, "w", encoding="utf-8") as f:
        f.write(template)


def create_mobile_mindmap_html(json_data, output_html):
    template = f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Vertical Mindmap</title>
        <style>
            :root {{
                --bg-top: #f5f1e8;
                --bg-bottom: #ece7dd;
                --surface: rgba(255, 255, 255, 0.96);
                --surface-soft: rgba(255, 255, 255, 0.78);
                --leaf-surface: rgba(255, 255, 255, 0.45);
                --text: #1f2937;
                --muted: #5f6b76;
                --root-start: #123952;
                --root-end: #4d788d;
                --branch-1: #b65f45;
                --branch-2: #64846c;
                --branch-3: #5f7798;
                --branch-4: #8b6782;
                --branch-5: #be9441;
            }}

            * {{
                box-sizing: border-box;
            }}

            html, body {{
                margin: 0;
                padding: 0;
                color: var(--text);
                font-family: "AlibabaPuHuiTi-3-65-Medium", "PingFang SC", "Microsoft YaHei", sans-serif;
                background:
                    radial-gradient(circle at top, rgba(255, 255, 255, 0.9), transparent 36%),
                    linear-gradient(180deg, var(--bg-top) 0%, var(--bg-bottom) 100%);
            }}

            body {{
                min-height: 100vh;
                padding: 24px 14px 48px;
            }}

            #mindmap-mobile {{
                width: min(100%, 460px);
                margin: 0 auto;
                padding-top: 56px;
                padding-bottom: 120px;
            }}

            .root-card {{
                padding: 18px 18px 16px;
                border-radius: 22px;
                background: linear-gradient(135deg, var(--root-start), var(--root-end));
                color: #fff;
                box-shadow: 0 16px 36px rgba(18, 57, 82, 0.18);
            }}

            .root-label {{
                font-size: 12px;
                letter-spacing: 0.12em;
                text-transform: uppercase;
                opacity: 0.76;
            }}

            .root-title {{
                margin-top: 8px;
                font-size: 24px;
                line-height: 1.35;
                font-weight: 700;
            }}

            .branches {{
                position: relative;
                margin-top: 22px;
                padding-left: 18px;
            }}

            .branches::before {{
                content: "";
                position: absolute;
                left: 7px;
                top: 8px;
                bottom: 8px;
                width: 2px;
                background: linear-gradient(180deg, rgba(18, 57, 82, 0.24), rgba(18, 57, 82, 0.05));
            }}

            .branch {{
                --accent: var(--branch-1);
                position: relative;
                margin-bottom: 28px;
                padding-left: 16px;
            }}

            .branch::before {{
                content: "";
                position: absolute;
                left: -11px;
                top: 28px;
                width: 16px;
                height: 3px;
                background: var(--accent);
                opacity: 0.78;
            }}

            .branch-title {{
                width: 100%;
                padding: 14px 16px;
                border-radius: 18px;
                background: color-mix(in srgb, var(--accent) 14%, white);
                border-left: 6px solid var(--accent);
                box-shadow: 0 10px 20px rgba(0, 0, 0, 0.04);
                font-size: 19px;
                font-weight: 700;
                line-height: 1.35;
                color: #1b2a35;
            }}

            .branch + .branch {{
                margin-top: 20px;
            }}

            .children {{
                position: relative;
                margin-top: 12px;
                margin-left: 10px;
                padding-left: 16px;
            }}

            .children::before {{
                content: "";
                position: absolute;
                left: 6px;
                top: 4px;
                bottom: 8px;
                width: 2px;
                background: linear-gradient(180deg, color-mix(in srgb, var(--accent) 35%, white), rgba(255, 255, 255, 0));
            }}

            .node {{
                --accent: var(--branch-2);
                position: relative;
                margin: 0 0 10px;
                padding-left: 14px;
            }}

            .node::before {{
                content: "";
                position: absolute;
                left: -6px;
                top: 18px;
                width: 12px;
                height: 2px;
                background: color-mix(in srgb, var(--accent) 72%, white);
            }}

            .node-card.group {{
                position: relative;
                padding: 4px 0 4px 12px;
                font-size: 15px;
                font-weight: 700;
                line-height: 1.45;
                color: #22303a;
                word-break: break-word;
            }}

            .node-card.group::before {{
                content: "";
                position: absolute;
                left: 0;
                top: 4px;
                bottom: 4px;
                width: 4px;
                border-radius: 999px;
                background: color-mix(in srgb, var(--accent) 88%, white);
            }}

            .node-card.leaf {{
                background: transparent;
                padding: 6px 0;
                font-size: 14px;
                line-height: 1.6;
                color: var(--muted);
            }}

            .leaf-keyword {{
                color: color-mix(in srgb, var(--accent) 92%, #1f2937);
                font-weight: 700;
            }}

            .leaf-sep {{
                color: color-mix(in srgb, var(--accent) 82%, #1f2937);
                font-weight: 700;
            }}

            .leaf-desc {{
                color: var(--text);
                font-weight: 500;
            }}

            @media (min-width: 768px) {{
                body {{
                    padding: 28px 0 54px;
                }}

                #mindmap-mobile {{
                    width: min(92vw, 560px);
                    padding-top: 40px;
                    padding-bottom: 96px;
                }}
            }}
        </style>
    </head>
    <body>
        <main id="mindmap-mobile"></main>
        <script>
            (() => {{
                const data = {json.dumps(json_data, ensure_ascii=False)};
                const palette = ["var(--branch-1)", "var(--branch-2)", "var(--branch-3)", "var(--branch-4)", "var(--branch-5)"];
                const container = document.getElementById("mindmap-mobile");

                function getChildren(node) {{
                    if (Array.isArray(node.children)) return node.children;
                    if (Array.isArray(node.topics)) return node.topics;
                    return [];
                }}

                function createLeafContent(title, accent) {{
                    const leaf = document.createElement("div");
                    leaf.className = "node-card leaf";
                    leaf.style.setProperty("--accent", accent);

                    const parts = String(title || "").split("：");
                    if (parts.length >= 2) {{
                        const keyword = document.createElement("span");
                        keyword.className = "leaf-keyword";
                        keyword.textContent = parts.shift();
                        leaf.appendChild(keyword);

                        const sep = document.createElement("span");
                        sep.className = "leaf-sep";
                        sep.textContent = "：";
                        leaf.appendChild(sep);

                        const desc = document.createElement("span");
                        desc.className = "leaf-desc";
                        desc.textContent = parts.join("：");
                        leaf.appendChild(desc);
                    }} else {{
                        leaf.textContent = title || "";
                    }}
                    return leaf;
                }}

                function buildNode(node, depth, accent) {{
                    const wrapper = document.createElement("section");
                    wrapper.className = `node depth-${{depth}}`;
                    wrapper.style.setProperty("--accent", accent);

                    const children = getChildren(node);
                    const title = node.title || node.topic || "";

                    if (children.length) {{
                        const card = document.createElement("div");
                        card.className = "node-card group";
                        card.textContent = title;
                        wrapper.appendChild(card);

                        const childBox = document.createElement("div");
                        childBox.className = "children";
                        childBox.style.setProperty("--accent", accent);
                        children.forEach((child) => childBox.appendChild(buildNode(child, depth + 1, accent)));
                        wrapper.appendChild(childBox);
                    }} else {{
                        wrapper.classList.add("leaf");
                        wrapper.appendChild(createLeafContent(title, accent));
                    }}

                    return wrapper;
                }}

                const root = document.createElement("section");
                root.className = "root-card";
                root.innerHTML = `
                    <div class="root-label">Mindmap</div>
                    <div class="root-title">${{data.title || data.topic || ""}}</div>
                `;
                container.appendChild(root);

                const branches = document.createElement("div");
                branches.className = "branches";
                getChildren(data).forEach((child, index) => {{
                    const accent = palette[index % palette.length];
                    const branch = document.createElement("section");
                    branch.className = "branch";
                    branch.style.setProperty("--accent", accent);

                    const title = document.createElement("div");
                    title.className = "branch-title";
                    title.textContent = child.title || child.topic || "";
                    branch.appendChild(title);

                    const branchChildren = getChildren(child);
                    if (branchChildren.length) {{
                        const childBox = document.createElement("div");
                        childBox.className = "children";
                        childBox.style.setProperty("--accent", accent);
                        branchChildren.forEach((item) => childBox.appendChild(buildNode(item, 2, accent)));
                        branch.appendChild(childBox);
                    }}

                    branches.appendChild(branch);
                }});
                container.appendChild(branches);
            }})();
        </script>
    </body>
    </html>
    """
    with open(output_html, "w", encoding="utf-8") as f:
        f.write(template)


async def capture_html_png(
    html_path,
    png_path,
    viewport,
    selector,
    padding,
    full_height=False,
    device_scale_factor=3,
    logger: Optional[Any] = None,
):
    stage = "playwright_init"
    browser = None
    context = None
    page = None
    try:
        async with async_playwright() as p:
            stage = "chromium_launch"
            browser = await p.chromium.launch(args=["--disable-web-security", "--disable-gpu"])

            stage = "new_context"
            context = await browser.new_context(device_scale_factor=device_scale_factor)
            page = await context.new_page()
            await page.set_viewport_size(viewport)

            abs_path = Path(os.path.abspath(html_path)).as_uri()
            stage = "page_goto"
            await page.goto(abs_path, wait_until="networkidle")

            stage = "wait_for_selector"
            await page.wait_for_selector(selector, timeout=10000)
            await page.wait_for_timeout(1000)

            stage = "measure_layout"
            metrics = await page.evaluate(
                """({ selector, padding }) => {
                    const target = document.querySelector(selector);
                    const rect = target.getBoundingClientRect();
                    const contentHeight = Math.max(
                        target.scrollHeight || 0,
                        target.offsetHeight || 0,
                        Math.ceil(rect.height)
                    );
                    return {
                        left: Math.max(0, Math.floor(rect.left)),
                        top: Math.max(0, Math.floor(rect.top)),
                        width: Math.ceil(rect.width),
                        height: Math.ceil(rect.height),
                        fullHeight: Math.ceil(contentHeight),
                        padding: padding
                    };
                }""",
                {"selector": selector, "padding": padding},
            )

            required_width = max(viewport["width"], metrics["left"] + metrics["width"] + padding * 2)
            required_height = max(
                viewport["height"],
                metrics["top"] + (metrics["fullHeight"] if full_height else metrics["height"]) + padding * 2,
            )

            if required_width > viewport["width"] or required_height > viewport["height"]:
                stage = "resize_viewport"
                await page.set_viewport_size({"width": required_width, "height": required_height})
                await page.wait_for_timeout(300)

            if full_height:
                stage = "remeasure_full_height"
                metrics = await page.evaluate(
                    """({ selector, padding }) => {
                        const target = document.querySelector(selector);
                        const rect = target.getBoundingClientRect();
                        const contentHeight = Math.max(
                            target.scrollHeight || 0,
                            target.offsetHeight || 0,
                            Math.ceil(rect.height)
                        );
                        return {
                            left: Math.max(0, Math.floor(rect.left)),
                            top: Math.max(0, Math.floor(rect.top)),
                            width: Math.ceil(rect.width),
                            height: Math.ceil(contentHeight),
                            padding: padding
                        };
                    }""",
                    {"selector": selector, "padding": padding},
                )
            elif required_width > viewport["width"] or required_height > viewport["height"]:
                stage = "remeasure_after_resize"
                metrics = await page.evaluate(
                    """({ selector }) => {
                        const target = document.querySelector(selector);
                        const rect = target.getBoundingClientRect();
                        return {
                            left: Math.max(0, Math.floor(rect.left)),
                            top: Math.max(0, Math.floor(rect.top)),
                            width: Math.ceil(rect.width),
                            height: Math.ceil(rect.height),
                            fullHeight: Math.ceil(rect.height)
                        };
                    }""",
                    {"selector": selector},
                )

            clip = {
                "x": max(0, metrics["left"] - padding),
                "y": max(0, metrics["top"] - padding),
                "width": metrics["width"] + padding * 2,
                "height": metrics["height"] + padding * 2,
            }
            stage = "screenshot"
            await page.screenshot(path=png_path, clip=clip, omit_background=True, scale="css")
    except Exception:
        if logger is not None:
            logger.error(
                (
                    "❌ 脑图截图异常: stage=%s html=%s png=%s selector=%s viewport=%s "
                    "padding=%s full_height=%s device_scale_factor=%s"
                ),
                stage,
                html_path,
                png_path,
                selector,
                viewport,
                padding,
                full_height,
                device_scale_factor,
                exc_info=True,
            )
            logger.debug("🧵 脑图截图异常堆栈:\n%s", traceback.format_exc())
        raise
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass


async def render_mindmap_to_image(
    mindmap_json: dict,
    png_path: str,
    layout: str,
    js_d3: str = "",
    js_markmap_lib: str = "",
    js_markmap_view: str = "",
    logger: Optional[Any] = None,
) -> bool:
    try:
        html_path = png_path.replace(".png", ".html")
        resolved_layout = resolve_mindmap_layout(layout)
        normalized_json = _unwrap_mindmap_payload(mindmap_json)
        if isinstance(normalized_json, dict) and normalized_json.get("_mindmap_skip"):
            if logger is not None:
                logger.warning("⚠️ 跳过脑图渲染: %s", get_mindmap_skip_reason(normalized_json))
            return False

        normalized_json, stats, normalize_reason = _normalize_mindmap_tree(normalized_json)
        if normalized_json is None:
            if logger is not None:
                logger.error("❌ 脑图结构归一化失败，取消渲染: %s", normalize_reason)
            return False
        if stats["string_nodes"] and logger is not None:
            logger.warning(
                "⚠️ 脑图渲染前自动转换了 %d 个字符串子节点",
                stats["string_nodes"],
            )

        valid, reason = _validate_mindmap_payload(normalized_json)
        if not valid:
            if logger is not None:
                logger.error("❌ 脑图结构无效，取消渲染: %s; payload=%r", reason, repr(mindmap_json)[:500])
            return False
        mindmap_json = normalized_json

        if resolved_layout == "horizontal":
            md_content = mindmap_json_to_markdown(mindmap_json)
            create_markmap_html(
                md_content=md_content,
                output_html=html_path,
                js_d3=js_d3,
                js_markmap_lib=js_markmap_lib,
                js_markmap_view=js_markmap_view,
            )
            await capture_html_png(
                html_path=html_path,
                png_path=png_path,
                viewport={"width": 3840, "height": 2160},
                selector="svg g",
                padding=150,
                full_height=False,
                device_scale_factor=1,
                logger=logger,
            )
        else:
            create_mobile_mindmap_html(mindmap_json, html_path)
            await capture_html_png(
                html_path=html_path,
                png_path=png_path,
                viewport={"width": 1080, "height": 2200},
                selector="#mindmap-mobile",
                padding=24,
                full_height=True,
                device_scale_factor=2,
                logger=logger,
            )

        if os.path.exists(html_path):
            try:
                os.remove(html_path)
            except Exception:
                pass
        return os.path.exists(png_path)
    except Exception as e:
        if logger is not None:
            logger.error(
                "❌ 脑图渲染异常: layout=%s png=%s html=%s err=%r",
                resolved_layout if "resolved_layout" in locals() else layout,
                png_path,
                html_path if "html_path" in locals() else "",
                e,
                exc_info=True,
            )
        return False
