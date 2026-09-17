"""
中国银行外汇牌价查询服务
集成到微信机器人中 - 修复缓存和翻页问题
"""

import base64
import json
import logging
import os
import random
import re
import tempfile
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import pytz

from ddddocr import DdddOcr
# 设置 matplotlib 为非 GUI 后端，避免 tkinter 线程问题
import matplotlib
matplotlib.use('Agg')  # 使用 Agg 后端，不依赖 GUI
import matplotlib.pyplot as plt
import requests
from .net_utils import request_with_retry
from lxml import etree


from app.services.plugin_config_context import ScopedConfigAttribute
from app.utils.plugin_config import get_config


class BOCExchangeService:
    """中国银行外汇牌价查询服务"""

    # 新浪财经保留的“中行人民币牌价历史数据”币种代码。该数据源提供
    # 每日牌价，适合在中行官方历史检索增加极验后继续补齐走势图。
    SINA_HISTORY_CODES = {
        "美元": "USD", "英镑": "GBP", "欧元": "EUR", "澳门元": "MOP",
        "泰国铢": "THP", "菲律宾比索": "PHP", "港币": "HKD",
        "瑞士法郎": "CHF", "新加坡元": "SGD", "瑞典克朗": "SEK",
        "丹麦克朗": "DKK", "挪威克朗": "NOK", "日元": "JPY",
        "加拿大元": "CAD", "澳大利亚元": "AUD", "新西兰元": "NZD",
        "韩国元": "KRW",
    }

    CURRENCY_MAPPING = {
        "美元": ["美元", "美金", "USD", "dollar"], "欧元": ["欧元", "EUR", "euro"], "英镑": ["英镑", "GBP", "pound"],
        "港币": ["港币", "港元", "HKD"], "日元": ["日元", "JPY", "yen"], "瑞士法郎": ["瑞士法郎", "瑞郎", "CHF"],
        "加拿大元": ["加拿大元", "加币", "CAD"], "澳大利亚元": ["澳大利亚元", "澳元", "AUD"], "新加坡元": ["新加坡元", "新币", "SGD"],
        "新西兰元": ["新西兰元", "NZD"], "韩国元": ["韩国元", "韩元", "KRW"], "泰国铢": ["泰国铢", "泰铢", "THB"],
        "卢布": ["卢布", "俄罗斯卢布", "RUB"], "林吉特": ["林吉特", "马来西亚林吉特", "MYR"], "新台币": ["新台币", "台币", "TWD"],
        "丹麦克朗": ["丹麦克朗", "DKK"], "挪威克朗": ["挪威克朗", "NOK"], "瑞典克朗": ["瑞典克朗", "SEK"],
        "捷克克朗": ["捷克克朗", "CZK"], "匈牙利福林": ["匈牙利福林", "HUF", "福林", "匈牙利币"], "波兰兹罗提": ["波兰兹罗提", "PLN"],
        "土耳其里拉": ["土耳其里拉", "TRY"], "墨西哥比索": ["墨西哥比索", "MXN"], "巴西里亚尔": ["巴西里亚尔", "BRL"],
        "印度卢比": ["印度卢比", "INR"], "印尼卢比": ["印尼卢比", "IDR"], "菲律宾比索": ["菲律宾比索", "PHP"],
        "越南盾": ["越南盾", "VND"], "南非兰特": ["南非兰特", "ZAR"], "以色列谢克尔": ["以色列谢克尔", "ILS"],
        "沙特里亚尔": ["沙特里亚尔", "SAR"], "阿联酋迪拉姆": ["阿联酋迪拉姆", "AED"], "科威特第纳尔": ["科威特第纳尔", "KWD"],
        "卡塔尔里亚尔": ["卡塔尔里亚尔", "QAR"], "蒙古图格里克": ["蒙古图格里克", "MNT"], "柬埔寨瑞尔": ["柬埔寨瑞尔", "KHR"],
        "澳门元": ["澳门元", "澳门币", "MOP"], "文莱元": ["文莱元", "BND"], "尼泊尔卢比": ["尼泊尔卢比", "NPR"],
        "巴基斯坦卢比": ["巴基斯坦卢比", "PKR"], "塞尔维亚第纳尔": ["塞尔维亚第纳尔", "RSD"]
    }

    @ScopedConfigAttribute
    def cache_duration(self):
        return timedelta(hours=float(get_config("cache_expire_hours", 1, plugin_name="boc_rate")))

    def __init__(self, config, cache_dir, workers):
        self.config = config
        self.workers = workers
        self.logger = logging.getLogger(__name__)

        # 中行在 2026 年 7 月下线了旧的 JSP 历史查询接口，并将历史查询
        # 切换为带极验验证的新 JSON 接口。插件查询实时牌价无需绕过验证码，
        # 直接读取中行公开发布的牌价快照页（当前页 + 归档分页）即可。
        self.static_rate_url = "https://www.boc.cn/sourcedb/whpj/index.html"
        self.sina_history_url = "https://biz.finance.sina.com.cn/forex/forex.php"
        # 保留旧地址字段仅用于兼容/诊断；正常查询不再调用它们。
        self.base_url = "https://srh.bankofchina.com/search/whpj/"
        self.captcha_url = self.base_url + "CaptchaServlet.jsp"
        self.search_url = self.base_url + "search_cn.jsp"
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

        # 时区配置 - BOC数据使用北京时间
        self.beijing_tz = pytz.timezone('Asia/Shanghai')

        # 缓存配置：改为插件私有目录 app/plugins/boc_rate/exchange_rate_cache
        # 使用文件所在目录作为基准，便于统一管理与清理
        self.cache_dir = str(cache_dir or os.path.join(os.path.dirname(__file__), "exchange_rate_cache"))
        self.cache_index_file = os.path.join(self.cache_dir, "cache_index.json")
        self.memory_cache = {}
        self.cache_duration = timedelta(minutes=30)

        self._query_lock = threading.Lock()
        self._is_querying = False
        self._ocr = None
        self._chart_callback = None

        self._init_cache()

        self.logger.info("✅ 中行汇率查询服务初始化完成 (含文件缓存)")

    def _get_beijing_now(self) -> datetime:
        """获取北京时间的当前时间"""
        return datetime.now(self.beijing_tz).replace(tzinfo=None)

    def _get_beijing_today_str(self) -> str:
        """获取北京时间的今天日期字符串"""
        return self._get_beijing_now().strftime("%Y-%m-%d")

    def _parse_publish_time(self, time_str: str) -> datetime:
        """兼容新的发布时间格式（支持 . / - 分隔符）"""
        for fmt in ("%Y.%m.%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(time_str, fmt)
            except ValueError:
                continue
        raise ValueError(f"发布时间格式不支持: {time_str}")

    def _normalize_publish_time_str(self, time_str: str) -> str:
        """将发布时间标准化为 2025.12.06 10:30:00 形式"""
        dt = self._parse_publish_time(time_str)
        return dt.strftime("%Y.%m.%d %H:%M:%S")

    def _init_cache(self):
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)
            self.logger.info(f"创建缓存目录: {self.cache_dir}")

        if not os.path.exists(self.cache_index_file):
            self._save_cache_index({"currencies": {}, "version": "2.0"})

    def _get_ocr(self):
        if self._ocr is None:
            try:
                self._ocr = DdddOcr(show_ad=False)
                self.logger.info("✅ OCR识别器初始化成功")
            except Exception as e:
                self.logger.error(f"❌ OCR识别器初始化失败: {e}")
                raise
        return self._ocr

    def _fetch_static_snapshots(
        self, currency: str, start_date: str, end_date: str,
        max_pages: Optional[int] = None
    ) -> List[Dict]:
        """读取中行公开牌价快照页，并筛选币种和日期范围。

        index.html 是最新快照，index_1.html ... index_9.html 是近期快照。
        这些页面无需验证码，字段也与插件缓存格式一致。中行页面目前声明
        "共10页"；如果页数变化，会从首页文本动态识别。
        """
        headers = {"User-Agent": self.user_agent}
        first_response = request_with_retry(
            "get", self.static_rate_url, logger=self.logger,
            headers=headers, timeout=15
        )
        first_response.raise_for_status()
        first_response.encoding = first_response.apparent_encoding or "utf-8"

        page_count_match = re.search(r"共\s*(?:<[^>]+>\s*)*(\d+)", first_response.text)
        if not page_count_match:
            page_count_match = re.search(r"createPageHTML\s*\(\s*(\d+)", first_response.text)
        page_count = int(page_count_match.group(1)) if page_count_match else 1

        page_count = max(1, min(page_count, 20))
        if max_pages is not None:
            page_count = min(page_count, max(1, max_pages))

        responses = [first_response]
        base_dir = self.static_rate_url.rsplit("/", 1)[0]

        def fetch_archive_page(page_index):
            page_url = f"{base_dir}/index_{page_index}.html"
            response = request_with_retry(
                "get", page_url, logger=self.logger,
                headers=headers, timeout=15
            )
            response.raise_for_status()
            response.encoding = response.apparent_encoding or "utf-8"
            return response

        # 归档页彼此独立，并发读取可避免 10 个页面串行导致首次查询变慢。
        if page_count > 1:
            with ThreadPoolExecutor(max_workers=min(6, page_count - 1)) as executor:
                futures = [executor.submit(fetch_archive_page, i) for i in range(1, page_count)]
                for future in as_completed(futures):
                    responses.append(future.result())

        start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
        aliases = {"巴西里亚尔": "巴西雷亚尔"}
        site_currency = aliases.get(currency, currency)
        records = []
        seen = set()

        for response in responses:
            html = etree.HTML(response.text)
            if html is None:
                continue
            for row in html.xpath("//table//tr"):
                cells = ["".join(cell.itertext()).strip() for cell in row.xpath("./td")]
                if len(cells) < 8 or cells[0] != site_currency:
                    continue
                try:
                    publish_time = self._normalize_publish_time_str(cells[6])
                    publish_dt = self._parse_publish_time(publish_time)
                except ValueError:
                    continue
                if not (start_dt <= publish_dt.date() <= end_dt):
                    continue
                key = (site_currency, publish_time)
                if key in seen:
                    continue
                seen.add(key)
                records.append({
                    "货币名称": currency,
                    "现汇买入价": cells[1],
                    "现钞买入价": cells[2],
                    "现汇卖出价": cells[3],
                    "现钞卖出价": cells[4],
                    "中行折算价": cells[5],
                    "发布时间": publish_time,
                })

        records.sort(key=lambda item: self._parse_publish_time(item["发布时间"]), reverse=True)
        self.logger.info(
            f"✅ 从中行公开快照页获取 {currency} 数据 {len(records)} 条 "
            f"({start_date} 至 {end_date})"
        )
        return records

    def _fetch_sina_history(self, currency: str, start_date: str, end_date: str) -> List[Dict]:
        """获取新浪财经保存的中行每日历史牌价。

        新浪页面的“中行钞卖价/汇卖价”为合并字段；本插件走势图只使用
        现汇卖出价，因此将该值写入现汇/现钞卖出价两个兼容字段。
        """
        money_code = self.SINA_HISTORY_CODES.get(currency)
        if not money_code:
            self.logger.info(f"新浪中行历史数据源暂不支持币种: {currency}")
            return []

        params = {
            "startdate": start_date,
            "enddate": end_date,
            "money_code": money_code,
            "type": "0",
            "page": "1",
            "call_type": "ajax",
        }
        headers = {
            "User-Agent": self.user_agent,
            "Referer": "https://finance.sina.com.cn/",
        }

        first_response = request_with_retry(
            "get", self.sina_history_url, logger=self.logger,
            headers=headers, params=params, timeout=15
        )
        first_response.raise_for_status()
        first_response.encoding = "gbk"
        first_html = etree.HTML(first_response.text)
        if first_html is None:
            return []

        page_numbers = []
        for text in first_html.xpath('//a[contains(concat(" ", @class, " "), " page ")]/text()'):
            text = text.strip()
            if text.isdigit():
                page_numbers.append(int(text))
        page_count = max(page_numbers, default=1)
        page_count = max(1, min(page_count, 20))

        responses = [first_response]

        def fetch_page(page):
            page_params = dict(params)
            page_params["page"] = str(page)
            response = request_with_retry(
                "get", self.sina_history_url, logger=self.logger,
                headers=headers, params=page_params, timeout=15
            )
            response.raise_for_status()
            response.encoding = "gbk"
            return response

        if page_count > 1:
            with ThreadPoolExecutor(max_workers=min(4, page_count - 1)) as executor:
                futures = [executor.submit(fetch_page, page) for page in range(2, page_count + 1)]
                for future in as_completed(futures):
                    responses.append(future.result())

        records = []
        seen_dates = set()
        for response in responses:
            html = etree.HTML(response.text)
            if html is None:
                continue
            for row in html.xpath('//table[contains(concat(" ", @class, " "), " list_table ")]//tr'):
                cells = ["".join(cell.itertext()).strip() for cell in row.xpath("./td")]
                if len(cells) < 6 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[0]):
                    continue
                if cells[0] in seen_dates:
                    continue
                seen_dates.add(cells[0])
                sell_rate = "" if cells[3] == "--" else cells[3]
                records.append({
                    "货币名称": currency,
                    "现汇买入价": "" if cells[1] == "--" else cells[1],
                    "现钞买入价": "" if cells[2] == "--" else cells[2],
                    "现汇卖出价": sell_rate,
                    "现钞卖出价": sell_rate,
                    "中行折算价": "" if cells[5] == "--" else cells[5],
                    "发布时间": f"{cells[0].replace('-', '.')} 00:00:00",
                })

        records.sort(key=lambda item: self._parse_publish_time(item["发布时间"]), reverse=True)
        self.logger.info(
            f"✅ 从新浪中行历史牌价获取 {currency} 每日数据 {len(records)} 条 "
            f"({start_date} 至 {end_date})"
        )
        return records

    def _fetch_history_data(self, currency: str, start_date: str, end_date: str) -> List[Dict]:
        """合并每日历史牌价与中行官网近期盘中快照。"""
        daily_records = self._fetch_sina_history(currency, start_date, end_date)
        snapshot_records = self._fetch_static_snapshots(currency, start_date, end_date)
        records = daily_records + snapshot_records
        seen = set()
        unique_records = []
        for item in records:
            key = (item["货币名称"], item["发布时间"])
            if key not in seen:
                seen.add(key)
                unique_records.append(item)
        unique_records.sort(
            key=lambda item: self._parse_publish_time(item["发布时间"]), reverse=True
        )
        return unique_records

    def _fetch_first_page_with_retry(self, currency: str, start_date: str, end_date: str) -> Tuple[Optional[str], int, List[Dict]]:
        """获取公开快照数据，保持旧调用方所需的返回结构。"""
        try:
            content = self._fetch_history_data(currency, start_date, end_date)
            # 两个新数据源均已在此方法内完成翻页和合并；调用方不得再走
            # 旧 JSP 的 paramtk 翻页流程。
            return "history-complete", len(content), content
        except Exception as e:
            raise Exception(f"获取第一页数据失败: {e}") from e

    def _normalize_currency(self, user_input: str) -> Optional[str]:
        user_input = user_input.strip().upper()
        for official_name, aliases in self.CURRENCY_MAPPING.items():
            if user_input == official_name.upper() or user_input in [a.upper() for a in aliases]:
                return official_name
        return None

    def _extract_currency_from_message(self, message: str) -> str:
        cleaned_message = re.sub(r'@\S+', '', message).strip()
        for official_name, aliases in self.CURRENCY_MAPPING.items():
            for alias in aliases:
                if alias in cleaned_message:
                    return official_name
        return "欧元"

    # === 🔥 新增：文件缓存核心逻辑 (从test.py移植并优化) ===

    def _get_cache_filename(self, currency: str, date_str: str) -> str:
        """生成缓存文件名"""
        safe_currency = currency.replace("/", "_").replace("\\", "_")
        return os.path.join(self.cache_dir, f"{safe_currency}_{date_str}.json")

    def _load_cached_data(self, currency: str, start_date: str, end_date: str) -> Tuple[List[Dict], set, Optional[datetime]]:
        """从文件缓存加载数据，返回数据、日期集合和今日最新时间戳"""
        cached_data, cached_dates = [], set()
        today_latest_time = None
        today_str = self._get_beijing_today_str()  # 使用北京时间的今天

        current_date = datetime.strptime(start_date, "%Y-%m-%d")
        end_datetime = datetime.strptime(end_date, "%Y-%m-%d")

        while current_date <= end_datetime:
            date_str = current_date.strftime("%Y-%m-%d")
            cache_file = self._get_cache_filename(currency, date_str)
            if os.path.exists(cache_file):
                try:
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        cache_content = json.load(f)
                    records = cache_content.get("records", cache_content)
                    cached_data.extend(records)
                    cached_dates.add(date_str)

                    # 如果是今天的数据，找出最新时间戳（BOC数据本身就是北京时间）
                    if date_str == today_str and records:
                        for record in records:
                            try:
                                record_time = self._parse_publish_time(record["发布时间"])
                                if today_latest_time is None or record_time > today_latest_time:
                                    today_latest_time = record_time
                            except Exception as e:
                                self.logger.warning(f"解析时间戳失败: {record.get('发布时间')}, error: {e}")

                except Exception as e:
                    self.logger.warning(f"加载缓存文件失败 {cache_file}: {e}")
            current_date += timedelta(days=1)

        self.logger.info(f"从文件缓存加载 {currency} 数据: {len(cached_data)} 条, 覆盖 {len(cached_dates)} 天")
        if today_latest_time:
            self.logger.info(f"今日最新缓存数据时间(北京时间): {today_latest_time.strftime('%Y-%m-%d %H:%M:%S')}")

        return cached_data, cached_dates, today_latest_time

    def _get_missing_date_ranges(self, start_date: str, end_date: str, cached_dates: set) -> List[Tuple[str, str]]:
        """获取缺失的日期范围"""
        missing_ranges = []
        current_date = datetime.strptime(start_date, "%Y-%m-%d")
        end_datetime = datetime.strptime(end_date, "%Y-%m-%d")

        range_start = None
        while current_date <= end_datetime:
            date_str = current_date.strftime("%Y-%m-%d")

            if date_str not in cached_dates:
                # 开始一个新的缺失范围
                if range_start is None:
                    range_start = date_str
            else:
                # 结束当前缺失范围
                if range_start is not None:
                    missing_ranges.append((range_start, (current_date - timedelta(days=1)).strftime("%Y-%m-%d")))
                    range_start = None

            current_date += timedelta(days=1)

        # 处理最后一个范围
        if range_start is not None:
            missing_ranges.append((range_start, end_date))

        self.logger.info(f"发现 {len(missing_ranges)} 个缺失日期范围: {missing_ranges}")
        return missing_ranges

    def _should_update_today_data(self, today_latest_time: Optional[datetime], min_interval_minutes: int = 10) -> bool:
        """检查是否应该更新今天的数据（基于北京时间）"""
        if today_latest_time is None:
            self.logger.info("今日没有缓存数据，需要获取")
            return True

        # 计算距离最新缓存数据的时间间隔（使用北京时间）
        beijing_now = self._get_beijing_now()
        time_diff = beijing_now - today_latest_time
        minutes_diff = time_diff.total_seconds() / 60

        if minutes_diff >= min_interval_minutes:
            self.logger.info(f"今日最新缓存数据时间(北京): {today_latest_time.strftime('%H:%M:%S')}, "
                           f"距离北京时间现在 {minutes_diff:.1f} 分钟，需要更新")
            return True
        else:
            self.logger.info(f"今日最新缓存数据时间(北京): {today_latest_time.strftime('%H:%M:%S')}, "
                           f"距离北京时间现在仅 {minutes_diff:.1f} 分钟，暂不更新")
            return False


    def _fetch_missing_data_sync(self, currency: str, missing_ranges: List[Tuple[str, str]]) -> List[Dict]:
        """增量获取缺失日期的数据"""
        if not missing_ranges:
            self.logger.info("没有缺失的日期范围,无需获取新数据")
            return []

        # 公开快照页一次即可覆盖其保留的全部近期数据，无需按天重复抓取。
        range_start = min(item[0] for item in missing_ranges)
        range_end = max(item[1] for item in missing_ranges)
        try:
            new_data = self._fetch_history_data(currency, range_start, range_end)
            self.logger.info(f"增量获取完成,共获得新数据: {len(new_data)} 条")
            return new_data
        except Exception as e:
            self.logger.error(f"获取缺失范围 {range_start} 到 {range_end} 失败: {e}")
            return []


    def _save_data_to_cache(self, currency: str, data: List[Dict]):
        """保存数据到文件缓存"""
        if not data: return

        daily_data = {}
        for item in data:
            try:
                normalized_time = self._normalize_publish_time_str(item["发布时间"])
                date = self._parse_publish_time(normalized_time).strftime("%Y-%m-%d")
                normalized_item = dict(item)
                normalized_item["发布时间"] = normalized_time
                if date not in daily_data:
                    daily_data[date] = []
                daily_data[date].append(normalized_item)
            except Exception as e:
                self.logger.warning(f"解析时间失败，跳过缓存: {item.get('发布时间')}, error: {e}")

        for date, records in daily_data.items():
            cache_file = self._get_cache_filename(currency, date)
            cache_data = {
                "metadata": {
                    "currency": currency, "date": date, "record_count": len(records),
                    "cached_at": self._get_beijing_now().isoformat(), "data_version": "2.0"
                },
                "records": records
            }
            try:
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(cache_data, f, ensure_ascii=False, indent=2)
                self.logger.info(f"保存文件缓存 {currency} {date}: {len(records)} 条")
            except Exception as e:
                self.logger.error(f"保存文件缓存失败 {cache_file}: {e}")

    def query_exchange_rate(self, message: str, chat_name: str) -> Tuple[str, Optional[str]]:
        currency = self._extract_currency_from_message(message)
        normalized_currency = self._normalize_currency(currency)
        if not normalized_currency:
            return f"❌ 不支持的币种: {currency}", None

        self.logger.info(f"🔍 查询汇率: {normalized_currency}")
        self.logger.info(f"🕐 当前北京时间: {self._get_beijing_now().strftime('%Y-%m-%d %H:%M:%S')}")

        # 1. 检查内存缓存
        cache_key = f"exchange_rate_{normalized_currency}"
        if cache_key in self.memory_cache and (self._get_beijing_now() - self.memory_cache[cache_key]['timestamp'] < self.cache_duration):
            cached_item = self.memory_cache[cache_key]['data']
            self.logger.info(f"✅ 使用内存缓存数据: {normalized_currency}")
            text_reply = f"💰 {normalized_currency}中行牌价(现汇卖出价)\n📈 {cached_item['latest_rate']}\n🕐 {cached_item['latest_time']}\n💡 (内存缓存)"
            return text_reply, None

        # 2. 检查文件缓存（使用北京时间）
        beijing_now = self._get_beijing_now()
        end_date_str = beijing_now.strftime("%Y-%m-%d")
        start_date_str = (beijing_now - timedelta(days=30)).strftime("%Y-%m-%d")

        all_contents, cached_dates, today_latest_time = self._load_cached_data(normalized_currency, start_date_str, end_date_str)

        # 检查是否需要更新今天的数据
        need_update_today = self._should_update_today_data(today_latest_time, min_interval_minutes=10)

        # 如果有缓存数据且今天不需要更新，则使用缓存
        if all_contents and not need_update_today:
            self.logger.info(f"✅ 使用文件缓存数据: {normalized_currency}")
            sorted_data = sorted(all_contents, key=lambda x: self._parse_publish_time(x["发布时间"]), reverse=True)
            latest_item = sorted_data[0]
            latest_rate = float(latest_item["现汇卖出价"])
            latest_time = latest_item["发布时间"]

            text_reply = f"💰 {normalized_currency}中行牌价(现汇卖出价)\n📈 {latest_rate}\n🕐 {latest_time}\n💡 (文件缓存)"

            # 异步生成图表
            self.workers.start(
                f"chart-cache-{normalized_currency}-{time.time_ns()}",
                self._generate_chart_from_cache,
                args=(all_contents, normalized_currency, chat_name),
            )
            return text_reply, None

        # 3. 快速响应优化：先获取第一页最新汇率，后台完成完整数据获取
        with self._query_lock:
            if self._is_querying:
                return "⏳ 汇率查询正在进行中，请稍后再试", None
            self._is_querying = True

        try:
            # 计算缺失的日期范围
            missing_ranges = self._get_missing_date_ranges(start_date_str, end_date_str, cached_dates)

            # 如果需要更新今天的数据，将今天加入到需要更新的范围（使用北京时间）
            today_str = self._get_beijing_today_str()
            if need_update_today and today_str not in [r[1] for r in missing_ranges]:
                # 如果今天不在缺失范围内，单独添加今天进行更新
                if today_str in cached_dates:
                    self.logger.info(f"今天数据需要更新，添加到更新范围: {today_str}")
                    missing_ranges.append((today_str, today_str))

            if missing_ranges:
                self.logger.info(f"需要更新 {normalized_currency} 数据，先获取最新汇率")

                # 🚀 优化：先快速获取第一页最新数据，立即返回给用户
                latest_data = self._get_latest_rate_sync(normalized_currency)
                if not latest_data['success']:
                    return f"❌ {latest_data['error']}", None

                # 立即返回最新汇率
                text_reply = f"💰 {normalized_currency}中行牌价(现汇卖出价)\n📈 {latest_data['latest_rate']}\n🕐 {latest_data['latest_time']}"

                # 🔑 关键修复：立即释放查询锁，让主线程可以立即返回
                with self._query_lock:
                    self._is_querying = False

                # 🔄 后台完成完整数据更新并生成完整走势图
                self.workers.start(
                    f"update-chart-{normalized_currency}-{time.time_ns()}",
                    self._background_complete_update_and_chart,
                    args=(normalized_currency, missing_ranges, all_contents, need_update_today, today_str, cached_dates, chat_name, latest_data),
                )

                return text_reply, None
            else:
                self.logger.info(f"所有日期都已缓存，无需增量更新")

            # 如果仍然没有数据，尝试获取最新数据
            if not all_contents:
                self.logger.info(f"缓存为空，获取最新汇率数据")
                latest_data = self._get_latest_rate_sync(normalized_currency)
                if not latest_data['success']:
                    return f"❌ {latest_data['error']}", None

                text_reply = f"💰 {normalized_currency}中行牌价(现汇卖出价)\n📈 {latest_data['latest_rate']}\n🕐 {latest_data['latest_time']}"

                # 启动后台完整数据获取
                self.workers.start(
                    f"full-chart-{normalized_currency}-{time.time_ns()}",
                    self._generate_full_chart_background,
                    args=(normalized_currency, latest_data, chat_name),
                )

                return text_reply, None

            # 使用现有缓存数据生成回复和图表
            sorted_data = sorted(all_contents, key=lambda x: self._parse_publish_time(x["发布时间"]), reverse=True)
            latest_item = sorted_data[0]
            latest_rate = float(latest_item["现汇卖出价"])
            latest_time = latest_item["发布时间"]

            text_reply = f"💰 {normalized_currency}中行牌价(现汇卖出价)\n📈 {latest_rate}\n🕐 {latest_time}"

            # 更新内存缓存
            cache_data = {
                'latest_rate': latest_rate, 'latest_time': latest_time,
                'all_data': all_contents
            }
            self.memory_cache[f"exchange_rate_{normalized_currency}"] = {'data': cache_data, 'timestamp': self._get_beijing_now()}

            # 异步生成图表
            self.workers.start(
                f"chart-{normalized_currency}-{time.time_ns()}",
                self._generate_chart_from_cache,
                args=(all_contents, normalized_currency, chat_name),
            )

            return text_reply, None

        except Exception as e:
            self.logger.error(f"❌ 查询汇率异常: {e}")
            return "❌ 查询失败，请稍后重试", None
        finally:
            with self._query_lock:
                self._is_querying = False

    def _background_complete_update_and_chart(self, currency: str, missing_ranges: List[Tuple[str, str]],
                                            existing_contents: List[Dict], need_update_today: bool,
                                            today_str: str, cached_dates: set, chat_name: str, latest_data: Dict):
        """后台完成完整数据更新并生成发送完整走势图"""
        try:
            self.logger.info(f"🔄 开始后台完整更新 {currency} 数据")

            # 获取缺失的数据
            new_data = self._fetch_missing_data_sync(currency, missing_ranges)

            # 合并数据
            all_contents = existing_contents.copy()

            if new_data:
                # 如果包含今天的更新，需要先清除今天的旧缓存
                if need_update_today and today_str in cached_dates:
                    # 从现有数据中移除今天的旧数据
                    filtered_contents = []
                    for item in all_contents:
                        try:
                            if self._parse_publish_time(item["发布时间"]).strftime("%Y-%m-%d") != today_str:
                                filtered_contents.append(item)
                        except Exception as e:
                            self.logger.warning(f"跳过无法解析的发布时间记录: {item.get('发布时间')}, error: {e}")
                    all_contents = filtered_contents
                    self.logger.info(f"清除今日旧缓存数据，剩余 {len(all_contents)} 条")

                # 保存新数据到缓存
                self._save_data_to_cache(currency, new_data)
                # 合并缓存数据和新数据
                all_contents.extend(new_data)
                self.logger.info(f"后台增量更新完成，新增 {len(new_data)} 条数据，总计 {len(all_contents)} 条")
            else:
                self.logger.warning(f"后台增量获取失败，使用现有缓存数据")
                # 如果没有新数据但有现有缓存，则使用现有缓存
                if not all_contents and latest_data.get('first_page_data'):
                    all_contents = latest_data['first_page_data']

            # 确保有数据用于生成图表
            if all_contents:
                # 更新内存缓存
                sorted_data = sorted(all_contents, key=lambda x: self._parse_publish_time(x["发布时间"]), reverse=True)
                cache_data = {
                    'latest_rate': float(sorted_data[0]["现汇卖出价"]),
                    'latest_time': sorted_data[0]["发布时间"],
                    'all_data': all_contents
                }
                self.memory_cache[f"exchange_rate_{currency}"] = {
                    'data': cache_data,
                    'timestamp': self._get_beijing_now()
                }

                # 生成并发送完整走势图（基于完整历史数据）
                self._generate_chart_from_cache(all_contents, currency, chat_name)
            else:
                self.logger.warning(f"❌ 后台更新后仍无数据，无法生成 {currency} 走势图")

        except Exception as e:
            self.logger.error(f"❌ 后台完整更新失败: {e}")

    def _generate_chart_from_cache(self, cached_data: List[Dict], currency: str, chat_name: str):
        """从缓存数据生成并发送图表"""
        try:
            self.logger.info(f"📊 从缓存数据生成 {currency} 走势图")
            chart_path = self._create_trend_chart(cached_data, currency)
            if chart_path and self._chart_callback:
                self._chart_callback(chart_path, currency)
        except Exception as e:
            self.logger.error(f"❌ 从缓存生成图表失败: {e}")



    def _get_latest_rate_sync(self, currency: str) -> Dict:
        """同步获取最新汇率（仅第一页）"""
        beijing_now = self._get_beijing_now()
        end_date = beijing_now.strftime("%Y-%m-%d")

        # 30 days history date for chart/background processing
        history_start_date = (beijing_now - timedelta(days=30)).strftime("%Y-%m-%d")

        # Use TODAY as start_date for the query to ensure we get the latest rate
        # The API seems to return oldest data first if searched with a range
        query_start_date = end_date

        try:
            # 快速回复只读取官网当前页；归档快照由后台更新流程再并发抓取。
            first_page_data = self._fetch_static_snapshots(
                currency, query_start_date, end_date, max_pages=1
            )
            if not first_page_data:
                return {'success': False, 'error': '未获取到汇率数据'}
            sorted_data = sorted(first_page_data, key=lambda x: self._parse_publish_time(x["发布时间"]), reverse=True)
            latest_item = sorted_data[0]
            return {
                'success': True, 'latest_rate': float(latest_item["现汇卖出价"]), 'latest_time': latest_item["发布时间"],
                'first_page_data': first_page_data,
                'start_date': history_start_date, # Return 30-day history start date for chart logic
                'end_date': end_date
            }
        except Exception as e:
            self.logger.error(f"❌ 快速获取最新汇率失败: {e}")
            return {'success': False, 'error': f'获取数据失败: {str(e)}'}

    def _fetch_first_page_sync(self, currency: str, start_date: str, end_date: str) -> List[Dict]:
        """同步获取第一页数据（简化版）"""
        try:
            _, _, content = self._fetch_first_page_with_retry(currency, start_date, end_date)
            self.logger.info(f"✅ 快速获取 {currency} 第1页成功，总记录数: {len(content)}")
            return content
        except Exception as e:
            self.logger.error(f"❌ 快速获取第一页失败: {e}")
            raise e

    def _generate_full_chart_background(self, currency: str, latest_data: Dict, chat_name: str):
        """后台线程生成完整图表（仅在没有缓存时使用）"""
        try:
            self.logger.info(f"📊 开始后台生成{currency}完整走势图")

            # 先检查是否有缓存数据，实现智能合并
            start_date_str = latest_data['start_date']
            end_date_str = latest_data['end_date']
            cached_data, cached_dates, today_latest_time = self._load_cached_data(currency, start_date_str, end_date_str)

            if cached_data:
                self.logger.info(f"发现现有缓存数据: {len(cached_data)} 条")
                # 计算需要补充的日期范围
                missing_ranges = self._get_missing_date_ranges(start_date_str, end_date_str, cached_dates)

                if missing_ranges:
                    # 只获取缺失的数据
                    new_data = self._fetch_missing_data_sync(currency, missing_ranges)
                    if new_data:
                        self._save_data_to_cache(currency, new_data)
                        cached_data.extend(new_data)
                        self.logger.info(f"增量更新完成，总数据量: {len(cached_data)} 条")

                all_contents = cached_data
            else:
                # 没有缓存，获取完整数据
                all_contents = self._fetch_complete_data_sync(currency, start_date_str, end_date_str, latest_data['first_page_data'])
                if all_contents:
                    self._save_data_to_cache(currency, all_contents)

            if not all_contents:
                self.logger.error(f"❌ 未获取到完整数据，无法生成{currency}走势图")
                return

            chart_path = self._create_trend_chart(all_contents, currency)
            if chart_path:
                sorted_data = sorted(all_contents, key=lambda x: self._parse_publish_time(x["发布时间"]), reverse=True)
                cache_data = {
                    'latest_rate': float(sorted_data[0]["现汇卖出价"]), 'latest_time': sorted_data[0]["发布时间"],
                    'chart_path': chart_path, 'all_data': all_contents
                }
                # 更新内存缓存
                self.memory_cache[f"exchange_rate_{currency}"] = {'data': cache_data, 'timestamp': self._get_beijing_now()}

                self.logger.info(f"✅ {currency}完整走势图生成完成: {chart_path}")
                # 注意：图表已经在前面发送过了，这里不重复发送
        except Exception as e:
            self.logger.error(f"❌ 后台生成{currency}图表失败: {e}")

    def _fetch_complete_data_sync(self, currency: str, start_date: str, end_date: str, first_page_data: List[Dict]) -> List[Dict]:
        """同步获取完整数据（简化版）"""
        all_contents = first_page_data.copy()
        try:
            paramtk, record_count, first_page_content = self._fetch_first_page_with_retry(currency, start_date, end_date)

            if first_page_content:
                all_contents = first_page_content

            if record_count > 20 and paramtk != "history-complete":
                total_pages = (record_count + 19) // 20
                self.logger.info(f"📊 需要获取总计{total_pages}页数据")

                # 获取token和captcha_str用于翻页
                ocr = self._get_ocr()
                token = self._get_captcha_sync()
                captcha_str = self._get_captcha_char_sync(ocr)

                for page in range(2, total_pages + 1):
                    time.sleep(1.5)

                    # --- 建议修改：为每页添加重试机制 ---
                    page_content, new_paramtk = None, None
                    for page_attempt in range(2): # 每页最多重试1次
                        error, new_paramtk, _, page_content = self._query_data_sync(start_date, end_date, token, captcha_str, paramtk, page, False, currency)
                        if not error:
                            break # 成功则跳出重试
                        self.logger.warning(f"⚠️ 第 {page} 页获取失败 (尝试 {page_attempt+1}/2): {error}")
                        if "访问" in error or "频率" in error:
                            self.logger.info(f"🛑 遇到访问限制，停止翻页")
                            break # 遇到访问限制，跳出重试
                        time.sleep(3) # 其他错误，等待3秒后重试
                    # --- 结束修改 ---

                    if error:
                        self.logger.warning(f"⚠️ 第{page}页获取失败: {error}")
                        if "访问" in error or "频率" in error:
                            self.logger.info(f"🛑 遇到访问限制，停止翻页")
                            break
                        continue
                    else:
                        if page_content: all_contents.extend(page_content)
                        if new_paramtk: paramtk = new_paramtk
                    if page % 3 == 0: time.sleep(3)

            self.logger.info(f"✅ {currency}完整数据获取完成，共{len(all_contents)}条")
            return all_contents
        except Exception as e:
            self.logger.error(f"❌ 获取完整数据异常: {e}")
            return all_contents

    def _get_captcha_sync(self) -> str:
        response = request_with_retry("get", self.captcha_url, logger=self.logger, timeout=10)
        response.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
            f.write(base64.b64decode(response.content))
            self.temp_captcha_file = f.name
        return response.headers.get("token")

    def _get_captcha_char_sync(self, ocr) -> str:
        with open(self.temp_captcha_file, "rb") as f:
            image = f.read()
        result = ocr.classification(image)
        try: os.unlink(self.temp_captcha_file)
        except: pass
        self.logger.info(f"🔐 验证码识别: {result}")
        return result

    def _query_data_sync(self, start_date, end_date, token, captcha_char, paramtk, page, is_first, currency):
        headers = {"User-Agent": self.user_agent, "Content-Type": "application/x-www-form-urlencoded"}
        if is_first:
            data = {
                "searchDate": start_date,  # Updated from erectDate
                # "nothing": end_date,      # Removed based on observation
                "pjname": currency,
                "head": "head_620.js",
                "bottom": "bottom_591.js",
                "first": 1,
                "token": token,
                "captcha": captcha_char
            }
        else:
            data = {
                "searchDate": start_date,  # Updated from erectDate
                # "nothing": end_date,      # Removed
                "page": page,
                "pjname": currency,
                "head": "head_620.js",
                "bottom": "bottom_591.js",
                "paramtk": paramtk,
                "token": token
            }

        response = request_with_retry("post", self.search_url, logger=self.logger, headers=headers, data=data, timeout=15)
        response.raise_for_status()
        html_content = response.text.replace("GBK", "UTF-8").replace("\n", "").replace("\r", "").replace("\t", "")

        if "验证码错误" in html_content: return "验证码错误", None, 0, []
        if "验证码已过期" in html_content: return "验证码已过期", None, 0, []

        # Use XPath to extract paramtk for better robustness
        try:
            tree = etree.HTML(html_content)
            new_paramtk = tree.xpath('//input[@name="paramtk"]/@value')[0]
        except Exception:
            new_paramtk = None

        # if not new_paramtk:
        #    self.logger.warning(f"⚠️ paramtk not found in response")

        record_count = int((re.findall(r"m_nRecordCount\s*=\s*(\d+);", html_content) or [0])[0])
        content = self._parse_html(html_content)
        return None, new_paramtk, record_count, content

    def _parse_html(self, html_content: str) -> List[Dict]:
        html = etree.HTML(html_content)
        data = []
        for row in html.xpath("//div[@class='BOC_main publish']//table//tr"):
            if row.xpath("./th"): continue
            try:
                publish_time_raw = row.xpath("./td[7]/text()")[0].strip()
                try:
                    publish_time = self._normalize_publish_time_str(publish_time_raw)
                except Exception as e:
                    self.logger.warning(f"发布时间格式异常，使用原始值: {publish_time_raw}, error: {e}")
                    publish_time = publish_time_raw
                item = {
                    "货币名称": row.xpath("./td[1]/text()")[0].strip(),
                    "现汇买入价": row.xpath("./td[2]/text()")[0].strip(),
                    "现钞买入价": row.xpath("./td[3]/text()")[0].strip(),
                    "现汇卖出价": row.xpath("./td[4]/text()")[0].strip(),
                    "现钞卖出价": row.xpath("./td[5]/text()")[0].strip(),
                    "中行折算价": row.xpath("./td[6]/text()")[0].strip(),
                    "发布时间": publish_time,
                }
                data.append(item)
            except IndexError: continue
        return data

    def _create_trend_chart(self, all_contents: List[Dict], currency: str) -> Optional[str]:
        if not all_contents: return None
        dates, rates = [], []
        for item in all_contents:
            try:
                dates.append(self._parse_publish_time(item["发布时间"]))
                rates.append(float(item["现汇卖出价"]))
            except: continue

        if not dates: return None

        try:
            # 确保在非主线程中使用 matplotlib 时的安全性
            plt.ioff()  # 关闭交互模式

            sorted_data = sorted(zip(dates, rates))
            dates, rates = zip(*sorted_data)

            min_rate, max_rate, avg_rate = min(rates), max(rates), sum(rates) / len(rates)
            rate_range = max_rate - min_rate
            min_idx, max_idx = rates.index(min_rate), rates.index(max_rate)
            min_time, max_time = dates[min_idx], dates[max_idx]
            latest_time, latest_rate = dates[-1], rates[-1]

            plt.style.use('default')
            plt.rcParams.update({
                'font.sans-serif': ['Microsoft YaHei', 'SimHei', 'Arial Unicode MS'], 'axes.unicode_minus': False,
                'figure.facecolor': 'white', 'axes.facecolor': 'white', 'savefig.facecolor': 'white',
                'savefig.edgecolor': 'none', 'font.size': 12, 'axes.linewidth': 1.2, 'grid.linewidth': 0.5, 'grid.alpha': 0.4
            })

            fig, ax = plt.subplots(figsize=(20, 12), dpi=150)
            ax.plot(dates, rates, linewidth=2, alpha=0.9, color='#2E86AB', zorder=3, label='现汇卖出价')
            ax.fill_between(dates, rates, min_rate - rate_range * 0.02, alpha=0.15, color='#2E86AB', zorder=1)
            ax.axhline(y=avg_rate, color='#F18F01', linestyle='--', linewidth=2.5, alpha=0.8, zorder=2, label=f'平均值: {avg_rate:.2f}')

            bbox_high = dict(boxstyle="round,pad=0.5", fc='#FFE5E5', ec='#E74C3C', lw=2, alpha=0.95)
            ax.annotate(f'最高点\n{max_rate:.2f}\n{max_time:%m-%d %H:%M}', (max_time, max_rate),
                        xytext=(max_time, max_rate + rate_range * 0.1), ha='center', va='bottom',
                        fontsize=12, fontweight='bold', bbox=bbox_high, color='#C0392B',
                        arrowprops=dict(arrowstyle='->', color='#E74C3C', lw=2), zorder=5)

            bbox_low = dict(boxstyle="round,pad=0.5", fc='#E8F5E8', ec='#27AE60', lw=2, alpha=0.95)
            ax.annotate(f'最低点\n{min_rate:.2f}\n{min_time:%m-%d %H:%M}', (min_time, min_rate),
                        xytext=(min_time, min_rate - rate_range * 0.1), ha='center', va='top',
                        fontsize=12, fontweight='bold', bbox=bbox_low, color='#1E8449',
                        arrowprops=dict(arrowstyle='->', color='#27AE60', lw=2), zorder=5)

            # 最新点标注（放在数据点右边，箭头朝左指向数据点）
            bbox_latest = dict(boxstyle="round,pad=0.5", fc='#E8F0FE', ec='#1F6FEB', lw=2, alpha=0.95)
            # 计算标注位置：在数据点右边，水平偏移参考最高点最低点的距离设置
            time_span = dates[-1] - dates[0]  # 整个时间序列的总时间跨度
            time_offset = time_span * 0.05  # 5%的时间跨度作为偏移，参考其他点的短距离
            offset_time = latest_time + time_offset  # 向右偏移
            ax.annotate(f'最新点\n{latest_rate:.2f}\n{latest_time:%m-%d %H:%M}', (latest_time, latest_rate),
                        xytext=(offset_time, latest_rate), ha='left', va='center',
                        fontsize=12, fontweight='bold', bbox=bbox_latest, color='#0B5ED7',
                        arrowprops=dict(arrowstyle='->', color='#1F6FEB', lw=2), zorder=5)

            ax.set_xlabel('时间', fontsize=16, fontweight='bold', color='#2C3E50', labelpad=15)
            unit = "人民币/100日元" if currency == "日元" else f"人民币/100{currency}"
            ax.set_ylabel(f'现汇卖出价 ({unit})', fontsize=16, fontweight='bold', color='#2C3E50', labelpad=15)

            ax.grid(True, linestyle='-', alpha=0.3, color='#BDC3C7', zorder=0)
            ax.set_axisbelow(True)
            ax.spines[['top', 'right']].set_visible(False)
            ax.spines[['left', 'bottom']].set_color('#7F8C8D')
            ax.spines[['left', 'bottom']].set_linewidth(1.5)

            y_margin = rate_range * 0.12
            ax.set_ylim(min_rate - y_margin, max_rate + y_margin)

            from matplotlib.dates import DateFormatter, DayLocator
            date_range_days = (max(dates) - min(dates)).days if dates else 0
            if date_range_days <= 7: locator, interval = DayLocator, 1
            elif date_range_days <= 30: locator, interval = DayLocator, 3
            else: locator, interval = DayLocator, max(1, date_range_days//10)
            ax.xaxis.set_major_locator(locator(interval=interval))
            ax.xaxis.set_major_formatter(DateFormatter('%m-%d'))

            plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha='right', fontsize=11)
            ax.tick_params(axis='y', labelsize=11)

            plt.suptitle(f'{currency} 中行牌价(现汇卖出价走势图)', fontsize=24, fontweight='bold', color='#2C3E50', y=0.96)
            subtitle = f'数据时间: {dates[0]:%Y年%m月%d日} - {dates[-1]:%Y年%m月%d日} | 共{len(rates)}个数据点'
            plt.figtext(0.5, 0.92, subtitle, ha='center', va='top', fontsize=14, color='#7F8C8D', style='italic')

            legend = ax.legend(loc='upper left', frameon=True, fancybox=True, shadow=True, framealpha=0.95, edgecolor='#BDC3C7', fontsize=12)
            legend.get_frame().set_facecolor('white')

            ax.text(0.99, 0.01, '数据来源: 中国银行外汇牌价', transform=ax.transAxes, ha='right', va='bottom', fontsize=10, color='#95A5A6', style='italic')

            plt.tight_layout(rect=[0.05, 0.1, 0.95, 0.9])

            chart_filename = tempfile.mktemp(suffix=f'_{currency}_trend_{self._get_beijing_now():%Y%m%d_%H%M%S}.png')
            plt.savefig(chart_filename, dpi=200, bbox_inches='tight')
            plt.close()

            self.logger.info(f"✅ 优化走势图已生成: {chart_filename}")
            return chart_filename

        except Exception as e:
            self.logger.error(f"❌ 生成走势图失败: {e}")
            # 确保释放所有 matplotlib 资源
            try:
                plt.close('all')
            except:
                pass
            return None
        finally:
            # 确保清理所有可能的资源
            try:
                plt.clf()  # 清除当前图形
                plt.close('all')  # 关闭所有图形
            except:
                pass

    def _save_cache_index(self, cache_index: Dict):
        """保存缓存索引"""
        cache_index["last_updated"] = self._get_beijing_now().isoformat()
        with open(self.cache_index_file, 'w', encoding='utf-8') as f:
            json.dump(cache_index, f, ensure_ascii=False, indent=2)

    def _load_cache_index(self):
        """加载缓存索引"""
        try:
            with open(self.cache_index_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"currencies": {}, "version": "2.0"}


