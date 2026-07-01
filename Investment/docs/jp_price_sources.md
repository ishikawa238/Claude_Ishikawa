# JP 日次価格データソース調査メモ

## 現状（実装）

| 項目 | 内容 |
|------|------|
| ソース | **yfinance**（Yahoo Finance 非公式 API） |
| モジュール | `src/yfinance_fetcher.py` |
| 利用箇所 | 夜バッチ JP 日次、初回 `fetch_historical` |
| 監視 | `DailyPriceFetchResult` → Slack `price_source_alert`、 `batch_run_log.memo` |

EODHD Bulk EOD は **JP 未対応** のため、US は EODHD・JP は yfinance という二系統構成。

## 代替候補（調査）

| ソース | 概算コスト | メリット | デメリット |
|--------|-----------|---------|-----------|
| **yfinance**（現行） | 無料 | 実装済・APIキー不要 | 非公式・障害時に全停止しうる |
| **J-Quants** | 無料プランあり | 公式・財務データと統合可 | 日次株価はプラン・エンドポイント要確認、レート制限 |
| **EODHD JP** | プラン次第 | US と同一 API | 現プランで Bulk JP 未対応（README 既知制約） |
| **Stooq** | 無料 | CSV ダウンロード | 自動化・銘柄マスタ連携の手間 |
| **moomoo OpenD** | 口座連携 | リアルタイムに近い | OpenD 常時起動が必要、全銘柄一括は不向き |

## 推奨方針（短期）

1. **監視強化**（実装済）: 全件失敗時は Slack に明示、シグナル検出をスキップ
2. **ペーパートレード継続**: 本番資金投入前に期待値検証
3. **障害時の手動確認**: `logs/batch_YYYYMMDD.log` とダッシュボードのシステムヘルス

## 推奨方針（中期）

J-Quants の日次株価 API が利用可能か `jquants-api-client` で検証し、**yfinance をフォールバック**とする二段構えを検討する。コストと実装工数のトレードオフを `config.json` で切替可能にするのが理想。

## 関連ファイル

- `src/yfinance_fetcher.py` — `fetch_daily_yf_with_status()`, `DailyPriceFetchResult`
- `scripts/batch_evening_jp.py` — 夜バッチ JP 価格取得
- `src/notifier.py` — `price_source_alert` パラメータ
