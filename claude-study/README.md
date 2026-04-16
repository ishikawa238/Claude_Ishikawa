# claude-study

Claude Code の練習用プロジェクト。
add / subtract / multiply / divide の4演算に対応した簡単なCLI電卓。

## 使い方

```bash
python -m src.main 10 add 3       # → 13.0
python -m src.main 10 subtract 4  # → 6.0
python -m src.main 7 multiply 6   # → 42.0
python -m src.main 10 divide 4    # → 2.5
python -m src.main 10 divide 0    # → エラー: ゼロで割ることはできません (exit code 1)
```

## sales.csv 集計ツール

```bash
python -m src.report data/sales.csv                           # → 集計結果を表示（全件）
python -m src.report data/sales.csv --category food           # → food カテゴリのみ集計
python -m src.report data/sales.csv --top 2                   # → amount上位2件のみ集計
python -m src.report data/sales.csv --category food --top 1   # → food内のamount上位1件を集計
python -m src.report data/sales.csv --sort asc              # → amount昇順で集計（全件）
python -m src.report data/sales.csv --sort desc             # → amount降順で集計（全件）
python -m src.report data/sales.csv --sort asc --top 2      # → amount昇順の先頭2件を集計
python -m src.report data/sales.csv --category unknown        # → エラー: カテゴリが見つかりません (exit code 1)
python -m src.report not_found.csv                            # → エラー: ファイルが見つかりません (exit code 1)
```

出力例：

```
=== 全体 ===
件数: 4
合計: 4000.0
平均: 1000.0

=== カテゴリ別 ===
book      : 件数=2  合計=1300.0  平均=650.0
food      : 件数=2  合計=2700.0  平均=1350.0
```

## テスト

```bash
pytest
```
