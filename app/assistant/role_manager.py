"""
角色管理器 - 处理 Assistant 的角色设定
"""

import json
import logging
from typing import Dict
from pathlib import Path

from app.services.config_service import get_setting
from app.assistant.prompt_renderer import render_role_prompt

logger = logging.getLogger(__name__)


class RoleManager:
    """角色管理器"""

    def __init__(self):
        self.roles = {}
        self.output_settings = {}
        self._load_roles()
        logger.info(f"🎭 RoleManager初始化完成，加载了 {len(self.roles)} 个角色")

    def _load_roles(self) -> None:
        """加载角色配置"""
        # 先加载默认角色
        self.roles = {
            "通用助手": "你是一个有用的AI助手",
        }
        self.output_settings = {}

        # 从数据库加载角色
        self._load_roles_from_database()

        # 尝试从配置文件加载更多角色（作为备用）
        try:
            roles_file = Path("data/chatbot_roles.json")
            if roles_file.exists():
                with open(roles_file, 'r', encoding='utf-8') as f:
                    additional_roles = json.load(f)
                    # 只添加数据库中不存在的角色
                    for name, prompt in additional_roles.items():
                        if name not in self.roles:
                            self.roles[name] = prompt
                    logger.info(f"✅ 从配置文件加载了 {len(additional_roles)} 个额外角色")
        except Exception as e:
            logger.warning(f"⚠️ 加载角色配置文件失败: {e}")

    def _load_roles_from_database(self) -> None:
        """从数据库加载角色"""
        try:
            from app.models.base import SessionLocal
            from app.models.chatbot_role import ChatBotRole

            with SessionLocal() as db:
                db_roles = db.query(ChatBotRole).all()
                logger.info(f"🔍 开始从数据库加载角色，共查询到 {len(db_roles)} 个角色")

                for role in db_roles:
                    self.roles[role.name] = role.prompt
                    self.output_settings[role.name] = {
                        "enabled": bool(getattr(role, "output_split_enabled", False)),
                        "max_chars": int(getattr(role, "output_max_chars", 120) or 120),
                        "max_count": int(getattr(role, "output_max_count", 3) or 3),
                        "strip_trailing_period": bool(getattr(role, "output_strip_trailing_period", True)),
                        "interval_seconds": float(getattr(role, "output_interval_seconds", 1.0) or 0.0),
                    }
                    logger.info(f"🎭 加载角色: name='{role.name}' display_name='{role.display_name}' prompt_length={len(role.prompt)}")

                logger.info(f"✅ 从数据库加载完成，当前roles字典中有 {len(self.roles)} 个角色")
                logger.info(f"📋 所有角色名称: {list(self.roles.keys())}")

        except Exception as e:
            logger.error(f"❌ 从数据库加载角色失败: {e}", exc_info=True)

    def get_role_prompt(self, role_name: str, variables: dict = None) -> str:
        """获取指定角色的 prompt 并替换系统变量

        Args:
            role_name: 角色名称
            variables: 可选的变量字典，支持以下变量：
                - chat_text: 格式化的聊天历史
                - search_results: 搜索结果（纯文本）
                - sender: 发送者
                - content: 消息内容

        Returns:
            替换变量后的 prompt（如果提供了 variables）
        """
        logger.debug(f"🔍 尝试获取角色 '{role_name}' 的prompt，当前roles字典有 {len(self.roles)} 个角色")
        logger.debug(f"📋 当前所有角色: {list(self.roles.keys())}")

        # 获取基础 Prompt
        if role_name in self.roles:
            logger.debug(f"✅ 找到角色 '{role_name}'")
            base_prompt = self.roles[role_name]
        else:
            # 如果找不到，使用默认角色
            base_prompt = self.roles.get("通用助手", self._get_default_role_template())
            logger.warning(f"⚠️ 角色 '{role_name}' 不存在，使用默认角色")


        # 如果提供了变量，进行替换
        if variables:
            base_prompt = render_role_prompt(base_prompt, variables)
            logger.debug(f"🔄 已替换系统变量（安全模式），prompt 长度: {len(base_prompt)}")

        return base_prompt

    def get_output_settings(self, role_name: str) -> dict:
        """获取角色的输出规范配置。"""
        return self.output_settings.get(role_name, {
            "enabled": False,
            "max_chars": 120,
            "max_count": 3,
            "strip_trailing_period": True,
            "interval_seconds": 1.0,
        }).copy()

    def add_role(self, role_name: str, prompt: str) -> bool:
        """添加新角色"""
        try:
            self.roles[role_name] = prompt
            self._save_roles()
            logger.info(f"✅ 添加角色成功: {role_name}")
            return True
        except Exception as e:
            logger.error(f"❌ 添加角色失败: {e}")
            return False

    def update_role(self, role_name: str, prompt: str) -> bool:
        """更新角色"""
        try:
            if role_name not in self.roles:
                logger.warning(f"⚠️ 尝试更新不存在的角色: {role_name}")
                return False

            self.roles[role_name] = prompt
            self._save_roles()
            logger.info(f"✅ 更新角色成功: {role_name}")
            return True
        except Exception as e:
            logger.error(f"❌ 更新角色失败: {e}")
            return False

    def delete_role(self, role_name: str) -> bool:
        """删除角色"""
        try:
            if role_name == "default":
                logger.warning("⚠️ 不能删除默认角色")
                return False

            if role_name not in self.roles:
                logger.warning(f"⚠️ 尝试删除不存在的角色: {role_name}")
                return False

            del self.roles[role_name]
            self._save_roles()
            logger.info(f"✅ 删除角色成功: {role_name}")
            return True
        except Exception as e:
            logger.error(f"❌ 删除角色失败: {e}")
            return False

    def get_all_roles(self) -> Dict[str, str]:
        """获取所有角色"""
        return self.roles.copy()

    def get_role_list(self) -> list:
        """获取角色名称列表"""
        return list(self.roles.keys())

    def _save_roles(self) -> None:
        """保存角色配置到文件"""
        try:
            roles_file = Path("data/chatbot_roles.json")
            roles_file.parent.mkdir(parents=True, exist_ok=True)

            roles_to_save = self.roles.copy()

            with open(roles_file, 'w', encoding='utf-8') as f:
                json.dump(roles_to_save, f, ensure_ascii=False, indent=2)

            logger.debug(f"💾 保存角色配置成功: {len(roles_to_save)} 个自定义角色")

        except Exception as e:
            logger.error(f"❌ 保存角色配置失败: {e}")

    def _get_default_role_template(self) -> str:
        """获取默认角色模板（包含所有系统变量）"""
        return """你是一个专业的AI助手。

## 聊天历史
{chat_text}

## 网络搜索结果
{search_results}

## 当前问题
发送者：{sender}
问题：{content}

请基于以上信息提供专业、准确的回复。"""

    def reload_roles(self) -> None:
        """重新加载角色配置"""
        logger.info("🔄 重新加载角色配置...")
        self.roles.clear()
        self._load_roles()
        logger.info(f"✅ 重新加载完成，当前有 {len(self.roles)} 个角色")
