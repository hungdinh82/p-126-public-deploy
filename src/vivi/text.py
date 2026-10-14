"""Vietnamese text normalization shared by routing and spoken responses."""

import re
import unicodedata

# "ViVi ơi" / "Hey ViVi" in front of a command: after a wake word, STT often keeps
# the call itself ("ViVi ơi, mở cốp sau"), and the parser must see only the command.
_ADDRESS = re.compile(
    r"^\s*(?:(?:hey|hây|hay|hê|ê|này|nè|alo)[\s,]+)?v[iy]\s*v[iy](?:\s+(?:ơi|à|ạ|nhé|nha))?(?=[\s,.!?…:-]|$)[\s,.!?…:-]*",
    re.IGNORECASE,
)


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFD", text.lower())
    return "".join(char for char in value if unicodedata.category(char) != "Mn").replace("đ", "d")


# Words TTS voices read the English way ("ô ka"), spelled as Vietnamese say them.
# Display text keeps "OK"; only what is spoken changes.
_SPOKEN = ((re.compile(r"\b(?:ok|okay|oke)\b", re.IGNORECASE), "ô kê"),)


def spoken_text(text: str) -> str:
    for pattern, said in _SPOKEN:
        text = pattern.sub(said, text)
    return text


def strip_address(text: str) -> str:
    """Drop a leading call to ViVi; a bare call is kept so it can be answered."""
    rest = _ADDRESS.sub("", unicodedata.normalize("NFC", text), count=1)
    return rest if rest.strip() else text


def strip_fillers(text: str) -> str:
    """Drop leading hesitation sounds with punctuation, never a bare consent."""
    return re.sub(r"^\s*(?:(?:ừm|ờm|ờ|à|uhm|hmm)[,.…:;]+\s*)+(?=\S)", "", text, flags=re.I)
