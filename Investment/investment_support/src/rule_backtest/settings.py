"""バックテストの実行条件。既定値は config.json の現行ルールから取る。"""

from __future__ import annotations

from dataclasses import dataclass

from src.config import CONFIG, get_swing_config

# 同じ日付では JP の取引日 → US の取引日の順に起きる（米国の取引は日本時間の夜～翌朝）
MARKETS = ("JP", "US")
SIZING_MODES = ("fixed", "compound")
STOP_MODES = ("intraday", "close")


@dataclass
class BacktestSettings:
    markets: tuple[str, ...]
    start: str
    end: str
    capital: dict[str, float]           # 市場ごとの初期資金（JP は円、US はドル）
    max_holdings: int                   # 全市場合計の同時保有上限
    risk_pct: float                     # 1トレードの許容リスク（割合。0.02 = 2%）
    usdjpy: float = 150.0               # 複数市場のとき評価額・月間損失を円に換算する固定レート
    commission_pct: float = 0.1         # 片道手数料（%）
    slippage_pct: float = 0.1           # 片道スリッページ（%）
    lot_size: int = 1                   # 売買単位（株）
    sizing: str = "fixed"               # fixed: 初期資金基準 / compound: その市場の前日評価額基準
    stop_mode: str = "intraday"         # intraday: 安値で損切り約定 / close: 終値判定→翌寄付
    risk_control: bool = True           # 月間損失・連続損失による新規買い停止
    monthly_loss_stop_pct: float = 6.0
    max_consecutive_losses: int = 3

    def __post_init__(self) -> None:
        unknown = [m for m in self.markets if m not in MARKETS]
        if unknown or not self.markets:
            raise ValueError(f"markets は {MARKETS} から1つ以上: {self.markets}")
        self.markets = tuple(m for m in MARKETS if m in self.markets)
        missing = [m for m in self.markets if m not in self.capital]
        if missing:
            raise ValueError(f"初期資金が未指定の市場があります: {missing}")
        self.capital = {m: float(self.capital[m]) for m in self.markets}
        if self.sizing not in SIZING_MODES:
            raise ValueError(f"sizing は {SIZING_MODES} のいずれか: {self.sizing}")
        if self.stop_mode not in STOP_MODES:
            raise ValueError(f"stop_mode は {STOP_MODES} のいずれか: {self.stop_mode}")
        if self.start > self.end:
            raise ValueError(f"開始日が終了日より後です: {self.start} > {self.end}")
        if self.usdjpy <= 0:
            raise ValueError(f"usdjpy は正の値: {self.usdjpy}")
        self.lot_size = max(int(self.lot_size), 1)
        self.max_holdings = max(int(self.max_holdings), 1)

    @property
    def currency(self) -> str:
        """評価通貨。US 単独ならドル、それ以外は円。"""
        return "USD" if self.markets == ("US",) else "JPY"

    def fx(self, market: str) -> float:
        """market の通貨建ての金額を評価通貨に換算するレート。"""
        return self.usdjpy if market == "US" and self.currency == "JPY" else 1.0

    @property
    def initial_capital(self) -> float:
        """評価通貨での初期資金の合計。"""
        return sum(self.capital[m] * self.fx(m) for m in self.markets)

    @classmethod
    def from_config(
        cls,
        markets: tuple[str, ...] | list[str],
        start: str,
        end: str,
        *,
        capital_jpy: float | None = None,
        capital_usd: float | None = None,
        **overrides,
    ) -> "BacktestSettings":
        """config.json の現行値で埋め、None 以外の引数で上書きする。"""
        cap = CONFIG.get("capital", {})
        swing = get_swing_config()
        rc = CONFIG.get("risk_control", {})
        capital = {
            "JP": float(capital_jpy if capital_jpy is not None else cap.get("total_capital_jpy", 0)),
            "US": float(capital_usd if capital_usd is not None else cap.get("total_capital_usd", 0)),
        }
        values = {
            "max_holdings": int(cap.get("max_concurrent_holdings", 3)),
            "risk_pct": float(
                swing.get("max_risk_per_trade_pct", cap.get("max_risk_per_trade_pct", 2.0))
            ) / 100,
            "usdjpy": float(cap.get("usdjpy_fallback", 150.0)),
            "monthly_loss_stop_pct": float(rc.get("monthly_loss_stop_pct", 0) or 0),
            "max_consecutive_losses": int(rc.get("max_consecutive_losses_before_pause", 0) or 0),
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(
            markets=tuple(markets), start=start, end=end,
            capital={m: capital[m] for m in markets}, **values,
        )
