"""Pinyin romanization with polyphonic handling.

``pinyin_map.json`` keeps a single reading per character, which turns 长安 into
"zhangan".  This module keeps that map as the base and adds two corrections:

* a phrase table for whole words whose reading is not derivable character by
  character (长安 -> chang + an);
* candidate readings for common polyphonic characters, indexed as extra
  variants so both "changan" and "zhangan" reach the same title.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

MAX_PHRASE_LENGTH = 8
MAX_VARIANTS = 8
CJK = re.compile(r"[\u3400-\u9fff]")
KEEPER = re.compile(r"[0-9a-z]")
DEFAULT_PINYIN_PATH = Path(__file__).resolve().parents[1] / "backend-src" / "pinyin_map.json"

# Whole words whose correct reading differs from the per-character default.
# Values are the per-character syllables so initials stay derivable.
PHRASE_OVERRIDES: dict[str, tuple[str, ...]] = {
    "长安": ("chang", "an"),
    "长相思": ("chang", "xiang", "si"),
    "长沙": ("chang", "sha"),
    "长城": ("chang", "cheng"),
    "长春": ("chang", "chun"),
    "长江": ("chang", "jiang"),
    "长津湖": ("chang", "jin", "hu"),
    "长征": ("chang", "zheng"),
    "长歌行": ("chang", "ge", "xing"),
    "长夜": ("chang", "ye"),
    "长生": ("chang", "sheng"),
    "重庆": ("chong", "qing"),
    "重启": ("chong", "qi"),
    "重生": ("chong", "sheng"),
    "重案": ("chong", "an"),
    "重逢": ("chong", "feng"),
    "重来": ("chong", "lai"),
    "重制": ("chong", "zhi"),
    "银行": ("yin", "hang"),
    "行业": ("hang", "ye"),
    "行家": ("hang", "jia"),
    "商行": ("shang", "hang"),
    "音乐": ("yin", "yue"),
    "乐队": ("yue", "dui"),
    "乐章": ("yue", "zhang"),
    "乐团": ("yue", "tuan"),
    "还珠格格": ("huan", "zhu", "ge", "ge"),
    "归还": ("gui", "huan"),
    "都挺好": ("dou", "ting", "hao"),
    "首都": ("shou", "du"),
    "会计": ("kuai", "ji"),
    "角色": ("jue", "se"),
    "主角": ("zhu", "jue"),
    "配角": ("pei", "jue"),
    "独角兽": ("du", "jiao", "shou"),
    "西藏": ("xi", "zang"),
    "藏地": ("zang", "di"),
    "宝藏": ("bao", "zang"),
    "秘藏": ("mi", "cang"),
    "重新": ("chong", "xin"),
    "重塑": ("chong", "su"),
    "重启人生": ("chong", "qi", "ren", "sheng"),
}

# Extra readings that the single-reading map cannot express.  The map value is
# always kept as the primary reading.
POLYPHONE_ALTS: dict[str, tuple[str, ...]] = {
    "长": ("chang",),
    "重": ("chong",),
    "行": ("hang",),
    "乐": ("yue",),
    "还": ("huan",),
    "都": ("du",),
    "便": ("pian",),
    "藏": ("zang",),
    "会": ("kuai",),
    "差": ("chai",),
    "传": ("zhuan",),
    "觉": ("jiao",),
    "角": ("jue",),
    "秘": ("bi",),
    "血": ("xie",),
    "强": ("jiang",),
    "曾": ("zeng",),
    "省": ("xing",),
    "系": ("ji",),
    "数": ("shu",),
    "兴": ("xing",),
    "铺": ("pu",),
    "磨": ("mo",),
    "削": ("xue",),
    "尽": ("jin",),
    "任": ("ren",),
    "担": ("dan",),
    "漫": ("man",),
    "禁": ("jin",),
    "逮": ("dai",),
    "堡": ("pu",),
    "贴": ("tie",),
    "缝": ("feng",),
    "露": ("lou",),
    "漂": ("piao",),
    "扎": ("zha",),
    "什": ("shen",),
    "谷": ("gu",),
    "木": ("mu",),
}


@dataclass(frozen=True)
class Romanized:
    pinyin: str
    initials: str
    pinyin_alts: tuple[str, ...] = ()
    initials_alts: tuple[str, ...] = ()

    def tokens(self) -> list[str]:
        values: list[str] = []
        for value in (self.pinyin, *self.pinyin_alts, self.initials, *self.initials_alts):
            if value and value not in values:
                values.append(value)
        return values


def load_pinyin_map(path=DEFAULT_PINYIN_PATH) -> dict[str, str]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if str(item).strip()}


class PinyinEngine:
    def __init__(self, pinyin_map=None, map_path=DEFAULT_PINYIN_PATH):
        self.map = dict(pinyin_map) if isinstance(pinyin_map, dict) else load_pinyin_map(map_path)
        self._phrase_lengths = sorted({len(phrase) for phrase in PHRASE_OVERRIDES}, reverse=True)

    def _phrase_at(self, text: str, index: int) -> tuple[str, tuple[str, ...]] | None:
        for length in self._phrase_lengths:
            if length > MAX_PHRASE_LENGTH:
                continue
            chunk = text[index:index + length]
            syllables = PHRASE_OVERRIDES.get(chunk)
            if syllables and len(chunk) == length:
                return chunk, syllables
        return None

    def _syllables(self, text: str) -> list[tuple[str, tuple[str, ...], bool]]:
        """Return (syllable, source characters, came-from-CJK) per position."""
        units: list[tuple[str, tuple[str, ...], bool]] = []
        index = 0
        while index < len(text):
            char = text[index]
            phrase = self._phrase_at(text, index) if CJK.match(char) else None
            if phrase:
                chunk, syllables = phrase
                for offset, syllable in enumerate(syllables):
                    units.append((syllable, (chunk[offset],), True))
                index += len(chunk)
                continue
            if CJK.match(char):
                primary = self.map.get(char)
                if primary:
                    units.append((primary, (char,), True))
                else:
                    units.append((char, (char,), True))
            elif KEEPER.match(char):
                units.append((char, (), False))
            index += 1
        return units

    def _candidates(self, source: str, syllable: str) -> list[str]:
        """Alternative readings for one character position, deterministic order."""
        values: list[str] = []
        for candidate in POLYPHONE_ALTS.get(source, ()):
            if candidate != syllable and candidate not in values:
                values.append(candidate)
        mapped = self.map.get(source)
        if mapped and mapped != syllable and mapped not in values:
            values.append(mapped)
        return values

    def romanize(self, text: str) -> Romanized:
        units = self._syllables(str(text or "").lower())
        if not units:
            return Romanized("", "")
        primary_syllables = [syllable for syllable, _, _ in units]
        primary = "".join(primary_syllables)
        initials = "".join(
            (syllable[0] if syllable else "") for syllable, _, from_cjk in units if from_cjk
        )
        if not initials:
            # Pure latin titles have no syllable structure to shorten.
            initials = primary

        alt_pinyin: list[str] = []
        alt_initials: list[str] = []
        for position, (syllable, sources, _) in enumerate(units):
            for source in sources:
                for candidate in self._candidates(source, syllable):
                    variant = list(primary_syllables)
                    variant[position] = candidate
                    text_value = "".join(variant)
                    if text_value and text_value != primary and text_value not in alt_pinyin:
                        alt_pinyin.append(text_value)
                        alt_initials.append("".join(
                            (item[0] if item else "")
                            for item, (_, _, from_cjk) in zip(variant, units)
                            if from_cjk
                        ))
                    if len(alt_pinyin) >= MAX_VARIANTS:
                        break
            if len(alt_pinyin) >= MAX_VARIANTS:
                break
        return Romanized(primary, initials, tuple(alt_pinyin), tuple(alt_initials))


_ENGINE: PinyinEngine | None = None


def default_engine() -> PinyinEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = PinyinEngine()
    return _ENGINE


def romanize(text: str) -> Romanized:
    return default_engine().romanize(text)
