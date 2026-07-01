"""
Phase 0: moomoo お気に入り監視の API 検証（読み取り専用）

実行:
    cd investment_support
    python -m scripts._diag_watchlist_monitor
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import CONFIG
from src.swing_detector import evaluate_swing, macd_confirm_detail
from src.technical_calculator import calculate_snapshot

_MOOMOO_SDK = _PROJECT_ROOT / "MMAPI4Python_10.2.6208"
_MARKET_PREFIX = {"JP": "JP", "US": "US", "HK": "HK"}


def _ensure_sdk() -> None:
    sdk = str(_MOOMOO_SDK)
    if sdk not in sys.path:
        sys.path.insert(0, sdk)


def _moomoo_cfg() -> dict:
    return CONFIG.get("moomoo") or {}


def _monitor_cfg() -> dict:
    moomoo = _moomoo_cfg()
    return moomoo.get("watchlist_monitor") or {}


def _parse_code(code: str) -> tuple[str, str] | None:
    code = (code or "").strip()
    if "." not in code:
        return None
    prefix, ticker = code.split(".", 1)
    market = _MARKET_PREFIX.get(prefix.upper())
    if not market:
        return None
    return ticker.upper(), market


def _list_groups(host: str, port: int) -> list[dict]:
    _ensure_sdk()
    from moomoo import OpenQuoteContext
    from moomoo.common.constant import RET_OK, UserSecurityGroupType

    ctx = OpenQuoteContext(host=host, port=port)
    try:
        ret, data = ctx.get_user_security_group(UserSecurityGroupType.ALL)
        if ret != RET_OK:
            return [{"error": str(data)}]
        if data is None or data.empty:
            return []
        rows = []
        for _, row in data.iterrows():
            rows.append({
                "group_name": str(row.get("group_name", "")).strip(),
                "group_type": str(row.get("group_type", "")),
            })
        return rows
    finally:
        ctx.close()


def _fetch_group_securities(host: str, port: int, group_name: str) -> tuple[int, object]:
    _ensure_sdk()
    from moomoo import OpenQuoteContext
    from moomoo.common.constant import RET_OK

    ctx = OpenQuoteContext(host=host, port=port)
    try:
        return ctx.get_user_security(group_name)
    finally:
        ctx.close()


def _summarize_securities(data) -> dict:
    if data is None or getattr(data, "empty", True):
        return {"total": 0, "by_market": {}, "samples": []}
    by_market: dict[str, int] = {}
    samples: list[dict] = []
    for _, row in data.iterrows():
        code = str(row.get("code", ""))
        name = str(row.get("name", ""))
        parsed = _parse_code(code)
        if parsed:
            ticker, market = parsed
            by_market[market] = by_market.get(market, 0) + 1
            if len(samples) < 5:
                samples.append({"code": code, "ticker": ticker, "market": market, "name": name})
        else:
            by_market["_other"] = by_market.get("_other", 0) + 1
    return {"total": len(data), "by_market": by_market, "samples": samples}


def _kline_to_prices(kline_df, snapshot_row: dict | None) -> list[dict]:
    prices: list[dict] = []
    for _, row in kline_df.iterrows():
        time_key = str(row.get("time_key", ""))[:10]
        if not time_key:
            continue
        prices.append({
            "date": time_key,
            "open": float(row.get("open") or 0),
            "high": float(row.get("high") or 0),
            "low": float(row.get("low") or 0),
            "close": float(row.get("close") or 0),
            "adj_close": float(row.get("close") or 0),
            "volume": float(row.get("volume") or 0),
        })
    if not snapshot_row:
        return prices
    today = date.today().strftime("%Y-%m-%d")
    last_price = snapshot_row.get("last_price")
    if last_price is None:
        return prices
    bar = {
        "date": today,
        "open": float(snapshot_row.get("open_price") or last_price),
        "high": float(snapshot_row.get("high_price") or last_price),
        "low": float(snapshot_row.get("low_price") or last_price),
        "close": float(last_price),
        "adj_close": float(last_price),
        "volume": float(snapshot_row.get("volume") or 0),
    }
    if prices and prices[-1]["date"] == today:
        prices[-1] = bar
    else:
        prices.append(bar)
    return prices


def _fetch_kline_and_snapshot(host: str, port: int, code: str) -> dict:
    _ensure_sdk()
    from moomoo import OpenQuoteContext
    from moomoo.common.constant import RET_OK, AuType, KLType

    start = (date.today() - timedelta(days=400)).strftime("%Y-%m-%d")
    ctx = OpenQuoteContext(host=host, port=port)
    try:
        ret_k, kline, _ = ctx.request_history_kline(
            code, start=start, end=None, ktype=KLType.K_DAY,
            autype=AuType.QFQ, max_count=300,
        )
        ret_s, snap = ctx.get_market_snapshot([code])
        result = {"code": code, "kline_ok": ret_k == RET_OK, "snapshot_ok": ret_s == RET_OK}
        if ret_k != RET_OK:
            result["kline_error"] = str(kline)
            return result
        if ret_s != RET_OK:
            result["snapshot_error"] = str(snap)
            return result
        snap_row = snap.iloc[0].to_dict() if not snap.empty else {}
        prices = _kline_to_prices(kline, snap_row)
        parsed = _parse_code(code)
        if not parsed:
            result["error"] = "unsupported code"
            return result
        ticker, _market = parsed
        target = date.today()
        snap_calc = calculate_snapshot(ticker, target, prices)
        tk = {
            "ticker": ticker,
            "market": _market,
            "name": snap_row.get("name"),
            "close": snap_calc.get("close") or snap_row.get("last_price"),
            "ema25": snap_calc.get("ema25"),
            "ema75": snap_calc.get("ema75"),
            "rsi14": snap_calc.get("rsi14"),
            "atr14": snap_calc.get("atr14"),
            "volume": snap_calc.get("volume"),
            "vol_20d_avg": snap_calc.get("vol_20d_avg"),
            "macd_diff": snap_calc.get("macd_diff"),
            "macd_dea": snap_calc.get("macd_dea"),
            "macd_hist": snap_calc.get("macd_hist"),
            "macd_diff_prev": snap_calc.get("macd_diff_prev"),
            "macd_dea_prev": snap_calc.get("macd_dea_prev"),
            "macd_hist_prev": snap_calc.get("macd_hist_prev"),
        }
        swing = evaluate_swing(tk, target.strftime("%Y-%m-%d"))
        result.update({
            "ticker": ticker,
            "price_bars": len(prices),
            "last_price": snap_row.get("last_price"),
            "ema25": tk["ema25"],
            "ema75": tk["ema75"],
            "rsi14": tk["rsi14"],
            "macd_confirm_type": swing.get("macd_confirm_type"),
            "label": swing.get("label"),
            "conditions_met": swing.get("_conditions_met_count"),
            "macd_both": swing.get("macd_confirm_type") == "both",
            "watchlist_strict_ok": (
                swing.get("cond_c_weekly_uptrend") == 1
                and swing.get("cond_c_rsi_oversold") == 1
                and swing.get("macd_confirm_type") == "both"
            ),
        })
        return result
    finally:
        ctx.close()


def main() -> int:
    cfg = _moomoo_cfg()
    mon = _monitor_cfg()
    host = cfg.get("host", "127.0.0.1")
    port = int(cfg.get("port", 11111))
    primary_group = mon.get("group_name", "Favorites")
    fallbacks = mon.get("group_name_fallbacks") or [
        "Favorites", "All", "お気に入り", "全て",
    ]

    report: dict = {
        "phase": 0,
        "date": date.today().isoformat(),
        "opend": {"host": host, "port": port},
        "config_group_name": primary_group,
        "groups": [],
        "group_probe": [],
        "resolved_group": None,
        "resolved_summary": None,
        "indicator_probe": [],
        "jp_read_ok": False,
        "us_read_ok": False,
        "recommendations": [],
    }

    print("=== Phase 0: moomoo お気に入り監視 API 検証 ===\n")

    try:
        groups = _list_groups(host, port)
    except Exception as e:
        print(f"OpenD 接続失敗: {e}")
        print("moomoo OpenD が起動しているか確認してください。")
        return 2

    report["groups"] = groups
    print(f"[1] グループ一覧 ({len(groups)} 件):")
    for g in groups:
        print(f"    - {g.get('group_name')!r}  type={g.get('group_type')}")

    group_names = {g.get("group_name") for g in groups if g.get("group_name")}
    candidates = [primary_group] + [n for n in fallbacks if n != primary_group]

    resolved = None
    resolved_data = None
    for name in candidates:
        ret, data = _fetch_group_securities(host, port, name)
        summary = _summarize_securities(data if ret == 0 else None)
        probe = {
            "group_name": name,
            "ret": ret,
            "exists_in_list": name in group_names,
            **summary,
        }
        if ret != 0:
            probe["error"] = str(data)
        report["group_probe"].append(probe)
        print(f"\n[2] get_user_security({name!r}): ret={ret}, total={summary['total']}, "
              f"by_market={summary['by_market']}")
        if ret == 0 and summary["total"] > 0 and resolved is None:
            resolved = name
            resolved_data = data

    if resolved is None and groups:
        for g in groups:
            name = g.get("group_name")
            if not name or name in candidates:
                continue
            ret, data = _fetch_group_securities(host, port, name)
            if ret != 0:
                continue
            summary = _summarize_securities(data)
            if summary["by_market"].get("US", 0) + summary["by_market"].get("JP", 0) == 0:
                continue
            report["group_probe"].append({
                "group_name": name,
                "ret": ret,
                "exists_in_list": True,
                "auto_discovered": True,
                **summary,
            })
            resolved = name
            resolved_data = data
            break

    report["resolved_group"] = resolved
    if resolved:
        summary = _summarize_securities(resolved_data)
        report["resolved_summary"] = summary
        report["us_read_ok"] = summary["by_market"].get("US", 0) > 0
        report["jp_read_ok"] = summary["by_market"].get("JP", 0) > 0
        print(f"\n[3] 採用グループ: {resolved!r}  (US={summary['by_market'].get('US', 0)}, "
              f"JP={summary['by_market'].get('JP', 0)})")
    else:
        print("\n[3] 採用グループ: なし（候補グループから銘柄を取得できませんでした）")
        report["recommendations"].append(
            "group_name を moomoo OpenAPI の実名（例: All, Favorites）に合わせてください。"
            "日本語の「全て」「お気に入り」は API では Unknown watchlists になります。"
        )

    # JP kline 可否（お気に入りに JP が無くても API 単体検証）
    jp_kline_probe = []
    for code in ("JP.7203", "JP.7974"):
        try:
            probe = _fetch_kline_and_snapshot(host, port, code)
        except Exception as e:
            probe = {"code": code, "error": str(e)}
        jp_kline_probe.append(probe)
    report["jp_kline_probe"] = jp_kline_probe
    report["jp_kline_ok"] = all(p.get("kline_ok") for p in jp_kline_probe)
    print("\n[4b] JP kline 単体検証:")
    for p in jp_kline_probe:
        print(f"    {p.get('code')}: kline={p.get('kline_ok')} error={p.get('kline_error') or p.get('error')}")

    # 指標検証: US 優先、なければ JP
    test_codes: list[str] = []
    if resolved_data is not None and not resolved_data.empty:
        for _, row in resolved_data.iterrows():
            code = str(row.get("code", ""))
            parsed = _parse_code(code)
            if parsed and parsed[1] == "US":
                test_codes.append(code)
                if len(test_codes) >= 3:
                    break
        if not test_codes:
            for _, row in resolved_data.iterrows():
                code = str(row.get("code", ""))
                parsed = _parse_code(code)
                if parsed and parsed[1] == "JP":
                    test_codes.append(code)
                    if len(test_codes) >= 3:
                        break

    print(f"\n[4] 指標検証 ({len(test_codes)} 銘柄):")
    both_count = 0
    strict_count = 0
    for code in test_codes:
        try:
            probe = _fetch_kline_and_snapshot(host, port, code)
        except Exception as e:
            probe = {"code": code, "error": str(e)}
        report["indicator_probe"].append(probe)
        if probe.get("macd_both"):
            both_count += 1
        if probe.get("watchlist_strict_ok"):
            strict_count += 1
        print(
            f"    {code}: kline={probe.get('kline_ok')} snapshot={probe.get('snapshot_ok')} "
            f"label={probe.get('label')} macd={probe.get('macd_confirm_type')} "
            f"strict_ok={probe.get('watchlist_strict_ok')}"
        )
        if probe.get("error"):
            print(f"      error: {probe['error']}")

    if not test_codes:
        print("    (検証対象銘柄なし)")
        report["recommendations"].append("お気に入りに US/JP 株を1件以上登録してください。")

    # 推奨
    if resolved and resolved != primary_group:
        report["recommendations"].append(
            f"config の group_name を {resolved!r} に更新することを推奨します。"
        )
    if report["us_read_ok"]:
        report["recommendations"].append("初版 markets は [\"US\"] で問題ありません。")
    if not report.get("jp_kline_ok", False):
        report["recommendations"].append(
            "JP の kline/snapshot は本環境では取得不可（Unknown stock）。"
            "初版 watchlist_monitor の markets は [\"US\"] のみとする。"
        )
    elif report["jp_read_ok"]:
        report["recommendations"].append(
            "JP 銘柄の読み取り・kline とも可能。markets に JP を追加可。"
        )

    report["recommendations"].append(
        f"検証銘柄 {len(test_codes)} 件中 MACD both={both_count}, "
        f"厳格3条件={strict_count}（both 必須は日次より厳しい）"
    )

    out_path = _PROJECT_ROOT / "logs" / f"phase0_watchlist_{date.today().strftime('%Y%m%d')}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n[5] 推奨事項:")
    for r in report["recommendations"]:
        print(f"    - {r}")
    print(f"\nレポート保存: {out_path}")
    return 0 if resolved else 1


if __name__ == "__main__":
    raise SystemExit(main())
