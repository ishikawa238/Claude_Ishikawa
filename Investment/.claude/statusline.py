import json
import os
import sys


def render_bar(percent: int, width: int = 10) -> str:
    filled = max(0, min(width, percent * width // 100))
    empty = width - filled
    return "▓" * filled + "░" * empty


def main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        print("[Unknown] ░░░░░░░░░░ 0%")
        return

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("[Unknown] ░░░░░░░░░░ 0%")
        return

    model = data.get("model", {}).get("display_name") or "Unknown"
    used_percentage = data.get("context_window", {}).get("used_percentage", 0)

    try:
        percent = int(float(used_percentage))
    except (TypeError, ValueError):
        percent = 0

    percent = max(0, min(100, percent))
    cwd = os.getcwd()
    print(f"{cwd}  [{model}]  {render_bar(percent)}  {percent}%")


if __name__ == "__main__":
    main()