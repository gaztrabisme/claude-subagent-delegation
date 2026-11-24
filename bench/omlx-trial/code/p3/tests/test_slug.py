import pytest

from slug import slugify


def test_basic_words():
    assert slugify("Hello World") == "hello-world"


def test_accents_are_folded():
    assert slugify("Crème Brûlée") == "creme-brulee"


def test_runs_of_symbols_and_spaces_collapse():
    assert slugify("  Multiple   spaces & symbols!!  ") == "multiple-spaces-symbols"


def test_existing_hyphens_are_kept_single():
    assert slugify("Already-slugged-text") == "already-slugged-text"


def test_empty_input_gives_na():
    assert slugify("") == "n-a"


def test_only_symbols_gives_na():
    assert slugify("!!!") == "n-a"


def test_non_latin_text_gives_na():
    assert slugify("日本語") == "n-a"


def test_truncation_drops_trailing_hyphen():
    assert slugify("one two three four five", 8) == "one-two"


def test_truncation_to_ten_characters():
    assert slugify("one two three four five", 10) == "one-two-th"


def test_default_max_len_is_60_and_has_no_trailing_hyphen():
    result = slugify("word " * 40)
    assert len(result) <= 60
    assert not result.endswith("-")
    assert result.startswith("word-word")


def test_more_accent_folding():
    assert slugify("Ångström") == "angstrom"
    assert slugify("Žluťoučký kůň") == "zlutoucky-kun"


def test_max_len_zero_raises():
    with pytest.raises(ValueError):
        slugify("anything", 0)
