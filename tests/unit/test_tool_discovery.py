"""Documentation discovery retains Vietnamese spelling without choosing intent."""

import pytest

from src.vivi.capabilities.registry import catalog
from src.vivi.dialogue.tool_discovery import discover_capabilities


@pytest.mark.parametrize("text", ["tạm dừng nhạc và bật đèn pha", "bật nhạc và bật đèn"])
def test_lamp_word_does_not_discover_navigation_destination(text):
    names = {c["name"] for c in discover_capabilities(text, catalog(True))}
    assert "navigation.plan" not in names
    assert names.intersection({"media.play", "media.pause"})


@pytest.mark.parametrize("text", ["đến Huế", "den Hue", "dến Huế"])
def test_destination_spelling_and_missing_accents_remain_discoverable(text):
    names = {c["name"] for c in discover_capabilities(text, catalog(True))}
    assert names == {"navigation.plan"}


@pytest.mark.parametrize("text", ["mở cửa sổ bên trái", "mo cua so ben trai", "mơ cua so ben trai"])
def test_specific_window_phrase_does_not_add_cabin_or_body_tools(text):
    names = {c["name"] for c in discover_capabilities(text, catalog(True))}
    assert names == {"window.set_position"}


def test_unknown_words_preserve_discovery_instead_of_declaring_unsupported():
    capabilities = catalog(True)
    assert discover_capabilities("xyzzy", capabilities) == capabilities
