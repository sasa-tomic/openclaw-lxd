from __future__ import annotations

import sys

sys.path.insert(0, "/projects/automations/twitter")

from twitter_utils import is_english_text


def test_is_english_text_accepts_english():
    assert is_english_text("Cloud costs keep rising while support quality drops.") is True


def test_is_english_text_rejects_spanish():
    assert is_english_text("Los costos de la nube siguen subiendo y el soporte es malo.") is False


def test_is_english_text_rejects_japanese():
    assert is_english_text("クラウドのコストが上がり続けています。") is False
