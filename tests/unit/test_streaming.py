import itertools

import pytest

from rag.streaming import ThinkSplitter


def run(chunks):
    s = ThinkSplitter()
    out = []
    for c in chunks:
        out += s.feed(c)
    out += s.flush()
    think = "".join(t for k, t in out if k == "thinking")
    answer = "".join(t for k, t in out if k == "answer")
    return think, answer


def test_plain_answer_without_think():
    assert run(["Hello ", "world"]) == ("", "Hello world")


def test_basic_split():
    assert run(["<think>plan</think>The answer"]) == ("plan", "The answer")


def test_tags_split_across_chunks_every_possible_way():
    full = "<think>reason it out</think>Final [1]."
    for cut1, cut2 in itertools.combinations(range(1, len(full)), 2):
        assert run([full[:cut1], full[cut1:cut2], full[cut2:]]) == ("reason it out", "Final [1]."), (cut1, cut2)


def test_one_character_at_a_time():
    assert run(list("<think>abc</think>xyz")) == ("abc", "xyz")


def test_partial_tag_prefix_is_not_swallowed_when_it_is_just_text():
    assert run(["a <thi", "s is fine"]) == ("", "a <this is fine")


def test_less_than_sign_in_answer():
    assert run(["5 < 6 and 7 > 3"]) == ("", "5 < 6 and 7 > 3")


def test_unclosed_think_is_flushed_as_thinking():
    assert run(["<think>never closed"]) == ("never closed", "")


def test_multiple_blocks():
    assert run(["<think>a</think>X<think>b</think>Y"]) == ("ab", "XY")


def test_stray_close_tag_is_hidden_from_users():
    assert run(["reasoning</think>answer"]) == ("", "reasoninganswer")


def test_empty_think_block():
    assert run(["<think></think>ok"]) == ("", "ok")


def test_empty_stream():
    assert run([]) == ("", "")


def test_tokens_stream_incrementally():
    s = ThinkSplitter()
    assert s.feed("<think>he") == [("thinking", "he")]
    assert s.feed("llo</think>hi") == [("thinking", "llo"), ("answer", "hi")]
