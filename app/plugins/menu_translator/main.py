"""
菜单翻译插件 (menu_translator)

交互流程：
1. 用户发送触发关键词（如"翻译菜单"）
2. 插件进入"等待图片"状态，最长等待 image_window_minutes 分钟
3. 用户发送图片，插件缓存图片路径
4. 收到最后一张图片后，若 idle_timeout_seconds 秒内无新图片，提前结束收集
5. 插件一次性将所有图片通过 litellm（OpenAI vision 格式）传给大模型
6. 使用 Pydantic 结构化输出验证 JSON，渲染双语 HTML，生成 PDF，发送给用户
"""

import re
import json
import base64
import os
import time
import logging
import threading
from pathlib import Path
from typing import Optional, List, Dict, Any

import json
from pydantic import BaseModel, Field

from app.core.event_bus import Event, EventType, get_event_bus
from app.services.plugin_config_context import ScopedConfigAttribute
from app.utils.plugin_config import get_config
from app.services.llm_manager import get_llm_manager
from app.assistant.chat_log import ChatLogManager

from .menu_generator import generate_html, render_image

logger = logging.getLogger(__name__)

PLUGIN_NAME = "menu_translator"

# ──────────────────────────────────────────────
# Pydantic 结构化输出模型
# ──────────────────────────────────────────────

class MenuItem(BaseModel):
    name_zh: str = Field(description="菜品中文名称")
    name_orig: str = Field(description="菜品原文名称")
    desc_zh: str = Field(default="", description="菜品中文描述，无则为空字符串")
    desc_orig: str = Field(default="", description="菜品原文描述，无则为空字符串")
    price: str = Field(default="", description="价格，保留原样，无则为空字符串")

class MenuSection(BaseModel):
    section_name_zh: str = Field(description="分区中文名称，如前菜、主菜、甜点")
    section_name_orig: str = Field(default="", description="分区原文名称")
    items: List[MenuItem] = Field(description="该分区下的菜品列表")

class MenuData(BaseModel):
    restaurant_name: str = Field(default="", description="餐厅名称，识别不到则为空字符串")
    sections: List[MenuSection] = Field(description="菜单分区列表")


class MenuTranslatorPlugin:
    """菜单翻译插件主类"""

    @ScopedConfigAttribute
    def trigger_keywords(self):
        return get_config('trigger_keywords', plugin_name=PLUGIN_NAME) or ['翻译菜单', '菜单翻译', 'menu']

    @ScopedConfigAttribute
    def image_window_minutes(self):
        return int(get_config('image_window_minutes', 5, plugin_name=PLUGIN_NAME))

    @ScopedConfigAttribute
    def idle_timeout_seconds(self):
        return int(get_config('idle_timeout_seconds', 15, plugin_name=PLUGIN_NAME))

    @ScopedConfigAttribute
    def max_images(self):
        return int(get_config('max_images', 6, plugin_name=PLUGIN_NAME))

    @ScopedConfigAttribute
    def translate_prompt(self):
        return get_config('translate_prompt', plugin_name=PLUGIN_NAME) or self._default_prompt()

    def __init__(self, context=None):
        self.context = context
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        if context is not None:
            migration_notes = context.storage.migrate_legacy_directory(
                Path(self.plugin_dir) / "images", storage_class="generated", relative="menus"
            )
            self.output_dir = str(context.storage.generated_root / "menus")
            if migration_notes:
                context.audit.record(
                    "storage_migration",
                    summary="菜单翻译结果已迁移到插件标准存储目录",
                    details={"moved_files": len(migration_notes)},
                )
        else:
            self.output_dir = os.path.join(self.plugin_dir, "images")
        os.makedirs(self.output_dir, exist_ok=True)

        # 从 config.json 读取配置
        self.trigger_keywords: List[str] = get_config("trigger_keywords", plugin_name=PLUGIN_NAME) or [
            "翻译菜单", "菜单翻译", "menu"
        ]
        self.image_window_minutes: int = int(
            get_config("image_window_minutes", 5, plugin_name=PLUGIN_NAME)
        )
        self.idle_timeout_seconds: int = int(
            get_config("idle_timeout_seconds", 15, plugin_name=PLUGIN_NAME)
        )
        self.max_images: int = int(
            get_config("max_images", 6, plugin_name=PLUGIN_NAME)
        )
        self.translate_prompt: str = get_config("translate_prompt", plugin_name=PLUGIN_NAME) or self._default_prompt()

        # 状态管理：{chat_name: SessionState}
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

        logger.info(
            f"🍽️ 菜单翻译插件初始化完成 | 触发词: {self.trigger_keywords} | "
            f"等待窗口: {self.image_window_minutes}min | 空闲超时: {self.idle_timeout_seconds}s"
        )


    def _default_prompt(self) -> str:
        return (
            "你是一位精通多国饮食文化的专业菜单翻译专家。请分析图片内容，提取菜单信息并翻译成中文。\n\n"
            "核心原则：\n"
            "1. **准确翻译**：菜名应采用通用的中文叫法（如 'Spaghetti Carbonara' -> '培根蛋面' 而非 '卡波纳拉意大利面'）。\n"
            "2. **保留原貌**：必须保留原文名称 (name_orig) 和描述 (desc_orig)。\n"
            "3. **结构还原**：利用字体大小、排版布局来识别 '分区' (Sections)，避免将分类标题误识别为菜品。\n"
            "4. **排除干扰**：忽略广告、WiFi密码、地址电话等非菜单内容（除非它是餐厅名称）。\n\n"
            "字段要求：\n"
            "- name_zh: 简洁明了的中文菜名。\n"
            "- desc_zh: 翻译菜品描述/配料。如果原文有'辣'、'素食'等图标或标记，请在描述后用括号注明，如'(含坚果)'。\n"
            "- price: 保持原样（包含货币符号）。\n"
            "- sections: 合理划分前菜、主菜、饮品等区域。\n"
            "- restaurant_name: 餐厅名称，识别不到则为空字符串。\n\n"
            "处理多图时，请自动去重并合并同类项。"
        )

    # ──────────────────────────────────────────────
    # 事件处理器
    # ──────────────────────────────────────────────

    def handle_text(self, event: Event) -> bool:
        """处理文本消息：检测触发关键词"""
        try:
            message = event.data.get("message", "").strip()
            chat_name = event.data.get("chat_name", "")
            wx = event.context.get("wx")

            if not message or not chat_name:
                return False

            # 检测触发关键词
            triggered = any(kw in message for kw in self.trigger_keywords)
            if not triggered:
                return False

            logger.info(f"🍽️ [{chat_name}] 检测到菜单翻译触发词: {message!r}")

            with self._lock:
                # 如果已有进行中的 session，先取消
                self._cancel_session(chat_name)

                # 建立新 session
                self._sessions[chat_name] = {
                    "triggered_at": time.time(),
                    "images": [],
                    "last_image_at": None,
                    "wx": wx,
                    "timer": None,
                    "max_timer": None,
                }
                
                # 申请会话期权限豁免（允许不@）
                # 有效期：image_window_minutes (分钟) * 60
                get_event_bus().request_session_permission(
                    chat_name, 
                    PLUGIN_NAME, 
                    self.image_window_minutes * 60
                )

            # 发送提示
            if wx:
                wx.send_message(
                    chat_name,
                    f"📷 请发送菜单图片（最多 {self.max_images} 张）"
                    # f"发完后 {self.idle_timeout_seconds} 秒自动开始翻译，"
                    # f"或最长等待 {self.image_window_minutes} 分钟。"
                )

            # 启动最长等待定时器
            max_timer = self.context.workers.start_timer(
                f"max-timeout-{chat_name}-{time.time_ns()}",
                self.image_window_minutes * 60,
                self._on_max_timeout,
                args=(chat_name,),
            )

            with self._lock:
                if chat_name in self._sessions:
                    self._sessions[chat_name]["max_timer"] = max_timer

            return True

        except Exception as e:
            logger.error(f"❌ menu_translator handle_text 失败: {e}", exc_info=True)
            return False

    def handle_image(self, event: Event) -> bool:
        """处理图片消息：收集图片到当前 session"""
        try:
            chat_name = event.data.get("chat_name", "")
            file_path = event.data.get("file_path", "")
            wx = event.context.get("wx")

            if not chat_name:
                return False

            with self._lock:
                session = self._sessions.get(chat_name)
                if not session:
                    return False  # 没有活跃 session，不处理

                # 检查 session 是否已过期
                elapsed = time.time() - session["triggered_at"]
                if elapsed > self.image_window_minutes * 60:
                    logger.info(f"🍽️ [{chat_name}] Session 已过期，忽略图片")
                    return False

                # 如果图片路径不存在，尝试下载
                if not file_path or not Path(file_path).exists():
                    message_id = event.data.get("message_id")
                    if wx and message_id:
                        try:
                            file_path = wx.download_image_message(chat_name, message_id)
                            logger.info(f"🍽️ [{chat_name}] 图片下载成功: {file_path}")
                        except Exception as e:
                            logger.error(f"❌ [{chat_name}] 图片下载失败: {e}")
                            return False

                if not file_path or not Path(file_path).exists():
                    logger.warning(f"⚠️ [{chat_name}] 图片路径无效，跳过")
                    return False

                # 检查图片数量上限
                if len(session["images"]) >= self.max_images:
                    logger.info(f"🍽️ [{chat_name}] 已达到最大图片数 {self.max_images}，忽略新图片")
                    return False

                # 添加图片
                session["images"].append((time.time(), str(file_path)))
                session["last_image_at"] = time.time()
                count = len(session["images"])
                logger.info(f"🍽️ [{chat_name}] 收集到第 {count} 张图片: {file_path}")

                # 取消旧的空闲定时器，重新计时
                old_timer = session.get("timer")
                if old_timer:
                    old_timer.cancel()

                # 如果已达上限，立即触发
                if count >= self.max_images:
                    logger.info(f"🍽️ [{chat_name}] 已收集 {self.max_images} 张图片，立即开始翻译")
                    self.context.workers.start(
                        f"translate-{chat_name}-{time.time_ns()}",
                        self._process_session,
                        args=(chat_name,),
                    )
                    return True

                # 设置新的空闲超时定时器
                idle_timer = self.context.workers.start_timer(
                    f"idle-timeout-{chat_name}-{time.time_ns()}",
                    self.idle_timeout_seconds,
                    self._on_idle_timeout,
                    args=(chat_name,),
                )
                session["timer"] = idle_timer

            return True

        except Exception as e:
            logger.error(f"❌ menu_translator handle_image 失败: {e}", exc_info=True)
            return False

    # ──────────────────────────────────────────────
    # 定时器回调
    # ──────────────────────────────────────────────

    def _on_idle_timeout(self, chat_name: str):
        """空闲超时：最后一张图片后 idle_timeout_seconds 秒无新图片"""
        logger.info(f"🍽️ [{chat_name}] 空闲超时触发，开始翻译")
        self._process_session(chat_name)

    def _on_max_timeout(self, chat_name: str):
        """最长等待超时"""
        with self._lock:
            session = self._sessions.get(chat_name)
            if not session:
                return
            if not session.get("images"):
                wx = session.get("wx")
                if wx:
                    wx.send_message(chat_name, "⏰ 等待超时，未收到图片，菜单翻译已取消。")
                self._sessions.pop(chat_name, None)
                return

        logger.info(f"🍽️ [{chat_name}] 最长等待超时，开始翻译")
        self._process_session(chat_name)

    # ──────────────────────────────────────────────
    # 核心翻译流程
    # ──────────────────────────────────────────────

    def _process_session(self, chat_name: str):
        """执行翻译流程（在独立线程中运行）"""
        with self._lock:
            session = self._sessions.pop(chat_name, None)
            if not session:
                return
            # 取消所有定时器
            for key in ("timer", "max_timer"):
                t = session.get(key)
                if t:
                    t.cancel()

        wx = session.get("wx")
        images = session.get("images", [])

        if not images:
            logger.warning(f"⚠️ [{chat_name}] 没有收集到图片，翻译取消")
            if wx:
                wx.send_message(chat_name, "⚠️ 未收到图片，菜单翻译已取消。")
            return

        logger.info(f"🍽️ [{chat_name}] 开始翻译 {len(images)} 张图片")

        if wx:
            wx.send_message(chat_name, f"收到 {len(images)} 张菜单图片，正在整理...")

        try:
            # 1. 一次性调用 Gemini 翻译所有图片
            menu_data = self._translate_images_batch(images, chat_name)

            if not menu_data or not menu_data.get("sections"):
                if wx:
                    wx.send_message(chat_name, "❌ 菜单识别失败，请稍后重试。")
                return

            # 2. 生成 HTML
            html_content = generate_html(menu_data)

            # 3. 渲染图片
            # 优先使用餐厅名称作为文件名
            restaurant_name = menu_data.get("restaurant_name", "").strip()
            date_str = time.strftime("%Y%m%d")
            
            if restaurant_name:
                # 简单清洗文件名
                safe_name = re.sub(r'[\\/:*?"<>|]', "_", restaurant_name)
                # 截断过长的名称
                if len(safe_name) > 30:
                    safe_name = safe_name[:30]
                img_filename = f"{safe_name}_Menu_{date_str}.png"
            else:
                # Fallback 到群名
                safe_name = re.sub(r'[\\/:*?"<>|]', "_", chat_name)
                img_filename = f"Menu_{safe_name}_{date_str}.png"
                
            img_path = os.path.join(self.output_dir, img_filename)

            success = render_image(html_content, img_path)

            if not success or not os.path.exists(img_path):
                if wx:
                    wx.send_message(chat_name, "❌ 图片生成失败，请稍后重试。")
                return

            # 4. 发送图片
            if wx:
                try:
                    wx.send_files(chat_name, img_path)
                    logger.info(f"✅ [{chat_name}] 菜单翻译图片已发送: {img_path}")
                except Exception as e:
                    logger.error(f"❌ [{chat_name}] 发送图片失败: {e}")
                    wx.send_message(chat_name, "❌ 图片发送失败，请稍后重试。")

            # 5. 将翻译结果写入 chat_logs，供 chatbot 作为上下文
            try:
                menu_text = self._format_menu_text(menu_data)
                chat_log = ChatLogManager()
                chat_log.save_message(chat_name, "菜单翻译", menu_text)
                logger.info(f"📝 [{chat_name}] 菜单翻译结果已写入 chat_logs")
            except Exception as e:
                logger.warning(f"⚠️ [{chat_name}] 写入 chat_logs 失败（不影响主流程）: {e}")

        except Exception as e:
            logger.error(f"❌ [{chat_name}] 翻译流程失败: {e}", exc_info=True)
            if wx:
                wx.send_message(chat_name, "⚠️ 菜单翻译失败，请稍后重试。")

    def _format_menu_text(self, menu_data: Dict) -> str:
        """
        将 menu_data 格式化为可读的中文纯文本，写入 chat_logs 供 chatbot 作为上下文。
        格式示例：
          【餐厅名称】
          ── 前菜 ──
          • 凯撒沙拉 (Caesar Salad) ¥38
            生菜、培根碎、帕玛森芝士
        """
        lines = []
        restaurant = menu_data.get("restaurant_name", "").strip()
        if restaurant:
            lines.append(f"【{restaurant}】菜单翻译结果：")
        else:
            lines.append("菜单翻译结果：")

        for section in menu_data.get("sections", []):
            sec_zh = section.get("section_name_zh", "").strip()
            sec_orig = section.get("section_name_orig", "").strip()
            if sec_orig and sec_orig != sec_zh:
                lines.append(f"\n── {sec_zh}（{sec_orig}）──")
            elif sec_zh:
                lines.append(f"\n── {sec_zh} ──")

            for item in section.get("items", []):
                name_zh = item.get("name_zh", "").strip()
                name_orig = item.get("name_orig", "").strip()
                price = item.get("price", "").strip()
                desc_zh = item.get("desc_zh", "").strip()

                # 菜名行
                name_line = f"• {name_zh}"
                if name_orig and name_orig != name_zh:
                    name_line += f" ({name_orig})"
                if price:
                    name_line += f"  {price}"
                lines.append(name_line)

                # 描述行（缩进）
                if desc_zh:
                    lines.append(f"  {desc_zh}")

        return "\n".join(lines)

    def _translate_images_batch(self, images: List[tuple], chat_name: str) -> Optional[Dict]:
        """
        一次性将所有图片通过 litellm（OpenAI vision 格式）发送给大模型。
        所有图片作为多个 image_url 对象放在同一条 user 消息中。
        使用 Pydantic JSON Schema 嵌入 system prompt 实现结构化输出。
        返回符合 MenuData 结构的字典，失败返回 None。
        """
        try:
            # 构建 user message content：先放文字指令，再依次放所有图片
            user_content = [{"type": "text", "text": "请识别并翻译图片中的菜单内容，按要求输出JSON。"}]

            loaded_count = 0
            for idx, (ts, img_path) in enumerate(images):
                logger.info(f"🍽️ [{chat_name}] 加载第 {idx+1}/{len(images)} 张图片: {img_path}")
                try:
                    with open(img_path, "rb") as f:
                        img_data = f.read()

                    mime_type = self._detect_mime_type(img_data, img_path)
                    img_b64 = base64.b64encode(img_data).decode("utf-8")
                    user_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{img_b64}"}
                    })
                    loaded_count += 1
                    logger.info(f"🍽️ [{chat_name}] 图片 {idx+1} 加载成功: {mime_type}, {len(img_data)} bytes")
                except Exception as e:
                    logger.error(f"❌ [{chat_name}] 加载图片 {idx+1} 失败: {e}")
                    continue

            if loaded_count == 0:
                logger.error(f"❌ [{chat_name}] 没有成功加载任何图片")
                return None

            # 将 Pydantic JSON Schema 嵌入 system prompt，要求模型严格按格式输出
            schema_str = json.dumps(MenuData.model_json_schema(), ensure_ascii=False, indent=2)
            system_prompt = (
                self.translate_prompt
                + "\n\n请严格按照以下 JSON Schema 输出，不要有任何额外说明或 markdown 代码块：\n"
                + schema_str
            )

            # 尝试次数
            max_retries = 3
            last_error = None

            for attempt in range(max_retries):
                try:
                    logger.info(f"🍽️ [{chat_name}] 调用 LLM（{loaded_count} 张图片，litellm vision，尝试 {attempt+1}/{max_retries}）...")
                    llm_manager = get_llm_manager()
                    
                    # 使用 detail: high 模式
                    vision_messages = []
                    # System prompt
                    vision_messages.append({"role": "system", "content": system_prompt})
                    
                    # User content with images
                    user_content_payload = [{"type": "text", "text": "请识别并翻译图片中的菜单内容，按要求输出JSON。"}]
                    for item in user_content[1:]: # Skip the first text item we created earlier, rebuild with detail parameter
                        if item["type"] == "image_url":
                            user_content_payload.append({
                                "type": "image_url",
                                "image_url": {
                                    "url": item["image_url"]["url"],
                                    "detail": "high"
                                }
                            })
                    vision_messages.append({"role": "user", "content": user_content_payload})

                    raw_text = llm_manager.call(
                        plugin_name=PLUGIN_NAME,
                        call_type="vision",
                        messages=vision_messages
                    )

                    logger.info(f"🍽️ [{chat_name}] LLM 返回 {len(raw_text)} 字符")

                    # 清理可能的 markdown 代码块包装
                    text = raw_text.strip()
                    code_block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
                    if code_block:
                        text = code_block.group(1)
                    else:
                        start, end = text.find("{"), text.rfind("}")
                        if start != -1 and end > start:
                            text = text[start:end + 1]

                    # 尝试解析 JSON
                    try:
                        menu_obj = MenuData.model_validate_json(text)
                        result = menu_obj.model_dump()
                        logger.info(
                            f"✅ [{chat_name}] 解析成功: {len(result.get('sections', []))} 个分区, "
                            f"餐厅: {result.get('restaurant_name', '(未知)')}"
                        )
                        return result
                    except Exception as json_err:
                        logger.warning(f"⚠️ [{chat_name}] JSON 解析失败 (尝试 {attempt+1}): {json_err}")
                        # 简单的 fallback: 尝试用 json.loads 修复（虽然 model_validate_json 已经很强了，这里主要防脏数据）
                        # 如果是 Pydantic 校验错误，通常意味着 LLM 输出格式不对，重试可能有效
                        last_error = json_err
                        continue

                except Exception as e:
                    logger.error(f"❌ [{chat_name}] LLM 调用失败 (尝试 {attempt+1}): {e}")
                    last_error = e
                    time.sleep(1) # 稍作等待
            
            logger.error(f"❌ [{chat_name}] 重试 {max_retries} 次后仍失败: {last_error}")
            return None

        except Exception as e:
            logger.error(f"❌ [{chat_name}] 翻译流程致命错误: {e}", exc_info=True)
            return None

    def _detect_mime_type(self, img_data: bytes, img_path: str) -> str:
        """根据文件头和扩展名检测 MIME 类型"""
        # 文件头检测（优先）
        if img_data.startswith(b'\xff\xd8\xff'):
            return "image/jpeg"
        elif img_data.startswith(b'\x89PNG'):
            return "image/png"
        elif img_data.startswith(b'GIF8'):
            return "image/gif"
        elif img_data.startswith(b'RIFF') and img_data[8:12] == b'WEBP':
            return "image/webp"

        # 扩展名回退
        suffix = Path(img_path).suffix.lower()
        mime_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp",
            ".gif": "image/gif",
        }
        return mime_map.get(suffix, "image/jpeg")

    def _cancel_session(self, chat_name: str):
        """取消并清理 session（需在 _lock 内调用）"""
        session = self._sessions.pop(chat_name, None)
        if session:
            for key in ("timer", "max_timer"):
                t = session.get(key)
                if t:
                    t.cancel()
            
            # 释放会话期权限
            get_event_bus().release_session_permission(chat_name, PLUGIN_NAME)
            
            logger.info(f"🍽️ [{chat_name}] 已取消旧 session")


# ──────────────────────────────────────────────
# 全局实例与注册函数
# ──────────────────────────────────────────────

plugin: Optional[MenuTranslatorPlugin] = None


def handle_text(event: Event) -> bool:
    if plugin:
        return plugin.handle_text(event)
    return False


def handle_image(event: Event) -> bool:
    if plugin:
        return plugin.handle_image(event)
    return False


def register(event_bus, subscribe, context):
    """注册插件"""
    global plugin
    logger.info("🍽️ 注册 menu_translator 插件...")
    try:
        plugin = MenuTranslatorPlugin(context)
        context.health.register(lambda: {
            "status": "healthy" if plugin is not None else "unhealthy",
            "message": "菜单翻译会话服务已就绪" if plugin is not None else "菜单翻译服务未初始化",
            "active_sessions": len(plugin._sessions) if plugin is not None else 0,
        })
        context.register_cleanup(unregister)

        subscribe(
            event_type=EventType.TEXT_MESSAGE_RECEIVED,
            handler=handle_text
        )

        subscribe(
            event_type=EventType.IMAGE_MESSAGE_RECEIVED,
            handler=handle_image
        )

        logger.info("✅ menu_translator 插件注册成功")
    except Exception as e:
        logger.error(f"❌ menu_translator 插件注册失败: {e}", exc_info=True)


def unregister():
    """取消注册插件"""
    global plugin
    logger.info("🍽️ 卸载 menu_translator 插件...")
    if plugin:
        with plugin._lock:
            for chat_name in list(plugin._sessions.keys()):
                plugin._cancel_session(chat_name)
    plugin = None
    logger.info("✅ menu_translator 插件卸载完成")


if __name__ == "__main__":
    # 本地测试模式
    import sys
    
    # 确保能导入 app 包（将 Mabobot 项目根目录加入 sys.path）。
    root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)
        
    logging.basicConfig(level=logging.INFO)
    logger.info("🚀 启动菜单翻译插件本地测试模式...")
    
    # 需要初始化 EventBus 和 LLMManager 的环境上下文，但由于仅仅做本地接口调用，
    # 我们尽量保证 get_llm_manager() 能够运行
    # 如果根目录下的 config.json 配置完好，get_llm_manager() 一般能够直接初始化
    
    test_plugin = MenuTranslatorPlugin()
    
    img1 = os.path.join(test_plugin.plugin_dir, "test1.png")
    img2 = os.path.join(test_plugin.plugin_dir, "test2.png")
    
    images_to_test = []
    if os.path.exists(img1):
        images_to_test.append((time.time(), img1))
    if os.path.exists(img2):
        images_to_test.append((time.time(), img2))
        
    if not images_to_test:
        logger.error(f"❌ 找不到测试用的图片 {img1} 或 {img2}")
        sys.exit(1)
        
    chat_name = "local_test_chat"
    
    test_plugin._sessions[chat_name] = {
        "triggered_at": time.time(),
        "images": images_to_test,
        "last_image_at": time.time(),
        "wx": None,
        "timer": None,
        "max_timer": None,
    }
    
    logger.info(f"📂 准备对 {len(images_to_test)} 张本地图片进行翻译渲染...")
    test_plugin._process_session(chat_name)
    logger.info("🎉 本地测试流程执行结束，请在 images/ 目录下查看生成的截图！")
