"""
トレード実績記録スクリプト

実行方法（investment_support/ ディレクトリから）:
    python -m scripts.add_trade_result

処理内容:
- 対話形式で決済済みトレードの実績を入力する
- trade_result_log に記録し、holdings テーブルを決済済みに更新する
- 入力した銘柄に紐づく直近のシグナル (trade_signal_log) を自動検索する
"""

import sys
from datetime import date, datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.data_store import fetch_all, fetch_one, get_connection, insert_one
from src.logger import get_logger

logger = get_logger(__name__)


def _input(prompt: str, default: str = "") -> str:
    """プロンプトを表示して入力を受け取る。空Enter で default を返す。"""
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
        return val if val else default
    except (KeyboardInterrupt, EOFError):
        print("\n中断しました。")
        sys.exit(0)


def _to_float(s: str) -> float | None:
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _find_signal(ticker: str) -> list[dict]:
    """指定銘柄の直近シグナルを最大5件返す。"""
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            "SELECT id, signal_date, pattern, entry_price, stop_loss_price, "
            "target_price, market "
            "FROM trade_signal_log "
            "WHERE ticker = ? "
            "ORDER BY signal_date DESC LIMIT 5",
            (ticker,),
        )
    return [dict(r) for r in rows]


def _find_holding(ticker: str) -> dict | None:
    """保有中（未決済）の銘柄を返す。"""
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT id, entry_price, entry_date, quantity, stop_loss_price "
            "FROM holdings WHERE ticker = ? AND is_closed = 0",
            (ticker,),
        )
    return dict(row) if row else None


def _close_holding(holding_id: int, exit_price: float, exit_date: str, reason: str) -> None:
    """holdings テーブルの当該行を決済済みに更新する。"""
    with get_connection() as conn:
        conn.execute(
            "UPDATE holdings SET is_closed=1, close_price=?, close_date=?, "
            "close_reason=?, updated_at=? WHERE id=?",
            (exit_price, exit_date, reason, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), holding_id),
        )


def main() -> None:
    print("\n" + "=" * 55)
    print("  トレード実績記録")
    print("=" * 55)

    # ── ティッカー入力 ──────────────────────────────
    ticker = _input("ティッカー（例: 7203 / AAPL）").upper()
    if not ticker:
        print("ティッカーを入力してください。")
        sys.exit(1)

    # ── シグナル選択 ────────────────────────────────
    signals = _find_signal(ticker)
    signal_id: int | None = None

    if signals:
        print(f"\n{ticker} の直近シグナル:")
        for i, s in enumerate(signals, 1):
            print(f"  {i}. [{s['signal_date']}] パターン{s['pattern']}  "
                  f"エントリー{s['entry_price']}  損切り{s['stop_loss_price']}  "
                  f"目標{s['target_price']}  (id={s['id']})")
        choice = _input("紐づけるシグナル番号 (0=なし)", "1")
        try:
            idx = int(choice)
            if 1 <= idx <= len(signals):
                signal_id = signals[idx - 1]["id"]
        except ValueError:
            pass
    else:
        print(f"  {ticker} のシグナル記録が見つかりません。シグナルなしで記録します。")
        # シグナルなしは signal_id=None → FKがあるので仮シグナルが必要
        # ここでは直接 signal_id を手入力させる
        manual_id = _input("signal_id を手動入力（不明なら 0）", "0")
        sid = _to_float(manual_id)
        signal_id = int(sid) if sid and int(sid) > 0 else None

    if signal_id is None:
        print("シグナルが特定できないため記録できません。")
        print("  → trade_signal_log に対象シグナルが存在することを確認してください。")
        sys.exit(1)

    # ── 決済情報入力 ────────────────────────────────
    today_str = date.today().strftime("%Y-%m-%d")

    actual_entry = _to_float(_input("実際のエントリー価格 (空=シグナル参照)"))
    actual_entry_date = _input("エントリー日 (YYYY-MM-DD)", today_str)
    exit_price_raw = _input("決済価格")
    exit_price = _to_float(exit_price_raw)
    if exit_price is None:
        print("決済価格が不正です。")
        sys.exit(1)

    exit_date = _input("決済日 (YYYY-MM-DD)", today_str)

    print("\n決済理由:")
    print("  1. stop_loss  （損切り）")
    print("  2. take_profit（利益確定）")
    print("  3. manual     （手動決済）")
    reason_map = {"1": "stop_loss", "2": "take_profit", "3": "manual"}
    reason_choice = _input("番号を入力", "3")
    exit_reason = reason_map.get(reason_choice, "manual")

    memo = _input("メモ（任意）")

    # ── P&L 計算 ────────────────────────────────────
    # entry_price はシグナルから参照
    with get_connection() as conn:
        sig = fetch_one(conn, "SELECT entry_price FROM trade_signal_log WHERE id=?", (signal_id,))
    ref_entry = actual_entry or (float(sig["entry_price"]) if sig else None)

    pnl_pct = None
    pnl_amount = None
    if ref_entry and ref_entry > 0:
        pnl_pct = round((exit_price - ref_entry) / ref_entry * 100, 2)

    holding = _find_holding(ticker)
    if holding and holding.get("quantity") and ref_entry:
        pnl_amount = round((exit_price - ref_entry) * holding["quantity"], 2)

    # ── DB 書き込み ─────────────────────────────────
    row = {
        "signal_id":          signal_id,
        "ticker":             ticker,
        "actual_entry_price": actual_entry,
        "actual_entry_date":  actual_entry_date,
        "actual_exit_price":  exit_price,
        "actual_exit_date":   exit_date,
        "exit_reason":        exit_reason,
        "pnl_pct":            pnl_pct,
        "pnl_amount":         pnl_amount,
        "memo":               memo or None,
    }

    with get_connection() as conn:
        new_id = insert_one(conn, "trade_result_log", row)

    # holdings も決済済みに更新
    if holding:
        _close_holding(holding["id"], exit_price, exit_date, exit_reason)
        print(f"\nholdings テーブルを決済済みに更新しました。")

    # ── 結果表示 ────────────────────────────────────
    print("\n" + "=" * 55)
    print("  記録完了")
    print("=" * 55)
    print(f"  trade_result_log id : {new_id}")
    print(f"  ティッカー          : {ticker}")
    print(f"  決済価格            : {exit_price}")
    print(f"  決済日              : {exit_date}")
    print(f"  決済理由            : {exit_reason}")
    if pnl_pct is not None:
        sign = "+" if pnl_pct >= 0 else ""
        print(f"  損益率              : {sign}{pnl_pct}%")
    if pnl_amount is not None:
        sign = "+" if pnl_amount >= 0 else ""
        print(f"  損益額              : {sign}{pnl_amount}")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    main()
