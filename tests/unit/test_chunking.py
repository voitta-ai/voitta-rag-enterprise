"""Tests for the markdown chunker + anchor-chunk picker."""

from __future__ import annotations

from voitta_image_rag.services.chunking import (
    ChunkInfo,
    anchor_chunk_for_position,
    chunk_markdown,
)


def test_empty_text_yields_no_chunks() -> None:
    assert chunk_markdown("") == []


def test_short_text_is_one_chunk() -> None:
    chunks = chunk_markdown("hello world")
    assert len(chunks) == 1
    assert chunks[0].text == "hello world"
    assert chunks[0].char_start == 0
    assert chunks[0].char_end == len("hello world")


def test_long_text_yields_multiple_chunks_with_overlap() -> None:
    text = "x" * 6000
    chunks = chunk_markdown(text, target_chars=2000, overlap_chars=200)
    assert len(chunks) >= 3
    for i in range(len(chunks) - 1):
        # consecutive chunks overlap on roughly overlap_chars
        assert chunks[i + 1].char_start < chunks[i].char_end


def test_offsets_are_consistent_with_text() -> None:
    text = "alpha\n\n" + ("beta\n\n" * 200) + "gamma"
    chunks = chunk_markdown(text, target_chars=300, overlap_chars=50)
    assert len(chunks) > 1
    for c in chunks:
        assert text[c.char_start : c.char_end] == c.text


def test_chunker_prefers_paragraph_boundary() -> None:
    text = "a" * 1500 + "\n\n" + "b" * 1500
    chunks = chunk_markdown(text, target_chars=2000, overlap_chars=100)
    # The first chunk should end at the paragraph boundary, not mid-word.
    assert chunks[0].char_end == 1500 + 2  # right after the "\n\n"


def test_anchor_position_inside_a_chunk() -> None:
    chunks = [
        ChunkInfo("alpha", 0, 5),
        ChunkInfo("beta", 5, 9),
        ChunkInfo("gamma", 9, 14),
    ]
    assert anchor_chunk_for_position(0, chunks) == 0
    assert anchor_chunk_for_position(6, chunks) == 1
    assert anchor_chunk_for_position(13, chunks) == 2


def test_anchor_falls_back_to_nearest_in_gap() -> None:
    """Position lands in an inter-chunk gap → nearest chunk wins."""
    chunks = [
        ChunkInfo("a", 0, 5),
        ChunkInfo("b", 10, 15),
    ]
    # position=7 is closer to chunk 0's end (distance 2) than chunk 1's start (3)
    assert anchor_chunk_for_position(7, chunks) == 0
    # position=8 is closer to chunk 1's start (distance 2) than chunk 0's end (3)
    assert anchor_chunk_for_position(8, chunks) == 1
    # position=9 is closer to chunk 1 (distance 1) than chunk 0's end (4).
    assert anchor_chunk_for_position(9, chunks) == 1


def test_anchor_empty_returns_none() -> None:
    assert anchor_chunk_for_position(0, []) is None
