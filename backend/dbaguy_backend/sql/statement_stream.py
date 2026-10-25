"""A SQL file read as statements, a chunk at a time (spec ⑥ D-06-3): what Run SQL file sends.

The file is read in chunks of `STREAM_CHUNK` bytes; only the text up to the chunk's last line break is split (a `GO`
or `DELIMITER` line can only be told whole), statements that ended are given out and the one that has not waits for
the next chunk, which then rescans it from the start of its line. A statement that stays unfinished doubles the next
read, so a large one is rescanned only a few times; one that grows past `MAX_STATEMENT_CHARS` stops the stream. Each
statement carries the line it starts on, its number (psql meta-command lines are not numbered) and how many bytes of
the file lie before its end. Pure: the bytes come from the `read` callable it is given.

The limit on one statement, and what the cut at a line break means for it:
- A statement is measured from its first token (the comments before it do not count) to the end of its terminator, the
  same whether it ended inside a read or went on over several. Comments and blank lines between statements are dropped
  as they are read, so a file of them is not held. A read is never larger than the room left under the limit plus one
  character, so what is held never goes past it by more than that, and is scanned once more at most before the error.
- The line the text is cut in is read whole. A file of many small statements on ONE line (no line break) is therefore
  limited like one statement: the line is held until it ends, and a line longer than `MAX_STATEMENT_CHARS` stops the
  stream whatever the statements in it are. The part of that line that has been read counts whole towards the
  statement it belongs to, so a statement a few characters under the limit that ends in the middle of a line much
  longer than the rest can stop the stream where reading the file whole would not.
- A file with carriage returns only (no `\n`) is one line, and limited as one."""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .dialects import Dialect
from .lexer import INSIGNIFICANT, scan
from .splitter import SplitState, StatementSpan, iter_spans

# Bytes read at a time. Not the spec's 1 MiB (D-06-3): reading 1 MiB at a time left the process 70–90 MB larger after a
# few hundred 1,000-row INSERTs (macOS malloc keeps the freed multi-megabyte strings of text with an emoji in it, which
# Python holds 4 bytes a character), 512 KiB 15 MB, at 8% of the splitting speed (⑥ A10, measured while planning 06a)
STREAM_CHUNK = 512 * 1024
MAX_STATEMENT_CHARS = 256 * 1_048_576  # one statement, as text
FILE_NOT_UTF8 = "file_not_utf8"
STATEMENT_TOO_LARGE = "statement_too_large"
# SSMS's "Generate Scripts" saves "Unicode text" by default: UTF-16 with a byte order mark (pre-flight L5). Neither BOM
# can start UTF-8 text, so the first bytes alone tell it, and the inspection of a file (Task 3) says the same words.
UTF16_MESSAGE = "This file is UTF-16 (SSMS's 'Unicode text'): save it as UTF-8"
_BOM = b"\xef\xbb\xbf"
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
_SPACE = re.compile(r"\s*")
_MEBIBYTE = 1_048_576
_SLICE = 1 << 20  # characters encoded at a time to count bytes: no copy of a whole large statement
_CONTINUATION = bytes(range(0x80, 0xC0))  # the bytes of a UTF-8 character after its first


def _characters(data: bytes) -> int:
    """How many characters UTF-8 `data` holds (a character cut off at its end counts): the bytes that start one."""
    return len(data) if data.isascii() else len(data.translate(None, _CONTINUATION))


def starts_with_utf16_bom(head: bytes) -> bool:
    """The first bytes of a file are a UTF-16 byte order mark (`head`: at least its first two bytes)."""
    return head.startswith(_UTF16_BOMS)


class StreamError(Exception):
    """The file cannot be read on as statements: `code` is FILE_NOT_UTF8 or STATEMENT_TOO_LARGE, `line` 1-based."""

    def __init__(self, code: str, message: str, line: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.line = line


@dataclass(frozen=True, slots=True)
class FileStatement:
    index: int  # 1-based among the file's statements; 0 for a psql meta-command line
    line: int  # 1-based line of its first character
    text: str
    end_byte: int  # bytes of the file up to the end of its terminator (the BOM included)
    repeat: int = 1  # T-SQL `GO n`
    meta: bool = False  # a psql meta-command line (PostgreSQL): never sent


class _Cursor:
    """A position in the current text with what lies before it: its line number and its byte offset in the file."""

    __slots__ = ("char", "line", "byte")

    def __init__(self, byte: int) -> None:
        self.char, self.line, self.byte = 0, 1, byte

    def advance(self, text: str, to: int) -> None:
        if to > self.char:
            self.line += text.count("\n", self.char, to)
            if text.isascii():  # a flag of the string: a character is a byte, nothing to encode
                self.byte += to - self.char
            else:  # in slices, never a copy of the whole stretch (a statement can be 256 MiB)
                for begin in range(self.char, to, _SLICE):
                    piece = text[begin:min(begin + _SLICE, to)]
                    self.byte += len(piece) if piece.isascii() else len(piece.encode("utf-8"))
            self.char = to

    def rebase(self, text: str, start: int) -> None:
        """The text now starts at `start`: positions before it are gone."""
        self.advance(text, start)
        self.char -= start


class StatementStream:
    """`for statement in StatementStream(file.read, dialect)`. Raises StreamError from the iteration.

    A line that is not UTF-8 text stops the stream at that line, after every statement that ended before it (pre-flight
    M3): they were read, so they are given out, and only then is the error raised."""

    def __init__(
        self, read: Callable[[int], bytes], dialect: Dialect, *, chunk: int = STREAM_CHUNK,
        max_statement_chars: int = MAX_STATEMENT_CHARS,
    ) -> None:
        self._read = read
        self._dialect = dialect
        self._chunk = chunk
        self._max = max_statement_chars
        self._meta = dialect is Dialect.POSTGRES  # psql meta-commands: `\restrict` in pg_dump's output (P8)
        self.bytes_read = 0  # of the file, so far

    def __iter__(self) -> Iterator[FileStatement]:
        pending = b""  # read, after the last line break
        text = ""  # decoded: from the start of the line where the statement that has not ended begins
        state = SplitState()
        spans_at = _Cursor(byte=0)  # where the last statement given out ended
        lines_at = _Cursor(byte=0)  # where the last statement given out began (its line)
        index = 0
        size = self._room(self._chunk, 0)
        bom_checked = False
        while True:
            data = self._read(size)
            self.bytes_read += len(data)
            eof = not data
            pending += data
            if not bom_checked:
                if len(pending) < len(_BOM) and not eof:
                    continue
                bom_checked = True
                if starts_with_utf16_bom(pending):
                    raise StreamError(FILE_NOT_UTF8, UTF16_MESSAGE, 1)
                if pending.startswith(_BOM):
                    pending = pending[len(_BOM):]
                    spans_at.byte = lines_at.byte = len(_BOM)
            cut = len(pending) if eof else pending.rfind(b"\n") + 1
            complete, pending = pending[:cut], pending[cut:]
            start_line = lines_at.line + text.count("\n", lines_at.char)
            bad: StreamError | None = None
            try:
                text += complete.decode("utf-8")
            except UnicodeDecodeError as exc:
                line = start_line + complete.count(b"\n", 0, exc.start)
                bad = StreamError(FILE_NOT_UTF8, f"Line {line:,} is not UTF-8 text", line)
                good = complete.rfind(b"\n", 0, exc.start) + 1  # the lines before the bad one are text
                text += complete[:good].decode("utf-8")
                eof = False  # the statement the bad line is part of never ends: held back
            ended = False
            # The read ended inside a line, so there is nothing new to split: what is held was scanned already, and
            # only the cap (below) is left to decide, without scanning it again
            spans = iter_spans(text, self._dialect, state, final=eof, meta_commands=self._meta) if (
                complete or eof
            ) else ()
            for span in spans:
                lines_at.advance(text, span.start)
                if self._measure(span) > self._max:  # only a read that gave more than it was asked for gets here
                    raise self._too_large(lines_at.line)
                spans_at.advance(text, span.terminator_end)
                index += not span.meta
                yield FileStatement(
                    index=0 if span.meta else index, line=lines_at.line, text=span.text, end_byte=spans_at.byte,
                    repeat=span.repeat, meta=span.meta,
                )
                ended = True
            if bad is not None:
                raise bad
            if eof:
                return
            self._skip_comments(text, state)
            line_start = text.rfind("\n", 0, state.resume) + 1
            spans_at.rebase(text, line_start)
            lines_at.rebase(text, line_start)
            text, state.resume = text[line_start:], state.resume - line_start
            where, held = self._held(text, state, pending)
            if held > self._max:
                raise self._too_large(lines_at.line + text.count("\n", lines_at.char, where))
            # a statement that goes on: read more at once, so it is rescanned fewer times (within the room left)
            size = self._room(self._chunk if ended else size * 2, held)

    def _room(self, wanted: int, held: int) -> int:
        """The next read: `wanted` bytes, but no more than it takes to pass the limit by one character."""
        return max(1, min(wanted, self._max - held + 1))

    def _skip_comments(self, text: str, state: SplitState) -> None:
        """Nothing but comments and blanks follows the last statement: `resume` moves past those that are complete, so
        a file of them is not held. A statement that has begun keeps the comments before it (rescanned with it)."""
        end = state.resume
        for token in scan(text, self._dialect, state.resume, state.delimiter):
            if token.kind not in INSIGNIFICANT:
                return
            if not token.terminated:  # a block comment that is still open
                break
            end = token.end
        state.resume = end

    def _held(self, text: str, state: SplitState, pending: bytes) -> tuple[int, int]:
        """What is held of the statement that has not ended: (a position in `text` to name its line from, characters).

        It is measured from its first token, as a finished statement is (comments before it are not part of it), to the
        end of the line read, `pending`, the cut line, included. With no token yet, it is the open comment, if any."""
        cut = _characters(pending)
        for token in scan(text, self._dialect, state.resume, state.delimiter):
            if token.kind not in INSIGNIFICANT:
                return token.start, len(text) - token.start + cut
        return state.resume, len(text) - state.resume + cut

    @staticmethod
    def _measure(span: StatementSpan) -> int:
        """A finished statement's size as the unfinished one was measured: from its first token to the end of its
        terminator (for the last statement of a file, which has none, to its last token)."""
        return span.terminator_end - span.start

    def _too_large(self, line: int) -> StreamError:
        return StreamError(
            STATEMENT_TOO_LARGE,
            f"The statement starting at line {line:,} is larger than {self._max // _MEBIBYTE:,} MiB", line,
        )
