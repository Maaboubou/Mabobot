#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Google 以图搜图服务（基于 Chrome + Selenium）

- 复用远程调试端口连接已启动的 Chrome（必要时尝试自动拉起）
- 优先走 images.google.com 的相机上传路径（含文件对话框自动化与回退到直接 file input）
- 兜底使用 Google Lens 上传页（lens.google.com/upload）
- 提取标题与外部链接，并进行简单清洗
"""

import os
import time
import logging
from typing import List, Tuple, Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from app.services.shared_chrome import SharedChromeSettings, get_shared_chrome_operation_lock
from app.services.shared_chrome_runtime import get_shared_chrome_runtime


logger = logging.getLogger(__name__)


class GoogleImageReverseSearchService:
    def __init__(
        self,
        *,
        chrome_debug_port: int,
        chrome_path: str,
        chrome_user_data_dir: str,
        chrome_profile_dir: str,
        connect_retries: int = 10,
        connect_retry_sleep: float = 0.5,
    ) -> None:
        self.chrome_debug_port = int(chrome_debug_port)
        self.chrome_path = chrome_path
        self.chrome_user_data_dir = chrome_user_data_dir
        self.chrome_profile_dir = chrome_profile_dir
        self.connect_retries = connect_retries
        self.connect_retry_sleep = connect_retry_sleep
        self.browser_runtime = get_shared_chrome_runtime(
            settings=SharedChromeSettings(
                self.chrome_debug_port, self.chrome_path,
                self.chrome_user_data_dir, self.chrome_profile_dir,
            ),
            logger=logger,
        )
        self.browser_runtime.connect_retries = connect_retries
        self.browser_runtime.WINDOW_HANDLE_STABILIZE_DELAY = connect_retry_sleep

    # ---------- Public ----------
    def search(self, image_path: str, max_results: int = 10, wait_seconds: int = 6) -> Tuple[List[str], List[str], str]:
        with get_shared_chrome_operation_lock():
            return self._search_locked(image_path, max_results, wait_seconds)

    def _search_locked(self, image_path: str, max_results: int, wait_seconds: int) -> Tuple[List[str], List[str], str]:
        """以图搜图主流程：先尝试 images.google.com，相机上传；失败再用 Lens 上传。
        返回 (titles, urls, page_text)
        """
        driver = None
        worker_handle: Optional[str] = None
        original_handle: Optional[str] = None
        try:
            self._ensure_chrome_ready()
            driver = self._connect_driver()

            original_handle = driver.current_window_handle if driver.window_handles else None
            worker_handle = self._get_or_create_worker_tab(driver)
            driver.switch_to.window(worker_handle)

            # 尝试路径 A：images.google.com 相机上传
            try:
                titles, urls = self._search_via_images_google(driver, image_path, wait_seconds)
                if titles or urls:
                    page_text = self._extract_full_page_text(driver)
                    return titles[:max_results], urls[:max_results], page_text
            except Exception as e:
                logger.warning("images.google.com 流程失败，将尝试 Lens 兜底: %s", e)

            # 尝试路径 B：Google Lens 上传
            try:
                titles, urls = self._search_via_lens(driver, image_path, wait_seconds)
                page_text = self._extract_full_page_text(driver)
                return titles[:max_results], urls[:max_results], page_text
            except Exception as e:
                logger.error("Lens 兜底流程失败: %s", e)
                return [], [], ""
        finally:
            try:
                if driver:
                    # 复位到空白页，保留工作标签页供下次复用
                    try:
                        if worker_handle and worker_handle in getattr(driver, "window_handles", []):
                            driver.switch_to.window(worker_handle)
                            try:
                                driver.get("about:blank")
                                driver.execute_script("document.title='IMAGE_SEARCH_WORKER';")
                            except Exception:
                                pass
                        if original_handle and original_handle in getattr(driver, "window_handles", []):
                            driver.switch_to.window(original_handle)
                    except Exception:
                        pass
                    # 不关闭远程 Chrome，只结束本次会话
                    self.browser_runtime.release_driver(driver)
            except Exception:
                pass

    # ---------- Internals ----------
    def _search_via_images_google(self, driver, image_path: str, wait_seconds: int) -> Tuple[List[str], List[str]]:
        logger.info("导航到 images.google.com ...")
        # 为潜在的网络阻塞设置较短的页面加载超时，超时则继续后续尝试
        try:
            driver.set_page_load_timeout(8)
        except Exception:
            pass
        try:
            driver.get("https://images.google.com/?hl=zh-CN")
        except TimeoutException:
            logger.warning("加载 images.google.com 超时，继续尝试相机入口")
            try:
                # 尝试停止继续加载，避免阻塞
                driver.execute_script("window.stop();")
            except Exception:
                pass
        time.sleep(0.5)

        # 使用短等待，若相机图标不可用则快速回退到 Lens
        wait_short = WebDriverWait(driver, 3)
        wait = WebDriverWait(driver, 6)

        # 点击相机图标（Google Lens 入口）
        logger.info("尝试点击相机图标 ...")
        lens_clicked = False
        try:
            # 先短等 3s 尝试
            try:
                lens_icon = wait_short.until(EC.element_to_be_clickable((By.CLASS_NAME, "Gdd5U")))
            except Exception:
                # 再宽松等 6s
                lens_icon = wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "Gdd5U")))
            lens_icon.click()
            lens_clicked = True
            logger.info("相机图标点击成功")
        except Exception as e:
            logger.warning("相机图标点击失败: %s", e)

        if not lens_clicked:
            # 直接切换到 Lens 上传页
            return self._search_via_lens(driver, image_path, wait_seconds)

        time.sleep(1.5)

        # 点击上传按钮（多选择器尝试）
        upload_clicked = False
        upload_selectors = [
            # 中文
            (By.XPATH, "//span[contains(normalize-space(), '上传文件')]|//button[contains(normalize-space(), '上传文件')]|//div[contains(normalize-space(), '上传文件')]"),
            # 英文常见文案
            (By.XPATH, "//span[contains(translate(normalize-space(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'upload file')]") ,
            (By.XPATH, "//span[contains(translate(normalize-space(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'upload an image')]") ,
            (By.XPATH, "//button[contains(translate(normalize-space(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'upload')]") ,
        ]
        for by, sel in upload_selectors:
            try:
                btn = wait.until(EC.element_to_be_clickable((by, sel)))
                driver.execute_script("arguments[0].click();", btn)
                upload_clicked = True
                logger.info("上传按钮点击成功: %s", sel)
                break
            except Exception as e:
                logger.debug("上传按钮尝试失败: %s (%s)", sel, e)

        if not upload_clicked:
            logger.warning("未能找到上传按钮，尝试直接使用 file input")

        # 文件上传：优先尝试 OS 文件对话框自动化，失败则找隐藏的 file input
        if not self._try_upload_via_os_dialog(image_path):
            # 直接寻找 file input 并发送路径
            try:
                inputs = driver.find_elements(By.XPATH, "//input[@type='file']")
                if inputs:
                    inputs[0].send_keys(os.path.abspath(image_path))
                    logger.info("已通过隐藏 file input 选择文件")
                else:
                    logger.error("未找到文件选择 input")
                    return [], []
            except Exception as e:
                logger.error("文件选择失败: %s", e)
                return [], []

        # 等待结果加载
        logger.info("等待搜索结果 ...")
        time.sleep(max(4, wait_seconds))

        # 可选：尝试点击“完全匹配的结果”
        try:
            span = wait.until(EC.presence_of_element_located((
                By.XPATH,
                "//span[normalize-space()='完全匹配的结果' or normalize-space()='Exact matches' or normalize-space()='Exact match']"
            )))
            try:
                link = span.find_element(By.XPATH, "./ancestor::a")
                link.click()
                time.sleep(2)
                logger.info("已点击完全匹配的结果/Exact matches")
            except Exception:
                # 有时span不在a内，尝试其父级可点击容器
                try:
                    span.click()
                    time.sleep(2)
                    logger.info("已点击匹配结果标签本身")
                except Exception:
                    logger.debug("匹配结果元素不可点击，跳过")
        except Exception:
            logger.info("未找到'完全匹配的结果/Exact matches'，继续提取")

        # 关闭可能的弹窗
        try:
            close_btn = wait.until(EC.element_to_be_clickable((
                By.XPATH,
                "//span[contains(text(), '关闭')] | //span[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'close')] | //button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'close')]"
            )))
            driver.execute_script("arguments[0].click();", close_btn)
            time.sleep(1)
            logger.info("已关闭弹窗/Close")
        except Exception:
            pass

        raw_titles, urls_g = self._extract_results_google_style(driver)
        # 同时使用通用提取作为补充
        raw_titles_extend, urls_a = self._extract_results(driver)
        # 合并链接（去重，保序），标题不做本地清洗，交由AI处理
        urls = self._merge_unique_urls(urls_g, urls_a, limit=30)
        titles = list(raw_titles) + list(raw_titles_extend)
        logger.info("images.google.com 提取完成: %s 个标题(原始), %s 个链接", len(titles), len(urls))
        return titles, urls

    def _search_via_lens(self, driver, image_path: str, wait_seconds: int) -> Tuple[List[str], List[str]]:
        lens_url = "https://lens.google.com/upload?hl=zh-CN"
        logger.info("导航到 Google Lens 上传页: %s", lens_url)
        driver.get(lens_url)

        # 等待文件输入出现并上传
        file_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
        )
        file_input.send_keys(os.path.abspath(image_path))
        logger.info("已上传图片，等待结果页加载…")

        # 等待结果加载（使用宽松判定）
        time.sleep(max(4, wait_seconds))

        titles, urls = self._extract_results(driver)
        if not titles and not urls:
            time.sleep(3)
            titles, urls = self._extract_results(driver)

        logger.info("Lens 提取完成: %s 个标题, %s 个链接", len(titles), len(urls))
        return titles, urls
    def _ensure_chrome_ready(self) -> None:
        self.browser_runtime.ensure_browser()

    def _connect_driver(self):
        return self.browser_runtime.connect_driver()

    def _get_or_create_worker_tab(self, driver) -> str:
        WORKER_TITLE = "IMAGE_SEARCH_WORKER"
        # 查找已存在
        for handle in driver.window_handles:
            try:
                driver.switch_to.window(handle)
                title = driver.execute_script("return document.title || ''") or ""
                if title == WORKER_TITLE:
                    return handle
            except Exception:
                continue
        # 创建
        driver.execute_script("window.open('about:blank','_blank');")
        handle = driver.window_handles[-1]
        driver.switch_to.window(handle)
        try:
            driver.execute_script(f"document.title='{WORKER_TITLE}';")
        except Exception:
            pass
        return handle

    def _try_upload_via_os_dialog(self, image_path: str) -> bool:
        """尝试通过系统文件对话框进行上传，依赖 pygetwindow/pyautogui/pyperclip，可选。
        成功返回 True，失败返回 False。
        """
        try:
            import pygetwindow as gw  # type: ignore
            import pyautogui  # type: ignore
            import pyperclip  # type: ignore
        except Exception:
            return False

        try:
            time.sleep(1)
            windows = gw.getWindowsWithTitle("打开") or gw.getWindowsWithTitle("Open")
            if not windows:
                return False
            win = windows[0]
            try:
                win.activate()
            except Exception:
                pass
            time.sleep(0.8)
            pyperclip.copy(os.path.abspath(image_path))
            time.sleep(0.3)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.6)
            pyautogui.press("enter")
            time.sleep(0.6)
            # 某些系统会弹二次确认
            try:
                pyautogui.press("enter")
            except Exception:
                pass
            logger.info("通过系统对话框完成文件选择")
            return True
        except Exception as e:
            logger.debug("系统对话框自动化失败: %s", e)
            return False

    def _extract_results(self, driver) -> Tuple[List[str], List[str]]:
        titles: List[str] = []
        urls: List[str] = []
        seen = set()

        def is_external(href: str) -> bool:
            if not href:
                return False
            href = href.lower()
            if href.startswith("chrome:") or href.startswith("about:"):
                return False
            # 排除 Google 自身域名
            if ".google." in href or "lens.google." in href:
                return False
            return href.startswith("http")

        # 优先尝试：带有可见文本的链接
        try:
            anchors = driver.find_elements(By.CSS_SELECTOR, "a[href]")
            for a in anchors:
                try:
                    href = a.get_attribute("href") or ""
                    text = (a.text or a.get_attribute("aria-label") or "").strip()
                    if not is_external(href):
                        continue
                    if href in seen:
                        continue
                    if not text:
                        # 尝试找最近的标题元素
                        h3 = None
                        try:
                            h3 = a.find_element(By.XPATH, "./ancestor::div[1]//h3")
                        except Exception:
                            pass
                        text = (h3.text if h3 else "").strip()
                    if not text:
                        # 最后兜底用域名
                        text = self._extract_domain(href)
                    seen.add(href)
                    titles.append(text)
                    urls.append(href)
                except Exception:
                    continue
        except Exception:
            pass

        # 去重并裁剪
        cleaned: List[Tuple[str, str]] = []
        seen_href = set()
        for t, u in zip(titles, urls):
            if not u or u in seen_href:
                continue
            seen_href.add(u)
            cleaned.append((t, u))

        titles = [t for t, _ in cleaned]
        urls = [u for _, u in cleaned]
        return titles, urls

    def _extract_results_google_style(self, driver) -> Tuple[List[str], List[str]]:
        """兼容旧版样式的提取：
        - 标题: //a/div/div[2]/div[1]
        - 对应链接: //a[.//div/div[2]/div[1]]，过滤 google 自身域名
        """
        titles: List[str] = []
        urls: List[str] = []
        try:
            elements = driver.find_elements(By.XPATH, "//a/div/div[2]/div[1]")
            titles = [el.text.strip() for el in elements if el.text.strip()]
        except Exception as e:
            logger.debug("标题提取失败: %s", e)

        try:
            anchors = driver.find_elements(By.XPATH, "//a[.//div/div[2]/div[1]]")
            for a in anchors:
                if len(urls) >= 30:
                    break
                try:
                    href = a.get_attribute("href")
                    if href and "http" in href and "google" not in href:
                        urls.append(href)
                except Exception:
                    continue
        except Exception as e:
            logger.debug("链接提取失败: %s", e)

        return titles, urls

    def _merge_unique_urls(self, primary: List[str], secondary: List[str], *, limit: int = 30) -> List[str]:
        merged: List[str] = []
        seen = set()
        for u in list(primary) + list(secondary):
            if not u or u in seen:
                continue
            seen.add(u)
            merged.append(u)
            if len(merged) >= limit:
                break
        return merged

    def _extract_full_page_text(self, driver) -> str:
        """提取当前页面上“所有可见文字”（不分种类）。
        直接使用 body.innerText，并做轻度规范化与长度裁剪。
        """
        try:
            text = driver.execute_script("return document.body && document.body.innerText || ''") or ""
        except Exception:
            text = ""
        # 轻度规范化：折叠过多空行
        import re
        text = re.sub(r"\n{3,}", "\n\n", str(text)).strip()
        MAX_CHARS = 24000
        return text[:MAX_CHARS]

    def _clean_text(self, text_list: List[str]) -> List[str]:
        stopwords = {
            "全部", "产品", "家庭作业", "外观匹配", "关于此图片", "反馈", "模糊处理",
            "滤除", "安全搜索", "关闭", "搜索结果", "无障碍功能", "帮助",
            "条款", "隐私权", "登录", "页脚链接", "跳到主要内容",
            "发送反馈", "更新位置信息", "过滤条件和主题"
        }
        seen = set()
        cleaned: List[str] = []
        for line in text_list:
            line = (line or "").strip()
            if not line or line in seen:
                continue
            if line in stopwords or any(sw in line for sw in stopwords):
                continue
            seen.add(line)
            cleaned.append(line)
        return cleaned

    def _extract_domain(self, url: str) -> str:
        try:
            import re
            m = re.search(r"https?://([^/]+)", url)
            if m:
                d = m.group(1)
                return re.sub(r"^www\\.", "", d)
            return url
        except Exception:
            return url


