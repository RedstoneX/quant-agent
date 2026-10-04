"""Placeholder detection for delivered credentials (moved out of src/credentials.py).

Reports a REASON, never the value that triggered it. Re-exported by src.credentials.
"""

# Words a person writes when they mean "replace me". Matched case-insensitively
# as substrings, so `placeholder-alpaca-key` and `PLACEHOLDER` both fire without
# this module ever hardcoding the literal value currently in `.env`.
_PLACEHOLDER_WORDS: tuple[str, ...] = (
    "placeholder",
    "changeme",
    "change-me",
    "change_me",
    "replaceme",
    "replace-me",
    "yourkey",
    "your-key",
    "your_key",
    "yoursecret",
    "your-secret",
    "your_secret",
    "dummy",
    "example",
    "sample",
    "notreal",
    "not-real",
    "fakekey",
    "fake-key",
    "todo",
    "fixme",
    "insert",
    "paste",
    "xxxx",
)


def _normalise_words(text: str) -> str:
    """Lowercase, and reduce every run of non-alphanumeric characters to one space.

    Surrounded by spaces so a whole-word test is a plain substring test on the
    result — `" todo "` is in `" please todo this "` but not in `" aktodoi3x "`.
    """
    out: list[str] = []
    previous_was_separator = True
    for character in text.lower():
        if character.isalnum():
            out.append(character)
            previous_was_separator = False
        elif not previous_was_separator:
            out.append(" ")
            previous_was_separator = True
    return " " + "".join(out).strip() + " "


def _contains_phrase(normalised_value: str, normalised_word: str) -> bool:
    """True when `normalised_word` appears in `normalised_value` on word boundaries."""
    return normalised_word.strip() != "" and normalised_word in normalised_value


def placeholder_reason(value: str) -> str | None:
    """Return a plain-English reason this value is obviously not a real key, else None.

    The reason never contains the value. Callers log the reason.
    """
    if not value:
        return "it is empty"

    # Matched as whole WORDS, not as raw substrings. The substring form had a
    # false-positive surface nobody had measured: a real key is an opaque run of
    # characters, and "todo", "insert" and "xxxx" can all appear inside one by
    # chance — at which point the desk would shout "placeholder" about a working
    # credential. Normalising every non-alphanumeric run to a single space on
    # BOTH sides keeps hyphen/underscore spellings working (`your-key`,
    # `CHANGE_ME`, `placeholder-alpaca-key`) while requiring the word to stand on
    # its own. No threshold and no length rule is introduced by this.
    normalised = _normalise_words(value)
    for word in _PLACEHOLDER_WORDS:
        if _contains_phrase(normalised, _normalise_words(word)):
            return f"it contains the word '{word}', which is what a fill-this-in stand-in looks like"

    # A credential is sent verbatim as an HTTP header value. Whitespace inside
    # one is not a valid credential under any issuing format; it is a copy-paste
    # accident or a sentence someone typed into the field.
    if any(character.isspace() for character in value):
        return "it contains a space or a line break, which a real key never does"

    # One character repeated. No threshold to choose — either the value carries
    # exactly one distinct character or it does not.
    if len(set(value)) == 1:
        return "it is the same character repeated"

    # A credential that is sent as a header value has to be printable ASCII.
    # Anything else is a mangled paste, not a key.
    if any(not (0x21 <= ord(character) <= 0x7E) for character in value):
        return "it contains characters that cannot appear in a real key"

    return None


def looks_like_placeholder(value: str) -> bool:
    """True when `value` is obviously a stand-in rather than a real credential."""
    return placeholder_reason(value) is not None
