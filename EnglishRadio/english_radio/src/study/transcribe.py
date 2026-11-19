"""
英語の文字起こし（faster-whisper を PC 内で実行）。

Whisper の出力は区切りが不規則なので、単語ごとの時刻を使って
「1文 = 1セグメント」にまとめ直す（学習ページで1文ずつ表示・再生するため）。
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from shared.logger import get_logger

DEFAULT_MODEL = "small.en"
MAX_WORDS_PER_SENTENCE = 45  # 句点が来なくてもこの語数で区切る

# 文末に見えても文の終わりではない語（Mr. Dr. U.S. など）
_ABBREVIATIONS = {"mr.", "mrs.", "ms.", "dr.", "st.", "vs.", "u.s.", "u.k.", "a.m.", "p.m.", "etc.", "no."}
_SENTENCE_END_RE = re.compile(r"[.?!][\"')\]]*$")

_models: dict = {}


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


def _logger():
    return get_logger(__name__)


def is_sentence_end(word: str) -> bool:
    token = word.strip()
    if not _SENTENCE_END_RE.search(token):
        return False
    return token.lower().strip("\"')]") not in _ABBREVIATIONS


def group_sentences(words: list[Word], max_words: int = MAX_WORDS_PER_SENTENCE) -> list[dict]:
    """単語の並びを文ごとにまとめて [{start, end, text}] を返す。"""
    sentences: list[dict] = []
    current: list[Word] = []

    def flush() -> None:
        text = "".join(w.text for w in current).strip()
        if text:
            sentences.append({"start": round(current[0].start, 2), "end": round(current[-1].end, 2), "text": text})
        current.clear()

    for word in words:
        current.append(word)
        if is_sentence_end(word.text) or len(current) >= max_words:
            flush()
    if current:
        flush()
    return sentences


def _load_model(model_name: str):
    if model_name not in _models:
        from faster_whisper import WhisperModel  # 重いので使うときだけ読み込む

        threads = max(1, (os.cpu_count() or 4) - 1)  # 1コアは他の作業用に空けておく
        _logger().info(f"Whisper モデルを読み込み中: {model_name}（初回はダウンロードに数分かかります）")
        _models[model_name] = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=threads)
    return _models[model_name]


def _words_from_segments(segments) -> list[Word]:
    words: list[Word] = []
    for seg in segments:
        if getattr(seg, "words", None):
            words.extend(Word(w.start, w.end, w.word) for w in seg.words)
        else:  # 単語時刻が無いときはセグメント全体を1語として扱う
            words.append(Word(seg.start, seg.end, " " + seg.text.strip()))
    return words


def transcribe(path: Path, model_name: str = DEFAULT_MODEL) -> list[dict]:
    """mp3 を文字起こしして、1文ずつの [{start, end, text}] を返す。"""
    model = _load_model(model_name)
    segments, _info = model.transcribe(
        str(path), language="en", vad_filter=True, word_timestamps=True, condition_on_previous_text=False,
    )
    sentences = group_sentences(_words_from_segments(segments))
    if not sentences:
        raise ValueError("音声から英語を聞き取れませんでした")
    return sentences
