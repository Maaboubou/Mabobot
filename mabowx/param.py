"""mabowx 运行参数。"""

from __future__ import annotations

import os
from pathlib import Path


class WxParam:
    """全局运行参数。

    V1 只保留首版范围所需的配置项；后续里程碑再逐步补充。
    """

    # UI 语言。V1 只实现简体中文。
    LANGUAGE = "cn"

    # 是否启用文件日志。
    ENABLE_FILE_LOGGER = True

    # 日志目录。可被环境变量 MABOWX_LOG_DIR 覆盖。
    LOG_DIR: Path = Path(os.getcwd()) / "mabowx_logs"

    # 文件日志始终按 DEBUG 记录；这里保留后续自定义入口。
    LOG_LEVEL = "INFO"

    # 文件日志按大小滚动，最多保留 6 份备份、14 天；实际规则见 mabobot_logging。
    LOG_BACKUP_COUNT = 6

    # 单条日志最大字符数，超过后截断。
    LOG_MAX_MESSAGE_CHARS = 8000

    # 文件/图片默认保存目录。
    DEFAULT_SAVE_PATH: Path = Path(os.getcwd()) / "mabowx文件下载"

    # 消息哈希辅助判断，开启后轻微影响性能。
    MESSAGE_HASH = False

    # 头像到消息的 X/Y 偏移量，用于消息定位和方向判断。
    DEFAULT_MESSAGE_XBIAS = 51
    DEFAULT_MESSAGE_YBIAS = 30
    FORCE_MESSAGE_XBIAS = False

    # 监听轮询间隔（秒）。
    LISTEN_INTERVAL = 1

    # 打开微信内置浏览器时，独立聊天窗口的 UIA 根节点可能短暂失效。
    # 在此宽限期内尝试重新绑定，不因单次 Exists(False) 永久退出监听。
    LISTENER_WINDOW_MISSING_GRACE = 20.0

    # 监听回调线程池大小。
    LISTENER_EXCUTOR_WORKERS = 4

    # 搜索聊天对象超时时间（秒）。
    SEARCH_CHAT_TIMEOUT = 4

    # 发送文件超时时间（秒）。
    SEND_FILE_TIMEOUT = 10

    # 聊天窗口尺寸。微信 4.x 只注册可见 UIA 控件，因此需要拉大窗口。
    CHAT_WINDOW_SIZE = (1600, 6000)

    # 输入框内容相似度阈值，低于该值不触发发送，避免发错。
    SEND_CONTENT_RATIO = 0.9

    # GetNextNewMessage 限制。
    GET_NEXT_MAX_QUANTITY = 30
    GET_NEXT_MAX_RUNTIME = 10

    # 特殊会话名称。
    SPECIAL_SESSION_NAME = ["公众号", "折叠的聊天", "QQ邮箱提醒", "服务号"]

    # 回调结束标识。
    CALLBACK_STOP_SIGN = "stop"

    # @成员输入间隔（秒）。
    INPUT_AT_INTERVAL = 0.5


class WxResponse(dict):
    """统一的 API 返回结构，兼容原版的三态结果。"""

    def __init__(self, status: str, message: str = "", data: dict | None = None):
        super().__init__(status=status, message=message, data=data or {})

    def __str__(self) -> str:
        return str(self.to_dict())

    def __repr__(self) -> str:
        return str(self.to_dict())

    def __bool__(self) -> bool:
        return self.is_success

    @property
    def is_success(self) -> bool:
        return self["status"] == "成功"

    def to_dict(self) -> dict:
        return {
            "status": self["status"],
            "message": self["message"],
            "data": self["data"],
        }

    @classmethod
    def success(cls, message: str = "", data: dict | None = None) -> "WxResponse":
        return cls("成功", message, data)

    @classmethod
    def failure(cls, message: str = "", data: dict | None = None) -> "WxResponse":
        return cls("失败", message, data)

    @classmethod
    def error(cls, message: str = "", data: dict | None = None) -> "WxResponse":
        return cls("错误", message, data)
