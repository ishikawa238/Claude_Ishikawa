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
from datetime import date
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MOOMOO_SDK = _PROJECT_ROOT / "MMAPI4Python_10.2.6208"
_HOLDINGS_CSV = _PROJECT_ROOT / "holdings.csv"

_CSV_HEADER = ["ticker", "name", "market", "entry_price", "entry_date",
               "quantity", "stop_loss_price", "memo"]

# moomoo code → market マッピング
_MARKET_PREFIX = {"JP": "JP", "US": "US", "HK": "HK", "SH": "JP", "SZ": "JP"}

from src.config import CONFIG
from src.logger import get_logger

logger = get_logger(__name__)


def _get_moomoo_cfg() -> dict | None:
    """config.json から moomoo セクションを取得。なければ None。"""
    cfg = CONFIG.get("moomoo")
    if not cfg:
        return None
    return cfg


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
