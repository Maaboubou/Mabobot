#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
启动脚本 - 新版微信自动化助手
"""

import os
import sys
import logging
from pathlib import Path
from dotenv import dotenv_values, load_dotenv

from app.utils.network_env import configure_startup_network_environment
from mabobot_logging import configure_process_logging, configured_level


# 先读取当前环境以定位备份目录；待恢复计划必须在 uvicorn 导入
# app.main 之前应用，才能安全替换代码、数据库与 .env。
load_dotenv()
try:
    from app.services.backup_service import apply_pending_restore

    restored = apply_pending_restore(Path.cwd())
    if restored:
        print(f"已应用恢复计划：{restored.get('archive')} ({restored.get('files')} files)")
        load_dotenv(override=True)
except Exception as exc:
    print(f"恢复计划执行失败，已阻止应用启动：{exc}", file=sys.stderr)
    raise

_DOTENV_WEB_HOST = str(dotenv_values().get("WEB_HOST") or "").strip()

# 必须早于 uvicorn 导入 app.main；否则 LiteLLM 和插件初始化会先读取代理环境。
configure_startup_network_environment()

import uvicorn


def setup_logging(level=None):
    """配置日志"""
    os.makedirs("data", exist_ok=True)
    configure_process_logging("app", level=level)

    # 配置第三方库日志级别，减少噪音
    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # 只记录警告以上
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)  # 禁用访问日志
    logging.getLogger("mabowx").setLevel(logging.INFO)
    logging.getLogger("requests").setLevel(logging.WARNING)  # requests库只记录警告
    logging.getLogger("urllib3").setLevel(logging.WARNING)  # urllib3库只记录警告
    logging.getLogger("app.plugins").setLevel(logging.WARNING)  # 插件注册日志设为警告以上


def check_environment():
    """检查运行环境"""
    logger = logging.getLogger(__name__)

    # 检查Python版本
    if sys.version_info < (3, 11) or sys.version_info >= (3, 13):
        logger.error("64-bit Python 3.11 or 3.12 is required")
        sys.exit(1)

    # 检查必要的目录
    required_dirs = ["app", "data"]
    for directory in required_dirs:
        if not Path(directory).exists():
            logger.error(f"Required directory not found: {directory}")
            sys.exit(1)

    # 检查环境变量
    env_file = Path(".env")
    if env_file.exists():
        logger.debug(f"Found environment file: {env_file}")
    else:
        logger.warning("No .env file found. Make sure to set required environment variables.")

    logger.debug("Environment check completed")


def main():
    """主函数"""
    print("=" * 60)
    from app.version import APP_VERSION

    print(f"Mabobot v{APP_VERSION}")
    print("基于FastAPI + 事件总线 + 插件化架构")
    print("=" * 60)

    # 解析命令行参数
    import argparse
    parser = argparse.ArgumentParser(description="Mabobot")
    parser.add_argument(
        "--host",
        default=_DOTENV_WEB_HOST or os.getenv("WEB_HOST", "127.0.0.1"),
        help="Host to bind (default: project .env WEB_HOST, process WEB_HOST, or 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("WEB_PORT", "8888")),
        help="Port to bind (default: WEB_PORT or 8888)",
    )
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development")
    parser.add_argument("--workers", type=int, default=1, help="Number of worker processes")
    parser.add_argument(
        "--restore",
        metavar="ARCHIVE",
        help="离线恢复指定 .mabobot-backup.zip 后退出（项目必须处于停止状态）",
    )
    parser.add_argument("--log-level", default=None, choices=["debug", "info", "warning", "error"],
                       help="Log level")

    args = parser.parse_args()
    if args.workers > 1 and not args.reload:
        os.environ["MABOBOT_WEB_MULTI_WORKER"] = "1"
    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)
    check_environment()

    if args.restore:
        from app.services.backup_service import BackupService

        result = BackupService(Path.cwd()).restore_archive(Path(args.restore))
        logger.info(
            "Restore completed from %s (%s files); restart normally to validate the restored system",
            result.get("archive"),
            result.get("files"),
        )
        return

    logger.info(f"Starting server on {args.host}:{args.port}")
    effective_level = logging.getLevelName(configured_level(args.log_level)).lower()
    logger.info("Log level: %s", effective_level)
    logger.info(f"Reload mode: {args.reload}")

    try:
        # 启动FastAPI应用
        uvicorn.run(
            "app.main:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            workers=args.workers if not args.reload else 1,
            log_level=effective_level,
            access_log=False  # 关闭uvicorn访问日志，减少噪音
        )
    except KeyboardInterrupt:
        logger.info("Application stopped by user")
    except Exception as e:
        logger.error(f"Failed to start application: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
