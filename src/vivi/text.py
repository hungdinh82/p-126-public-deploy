"""Vietnamese text normalization shared by routing and spoken responses."""

import unicodedata


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFD", text.lower())
    return "".join(char for char in value if unicodedata.category(char) != "Mn").replace("đ", "d")
