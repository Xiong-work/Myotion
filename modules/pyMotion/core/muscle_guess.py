"""
modules/pyMotion/core/muscle_guess.py — best-effort channel-label -> muscle
short-name guessing, shared by main.py (EMGAddWindow's single-participant
wizard) and widgets/channel_mapping_panel.py (Batch Import's channel mapping
panel).

This used to live in main.py, imported into channel_mapping_panel.py via a
deferred `from main import _guess_muscle_from_channel` -- which crashed with
a real NameError ("muscleName is not defined") the first time it actually
ran in the live app. Root cause: main.py -> modules -> ui_functions ->
main.py is circular, so `from main import ...` (main isn't literally the
running __main__ module, so this triggers a fresh, separate import of
main.py) can execute main.py's own `from modules import *` before it has
finished binding names the first time around. Moving this logic to a module
with no path back into main.py/modules removes the circularity entirely.
"""
import re

from thefuzz import fuzz as _fuzz

from .muscleName import muscleName

# Strip known sensor-brand/system prefixes.
_BRAND_STRIP_RE = re.compile(
    r'^(?:'
    r'(?:Delsys|Trigno|Noraxon|Cometa|BioNomadix|BIOPAC|Biometrics|Wave)[.\-_ ]?|'
    r'Sensor\s*\d+[.\-_ ]|'
    r'Ch(?:annel)?\s*\d+[.\-_ ]'
    r')+',
    re.IGNORECASE,
)
# Strip generic EMG prefix/suffix.
_EMG_AFFIX_RE = re.compile(
    r'(?:^EMG[.\-_ ]?|[.\-_ ]?EMG\s*\d*$|\s*\d+$)',
    re.IGNORECASE,
)
# Sync / trigger channels that are never EMG signals.
_SYNC_CHANNEL_RE = re.compile(
    r'\b(?:sync|同步|reference|trigger|trig|clock)\b',
    re.IGNORECASE,
)

_MUSCLE_BASE_NAMES = None


def _get_muscle_bases():
    global _MUSCLE_BASE_NAMES
    if _MUSCLE_BASE_NAMES is None:
        seen, result = set(), []
        for s, l in zip(muscleName.short, muscleName.long):
            base_s, base_l = s[:-2], l[:-2]
            if base_s not in seen:
                seen.add(base_s)
                result.append((base_s, base_l))
        _MUSCLE_BASE_NAMES = result
    return _MUSCLE_BASE_NAMES


def _is_sync_channel(chan: str) -> bool:
    """Return True if the channel is a sync/trigger line, not an EMG signal."""
    return bool(_SYNC_CHANNEL_RE.search(chan))


def _detect_side(chan: str):
    """Return 'L', 'R', or None from channel label."""
    if re.search(r'右|right\b', chan, re.IGNORECASE):
        return 'R'
    if re.search(r'左|left\b', chan, re.IGNORECASE):
        return 'L'
    m = re.search(r'[-_\s.]([RL])\s*$', chan, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return None


def _tokenise_channel(chan: str):
    """Extract meaningful tokens from a channel label for muscle matching."""
    text = _BRAND_STRIP_RE.sub('', chan)
    text = re.sub(r'[一-鿿←-⇿→←↑↓]', ' ', text)  # CJK / arrows
    text = re.sub(r'\b(?:right|left)\b', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'[-_.\\/()[\]]+', ' ', text)
    return [t for t in text.upper().split()
            if len(t) >= 2 and t not in ('L', 'R', 'LL', 'RR')]


def _guess_muscle_from_channel(chan: str):
    """Return best matching muscle short name (with side) from a raw channel label.

    Strategy:
      1. Detect side from Chinese chars (右=R, 左=L) or trailing -R/-L.
      2. Strip brand prefix + EMG affix; try exact / substring match on short names.
      3. Tokenise the remaining text; score each token with partial_ratio against
         every muscle's LONG base name — handles abbreviations like "TIB ANT" for
         "Tibialis Anterior" and "LUMBAR ES" for "Lumbar Erector Spinae".
      4. Combine best-scoring base with detected side.
    """
    side = _detect_side(chan)

    # Fast path: exact or substring match on short names after cleaning
    cleaned = _BRAND_STRIP_RE.sub('', chan)
    cleaned = _EMG_AFFIX_RE.sub('', cleaned)
    normed = re.sub(r'[_\s.]+', '-', cleaned).strip('-').upper()
    for short in muscleName.short:
        if normed == short.upper():
            return short
    for short in muscleName.short:
        if short.upper() in normed:
            return short

    # Token-level matching against long muscle names
    tokens = _tokenise_channel(chan)
    if not tokens:
        return None

    best_score = 65.0
    best_base_s = None
    for base_s, base_l in _get_muscle_bases():
        base_l_lo = base_l.lower()
        scores = [_fuzz.partial_ratio(t.lower(), base_l_lo) for t in tokens]
        avg = sum(scores) / len(scores)
        if avg > best_score:
            best_score = avg
            best_base_s = base_s

    if best_base_s is None:
        return None

    candidate = best_base_s + ('-R' if side == 'R' else '-L' if side == 'L' else '')
    return candidate if candidate in muscleName.short else None
