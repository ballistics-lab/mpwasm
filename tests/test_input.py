"""InputEditor: what a terminal sends, cleaned up for MicroPython's REPL, and the history the build lacks."""

import pytest

from mpwasm import MicroPython
from mpwasm._input import InputEditor, repl_is_affected
from mpwasm._serve import Console

PROMPT = ">>> "


def editor(prompt: str = PROMPT) -> InputEditor:
    e = InputEditor()
    e.note_output(prompt)
    return e


def typed(e: InputEditor, *lines: bytes) -> None:
    """Type lines and press Enter, with the main prompt showing each time and the echo in between."""
    for line in lines:
        e.note_output(PROMPT)
        e.feed(line)
        e.note_output(line.decode())  # what the REPL echoes as it is typed: must not make the prompt "go away"
        e.feed(b"\r")
        e.note_output("\r\n" + PROMPT)


# ── Enter ───────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("enter", [b"\r", b"\n", b"\r\n"])
def test_any_newline_is_one_enter(enter: bytes) -> None:
    assert editor().feed(b"x = 1" + enter) == b"x = 1\r"


def test_pasted_lines_become_one_enter_each() -> None:
    assert editor().feed(b"a\r\nb\r\nc\n") == b"a\rb\rc\r"


def test_crlf_split_across_reads() -> None:
    e = editor()
    assert e.feed(b"a\r") == b"a\r"
    assert e.feed(b"\nb") == b"b"  # the LF of the CRLF that was cut in two


def test_two_separate_enters_stay_two() -> None:
    assert editor().feed(b"\r\r") == b"\r\r"


# ── terminal noise ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "seq", [b"\x1b[I", b"\x1b[O", b"\x1b[200~", b"\x1b[201~", b"\x1b[24;80R", b"\x1bx", b"\x1b[1;5C"]
)
def test_noise_is_dropped(seq: bytes) -> None:
    assert editor().feed(b"a" + seq + b"b") == b"ab"


def test_bracketed_paste_markers_are_removed_around_text() -> None:
    assert editor().feed(b"\x1b[200~import os\x1b[201~\r") == b"import os\r"


def test_escape_sequence_split_across_reads() -> None:
    e = editor()
    e.feed(b"abc")
    assert e.feed(b"\x1b") == b"" and e.feed(b"[") == b""
    assert e.feed(b"D") == b"\x1b[D"  # a Left arrow, reassembled


def test_ss3_cursor_keys_become_csi() -> None:
    e = editor()
    e.feed(b"abc")
    assert e.feed(b"\x1bOD\x1bOH\x1bOF") == b"\x1b[D\x1b[H\x1b[F"


def test_cursor_keys_on_an_empty_line_are_dropped() -> None:
    # this build's REPL prints an escape sequence as text when the line is empty
    assert editor().feed(b"\x1b[D\x1b[C\x1b[H\x1b[F\x1b[3~\x1b[A\x1b[B") == b""


@pytest.mark.parametrize("key", [b"\x7f", b"\x08", b"\x15", b"\x0b", b"\x17", b"\x09", b"\x0c", b"\x00", b"\x1a"])
def test_editing_keys_on_an_empty_prompt_are_dropped(key: bytes) -> None:
    # each one makes the REPL redraw ">>> " (the ">>> >>> >>>" of holding Backspace)
    e = editor()
    assert e.feed(key) == b""
    assert e.feed(b"x") == b"x"


def test_backspace_on_an_empty_prompt_prints_nothing_but_still_works_after_typing() -> None:
    e = editor()
    assert e.feed(b"\x7f\x7f\x7f") == b""
    assert e.feed(b"ab\x7f") == b"ab\x7f"  # with text on the line it goes through


@pytest.mark.parametrize("key", [b"\x03", b"\x02", b"\x04", b"\r"])
def test_keys_with_a_meaning_at_an_empty_prompt_pass(key: bytes) -> None:
    out = editor().feed(key)
    assert out == (b"\r" if key == b"\r" else key)


def test_backspace_on_a_continuation_line_passes_it_removes_an_indent_level() -> None:
    assert editor("... ").feed(b"\x7f") == b"\x7f"


# ── raw REPL: hands off ─────────────────────────────────────────────────────────────────────────────


def test_raw_repl_passes_bytes_through_exactly() -> None:
    e = editor()
    script = b"x = 1\nprint(x)\r\n\x1b[A\x1b[200~"
    assert e.feed(b"\x01" + script + b"\x04") == b"\x01" + script + b"\x04"
    assert e.raw
    assert e.feed(b"\x02\n") == b"\x02\r"  # Ctrl-B leaves raw REPL, and normal handling resumes
    assert not e.raw


def test_ctrl_a_mid_line_is_home_not_raw_repl() -> None:
    e = editor()
    e.feed(b"abc")
    e.feed(b"\x01")
    assert not e.raw


# ── history ─────────────────────────────────────────────────────────────────────────────────────────


UP, DOWN = b"\x1b[A", b"\x1b[B"


def test_up_recalls_and_down_returns_to_the_draft() -> None:
    e = editor()
    typed(e, b"first", b"second")
    e.feed(b"dra")
    assert e.feed(UP) == b"\x1b[F" + b"\x7f" * 3 + b"second"
    assert e.feed(UP) == b"\x1b[F" + b"\x7f" * 6 + b"first"
    assert e.feed(UP) == b"\x1b[F" + b"\x7f" * 5 + b"first"  # already at the oldest
    assert e.feed(DOWN) == b"\x1b[F" + b"\x7f" * 5 + b"second"
    assert e.feed(DOWN) == b"\x1b[F" + b"\x7f" * 6 + b"dra"  # back to what was being typed


def test_up_on_an_empty_line_just_types_the_entry() -> None:
    e = editor()
    typed(e, b"print(1)")
    assert e.feed(UP) == b"print(1)"  # no End / backspaces: nothing to erase


def test_no_history_no_output() -> None:
    assert editor().feed(UP + DOWN) == b""


def test_consecutive_duplicates_are_stored_once() -> None:
    e = editor()
    typed(e, b"a", b"a", b"b", b"a")
    assert e.history == [b"a", b"b", b"a"]


def test_only_main_prompt_lines_are_recorded_and_recalled() -> None:
    e = editor("... ")
    e.feed(b"    body\r")
    assert e.history == []
    typed(e, b"x = 1")
    e.note_output("\r\n...     ")  # a continuation prompt, with the auto-indent the REPL typed
    assert e.feed(UP) == b""  # at a continuation prompt Up does nothing


def test_prompt_survives_the_echo_of_typing() -> None:
    e = editor()
    e.feed(b"abc")
    e.note_output("abc")  # the echo: the last thing printed is no longer ">>> "
    assert e.at_main_prompt
    e.feed(b"\r")
    assert e.history == [b"abc"]


def test_editing_a_line_before_recalling_uses_the_tracked_length() -> None:
    e = editor()
    typed(e, b"hist")
    e.feed(b"abcd\x1b[D\x1b[D\x7fX")  # abcd, cursor left twice, backspace deletes b, X typed: "aXcd"
    assert e.feed(UP) == b"\x1b[F" + b"\x7f" * 4 + b"hist"


def test_tab_completion_turns_history_off_for_that_line() -> None:
    e = editor()
    typed(e, b"old")
    e.feed(b"pri\t")  # completion result is only seen by the REPL
    assert e.feed(UP) == b""
    e.feed(b"\r")
    e.note_output(PROMPT)
    assert e.feed(UP) == b"old"  # a fresh line has history again


def test_history_is_bounded() -> None:
    e = InputEditor(history_size=3)
    e.note_output(PROMPT)
    typed(e, *[str(n).encode() for n in range(6)])
    assert e.history == [b"3", b"4", b"5"]


def test_reset_keeps_history_and_clears_the_line() -> None:
    e = editor()
    typed(e, b"keep")
    e.feed(b"half")
    e.reset()
    assert e.history == [b"keep"] and e.line_empty and not e.raw


# ── Ctrl-D and Ctrl-C ───────────────────────────────────────────────────────────────────────────────


def test_eof_only_at_an_empty_line() -> None:
    e = editor()
    e.feed(b"x\x04", eof_on_ctrl_d=True)
    assert not e.eof  # Ctrl-D with text typed is not "exit"
    e.feed(b"\x03")
    assert e.line_empty
    assert e.feed(b"\x04rest", eof_on_ctrl_d=True) == b"" and e.eof


# ── through the console (serial clients), on a real interpreter ─────────────────────────────────────


def test_console_history_and_paste_end_to_end() -> None:
    console = Console(MicroPython)
    try:
        assert b"42" in console.feed(b"print(6*7)\r\n")  # a CRLF paste: one execution, one prompt
        out = console.feed(b"\x1b[A\r")  # Up, Enter: runs it again
        assert b"42" in out and out.count(b">>> ") == 1
        assert b"[A" not in out
    finally:
        console.close()


# ── not compensating: a healthy build's own REPL does the work ──────────────────────────────────────


def native() -> InputEditor:
    e = InputEditor(compensate=False)
    e.note_output(PROMPT)
    return e


@pytest.mark.parametrize(
    "keys",
    [b"a\r\nb\n", b"\x7f\x15\t", b"\x1b[A\x1b[B", b"\x1b[200~x\x1b[201~", b"\x1b[D\x1bOH", b"\x1b[I"],
)
def test_without_compensation_input_passes_through_untouched(keys: bytes) -> None:
    assert native().feed(keys) == keys


def test_without_compensation_history_keys_still_stop_line_tracking() -> None:
    e = native()
    e.feed(b"ab\x1b[A")  # the REPL recalls something: what is on the line is now unknown to us
    assert not e.line_empty
    e.feed(b"\x03")
    assert e.line_empty


def test_without_compensation_raw_repl_and_eof_are_still_tracked() -> None:
    e = native()
    e.feed(b"\x01")
    assert e.raw
    e.feed(b"\x02")
    assert not e.raw
    e.feed(b"\x04", eof_on_ctrl_d=True)
    assert e.eof


# ── detecting whether the build needs the workaround ────────────────────────────────────────────────


def test_repl_is_affected_matches_the_builds_real_behaviour() -> None:
    mp = MicroPython()
    try:
        mp.repl_init()
        mp.output()
        affected = repl_is_affected(mp)
        mp.output()
        mp.repl_feed(b"\x7f\x7f\x7f")  # what the check is about: Backspace on an empty prompt
        assert (">>> " in mp.output()) == affected
    finally:
        mp.close()


def test_console_hides_the_bug_either_way() -> None:
    console = Console(MicroPython)
    try:
        assert console.feed(b"\x7f\x7f\x7f") == b""  # no `>>> >>> >>>`, affected build or not
        assert console.feed(b"x = 1\r\ny = 2\r\n").count(b">>> ") == 2  # a CRLF paste: one prompt per line
        out = console.feed(b"\x1b[A\r")  # Up recalls the last line
        assert out.count(b">>> ") == 1 and b"[A" not in out
    finally:
        console.close()
