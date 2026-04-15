"""
moomoo（富途証券）保有ポジション同期スクリプト

実行方法（investment_support/ ディレクトリから）:
    python -m scripts.sync_from_moomoo

前提条件:
    1. moomoo アプリをインストール済みであること
    2. OpenD（moomoo が提供するローカルデーモン）が起動していること
       ダウンロード: https://www.moomoo.com/OpenAPI/
    3. futu-api がインストール済みであること（pip install futu-api）

処理内容:
    - moomoo の現在の保有ポジション一覧を取得する
    - holdings.csv を更新する（新規追加・数量/価格の更新）
    - 既存行の stop_loss_price は上書きしない（ユーザーが手動設定した値を保持）
    - moomoo にないポジションは holdings.csv から削除しない（手動管理分を保護）
"""

import csv
import sys
from datetime import date
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import CONFIG
from src.logger import get_logger

logger = get_logger(__name__)

_HOLDINGS_CSV = _PROJECT_ROOT / "holdings.csv"
_CSV_HEADER   = ["ticker", "name", "market", "entry_price", "entry_date",
                 "quantity", "stop_loss_price", "memo"]

# OpenD の接続設定（config.json の moomoo セクションから取得）
_MOOMOO_CFG   = CONFIG.get("moomoo", {})
_OPEND_HOST   = _MOOMOO_CFG.get("host", "127.0.0.1")
_OPEND_PORT   = int(_MOOMOO_CFG.get("port", 11111))
_SECURITY_FIRM = _MOOMOO_CFG.get("security_firm", "")

_ENV_AUTO = "AUTO"
_ENV_REAL = "REAL"
_ENV_SIMULATE = "SIMULATE"

# moomoo の市場コード → 内部市場コードのマッピング
_MARKET_MAP = {
    "US": "US",   # 米国株
    "JP": "JP",   # 日本株（東証）
    "HK": None,   # 香港株（現行システム未対応）
    "SH": None,   # 中国A株（上海）
    "SZ": None,   # 中国A株（深圳）
}


def _iter_market_codes() -> list[str]:
    return [code for code in dict.fromkeys(_MARKET_MAP.keys())]


def _get_supported_trade_markets(ft, market_codes: list[str] | None = None) -> list[tuple[str, str]]:
    """現在の moomoo SDK で利用可能な取引市場を返す。"""
    markets = []
    for market_code in market_codes or _iter_market_codes():
        internal_market = _MARKET_MAP.get(market_code)
        if internal_market is None:
            continue
        trd_market = getattr(ft.TrdMarket, market_code, None)
        if trd_market is None:
            logger.warning(f"TrdMarket.{market_code} が存在しないためスキップします")
            continue
        markets.append((internal_market, trd_market))
    return markets


def _normalize_env_name(value) -> str:
    return str(value).strip().upper()


def _select_trade_profile(account_rows: list[dict], requested_env: str = _ENV_AUTO) -> tuple[str | None, list[str], list[str]]:
    """利用可能な口座一覧から照会対象の取引環境と市場を決める。"""
    requested = _normalize_env_name(requested_env or _ENV_AUTO)
    env_order = [requested] if requested in (_ENV_REAL, _ENV_SIMULATE) else [_ENV_REAL, _ENV_SIMULATE]
    unsupported_markets = set()

    for env_name in env_order:
        supported_markets = []
        seen_markets = set()
        for row in account_rows:
            if _normalize_env_name(row.get("trd_env")) != env_name:
                continue
            for market_code in row.get("trdmarket_auth") or []:
                internal_market = _MARKET_MAP.get(market_code)
                if internal_market is None:
                    if market_code in _MARKET_MAP:
                        unsupported_markets.add(market_code)
                    continue
                if internal_market not in seen_markets:
                    supported_markets.append(internal_market)
                    seen_markets.add(internal_market)
        if supported_markets:
            return env_name, supported_markets, sorted(unsupported_markets)

    return None, [], sorted(unsupported_markets)


def _resolve_security_firm(ft):
    """config の security_firm 文字列を SDK の SecurityFirm 値に変換する。"""
    if not _SECURITY_FIRM:
        return ft.SecurityFirm.FUTUSECURITIES
    val = getattr(ft.SecurityFirm, _SECURITY_FIRM, None)
    if val is None:
        logger.warning(f"SecurityFirm '{_SECURITY_FIRM}' が不明です。デフォルトを使用します")
        return ft.SecurityFirm.FUTUSECURITIES
    return val


def _fetch_account_rows(ft) -> tuple[list[dict], str | None]:
    ctx = None
    try:
        ctx = ft.OpenSecTradeContext(
            filter_trdmarket=ft.TrdMarket.NONE,
            host=_OPEND_HOST,
            port=_OPEND_PORT,
            security_firm=_resolve_security_firm(ft),
        )
        ret, data = ctx.get_acc_list()
        if ret != ft.RET_OK:
            return [], f"口座一覧の取得に失敗しました: {data}"
        return data.to_dict("records"), None
    except Exception as e:
        return [], f"口座一覧の取得中にエラーが発生しました: {e}"
    finally:
        if ctx is not None:
            ctx.close()


def _strip_market_prefix(code: str) -> tuple[str, str]:
    """
    moomoo のコード形式から市場とティッカーを抽出する。
    例: "US.AAPL" → ("US", "AAPL")
        "JP.7203" → ("JP", "7203")
    """
    if "." in code:
        market, ticker = code.split(".", 1)
        return market.upper(), ticker
    return "US", code


def _load_existing_csv() -> dict[str, dict]:
    """既存の holdings.csv を読み込んで {ticker: row} の辞書で返す。"""
    if not _HOLDINGS_CSV.exists():
        return {}
    result = {}
    with open(_HOLDINGS_CSV, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("ticker"):
                result[row["ticker"]] = row
    return result


def _save_csv(rows: list[dict]) -> None:
    """holdings.csv を保存する。"""
    with open(_HOLDINGS_CSV, encoding="utf-8", newline="") as f:
        pass  # 存在確認
    with open(_HOLDINGS_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_HEADER, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def fetch_positions_from_moomoo(trade_env: str = _ENV_AUTO) -> tuple[list[dict], str | None]:
    """
    moomoo OpenAPI から保有ポジション一覧を取得する。
    返り値: [{"ticker": "AAPL", "market": "US", "name": "...", ...}, ...]
    """
    try:
        import moomoo as ft
    except ImportError:
        logger.error("moomoo-api がインストールされていません: pip install moomoo-api")
        return [], "moomoo-api がインストールされていません。"

    account_rows, account_error = _fetch_account_rows(ft)
    if account_error:
        logger.error(account_error)
        return [], account_error

    selected_env, available_market_codes, unsupported_markets = _select_trade_profile(account_rows, trade_env)
    if unsupported_markets:
        logger.warning(
            "現行システム未対応の市場権限を検出しました: " + ", ".join(unsupported_markets)
        )

    if selected_env is None:
        message = "利用可能な証券口座が見つかりませんでした。"
        if trade_env and _normalize_env_name(trade_env) in (_ENV_REAL, _ENV_SIMULATE):
            message = f"{_normalize_env_name(trade_env)} 環境で利用可能な US/JP 口座が見つかりませんでした。"
        logger.error(message)
        return [], message

    trade_markets = _get_supported_trade_markets(ft, available_market_codes)
    if not trade_markets:
        logger.error("利用可能な取引市場が moomoo SDK から見つかりませんでした")
        return [], "利用可能な取引市場が moomoo SDK から見つかりませんでした。"

    positions = []
    logger.info(
        f"moomoo の照会環境を選択: env={selected_env}, markets={','.join(market for market, _ in trade_markets)}"
    )

    failures = []
    queried_market_count = 0

    for market_code, trd_market in trade_markets:
        trd_ctx = None
        try:
            trd_ctx = ft.OpenSecTradeContext(
                filter_trdmarket=trd_market,
                host=_OPEND_HOST,
                port=_OPEND_PORT,
                security_firm=_resolve_security_firm(ft),
            )
            queried_market_count += 1
            ret, data = trd_ctx.position_list_query(trd_env=getattr(ft.TrdEnv, selected_env))
            if ret != ft.RET_OK:
                message = f"{market_code} 市場のポジション取得失敗: {data}"
                logger.warning(message)
                failures.append(message)
                continue

            for _, row in data.iterrows():
                code = str(row.get("code", ""))
                market_raw, ticker = _strip_market_prefix(code)
                market = _MARKET_MAP.get(market_raw)

                if market is None:
                    logger.info(f"  スキップ（対象外市場 {market_raw}）: {code}")
                    continue

                qty = float(row.get("qty", 0))
                cost_price = float(row.get("cost_price", 0))
                name = str(row.get("stock_name", "")).strip()

                if qty <= 0 or cost_price <= 0:
                    continue

                positions.append({
                    "ticker": ticker,
                    "market": market,
                    "name": name,
                    "entry_price": round(cost_price, 4),
                    "quantity": qty,
                    "entry_date": date.today().strftime("%Y-%m-%d"),
                })
        except Exception as e:
            message = f"{market_code} 市場への接続または取得に失敗しました: {e}"
            logger.warning(message)
            failures.append(message)
        finally:
            if trd_ctx is not None:
                trd_ctx.close()

    logger.info(f"moomoo からポジション取得完了: {len(positions)} 件")
    if positions:
        return positions, None
    if failures:
        return [], " / ".join(failures)
    if queried_market_count > 0:
        return [], None
    return [], "OpenD への接続または市場照会に失敗しました。"


def sync(dry_run: bool = False, trade_env: str = _ENV_AUTO) -> None:
    """
    moomoo のポジションを holdings.csv に同期する。

    dry_run=True の場合は変更内容を表示するのみで CSV を書き換えない。
    """
    print("\n" + "=" * 55)
    print("  moomoo → holdings.csv 同期")
    print("=" * 55)

    # moomoo からポジション取得
    moomoo_positions, fetch_error = fetch_positions_from_moomoo(trade_env=trade_env)
    if not moomoo_positions:
        print("  moomoo からポジションを取得できませんでした。")
        if fetch_error:
            print(f"  詳細: {fetch_error}")
        else:
            print("  対象市場に保有ポジションがない可能性があります。")
        return

    # 既存 CSV 読み込み
    existing = _load_existing_csv()

    added   = []
    updated = []

    default_sl_pct = float(CONFIG["capital"]["default_stop_loss_pct"]) / 100

    for pos in moomoo_positions:
        ticker = pos["ticker"]
        if ticker in existing:
            # 既存行: entry_price と quantity のみ更新。stop_loss_price は保持
            old = existing[ticker]
            old["entry_price"] = pos["entry_price"]
            old["quantity"]    = pos["quantity"]
            old["name"]        = pos["name"] or old.get("name", "")
            # entry_date は既存を保持（最初に買った日を記録しておくため）
            updated.append(ticker)
            print(f"  更新: {ticker}  取得単価={pos['entry_price']}  数量={pos['quantity']}")
        else:
            # 新規: stop_loss_price を自動設定（-8%）
            sl = round(pos["entry_price"] * (1 - default_sl_pct), 2)
            new_row = {
                "ticker":          ticker,
                "name":            pos["name"],
                "market":          pos["market"],
                "entry_price":     pos["entry_price"],
                "entry_date":      date.today().strftime("%Y-%m-%d"),
                "quantity":        pos["quantity"],
                "stop_loss_price": sl,
                "memo":            "moomoo同期",
            }
            existing[ticker] = new_row
            added.append(ticker)
            print(f"  追加: {ticker}  {pos['name']}  取得単価={pos['entry_price']}"
                  f"  数量={pos['quantity']}  損切り={sl}（-{default_sl_pct*100:.0f}%自動設定）")

    print(f"\n  追加: {len(added)} 件  更新: {len(updated)} 件")

    if dry_run:
        print("\n  [DRY RUN] CSV への書き込みはスキップしました。")
        return

    # CSV 書き込み
    rows = list(existing.values())
    _save_csv(rows)
    print(f"  holdings.csv を更新しました: {_HOLDINGS_CSV}")

    # 注意事項
    if added:
        print(f"\n  【注意】以下の銘柄は損切りラインを -8% で自動設定しました。")
        print(f"  実際のシグナルの損切りラインに合わせて holdings.csv を手動修正してください。")
        for t in added:
            print(f"    {t}  stop_loss_price = {existing[t]['stop_loss_price']}")

    print("=" * 55 + "\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="moomoo → holdings.csv 同期")
    parser.add_argument("--dry-run", action="store_true",
                        help="変更内容を表示するのみで CSV を書き換えない")
    parser.add_argument("--env", choices=["auto", "real", "simulate"], default="auto",
                        help="取引環境。未指定時は real を優先し、なければ simulate を使う")
    args = parser.parse_args()
    sync(dry_run=args.dry_run, trade_env=args.env)
