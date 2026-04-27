"""scorer モジュールのユニットテスト"""

import unittest
from unittest.mock import patch

MOCK_CONFIG = {
    "scoring": {
        "primary_keywords": ["AML", "blockchain"],
        "secondary_keywords": ["DeFi", "KYC"],
        "primary_weight": 15,
        "secondary_weight": 5,
        "title_multiplier": 3,
        "score_threshold": 30,
        "max_notify": 10,
    },
}


class TestCalcScore(unittest.TestCase):
    """_calc_score の単体テスト"""

    def setUp(self):
        self._patcher = patch("src.scorer.CONFIG", MOCK_CONFIG)
        self._patcher.start()
        from src.scorer import _calc_score
        self._calc_score = _calc_score
        self._scoring_cfg = MOCK_CONFIG["scoring"]

    def tearDown(self):
        self._patcher.stop()

    def test_primary_keyword_in_title(self):
        """primary keyword がタイトルにマッチ → 高スコア"""
        score, matched = self._calc_score(
            "AML Detection on Blockchain",
            "This paper studies something.",
            self._scoring_cfg,
        )
        self.assertIn("AML", matched)
        self.assertIn("blockchain", matched)
        # AML: title(15*3) + blockchain: title(15*3) = 90
        self.assertEqual(score, 90)

    def test_secondary_keyword_in_abstract_only(self):
        """secondary keyword が Abstract のみ → 低スコア"""
        score, matched = self._calc_score(
            "A Study on Finance",
            "We explore DeFi protocols and KYC compliance.",
            self._scoring_cfg,
        )
        self.assertIn("DeFi", matched)
        self.assertIn("KYC", matched)
        # DeFi: abstract(5) + KYC: abstract(5) = 10
        self.assertEqual(score, 10)

    def test_no_match_returns_zero(self):
        """マッチなし → スコア 0"""
        score, matched = self._calc_score(
            "Quantum Computing Advances",
            "This paper explores quantum entanglement.",
            self._scoring_cfg,
        )
        self.assertEqual(score, 0)
        self.assertEqual(matched, [])

    def test_score_capped_at_100(self):
        """スコアの上限が 100 にクリップされる"""
        # AML in title(45) + abstract(15) + blockchain in title(45) + abstract(15) = 120 → 100
        score, _ = self._calc_score(
            "AML blockchain analysis",
            "aml and blockchain study",
            self._scoring_cfg,
        )
        self.assertEqual(score, 100)

    def test_matched_keywords_populated(self):
        """matched_keywords にマッチしたキーワードが正しく含まれる"""
        _, matched = self._calc_score(
            "KYC on blockchain",
            "No relevant text.",
            self._scoring_cfg,
        )
        self.assertIn("blockchain", matched)
        self.assertIn("KYC", matched)
        self.assertNotIn("AML", matched)


class TestScorePapers(unittest.TestCase):
    """score_papers の結合テスト"""

    def test_returns_new_dicts_with_score(self):
        """入力 dict を変更せず、score 付き新 dict を返す"""
        with patch("src.scorer.CONFIG", MOCK_CONFIG):
            from src.scorer import score_papers

            papers = [
                {"title": "AML study", "abstract": "some text", "arxiv_id": "1"},
                {"title": "Unrelated", "abstract": "nothing here", "arxiv_id": "2"},
            ]
            original_keys = set(papers[0].keys())
            scored = score_papers(papers)

            # 元の dict にはキーが追加されていない
            self.assertEqual(set(papers[0].keys()), original_keys)
            # scored には score と matched_keywords がある
            self.assertIn("score", scored[0])
            self.assertIn("matched_keywords", scored[0])
            # スコア降順でソートされている
            self.assertGreaterEqual(scored[0]["score"], scored[1]["score"])


if __name__ == "__main__":
    unittest.main()
