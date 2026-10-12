"""Per-reader HLS sequence continuity for VLC's adaptive demux gap assertion.

The relay may compact missing source media sequence numbers so VLC sees a
monotonic playlist.  AES-128's implicit IV is the *source* media sequence
number, so encrypted segments receive an explicit IV before that sequence is
rewritten.
"""
import math
import re
import threading
from collections import OrderedDict, namedtuple


_lock = threading.Lock()
_windows = OrderedDict()
_Window = namedtuple(
    "_Window",
    "source_start source_end synthetic_start segment_count output "
    "synthetic_gap_at_start discontinuity_offset effective_source_dseq "
    "source_discontinuities",
)


def _tag_number(lines, prefix, default=None):
    matches = [(index, line.strip()[len(prefix):].strip())
               for index, line in enumerate(lines)
               if line.strip().startswith(prefix)]
    if not matches:
        return None, default
    if len(matches) != 1 or not matches[0][1].isdigit():
        return None, None
    return matches[0][0], int(matches[0][1])


def _attributes(tag_line):
    """Parse an HLS attribute list without splitting commas inside quotes."""
    rest = tag_line.partition(":")[2]
    fields = []
    field = []
    quoted = False
    escaped = False
    for char in rest:
        if escaped:
            field.append(char)
            escaped = False
        elif char == "\\" and quoted:
            field.append(char)
            escaped = True
        elif char == '"':
            field.append(char)
            quoted = not quoted
        elif char == "," and not quoted:
            fields.append("".join(field).strip())
            field = []
        else:
            field.append(char)
    if quoted or escaped:
        return None
    fields.append("".join(field).strip())

    attrs = {}
    for item in fields:
        name, separator, value = item.partition("=")
        if not separator or not name.strip():
            return None
        name = name.strip().upper()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        attrs[name] = value
    return attrs


def _implicit_aes128_key(tag_line):
    attrs = _attributes(tag_line)
    if not attrs:
        return False
    return (attrs.get("METHOD", "").upper() == "AES-128"
            and attrs.get("KEYFORMAT", "identity") == "identity"
            and "IV" not in attrs)


def _key_tag_with_iv(tag_line, source_sequence):
    return "%s,IV=0x%032x" % (tag_line, source_sequence)


def _segment_durations(lines):
    durations = []
    for line in lines:
        if line.strip().startswith("#EXTINF:"):
            value = line.strip()[8:].split(",", 1)[0].strip()
            try:
                duration = float(value)
            except (TypeError, ValueError):
                raise ValueError("invalid live segment duration")
            if not math.isfinite(duration) or not 0 < duration < 3600:
                raise ValueError("invalid live segment duration")
            durations.append(duration)
    return durations


def _leading_discontinuity(lines, first_segment_index):
    return any(line.strip() == "#EXT-X-DISCONTINUITY"
               for line in lines[:first_segment_index])


def _source_discontinuities(lines, source_start):
    """Return source sequence numbers whose preceding boundary is a tag."""
    events = []
    segment_index = 0
    for line in lines:
        stripped = line.strip()
        if stripped == "#EXT-X-DISCONTINUITY":
            events.append(source_start + segment_index)
        elif stripped and not stripped.startswith("#"):
            segment_index += 1
    return tuple(events)


def _rewrite(lines, source_start, synthetic_start, dseq_index, source_dseq,
             discontinuity_offset, synthetic_gap_at_start):
    """Rewrite the media sequence and retain sequence-dependent HLS meaning."""
    output_dseq = source_dseq + discontinuity_offset
    result = []
    first_extinf = next(index for index, line in enumerate(lines)
                        if line.strip().startswith("#EXTINF:"))
    insert_dseq = dseq_index is None and (
        output_dseq != 0 or synthetic_gap_at_start or
        any(line.strip() == "#EXT-X-DISCONTINUITY" for line in lines))
    first_discontinuity = next((index for index, line in enumerate(lines)
                                if line.strip() == "#EXT-X-DISCONTINUITY"), None)
    if insert_dseq:
        # Keep the required ordering before any discontinuity tag. Usually
        # this is immediately after MEDIA-SEQUENCE; malformed source ordering
        # can place that tag earlier, in which case insert before it.
        sequence_index = next(index for index, line in enumerate(lines)
                              if line.strip().startswith("#EXT-X-MEDIA-SEQUENCE:"))
        insert_dseq_at = (first_discontinuity
                          if first_discontinuity is not None
                          and first_discontinuity < sequence_index
                          else sequence_index + 1)
    else:
        insert_dseq_at = None
    segment_index = 0
    active_key = None
    pending_extinf = False

    for index, line in enumerate(lines):
        if insert_dseq_at == index:
            result.append("#EXT-X-DISCONTINUITY-SEQUENCE:%d" % output_dseq)
        stripped = line.strip()
        if stripped.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            result.append("#EXT-X-MEDIA-SEQUENCE:%d" % synthetic_start)
            continue
        if stripped.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
            result.append("#EXT-X-DISCONTINUITY-SEQUENCE:%d" % output_dseq)
            continue
        if stripped.startswith("#EXT-X-KEY:"):
            active_key = stripped
        if stripped.startswith("#EXTINF:"):
            if index == first_extinf and synthetic_gap_at_start:
                result.append("#EXT-X-DISCONTINUITY")
            result.append(line)
            pending_extinf = True
            continue
        if stripped and not stripped.startswith("#"):
            if pending_extinf:
                if _implicit_aes128_key(active_key or ""):
                    result.append(_key_tag_with_iv(
                        active_key, source_start + segment_index))
                result.append(line)
                segment_index += 1
                pending_extinf = False
                continue
            result.append(line)
            continue
        result.append(line)

    return "\n".join(result)


def normalize(text, base_url, session):
    """Compact gaps in a marked live media playlist for one VLC playback.

    AES-128 implicit IVs continue to use each segment's original sequence.
    An overlapping stale response returns the latest normalized window. A
    non-overlapping sequence rewind is ambiguous between a delayed request and
    an upstream restart, so it fails closed for the caller to reopen playback.
    Unsupported/delta playlists are passed through because their omitted
    segment numbering is ambiguous.
    """
    if not session or not re.fullmatch(r"[0-9a-f]{32}", session):
        return text
    if "#EXTINF:" not in text or "#EXT-X-ENDLIST" in text:
        return text
    if "#EXT-X-PLAYLIST-TYPE:VOD" in text or "#EXT-X-SKIP:" in text:
        return text

    lines = text.splitlines()
    durations = _segment_durations(lines)
    if not durations:
        return text

    sequence_index, source_start = _tag_number(
        lines, "#EXT-X-MEDIA-SEQUENCE:")
    if sequence_index is None:
        # Without an explicit sequence, a sliding live window cannot be
        # distinguished from a new stream while preserving AES implicit IVs.
        return text
    first_segment_index = next((index for index, line in enumerate(lines)
                                if line.strip() and not line.strip().startswith("#")), None)
    first_extinf_index = next((index for index, line in enumerate(lines)
                               if line.strip().startswith("#EXTINF:")), None)
    if (first_segment_index is None or first_extinf_index is None
            or sequence_index >= first_extinf_index):
        return text

    uri_indexes = [index for index, line in enumerate(lines)
                   if line.strip() and not line.strip().startswith("#")]
    if len(uri_indexes) != len(durations):
        return text

    dseq_index, source_dseq = _tag_number(
        lines, "#EXT-X-DISCONTINUITY-SEQUENCE:", default=0)
    first_discontinuity_index = next((index for index, line in enumerate(lines)
                                      if line.strip() == "#EXT-X-DISCONTINUITY"), None)
    if (source_dseq is None or (dseq_index is not None and
                                (dseq_index >= first_extinf_index or
                                 (first_discontinuity_index is not None and
                                  dseq_index > first_discontinuity_index)))):
        return text

    key = (session, base_url)
    segment_count = len(uri_indexes)
    source_end = source_start + segment_count
    has_leading_discontinuity = _leading_discontinuity(lines, first_segment_index)
    source_discontinuities = _source_discontinuities(lines, source_start)

    with _lock:
        previous = _windows.get(key)
        if previous is None:
            synthetic_start = 0
            discontinuity_offset = 0
            synthetic_gap_at_start = False
            effective_source_dseq = source_dseq
        elif (source_start < previous.source_start or
              (source_start == previous.source_start and
               source_end < previous.source_end)):
            if source_start < previous.source_start:
                if source_end <= previous.source_start:
                    # This could be a delayed old response or an upstream
                    # sequence reset. There is no overlap proving it is stale,
                    # so fail closed and let playback reopen with a new token.
                    raise ValueError("live media sequence reset or stale window")
                # An overlapping delayed response cannot rewind VLC's current
                # live window. Return the latest response already normalized.
                _windows.move_to_end(key)
                return previous.output
            # A shorter window with the same start is an out-of-order response.
            _windows.move_to_end(key)
            return previous.output
        elif source_start == previous.source_start:
            synthetic_start = previous.synthetic_start
            discontinuity_offset = previous.discontinuity_offset
            synthetic_gap_at_start = previous.synthetic_gap_at_start
            effective_source_dseq = (source_dseq if dseq_index is not None
                                     else previous.effective_source_dseq)
        else:
            passed_previous_gap = int(previous.synthetic_gap_at_start)
            discontinuity_offset = previous.discontinuity_offset + passed_previous_gap
            if dseq_index is not None:
                effective_source_dseq = source_dseq
            else:
                previous_events_before = sum(
                    event < source_start for event in previous.source_discontinuities)
                previous_events_at_start = sum(
                    event == source_start for event in previous.source_discontinuities)
                current_events_at_start = sum(
                    event == source_start for event in source_discontinuities)
                dropped_boundary_events = max(
                    0, previous_events_at_start - current_events_at_start)
                effective_source_dseq = (
                    previous.effective_source_dseq + previous_events_before
                    + dropped_boundary_events)
            if source_start <= previous.source_end:
                synthetic_start = (previous.synthetic_start
                                   + source_start - previous.source_start)
                synthetic_gap_at_start = False
            else:
                synthetic_start = previous.synthetic_start + previous.segment_count
                synthetic_gap_at_start = not has_leading_discontinuity

        normalized = _rewrite(
            lines, source_start, synthetic_start, dseq_index, effective_source_dseq,
            discontinuity_offset, synthetic_gap_at_start)
        _windows[key] = _Window(
            source_start, source_end, synthetic_start, segment_count,
            normalized, synthetic_gap_at_start, discontinuity_offset,
            effective_source_dseq, source_discontinuities)
        _windows.move_to_end(key)
        while len(_windows) > 128:
            _windows.popitem(last=False)
        return normalized
