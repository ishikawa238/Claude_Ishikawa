#!/bin/bash

render_default() {
	echo "[Unknown] ░░░░░░░░░░ 0%"
}

if [ -t 0 ]; then
	render_default
	exit 0
fi

input=$(cat)

if [ -z "$input" ]; then
	render_default
	exit 0
fi

if command -v jq >/dev/null 2>&1; then
	MODEL=$(printf '%s' "$input" | jq -r '.model.display_name // "Unknown"')
	PCT=$(printf '%s' "$input" | jq -r '.context_window.used_percentage // 0' | cut -d. -f1)
else
	PYTHON_BIN=""
	for candidate in python3 python py; do
		if command -v "$candidate" >/dev/null 2>&1; then
			PYTHON_BIN="$candidate"
			break
		fi
	done

	if [ -z "$PYTHON_BIN" ]; then
		render_default
		exit 0
	fi

	mapfile -t parsed < <(
		printf '%s' "$input" | "$PYTHON_BIN" -c 'import json, sys
try:
		data = json.load(sys.stdin)
except Exception:
		print("Unknown")
		print("0")
		raise SystemExit(0)
model = data.get("model", {}).get("display_name") or "Unknown"
pct = data.get("context_window", {}).get("used_percentage", 0)
try:
		pct = int(float(pct))
except Exception:
		pct = 0
print(model)
print(pct)'
	)

	MODEL=${parsed[0]:-Unknown}
	PCT=${parsed[1]:-0}
	MODEL=${MODEL%$'\r'}
	PCT=${PCT%$'\r'}
fi

case "$PCT" in
	''|*[!0-9]*) PCT=0 ;;
esac

[ "$PCT" -lt 0 ] && PCT=0
[ "$PCT" -gt 100 ] && PCT=100

BAR_WIDTH=10
FILLED=$((PCT * BAR_WIDTH / 100))
EMPTY=$((BAR_WIDTH - FILLED))
BAR=""
[ "$FILLED" -gt 0 ] && printf -v FILL "%${FILLED}s" && BAR="${FILL// /▓}"
[ "$EMPTY" -gt 0 ] && printf -v PAD "%${EMPTY}s" && BAR="${BAR}${PAD// /░}"
echo "[$MODEL] $BAR $PCT%"
