import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

import anthropic
import httpx2

from src.study import ai


def response(data, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=json.dumps(data))])


def segs(n):
    return [{"text_en": f"Sentence {i}."} for i in range(n)]


class SettingsTest(unittest.TestCase):
    def test_key_from_config_then_env(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertIsNone(ai.settings_from_config({}))
            self.assertIsNone(ai.settings_from_config({"anthropic_api_key": "YOUR_ANTHROPIC_API_KEY"}))
            s = ai.settings_from_config({"anthropic_api_key": "sk-test", "model": "claude-x"})
            self.assertEqual((s.api_key, s.model), ("sk-test", "claude-x"))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-env"}):
            self.assertEqual(ai.settings_from_config({}).model, ai.DEFAULT_MODEL)


class TranslateTest(unittest.TestCase):
    def test_chunks_and_keeps_valid_numbers_only(self):
        client = mock.Mock()

        def fake_create(**kwargs):
            lines = kwargs["messages"][0]["content"].splitlines()
            nums = [int(l[1:l.index("]")]) for l in lines]
            items = [{"i": n, "ja": f"訳{n}"} for n in nums] + [{"i": 999, "ja": "範囲外"}]
            return response({"translations": items})

        client.messages.create.side_effect = fake_create
        result = ai.translate(client, "m", segs(ai.TRANSLATE_CHUNK + 5))
        self.assertEqual(client.messages.create.call_count, 2)
        self.assertEqual(len(result), ai.TRANSLATE_CHUNK + 5)
        self.assertEqual(result[ai.TRANSLATE_CHUNK], f"訳{ai.TRANSLATE_CHUNK}")
        self.assertNotIn(999, result)
        fmt = client.messages.create.call_args.kwargs["output_config"]["format"]
        self.assertEqual(fmt["type"], "json_schema")

    def test_too_many_missing_fails(self):
        client = mock.Mock()
        client.messages.create.return_value = response({"translations": [{"i": 0, "ja": "訳"}]})
        with self.assertRaises(ai.StudyAIError):
            ai.translate(client, "m", segs(4))

    def test_truncated_or_bad_json_fails(self):
        client = mock.Mock()
        client.messages.create.return_value = response({}, stop_reason="max_tokens")
        with self.assertRaises(ai.StudyAIError):
            ai.translate(client, "m", segs(1))
        client.messages.create.return_value = SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text="not json")])
        with self.assertRaises(ai.StudyAIError):
            ai.translate(client, "m", segs(1))

    def test_api_errors_become_study_errors(self):
        client = mock.Mock()
        req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        client.messages.create.side_effect = anthropic.APIConnectionError(request=req)
        with self.assertRaises(ai.StudyAIError):
            ai.translate(client, "m", segs(1))


class ExtractStudyTest(unittest.TestCase):
    def test_cleans_items(self):
        items = [
            {"term": "crash out", "kind": "word", "meaning_ja": "キレる", "example_en": "Don't crash out.",
             "example_ja": "キレないで。", "segment_idx": 1},
            {"term": "Crash Out", "kind": "phrase", "meaning_ja": "重複", "example_en": "", "example_ja": "",
             "segment_idx": 1},
            {"term": "", "kind": "word", "meaning_ja": "空", "example_en": "", "example_ja": "", "segment_idx": 0},
            {"term": "sarcasm", "kind": "word", "meaning_ja": "皮肉", "example_en": "x", "example_ja": "y",
             "segment_idx": 99},
        ]
        client = mock.Mock()
        client.messages.create.return_value = response({"summary_ja": " 要約 ", "items": items})
        summary, vocab = ai.extract_study(client, "m", segs(3), "title")
        self.assertEqual(summary, "要約")
        self.assertEqual([v["term"] for v in vocab], ["crash out", "sarcasm"])
        self.assertEqual(vocab[0]["kind"], "phrase")  # 空白を含む語はフレーズ扱い
        self.assertIsNone(vocab[1]["segment_idx"])  # 範囲外の文番号は捨てる
        self.assertIn("Episode title: title", client.messages.create.call_args.kwargs["messages"][0]["content"])

    def test_vocab_limit(self):
        many = [{"term": f"w{i}", "kind": "word", "meaning_ja": "m"} for i in range(30)]
        self.assertEqual(len(ai.clean_vocab(many, 1)), ai.MAX_VOCAB)


if __name__ == "__main__":
    unittest.main()
