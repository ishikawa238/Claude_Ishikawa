# rule_backtest — 現行ルールのバックテスト

`price_history` から日ごとの指標・シグナルを再計算し、現行の売買ルールで売買した場合の損益をシミュレーションする。
既定は本番と同じ日米合算（保有枠と運用停止ルールを両市場で共有し、資金は市場ごと）。

`technical_snapshot` / `daily_signals` は 2026-04 以降しか残っていないため、保存済みのシグナルは使わない。
候補の判定・並び順・損切り幅・推奨投入額・出口判定は、本番の関数（`signal_detector._evaluate_strategy`、
`select_notify_candidates`、`entry_calculator.calc_entry_set` / `calc_recommended_amount`、`exit_simple.evaluate_exit`）をそのまま呼ぶ。
ルールを変えるとバックテストにも自動で反映される。

旧来の `src/backtester.py`（`scripts.backtest_signals`）は保存済み `daily_signals` を固定 RR で評価するもので、現行ルールの検証には使わない。

## 使い方

`investment_support/` から実行する。

```cmd
python -m scripts.backtest_rules
python -m scripts.backtest_rules --capital-jpy 100000 --capital-usd 1000 --compare-max-holdings 5,10,15,20
python -m scripts.backtest_rules --market JP --start 2024-01-01 --end 2026-09-30 --stop-mode close
```

| 引数 | 内容 | 既定 |
|------|------|------|
| `--market` | `ALL`（日米合算）/ `JP` / `US` | ALL |
| `--start` / `--end` | 期間 | 2024-01-01 ～ 今日 |
| `--capital-jpy` / `--capital-usd` | 日本株（円）・米国株（ドル）の初期資金 | `capital.total_capital_jpy` / `total_capital_usd` |
| `--usdjpy` | 合算時の円換算レート（固定） | `capital.usdjpy_fallback` |
| `--max-holdings` | 同時保有上限（全市場合計） | `capital.max_concurrent_holdings` |
| `--compare-max-holdings` | 保有上限だけを変えた比較（例: `5,10,15,20`）。データ読み込みは1回 | なし |
| `--commission` / `--slippage` | 片道手数料・スリッページ（%） | 0.1 / 0.1 |
| `--lot-size` | 売買単位（株） | 1 |
| `--sizing` | `fixed`（初期資金基準）/ `compound`（その市場の評価額基準） | fixed |
| `--stop-mode` | `intraday`（安値で損切り）/ `close`（終値判定→翌寄付） | intraday |
| `--no-risk-control` | 月間損失・連続損失による新規買い停止を無効化 | 有効 |
| `--output-dir` / `--no-save` | 結果の出力先 / 書き出さない | `data/backtest/<日時>_<市場>/` |

表示: 全体の成績、決済理由、市場ごとの新規買いの判定内訳、市場・戦略別の成績、年ごとの成績（指数の買い持ちと比較）、月次の評価額。

出力ファイル（`data/backtest/` は git 管理外）:

- `trades.csv` — 決済ごとの記録（金額は各市場の通貨。分割利確は別行で、`position_id` で同じエントリーをまとめられる）
- `equity.csv` — 日次の評価額（評価通貨）・市場ごとの現金・保有数・市場環境
- `summary.json` — 成績指標・判定内訳・実行条件
- `holdings_comparison.json` — `--compare-max-holdings` 指定時の、保有上限ごとのサマリー

## モジュール

| ファイル | 役割 |
|---------|------|
| `market_data.py` | DB を読み取り専用で開き、銘柄ごとの価格・指標・日ごとの流動性フィルタ結果・RS 母集団・指数を用意する |
| `indicators.py` | `calculate_snapshot` と同じ式の指標を銘柄ごとに時系列で一括計算する |
| `signals.py` | `MarketFeed`: 日ごとの市場環境・RS スコア・候補（通知枠と並び順）・出口判定用テクニカル・指数リターンを返す |
| `simulator.py` | `PortfolioSimulator`: 約定・損切り・利確・保有枠・運用停止ルールを日本時間の順に処理する |
| `settings.py` | `BacktestSettings`: 実行条件（既定値は config.json） |
| `report.py` | 成績指標の集計（全体・市場×戦略・年別・保有上限の比較）、表示、ファイル出力 |

## シミュレーションの約束事

日本時間の順に処理する。同じ日付では日本株の取引日 → 米国株の取引日（米国の取引は日本時間の夜～翌朝）。
1市場の1営業日の処理順:

1. 寄付: 前日に決めた決済 → 前日シグナルの新規買い（始値 + スリッページ）
2. 場中: 損切り（`intraday` は安値で判定し、損切り価格と始値の安い方で約定）
3. 引け: `exit_simple` の利確・停滞判定（翌営業日の寄付で決済）
4. 引け後: 運用停止ルール → 市場環境 → 新規シグナル

各日付の処理が終わったら終値で時価評価する（合算時は円。ドルは固定レートで換算）。

- 新規買いは、その市場の市場環境が「通常」の日だけ。候補は本番の通知枠（スイング + ブレイクアウト）と同じ件数・順番で、上から保有枠が空いている分だけ買う
- 保有枠は全市場合計。「保有数 − 全量決済待ち + もう一方の市場の約定待ち注文」を使用中として数える
- 推奨投入額は本番と同じく市場ごとの資金で計算する（`その市場の資金 × 2% ÷ 損切り幅`、上限 `その市場の資金 ÷ 保有上限`）。
  保有枠を両市場で分け合うため、合算では資金の多くが現金のまま残る
- 損切り価格は「約定価格 ×（1 − エントリーセットの損切り幅）」。約定当日も損切り・利確を判定する（ペーパートレードは約定当日を判定しない）
- `exit_simple` は本番と同じく、終値が損切り価格の 2% 以内のときは判定しない。保有日数は暦日
- 前日比 0.4 倍未満・2.5 倍超の価格の不連続（分割未調整の疑い）は前日終値で手仕舞い、直近 130 本以内に不連続がある銘柄は候補にしない
- 期間の最終日に残ったポジションは終値で手仕舞う（`end_of_test`）
- 運用停止ルールはバックテスト内の決済を両市場まとめて判定する。月間損失は円換算した損益の合計を、円換算した初期資金の合計と比べる
  （本番の `risk_control` は `trade_result_log.pnl_amount` を通貨を換算せずに合計している）

## 本番との一致確認（2026-09/10 の `daily_signals` と比較）

- JP: スクリーニング通過銘柄は価格の不連続による除外分を除いて一致、3条件成立銘柄は 373/373 で一致
- US: 指標値（EMA・RSI・MACD）は一致。条件の食い違い 462 件はすべて RSI 条件で、うち 439 件は本番側の
  バッチ未実行日により「直近5日の RSI」の参照日がずれたもの

## 既知の制約

- 為替は固定レート。為替損益は含まない
- US の RS スコアの母集団は「前日までの直近 90 日に売買代金の下限を超えた銘柄」で近似する。本番が全銘柄を計算した日（週1回程度）とは RS が数ポイントずれる
- 銘柄マスタの `is_active = 1` の銘柄だけを使う（上場廃止銘柄が抜ける生存バイアスがある）
- 決算日の接近チェックと投資委員会・LLM の判定は再現しない
