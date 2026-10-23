#!/usr/bin/env python3
"""Periodically refresh cached forecasts before users request them."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000", help="Base URL of ionoscope-api.")
    parser.add_argument("--interval-seconds", type=float, default=900.0, help="Delay between refresh runs.")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds.")
    parser.add_argument("--stations", default="", help="Comma-separated station codes. Empty means all available.")
    parser.add_argument("--all-stations", action="store_true", help="Refresh stations without prepared data too.")
    parser.add_argument("--fail-on-error", action="store_true", help="Return failures from refresh command.")
    parser.add_argument("--run-on-start", action="store_true", help="Run one refresh immediately before sleeping.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    first_iteration = True
    while True:
        if first_iteration and not args.run_on_start:
            first_iteration = False
            time.sleep(max(args.interval_seconds, 1.0))
            continue

        completed = subprocess.run(build_refresh_command(args), check=False)
        if completed.returncode != 0:
            print(f"forecast refresh iteration failed with exit code {completed.returncode}", file=sys.stderr)

        first_iteration = False
        time.sleep(max(args.interval_seconds, 1.0))


def build_refresh_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        "scripts/refresh_forecast_cache.py",
        "--api-url",
        args.api_url,
        "--timeout",
        str(args.timeout),
    ]
    if args.stations:
        command.extend(["--stations", args.stations])
    if args.all_stations:
        command.append("--all-stations")
    if args.fail_on_error:
        command.append("--fail-on-error")
    return command


if __name__ == "__main__":
    raise SystemExit(main())
