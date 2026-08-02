"""Transcript post-processing shared by the dictation engine and its tests.

Whisper is good but it has two well-known habits that make raw output unsafe to
paste straight into whatever window you had focused:

* on silence or noise it invents a stock phrase it saw a million times in its
  training subtitles ("Thank you.", "Tack för att du tittade!"), and
* it has never heard your proper nouns, so "aiOS" comes back as "AI OS",
  "I-O-S", "Ayos"…

Everything here is pure text in / text out so it can be tested without a model,
a microphone or a GPU.
"""

from __future__ import annotations

import re
import unicodedata

# Phrases that are never something a person dictates at their desk. These are
# dropped on sight, whatever the acoustic model thought it heard.
SUBTITLE_ARTIFACTS = (
    "thanks for watching",
    "thanks for watching!",
    "thank you for watching",
    "thank you for watching!",
    "please subscribe",
    "like and subscribe",
    "subscribe to my channel",
    "see you in the next video",
    "tack for att du tittade",
    "tack for att ni tittade",
    "vi ses i nasta video",
    "prenumerera",
)

# Stock one-liners Whisper falls back on when it is handed silence. These are
# also perfectly ordinary things to say to a voice agent ("thanks, bye"), so
# they are only dropped when the decoder itself reports that the audio was
# probably not speech.
SILENCE_FALLBACKS = (
    "thank you",
    "thank you very much",
    "thanks",
    "bye",
    "bye bye",
    "goodbye",
    "you",
    "oh",
    "uh",
    "um",
    "hmm",
    "mm",
    "the end",
    "tack",
    "tack sa mycket",
    "hej da",
    "vi ses",
    "ja",
)

# How certain the model has to be that a clip was not speech before the
# ambiguous list is allowed to fire.
NO_SPEECH_CUTOFF = 0.6

# Bracketed sound events, music notes and bare punctuation are never dictation.
_NON_SPEECH_RE = re.compile(
    r"^\s*[\[\(\*♪&#\.\-—…]*\s*"
    r"(blank_audio|blank audio|music|silence|applause|laughter|inaudible|no audio|musik|tystnad)?"
    r"\s*[\]\)\*♪\.\-—…!?]*\s*$",
    re.IGNORECASE,
)
_CREDIT_RE = re.compile(
    r"^(subtitles?|subs|undertext\w*|svensk text|översättning|oversattning|translation|"
    r"transcription|transcript)\b[^.]{0,60}(by|av|:)\b.{0,60}$",
    re.IGNORECASE,
)


def _fold(text):
    """Casefold and strip accents/punctuation so phrase matching is forgiving."""
    stripped = unicodedata.normalize("NFKD", str(text or ""))
    stripped = "".join(char for char in stripped if not unicodedata.combining(char))
    stripped = stripped.casefold()
    stripped = re.sub(r"[^\w\s]+", " ", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


_SUBTITLE_FOLDED = frozenset(_fold(phrase) for phrase in SUBTITLE_ARTIFACTS)
_SILENCE_FOLDED = frozenset(_fold(phrase) for phrase in SILENCE_FALLBACKS)


def is_non_speech_marker(text):
    """True for `[BLANK_AUDIO]`, `♪`, `...` and friends — safe to drop anywhere."""
    raw = str(text or "").strip()
    return not raw or bool(_NON_SPEECH_RE.match(raw))


def is_hallucination(text, no_speech_prob=None):
    """True when the transcript is a Whisper silence artifact rather than speech.

    Matches the whole transcript or nothing, so real speech that merely starts
    with "thank you" is never dropped. The ambiguous one-liners in
    ``SILENCE_FALLBACKS`` additionally require the decoder to have reported a
    high no-speech probability, which is what separates a stray "Thank you."
    over fan noise from the user actually thanking the agent.
    """
    raw = str(text or "").strip()
    if is_non_speech_marker(raw):
        return True
    folded = _fold(raw)
    if not folded:
        return True
    # Anything with real length is real speech, whatever it starts with.
    if len(folded.split()) > 8:
        return False
    if folded in _SUBTITLE_FOLDED or _CREDIT_RE.match(raw):
        return True
    if folded in _SILENCE_FOLDED:
        try:
            probability = float(no_speech_prob)
        except (TypeError, ValueError):
            return False
        return probability >= NO_SPEECH_CUTOFF
    return False


def apply_replacements(text, replacements):
    """Whole-word, case-insensitive substitution that keeps the user's casing.

    A replacement written lower case still fires on a capitalized occurrence at
    the start of a sentence, and restores the capital.
    """
    result = str(text or "")
    if not result or not replacements:
        return result
    for source, target in replacements.items():
        source = str(source or "").strip()
        if not source:
            continue
        target = str(target or "")
        # \b does not fire next to punctuation-only tokens, so fall back to a
        # whitespace boundary when the term does not start/end with a word char.
        left = r"\b" if source[:1].isalnum() else r"(?<!\S)"
        right = r"\b" if source[-1:].isalnum() else r"(?!\S)"
        pattern = re.compile(left + re.escape(source) + right, re.IGNORECASE)

        def substitute(match, replacement=target):
            found = match.group(0)
            if len(found) > 1 and found.isupper():
                return replacement.upper()
            if found[:1].isupper() and replacement[:1].islower():
                return replacement[:1].upper() + replacement[1:]
            return replacement

        result = pattern.sub(substitute, result)
    return result


def build_initial_prompt(vocabulary, base=""):
    """Bias the decoder toward the user's proper nouns.

    Whisper conditions on this text as if it were the previous segment, so a
    plain comma-separated list of the words is the most reliable form.
    """
    terms = [str(term).strip() for term in (vocabulary or []) if str(term).strip()]
    parts = [str(base or "").strip()]
    if terms:
        # Keep it well under Whisper's 224-token conditioning window.
        joined = ", ".join(terms)[:700]
        parts.append(f"Vocabulary used by this speaker: {joined}.")
    return " ".join(part for part in parts if part).strip()


def tidy_transcript(text):
    """Cosmetic cleanup: collapse whitespace and unstick trailing punctuation.

    Deliberately does NOT collapse repeated words — "that was very very good"
    is ordinary speech. Duplicates that come from a chunk boundary are handled
    by :func:`join_chunks`, which knows where the seams actually are.
    """
    result = re.sub(r"\s+", " ", str(text or "")).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", result)


def join_chunks(parts):
    """Join per-chunk transcripts, dropping words duplicated across a seam.

    Whisper's VAD sometimes trims the same syllable into both sides of a
    flush, so the last word of one chunk reappears as the first word of the
    next. Only the seam is examined, so repetition the user actually spoke
    inside a chunk is left alone.
    """
    cleaned = [tidy_transcript(part) for part in (parts or [])]
    cleaned = [part for part in cleaned if part]
    if not cleaned:
        return ""
    joined = cleaned[0]
    for part in cleaned[1:]:
        left = re.search(r"(\w+)\W*$", joined)
        right = re.match(r"^\W*(\w+)", part)
        if left and right and left.group(1).casefold() == right.group(1).casefold():
            # Drop the repeat from the incoming chunk, keeping its punctuation.
            part = part[right.end(1):].lstrip()
            part = re.sub(r"^[^\w]+", "", part)
        joined = f"{joined} {part}".strip() if part else joined
    return tidy_transcript(joined)


def clean_transcript(text, *, replacements=None, filter_hallucinations=True, no_speech_prob=None):
    """The full path from raw Whisper output to something safe to type.

    Returns an empty string when the transcript was only a silence artifact.
    """
    result = tidy_transcript(text)
    if not result:
        return ""
    if filter_hallucinations and is_hallucination(result, no_speech_prob=no_speech_prob):
        return ""
    return apply_replacements(result, replacements or {})
