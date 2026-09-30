"""IME candidate hotwords for the search box.

Film titles come from the Douban catalogue the app already keeps fresh
(`catalog.home` for the five hot categories), so this adds no scraping and no
dependency. A bounded generic supplement is scraped straight from the public
Baidu hotboard and is best-effort only: any failure still ships film words.

No user input is ever sent upstream: `catalog.home` receives a category name
and the supplement request is one fixed public URL.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path


CATEGORIES = ("movie", "tv", "anime", "variety", "short")
GENERAL = "general"
WORD_KINDS = frozenset(CATEGORIES) | {GENERAL}

CACHE_MAX_AGE = 6 * 60 * 60
CACHE_SCHEMA = 1
MAX_WORDS = 800
MAX_GENERAL_WORDS = 30
MAX_PHRASE_LEN = 16

# Backoff for an exhausted refresh: 5s, 10s, 20s ... capped at RETRY_MAX.
RETRY_BASE = 5.0
RETRY_MAX = 300.0
RETRY_EXPONENT_CAP = 16
# A partial refresh retries on a short fixed interval, still with backoff.
PARTIAL_RETRY_DELAY = 30.0

SUPPLEMENT_URL = "https://top.baidu.com/board?tab=realtime"
SUPPLEMENT_TIMEOUT = 6
SUPPLEMENT_MAX_BYTES = 2 * 1024 * 1024
SUPPLEMENT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")
_SDATA = re.compile(r"<!--s-data:(.*?)-->", re.S)

PINYIN_PATH = Path(__file__).with_name("pinyin_map.json")

_PHRASE_STRIP = re.compile(r"[\s,;:!?~`\"'“”‘’()（）\[\]{}<>《》【】…—\-_/\\|+*=&%$#@^|.]+")
_SENTENCE = re.compile(r"[，。！？；、,.!?;]")
_URLISH = re.compile(r"(?i)(?:https?://|www\.)")
_TERM_OK = re.compile(r"[\w\s\u3400-\u9fff]+", re.UNICODE)
_FOLD = re.compile(r"[^0-9a-z\u3400-\u9fff]+")
_SILENT = re.compile(r"[^a-z]+")

SECTION_BONUS = {"最近热门": 120, "最新上映": 80, "豆瓣高分": 40}
DEFAULT_BONUS = 60


def _is_cjk(char):
    code = ord(char)
    return ("\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff"
            or 0x20000 <= code <= 0x323AF)


def load_pinyin_map(path=None):
    """Read the shipped single-reading pinyin table. Never raises."""
    try:
        value = json.loads(Path(path or PINYIN_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {key: reading for key, reading in value.items() if isinstance(reading, str)}


def convert_text(text, pinyin_map=None):
    """`庆余年` -> `("qingyunian", "qyn")`.

    A single unmapped CJK character rejects the whole term: silently dropping it
    would hand the IME a wrong full pinyin for a wrong string. ASCII letters and
    digits pass through (uppercase normalized down), one initial per word.
    """
    table = load_pinyin_map() if pinyin_map is None else pinyin_map
    syllables, initials = [], []
    in_ascii_word = False
    for char in unicodedata.normalize("NFKC", str(text or "")).lower():
        mapped = table.get(char)
        if mapped:
            syllable = _SILENT.sub("", mapped.lower())
            if syllable:
                syllables.append(syllable)
                initials.append(syllable[0])
            in_ascii_word = False
        elif char.isascii() and char.isalnum():
            syllables.append(char)
            if not in_ascii_word:
                initials.append(char)
            in_ascii_word = True
        elif _is_cjk(char):
            return "", ""          # unknown CJK: no guess, no candidate
        else:
            in_ascii_word = False
    return "".join(syllables), "".join(initials)


def _fold(value):
    return _FOLD.sub("", unicodedata.normalize("NFKC", str(value or "")).lower())


def acceptable_phrase(text):
    """Short standalone term only: no sentence punctuation, URL, or long text."""
    phrase = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not phrase or len(phrase) > MAX_PHRASE_LEN:
        return False
    if _URLISH.search(phrase) or _SENTENCE.search(phrase):
        return False
    return _PHRASE_STRIP.sub("", phrase) == phrase or bool(_TERM_OK.fullmatch(phrase))


def filter_phrase_terms(values, limit=MAX_GENERAL_WORDS):
    """Normalize, dedupe, and cap the generic phrases."""
    out, seen = [], set()
    for value in values or []:
        phrase = unicodedata.normalize("NFKC", str(value or "")).strip()
        if not acceptable_phrase(phrase):
            continue
        key = _fold(phrase)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(phrase)
        if len(out) >= limit:
            break
    return out


def _section_rows(payload):
    """Flatten one home payload's sections into (title, items)."""
    if not isinstance(payload, dict):
        return []
    rows = []
    for section in payload.get("sections") or []:
        if isinstance(section, dict):
            rows.append((str(section.get("title") or ""),
                         [row for row in (section.get("items") or []) if isinstance(row, dict)]))
    return rows


def collect_film_terms(homes, pinyin_map=None):
    """`catalog.home` payloads -> ordered, weighted, unique film terms."""
    table = load_pinyin_map() if pinyin_map is None else pinyin_map
    merged, order = {}, []
    for category in CATEGORIES:
        payload = homes.get(category) if isinstance(homes, dict) else None
        if not isinstance(payload, dict):
            continue
        sections = _section_rows(payload)
        sections.sort(key=lambda row: -SECTION_BONUS.get(row[0], DEFAULT_BONUS))
        for rank, (title_key, items) in enumerate(sections):
            for position, row in enumerate(items):
                text = str(row.get("title") or "").strip()
                pinyin, initials = convert_text(text, table)
                key = _fold(text)
                if not text or not pinyin or not key:
                    continue
                # Hotter section and earlier row score higher; CATEGORIES order
                # breaks ties so `movie` outranks the rest.
                weight = (10000 - rank * 600 - min(position, 30) * 10
                          + SECTION_BONUS.get(title_key, DEFAULT_BONUS)
                          + len(CATEGORIES) - CATEGORIES.index(category))
                known = merged.get(key)
                if known is None:
                    order.append(key)
                    merged[key] = {"text": text, "pinyin": pinyin, "initials": initials,
                                   "kind": category, "weight": weight}
                elif weight > known["weight"]:
                    known["weight"] = weight
    return [merged[key] for key in order]


def build_words(film_terms, general_phrases=(), pinyin_map=None, limit=MAX_WORDS):
    """Merge film titles (priority) and generic phrases into the wire shape."""
    table = load_pinyin_map() if pinyin_map is None else pinyin_map
    words, index = [], {}
    general = [{"text": phrase, "kind": GENERAL, "weight": 1} for phrase in (general_phrases or [])]
    for term in list(film_terms or []) + general:
        if len(words) >= limit:
            break
        if not isinstance(term, dict):
            continue
        text = str(term.get("text") or "").strip()
        key = _fold(text)
        if not text or not key:
            continue
        kind = term.get("kind") if term.get("kind") in WORD_KINDS else GENERAL
        pinyin, initials = term.get("pinyin"), term.get("initials")
        if not pinyin or not initials:
            pinyin, initials = convert_text(text, table)
        if not pinyin or not initials:
            continue
        try:
            weight = max(0, min(int(term.get("weight") or 0), 100000))
        except (TypeError, ValueError):
            weight = 0
        known = index.get(key)
        if known is None:
            index[key] = len(words)
            words.append({"text": text, "pinyin": pinyin, "initials": initials,
                          "kind": kind, "weight": weight})
        else:
            word = words[known]
            word["weight"] = max(word["weight"], weight)
            if kind in CATEGORIES and word["kind"] not in CATEGORIES:
                word["kind"] = kind      # a film duplicate outranks a generic one
    return words


def parse_baidu_board(document):
    """Baidu hotboard HTML, or an already-parsed payload -> list of titles."""
    payload = document
    if isinstance(payload, (bytes, bytearray, str)):
        text = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) \
            else payload
        match = _SDATA.search(text)
        if not match:
            return []
        try:
            payload = json.loads(match.group(1))
        except ValueError:
            return []
    if isinstance(payload, dict):
        cards = (payload.get("data") or {}).get("cards") or payload.get("cards") or []
        content = cards[0].get("content") if cards and isinstance(cards[0], dict) else None
        # Some renders nest the rows one level deeper.
        while isinstance(content, list) and content and isinstance(content[0], dict) \
                and isinstance(content[0].get("content"), list) and not content[0].get("word"):
            content = content[0]["content"]
    else:
        content = payload
    titles = []
    for row in (content or [])[:200]:
        if isinstance(row, dict):
            title = str(row.get("word") or row.get("title") or "").strip()
            if title:
                titles.append(title)
        elif isinstance(row, str) and row.strip():
            titles.append(row.strip())
    return titles


def _default_fetch_json(url, timeout=None):
    """Fetch the hotboard page and return its embedded payload as titles."""
    request = urllib.request.Request(url, headers={"User-Agent": SUPPLEMENT_UA,
                                                   "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(request, timeout=timeout or SUPPLEMENT_TIMEOUT) as response:
        body = response.read(SUPPLEMENT_MAX_BYTES + 1)
    if len(body) > SUPPLEMENT_MAX_BYTES:
        raise ValueError("supplement payload too large")
    return parse_baidu_board(body)


def _valid_words(value):
    """Reject a malformed words list rather than passing junk to the client."""
    if not isinstance(value, list) or not value or len(value) > MAX_WORDS:
        return None
    for word in value:
        if not isinstance(word, dict) or set(word) != {"text", "pinyin", "initials",
                                                      "kind", "weight"}:
            return None
        if not all(isinstance(word[key], str) and word[key]
                   for key in ("text", "pinyin", "initials")):
            return None
        if word["kind"] not in WORD_KINDS:
            return None
        if not isinstance(word["weight"], int) or isinstance(word["weight"], bool):
            return None
    return value


def _finite_number(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and -1e18 < number < 1e18 else default


def _usable_home(payload):
    """A home payload is usable only if it is live-ish and actually has rows."""
    if not isinstance(payload, dict) or not payload.get("sections"):
        return False
    if payload.get("upstream_available") is False:
        return False
    if str(payload.get("source") or "") in ("douban-snapshot", "douban-cache",
                                            "douban-unavailable"):
        return False
    return not bool(payload.get("stale"))


def _payload(words, updated_at, source, stale):
    return {"words": words, "updated_at": _finite_number(updated_at),
            "source": source, "stale": bool(stale)}


class HotwordService:
    """Hotword payload for `GET /api/ime/hotwords`.

    `get()` answers immediately from the persisted cache or the built-in
    catalogue snapshot, then starts one coalesced background refresh.
    """

    def __init__(self, catalog, cache_path, fetch_json=None, clock=None):
        self.catalog = catalog
        self.cache_path = Path(cache_path)
        self._fetch_json = fetch_json or _default_fetch_json
        self._clock = clock or time.time
        self._pinyin = load_pinyin_map()
        self._lock = threading.Lock()
        self._refreshing = False
        self._retry_attempts = 0
        self._retry_not_before = 0.0
        self._refreshes = 0
        self._failures = 0
        self._snapshot_cache = None
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def get(self):
        """Return `{words, updated_at, source, stale}` without blocking on the network."""
        now = float(self._clock())
        record = self._load_record()
        words = _valid_words((record or {}).get("words"))
        if record and words:
            updated_at = _finite_number(record.get("updated_at"), now)
            # `degraded` marks a partial refresh: it must not sit quietly for the
            # full 6h, so a short-interval retry keeps filling in the gaps.
            stale = ((now - updated_at) > CACHE_MAX_AGE or bool(record.get("degraded"))
                     or str(record.get("source") or "") != "catalog-live")
            if stale:
                self._start_refresh(now)
            return _payload(words, updated_at, str(record.get("source") or "cache"), stale)
        payload = _payload(self._snapshot_words(), self._snapshot_time(),
                           "catalog-snapshot", True)
        self._start_refresh(now)
        return payload

    def stats(self):
        with self._lock:
            return {"refreshes": self._refreshes, "failures": self._failures,
                    "retry_attempts": self._retry_attempts, "refreshing": self._refreshing}

    # ---- cache ------------------------------------------------------

    def _load_record(self):
        """Return a validated record, or None. A corrupt cache is simply ignored."""
        try:
            value = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(value, dict) or value.get("schema") != CACHE_SCHEMA:
            return None
        if _valid_words(value.get("words")) is None:
            return None
        value["updated_at"] = _finite_number(value.get("updated_at"))
        return value

    def _write_cache(self, record):
        """Atomic: write a temp file in the same directory, then os.replace."""
        tmp = self.cache_path.with_name(self.cache_path.name + ".tmp")
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.cache_path)
        except (OSError, TypeError, ValueError):
            try:
                tmp.unlink()
            except OSError:
                pass

    # ---- built-in snapshot -----------------------------------------

    def _snapshot_time(self):
        """The snapshot's own build time. Fixed, so `updated_at` never drifts."""
        snapshot = getattr(self.catalog, "snapshot", None)
        if not isinstance(snapshot, dict):
            return 0.0
        for key in ("generatedAt", "generated_at", "built_at"):
            raw = snapshot.get(key)
            if isinstance(raw, bool):
                continue
            if isinstance(raw, (int, float)):
                return _finite_number(raw)
            if isinstance(raw, str) and raw.strip():
                try:
                    return datetime.fromisoformat(raw.strip().replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
        return 0.0

    def _snapshot_words(self):
        """Titles from the catalogue's bundled snapshot. Never touches the network."""
        with self._lock:
            if self._snapshot_cache is not None:
                return self._snapshot_cache
        homes = {category: self._snapshot_home(category) for category in CATEGORIES}
        words = build_words(collect_film_terms(homes, self._pinyin), (), self._pinyin)
        with self._lock:
            self._snapshot_cache = words
        return words

    def _snapshot_home(self, category):
        """One category's bundled snapshot payload; never touches the network."""
        snapshot = getattr(self.catalog, "snapshot", None)
        if not isinstance(snapshot, dict):
            return {}
        home_block = snapshot.get("home")
        if not isinstance(home_block, dict):
            return {}
        payload = home_block.get(category)
        return payload if isinstance(payload, dict) else {}

    # ---- background refresh ----------------------------------------

    def _start_refresh(self, now):
        """Coalesce: one refresh at a time, and none during a failure backoff."""
        with self._lock:
            if self._refreshing or self._retry_not_before > now:
                return
            self._refreshing = True
        threading.Thread(target=self._refresh, name="ime-hotwords", daemon=True).start()

    def _next_retry_delay(self):
        """Exponential with a capped exponent, so a long outage cannot overflow."""
        with self._lock:
            attempts = min(self._retry_attempts, RETRY_EXPONENT_CAP)
            self._retry_attempts += 1
            delay = min(RETRY_BASE * 2 ** attempts, RETRY_MAX)
            self._retry_not_before = float(self._clock()) + delay
            return delay

    def _refresh(self):
        previous = self._load_record()
        try:
            # `degraded` is judged on THIS round's upstream answer only. A
            # fallback payload fills in words below, but must never make a
            # failed category look like a successful refresh (that would push
            # the next attempt out by a full 6h).
            homes, degraded = {}, []
            for category in CATEGORIES:
                try:
                    payload = self.catalog.home(category, force_refresh=True)
                except Exception:
                    payload = None
                if not _usable_home(payload):
                    degraded.append(category)
                    # Fill words from the last good record, else the built-in
                    # snapshot, so one bad shelf never empties the list.
                    homes[category] = ((previous or {}).get("homes") or {}).get(category) \
                        or self._snapshot_home(category)
                else:
                    homes[category] = payload

            films = collect_film_terms(homes, self._pinyin)
            if not films:
                raise ValueError("no usable catalogue home payload")

            phrases = self._supplement()
            if not phrases:
                # A dead generic source must not drop the previous general words;
                # they stay as the fallback tail of the list.
                phrases = [word["text"] for word in (previous or {}).get("words") or []
                           if isinstance(word, dict) and word.get("kind") == GENERAL]
            words = build_words(films, filter_phrase_terms(phrases), self._pinyin)
            if not words:
                raise ValueError("no hotwords built")

            if len(degraded) == len(CATEGORIES):
                # Nothing came back live: keep the old record and its timestamp
                # untouched rather than relabelling stale words as fresh.
                raise ValueError("all catalogue categories unavailable")
            self._write_cache({"schema": CACHE_SCHEMA, "updated_at": float(self._clock()),
                               "source": "catalog-live", "homes": homes,
                               "degraded": degraded, "words": words})
            with self._lock:
                if degraded:
                    # A partial refresh must still back off, otherwise every
                    # stale GET re-pulls all five categories upstream.
                    self._defer_locked(PARTIAL_RETRY_DELAY)
                else:
                    self._retry_attempts = 0
                    self._retry_not_before = 0.0
            self._refreshes += 1
        except Exception:
            self._next_retry_delay()
            with self._lock:
                self._failures += 1
        finally:
            with self._lock:
                self._refreshing = False

    def _defer_locked(self, delay):
        self._retry_not_before = float(self._clock()) + delay

    def _supplement(self):
        """Best-effort generic phrases; any problem here yields no extra words."""
        try:
            payload = self._fetch_json(SUPPLEMENT_URL, timeout=SUPPLEMENT_TIMEOUT)
        except Exception:
            return []
        return filter_phrase_terms(parse_baidu_board(payload))


__all__ = ["HotwordService", "CATEGORIES", "MAX_WORDS", "MAX_GENERAL_WORDS", "CACHE_MAX_AGE",
           "SUPPLEMENT_URL", "convert_text", "load_pinyin_map", "collect_film_terms",
           "build_words", "filter_phrase_terms", "acceptable_phrase", "parse_baidu_board"]
