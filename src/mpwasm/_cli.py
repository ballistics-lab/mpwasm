"""Command line in the style of the unix `micropython` binary.

    mpwasm [<opts>] [-X <implopt>] [-c <command> | -m <module> | <filename>] [args ...]

With no command, module or file it starts the REPL when stdin is a terminal (MicroPython's own REPL:
its line editing, history, auto-indent and continuation lines), and otherwise runs stdin as a script.
Exit status follows `micropython`: 0, `sys.exit(n)` -> n, an uncaught exception -> 1.

Limits of running MicroPython as WebAssembly: its filesystem is an in-memory one, so a script cannot
import sibling files from disk (only what is in the frozen/built-in modules), and output is delivered
when the script finishes rather than while it runs. The REPL is interactive as usual.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import logging
import os
import re
import stat
import sys
import sysconfig
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, NoReturn

from . import MicroPython, MicroPythonError, bundled_version, npm_versions
from ._hosts import HOSTS
from ._input import InputEditor, repl_is_affected
from ._serve import cooked, serve_pty, serve_tcp

try:
    from ._version import version as __version__
except ImportError:  # running from a source tree that has not been built
    __version__ = "0+unknown"

PROG: Final = "mpwasm"
DEFAULT_HEAP: Final = 2 * 1024 * 1024  # `micropython`'s default

# ── help texts (constants, as in rp2040py's CLI) ─────────────────────────────────────────────────────
_USAGE: Final = "%(prog)s [<opts>] [-X <implopt>] [-c <command> | -m <module> | <filename>] [args ...]"
_DESCRIPTION: Final = (
    "MicroPython (its WebAssembly build) on the command line, in the style of the unix `micropython` binary. "
    "With no command, module or file it starts a REPL (a script on stdin when stdin is piped)."
)
_INSPECT_HELP: Final = "enable inspection via REPL after running the command/module/file"
_VERBOSE_HELP: Final = "verbose: show the JavaScript host (twice: also its log); can be repeated"
_OPT_HELP: Final = "apply bytecode optimizations of level N (-O = -O1, -OO = -O2, -O0..-O3)"
_VERSION_HELP: Final = "show version information and exit"
_LIST_HELP: Final = "list the MicroPython releases available on npm and exit"
_NPM_HELP: Final = (
    "use this MicroPython release from npm instead of the bundled one: a version, a prefix (1.28 is the "
    "newest 1.28.x) or a tag (latest); downloaded once and cached (default: the bundled build)"
)
_MJS_HELP: Final = "use this local loader (.mjs) instead of the build's own"
_WASM_HELP: Final = "use this local .wasm instead of the build's own"
_PTY_HELP: Final = "serve the REPL on a pseudo-terminal (POSIX) and print its path for `mpremote connect <path>`"
_TCP_PORT_HELP: Final = "serve the REPL on this TCP port (0 = any free one) for `mpremote connect socket://host:port`"
_TCP_HOST_HELP: Final = "address to listen on with --tcp-port (default: %(default)s)"
_COMMAND_HELP: Final = "run this command, then exit; everything after it becomes sys.argv[1:]"
_MODULE_HELP: Final = (
    "run this module from the current directory (or $MICROPYPATH) as __main__; the rest is sys.argv[1:]"
)
_REST_HELP: Final = "a script file to run, then exit; everything after it becomes sys.argv[1:]"
_IMPL_HELP: Final = "implementation specific option, see below; can be repeated"
_EPILOG: Final = (
    "Implementation specific options (-X):\n"
    "  compile-only            parse and compile only\n"
    "  heapsize=<n>[w][K|M]    set the heap size for the GC (default {heap})\n"
    "  host={hosts}\n"
    "                          JavaScript host to run on (default: the first that starts)\n"
    "  variant=<name>          the build's variant, e.g. ulab (numpy-like arrays)\n"
    "\n"
    "Like micropython, options are read up to the first command/module/file; whatever follows belongs to the\n"
    "script (so `mpwasm script.py --npm x` passes --npm to the script)."
)


class UsageError(Exception):
    """A bad command line: reported as `prog: message` with status 2 (`show_usage`: preceded by the usage line)."""

    def __init__(self, message: str, *, show_usage: bool = False) -> None:
        super().__init__(message)
        self.show_usage = show_usage


class _Parser(argparse.ArgumentParser):
    """argparse that raises instead of exiting, so `main()` can return the status (and be called from an IDE)."""

    def error(self, message: str) -> NoReturn:
        raise UsageError(message, show_usage=True)


@dataclass
class Options:
    command: str | None = None
    module: str | None = None
    filename: str | None = None
    argv: list[str] = field(default_factory=list[str])  # becomes sys.argv
    inspect: bool = False
    verbose: int = 0
    opt_level: int | None = None
    heapsize: int = DEFAULT_HEAP
    compile_only: bool = False
    host: str | None = None
    variant: str = ""
    npm: str | None = None
    mjs: str | None = None
    wasm: str | None = None
    pty: bool = False
    tcp_port: int | None = None
    tcp_host: str = "127.0.0.1"
    list_versions: bool = False
    show_help: bool = False
    show_version: bool = False


def parse_heapsize(text: str) -> int:
    """`<n>[w][K|M]`: n bytes, or n words (4 bytes on wasm32) with `w`, times 1024 / 1024**2."""
    m = re.fullmatch(r"(\d+)(w?)([KM]?)", text)
    if not m:
        raise UsageError(f"invalid heapsize {text!r}, expected <n>[w][K|M]")
    n = int(m.group(1)) * (4 if m.group(2) else 1)
    return n * {"": 1, "K": 1024, "M": 1024 * 1024}[m.group(3)]


def _impl_option(opts: Options, text: str) -> None:
    key, _, value = text.partition("=")
    if key == "compile-only" and not value:
        opts.compile_only = True
    elif key == "heapsize" and value:
        opts.heapsize = parse_heapsize(value)
    elif key == "host" and value in HOSTS:
        opts.host = value
    elif key == "variant" and re.fullmatch(r"[a-z0-9]*(-[a-z0-9]+)*", value):
        opts.variant = value
    elif key == "emit":
        raise UsageError("-X emit is not supported: the WebAssembly build has no native/viper emitter switch")
    else:
        raise UsageError(f"unknown implementation option: -X {text}")


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a port number, got {text!r}") from None
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port out of range: {port}")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=PROG,
        usage=_USAGE,
        description=_DESCRIPTION,
        epilog=_EPILOG.format(heap=DEFAULT_HEAP, hosts="{" + "|".join(HOSTS) + "}"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,  # `-h` is handled by main(), so it returns a status instead of exiting
        allow_abbrev=False,  # `--np` must not silently mean `--npm`
    )
    parser.add_argument("-h", "--help", action="store_true", help="show this help message and exit")
    parser.add_argument("--version", action="store_true", help=_VERSION_HELP)
    parser.add_argument("-i", dest="inspect", action="store_true", help=_INSPECT_HELP)
    parser.add_argument("-v", dest="verbose", action="count", default=0, help=_VERBOSE_HELP)
    parser.add_argument("-O", dest="opt_level", action="count", default=None, help=_OPT_HELP)
    for level in range(4):  # `-O2` (digits attached) is its own option: -O takes no argument
        parser.add_argument(f"-O{level}", dest="opt_level", action="store_const", const=level, help=argparse.SUPPRESS)
    parser.add_argument("-X", dest="impl", action="append", metavar="<implopt>", help=_IMPL_HELP)

    which = parser.add_argument_group("which MicroPython")
    which.add_argument("--npm", metavar="<version|tag>", help=_NPM_HELP)
    which.add_argument("--mjs", metavar="<path>", help=_MJS_HELP)
    which.add_argument("--wasm", metavar="<path>", help=_WASM_HELP)
    which.add_argument("--list-versions", action="store_true", help=_LIST_HELP)

    serve = parser.add_argument_group("serve the REPL to mpremote and other serial clients")
    transport = serve.add_mutually_exclusive_group()
    transport.add_argument("--pty", action="store_true", help=_PTY_HELP)
    transport.add_argument("--tcp-port", type=_port, metavar="<port>", default=None, help=_TCP_PORT_HELP)
    serve.add_argument("--tcp-host", default="127.0.0.1", metavar="<host>", help=_TCP_HOST_HELP)

    source = parser.add_argument_group("what to run")
    what = source.add_mutually_exclusive_group()
    # REMAINDER: -c/-m take the rest of the line, so a script's own options are never read as ours.
    what.add_argument("-c", dest="command", nargs=argparse.REMAINDER, metavar="<command>", help=_COMMAND_HELP)
    what.add_argument("-m", dest="module", nargs=argparse.REMAINDER, metavar="<module>", help=_MODULE_HELP)
    parser.add_argument("rest", nargs=argparse.REMAINDER, metavar="<filename> [args ...]", help=_REST_HELP)
    return parser


def parse_args(args: Sequence[str]) -> Options:
    """Options up to the first command/module/file (see the epilog of --help); the rest is the script's own argv."""
    ns = build_parser().parse_args(list(args))
    opts = Options(
        inspect=ns.inspect,
        verbose=ns.verbose,
        opt_level=ns.opt_level,
        npm=ns.npm,
        mjs=ns.mjs,
        wasm=ns.wasm,
        pty=ns.pty,
        tcp_port=ns.tcp_port,
        tcp_host=ns.tcp_host,
        list_versions=ns.list_versions,
        show_help=ns.help,
        show_version=ns.version,
    )
    impl: list[str] = ns.impl or []
    for text in impl:
        _impl_option(opts, text)
    if ns.command is not None:
        if not ns.command:
            raise UsageError("argument -c: expected an argument", show_usage=True)
        opts.command, opts.argv = ns.command[0], ["-c", *ns.command[1:]]
    elif ns.module is not None:
        if not ns.module:
            raise UsageError("argument -m: expected an argument", show_usage=True)
        opts.module, opts.argv = ns.module[0], list(ns.module)
    elif ns.rest:
        opts.filename, opts.argv = ns.rest[0], list(ns.rest)
    return opts


def find_module(name: str) -> str:
    """Path of module `name` (pkg.mod -> pkg/mod.py or pkg/mod/__main__.py) in the cwd or $MICROPYPATH."""
    rel = name.replace(".", os.sep)
    dirs = [os.getcwd(), *[d for d in os.environ.get("MICROPYPATH", "").split(os.pathsep) if d]]
    for d in dirs:
        for cand in (os.path.join(d, rel + ".py"), os.path.join(d, rel, "__main__.py")):
            if os.path.isfile(cand):
                return cand
    raise UsageError(f"no module named '{name}'")


# ── output and errors ─────────────────────────────────────────────────────────────────────────────────


def _write(stream: object, text: str) -> None:
    if text:
        write = getattr(stream, "write")  # noqa: B009 -- TextIO, typed loosely to accept sys.stdout replacements
        write(text)
        getattr(stream, "flush")()  # noqa: B009


def flush_streams(mp: MicroPython, *, raw_tty: bool = False) -> str:
    """Forward what MicroPython wrote to stdout and stderr.

    On a raw terminal (output post-processing off) a bare `\\n` would only move down, not back to column 0,
    so `raw_tty` turns them into `\\r\\n` as a board's serial console does.
    """
    if raw_tty:
        text = cooked(mp.output() + mp.error_output()).decode("utf-8", "replace")
        _write(sys.stdout, text)
        return text
    out, err = mp.output(), mp.error_output()
    _write(sys.stdout, out)
    _write(sys.stderr, err)
    return out + err


_EXC_PREFIX = "[JS] PythonError: "


def _traceback_text(exc: MicroPythonError) -> str:
    text = str(exc)
    return text.removeprefix(_EXC_PREFIX)


def exit_status(exc: MicroPythonError) -> int | None:
    """The status for a `SystemExit` (sys.exit), or None when the exception is something else."""
    last = [ln for ln in _traceback_text(exc).splitlines() if ln.strip()][-1:]
    m = re.fullmatch(r"SystemExit(?:: (.*))?", last[0]) if last else None
    if m is None:
        return None
    arg = m.group(1)
    if arg is None or arg in ("", "None"):
        return 0
    if re.fullmatch(r"-?\d+", arg):
        return int(arg)
    _write(sys.stderr, arg + "\n")  # sys.exit("message") prints it and fails
    return 1


def report(mp: MicroPython, exc: MicroPythonError, *, wrapped: bool = False) -> int:
    """Show what was printed before the error, then the traceback (or handle sys.exit); return the status."""
    _write(sys.stdout, exc.output)
    _write(sys.stderr, mp.error_output())
    status = exit_status(exc)
    if status is not None:
        return status
    lines = _traceback_text(exc).splitlines()
    if wrapped and len(lines) > 2 and lines[1].startswith('  File "<stdin>", line 1, in <module>'):
        del lines[1]  # the exec(compile(...)) line that wraps a file or -c, not part of the user's code
    _write(sys.stderr, "\n".join(lines) + "\n")
    return 1


# ── running code ──────────────────────────────────────────────────────────────────────────────────────


def run_source(mp: MicroPython, source: str, filename: str, argv: Sequence[str], compile_only: bool) -> int:
    """Run `source` as __main__ (compile-only just compiles it), reporting like `micropython` does."""
    try:
        # sys is read-only in this build (`sys.argv = ...` fails), but argv itself is a list: fill it in place.
        mp.run(f"import sys\nsys.argv[:] = {list(argv)!r}")
        compiled = f"compile({source!r}, {filename!r}, 'exec')"
        code = compiled if compile_only else f"exec({compiled})"
        out = mp.run(code)
    except MicroPythonError as exc:
        return report(mp, exc, wrapped=True)
    _write(sys.stdout, out)
    _write(sys.stderr, mp.error_output())
    return 0


def _on_ios() -> bool:
    """iOS Python apps (Pythonista, PythonIDE): consoles that look like a pipe or an odd file, but are interactive."""
    return (
        sys.platform == "ios"
        or "ios" in sysconfig.get_platform()
        or importlib.util.find_spec("objc_util") is not None  # Pythonista's own module
    )


def _stdin_mode() -> int | None:
    """st_mode of the file behind stdin, or None when stdin has no real file descriptor."""
    try:
        return os.fstat(sys.stdin.fileno()).st_mode
    except (AttributeError, OSError, ValueError):  # io.UnsupportedOperation is an OSError and a ValueError
        return None


def stdin_is_a_script() -> bool:
    """True when stdin is known to be piped-in input to run as a script (`micropython < script.py`).

    Only when it is really a pipe, a socket or a file -- never on iOS, and never when stdin has no file
    descriptor of its own, which is what a Pythonista/Windows-style console looks like. There a missing
    script means a REPL, the rule rp2040py's stdio bridge follows too: interactive unless known otherwise.
    """
    if _on_ios():
        return False
    mode = _stdin_mode()
    return mode is not None and (stat.S_ISFIFO(mode) or stat.S_ISREG(mode) or stat.S_ISSOCK(mode))


def _raw_terminal_available() -> bool:
    """A real tty on a platform with termios (not Windows, not iOS): MicroPython's own REPL can drive it."""
    return not _on_ios() and importlib.util.find_spec("termios") is not None and sys.stdin.isatty()


def _needs_more(source: str) -> bool:
    """Line-REPL heuristic: a block opener or an unclosed bracket wants continuation lines."""
    return source.rstrip().endswith(":") or any(source.count(o) > source.count(c) for o, c in ("()", "[]", "{}"))


_EXIT_WORDS = frozenset({"exit", "exit()", "quit", "quit()"})


def line_repl(mp: MicroPython) -> int:
    """A plain `input()` REPL, for consoles without a raw terminal: Pythonista, Windows, piped input.

    Type `exit()` (or `sys.exit(n)`, or send end-of-input) to leave: iOS keyboards have no Ctrl-D.
    """
    show_prompts = not stdin_is_a_script()  # a pipe gets no prompts and no banner, like `micropython < file`
    prompt1, prompt2 = (">>> ", "... ") if show_prompts else ("", "")
    if show_prompts:
        banner = mp.run("import sys\nprint(sys.version)").strip().split("; ", 1)[-1]
        print(banner)
        print('Type "help()" for more information.')
    while True:
        try:
            line = input(prompt1)
            if line.strip() in _EXIT_WORDS:
                return 0
            if _needs_more(line):
                lines = [line]
                while (more := input(prompt2)) != "":
                    lines.append(more)
                line = "\n".join(lines) + "\n"
        except EOFError:
            if show_prompts:
                print()
            return 0
        except KeyboardInterrupt:
            print("\nKeyboardInterrupt")
            continue
        try:
            out = mp.repl(line)
        except MicroPythonError as exc:
            status = exit_status(exc)  # sys.exit() ends the session, like on the unix binary
            if status is not None:
                return status
            report(mp, exc)
            continue
        _write(sys.stdout, out)
        _write(sys.stderr, mp.error_output())


def tty_repl(mp: MicroPython) -> int:
    """MicroPython's own REPL on a raw terminal: keystrokes go in, its echo and prompts come out."""
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    editor = InputEditor()  # works around the empty-line bug of MicroPython 1.29.0-6's REPL, where present

    def flush() -> None:
        editor.note_output(flush_streams(mp, raw_tty=True))

    mp.repl_init()
    try:
        tty.setraw(fd)
        flush()  # the banner and first prompt
        editor.compensate = repl_is_affected(mp)
        editor.note_output(mp.output())  # (the probe's own redrawn prompt, if any)
        while True:
            if not select.select([fd], [], [], 0.02)[0]:
                mp.pump()  # let asyncio tasks run while the user is idle
                flush()
                continue
            data = os.read(fd, 4096)
            if not data:
                return 0
            keys = editor.feed(data, eof_on_ctrl_d=True)  # Ctrl-D at an empty prompt leaves, like the unix binary
            if keys:
                mp.repl_feed(keys)
            flush()
            if editor.eof:
                _write(sys.stdout, "\r\n")
                return 0
    finally:
        # TCSANOW, not TCSADRAIN: DRAIN waits until the terminal has taken all queued output, so a terminal
        # that has stopped reading (or a pty nobody reads, as on macOS) would keep us from ever exiting.
        termios.tcsetattr(fd, termios.TCSANOW, saved)


def repl(mp: MicroPython) -> int:
    return tty_repl(mp) if _raw_terminal_available() else line_repl(mp)


# ── main ──────────────────────────────────────────────────────────────────────────────────────────────


def _read_source(opts: Options) -> tuple[str, str] | None:
    """(source, filename) for -c / -m / <filename>, or None when there is nothing to run."""
    if opts.command is not None:
        return opts.command, "<string>"
    if opts.module is not None:
        path = find_module(opts.module)
        opts.argv[0] = path
        with open(path, encoding="utf-8") as f:
            return f.read(), path
    if opts.filename is not None:
        try:
            with open(opts.filename, encoding="utf-8") as f:
                return f.read(), opts.filename
        except OSError as exc:
            raise UsageError(f"can't open file '{opts.filename}': {exc.strerror}") from None
    return None


def main(argv: Sequence[str] | None = None) -> None:
    """The console entry point (`mpwasm`, `python -m mpwasm`): runs the command line and exits with its status,
    as rp2040py's `main()` does. Use `run()` to get the status back instead."""
    sys.exit(run(argv))


def run(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return its exit status (0, `sys.exit(n)` -> n, an error -> 1 or 2)."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return _main(args)
    except UsageError as exc:
        if exc.show_usage:
            _write(sys.stderr, build_parser().format_usage())
        _write(sys.stderr, f"{PROG}: {'error: ' if exc.show_usage else ''}{exc}\n")
        return 2
    except (MicroPythonError, RuntimeError, ValueError, OSError) as exc:  # a build that can't load, no network, ...
        _write(sys.stderr, f"{PROG}: {exc}\n")
        return 1


def _main(args: list[str]) -> int:
    opts = parse_args(args)
    if opts.show_help:
        _write(sys.stdout, build_parser().format_help())
        return 0
    if opts.verbose:
        logging.basicConfig(level=logging.INFO, format=f"{PROG}: %(message)s")
    if opts.list_versions:
        bundled = bundled_version()
        for version in npm_versions():
            _write(sys.stdout, f"{version}{'  (bundled)' if version == bundled else ''}\n")
        return 0
    serving = opts.pty or opts.tcp_port is not None
    if opts.pty and opts.tcp_port is not None:
        raise UsageError("--pty and --tcp-port are mutually exclusive")
    if serving and (opts.command is not None or opts.module is not None or opts.filename is not None or opts.inspect):
        raise UsageError("--pty/--tcp-port serve the REPL for the interpreter's whole lifetime: no -c/-m/file/-i")
    if opts.pty and importlib.util.find_spec("pty") is None:
        raise UsageError("--pty needs a POSIX pseudo-terminal; use --tcp-port here")

    def new_interpreter() -> MicroPython:
        return MicroPython(
            opts.mjs, opts.wasm, npm=opts.npm, variant=opts.variant, heapsize=opts.heapsize, host=opts.host
        )

    if serving:  # the console owns its interpreter: it replaces it on every soft reset
        return serve_pty(new_interpreter) if opts.pty else serve_tcp(new_interpreter, opts.tcp_host, opts.tcp_port or 0)
    with contextlib.ExitStack() as stack:
        mp = stack.enter_context(new_interpreter())
        if opts.show_version:
            banner = mp.run("import sys\nprint(sys.version)").strip().split("; ", 1)[-1]
            _write(sys.stdout, f"{banner} (mpwasm {__version__}, {mp.host})\n")
            return 0
        if opts.verbose:
            _write(sys.stderr, f"{PROG}: host={mp.host} heapsize={opts.heapsize}\n")
        if opts.opt_level is not None:
            mp.run(f"import micropython\nmicropython.opt_level({opts.opt_level})")

        status = 0
        source = _read_source(opts)
        if source is None and not opts.inspect and stdin_is_a_script():
            source = sys.stdin.read(), "<stdin>"  # like `micropython < script.py`
            opts.argv = [""]
        if source is not None:
            status = run_source(mp, source[0], source[1], opts.argv, opts.compile_only)
            if opts.compile_only:
                return status
        if opts.verbose > 1:
            _write(sys.stderr, mp.js_log() + "\n")
        if source is None or opts.inspect:
            status = repl(mp)
        return status


if __name__ == "__main__":
    main()
