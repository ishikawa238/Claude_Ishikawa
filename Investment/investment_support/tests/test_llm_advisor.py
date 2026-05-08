"""
llm_advisor.py のテスト

プロンプト構築・API呼び出しモック・エラー処理を検証する。
"""

import unittest
from unittest.mock import patch, MagicMock

# DB・Webhook・設定ファイルに依存しないようモックを準備
_CONFIG_MOCK = {
    "slack": {"webhook_url": "https://XXXXX"},
    "jquants": {"earnings_alert_days_warning": "7", "earnings_alert_days_critical": "3"},
    "signal": {},
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "capital": {"total_capital_jpy": "1000000", "total_capital_usd": "10000",
                "max_risk_per_trade_pct": "2", "default_stop_loss_pct": "8"},
    "llm": {
        "enabled": True,
        "openai": {
            "enabled": True,
            "api_key": "sk-test-key",
            "model": "gpt-4.1",
            "max_tokens": 800,
            "temperature": 0.3,
            "timeout_sec": 5,
        },
        "anthropic": {
            "enabled": True,
            "api_key": "sk-ant-test-key",
            "model": "claude-opus-4-6",
            "max_tokens": 800,
            "temperature": 0.3,
            "timeout_sec": 5,
        },
        "gemini": {
            "enabled": True,
            "api_key": "AIzaSy-test-key",
            "model": "gemini-2.5-pro",
            "max_tokens": 800,
            "temperature": 0.3,
            "timeout_sec": 5,
        },
    },
}

with patch("src.config.CONFIG", _CONFIG_MOCK):
    from src import llm_advisor


# ============================================================
# テストデータ
# ============================================================

def _make_entry_set(**overrides) -> dict:
    base = {
        "ticker": "7203",
        "name": "トヨタ自動車",
        "market": "JP",
        "pattern": "A",
        "entry_price": 3200.0,
        "stop_loss_price": 2950.0,
        "stop_loss_pct": 7.8,
        "target_price": 3700.0,
        "recommended_amount": 150000,
        "final_score": 78.0,
        "recommend_rank": "A",
        "_rsi14": 55.5,
        "_vol_ratio": 1.8,
        "_trend_score": 82.0,
        "_pattern_a_score": 75.0,
        "_liquidity_score": 90.0,
        "_high_52w": 3500.0,
        "_close": 3200.0,
        "_ma5": 3180.0,
        "_ma25": 3100.0,
        "_ma75": 3000.0,
        "_ma200": 2800.0,
        "conditions_met": '["MA200上昇", "MA整列", "52週高値近接"]',
    }
    base.update(overrides)
    return base


def _make_holding(**overrides) -> dict:
    base = {
        "ticker": "AAPL",
        "name": "Apple Inc",
        "market": "US",
        "pnl_pct": 5.2,
        "holding_days": 15,
        "label": "hold",
        "reasons": [],
        "tech_rsi14": 60.0,
        "current_price": 190.0,
        "target_price": 210.0,
        "stop_loss_price": 170.0,
        "tech_ma5": 188.0,
        "tech_ma25": 185.0,
        "tech_ma200": 175.0,
    }
    base.update(overrides)
    return base


def _make_market_env(**overrides) -> dict:
    base = {
        "mode": "normal",
        "n225_close": 38500.0,
        "n225_ma25": 37800.0,
        "sp500_close": 5200.0,
        "sp500_ma25": 5100.0,
        "jp_above_ma25": 1,
        "us_above_ma25": 1,
    }
    base.update(overrides)
    return base


# ============================================================
# テスト
# ============================================================

class TestBuildPrompt(unittest.TestCase):
    """_build_prompt のテスト"""

    @patch("src.llm_advisor._fetch_mode_history", return_value=[])
    @patch("src.llm_advisor._fetch_index_mas", return_value=(None, None))
    def test_normal_data(self, _mock_mas, _mock_hist):
        """正常データでプロンプトが構築できる"""
        prompt = llm_advisor._build_prompt(
            [_make_entry_set()],
            [_make_holding()],
            _make_market_env(),
        )
        self.assertIn("市場環境", prompt)
        self.assertIn("通常", prompt)
        self.assertIn("7203", prompt)
        self.assertIn("トヨタ自動車", prompt)
        self.assertIn("AAPL", prompt)
        self.assertIn("買い候補の分析", prompt)

    @patch("src.llm_advisor._fetch_mode_history", return_value=[])
    @patch("src.llm_advisor._fetch_index_mas", return_value=(None, None))
    def test_empty_candidates_and_holdings(self, _mock_mas, _mock_hist):
        """空データでもエラーにならない"""
        prompt = llm_advisor._build_prompt([], [], _make_market_env())
        self.assertIn("買い候補\nなし", prompt)
        self.assertIn("保有銘柄\nなし", prompt)

    @patch("src.llm_advisor._fetch_mode_history", return_value=[])
    @patch("src.llm_advisor._fetch_index_mas", return_value=(None, None))
    def test_entry_sets_truncated(self, _mock_mas, _mock_hist):
        """エントリーセットが _MAX_ENTRY_SETS 件に制限される"""
        many_sets = [_make_entry_set(ticker=f"T{i}") for i in range(10)]
        prompt = llm_advisor._build_prompt(many_sets, [], _make_market_env())
        # 最大3件のみ含まれる
        self.assertIn("T0", prompt)
        self.assertIn("T2", prompt)
        self.assertNotIn("T3", prompt)

    @patch("src.llm_advisor._fetch_mode_history", return_value=[])
    @patch("src.llm_advisor._fetch_index_mas", return_value=(None, None))
    def test_caution_mode_displayed(self, _mock_mas, _mock_hist):
        """警戒モードが正しく表示される"""
        prompt = llm_advisor._build_prompt(
            [], [], _make_market_env(mode="caution")
        )
        self.assertIn("警戒", prompt)

    @patch("src.llm_advisor._fetch_mode_history", return_value=[])
    @patch("src.llm_advisor._fetch_index_mas", return_value=(None, None))
    def test_conditions_met_json_parsed(self, _mock_mas, _mock_hist):
        """conditions_met の JSON 文字列がパースされる"""
        prompt = llm_advisor._build_prompt(
            [_make_entry_set()], [], _make_market_env()
        )
        self.assertIn("MA200上昇", prompt)
        self.assertIn("MA整列", prompt)

    @patch("src.llm_advisor._fetch_mode_history", return_value=[])
    @patch("src.llm_advisor._fetch_index_mas", return_value=(None, None))
    def test_rich_entry_set_fields(self, _mock_mas, _mock_hist):
        """拡充フィールド（サブスコア、出来高、MA配置）が含まれる"""
        prompt = llm_advisor._build_prompt(
            [_make_entry_set()], [], _make_market_env()
        )
        self.assertIn("トレンド:82", prompt)
        self.assertIn("パターン:75", prompt)
        self.assertIn("流動性:90", prompt)
        self.assertIn("出来高:1.8倍", prompt)
        self.assertIn("MA配置:", prompt)
        self.assertIn("整列", prompt)

    @patch("src.llm_advisor._fetch_mode_history", return_value=[])
    @patch("src.llm_advisor._fetch_index_mas", return_value=(None, None))
    def test_rich_holding_fields(self, _mock_mas, _mock_hist):
        """拡充フィールド（目標距離、損切り距離、MA）が含まれる"""
        prompt = llm_advisor._build_prompt(
            [], [_make_holding()], _make_market_env()
        )
        self.assertIn("現在値:190", prompt)
        self.assertIn("目標:210", prompt)
        self.assertIn("損切り:170", prompt)
        self.assertIn("MA5:188", prompt)
        self.assertIn("ホールド", prompt)

    @patch("src.llm_advisor._fetch_mode_history", return_value=[])
    @patch("src.llm_advisor._fetch_index_mas", return_value=(None, None))
    def test_holding_earnings_alert(self, _mock_mas, _mock_hist):
        """決算接近アラートが表示される"""
        prompt = llm_advisor._build_prompt(
            [],
            [_make_holding(earnings_date="2026-05-13", earnings_days=5)],
            _make_market_env(),
        )
        self.assertIn("決算発表まで5日", prompt)

    @patch("src.llm_advisor._fetch_mode_history", return_value=["通常", "警戒", "通常"])
    @patch("src.llm_advisor._fetch_index_mas", return_value=(36000.0, 34000.0))
    def test_market_env_with_history_and_mas(self, _mock_mas, _mock_hist):
        """市場環境にモード推移とMA75/200が含まれる"""
        prompt = llm_advisor._build_prompt([], [], _make_market_env())
        self.assertIn("推移: 通常 → 警戒 → 通常", prompt)
        self.assertIn("MA75比", prompt)
        self.assertIn("MA200比", prompt)

    @patch("src.llm_advisor._fetch_mode_history", return_value=[])
    @patch("src.llm_advisor._fetch_index_mas", return_value=(None, None))
    def test_news_section_included(self, _mock_mas, _mock_hist):
        """ニュースが含まれると4項目の回答指示になる"""
        news = [
            {"title": "日銀利上げ見送り", "published": "2026-05-08", "source": "NHK"},
        ]
        prompt = llm_advisor._build_prompt(
            [], [], _make_market_env(), news=news
        )
        self.assertIn("市場ニュース", prompt)
        self.assertIn("日銀利上げ見送り", prompt)
        self.assertIn("ニュースの影響", prompt)
        self.assertIn("4項目", prompt)

    @patch("src.llm_advisor._fetch_mode_history", return_value=[])
    @patch("src.llm_advisor._fetch_index_mas", return_value=(None, None))
    def test_no_news_keeps_3_items(self, _mock_mas, _mock_hist):
        """ニュースなしの場合は3項目の回答指示"""
        prompt = llm_advisor._build_prompt([], [], _make_market_env())
        self.assertIn("3項目", prompt)
        self.assertNotIn("ニュースの影響", prompt)


class TestIsEnabled(unittest.TestCase):
    """_is_enabled のテスト"""

    def test_enabled_with_valid_key(self):
        """有効なAPIキーで True を返す"""
        with patch.object(llm_advisor, "_LLM_CONFIG", _CONFIG_MOCK["llm"]):
            self.assertTrue(llm_advisor._is_enabled("openai"))
            self.assertTrue(llm_advisor._is_enabled("anthropic"))

    def test_disabled_flag(self):
        """enabled=false で False を返す"""
        cfg = {
            "openai": {"enabled": False, "api_key": "sk-test"},
        }
        with patch.object(llm_advisor, "_LLM_CONFIG", cfg):
            self.assertFalse(llm_advisor._is_enabled("openai"))

    def test_placeholder_key(self):
        """プレースホルダー API キーで False を返す"""
        cfg = {
            "openai": {"enabled": True, "api_key": "YOUR_OPENAI_API_KEY"},
        }
        with patch.object(llm_advisor, "_LLM_CONFIG", cfg):
            self.assertFalse(llm_advisor._is_enabled("openai"))

    def test_missing_provider(self):
        """存在しないプロバイダーで False を返す"""
        with patch.object(llm_advisor, "_LLM_CONFIG", {}):
            self.assertFalse(llm_advisor._is_enabled("openai"))


class TestCallOpenAI(unittest.TestCase):
    """_call_openai のモックテスト"""

    @patch("src.llm_advisor.requests.post")
    def test_successful_call(self, mock_post):
        """成功レスポンスでテキストが返る"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "分析結果です。"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        mock_post.return_value = mock_resp

        result = llm_advisor._call_openai(
            "system", "user", _CONFIG_MOCK["llm"]["openai"]
        )
        self.assertEqual(result["text"], "分析結果です。")
        self.assertIsNone(result["error"])
        self.assertEqual(result["prompt_tokens"], 100)
        self.assertEqual(result["output_tokens"], 50)

    @patch("src.llm_advisor.requests.post")
    def test_http_error(self, mock_post):
        """HTTP エラーでエラーが返る"""
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.text = "Rate limit exceeded"
        mock_post.return_value = mock_resp

        result = llm_advisor._call_openai(
            "system", "user", _CONFIG_MOCK["llm"]["openai"]
        )
        self.assertEqual(result["text"], "")
        self.assertIn("429", result["error"])

    @patch("src.llm_advisor.requests.post")
    def test_timeout(self, mock_post):
        """タイムアウトで例外が発生する"""
        import requests as req
        mock_post.side_effect = req.exceptions.Timeout("Connection timed out")

        with self.assertRaises(req.exceptions.Timeout):
            llm_advisor._call_openai(
                "system", "user", _CONFIG_MOCK["llm"]["openai"]
            )


class TestCallAnthropic(unittest.TestCase):
    """_call_anthropic のモックテスト"""

    @patch("src.llm_advisor.requests.post")
    def test_successful_call(self, mock_post):
        """成功レスポンスでテキストが返る"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "content": [{"text": "Claudeの分析です。"}],
            "usage": {"input_tokens": 120, "output_tokens": 60},
        }
        mock_post.return_value = mock_resp

        result = llm_advisor._call_anthropic(
            "system", "user", _CONFIG_MOCK["llm"]["anthropic"]
        )
        self.assertEqual(result["text"], "Claudeの分析です。")
        self.assertIsNone(result["error"])
        self.assertEqual(result["prompt_tokens"], 120)
        self.assertEqual(result["output_tokens"], 60)

    @patch("src.llm_advisor.requests.post")
    def test_http_error(self, mock_post):
        """HTTP エラーでエラーが返る"""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal server error"
        mock_post.return_value = mock_resp

        result = llm_advisor._call_anthropic(
            "system", "user", _CONFIG_MOCK["llm"]["anthropic"]
        )
        self.assertEqual(result["text"], "")
        self.assertIn("500", result["error"])


class TestCallGemini(unittest.TestCase):
    """_call_gemini のモックテスト"""

    @patch("src.llm_advisor.requests.post")
    def test_successful_call(self, mock_post):
        """成功レスポンスでテキストが返る"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "Geminiの分析です。"}]}}],
            "usageMetadata": {"promptTokenCount": 110, "candidatesTokenCount": 55},
        }
        mock_post.return_value = mock_resp

        result = llm_advisor._call_gemini(
            "system", "user", _CONFIG_MOCK["llm"]["gemini"]
        )
        self.assertEqual(result["text"], "Geminiの分析です。")
        self.assertIsNone(result["error"])
        self.assertEqual(result["prompt_tokens"], 110)
        self.assertEqual(result["output_tokens"], 55)

    @patch("src.llm_advisor.requests.post")
    def test_http_error(self, mock_post):
        """HTTP エラーでエラーが返る"""
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "API key invalid"
        mock_post.return_value = mock_resp

        result = llm_advisor._call_gemini(
            "system", "user", _CONFIG_MOCK["llm"]["gemini"]
        )
        self.assertEqual(result["text"], "")
        self.assertIn("403", result["error"])


class TestGetLlmFeedback(unittest.TestCase):
    """get_llm_feedback の統合テスト（API はモック）"""

    @patch("src.llm_advisor._save_feedback")
    @patch("src.llm_advisor.requests.post")
    def test_all_providers_succeed(self, mock_post, mock_save):
        """全プロバイダーが成功した場合、3件の結果が返る"""
        def side_effect(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "openai" in url:
                resp.json.return_value = {
                    "choices": [{"message": {"content": "OpenAI回答"}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                }
            elif "anthropic" in url:
                resp.json.return_value = {
                    "content": [{"text": "Anthropic回答"}],
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                }
            else:
                resp.json.return_value = {
                    "candidates": [{"content": {"parts": [{"text": "Gemini回答"}]}}],
                    "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 50},
                }
            return resp
        mock_post.side_effect = side_effect

        with patch.object(llm_advisor, "_LLM_CONFIG", _CONFIG_MOCK["llm"]):
            results = llm_advisor.get_llm_feedback(
                [_make_entry_set()],
                [_make_holding()],
                _make_market_env(),
            )

        self.assertEqual(len(results), 3)
        providers = {r["provider"] for r in results}
        self.assertEqual(providers, {"openai", "anthropic", "gemini"})

    @patch("src.llm_advisor._save_feedback")
    @patch("src.llm_advisor.requests.post")
    def test_one_provider_fails(self, mock_post, mock_save):
        """一部が失敗しても、他のプロバイダーの結果は返る"""
        def side_effect(url, **kwargs):
            resp = MagicMock()
            if "openai" in url:
                resp.status_code = 200
                resp.json.return_value = {
                    "choices": [{"message": {"content": "OpenAI回答"}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                }
            elif "anthropic" in url:
                resp.status_code = 500
                resp.text = "Server error"
            else:
                resp.status_code = 200
                resp.json.return_value = {
                    "candidates": [{"content": {"parts": [{"text": "Gemini回答"}]}}],
                    "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 50},
                }
            return resp
        mock_post.side_effect = side_effect

        with patch.object(llm_advisor, "_LLM_CONFIG", _CONFIG_MOCK["llm"]):
            results = llm_advisor.get_llm_feedback(
                [_make_entry_set()], [_make_holding()], _make_market_env(),
            )

        self.assertEqual(len(results), 3)
        openai_result = [r for r in results if r["provider"] == "openai"][0]
        anthropic_result = [r for r in results if r["provider"] == "anthropic"][0]
        gemini_result = [r for r in results if r["provider"] == "gemini"][0]
        self.assertIsNone(openai_result["error"])
        self.assertIsNotNone(anthropic_result["error"])
        self.assertIsNone(gemini_result["error"])

    def test_disabled_returns_empty(self):
        """llm.enabled=false の場合は空リストを返す"""
        cfg = {"enabled": False}
        with patch.object(llm_advisor, "_LLM_CONFIG", cfg):
            results = llm_advisor.get_llm_feedback([], [], {})
        self.assertEqual(results, [])

    def test_no_valid_providers_returns_empty(self):
        """有効なプロバイダーがない場合は空リストを返す"""
        cfg = {
            "enabled": True,
            "openai": {"enabled": True, "api_key": "YOUR_OPENAI_API_KEY"},
            "anthropic": {"enabled": False, "api_key": "sk-ant-xxx"},
        }
        with patch.object(llm_advisor, "_LLM_CONFIG", cfg):
            results = llm_advisor.get_llm_feedback([], [], {})
        self.assertEqual(results, [])


class TestFormatMarketEnvRich(unittest.TestCase):
    """_format_market_env_rich のテスト"""

    @patch("src.llm_advisor._fetch_mode_history", return_value=[])
    @patch("src.llm_advisor._fetch_index_mas", return_value=(None, None))
    def test_normal_mode(self, _mock_mas, _mock_hist):
        text = llm_advisor._format_market_env_rich(_make_market_env())
        self.assertIn("通常", text)
        self.assertIn("日経平均", text)
        self.assertIn("S&P500", text)

    @patch("src.llm_advisor._fetch_mode_history", return_value=[])
    @patch("src.llm_advisor._fetch_index_mas", return_value=(None, None))
    def test_missing_index_data(self, _mock_mas, _mock_hist):
        """インデックスデータなしでもエラーにならない"""
        text = llm_advisor._format_market_env_rich({"mode": "caution"})
        self.assertIn("警戒", text)


class TestFormatHoldingsRich(unittest.TestCase):
    """_format_holdings_rich のテスト"""

    def test_holding_with_reasons(self):
        h = _make_holding(
            label="sell_watch",
            reasons=["MA5がMA25を下回った", "RSIが70を超過"],
        )
        text = llm_advisor._format_holdings_rich([h])
        self.assertIn("売り検討", text)
        self.assertIn("MA5がMA25を下回った", text)

    def test_holding_target_and_sl_distance(self):
        """目標・損切りとの距離が表示される"""
        text = llm_advisor._format_holdings_rich([_make_holding()])
        self.assertIn("目標:210", text)
        self.assertIn("損切り:170", text)


class TestFormatMaAlignment(unittest.TestCase):
    """_format_ma_alignment のテスト"""

    def test_perfect_order(self):
        """パーフェクトオーダー時に '整列' が付く"""
        text = llm_advisor._format_ma_alignment(100, 95, 90, 80)
        self.assertIn("整列", text)
        self.assertIn("MA5(100)", text)

    def test_not_aligned(self):
        """整列していない場合は '整列' がつかない"""
        text = llm_advisor._format_ma_alignment(90, 95, 100, 80)
        self.assertNotIn("整列", text)

    def test_insufficient_data(self):
        """MA が1つ以下の場合は空文字"""
        text = llm_advisor._format_ma_alignment(100, None, None, None)
        self.assertEqual(text, "")


class TestFormatNews(unittest.TestCase):
    """_format_news のテスト"""

    def test_empty_news(self):
        self.assertEqual(llm_advisor._format_news([]), "")

    def test_with_ticker(self):
        """銘柄付きニュースに [ticker] プレフィックスが付く"""
        news = [{"title": "トヨタ増収", "published": "2026-05-08", "source": "日経", "ticker": "7203"}]
        text = llm_advisor._format_news(news)
        self.assertIn("[7203]", text)
        self.assertIn("トヨタ増収", text)

    def test_without_ticker(self):
        """銘柄なしニュースにプレフィックスが付かない"""
        news = [{"title": "日銀利上げ", "published": "2026-05-08", "source": "NHK"}]
        text = llm_advisor._format_news(news)
        self.assertNotIn("[", text)
        self.assertIn("日銀利上げ", text)


if __name__ == "__main__":
    unittest.main()
