import subprocess
import sys

import pytest

from src.report import load_sales, summarize, format_report


class TestLoadSales:
    def test_loads_correct_row_count(self, sales_csv):
        records = load_sales(sales_csv)
        assert len(records) == 4

    def test_amount_is_float(self, sales_csv):
        records = load_sales(sales_csv)
        for r in records:
            assert isinstance(r["amount"], float)

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError, match="ファイルが見つかりません"):
            load_sales("nonexistent.csv")

    def test_invalid_amount(self, tmp_path):
        path = tmp_path / "bad.csv"
        path.write_text("date,amount,category\n2026-01-01,abc,food\n", encoding="utf-8")
        with pytest.raises(ValueError, match="不正"):
            load_sales(str(path))

    def test_missing_amount_column(self, tmp_path):
        path = tmp_path / "no_amount.csv"
        path.write_text("date,category\n2026-01-01,food\n", encoding="utf-8")
        with pytest.raises(ValueError, match="不正"):
            load_sales(str(path))

    def test_short_row(self, tmp_path):
        path = tmp_path / "short.csv"
        path.write_text("date,amount,category\n2026-01-01\n", encoding="utf-8")
        with pytest.raises(ValueError, match="不正"):
            load_sales(str(path))


class TestSummarize:
    def test_total(self, sales_csv):
        result = summarize(load_sales(sales_csv))
        assert result["total"] == 4000.0

    def test_count(self, sales_csv):
        result = summarize(load_sales(sales_csv))
        assert result["count"] == 4

    def test_average(self, sales_csv):
        result = summarize(load_sales(sales_csv))
        assert result["average"] == 1000.0

    def test_category_food(self, sales_csv):
        by_cat = summarize(load_sales(sales_csv))["by_category"]
        assert by_cat["food"]["count"] == 2
        assert by_cat["food"]["total"] == 2700.0
        assert by_cat["food"]["average"] == pytest.approx(1350.0)

    def test_category_book(self, sales_csv):
        by_cat = summarize(load_sales(sales_csv))["by_category"]
        assert by_cat["book"]["count"] == 2
        assert by_cat["book"]["total"] == 1300.0
        assert by_cat["book"]["average"] == pytest.approx(650.0)

    def test_empty_records(self):
        result = summarize([])
        assert result["count"] == 0
        assert result["total"] == 0.0
        assert result["average"] is None
        assert result["by_category"] == {}


class TestFormatReport:
    def test_contains_total(self, sales_csv):
        result = summarize(load_sales(sales_csv))
        output = format_report(result)
        assert "4000" in output

    def test_contains_average(self, sales_csv):
        result = summarize(load_sales(sales_csv))
        output = format_report(result)
        assert "1000.0" in output

    def test_contains_categories(self, sales_csv):
        result = summarize(load_sales(sales_csv))
        output = format_report(result)
        assert "food" in output
        assert "book" in output

    def test_contains_category_average(self, sales_csv):
        output = format_report(summarize(load_sales(sales_csv)))
        assert "1350.0" in output  # food の平均
        assert "650.0" in output   # book の平均

    def test_empty_shows_na(self):
        output = format_report(summarize([]))
        assert "N/A" in output


class TestCategoryFilter:
    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "src.report"] + args,
            capture_output=True,
            text=True,
        )

    def test_no_filter_returns_all(self, sales_csv):
        result = self._run([sales_csv])
        assert result.returncode == 0
        assert "件数: 4" in result.stdout

    def test_filter_food(self, sales_csv):
        result = self._run([sales_csv, "--category", "food"])
        assert result.returncode == 0
        assert "件数: 2" in result.stdout
        assert "2700" in result.stdout

    def test_filter_book(self, sales_csv):
        result = self._run([sales_csv, "--category", "book"])
        assert result.returncode == 0
        assert "件数: 2" in result.stdout
        assert "1300" in result.stdout

    def test_unknown_category_exits_with_error(self, sales_csv):
        result = self._run([sales_csv, "--category", "unknown"])
        assert result.returncode == 1
        assert "unknown" in result.stderr


class TestTopFilter:
    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "src.report"] + args,
            capture_output=True,
            text=True,
        )

    def test_top2_returns_two_records(self, sales_csv):
        # amount降順: 1500, 1200, 800, 500 → 上位2件の合計=2700
        result = self._run([sales_csv, "--top", "2"])
        assert result.returncode == 0
        assert "件数: 2" in result.stdout
        assert "2700" in result.stdout

    def test_top1_returns_one_record(self, sales_csv):
        # 上位1件: 1500
        result = self._run([sales_csv, "--top", "1"])
        assert result.returncode == 0
        assert "件数: 1" in result.stdout
        assert "1500" in result.stdout

    def test_top_with_category(self, sales_csv):
        # food: 1500, 1200 → top 1 → 合計=1500
        result = self._run([sales_csv, "--category", "food", "--top", "1"])
        assert result.returncode == 0
        assert "件数: 1" in result.stdout
        assert "1500" in result.stdout

    def test_top_exceeds_count_returns_all(self, sales_csv):
        # N > 件数 はエラーにせず全件返す
        result = self._run([sales_csv, "--top", "999"])
        assert result.returncode == 0
        assert "件数: 4" in result.stdout

    def test_top_zero_exits_with_error(self, sales_csv):
        result = self._run([sales_csv, "--top", "0"])
        assert result.returncode == 1
        assert "エラー" in result.stderr

    def test_top_negative_exits_with_error(self, sales_csv):
        result = self._run([sales_csv, "--top", "-1"])
        assert result.returncode == 1
        assert "エラー" in result.stderr

    def test_top_non_integer_exits_with_error(self, sales_csv):
        # argparse が type=int で弾く → exit code 2
        result = self._run([sales_csv, "--top", "abc"])
        assert result.returncode == 2


class TestSortOption:
    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "src.report"] + args,
            capture_output=True,
            text=True,
        )

    def test_sort_desc_returns_success(self, sales_csv):
        result = self._run([sales_csv, "--sort", "desc"])
        assert result.returncode == 0
        assert "件数: 4" in result.stdout

    def test_sort_asc_returns_success(self, sales_csv):
        result = self._run([sales_csv, "--sort", "asc"])
        assert result.returncode == 0
        assert "件数: 4" in result.stdout

    def test_sort_desc_with_top2_returns_highest_two(self, sales_csv):
        # desc + top2: amount降順で上位2件 → 1500 + 1200 = 2700
        result = self._run([sales_csv, "--sort", "desc", "--top", "2"])
        assert result.returncode == 0
        assert "件数: 2" in result.stdout
        assert "2700" in result.stdout

    def test_sort_asc_with_top2_returns_lowest_two(self, sales_csv):
        # asc + top2: amount昇順で先頭2件 → 500 + 800 = 1300
        result = self._run([sales_csv, "--sort", "asc", "--top", "2"])
        assert result.returncode == 0
        assert "件数: 2" in result.stdout
        assert "1300" in result.stdout

    def test_invalid_sort_value_exits_with_error(self, sales_csv):
        # choices 以外の値は argparse が弾く → exit code 2
        result = self._run([sales_csv, "--sort", "invalid"])
        assert result.returncode == 2
