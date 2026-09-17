"""
Assistant Prompt 渲染工具
- 安全变量替换（仅替换已知变量，避免 JSON 花括号误伤）
- Judge 双模式渲染（simple/template）
"""

import re
from typing import Dict, Iterable


ROLE_ALLOWED_VARS = ("chat_text", "search_results", "sender", "content")
JUDGE_ALLOWED_VARS = ("chat_text",)


def _safe_replace(template: str, variables: Dict[str, str], allowed_keys: Iterable[str]) -> str:
    """仅替换白名单变量，兼容 {var} 与 {{var}} 两种写法。"""
    if not template:
        return ""

    rendered = template
    for key in allowed_keys:
        value = str(variables.get(key, ""))
        # 支持 {{ key }}
        rendered = re.sub(r"\{\{\s*" + re.escape(key) + r"\s*\}\}", lambda _: value, rendered)
        # 支持 { key }
        rendered = re.sub(r"\{\s*" + re.escape(key) + r"\s*\}", lambda _: value, rendered)
    return rendered


def render_role_prompt(template: str, variables: Dict[str, str]) -> str:
    """渲染 Role Prompt（安全替换模式）。"""
    safe_vars = {
        "chat_text": "",
        "search_results": "",
        "sender": "",
        "content": "",
    }
    safe_vars.update(variables or {})
    return _safe_replace(template, safe_vars, ROLE_ALLOWED_VARS)


def render_judge_prompt(template: str, mode: str, variables: Dict[str, str]) -> str:
    """Compatibility renderer. Both legacy modes now receive automatic context."""
    from app.assistant.prompt_composer import compose_decision
    return "\n\n".join(b['content'] for b in compose_decision(template, str((variables or {}).get('chat_text',''))))
