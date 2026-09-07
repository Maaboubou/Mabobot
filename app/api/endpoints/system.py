"""
系统状态API端点
"""

import asyncio
import psutil
import time
import os
import subprocess
import sys
import threading
import json
import platform
from functools import partial
from typing import Dict, Any
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request

from app.utils.logging_utils import read_log_lines
from app.utils.system_temperature import get_temperature_status
from app.version import APP_VERSION



router = APIRouter()

_RESTART_SERVICE_ALIASES = {
    "all": "all",
    "system": "all",
    "web": "web",
    "app": "web",
    "start.py": "web",
}

_BOT_CONTROL_ACTIONS = {
    "start": "start-bot",
    "stop": "stop-bot",
}


def normalize_restart_service(service: str) -> str:
    """把 API 服务名转换成 launcher 可识别的重启动作。"""
    normalized = _RESTART_SERVICE_ALIASES.get(str(service or "").strip().casefold())
    if not normalized:
        raise ValueError(f"Unsupported restart service: {service}")
    return normalized


def write_restart_signal(signal_file: Path, action: str) -> None:
    """原子写入重启信号，避免 launcher 读取到尚未写完的空文件。"""
    temp_file = signal_file.with_name(
        f"{signal_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temp_file.write_text(action, encoding="utf-8")
        os.replace(temp_file, signal_file)
    finally:
        if temp_file.exists():
            temp_file.unlink()


def get_launcher_signal_protocol(project_root: Path) -> int:
    """返回当前活跃 launcher 的服务控制协议版本。"""
    state_file = project_root / "data" / "launcher_state.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        protocol = int(state.get("signal_protocol", 0))
        process = psutil.Process(int(state["pid"]))
        command_line = " ".join(process.cmdline()).casefold()
        launcher_id = str(state.get("launcher_id") or "").strip().casefold()
        is_desktop_launcher = (
            launcher_id == "mabobot.desktop.launcher"
            and "mabobot_launcher" in command_line
        )
        return protocol if process.is_running() and is_desktop_launcher else 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, psutil.Error):
        return 0


def launcher_supports_precise_restart(project_root: Path) -> bool:
    """确认当前活跃 launcher 支持区分 all/web 的第二版信号协议。"""
    return get_launcher_signal_protocol(project_root) >= 2


@router.get("/restart-capabilities")
async def get_restart_capabilities() -> Dict[str, Any]:
    """供前端确认当前后端支持精确的 Web 单独重启协议。"""
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    signal_protocol = get_launcher_signal_protocol(project_root)
    precise_restart = signal_protocol >= 2
    bot_control = signal_protocol >= 3
    return {
        "services": ["all", "web"] if precise_restart else ["all"],
        "controls": ["start-bot", "stop-bot"] if bot_control else [],
        "signal_protocol": signal_protocol or 1,
        "reason": (
            None
            if precise_restart
            else "当前管理面板进程不支持单独重启 Web 服务。请完整关闭并重新打开 Mabobot 管理面板。"
        ),
    }


@router.post("/control/bot/{action}")
async def control_bot_service(action: str) -> Dict[str, Any]:
    """通过 launcher 启动或停止 Bot 服务，并保持 Web 控制台在线。"""
    normalized_action = _BOT_CONTROL_ACTIONS.get(str(action or "").strip().casefold())
    if not normalized_action:
        raise HTTPException(status_code=400, detail=f"Unsupported Bot service action: {action}")

    project_root = Path(__file__).resolve().parent.parent.parent.parent
    if get_launcher_signal_protocol(project_root) < 3:
        raise HTTPException(
            status_code=409,
            detail="当前 launcher 尚未加载 Bot 启停协议，请完整重启一次管理控制台",
        )

    try:
        signal_file = project_root / ".restart_signal"
        write_restart_signal(signal_file, normalized_action)
        action_label = "启动" if normalized_action == "start-bot" else "停止"
        return {
            "status": "success",
            "message": f"Bot 服务{action_label}请求已提交",
            "service": "bot",
            "action": action,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to control Bot service: {str(e)}")


@router.get("/status")
async def get_system_status() -> Dict[str, Any]:
    """获取系统状态"""
    try:
        # CPU信息
        cpu_percent = psutil.cpu_percent(interval=1)
        cpu_count = psutil.cpu_count()

        # 内存信息
        memory = psutil.virtual_memory()

        # 磁盘信息
        disk = psutil.disk_usage('/')

        # 系统启动时间
        boot_time = psutil.boot_time()
        system_uptime_seconds = time.time() - boot_time

        # 应用启动时间
        process = psutil.Process()
        app_uptime_seconds = time.time() - process.create_time()

        # 格式化运行时长
        def format_uptime(seconds):
            days = int(seconds // 86400)
            hours = int((seconds % 86400) // 3600)
            minutes = int((seconds % 3600) // 60)
            if days > 0:
                return f"{days}天 {hours}小时"
            elif hours > 0:
                return f"{hours}小时 {minutes}分钟"
            else:
                return f"{minutes}分钟"

        # 获取监控服务状态
        monitor_status = {}
        try:
            from app.services.wechat_monitor_service import get_monitor_service
            monitor_service = get_monitor_service()
            monitor_status = monitor_service.get_status()
        except Exception as e:
            monitor_status = {"error": str(e)}

        # 返回扁平化的数据结构，方便前端使用
        return {
            "cpu_percent": cpu_percent,
            "cpu_count": cpu_count,
            "memory_percent": memory.percent,
            "memory_total": memory.total,
            "memory_used": memory.used,
            "memory_available": memory.available,
            "disk_percent": (disk.used / disk.total) * 100,
            "disk_total": disk.total,
            "disk_used": disk.used,
            "disk_free": disk.free,
            "temperature": get_temperature_status(),
            "system_uptime": format_uptime(system_uptime_seconds),
            "uptime": format_uptime(app_uptime_seconds),
            "system_uptime_seconds": system_uptime_seconds,
            "app_uptime_seconds": app_uptime_seconds,
            "wechat_monitor": monitor_status,
            "timestamp": time.time()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get system status: {str(e)}")


@router.get("/health")
@router.get("/health/live")
async def health_check() -> Dict[str, Any]:
    """Liveness probe: the Web process can answer requests."""
    return {
        "status": "live",
        "timestamp": time.time(),
        "version": APP_VERSION,
    }


@router.get("/health/ready")
async def readiness_check(request: Request) -> Dict[str, Any]:
    """Readiness is separate from liveness and exposes degraded components."""
    components = get_app_components(request)
    event_bus = components.get("event_bus")
    plugin_manager = components.get("plugin_manager")
    wechat_manager = components.get("wechat_manager")
    checks = {
        "event_bus": bool(event_bus and getattr(event_bus, "_running", False)),
        "plugin_manager": bool(plugin_manager),
        "wechat": bool(wechat_manager and wechat_manager.is_connected_cached()),
    }
    try:
        from app.models.base import engine

        with engine.connect() as connection:
            connection.execute(__import__("sqlalchemy").text("SELECT 1"))
        checks["database"] = True
    except Exception:
        checks["database"] = False

    critical_ready = checks["event_bus"] and checks["plugin_manager"] and checks["database"]
    return {
        "status": "ready" if critical_ready and checks["wechat"] else ("degraded" if critical_ready else "not_ready"),
        "ready": critical_ready,
        "checks": checks,
        "optional": ["wechat"],
        "timestamp": time.time(),
        "version": APP_VERSION,
    }


@router.get("/health/details")
async def health_details(request: Request) -> Dict[str, Any]:
    readiness = await readiness_check(request)
    return await asyncio.to_thread(_health_details_snapshot, readiness)


def _health_details_snapshot(readiness: Dict[str, Any]) -> Dict[str, Any]:
    """Health probes and disk/DB-backed summaries must not occupy the ASGI loop."""
    from app.services.backup_service import get_backup_service
    from app.services.plugin_runtime import get_plugin_runtime_registry
    from app.services.runtime_operations import get_runtime_operation_service

    runtime_plugins = get_plugin_runtime_registry().snapshot(include_storage=False)
    try:
        from app.services.llm_manager import get_llm_manager

        model_health = get_llm_manager().get_model_health()
    except Exception:
        model_health = []
    readiness.update(
        {
            "plugin_runtime": {
                "plugins": runtime_plugins,
                "unhealthy": sum(
                    (item.get("health") or {}).get("status") in {"unhealthy", "failed"}
                    for item in runtime_plugins
                ),
            },
            "operations": get_runtime_operation_service().stats(),
            "models": {
                "health": model_health,
                "open_circuits": sum(item.get("status") == "open" for item in model_health),
                "degraded": sum(item.get("status") in {"degraded", "half_open"} for item in model_health),
            },
            "backup": {
                "pending_restore": bool(get_backup_service().overview().get("pending_restore")),
            },
        }
    )
    return readiness


@router.get("/wechat-monitor")
async def get_wechat_monitor_status() -> Dict[str, Any]:
    """获取微信监控服务状态"""
    try:
        from app.services.wechat_monitor_service import get_monitor_service
        monitor_service = get_monitor_service()
        status = monitor_service.get_status()
        return {
            "status": "success",
            "data": status,
            "timestamp": time.time()
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "timestamp": time.time()
        }


@router.post("/wechat-monitor/check")
async def force_wechat_check() -> Dict[str, Any]:
    """强制执行一次微信状态检查"""
    try:
        from app.services.wechat_monitor_service import get_monitor_service
        monitor_service = get_monitor_service()
        result = monitor_service.force_check()
        return {
            "status": "success" if result.get("success") else "error",
            "data": result,
            "timestamp": time.time()
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "timestamp": time.time()
        }


@router.post("/wechat/restart")
async def restart_wechat(request: Request) -> Dict[str, Any]:
    """重启微信连接"""
    try:
        # 获取微信管理器
        components = get_app_components(request)
        wechat_manager = components.get("wechat_manager")

        if not wechat_manager:
            return {
                "status": "error",
                "message": "微信管理器不可用",
                "timestamp": time.time()
            }

        # 调用重启方法
        success = wechat_manager.restart_wechat()

        if success:
            return {
                "status": "success",
                "message": "微信重启请求已提交，请稍候检查状态",
                "timestamp": time.time()
            }
        else:
            return {
                "status": "error",
                "message": "微信重启请求失败",
                "timestamp": time.time()
            }

    except Exception as e:
        return {
            "status": "error",
            "message": f"重启微信连接时发生异常: {str(e)}",
            "timestamp": time.time()
        }


@router.get("/info")
async def get_system_info() -> Dict[str, Any]:
    """获取系统详细信息"""
    try:
        # 进程信息
        process = psutil.Process()

        # 获取机器人名称
        from app.services.config_service import get_setting
        bot_name = get_setting("WECHAT_BOT_NAME")

        return {
            "application": {
                "name": "Mabobot",
                "version": APP_VERSION,
                "bot_name": bot_name,
                "pid": process.pid,
                "memory_usage": process.memory_info().rss,
                "cpu_percent": process.cpu_percent(),
                "create_time": process.create_time(),
                "threads": process.num_threads()
            },
            "python": {
                "version": platform.python_version(),
                "platform": platform.python_implementation(),
            },
            "system": {
                "platform": platform.system(),
                "architecture": platform.machine() or "unknown",
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get system info: {str(e)}")


def get_app_components(request):
    """获取应用组件"""
    return {
        "event_bus": getattr(request.app.state, 'event_bus', None),
        "plugin_manager": getattr(request.app.state, 'plugin_manager', None),
        "wechat_manager": getattr(request.app.state, 'wechat_manager', None)
    }


@router.get("/components")
async def get_components_status(request: Request) -> Dict[str, Any]:
    """获取各组件状态"""
    try:
        components_data = get_app_components(request)
        components = {}

        # 事件总线状态
        event_bus = components_data.get("event_bus")
        if event_bus:
            components["event_bus"] = {
                "status": "running",
                "stats": event_bus.get_stats()
            }
        else:
            components["event_bus"] = {"status": "not_available"}

        # 插件管理器状态
        plugin_manager = components_data.get("plugin_manager")
        if plugin_manager:
            components["plugin_manager"] = {
                "status": "running",
                "stats": plugin_manager.get_stats()
            }
        else:
            components["plugin_manager"] = {"status": "not_available"}

        # 微信管理器状态
        wechat_manager = components_data.get("wechat_manager")
        if wechat_manager:
            try:
                is_connected = wechat_manager.is_connected_cached()
                stats = wechat_manager.get_stats()
            except Exception:
                is_connected = False
                stats = {}

            components["wechat_manager"] = {
                "status": "connected" if is_connected else "disconnected",
                "stats": stats
            }
        else:
            components["wechat_manager"] = {"status": "not_available"}

        return components

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get components status: {str(e)}")


@router.get("/logs/{log_type}")
async def get_logs(
    log_type: str,
    lines: int = 100,
    plugin_name: str = None,
    search: str = None
) -> Dict[str, Any]:
    """获取日志内容"""
    try:
        # 定义日志文件路径
        log_files = {
            "app": "logs/app.log",
            "wx_bot": "logs/wx_bot.log"
        }

        if log_type not in log_files:
            raise HTTPException(status_code=400, detail=f"Invalid log type: {log_type}")

        log_path = Path(log_files[log_type])

        if not log_path.exists():
            return {
                "log_type": log_type,
                "content": "",
                "lines_count": 0,
                "file_exists": False,
                "message": f"日志文件不存在: {log_path}"
            }

        # 普通查看仅反向读取文件尾部；筛选时逐行扫描并使用固定长度队列。
        # 放入线程池，避免日志 I/O 阻塞 FastAPI 事件循环。
        line_limit = max(1, min(int(lines or 100), 5000))
        required_text = (
            f"app.plugins.{plugin_name}"
            if plugin_name and log_type == "app"
            else None
        )
        loop = asyncio.get_running_loop()
        read_result = await loop.run_in_executor(
            None,
            partial(
                read_log_lines,
                log_path,
                max_lines=line_limit,
                search=search,
                required_text=required_text,
                include_rotated=True,
            ),
        )
        content = ''.join(read_result.lines)

        file_size = log_path.stat().st_size

        return {
            "log_type": log_type,
            "content": content,
            "lines_count": len(read_result.lines),
            # 尾读模式不再为了展示总行数而扫描整份文件。
            "total_lines": read_result.total_lines,
            "filtered_count": read_result.filtered_count,
            "counts_exact": read_result.counts_exact,
            "read_strategy": read_result.strategy,
            "file_exists": True,
            "file_size": file_size,
            "file_path": str(log_path)
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read log file: {str(e)}")


@router.post("/restart/{service}")
async def restart_service(service: str) -> Dict[str, Any]:
    """重启服务"""
    try:
        action = normalize_restart_service(service)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    project_root = Path(__file__).resolve().parent.parent.parent.parent
    if action == "web" and not launcher_supports_precise_restart(project_root):
        raise HTTPException(
            status_code=409,
            detail="当前 launcher 尚未加载 Web 单独重启协议，请完整重启一次管理控制台",
        )

    try:
        # 写入重启信号文件，由桌面启动器按 action 精确执行。
        # 使用绝对路径确保独立启动器进程能看到。
        signal_file = project_root / ".restart_signal"
        write_restart_signal(signal_file, action)

        print(f"[System] Restart signal written to: {signal_file} action={action}")

        service_label = "Web 服务" if action == "web" else "全部服务"

        return {
            "status": "success",
            "message": f"{service_label}重启请求已提交",
            "service": action,
            "restart_self": True, # 前端根据此字段判断是否需要倒计时刷新
            "note": f"系统主控进程将重启{service_label}，请稍候刷新页面"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to trigger restart: {str(e)}")


@router.get("/processes")
async def get_processes_status() -> Dict[str, Any]:
    """获取进程状态"""
    try:
        processes = {
            "wx_bot": {"running": False, "pid": None},
            "app": {"running": True, "pid": os.getpid()}
        }

        # 检查 wx_bot.py 进程
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if proc.info['cmdline'] and any('wx_bot.py' in str(cmd) for cmd in proc.info['cmdline']):
                    processes["wx_bot"] = {
                        "running": True,
                        "pid": proc.info['pid']
                    }
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        return {
            "processes": processes,
            "timestamp": time.time()
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get processes status: {str(e)}")
