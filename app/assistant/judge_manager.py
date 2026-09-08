"""Assistant 主动回复 Judge 配置管理器。"""

import logging
from typing import Dict, Any, Optional

from app.assistant.prompt_renderer import render_judge_prompt

logger = logging.getLogger(__name__)


class JudgeManager:
    """Judge 管理器"""

    def __init__(self):
        self.judges: Dict[str, Dict[str, Any]] = {}
        self._load_judges()
        logger.info(f"⚖️ JudgeManager 初始化完成，加载了 {len(self.judges)} 个 Judge")

    def _load_judges(self) -> None:
        """加载 Judge 配置"""
        self.judges = {}
        self._load_judges_from_database()

    def _load_judges_from_database(self) -> None:
        """从数据库加载 Judge"""
        try:
            from app.models.base import SessionLocal
            from app.models.chatbot_judge import ChatBotJudge

            with SessionLocal() as db:
                db_judges = db.query(ChatBotJudge).all()
                logger.info(f"🔍 开始从数据库加载 Judge，共查询到 {len(db_judges)} 个")

                for judge in db_judges:
                    self.judges[judge.name] = {
                        "name": judge.name,
                        "display_name": judge.display_name,
                        "description": judge.description or "",
                        "prompt": judge.prompt,
                        "prompt_mode": (judge.prompt_mode or "simple").lower(),
                        "trigger_msg_threshold": int(getattr(judge, "trigger_msg_threshold", 5) or 0),
                        "trigger_interval_minutes": int(getattr(judge, "trigger_interval_minutes", 1) or 0),
                        "cooldown_msg_threshold": int(getattr(judge, "cooldown_msg_threshold", 5) or 0),
                        "cooldown_minutes": int(getattr(judge, "cooldown_minutes", 1) or 0),
                    }
        except Exception as e:
            logger.error(f"❌ 从数据库加载 Judge 失败: {e}", exc_info=True)

    def get_judge(self, judge_name: str) -> Optional[Dict[str, Any]]:
        """获取指定 Judge 配置"""
        if judge_name in self.judges:
            return self.judges[judge_name]
        return self.judges.get("default_judge")

    def get_judge_prompt(self, judge_name: str, variables: Dict[str, Any] = None) -> str:
        """获取并渲染 Judge Prompt"""
        judge = self.get_judge(judge_name)
        if not judge:
            return ""

        return render_judge_prompt(
            template=judge.get("prompt", ""),
            mode=judge.get("prompt_mode", "simple"),
            variables=variables or {},
        )

    def get_judge_display_name(self, judge_name: str) -> str:
        """获取 Judge 展示名"""
        judge = self.get_judge(judge_name)
        if not judge:
            return judge_name
        return judge.get("display_name") or judge.get("name") or judge_name

    def get_judge_timing(self, judge_name: str) -> Dict[str, int]:
        """获取 Judge 的触发/冷却参数。"""
        judge = self.get_judge(judge_name) or {}
        return {
            "trigger_msg_threshold": int(judge.get("trigger_msg_threshold", 5) or 0),
            "trigger_interval_minutes": int(judge.get("trigger_interval_minutes", 1) or 0),
            "cooldown_msg_threshold": int(judge.get("cooldown_msg_threshold", 5) or 0),
            "cooldown_minutes": int(judge.get("cooldown_minutes", 1) or 0),
        }

    def reload_judges(self) -> None:
        """重新加载 Judge 配置"""
        logger.info("🔄 重新加载 Judge 配置...")
        self.judges.clear()
        self._load_judges()
        logger.info(f"✅ Judge 配置重新加载完成，当前有 {len(self.judges)} 个")

    def _get_default_judge_template(self) -> str:
        return """## Role
你是一个高情商的聊天群组观察员。

## Context Background
[对话开始]
{chat_text}
[对话结束]

## Task
请重点分析【对话结束】前的最后几条消息，判断是否需要主动回复。

## Output (Strict JSON)
{
  "atmosphere": "简述当前氛围（如：技术讨论、轻松闲聊、争论等）",
  "should_reply": true/false,
  "reason": "为什么判断需要或不需要回复"
}"""
