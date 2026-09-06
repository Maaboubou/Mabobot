"""Local preview trigger; executes the identical production generation pipeline."""
from __future__ import annotations
import argparse
from datetime import datetime
import json
from pathlib import Path
from .pipeline import generate_calendar, restyle_calendar


def main():
    parser = argparse.ArgumentParser(description="刘局推荐：子 Codex 月度发售日历预览")
    parser.add_argument("month", help="YYYY-MM")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--restyle-from", type=Path, help="复用该运行目录的核实数据，执行新版编辑与封面流程")
    args = parser.parse_args()
    if args.output:
        output = args.output
    else:
        from app.services.plugin_runtime import PluginStorage
        output = PluginStorage("game_release_calendar").persistent_path(
            f"runs/{args.month}/{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    progress = lambda p, s: print(f"[{p:3}%] {s}", flush=True)
    if args.restyle_from:
        result = restyle_calendar(args.restyle_from, output, progress=progress)
    else:
        result = generate_calendar(output, args.month, resume=args.resume, progress=progress)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
