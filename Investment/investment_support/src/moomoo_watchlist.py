"""
moomoo OpenD 経由で「システム候補」お気に入りグループを同期する。

- candidate のみ追加
- candidate でなくなってから 7 日経過後に DEL（全体お気に入りからも削除）
- 保有銘柄・スクリーニング★ブックマークは削除しない
"""

from __future__ import annotations

import csv
import sys
import time
from collections import deque
from datetime import date, datetime
from pathlib import Path

from shared.net import is_port_open
from src.config import CONFIG
from src.data_store import fetch_all, get_connection
from src.logger import get_logger

logger = get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MOOMOO_SDK = _PROJECT_ROOT / "MMAPI4Python_10.2.6208"
_SYNC_CSV = _PROJECT_ROOT / "moomoo_watchlist_sync.csv"
_CSV_FIELDS = ["ticker", "market", "moomoo_code", "added_at", "last_candidate_date"]

_MARKET_PREFIX = {"JP": "JP", "US": "US"}
_DEFAULT_GROUP = "システム候補"
_DEFAULT_MIN_DAYS = 7
# modify_user_security は「30 秒に 10 回まで」。追加・削除で共通のカウントになる
_API_BATCH_SIZE = 10
_API_WINDOW_SEC = 31.0
_modify_call_times: deque[float] = deque()


def _wait_for_modify_slot() -> None:
    """直近 _API_WINDOW_SEC 秒の呼び出しが上限に達していれば、枠が空くまで待つ。"""
    now = time.monotonic()
    while _modify_call_times and now - _modify_call_times[0] >= _API_WINDOW_SEC:
        _modify_call_times.popleft()
    if len(_modify_call_times) >= _API_BATCH_SIZE:
        time.sleep(_API_WINDOW_SEC - (now - _modify_call_times[0]))
        _modify_call_times.popleft()
    _modify_call_times.append(time.monotonic())

# moomoo OpenAPI の modify_user_security は Moomoo JP 口座で日本株に未対応。
# （JP.7974 等でも "not supported for SGX and OSE futures" が返る。US 株は動作確認済み）
_WATCHLIST_API_SUPPORTED_MARKETS = frozenset({"US"})


def _get_moomoo_cfg() -> dict | None:
    cfg = CONFIG.get("moomoo")
    return cfg if cfg else None


def _get_watchlist_cfg() -> dict | None:
    moomoo = _get_moomoo_cfg()
    if not moomoo:
        return None
    wl = moomoo.get("watchlist")
    if not isinstance(wl, dict):
        return None
    if not wl.get("enabled", False):
        return None
    return wl


def _get_watchlist_monitor_cfg() -> dict | None:
    moomoo = _get_moomoo_cfg()
    if not moomoo:
        return None
    mon = moomoo.get("watchlist_monitor")
    if not isinstance(mon, dict):
        return None
    if not mon.get("enabled", False):
        return None
    return mon


def parse_moomoo_code(code: str) -> tuple[str, str] | None:
    """moomoo code → (ticker, market)。JP/US のみ。"""
    code = (code or "").strip()
    if "." not in code:
        return None
    prefix, ticker = code.split(".", 1)
    market = _MARKET_PREFIX.get(prefix.upper())
    if not market:
        return None
    return ticker.strip().upper(), market


def _resolve_group_name(mon_cfg: dict, host: str, port: int) -> str | None:
    """設定とフォールバックから有効なグループ名を返す。"""
    candidates = [mon_cfg.get("group_name", "All")]
    fallbacks = mon_cfg.get("group_name_fallbacks") or ["All", "Favorites", "US"]
    for name in fallbacks:
        if name and name not in candidates:
            candidates.append(name)

    _ensure_sdk_path()
    from moomoo import OpenQuoteContext
    from moomoo.common.constant import RET_OK

    ctx = OpenQuoteContext(host=host, port=port)
    try:
        for name in candidates:
            if not name:
                continue
            ret, data = ctx.get_user_security(name)
            if ret != RET_OK:
                continue
            if data is not None and not data.empty:
                return name
        return None
    finally:
        ctx.close()


def fetch_watchlist_tickers(
    markets: list[str] | None = None,
) -> list[dict]:
    """
    moomoo お気に入りグループから銘柄一覧を取得する。

    返り値: [{"ticker", "market", "moomoo_code", "name"}, ...]
    """
    mon_cfg = _get_watchlist_monitor_cfg()
    if not mon_cfg:
        logger.debug("watchlist_monitor.enabled=false のためスキップ")
        return []

    moomoo_cfg = _get_moomoo_cfg() or {}
    host = moomoo_cfg.get("host", "127.0.0.1")
    port = int(moomoo_cfg.get("port", 11111))
    allowed = {m.upper() for m in (markets or mon_cfg.get("markets") or ["US"])}

    # SDK は OpenD 未起動だと接続を無期限に再試行して戻らないため、先に確認する
    if not is_port_open(host, port):
        logger.warning(f"moomoo OpenD に接続できません ({host}:{port})。お気に入り取得をスキップします")
        return []

    group_name = _resolve_group_name(mon_cfg, host, port)
    if not group_name:
        logger.warning("moomoo お気に入りグループが見つかりません")
        return []

    _ensure_sdk_path()
    from moomoo import OpenQuoteContext
    from moomoo.common.constant import RET_OK

    ctx = OpenQuoteContext(host=host, port=port)
    try:
        ret, data = ctx.get_user_security(group_name)
        if ret != RET_OK:
            logger.warning(f"get_user_security 失敗 ({group_name}): {data}")
            return []
        if data is None or data.empty:
            return []
        rows: list[dict] = []
        for _, row in data.iterrows():
            code = str(row.get("code", ""))
            parsed = parse_moomoo_code(code)
            if not parsed:
                continue
            ticker, market = parsed
            if market not in allowed:
                continue
            rows.append({
                "ticker": ticker,
                "market": market,
                "moomoo_code": code,
                "name": str(row.get("name", "") or ticker),
            })
        logger.info(f"moomoo お気に入り取得 ({group_name}): {len(rows)} 件")
        return rows
    finally:
        ctx.close()


def ticker_to_moomoo_code(ticker: str, market: str) -> str:
    """ticker + market → moomoo code（例: JP.7203, US.AAPL）。"""
    prefix = _MARKET_PREFIX.get((market or "").upper(), (market or "").upper())
    return f"{prefix}.{ticker.strip().upper()}"


def _parse_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%d").date()


def _days_since(last: str | date, today: date) -> int:
    last_d = _parse_date(last)
    if last_d is None:
        return 0
    return (today - last_d).days


def _row_key(ticker: str, market: str) -> str:
    return f"{(market or '').upper()}:{(ticker or '').strip().upper()}"


def load_tracked_rows() -> list[dict]:
    if not _SYNC_CSV.exists():
        return []
    rows: list[dict] = []
    with open(_SYNC_CSV, encoding="utf-8") as f:
        for line in csv.DictReader(f):
            ticker = (line.get("ticker") or "").strip().upper()
            market = (line.get("market") or "").strip().upper()
            if not ticker or not market:
                continue
            rows.append({
                "ticker": ticker,
                "market": market,
                "moomoo_code": (line.get("moomoo_code") or "").strip()
                or ticker_to_moomoo_code(ticker, market),
                "added_at": (line.get("added_at") or "").strip(),
                "last_candidate_date": (line.get("last_candidate_date") or "").strip(),
            })
    return rows


def save_tracked_rows(rows: list[dict]) -> None:
    sorted_rows = sorted(
        rows,
        key=lambda r: (r.get("market") or "", r.get("ticker") or ""),
    )
    with open(_SYNC_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for row in sorted_rows:
            writer.writerow({
                "ticker": row["ticker"],
                "market": row["market"],
                "moomoo_code": row["moomoo_code"],
                "added_at": row.get("added_at") or "",
                "last_candidate_date": row.get("last_candidate_date") or "",
            })


def fetch_candidate_tickers(market: str, target_date: date) -> set[str]:
    """daily_signals から candidate ティッカー集合を返す。"""
    target_str = target_date.strftime("%Y-%m-%d")
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            """SELECT ticker FROM daily_signals
               WHERE date = ? AND market = ? AND label = 'candidate'""",
            (target_str, market.upper()),
        )
    return {
        (r["ticker"] or "").strip().upper()
        for r in rows
        if r["ticker"]
    }


def _fetch_holding_tickers() -> set[str]:
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            "SELECT ticker FROM holdings WHERE is_closed = 0",
        )
    return {
        (r["ticker"] or "").strip().upper()
        for r in rows
        if r["ticker"]
    }


def _fetch_bookmark_tickers() -> set[str]:
    try:
        from src.screening_bookmarks import get_bookmark_tickers
        return {t.upper() for t in get_bookmark_tickers()}
    except Exception as e:
        logger.warning(f"ブックマーク取得スキップ: {e}")
        return set()


def plan_watchlist_sync(
    tracked_rows: list[dict],
    candidate_tickers: set[str],
    target_date: date,
    *,
    market: str,
    min_days_before_remove: int = _DEFAULT_MIN_DAYS,
    holding_tickers: set[str] | None = None,
    bookmark_tickers: set[str] | None = None,
    exclude_holdings: bool = True,
    exclude_bookmarked: bool = True,
) -> dict:
    """
    追加・更新・削除対象を決定する（API 呼び出しなし）。

    返り値: {add, update, remove, next_rows}
    """
    market_u = market.upper()
    candidates = {t.upper() for t in candidate_tickers}
    holdings = holding_tickers or set()
    bookmarks = bookmark_tickers or set()

    by_key = {_row_key(r["ticker"], r["market"]): dict(r) for r in tracked_rows}
    target_str = target_date.strftime("%Y-%m-%d")

    to_add: list[dict] = []
    to_update: list[str] = []
    to_remove: list[dict] = []

    for ticker in sorted(candidates):
        key = _row_key(ticker, market_u)
        if key in by_key:
            row = by_key[key]
            if row.get("last_candidate_date") != target_str:
                row["last_candidate_date"] = target_str
                to_update.append(key)
            continue
        code = ticker_to_moomoo_code(ticker, market_u)
        row = {
            "ticker": ticker,
            "market": market_u,
            "moomoo_code": code,
            "added_at": target_str,
            "last_candidate_date": target_str,
        }
        by_key[key] = row
        to_add.append(row)

    for key, row in list(by_key.items()):
        if row.get("market", "").upper() != market_u:
            continue
        ticker = row["ticker"]
        if ticker in candidates:
            continue
        days = _days_since(row.get("last_candidate_date"), target_date)
        if days < min_days_before_remove:
            continue
        if exclude_holdings and ticker in holdings:
            continue
        if exclude_bookmarked and ticker in bookmarks:
            continue
        to_remove.append(row)
        del by_key[key]

    return {
        "add": to_add,
        "update": to_update,
        "remove": to_remove,
        "next_rows": list(by_key.values()),
    }


def _ensure_sdk_path() -> None:
    sdk_path = str(_MOOMOO_SDK)
    if sdk_path not in sys.path:
        sys.path.insert(0, sdk_path)


def _modify_codes(
    host: str,
    port: int,
    group_name: str,
    op,
    codes: list[str],
) -> tuple[list[str], list[tuple[str, str]]]:
    """銘柄を1件ずつ操作し、成功・失敗を分けて返す。"""
    if not codes:
        return [], []
    _ensure_sdk_path()
    from moomoo import OpenQuoteContext
    from moomoo.common.constant import RET_OK

    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    ctx = OpenQuoteContext(host=host, port=port)
    try:
        for code in codes:
            _wait_for_modify_slot()
            ret, msg = ctx.modify_user_security(group_name, op, [code])
            if ret == RET_OK:
                succeeded.append(code)
            else:
                failed.append((code, str(msg)))
        return succeeded, failed
    finally:
        ctx.close()


def _group_exists(host: str, port: int, group_name: str) -> bool:
    _ensure_sdk_path()
    from moomoo import OpenQuoteContext
    from moomoo.common.constant import RET_OK, UserSecurityGroupType

    ctx = OpenQuoteContext(host=host, port=port)
    try:
        ret, data = ctx.get_user_security_group(UserSecurityGroupType.CUSTOM)
        if ret != RET_OK:
            logger.warning(f"moomoo お気に入りグループ一覧取得失敗: {data}")
            return False
        if data is None or data.empty:
            return False
        names = {str(n).strip() for n in data.get("group_name", [])}
        if group_name not in names:
            logger.warning(
                f"moomoo カスタムグループ一覧: {sorted(names)} "
                f"（期待: {group_name!r}）"
            )
        return group_name in names
    finally:
        ctx.close()


def sync_watchlist(market: str, target_date: date) -> dict:
    """
    candidate 銘柄を moomoo「システム候補」グループへ同期する。

    返り値: {added, updated, removed, skipped}
    """
    wl_cfg = _get_watchlist_cfg()
    if not wl_cfg:
        logger.debug("moomoo お気に入り同期: watchlist.enabled=false のためスキップ")
        return {"added": 0, "updated": 0, "removed": 0, "skipped": True}

    moomoo_cfg = _get_moomoo_cfg() or {}
    host = moomoo_cfg.get("host", "127.0.0.1")
    port = int(moomoo_cfg.get("port", 11111))
    group_name = wl_cfg.get("group_name", _DEFAULT_GROUP)
    min_days = int(wl_cfg.get("min_days_before_remove", _DEFAULT_MIN_DAYS))
    exclude_holdings = bool(wl_cfg.get("exclude_holdings", True))
    exclude_bookmarked = bool(wl_cfg.get("exclude_bookmarked", True))

    market_u = market.upper()
    target_str = target_date.strftime("%Y-%m-%d")
    logger.info(f"moomoo お気に入り同期開始 ({market_u} {target_str})")

    if market_u not in _WATCHLIST_API_SUPPORTED_MARKETS:
        logger.warning(
            f"moomoo お気に入り同期 ({market_u}): OpenAPI の modify_user_security は "
            f"{'日本株' if market_u == 'JP' else market_u} に未対応のためスキップします。"
            "US 候補は朝バッチで同期されます。"
        )
        return {
            "added": 0,
            "updated": 0,
            "removed": 0,
            "skipped": True,
            "error": "market_not_supported",
        }

    if not is_port_open(host, port):
        logger.warning(f"moomoo OpenD に接続できません ({host}:{port})。お気に入り同期をスキップします")
        return {"added": 0, "updated": 0, "removed": 0, "skipped": True, "error": "opend_unreachable"}

    candidates = fetch_candidate_tickers(market_u, target_date)
    tracked = load_tracked_rows()

    plan = plan_watchlist_sync(
        tracked,
        candidates,
        target_date,
        market=market_u,
        min_days_before_remove=min_days,
        holding_tickers=_fetch_holding_tickers(),
        bookmark_tickers=_fetch_bookmark_tickers(),
        exclude_holdings=exclude_holdings,
        exclude_bookmarked=exclude_bookmarked,
    )

    if not plan["add"] and not plan["remove"]:
        save_tracked_rows(plan["next_rows"])
        logger.info(
            f"moomoo お気に入り同期 ({market_u} {target_str}): "
            f"変更なし（candidate={len(candidates)} 件 / 追跡={len(tracked)} 件）"
        )
        return {
            "added": 0,
            "updated": len(plan["update"]),
            "removed": 0,
            "skipped": False,
        }

    if not _group_exists(host, port, group_name):
        logger.error(
            f"moomoo お気に入りグループ「{group_name}」が見つかりません。"
            "moomoo アプリでカスタムグループを作成してください。"
        )
        return {"added": 0, "updated": 0, "removed": 0, "skipped": True, "error": "group_not_found"}

    _ensure_sdk_path()
    from moomoo.common.constant import ModifyUserSecurityOp

    added = 0
    removed = 0

    add_codes = [r["moomoo_code"] for r in plan["add"]]
    if add_codes:
        ok_codes, fail_codes = _modify_codes(
            host, port, group_name, ModifyUserSecurityOp.ADD, add_codes,
        )
        if fail_codes:
            for code, msg in fail_codes:
                logger.warning(f"moomoo お気に入り追加失敗: {code} ({msg})")
        if ok_codes:
            added = len(ok_codes)
            ok_set = set(ok_codes)
            for row in plan["add"]:
                if row["moomoo_code"] in ok_set:
                    logger.info(f"  [お気に入り追加] {row['moomoo_code']}")
            # 追加に失敗した銘柄は CSV に残さない
            ok_keys = {
                _row_key(r["ticker"], r["market"])
                for r in plan["add"]
                if r["moomoo_code"] in ok_set
            }
            plan["next_rows"] = [
                r for r in plan["next_rows"]
                if _row_key(r["ticker"], r["market"]) not in {
                    _row_key(a["ticker"], a["market"]) for a in plan["add"]
                } or _row_key(r["ticker"], r["market"]) in ok_keys
            ]
        elif plan["add"]:
            return {
                "added": 0,
                "updated": 0,
                "removed": 0,
                "skipped": True,
                "error": fail_codes[0][1] if fail_codes else "add_failed",
            }

    remove_codes = [r["moomoo_code"] for r in plan["remove"]]
    if remove_codes:
        ok_codes, fail_codes = _modify_codes(
            host, port, group_name, ModifyUserSecurityOp.DEL, remove_codes,
        )
        if fail_codes:
            for code, msg in fail_codes:
                logger.warning(f"moomoo お気に入り削除失敗: {code} ({msg})")
        ok_set = set(ok_codes)
        removed = len(ok_codes)
        for row in plan["remove"]:
            if row["moomoo_code"] in ok_set:
                logger.info(f"  [お気に入り削除] {row['moomoo_code']}")
        # 削除に失敗した銘柄は CSV に残し、次回再試行する（全件失敗でも追加が成功していれば保存に進むため）
        plan["next_rows"].extend(
            r for r in plan["remove"] if r["moomoo_code"] not in ok_set
        )
        if not ok_codes and not added:
            return {
                "added": added,
                "updated": len(plan["update"]),
                "removed": 0,
                "skipped": True,
                "error": fail_codes[0][1] if fail_codes else "remove_failed",
            }

    save_tracked_rows(plan["next_rows"])
    logger.info(
        f"moomoo お気に入り同期完了 ({market_u} {target_str}): "
        f"追加 {added} / 更新 {len(plan['update'])} / 削除 {removed}"
    )
    return {
        "added": added,
        "updated": len(plan["update"]),
        "removed": removed,
        "skipped": False,
    }
