"""
moomoo OpenD からポジションを取得して holdings.csv を更新するモジュール

処理フロー:
1. moomoo OpenD に接続してポジション一覧を取得
2. code ("JP.3687", "US.GOOG") を ticker + market に変換
3. 既存 holdings.csv とマージ（stop_loss_price / memo / entry_date は既存値を優先）
4. holdings.csv を上書き保存

使い方:
    from src.moomoo_sync import sync_from_moomoo
    added, updated = sync_from_moomoo()          # CSV上書き
    added, updated = sync_from_moomoo(dry_run=True)  # 上書きしない
"""

import csv
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MOOMOO_SDK = _PROJECT_ROOT / "MMAPI4Python_10.2.6208"
_HOLDINGS_CSV = _PROJECT_ROOT / "holdings.csv"

_CSV_HEADER = ["ticker", "name", "market", "entry_price", "entry_date",
               "quantity", "stop_loss_price", "memo"]

# moomoo code → market マッピング
_MARKET_PREFIX = {"JP": "JP", "US": "US", "HK": "HK", "SH": "JP", "SZ": "JP"}

from shared.net import is_port_open
from src.config import CONFIG
from src.data_store import fetch_all, fetch_one, get_connection, insert_one
from src.logger import get_logger

logger = get_logger(__name__)


def _get_moomoo_cfg() -> dict | None:
    """config.json から moomoo セクションを取得。なければ None。"""
    cfg = CONFIG.get("moomoo")
    if not cfg:
        return None
    return cfg


def _opend_reachable(host: str, port: int) -> bool:
    """
    OpenD のポートに接続できるかを確認する。

    moomoo SDK は OpenD が起動していないと接続を無期限に再試行して処理が戻らず、
    バッチ全体がタイムアウトする。SDK を呼ぶ前にここで弾く。
    """
    if is_port_open(host, port):
        return True
    logger.warning(f"moomoo OpenD に接続できません ({host}:{port})。起動していないためスキップします")
    return False


def _fetch_positions(host: str, port: int) -> list[dict]:
    """
    moomoo OpenD からポジション一覧を取得する。

    返り値: [
        {"ticker": "3687", "name": "フィックスターズ", "market": "JP",
         "qty": 100, "cost_price": 1500.0, "cost_price_valid": True,
         "average_cost": 1500.0, "market_val": 160000.0, "pl_val": 10000.0},
        ...
    ]
    """
    # moomoo SDK を sys.path に追加
    sdk_path = str(_MOOMOO_SDK)
    if sdk_path not in sys.path:
        sys.path.insert(0, sdk_path)

    from moomoo import OpenSecTradeContext
    from moomoo.common.constant import RET_OK, TrdEnv, TrdMarket, SecurityFirm

    # 券商設定（デフォルト: moomoo証券 JP）
    cfg = _get_moomoo_cfg() or {}
    firm_name = cfg.get("security_firm", "FUTUJP")
    security_firm = getattr(SecurityFirm, firm_name, SecurityFirm.FUTUJP)

    results = []

    # JP と US をそれぞれ取得（口座が分かれている可能性があるため）
    for trd_market, market_label in [
        (TrdMarket.JP, "JP"),
        (TrdMarket.US, "US"),
    ]:
        try:
            ctx = OpenSecTradeContext(
                filter_trdmarket=trd_market,
                host=host,
                port=port,
                security_firm=security_firm,
            )
        except Exception as e:
            logger.warning(f"moomoo OpenD 接続失敗 ({market_label}): {e}")
            continue

        try:
            ret, data = ctx.position_list_query(trd_env=TrdEnv.REAL)
            if ret != RET_OK:
                logger.warning(f"moomoo ポジション取得失敗 ({market_label}): {data}")
                continue

            for _, row in data.iterrows():
                code = str(row.get("code", ""))
                if not code or "." not in code:
                    continue

                prefix, ticker = code.split(".", 1)
                market = _MARKET_PREFIX.get(prefix, prefix)
                qty = float(row.get("qty", 0))

                # ゼロポジションはスキップ
                if qty <= 0:
                    continue

                cost_valid = row.get("cost_price_valid", False)
                cost = float(row.get("cost_price", 0)) if cost_valid else float(row.get("average_cost", 0))

                results.append({
                    "ticker": ticker,
                    "name": str(row.get("stock_name", "")).strip(),
                    "market": market,
                    "qty": qty,
                    "cost_price": cost,
                    "market_val": float(row.get("market_val", 0)),
                    "pl_val": float(row.get("pl_val", 0)),
                })
        except Exception as e:
            logger.warning(f"moomoo ポジション処理中エラー ({market_label}): {e}")
        finally:
            ctx.close()

    return results


def fetch_account_cash() -> dict[str, float]:
    """
    moomoo OpenD から口座の現金残高（円）を取得する。

    moomoo証券JP（FUTUJP）は円建て口座のため、JP口座から1回だけ取得する。
    JP/US市場で同じ現金が通貨違いで返されるため、二重計上を避ける。

    返り値: {"cash_jpy": float, "cash_usd": float}
    moomoo 未接続時や取得失敗時は各値が 0.0 のまま返す。
    """
    result = {"cash_jpy": 0.0, "cash_usd": 0.0}

    cfg = _get_moomoo_cfg()
    if not cfg:
        return result

    host = cfg.get("host", "127.0.0.1")
    port = int(cfg.get("port", 11111))
    if not _opend_reachable(host, port):
        return result

    sdk_path = str(_MOOMOO_SDK)
    if sdk_path not in sys.path:
        sys.path.insert(0, sdk_path)

    from moomoo import OpenSecTradeContext
    from moomoo.common.constant import RET_OK, TrdEnv, TrdMarket, SecurityFirm, Currency

    firm_name = cfg.get("security_firm", "FUTUJP")
    security_firm = getattr(SecurityFirm, firm_name, SecurityFirm.FUTUJP)

    # 円建て口座（FUTUJP）のため JP 口座から円建てで1回だけ取得する
    try:
        ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.JP,
            host=host, port=port,
            security_firm=security_firm,
        )
    except Exception as e:
        logger.warning(f"moomoo OpenD 接続失敗: {e}")
        return result

    try:
        ret, data = ctx.accinfo_query(trd_env=TrdEnv.REAL, currency=Currency.JPY)
        if ret != RET_OK:
            logger.warning(f"moomoo 口座情報取得失敗: {data}")
            return result

        if data is not None and len(data) > 0:
            cash_val = data.iloc[0].get("cash", 0)
            if cash_val and str(cash_val) != "N/A":
                result["cash_jpy"] = float(cash_val)
                logger.info(f"moomoo 現金残高: ¥{result['cash_jpy']:,.0f}")
    except Exception as e:
        logger.warning(f"moomoo 口座情報処理中エラー: {e}")
    finally:
        ctx.close()

    return result


def _read_existing_csv() -> dict[str, dict]:
    """既存 holdings.csv を ticker をキーにした辞書で返す。"""
    existing = {}
    if not _HOLDINGS_CSV.exists():
        return existing

    with open(_HOLDINGS_CSV, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticker = row.get("ticker", "").strip()
            if ticker:
                existing[ticker] = row
    return existing


def _merge_positions(existing: dict[str, dict], positions: list[dict]) -> list[dict]:
    """
    moomoo ポジションと既存 CSV をマージする。

    - 既存行の stop_loss_price / memo / entry_date は保持
    - 数量・コスト・名前は moomoo の最新値で上書き
    - 新規銘柄は entry_date を今日に仮設定
    """
    today_str = date.today().strftime("%Y-%m-%d")
    merged = {}

    # moomoo ポジションをベースに構築
    for pos in positions:
        ticker = pos["ticker"]
        old = existing.get(ticker, {})

        merged[ticker] = {
            "ticker": ticker,
            "name": pos["name"] or old.get("name", ""),
            "market": pos["market"],
            "entry_price": pos["cost_price"],
            "entry_date": old.get("entry_date") or today_str,
            "quantity": pos["qty"],
            "stop_loss_price": old.get("stop_loss_price", ""),
            "memo": old.get("memo", "moomoo同期"),
        }

    return list(merged.values())


def _write_csv(rows: list[dict]) -> None:
    """holdings.csv を上書き保存する。"""
    with open(_HOLDINGS_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)


def sync_from_moomoo(dry_run: bool = False) -> tuple[int, int]:
    """
    moomoo OpenD からポジションを取得して holdings.csv を更新する。

    引数:
        dry_run: True の場合は CSV を上書きしない（ログ出力のみ）

    返り値: (追加件数, 更新件数)
    """
    cfg = _get_moomoo_cfg()
    if not cfg:
        logger.info("config.json に moomoo セクションがないためスキップ")
        return 0, 0

    host = cfg.get("host", "127.0.0.1")
    port = int(cfg.get("port", 11111))

    if not _opend_reachable(host, port):
        return 0, 0

    logger.info(f"moomoo OpenD に接続中... ({host}:{port})")
    positions = _fetch_positions(host, port)

    if not positions:
        logger.warning("moomoo からポジションが取得できませんでした")
        return 0, 0

    logger.info(f"moomoo から {len(positions)} 件のポジションを取得")

    existing = _read_existing_csv()
    merged = _merge_positions(existing, positions)

    # 追加 / 更新の集計
    added = 0
    updated = 0
    for row in merged:
        ticker = row["ticker"]
        if ticker not in existing:
            added += 1
            logger.info(f"  [新規] {ticker} ({row['name']}) {row['market']} "
                        f"qty={row['quantity']} cost={row['entry_price']}")
        else:
            updated += 1
            logger.info(f"  [更新] {ticker} ({row['name']}) {row['market']} "
                        f"qty={row['quantity']} cost={row['entry_price']}")

    if dry_run:
        logger.info(f"[dry-run] CSV 上書きをスキップ（追加 {added} / 更新 {updated}）")
        for row in merged:
            logger.info(f"  {row}")
    else:
        _write_csv(merged)
        logger.info(f"holdings.csv を更新しました（追加 {added} / 更新 {updated}）")

    return added, updated


# ============================================================
# 約定履歴同期（trade_result_log への自動記録）
# ============================================================


def _fetch_deal_history(host: str, port: int) -> list[dict]:
    """
    moomoo OpenD から過去90日の約定履歴を取得する。

    返り値: [
        {"ticker": "3687", "market": "JP", "price": 1500.0,
         "qty": 100, "trd_side": "BUY", "create_time": "2025-01-15 10:30:00",
         "deal_id": "12345", "order_id": "ORD-001"},
        ...
    ]
    """
    sdk_path = str(_MOOMOO_SDK)
    if sdk_path not in sys.path:
        sys.path.insert(0, sdk_path)

    from moomoo import OpenSecTradeContext
    from moomoo.common.constant import RET_OK, TrdEnv, TrdMarket, SecurityFirm

    cfg = _get_moomoo_cfg() or {}
    firm_name = cfg.get("security_firm", "FUTUJP")
    security_firm = getattr(SecurityFirm, firm_name, SecurityFirm.FUTUJP)

    results: list[dict] = []
    end_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")

    for trd_market, market_label in [
        (TrdMarket.JP, "JP"),
        (TrdMarket.US, "US"),
    ]:
        try:
            ctx = OpenSecTradeContext(
                filter_trdmarket=trd_market,
                host=host,
                port=port,
                security_firm=security_firm,
            )
        except Exception as e:
            logger.warning(f"moomoo OpenD 接続失敗 ({market_label}): {e}")
            continue

        try:
            ret, data = ctx.history_deal_list_query(
                trd_env=TrdEnv.REAL,
                start=start_date,
                end=end_date,
            )
            if ret != RET_OK:
                logger.warning(f"moomoo 約定履歴取得失敗 ({market_label}): {data}")
                continue

            for _, row in data.iterrows():
                code = str(row.get("code", ""))
                if not code or "." not in code:
                    continue

                prefix, ticker = code.split(".", 1)
                market = _MARKET_PREFIX.get(prefix, prefix)

                results.append({
                    "ticker": ticker,
                    "market": market,
                    "price": float(row.get("price", 0)),
                    "qty": float(row.get("qty", 0)),
                    "trd_side": str(row.get("trd_side", "")),
                    "create_time": str(row.get("create_time", "")),
                    "deal_id": str(row.get("deal_id", "")),
                    "order_id": str(row.get("order_id", "")),
                })
        except Exception as e:
            logger.warning(f"moomoo 約定履歴処理中エラー ({market_label}): {e}")
        finally:
            ctx.close()

    return results


def _find_matching_signal(ticker: str, trade_date: str) -> dict | None:
    """約定日から遡って最も近い trade_signal_log エントリを返す。"""
    with get_connection() as conn:
        row = fetch_one(
            conn,
            """SELECT id, signal_date, pattern, entry_price, stop_loss_price,
                      target_price, market
               FROM trade_signal_log
               WHERE ticker = ? AND signal_date <= ?
               ORDER BY signal_date DESC LIMIT 1""",
            (ticker, trade_date),
        )
    return dict(row) if row else None


def _is_duplicate_result(ticker: str, exit_date: str, exit_price: float) -> bool:
    """trade_result_log に同じトレードが既に存在するか確認する。"""
    with get_connection() as conn:
        row = fetch_one(
            conn,
            """SELECT COUNT(*) AS cnt FROM trade_result_log
               WHERE ticker = ? AND actual_exit_date = ?
                 AND ABS(actual_exit_price - ?) < 0.01""",
            (ticker, exit_date, exit_price),
        )
    return (row["cnt"] if row else 0) > 0


def _create_manual_signal(ticker: str, market: str, entry_price: float,
                          entry_date: str) -> int:
    """シグナルが存在しない場合に手動エントリー用のシグナルを作成する。"""
    stop_loss = round(entry_price * 0.92, 2)
    target = round(entry_price * 1.16, 2)
    row = {
        "signal_date": entry_date,
        "ticker": ticker,
        "name": "",
        "market": market,
        "pattern": "A",
        "entry_price": entry_price,
        "stop_loss_price": stop_loss,
        "stop_loss_pct": -8.0,
        "target_price": target,
        "recommended_amount": None,
        "market_mode": "normal",
        "conditions_met": "moomoo_sync",
    }
    with get_connection() as conn:
        return insert_one(conn, "trade_signal_log", row)


def sync_trade_results() -> int:
    """
    moomoo OpenD から約定履歴を取得して trade_result_log に同期する。

    処理フロー:
    1. 約定履歴を取得
    2. SELL 約定ごとに、同じ ticker の BUY 約定をペアリング
    3. trade_signal_log から対応するシグナルを検索
    4. 重複チェック後、trade_result_log に INSERT

    返り値: 新規記録件数
    """
    cfg = _get_moomoo_cfg()
    if not cfg:
        logger.info("config.json に moomoo セクションがないためスキップ")
        return 0

    host = cfg.get("host", "127.0.0.1")
    port = int(cfg.get("port", 11111))

    if not _opend_reachable(host, port):
        return 0

    logger.info("moomoo 約定履歴を取得中...")
    deals = _fetch_deal_history(host, port)

    if not deals:
        logger.info("moomoo から約定履歴が取得できませんでした")
        return 0

    logger.info(f"moomoo から {len(deals)} 件の約定を取得")

    # BUY と SELL をティッカーごとにグループ化
    buys: dict[str, list[dict]] = {}
    sells: dict[str, list[dict]] = {}
    for d in deals:
        side = d["trd_side"].upper()
        ticker = d["ticker"]
        if "BUY" in side:
            buys.setdefault(ticker, []).append(d)
        elif "SELL" in side:
            sells.setdefault(ticker, []).append(d)

    recorded = 0

    for ticker, sell_list in sells.items():
        buy_list = buys.get(ticker, [])

        for sell in sell_list:
            exit_price = sell["price"]
            exit_date_raw = sell["create_time"]
            # "2025-01-15 10:30:00" → "2025-01-15"
            exit_date = exit_date_raw[:10] if len(exit_date_raw) >= 10 else exit_date_raw

            # 重複チェック
            if _is_duplicate_result(ticker, exit_date, exit_price):
                continue

            # BUY 約定からエントリー価格を推定（直近の BUY を使用）
            entry_price = None
            entry_date = None
            for buy in sorted(buy_list, key=lambda b: b["create_time"]):
                buy_date = buy["create_time"][:10]
                if buy_date <= exit_date:
                    entry_price = buy["price"]
                    entry_date = buy_date
            # BUY が見つからない場合はスキップ
            if entry_price is None:
                logger.warning(f"  {ticker}: 対応する BUY 約定が見つかりません。スキップ")
                continue

            # シグナル検索
            signal = _find_matching_signal(ticker, exit_date)
            if signal:
                signal_id = signal["id"]
            else:
                # シグナルがない場合は手動エントリーとして作成
                signal_id = _create_manual_signal(
                    ticker, sell["market"], entry_price, entry_date,
                )
                logger.info(f"  {ticker}: シグナルなし → 手動エントリー作成 (id={signal_id})")

            # P&L 計算
            pnl_pct = round((exit_price - entry_price) / entry_price * 100, 2)
            qty = sell["qty"]
            pnl_amount = round((exit_price - entry_price) * qty, 2)

            # INSERT
            row = {
                "signal_id": signal_id,
                "ticker": ticker,
                "actual_entry_price": entry_price,
                "actual_entry_date": entry_date,
                "actual_exit_price": exit_price,
                "actual_exit_date": exit_date,
                "exit_reason": "manual",
                "pnl_pct": pnl_pct,
                "pnl_amount": pnl_amount,
                "memo": "moomoo同期",
            }
            with get_connection() as conn:
                insert_one(conn, "trade_result_log", row)

            # holdings も決済済みに更新
            with get_connection() as conn:
                conn.execute(
                    "UPDATE holdings SET is_closed=1, close_price=?, close_date=?, "
                    "close_reason=?, updated_at=? WHERE ticker=? AND is_closed=0",
                    (exit_price, exit_date, "manual",
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ticker),
                )

            sign = "+" if pnl_pct >= 0 else ""
            logger.info(f"  [記録] {ticker}: {entry_price} → {exit_price} ({sign}{pnl_pct}%)")
            recorded += 1

    logger.info(f"moomoo 約定履歴同期完了: {recorded} 件を新規記録")
    return recorded
