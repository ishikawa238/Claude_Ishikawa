"""
Claude で和訳・要約・覚えるべき単語/フレーズを作る。

- 和訳: 40文ずつ番号つきで送り、番号つき JSON で受け取る（文と訳がずれないように）
- 要約と単語: 1話につき1回。例文は本文の文番号で返してもらい、本文の該当文にリンクする
出力は structured outputs（output_config.format）で JSON に固定する。
"""

import json
import os
from dataclasses import dataclass

import anthropic

from shared.logger import get_logger

DEFAULT_MODEL = "claude-haiku-5-5"
TRANSLATE_CHUNK = 40
MAX_TOKENS = 16000
MAX_VOCAB = 15

_TRANSLATE_SYSTEM = (
    "You translate English podcast transcripts into natural Japanese for a Japanese learner of English. "
    "Translate each numbered sentence on its own, keeping the same number. "
    "Keep proper nouns in their usual Japanese form. If a sentence is a fragment, translate it as a fragment."
)

_STUDY_SYSTEM = (
    "You are an English teacher for an intermediate Japanese learner (around TOEIC 600-800). "
    "From the numbered podcast transcript, pick the words and phrases most worth memorising: "
    "useful in news and everyday conversation, around CEFR B1-C1 or EIKEN Pre-1. "
    "Prefer phrasal verbs, collocations and idioms over single basic words. "
    "Skip proper nouns, numbers and very basic words (e.g. 'people', 'today'). "
    f"Return at most {MAX_VOCAB} items. For each, give a short Japanese meaning as used in this context, "
    "quote the sentence it appears in as example_en (copied from the transcript), translate that sentence, "
    "and give that sentence's number. Also write a 2-3 sentence Japanese summary of the episode."
)

_TRANSLATE_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"i": {"type": "integer"}, "ja": {"type": "string"}},
                "required": ["i", "ja"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}

_STUDY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary_ja": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "kind": {"type": "string", "enum": ["word", "phrase"]},
                    "meaning_ja": {"type": "string"},
                    "example_en": {"type": "string"},
                    "example_ja": {"type": "string"},
                    "segment_idx": {"type": "integer"},
                },
                "required": ["term", "kind", "meaning_ja", "example_en", "example_ja", "segment_idx"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary_ja", "items"],
    "additionalProperties": False,
}


class StudyAIError(RuntimeError):
    """Claude での処理に失敗した（次回やり直せる）。"""


@dataclass(frozen=True)
class AISettings:
    api_key: str
    model: str = DEFAULT_MODEL


def _logger():
    return get_logger(__name__)


def settings_from_config(study_cfg: dict) -> AISettings | None:
    """API キー（config → 環境変数 ANTHROPIC_API_KEY の順）が無ければ None。"""
    key = (study_cfg.get("anthropic_api_key") or "").strip()
    if not key or key.startswith("YOUR_"):
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    return AISettings(api_key=key, model=study_cfg.get("model") or DEFAULT_MODEL)


def make_client(settings: AISettings) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.api_key, timeout=120.0, max_retries=2)


def _ask_json(client: anthropic.Anthropic, model: str, system: str, user: str, schema: dict) -> dict:
    try:
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        )
    except anthropic.APIStatusError as e:
        raise StudyAIError(f"Claude API エラー（HTTP {e.status_code}）: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise StudyAIError("Claude API に接続できませんでした") from e
    if response.stop_reason in ("refusal", "max_tokens"):
        raise StudyAIError(f"Claude の応答が途中で止まりました（{response.stop_reason}）")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise StudyAIError("Claude の応答を JSON として読めませんでした") from e


def _numbered(segments: list[dict], offset: int = 0) -> str:
    return "\n".join(f"[{offset + i}] {s['text_en']}" for i, s in enumerate(segments))


def translate(client: anthropic.Anthropic, model: str, segments: list[dict]) -> dict[int, str]:
    """{文番号: 和訳} を返す。抜けた文は返さない（全体の半分以上抜けたら失敗扱い）。"""
    result: dict[int, str] = {}
    for start in range(0, len(segments), TRANSLATE_CHUNK):
        chunk = segments[start:start + TRANSLATE_CHUNK]
        valid = range(start, start + len(chunk))
        data = _ask_json(client, model, _TRANSLATE_SYSTEM, _numbered(chunk, start), _TRANSLATE_SCHEMA)
        for item in data.get("translations", []):
            idx, ja = item.get("i"), str(item.get("ja", "")).strip()
            if isinstance(idx, int) and idx in valid and ja:
                result[idx] = ja
    if segments and len(result) < len(segments) / 2:
        raise StudyAIError(f"和訳が足りません（{len(result)}/{len(segments)} 文）")
    return result


def clean_vocab(items: list[dict], segment_count: int) -> list[dict]:
    """AI の単語リストを整える（空・重複・範囲外の文番号を除く）。"""
    seen: set[str] = set()
    cleaned: list[dict] = []
    for item in items:
        term = str(item.get("term", "")).strip()
        meaning = str(item.get("meaning_ja", "")).strip()
        if not term or not meaning or term.lower() in seen:
            continue
        seen.add(term.lower())
        idx = item.get("segment_idx")
        cleaned.append({
            "term": term[:80],
            "kind": "phrase" if item.get("kind") == "phrase" or " " in term else "word",
            "meaning_ja": meaning[:200],
            "example_en": str(item.get("example_en", "")).strip()[:500] or None,
            "example_ja": str(item.get("example_ja", "")).strip()[:500] or None,
            "segment_idx": idx if isinstance(idx, int) and 0 <= idx < segment_count else None,
        })
    return cleaned[:MAX_VOCAB]


def extract_study(client: anthropic.Anthropic, model: str, segments: list[dict],
                  title: str = "") -> tuple[str, list[dict]]:
    """(日本語要約, 単語リスト) を返す。"""
    user = f"Episode title: {title}\n\n{_numbered(segments)}"
    data = _ask_json(client, model, _STUDY_SYSTEM, user, _STUDY_SCHEMA)
    summary = str(data.get("summary_ja", "")).strip()
    return summary, clean_vocab(data.get("items", []), len(segments))
