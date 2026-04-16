import csv
import pytest


@pytest.fixture
def sales_csv(tmp_path):
    """テスト用の一時CSVファイルを返す。"""
    path = tmp_path / "sales.csv"
    rows = [
        {"date": "2026-04-01", "amount": "1200", "category": "food"},
        {"date": "2026-04-02", "amount": "800",  "category": "book"},
        {"date": "2026-04-03", "amount": "1500", "category": "food"},
        {"date": "2026-04-04", "amount": "500",  "category": "book"},
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "amount", "category"])
        writer.writeheader()
        writer.writerows(rows)
    return str(path)
