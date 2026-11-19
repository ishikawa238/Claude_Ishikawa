import unittest
from types import SimpleNamespace
from unittest import mock

from src.study import transcribe
from src.study.transcribe import Word


def words(text: str, step: float = 0.5) -> list[Word]:
    return [Word(i * step, i * step + step, " " + w) for i, w in enumerate(text.split())]


class GroupSentencesTest(unittest.TestCase):
    def test_splits_on_sentence_end(self):
        result = transcribe.group_sentences(words("Hello there. How are you? I'm fine!"))
        self.assertEqual([s["text"] for s in result], ["Hello there.", "How are you?", "I'm fine!"])
        self.assertEqual((result[1]["start"], result[1]["end"]), (1.0, 2.5))

    def test_abbreviations_do_not_split(self):
        result = transcribe.group_sentences(words("Mr. Smith went to the U.S. yesterday."))
        self.assertEqual(len(result), 1)

    def test_quote_after_period_still_ends(self):
        self.assertTrue(transcribe.is_sentence_end(' done."'))
        self.assertFalse(transcribe.is_sentence_end(" well,"))

    def test_long_run_is_cut_and_tail_kept(self):
        result = transcribe.group_sentences(words("a b c d e f g"), max_words=3)
        self.assertEqual([s["text"] for s in result], ["a b c", "d e f", "g"])

    def test_empty(self):
        self.assertEqual(transcribe.group_sentences([]), [])


class TranscribeTest(unittest.TestCase):
    def test_uses_word_timestamps_and_falls_back_to_segment_text(self):
        seg_words = SimpleNamespace(start=0, end=2, text="Hi. Ok", words=[
            SimpleNamespace(start=0.0, end=0.5, word=" Hi."), SimpleNamespace(start=0.6, end=1.0, word=" Ok")])
        seg_plain = SimpleNamespace(start=2.0, end=4.0, text=" then we go.", words=None)
        model = mock.Mock()
        model.transcribe.return_value = (iter([seg_words, seg_plain]), None)
        with mock.patch.object(transcribe, "_load_model", return_value=model):
            result = transcribe.transcribe("x.mp3", "tiny.en")
        self.assertEqual([s["text"] for s in result], ["Hi.", "Ok then we go."])
        self.assertEqual(result[1], {"start": 0.6, "end": 4.0, "text": "Ok then we go."})
        self.assertEqual(model.transcribe.call_args.kwargs["language"], "en")

    def test_silence_raises(self):
        model = mock.Mock()
        model.transcribe.return_value = (iter([]), None)
        with mock.patch.object(transcribe, "_load_model", return_value=model):
            with self.assertRaises(ValueError):
                transcribe.transcribe("x.mp3")


if __name__ == "__main__":
    unittest.main()
