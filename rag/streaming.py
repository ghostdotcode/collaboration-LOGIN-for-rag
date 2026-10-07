"""Incremental parser for the model's <think> ... </think> channel."""

from __future__ import annotations

from typing import List, Tuple

OPEN, CLOSE = "<think>", "</think>"


class ThinkSplitter:
    """
    Split a streamed completion into ("thinking", text) and ("answer", text).

    The previous implementation waited for the *entire* completion and then ran
    a regex, so the UI saw nothing for the whole generation and then a
    simulated typewriter. Tokens now flow as they arrive.

    Tags can be split across network chunks ("<thi" + "nk>"), so up to
    len(tag)-1 trailing characters that could still become a tag are held back
    until the next feed. Handles: no think block, multiple blocks, an unclosed
    block (flushed as thinking), and stray close tags.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False
        self.saw_think = False

    @staticmethod
    def _partial_tag_len(buf: str, tag: str) -> int:
        """Length of the longest suffix of buf that is a proper prefix of tag."""
        for size in range(min(len(tag) - 1, len(buf)), 0, -1):
            if tag.startswith(buf[-size:]):
                return size
        return 0

    def feed(self, chunk: str) -> List[Tuple[str, str]]:
        self._buf += chunk
        out: List[Tuple[str, str]] = []
        while True:
            if self._in_think:
                idx = self._buf.find(CLOSE)
                if idx != -1:
                    if idx:
                        out.append(("thinking", self._buf[:idx]))
                    self._buf = self._buf[idx + len(CLOSE):]
                    self._in_think = False
                    continue
                hold = self._partial_tag_len(self._buf, CLOSE)
            else:
                # Not inside a think block: an opening tag starts one. A *stray*
                # closing tag (some Qwen templates pre-open the block, so only
                # "</think>" ever appears) is dropped rather than shown to users.
                o, c = self._buf.find(OPEN), self._buf.find(CLOSE)
                if o != -1 and (c == -1 or o < c):
                    if o:
                        out.append(("answer", self._buf[:o]))
                    self._buf = self._buf[o + len(OPEN):]
                    self._in_think = True
                    self.saw_think = True
                    continue
                if c != -1:
                    if c:
                        out.append(("answer", self._buf[:c]))
                    self._buf = self._buf[c + len(CLOSE):]
                    continue
                hold = max(self._partial_tag_len(self._buf, OPEN), self._partial_tag_len(self._buf, CLOSE))
            emit = self._buf[: len(self._buf) - hold] if hold else self._buf
            if emit:
                out.append(("thinking" if self._in_think else "answer", emit))
            self._buf = self._buf[len(emit):]
            return out

    def flush(self) -> List[Tuple[str, str]]:
        rest, self._buf = self._buf, ""
        if not rest:
            return []
        return [("thinking" if self._in_think else "answer", rest)]
