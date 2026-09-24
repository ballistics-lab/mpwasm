"""Keyboard input for MicroPython's REPL: what a terminal sends, cleaned up, plus the history the build lacks.

MicroPython 1.29.0-6's WebAssembly REPL has a bug (shared/runtime/pyexec.c, commit 2e3304a12): while the line is
empty, *any* character -- Backspace, Tab, ESC, the LF of a CRLF -- restarts the REPL, which prints a new prompt and
forgets the escape sequence and the history state. So Backspace gives `>>> >>> >>>`, arrow keys print `[A`, Up
does not recall, and a pasted CRLF is two Enters. Every build from 1.22.0-335 to 1.28.0-6 is fine.

`InputEditor` sits between the terminal (or a serial client) and `MicroPython.repl_feed` and works around it -- but
only when asked to (`compensate=True`, which `repl_is_affected()` decides by trying the REPL): on a healthy build the
REPL's own editing and history are used untouched. What it does then:

- **Enter**: CR, LF and CRLF are all one Enter. Pasted text arrives as LF or CRLF, and a bare CRLF used to be two
  Enters (two prompts).
- **History**: Up/Down recall lines typed at the `>>> ` prompt, replacing the current line by typing over it.
  Continuation lines (`... `) are not recorded, and Up/Down do nothing there.
- **Empty line**: on an empty `>>> ` line this build's REPL redraws the prompt for every editing key (Backspace,
  Ctrl-U, Tab, cursor keys, ...) instead of ignoring it, giving `>>> >>> >>>`; those keys are dropped there. (On a
  `... ` continuation line they are passed on: Backspace removes an auto-indent level.)
- **Noise**: focus reports (`ESC[I`, `ESC[O`), bracketed-paste markers (`ESC[200~`, `ESC[201~`) and other CSI /
  SS3 sequences that the REPL does not know are dropped instead of being typed as text. Home/End/Delete/arrows in
  their SS3 forms (`ESC O H`, application-cursor mode) are turned into the CSI forms the REPL understands.
- **Raw REPL** (Ctrl-A at an empty line ... Ctrl-B) carries binary protocol, in which a LF is data: input passes
  through untouched, exactly.

Tracking of the line (and of raw REPL) always runs, even when not compensating, because the serial console needs to
know when Ctrl-D means "soft reset".

Only the line *being typed* is tracked (typed characters, cursor moves, backspace/Delete, Ctrl-U/K), enough to
replace it; after a Tab completion, whose result only the REPL sees, history is off for that line.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from . import MicroPython

__all__ = ("InputEditor", "repl_is_affected")

_ESC: Final = 0x1B
_CSI_FINAL: Final = range(0x40, 0x7F)
_CONTINUATION: Final = re.compile(r"\.\.\. *$")  # `... ` plus any auto-indent the REPL typed after it
_UP, _DOWN = b"\x1b[A", b"\x1b[B"
_END: Final = b"\x1b[F"
_BACKSPACE: Final = b"\x7f"
# What the REPL understands, keyed by what a terminal may send instead.
_KEYS: Final[dict[bytes, bytes]] = {
    b"[C": b"\x1b[C",
    b"[D": b"\x1b[D",
    b"[H": b"\x1b[H",
    b"[F": b"\x1b[F",
    b"[1~": b"\x1b[H",
    b"[7~": b"\x1b[H",
    b"[4~": b"\x1b[F",
    b"[8~": b"\x1b[F",
    b"[3~": b"\x1b[3~",
    b"OC": b"\x1b[C",
    b"OD": b"\x1b[D",
    b"OH": b"\x1b[H",
    b"OF": b"\x1b[F",
}
_UP_DOWN: Final[dict[bytes, bytes]] = {b"[A": _UP, b"[B": _DOWN, b"OA": _UP, b"OB": _DOWN}
# Control bytes that mean something at an empty prompt; every other one only makes the REPL redraw it.
_MEANINGFUL_WHEN_EMPTY: Final = frozenset({0x01, 0x02, 0x03, 0x04, 0x05, 0x0A, 0x0D})


def repl_is_affected(mp: MicroPython) -> bool:
    """Whether this build's REPL has the empty-line restart bug (see the module docstring).

    Call right after `repl_init()` and after taking its banner from `output()`: sends one Backspace, which a
    healthy REPL ignores at an empty prompt and an affected one answers with a fresh prompt.
    """
    mp.repl_feed(b"\x7f")
    return ">>> " in mp.output()


class InputEditor:
    """Cleans up terminal input for the REPL and keeps a history; see the module docstring."""

    def __init__(self, history_size: int = 200, *, compensate: bool = True) -> None:
        self.compensate = compensate
        self.history: list[bytes] = []
        self._max = history_size
        self._line = bytearray()
        self._cur = 0
        self._recall: int | None = None  # index into history while browsing
        self._draft = b""  # the line being typed before browsing started
        self._lost = False  # the REPL changed the line in a way we did not see (Tab completion)
        self._raw = False  # in raw REPL: hands off
        self._hold: bytes = b""  # an escape sequence cut off by the end of a read
        self._prev_cr: bool = False
        self._main_prompt = False  # the last prompt the REPL printed was `>>> ` (not a `... ` continuation)
        self.eof = False  # Ctrl-D at an empty line was seen (only with feed(eof_on_ctrl_d=True))

    # ── what the REPL prints ────────────────────────────────────────────────────────────────

    def note_output(self, text: str) -> None:
        """Feed the REPL's output through, so the editor knows which prompt is showing.

        A prompt is remembered until the next one: the echo of what is typed after it does not replace it.
        """
        if text.endswith(">>> "):
            self._main_prompt = True
        elif _CONTINUATION.search(text):
            self._main_prompt = False

    @property
    def at_main_prompt(self) -> bool:
        return self._main_prompt

    @property
    def line_empty(self) -> bool:
        return not self._line and not self._lost

    @property
    def _blank_main_prompt(self) -> bool:
        """An empty line at the `>>> ` prompt, where editing keys only make this build's REPL redraw it."""
        return self.at_main_prompt and not self._line and not self._lost

    @property
    def raw(self) -> bool:
        """In raw REPL (Ctrl-A ... Ctrl-B): input passes through untouched."""
        return self._raw

    @property
    def holding(self) -> bool:
        """An escape sequence is half-received: the next byte belongs to it."""
        return bool(self._hold)

    def reset(self, *, raw: bool = False) -> None:
        """Forget the line being typed (the REPL was restarted); the history stays."""
        self._reset_line()
        self._hold = b""
        self._prev_cr = False
        self._raw = raw

    # ── what the user types ─────────────────────────────────────────────────────────────────

    def feed(self, data: bytes, *, eof_on_ctrl_d: bool = False) -> bytes:
        """Return the bytes to send to the REPL for the bytes read from the terminal / client."""
        data = self._hold + data
        self._hold = b""
        out = bytearray()
        i = 0
        while i < len(data):
            b = data[i]
            if self._raw:
                out.append(b)
                if b == 0x02:  # Ctrl-B leaves raw REPL
                    self._raw = False
                    self._reset_line()
                i += 1
                continue
            if b == _ESC:
                end = self._sequence_end(data, i)
                if end is None:
                    self._hold = data[i:]  # incomplete: wait for the rest
                    break
                sent = self._escape(data[i + 1 : end])
                out += sent if self.compensate else data[i:end]
                i = end
                continue
            i += 1
            if b == 0x0A and self._prev_cr and self.compensate:
                self._prev_cr = False
                continue  # the LF of a CRLF
            self._prev_cr = b == 0x0D
            if b in (0x0D, 0x0A):
                self._enter()
                out.append(0x0D if self.compensate else b)
            elif b == 0x04 and eof_on_ctrl_d and self.line_empty:
                self.eof = True
                break
            elif b == 0x01 and self.line_empty:  # Ctrl-A at an empty line: raw REPL
                self._raw = True
                out.append(b)
            elif (
                self.compensate
                and (b < 0x20 or b == 0x7F)
                and b not in _MEANINGFUL_WHEN_EMPTY
                and self._blank_main_prompt
            ):
                continue  # Backspace, Ctrl-U, Tab, ... on an empty line: the REPL would only redraw the prompt
            else:
                out.append(b)
                self._typed(b)
        return bytes(out)

    @staticmethod
    def _sequence_end(data: bytes, i: int) -> int | None:
        """Index just past the escape sequence starting at data[i], or None if it is not complete yet."""
        if i + 1 >= len(data):
            return None
        kind = data[i + 1]
        if kind == 0x5B:  # CSI: ESC [ params intermediates final
            j = i + 2
            while j < len(data) and data[j] not in _CSI_FINAL:
                j += 1
            return j + 1 if j < len(data) else None
        if kind == 0x4F:  # SS3: ESC O final
            return i + 3 if i + 2 < len(data) else None
        return i + 2  # ESC + one character (Alt-key): dropped

    def _escape(self, body: bytes) -> bytes:
        """The bytes to send for an escape sequence (what a compensating editor sends; otherwise only tracked)."""
        if body in _UP_DOWN:
            if not self.compensate:
                self._lost = True  # the REPL's own history will change the line: we can no longer follow it
                return b""
            return self._history(_UP_DOWN[body] == _UP)
        key = _KEYS.get(body)
        if key is None or (self.compensate and self._blank_main_prompt):
            # Focus reports, bracketed paste, keys the REPL has no use for -- and any cursor key on an empty
            # line, where this build's REPL does not take escape sequences and would print them as text.
            return b""
        self._move(key)
        return key

    # ── tracking the line ───────────────────────────────────────────────────────────────────

    def _reset_line(self) -> None:
        self._line.clear()
        self._cur = 0
        self._recall = None
        self._lost = False

    def _enter(self) -> None:
        if self.at_main_prompt and self._line and not self._lost:
            text = bytes(self._line)
            if not self.history or self.history[-1] != text:
                self.history.append(text)
                del self.history[: -self._max]
        self._reset_line()

    def _typed(self, b: int) -> None:
        if b >= 0x20 and b != 0x7F:
            self._line.insert(self._cur, b)
            self._cur += 1
        elif b in (0x7F, 0x08):
            if self._cur:
                del self._line[self._cur - 1]
                self._cur -= 1
        elif b == 0x03:  # Ctrl-C: the line is abandoned
            self._reset_line()
        elif b == 0x15:  # Ctrl-U: kill to the start
            del self._line[: self._cur]
            self._cur = 0
        elif b == 0x0B:  # Ctrl-K: kill to the end
            del self._line[self._cur :]
        elif b == 0x01:
            self._cur = 0
        elif b == 0x05:
            self._cur = len(self._line)
        elif b == 0x09:  # Tab: completion happens where we cannot see it
            self._lost = True

    def _move(self, key: bytes) -> None:
        if key == b"\x1b[D":
            self._cur = max(0, self._cur - 1)
        elif key == b"\x1b[C":
            self._cur = min(len(self._line), self._cur + 1)
        elif key == b"\x1b[H":
            self._cur = 0
        elif key == b"\x1b[F":
            self._cur = len(self._line)
        elif key == b"\x1b[3~" and self._cur < len(self._line):
            del self._line[self._cur]

    # ── history ─────────────────────────────────────────────────────────────────────────────

    def _history(self, up: bool) -> bytes:
        if not self.at_main_prompt or self._lost or not self.history:
            return b""
        if up:
            if self._recall is None:
                self._draft = bytes(self._line)
                self._recall = len(self.history) - 1
            else:
                self._recall = max(0, self._recall - 1)
            return self._replace(self.history[self._recall])
        if self._recall is None:
            return b""
        self._recall += 1
        if self._recall >= len(self.history):
            self._recall = None
            return self._replace(self._draft)
        return self._replace(self.history[self._recall])

    def _replace(self, text: bytes) -> bytes:
        """Type over the current line: to its end, erase it all, write `text`."""
        out = (_END + _BACKSPACE * len(self._line) if self._line else b"") + text
        self._line = bytearray(text)
        self._cur = len(self._line)
        return out
