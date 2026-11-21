import json

import pytest

from clipper.output import FIELDS, build_sidecar, validate_sidecar, write_sidecar


def make(**over):
    kw = dict(clip_id="youtube_abc_000012_a1b2c3",
              source_url="https://www.youtube.com/watch?v=abc", platform="youtube",
              start=12.0, end=40.5, duration=27.9, score=0.71,
              transcript="This is the first sentence. And here is some more.",
              title="A title", description="A description.",
              hashtags=["#one", "#two", "#three"])
    kw.update(over)
    return build_sidecar(**kw)


def test_built_sidecar_is_valid_and_has_exactly_the_documented_fields():
    d = make()
    assert validate_sidecar(d) == []
    assert set(d) == set(FIELDS)


def test_long_title_is_cut_to_80_characters():
    d = make(title="word " * 40)
    assert len(d["title"]) <= 80
    assert validate_sidecar(d) == []


def test_missing_metadata_gets_safe_defaults():
    d = make(title="", description="", hashtags=None)
    assert d["title"] == "This is the first sentence."
    assert 3 <= len(d["hashtags"]) <= 5
    assert validate_sidecar(d) == []


def test_too_many_hashtags_are_trimmed_to_five():
    d = make(hashtags=[f"#t{i}" for i in range(9)])
    assert len(d["hashtags"]) == 5


@pytest.mark.parametrize("change", [
    {"title": "x" * 81}, {"hashtags": ["#a", "#b"]}, {"hashtags": ["a", "#b", "#c"]},
    {"platform": "vimeo"}, {"score": 1.5}, {"end_time": 1.0}, {"schema_version": 2},
    {"extra": 1},
])
def test_validator_rejects_bad_values(change):
    d = make()
    d.update(change)
    assert validate_sidecar(d) != []


def test_validator_rejects_missing_field():
    d = make()
    del d["transcript"]
    assert validate_sidecar(d) == ["missing field: transcript"]


def test_write_round_trips_and_refuses_invalid(tmp_path):
    d = make()
    p = write_sidecar(tmp_path, d)
    assert json.loads(p.read_text(encoding="utf-8")) == d
    d["title"] = ""
    with pytest.raises(ValueError):
        write_sidecar(tmp_path, d)
