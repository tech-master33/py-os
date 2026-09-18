"""Keeping model text intact between the wire and the ear.

Two different things go wrong with text that arrives from an AI provider, and
both of them look like nonsense on screen and in speech.

The first is mojibake: bytes that really are UTF-8 read as if they were
Latin-1, so ``café`` arrives as ``cafÃ©`` and an em dash as ``â€"``. That happens
whenever a streamed response is decoded with the wrong charset, which
:mod:`ai_providers` now avoids at the source; :func:`repair_mojibake` is here so
text that was already mangled, or that came back through a conversation, can be
put right again.

The second is characters nothing downstream can carry. An emoji that a provider
split into two surrogate halves leaves a lone surrogate, and a lone surrogate
cannot be encoded to any legacy codepage, so every Windows API boundary turns it
into a question mark. Control characters, private-use glyphs and stray
replacement marks are the same kind of trouble in a window or a voice.

The rule this module follows is the one the assistant promises: the transcript
keeps what the model actually wrote, and only the spoken form drops what a voice
cannot pronounce. That is why :func:`for_display` is gentle and
:func:`for_speech` is not.

Like :mod:`ai_providers` and :mod:`pyos_knowledge`, this module avoids importing
wx so it can be used and tested without a GUI, and it lives at the repository
root so the plugin loader does not treat it as an application.
"""

import re
import unicodedata

# The give-away sequences left behind by decoding UTF-8 as Latin-1. A string
# containing none of these is left completely alone.
MOJIBAKE_HINTS = (
    "\u00c3",  # Ã
    "\u00c2",  # Â
    "\u00e2\u20ac",  # â€
    "\u00f0\u0178",  # ðŸ
    "\u00e2\u0080",  # the raw C1 form, before a display pass
)

# What is allowed to survive into the history: everything except control
# characters and the mangled fragments left by a bad decode. Tabs and newlines
# are text; carriage returns only upset the Windows text control.
_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_REPLACEMENT_RUN_RE = re.compile("\ufffd+")

# Character categories a voice has no sensible way to say. Letters, digits,
# punctuation, spacing and mathematics symbols all stay; pictographs, dingbats,
# private-use glyphs, format marks (variation selectors, joiners), unassigned
# code points and surrogates go.
_UNSPEAKABLE_CATEGORIES = frozenset({"Cs", "Co", "Cn", "Cf", "So", "Sk"})

_DASHES_RE = re.compile("[\u2010-\u2015\u2212]")
_SPACING_RE = re.compile("[ \t]{2,}")
_SPACE_BEFORE_NEWLINE_RE = re.compile(" *\n")
_BLANK_LINES_RE = re.compile("\n{3,}")


def _has_surrogates(text):
    return any("\ud800" <= character <= "\udfff" for character in text)


def _join_surrogate_pairs(text):
    """Rejoin surrogate halves and drop the ones that cannot be repaired.

    A pair written as two escapes is one character that has been split in two;
    a half on its own is not a character at all. Encoding through UTF-16 and
    decoding again does both jobs at once: pairs become the character they
    stand for, and halves become the replacement mark that
    :func:`for_display` then reports and removes.
    """
    if not _has_surrogates(text):
        return text
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


# The two single-byte codecs a bad decode can go through. Latin-1 maps every
# byte to a C1 control character, while cp1252 maps some of them to printable
# characters such as the euro sign or a trademark mark, so text mangled by one
# of them cannot be read back with the other.
_LEGACY_CODECS = ("latin-1", "cp1252")


def repair_mojibake(text):
    """Undo text that was decoded as a legacy codepage when it was really UTF-8.

    The repair is only kept when it is unambiguous: the text has to contain one
    of the tell-tale sequences, re-encoding it has to succeed, and decoding the
    result as strict UTF-8 has to give something that is no longer mangled.
    Ordinary prose that merely contains an accent fails the last test and is
    returned untouched.
    """
    if not text or not any(hint in text for hint in MOJIBAKE_HINTS):
        return text
    for codec in _LEGACY_CODECS:
        try:
            candidate = text.encode(codec).decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
        # A repair that only swaps one kind of soup for another is no repair.
        if candidate != text and not any(hint in candidate for hint in MOJIBAKE_HINTS):
            return candidate
    return text


def is_suspicious(text):
    """Whether this text shows signs of a bad decode.

    Used by the assistant to say once, quietly, that part of an answer could not
    be shown as written, instead of letting the damage pass unnoticed.
    """
    if not text:
        return False
    if _has_surrogates(text):
        return True
    if "\ufffd" in text:
        return True
    if any(hint in text for hint in MOJIBAKE_HINTS):
        return True
    return bool(_CONTROL_RE.search(text))


def for_display(text):
    """Return text fit for the history: repaired, joined and free of controls.

    The model's own words and punctuation are kept, including accents and
    emoji, so the transcript is still what the model wrote and can be copied.
    Only things that are not really text are removed: lone surrogates, control
    characters, and runs of the replacement mark.
    """
    if not text:
        return ""
    text = repair_mojibake(text)
    text = _join_surrogate_pairs(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    text = _REPLACEMENT_RUN_RE.sub("\ufffd", text)
    text = unicodedata.normalize("NFC", text)
    return text


def for_speech(text):
    """Return the speakable form of text, for handing to a voice.

    Everything :func:`for_display` does, minus the characters no voice can say:
    pictographs and symbols are dropped, dashes become pauses, and the spacing
    left behind by a dropped character is tidied up. Accents, digits and normal
    punctuation survive, so ``café`` is still spoken as café and ``5 + 3`` as
    five plus three.
    """
    display = for_display(text)
    if not display:
        return ""

    kept = []
    for character in display:
        if character in ("\n", "\t"):
            kept.append("\n" if character == "\n" else " ")
            continue
        category = unicodedata.category(character)
        if character == "\ufffd" or category in _UNSPEAKABLE_CATEGORIES:
            continue
        kept.append(character)

    speech = "".join(kept)
    speech = _DASHES_RE.sub(", ", speech)
    speech = speech.replace("\u2026", "...")
    speech = speech.replace("\u00a0", " ").replace("\u2009", " ").replace("\u202f", " ")
    speech = _SPACING_RE.sub(" ", speech)
    speech = _SPACE_BEFORE_NEWLINE_RE.sub("\n", speech)
    # A dropped symbol often leaves its punctuation orphaned, so tidy the joins
    # that would read as a stumble: "word ," and ", ," and a line of commas.
    speech = re.sub(r"\s+([,.;:!?])", r"\1", speech)
    speech = re.sub(r"(,\s*){2,}", ", ", speech)
    speech = _BLANK_LINES_RE.sub("\n\n", speech)
    return speech.strip()
