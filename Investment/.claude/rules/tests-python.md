---
paths:
  - "investment_support/tests/**/*.py"
---

# テストコード用ルール

- unittest スタイルで記述する。
- 振る舞いを変更するときは、通常ケースと境界・異常系のテストを追加する。
- 過度なパラメータ化より、読みやすさを優先する。
- テスト名は内容がわかる名前にする。
- バグ修正時は、そのバグを検出できるテストを追加・更新する。
- 実行: `python -m unittest discover -s tests -v`（investment_support/ から）
