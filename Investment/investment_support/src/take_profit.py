"""
利確ターゲット管理モジュール

役割:
- 保有銘柄の利確タイプ（I/II/III）を決定する
- T1/T2/T3 の価格と売却株数を計算する
- take_profit_targets テーブルへの自動生成・到達判定・更新を行う

利確タイプ（守りモード = defensive）:
  パターンA（ブレイクアウト） → タイプI:  T1=+0.5R(50%), T2=+1.0R(30%), T3=+2.0R(20%)
  パターンB（押し目買い）     → タイプII: T1=+1.0R(30%), T2=+2.0R(30%), T3=MA25トレーリング(40%)
  対応シグナルなし           → タイプIII: 手動管理（ターゲットなし）

攻めモード（offensive）では、タイプI/II をより伸ばす形に上書きする:
  タイプI:  T1=+1.0R(20%), T2=+3.0R(20%), T3=+5.0R(20%), 残り40%は MA25→MA50 トレーリング
  タイプII: T1=+1.0R(30%), T2=+2.0R(30%), 残り40%は MA25→MA50 トレーリング
  さらに株価2倍到達で残量の半分を利確推奨する特例を持つ。
  SL引き上げ: T1到達で買値、T2到達で +1R、T3到達で +3R（直前のターゲット価格）。
"""

import math
from datetime import datetime
from typing import Optional

from src.config import get_offensive_config, is_offensive
from src.data_store import fetch_all, fetch_one, get_connection
from src.logger import get_logger

logger = get_logger(__name__)

# ============================================================
# タイプ別パラメータ（守りモードの既定値）
# ============================================================

_TYPE_PARAMS = {
    "I": {
        "t1_r_mult": 0.5,
        "t1_pct": 50,
        "t2_r_mult": 1.0,
        "t2_pct": 30,
        "t3_r_mult": 2.0,
        "t3_pct": 20,
    },
    "II": {
        "t1_r_mult": 1.0,
        "t1_pct": 30,
        "t2_r_mult": 2.0,
        "t2_pct": 30,
        "t3_r_mult": None,  # トレーリング（MA25割れ）
        "t3_pct": 40,
    },
}


def _get_type_params(profit_type: str) -> dict:
    """
    攻めモード/守りモードに応じてタイプ別パラメータを返す。
    攻めモードでは config.strategy.offensive.take_profit_type_i/ii で上書き可能。
    """
    if not is_offensive():
        return _TYPE_PARAMS.get(profit_type, {})

    off = get_offensive_config()
    if profit_type == "I":
        tp = off.get("take_profit_type_i", {})
        return {
            "t1_r_mult": float(tp.get("t1_r_mult", 1.0)),
            "t1_pct":    float(tp.get("t1_pct", 20)),
            "t2_r_mult": float(tp.get("t2_r_mult", 3.0)),
            "t2_pct":    float(tp.get("t2_pct", 20)),
            "t3_r_mult": float(tp.get("t3_r_mult", 5.0)),
            "t3_pct":    float(tp.get("t3_pct", 20)),
        }
    if profit_type == "II":
        tp = off.get("take_profit_type_ii", {})
        return {
            "t1_r_mult": float(tp.get("t1_r_mult", 1.0)),
            "t1_pct":    float(tp.get("t1_pct", 30)),
            "t2_r_mult": float(tp.get("t2_r_mult", 2.0)),
            "t2_pct":    float(tp.get("t2_pct", 30)),
            "t3_r_mult": None,
            "t3_pct":    float(tp.get("trailing_pct", 40)),
        }
    return {}


# ============================================================
# 利確タイプ決定
# ============================================================

def determine_profit_type(ticker: str) -> str:
    """
    trade_signal_log からパターンを取得し、利確タイプを返す。

    返り値: "I" / "II" / "III"
    """
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT pattern FROM trade_signal_log "
            "WHERE ticker = ? ORDER BY signal_date DESC LIMIT 1",
            (ticker,),
        )
    if not row:
        return "III"
    pattern = row["pattern"]
    if pattern == "A":
        return "I"
    if pattern in ("B", "C"):
        return "II"
    return "III"


# ============================================================
# ターゲット計算
# ============================================================

def calc_targets(
    entry_price: float,
    stop_loss_price: float,
    profit_type: str,
    quantity: float,
) -> dict:
    """
    利確タイプに基づいて T1/T2/T3 の価格と売却株数を計算する。

    返り値:
        {
            "profit_type": "I" / "II",
            "r_value": float,
            "t1_price": float, "t1_pct": float, "t1_qty": int,
            "t2_price": float, "t2_pct": float, "t2_qty": int,
            "t3_price": float|None, "t3_pct": float, "t3_qty": int,
        }
    タイプIII の場合は空辞書を返す。
    """
    if profit_type == "III":
        return {}

    params = _get_type_params(profit_type)
    if not params:
        return {}

    r_value = entry_price - stop_loss_price
    if r_value <= 0:
        return {}

    qty = max(int(quantity), 1) if quantity else 1

    # ターゲット価格
    t1_price = entry_price + r_value * params["t1_r_mult"]
    t2_price = entry_price + r_value * params["t2_r_mult"]
    t3_price = (entry_price + r_value * params["t3_r_mult"]) if params["t3_r_mult"] else None

    # 売却株数（少数株ルール適用）
    t1_qty, t2_qty, t3_qty = _split_quantity(
        qty, params["t1_pct"], params["t2_pct"], params["t3_pct"]
    )

    return {
        "profit_type": profit_type,
        "r_value": round(r_value, 4),
        "t1_price": round(t1_price, 2),
        "t1_pct": params["t1_pct"],
        "t1_qty": t1_qty,
        "t2_price": round(t2_price, 2),
        "t2_pct": params["t2_pct"],
        "t2_qty": t2_qty,
        "t3_price": round(t3_price, 2) if t3_price else None,
        "t3_pct": params["t3_pct"],
        "t3_qty": t3_qty,
    }


def _split_quantity(
    total: int,
    t1_pct: float,
    t2_pct: float,
    t3_pct: float,
) -> tuple[int, int, int]:
    """
    保有株数と各段階の売却割合から売却株数を計算する。

    少数株ルール:
      1株: T1で全売却
      2株: T1で1株、残り1株
      3株: T1で2株、残り1株
      4株以上: 標準の分割比率

    合計割合（t1_pct + t2_pct + t3_pct）が 100% 未満（攻めモード等）の場合は、
    余剰分をトレーリング枠として残し T1/T2/T3 には含めない。
    """
    if total <= 0:
        return (0, 0, 0)
    if total == 1:
        return (1, 0, 0)
    if total == 2:
        return (1, 0, 1)
    if total == 3:
        return (2, 0, 1)

    total_pct = float(t1_pct) + float(t2_pct) + float(t3_pct)
    if total_pct >= 99.0:
        # 守りモード: 端数は T1 に寄せて合計を total に揃える
        t3_qty = max(math.floor(total * t3_pct / 100), 1)
        t2_qty = max(math.floor(total * t2_pct / 100), 1)
        t1_qty = total - t2_qty - t3_qty
        if t1_qty <= 0:
            t1_qty = 1
            t2_qty = total - t1_qty - t3_qty
        return (t1_qty, t2_qty, t3_qty)

    # 攻めモード等: 余剰分はトレーリング枠として残す
    t1_qty = math.floor(total * t1_pct / 100)
    t2_qty = math.floor(total * t2_pct / 100)
    t3_qty = math.floor(total * t3_pct / 100)
    return (t1_qty, t2_qty, t3_qty)


# ============================================================
# DB操作: 自動生成・取得
# ============================================================

def ensure_targets(
    ticker: str,
    entry_price: float,
    stop_loss_price: float,
    quantity: float,
    target_date: str,
) -> Optional[dict]:
    """
    take_profit_targets に有効な行がなければ自動生成する。
    既に存在する場合はそのまま返す。

    返り値: take_profit_targets の行（dict）。タイプIII の場合は None。
    """
    existing = get_targets(ticker)
    if existing:
        return existing

    profit_type = determine_profit_type(ticker)
    if profit_type == "III":
        logger.debug(f"{ticker}: タイプIII（手動管理）— 利確ターゲットなし")
        return None

    targets = calc_targets(entry_price, stop_loss_price, profit_type, quantity)
    if not targets:
        logger.warning(f"{ticker}: ターゲット計算失敗（R <= 0 等）")
        return None

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = {
        "ticker": ticker,
        "profit_type": profit_type,
        "r_value": targets["r_value"],
        "t1_price": targets["t1_price"],
        "t1_pct": targets["t1_pct"],
        "t1_reached": 0,
        "t1_reached_date": None,
        "t2_price": targets["t2_price"],
        "t2_pct": targets["t2_pct"],
        "t2_reached": 0,
        "t2_reached_date": None,
        "t3_price": targets["t3_price"],
        "t3_pct": targets["t3_pct"],
        "t3_reached": 0,
        "t3_reached_date": None,
        "adjusted_stop": None,
        "trailing_active": 0,
        "is_active": 1,
        "created_at": now,
        "updated_at": now,
    }

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO take_profit_targets
                (ticker, profit_type, r_value,
                 t1_price, t1_pct, t1_reached, t1_reached_date,
                 t2_price, t2_pct, t2_reached, t2_reached_date,
                 t3_price, t3_pct, t3_reached, t3_reached_date,
                 adjusted_stop, trailing_active, is_active,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["ticker"], row["profit_type"], row["r_value"],
                row["t1_price"], row["t1_pct"], 0, None,
                row["t2_price"], row["t2_pct"], 0, None,
                row["t3_price"], row["t3_pct"], 0, None,
                None, 0, 1,
                now, now,
            ),
        )
    logger.info(
        f"{ticker}: 利確ターゲット自動生成（タイプ{profit_type}）"
        f" T1={targets['t1_price']} T2={targets['t2_price']}"
        f" T3={targets['t3_price'] or 'MA25トレーリング'}"
    )
    return get_targets(ticker)


def get_targets(ticker: str) -> Optional[dict]:
    """有効な利確ターゲットを取得する。"""
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT * FROM take_profit_targets "
            "WHERE ticker = ? AND is_active = 1",
            (ticker,),
        )
    return dict(row) if row else None


def deactivate_targets(ticker: str) -> None:
    """銘柄の利確ターゲットを無効化する（決済時）。"""
    with get_connection() as conn:
        conn.execute(
            "UPDATE take_profit_targets SET is_active = 0, "
            "updated_at = ? WHERE ticker = ? AND is_active = 1",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ticker),
        )


# ============================================================
# 到達判定
# ============================================================

def check_target_reached(
    ticker: str,
    current_price: float,
    entry_price: float,
    target_date: str,
    ma25: Optional[float] = None,
    ma50: Optional[float] = None,
) -> dict:
    """
    現在価格に基づいて T1/T2/T3 の到達判定を行い、DBを更新する。

    攻めモード時の追加挙動:
        - T1 到達で SL を買値へ、T2 で +1R、T3 で +3R へ引き上げる（直前のターゲット価格）
        - 株価が買値の 2 倍に到達した場合、残数の半分を利確推奨（double_milestone）
        - トレーリング枠が active な状態で MA25 を割ったら短期回収（trailing_level="ma25"）、
          MA50 を割ったら大化け枠も含めて全撤退（trailing_level="ma50"）を推奨

    返り値:
        {
            "newly_reached": list[str],   # 今回新たに到達した段階 ("t1", "t2", "t3")
            "adjusted_stop": float|None,  # 推奨SL引き上げ先
            "sell_qty": int,              # 今回売るべき推奨株数
            "trailing_triggered": bool,   # MA25/MA50 トレーリング発動
            "trailing_level": str|None,   # "ma25" or "ma50"
            "double_milestone": bool,     # 株価2倍到達（攻めモードのみ）
        }
    """
    result = {
        "newly_reached": [],
        "adjusted_stop": None,
        "sell_qty": 0,
        "trailing_triggered": False,
        "trailing_level": None,
        "double_milestone": False,
    }

    targets = get_targets(ticker)
    if not targets:
        return result

    offensive = is_offensive()
    r_val = float(targets["r_value"] or 0)
    profit_type = targets["profit_type"]

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updates: list[tuple[str, object]] = []

    # --- T1 到達判定 ---
    if (not targets["t1_reached"]
            and targets["t1_price"]
            and current_price >= targets["t1_price"]):
        result["newly_reached"].append("t1")
        # 守り・攻めとも T1 到達で SL を買値へ引き上げる
        result["adjusted_stop"] = round(entry_price, 2)
        updates.append(("t1_reached", 1))
        updates.append(("t1_reached_date", target_date))
        updates.append(("adjusted_stop", result["adjusted_stop"]))
        # 攻めモードは T1 到達時点でトレーリング枠を意識する
        if offensive:
            updates.append(("trailing_active", 1))

    # --- T2 到達判定 ---
    if (not targets["t2_reached"]
            and targets["t2_price"]
            and current_price >= targets["t2_price"]):
        result["newly_reached"].append("t2")
        if offensive:
            # 攻めモード: SL を +1R 付近へ
            result["adjusted_stop"] = round(entry_price + 1.0 * r_val, 2)
        else:
            result["adjusted_stop"] = round(entry_price + 0.5 * r_val, 2)
        updates.append(("t2_reached", 1))
        updates.append(("t2_reached_date", target_date))
        updates.append(("adjusted_stop", result["adjusted_stop"]))
        # タイプII: T2到達でトレーリング有効化
        if profit_type == "II" or offensive:
            updates.append(("trailing_active", 1))

    # --- T3 到達判定（タイプI のみ — 固定価格） ---
    if (not targets["t3_reached"]
            and targets["t3_price"]
            and current_price >= targets["t3_price"]):
        result["newly_reached"].append("t3")
        if offensive:
            # 攻めモード: SL を直前のターゲット価格（T2 = +3R 付近）へ。残り 40% はトレーリング継続
            result["adjusted_stop"] = round(entry_price + 3.0 * r_val, 2)
        else:
            result["adjusted_stop"] = round(entry_price + r_val, 2)
        updates.append(("t3_reached", 1))
        updates.append(("t3_reached_date", target_date))
        updates.append(("adjusted_stop", result["adjusted_stop"]))
        if offensive:
            updates.append(("trailing_active", 1))

    # --- トレーリング判定（攻めモード or タイプII） ---
    # 攻めモード時はトレーリング枠（残り25%）を MA25/MA50 で段階撤退する。
    # 守りモード時は従来通り タイプII の T2到達後 MA25割れで終了。
    if offensive:
        trailing_active = bool(targets["trailing_active"]) or any(
            stage in result["newly_reached"] for stage in ("t1", "t2", "t3")
        )
        ma50_break = False
        ma25_break = False
        if trailing_active:
            if ma50 is not None and current_price < ma50:
                ma50_break = True
            elif ma25 is not None and current_price < ma25:
                ma25_break = True

        if ma50_break:
            # 大化け枠も含めて全撤退（残量を全て手仕舞い）
            result["trailing_triggered"] = True
            result["trailing_level"] = "ma50"
        elif ma25_break:
            result["trailing_triggered"] = True
            result["trailing_level"] = "ma25"
    elif (profit_type == "II"
          and targets["t2_reached"]
          and not targets["t3_reached"]
          and ma25 is not None
          and current_price < ma25):
        # 守りモード タイプII の MA25 トレーリング
        result["newly_reached"].append("t3")
        result["trailing_triggered"] = True
        result["trailing_level"] = "ma25"
        updates.append(("t3_reached", 1))
        updates.append(("t3_reached_date", target_date))

    # --- 株価2倍特例（攻めモード）---
    if offensive and current_price >= entry_price * 2.0:
        result["double_milestone"] = True

    # --- 売却推奨株数の計算 ---
    # T1/T2/T3 到達分は段階比率に従う。
    if result["newly_reached"]:
        quantity = _get_holding_quantity(ticker)
        targets_info = calc_targets(
            entry_price,
            entry_price - r_val,
            profit_type,
            quantity,
        )
        for stage in result["newly_reached"]:
            result["sell_qty"] += targets_info.get(f"{stage}_qty", 0)

    # 攻めモードのトレーリング発動: 残量から短期回収50% / 全撤退
    if offensive and result["trailing_triggered"] and not result["newly_reached"]:
        remaining = max(int(_get_holding_quantity(ticker)), 0)
        if remaining > 0:
            if result["trailing_level"] == "ma50":
                result["sell_qty"] = remaining
            else:  # ma25
                result["sell_qty"] = max(remaining // 2, 1)

    # 株価2倍特例: 残量の半分を売却推奨（T 到達と独立に通知のため別フィールドとして付加）
    if result["double_milestone"]:
        remaining = max(int(_get_holding_quantity(ticker)), 0)
        result["double_sell_qty"] = max(remaining // 2, 1) if remaining > 0 else 0

    # --- DB更新 ---
    if updates:
        updates.append(("updated_at", now))
        set_clause = ", ".join(f"{col} = ?" for col, _ in updates)
        values = [val for _, val in updates]
        values.append(ticker)
        with get_connection() as conn:
            conn.execute(
                f"UPDATE take_profit_targets SET {set_clause} "
                "WHERE ticker = ? AND is_active = 1",
                values,
            )

    return result


def _get_holding_quantity(ticker: str) -> float:
    """holdings テーブルから保有数量を取得する。"""
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT quantity FROM holdings WHERE ticker = ? AND is_closed = 0",
            (ticker,),
        )
    return float(row["quantity"]) if row and row["quantity"] else 1


# ============================================================
# 整合性チェック
# ============================================================

def cleanup_stale_targets() -> int:
    """
    holdings に存在しない銘柄の利確ターゲットを無効化する。
    sync_holdings_from_csv() の後に呼ばれることを想定。

    返り値: 無効化した件数
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE take_profit_targets
            SET is_active = 0, updated_at = ?
            WHERE is_active = 1
              AND ticker NOT IN (
                  SELECT ticker FROM holdings WHERE is_closed = 0
              )
            """,
            (now,),
        )
        count = cursor.rowcount
    if count > 0:
        logger.info(f"利確ターゲット整合性チェック: {count}件を無効化")
    return count
