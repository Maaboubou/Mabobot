from __future__ import annotations

import logging
import re
import time
import uuid
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urljoin

from app.services.shared_chrome import get_shared_chrome_operation_lock, load_shared_chrome_settings

from .models import BookCandidate, compact_text, normalize_doi, normalize_isbn


logger = logging.getLogger(__name__)
BASE_URL = "https://zh.zlib.li/"


class EbookBrowserError(RuntimeError):
    pass


class EbookBrowserService:
    def __init__(self, *, temp_root: Path, page_timeout: int = 20, download_timeout: int = 120, max_file_mb: int = 100):
        self.temp_root = Path(temp_root)
        self.page_timeout = max(5, int(page_timeout))
        self.download_timeout = max(10, int(download_timeout))
        self.max_file_bytes = max(1, int(max_file_mb)) * 1024 * 1024
        self.operation_lock = get_shared_chrome_operation_lock()

    @staticmethod
    def _float(value: Any) -> float:
        try:
            return float(str(value or "0").strip())
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def candidate_from_element(cls, element: Any) -> BookCandidate:
        from selenium.webdriver.common.by import By

        def slot_text(slot: str) -> str:
            try:
                child = element.find_element(By.CSS_SELECTOR, f"[slot='{slot}']")
                return str(
                    child.text
                    or child.get_attribute("innerText")
                    or child.get_attribute("textContent")
                    or ""
                ).strip()
            except Exception:
                return ""

        author_text = slot_text("author")
        authors = tuple(part.strip() for part in re.split(r"[;；\r\n]+", author_text) if part.strip())
        return BookCandidate(
            source_id=str(element.get_attribute("id") or element.get_attribute("href") or "").strip(),
            title=slot_text("title"),
            authors=authors,
            language=str(element.get_attribute("language") or "").strip(),
            extension=str(element.get_attribute("extension") or "").strip(),
            year=str(element.get_attribute("year") or "").strip(),
            publisher=str(element.get_attribute("publisher") or "").strip(),
            isbn=str(element.get_attribute("isbn") or "").strip(),
            doi=str(element.get_attribute("doi") or "").strip(),
            href=str(element.get_attribute("href") or "").strip(),
            download_href=str(element.get_attribute("download") or "").strip(),
            filesize=str(element.get_attribute("filesize") or "").strip(),
            quality=cls._float(element.get_attribute("quality")),
            rating=cls._float(element.get_attribute("rating")),
        )

    @staticmethod
    def _attach_driver():
        from selenium import webdriver

        settings = load_shared_chrome_settings()
        options = webdriver.ChromeOptions()
        options.debugger_address = f"127.0.0.1:{settings.debug_port}"
        driver = webdriver.Chrome(options=options)
        return driver

    @staticmethod
    def _stop_driver_service(driver: Any) -> None:
        try:
            service = getattr(driver, "service", None)
            if service:
                service.stop()
        except Exception:
            logger.debug("停止临时 ChromeDriver 服务失败", exc_info=True)

    def _open_worker_tab(self, driver: Any) -> tuple[str | None, str]:
        original = driver.current_window_handle if driver.window_handles else None
        before = set(driver.window_handles)
        driver.execute_script("window.open('about:blank', '_blank');")
        deadline = time.monotonic() + 5
        worker = ""
        while time.monotonic() < deadline:
            additions = [handle for handle in driver.window_handles if handle not in before]
            if additions:
                worker = additions[-1]
                break
            time.sleep(0.1)
        if not worker:
            raise EbookBrowserError("无法创建电子书工作标签页")
        driver.switch_to.window(worker)
        return original, worker

    @staticmethod
    def _close_worker_tab(driver: Any, original: str | None, worker: str) -> None:
        try:
            if worker in driver.window_handles:
                driver.switch_to.window(worker)
                driver.close()
        except Exception:
            logger.debug("关闭电子书工作标签页失败", exc_info=True)
        try:
            handles = driver.window_handles
            if original and original in handles:
                driver.switch_to.window(original)
            elif handles:
                driver.switch_to.window(handles[0])
        except Exception:
            logger.debug("恢复共享 Chrome 原标签页失败", exc_info=True)

    def search(
        self,
        queries: Iterable[str],
        *,
        max_per_query: int = 50,
        expected_isbn: str = "",
        expected_doi: str = "",
    ) -> list[BookCandidate]:
        from selenium.webdriver.common.by import By

        results: list[BookCandidate] = []
        seen: set[str] = set()
        with self.operation_lock:
            driver = self._attach_driver()
            original: str | None = None
            worker = ""
            try:
                driver.set_page_load_timeout(self.page_timeout)
                original, worker = self._open_worker_tab(driver)
                for query in tuple(dict.fromkeys(str(item).strip() for item in queries if str(item).strip()))[:4]:
                    driver.get(urljoin(BASE_URL, f"s/{quote(query, safe='')}"))
                    deadline = time.monotonic() + self.page_timeout
                    started = time.monotonic()
                    elements = []
                    while time.monotonic() < deadline:
                        elements = driver.find_elements(By.CSS_SELECTOR, "z-bookcard")
                        if elements:
                            break
                        try:
                            ready = driver.execute_script("return document.readyState") == "complete"
                        except Exception:
                            ready = False
                        if ready and time.monotonic() - started >= 1.5:
                            break
                        time.sleep(0.25)
                    for element in elements[: max(1, int(max_per_query))]:
                        candidate = self.candidate_from_element(element)
                        key = candidate.source_id or candidate.href or f"{candidate.title}|{candidate.authors}|{candidate.extension}"
                        if candidate.title and key not in seen:
                            seen.add(key)
                            results.append(candidate)
                if expected_isbn or expected_doi:
                    results = self._verify_expected_identifiers(
                        driver,
                        results,
                        expected_isbn=expected_isbn,
                        expected_doi=expected_doi,
                    )
            finally:
                if worker:
                    self._close_worker_tab(driver, original, worker)
                self._stop_driver_service(driver)
        return results

    def _verify_expected_identifiers(
        self,
        driver: Any,
        candidates: list[BookCandidate],
        *,
        expected_isbn: str,
        expected_doi: str,
    ) -> list[BookCandidate]:
        """Confirm an identifier from detail-page text before granting exact-match confidence."""
        from selenium.webdriver.common.by import By

        isbn = normalize_isbn(expected_isbn)
        doi = normalize_doi(expected_doi)
        expected_isbn_text = compact_text(isbn)
        expected_doi_text = compact_text(doi)
        verified: list[BookCandidate] = []
        for index, candidate in enumerate(candidates):
            if index >= 12 or not candidate.href:
                verified.extend(candidates[index:])
                break
            try:
                driver.get(urljoin(BASE_URL, candidate.href))
                body_text = str(driver.find_element(By.TAG_NAME, "body").text or "")
                compact_body = compact_text(body_text)
                verified.append(
                    replace(
                        candidate,
                        isbn=isbn if expected_isbn_text and expected_isbn_text in compact_body else candidate.isbn,
                        doi=doi if expected_doi_text and expected_doi_text in compact_body else candidate.doi,
                    )
                )
                if (expected_isbn_text and expected_isbn_text in compact_body) or (
                    expected_doi_text and expected_doi_text in compact_body
                ):
                    verified.extend(candidates[index + 1 :])
                    break
            except Exception:
                logger.debug("详情页标识符复核失败: %s", candidate.href, exc_info=True)
                verified.append(candidate)
        return verified

    def download(self, candidate: BookCandidate) -> Path:
        from selenium.webdriver.common.by import By

        destination = self.temp_root / "downloads" / uuid.uuid4().hex
        destination.mkdir(parents=True, exist_ok=True)
        with self.operation_lock:
            driver = self._attach_driver()
            original: str | None = None
            worker = ""
            try:
                driver.set_page_load_timeout(self.page_timeout)
                original, worker = self._open_worker_tab(driver)
                driver.execute_cdp_cmd(
                    "Browser.setDownloadBehavior",
                    {"behavior": "allow", "downloadPath": str(destination.resolve()), "eventsEnabled": True},
                )
                detail_url = urljoin(BASE_URL, candidate.href)
                driver.get(detail_url)
                links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/dl/']")
                download_url = ""
                for link in links:
                    href = str(link.get_attribute("href") or "")
                    if href:
                        download_url = href
                        break
                if not download_url:
                    download_url = urljoin(BASE_URL, candidate.download_href)
                if not download_url:
                    raise EbookBrowserError("详情页没有可用下载链接")
                driver.execute_script("window.location.assign(arguments[0]);", download_url)
                path = self._wait_for_download(destination)
                self._validate_file(path, candidate.normalized_format)
                return path
            finally:
                try:
                    driver.execute_cdp_cmd("Browser.setDownloadBehavior", {"behavior": "default"})
                except Exception:
                    pass
                if worker:
                    self._close_worker_tab(driver, original, worker)
                self._stop_driver_service(driver)

    def _wait_for_download(self, destination: Path) -> Path:
        deadline = time.monotonic() + self.download_timeout
        last_sizes: dict[Path, int] = {}
        stable_counts: dict[Path, int] = {}
        while time.monotonic() < deadline:
            partial = list(destination.glob("*.crdownload"))
            files = [path for path in destination.iterdir() if path.is_file() and path.suffix != ".crdownload"]
            for path in files:
                size = path.stat().st_size
                if size > self.max_file_bytes:
                    raise EbookBrowserError("下载文件超过大小上限")
                stable_counts[path] = stable_counts.get(path, 0) + 1 if last_sizes.get(path) == size else 0
                last_sizes[path] = size
                if size > 0 and stable_counts[path] >= 2 and not partial:
                    return path
            time.sleep(0.5)
        raise EbookBrowserError("电子书下载超时")

    @staticmethod
    def _validate_file(path: Path, expected_format: str) -> None:
        if not path.exists() or path.stat().st_size <= 0:
            raise EbookBrowserError("下载文件为空")
        with path.open("rb") as stream:
            head = stream.read(256)
        suffix = path.suffix.casefold().lstrip(".")
        actual = ""
        if head.startswith(b"%PDF-"):
            actual = "pdf"
        elif head.startswith(b"PK"):
            try:
                with zipfile.ZipFile(path) as archive:
                    names = set(archive.namelist())
                    if "META-INF/container.xml" in names or "mimetype" in names:
                        actual = "epub"
            except zipfile.BadZipFile:
                pass
        elif b"BOOKMOBI" in head:
            actual = "azw3" if suffix == "azw3" else "mobi"
        if not actual:
            raise EbookBrowserError("下载内容不是受支持的电子书文件")
        if expected_format and actual != expected_format and {actual, expected_format} != {"mobi", "azw3"}:
            raise EbookBrowserError("下载文件格式与搜索结果不一致")
