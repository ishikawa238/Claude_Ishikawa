---
paths:
  - "investment_support/src/**/*.py"
  - "investment_support/scripts/**/*.py"
---

# Python ソースコード用ルール

- 明確な必要がない限り、現在の構成を維持する。
- 可能であれば、小さく単純な関数を優先する。
- 不正な入力に対しては、わかりやすく明示的に対処する。
- 名前は意味がわかりやすく、既存コードの命名規則とそろえる。
- すべてのモジュールで `get_logger(__name__)` を使う。
- config は `from src.config import CONFIG` で統一する。
