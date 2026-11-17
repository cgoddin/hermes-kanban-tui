"""Read-only SQLite Kanban navigation. No curses or store access at import."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import os
import re
import signal
import sqlite3
import sys
import textwrap
import time
import unicodedata

from .safety import sanitize


STATUSES = ("triage", "todo", "ready", "running", "review", "blocked", "scheduled", "done")
ALLOWED = {
    "tasks": frozenset(("id", "title", "body", "assignee", "status", "priority", "created_at",
                        "started_at", "completed_at", "result", "last_failure_error",
                        "current_run_id", "block_kind")),
    "task_comments": frozenset(("id", "task_id", "author", "body", "created_at")),
    "task_links": frozenset(("parent_id", "child_id")),
    "task_runs": frozenset(("id", "task_id", "profile", "status", "started_at", "ended_at",
                            "outcome", "summary", "error")),
}
REQUIRED = {
    "tasks": {"id", "title", "status", "created_at"},
    "task_comments": {"id", "task_id", "author", "body", "created_at"},
    "task_links": {"parent_id", "child_id"},
    "task_runs": {"id", "task_id", "status", "started_at"},
}


class BoardError(ValueError):
    """A safe-to-report board selection, schema or read error."""


def _path(value):
    # HOME is intentionally honored for portable installs and isolated tests.
    value = os.fspath(value)
    if not value:
        raise BoardError("Empty path; supply a Hermes home or DB path")
    if value.startswith("~") and value != "~" and not value.startswith("~/"):
        raise BoardError("Only the current user's ~ path is supported")
    return Path(value).expanduser().resolve()


def resolve_board(project=None, board=None, hermes_home=None) -> Path:
    """Resolve without creating data; explicit inputs outrank ambient pins.

    --project is a compatibility alias for an arbitrary board slug. With both
    options supplied, identities must agree. Explicit --hermes-home isolates
    selection from HERMES_KANBAN_DB/BOARD, but still reads that home's current
    board file. Invalid or stale selections never silently fall back.
    """
    home = _path(hermes_home if hermes_home is not None else
                 os.environ.get("HERMES_KANBAN_HOME") or
                 os.environ.get("HERMES_HOME") or "~/.hermes")

    def named(name):
        name = str(name).casefold()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
            raise BoardError("Invalid board name; use a 1–64 character slug or an explicit DB path")
        return home / "kanban.db" if name == "default" else home / "kanban" / "boards" / name / "kanban.db"

    expected = named(project).resolve() if project is not None else None
    if board is not None:
        value = os.fspath(board)
        is_path = isinstance(board, Path) or "/" in value or value.startswith((".", "~")) or value.endswith(".db")
        selected = (_path(value) if is_path else named(value)).resolve()
        if expected is not None and selected != expected:
            raise BoardError("Project/board mismatch; use matching board names and --hermes-home")
        return selected
    if expected is not None:
        return expected
    if hermes_home is None:
        if os.environ.get("HERMES_KANBAN_DB"):
            return _path(os.environ["HERMES_KANBAN_DB"])
        if os.environ.get("HERMES_KANBAN_BOARD"):
            return named(os.environ["HERMES_KANBAN_BOARD"]).resolve()
    current = home / "kanban" / "current"
    try:
        # Current Hermes uses a one-line UTF-8 slug, not board metadata.
        if current.exists() and not current.is_file():
            raise BoardError("Current board is not a regular file; select --board explicitly")
        with current.open(encoding="utf-8-sig") as handle:
            value = handle.read(256)
        if len(value) >= 256:
            raise BoardError("Current board file is too long; select --board explicitly")
        value = value.strip()
    except FileNotFoundError:
        if current.is_symlink():
            raise BoardError("Broken current-board link; select --board explicitly") from None
        value = "default"
    except (OSError, UnicodeError):
        raise BoardError("Cannot read current board; select --board explicitly") from None
    return named(value).resolve()


class BoardStore:
    """Fail-closed reader with both filesystem read-only and SQL authorizer gates."""

    def __init__(self, path):
        self.path = _path(path)
        self.connection = None
        self.columns = {}
        if not self.path.is_file():
            raise BoardError("Board DB does not exist or is not a file: " + sanitize(self.path))
        try:
            self.connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=1)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA query_only=ON")
            self.connection.execute("PRAGMA trusted_schema=OFF")
            self.connection.set_authorizer(self._authorize)
            for table in ALLOWED:
                row = self.connection.execute(
                    "SELECT type FROM sqlite_master WHERE name=?", (table,)
                ).fetchone()
                if row is None:
                    if table == "tasks":
                        raise BoardError("Unsupported board schema: missing tasks table")
                    continue
                if row[0] != "table":
                    raise BoardError("Unsupported board schema: views are not permitted")
                fields = {r[1] for r in self.connection.execute('PRAGMA table_info("' + table + '")')}
                if not REQUIRED[table] <= fields:
                    raise BoardError("Unsupported board schema: missing required columns in " + table)
                self.columns[table] = fields & ALLOWED[table]
        except (sqlite3.Error, OSError, BoardError) as exc:
            self.close()
            if isinstance(exc, BoardError):
                raise
            raise BoardError("Cannot read board DB: " + sanitize(exc)) from None

    @staticmethod
    def _authorize(action, arg1, arg2, database, source):
        if source is not None or database not in (None, "main"):
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            if arg1 == "sqlite_master" and arg2 in {"name", "type"}:
                return sqlite3.SQLITE_OK
            if arg1 in ALLOWED and arg2 in ALLOWED[arg1]:
                return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA:
            if arg1 == "table_info" and arg2 in ALLOWED:
                return sqlite3.SQLITE_OK
            if arg1 == "query_only" and arg2 is None:
                return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and arg2 == "coalesce":
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_TRANSACTION and arg1 in {"BEGIN", "COMMIT", "ROLLBACK"}:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def _fields(self, table, names, prefix=""):
        return ", ".join(
            prefix + '"' + name + '"' if name in self.columns[table] else 'NULL AS "' + name + '"'
            for name in names
        )

    @contextmanager
    def snapshot(self):
        try:
            self.connection.execute("BEGIN")
            yield
            self.connection.execute("COMMIT")
        except sqlite3.Error as exc:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise BoardError("Board read failed: " + sanitize(exc)) from None
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def tasks(self, done_limit=30, show_archived=False):
        if not isinstance(done_limit, int) or not 1 <= done_limit <= 500:
            raise BoardError("Recent done limit must be between 1 and 500")
        names = ("id", "title", "status", "assignee", "block_kind", "created_at", "completed_at", "priority")
        fields = self._fields("tasks", names)
        order = 'COALESCE(completed_at, created_at)' if "completed_at" in self.columns["tasks"] else "created_at"
        with self.snapshot():
            active = self.connection.execute(
                "SELECT " + fields + " FROM tasks WHERE status != 'done'" +
                ("" if show_archived else " AND status != 'archived'") + " ORDER BY created_at, id"
            ).fetchall()
            done = self.connection.execute(
                "SELECT " + fields + " FROM tasks WHERE status = 'done' ORDER BY " + order +
                " DESC, id LIMIT ?", (done_limit,)
            ).fetchall()
        return [dict(row) for row in active + done]

    def detail(self, task_id):
        with self.snapshot():
            row = self.connection.execute(
                "SELECT " + self._fields("tasks", sorted(ALLOWED["tasks"])) + " FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            detail = {"task": dict(row), "comments": [], "parents": [], "dependencies": [],
                      "last_run": None, "unavailable": []}
            for table in ("task_comments", "task_links", "task_runs"):
                if table not in self.columns:
                    detail["unavailable"].append(table)
            if "task_comments" in self.columns:
                detail["comments"] = [dict(r) for r in self.connection.execute(
                    "SELECT id, author, body, created_at FROM task_comments WHERE task_id=? ORDER BY created_at, id",
                    (task_id,))]
            if "task_links" in self.columns:
                # Parent links are prerequisite dependencies; also show children
                # that depend on this card. LEFT JOIN preserves dangling links.
                for key, direction, other in (("parents", "child_id", "parent_id"),
                                               ("dependencies", "parent_id", "child_id")):
                    detail[key] = [dict(r) for r in self.connection.execute(
                        'SELECT l."' + other + '" AS id, t.title, t.status FROM task_links AS l '
                        'LEFT JOIN tasks AS t ON t.id=l."' + other + '" WHERE l."' + direction + '"=? '
                        'ORDER BY l."' + other + '"', (task_id,))]
            if "task_runs" in self.columns:
                run = self.connection.execute(
                    "SELECT " + self._fields("task_runs", sorted(ALLOWED["task_runs"])) +
                    " FROM task_runs WHERE task_id=? ORDER BY started_at DESC, id DESC LIMIT 1", (task_id,)
                ).fetchone()
                detail["last_run"] = dict(run) if run else None
        return detail

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def detail_lines(detail):
    """Return sanitized prose without selecting events, metadata or process fields."""
    if detail is None:
        return ["Task is no longer available; refresh to select another card."]
    task = detail["task"]
    lines = [f"{task['id']} | {task['title']}"]
    for name in ("status", "assignee", "created_at", "started_at", "completed_at", "current_run_id", "block_kind"):
        lines.append(f"{name}: {task.get(name) if task.get(name) is not None else '-'}")
    for label, field in (("Body", "body"), ("Result", "result"), ("Last failure / blocker error", "last_failure_error")):
        lines.extend(["", label + ":", str(task.get(field) or "(none)")])
    lines.extend(["", "Parents / prerequisites (unfinished are blockers):"])
    for parent in detail["parents"]:
        marker = "BLOCKER" if parent["status"] not in ("done", "archived") else "satisfied"
        lines.append(f"{parent['id']} [{parent['status'] or 'missing'}] {parent['title'] or '(missing task)'} ({marker})")
    if not detail["parents"]:
        lines.append("(none)")
    lines.extend(["", "Dependencies / children (depend on this task):"])
    lines.extend(f"{r['id']} [{r['status'] or 'missing'}] {r['title'] or '(missing task)'}" for r in detail["dependencies"])
    if not detail["dependencies"]:
        lines.append("(none)")
    lines.extend(["", "Last run:"])
    run = detail["last_run"]
    if run:
        for name in ("id", "profile", "status", "started_at", "ended_at", "outcome", "summary", "error"):
            lines.append(f"{name}: {run.get(name) if run.get(name) is not None else '-'}")
    else:
        lines.append("(none)")
    lines.extend(["", "Comments:"])
    for comment in detail["comments"]:
        lines.append(f"{comment['created_at']} {comment['author']}: {comment['body']}")
    if not detail["comments"]:
        lines.append("(none)")
    if detail["unavailable"]:
        lines.append("Optional tables unavailable: " + ", ".join(detail["unavailable"]))
    return [line for item in lines for line in sanitize(item).split("\n")]


@dataclass
class Navigation:
    """Pure selection/scroll state, independent of curses and terminal dimensions."""
    cards: list
    selected_id: object = None
    top: int = 0
    detail_open: bool = False
    detail_top: int = 0

    def __post_init__(self):
        self.replace(self.cards)

    @property
    def index(self):
        return next((i for i, c in enumerate(self.cards) if c["id"] == self.selected_id), 0)

    def replace(self, cards):
        old_index = self.index
        self.cards = list(cards)
        if not any(c["id"] == self.selected_id for c in self.cards):
            self.selected_id = self.cards[min(old_index, len(self.cards) - 1)]["id"] if self.cards else None
            self.detail_top = 0
        self.top = max(0, self.top)

    def move(self, amount):
        if self.cards:
            self.selected_id = self.cards[max(0, min(len(self.cards) - 1, self.index + amount))]["id"]
            self.detail_top = 0

    def group(self, direction):
        if self.cards:
            status = self.cards[self.index]["status"]
            indices = range(self.index + direction, len(self.cards) if direction > 0 else -1, direction)
            for index in indices:
                if self.cards[index]["status"] != status:
                    self.selected_id = self.cards[index]["id"]
                    self.detail_top = 0
                    break

    def rows(self):
        rows = []
        groups = list(STATUSES) + sorted({c["status"] for c in self.cards} - set(STATUSES))
        for status in groups:
            cards = [c for c in self.cards if c["status"] == status]
            rows.append((None, f"{status} ({len(cards)})"))
            rows.extend((c["id"], f"{c['id']} {c['title']}" +
                         (f" @{c['assignee']}" if c.get("assignee") else "")) for c in cards)
        return rows

    def visible(self, height):
        rows = self.rows()
        selected = next((i for i, row in enumerate(rows) if row[0] == self.selected_id and row[0] is not None), 0)
        height = max(1, height)
        self.top = min(self.top, max(0, len(rows) - height))
        if selected < self.top:
            self.top = selected
        elif selected >= self.top + height:
            self.top = selected - height + 1
        return rows[self.top:self.top + height]


def grouped(cards):
    groups = list(STATUSES) + sorted({c["status"] for c in cards} - set(STATUSES))
    return [card for status in groups for card in cards if card["status"] == status]


def _clip(text, width):
    """Clip by terminal cells (not bytes); avoid half a wide character."""
    result, used = [], 0
    for char in sanitize(text).replace("\n", " "):
        cells = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in "WF" else 1)
        if used + cells > width:
            break
        result.append(char)
        used += cells
    return "".join(result)


def _wrap(lines, width):
    # textwrap alone counts codepoints, so use cell-aware clipping for each
    # resulting line, and retain every character even at one-column widths.
    width = max(2, width)
    result = []
    for line in lines:
        chunks = textwrap.wrap(sanitize(line), width=width, replace_whitespace=False) or [""]
        for chunk in chunks:
            while chunk:
                part = _clip(chunk, width)
                result.append(part)
                chunk = chunk[len(part):]
            if not chunk and chunks == [""]:
                result.append("")
    return result


def run_viewer(screen, store, project, done_limit=30, show_archived=False):
    import curses

    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    screen.timeout(200)
    nav = Navigation(grouped(store.tasks(done_limit, show_archived)))
    detail = None
    message = "READ ONLY"
    board_label = store.path.parent.name if store.path.parent.parent.name == "boards" else ("Default" if store.path.name == "kanban.db" else store.path.name)
    next_refresh = time.monotonic() + 5

    def refresh():
        nonlocal detail, message, next_refresh
        try:
            nav.replace(grouped(store.tasks(done_limit, show_archived)))
            detail = store.detail(nav.selected_id) if nav.detail_open and nav.selected_id is not None else None
            message = "READ ONLY | refreshed"
        except BoardError as exc:
            message = "Refresh failed: " + sanitize(exc)
        next_refresh = time.monotonic() + 5

    def draw(y, value, attr=0):
        height, width = screen.getmaxyx()
        if 0 <= y < height and width > 1:
            try:
                screen.addstr(y, 0, _clip(value, width - 1), attr)
            except curses.error:
                # Resize can race a draw; redraw at the next key/timeout.
                pass

    while True:
        if time.monotonic() >= next_refresh:
            refresh()
        height, width = screen.getmaxyx()
        screen.erase()
        draw(0, f"Project: {project or '(explicit board)'} | Board: {board_label} | {store.path}", curses.A_BOLD)
        if height < 4 or width < 12:
            draw(min(1, height - 1), "Small terminal: q exits; resize to view")
        else:
            draw(1, ("DETAIL | " if nav.detail_open else "BOARD | ") + message)
            capacity = max(1, height - 3)
            if nav.detail_open:
                lines = _wrap(detail_lines(detail), max(2, width - 1))
                nav.detail_top = max(0, min(nav.detail_top, max(0, len(lines) - capacity)))
                for y, line in enumerate(lines[nav.detail_top:nav.detail_top + capacity], 2):
                    draw(y, line)
            else:
                for y, (task_id, line) in enumerate(nav.visible(capacity), 2):
                    selected = task_id is not None and task_id == nav.selected_id
                    draw(y, ("> " if selected else "  ") + line,
                         curses.A_REVERSE if selected else (curses.A_BOLD if task_id is None else 0))
            draw(height - 1, "q quit | j/k move | h/l group | Enter detail | Esc back | r refresh | PgUp/PgDn g/G")
        screen.refresh()
        key = screen.getch()
        if key in (ord("q"), ord("Q")):
            return
        if key == curses.KEY_RESIZE or key == -1:
            continue
        if key == ord("r"):
            refresh()
            continue
        if key in (27, ord("b")):
            nav.detail_open = False
            continue
        if key in (10, 13, curses.KEY_ENTER, ord("d")) and nav.selected_id is not None:
            nav.detail_open = True
            nav.detail_top = 0
            refresh()
            continue
        amount = {ord("j"): 1, curses.KEY_DOWN: 1, ord("k"): -1, curses.KEY_UP: -1,
                  curses.KEY_NPAGE: max(1, height - 3), curses.KEY_PPAGE: -max(1, height - 3)}.get(key)
        if nav.detail_open:
            if amount is not None:
                nav.detail_top = max(0, nav.detail_top + amount)
            elif key in (ord("g"), curses.KEY_HOME):
                nav.detail_top = 0
            elif key in (ord("G"), curses.KEY_END):
                nav.detail_top = 10**9
        elif amount is not None:
            nav.move(amount)
        elif key in (ord("h"), curses.KEY_LEFT, ord("l"), curses.KEY_RIGHT):
            nav.group(-1 if key in (ord("h"), curses.KEY_LEFT) else 1)
        elif key in (ord("g"), curses.KEY_HOME):
            nav.move(-len(nav.cards))
        elif key in (ord("G"), curses.KEY_END):
            nav.move(len(nav.cards))


class SafeParser(argparse.ArgumentParser):
    def _print_message(self, message, file=None):
        if message:
            (file or sys.stderr).write(sanitize(message))


def _selftest():
    import unittest
    # Never discover other agents' tests or consult any store/config.
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parent.parent / "tests"))
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


def main(argv=None):
    parser = SafeParser(description="Read-only SQLite Kanban viewer (no Herdr calls)")
    parser.add_argument("--project", help="Compatibility alias for a board slug (no fixed project mapping)")
    parser.add_argument("--board", help="Board slug or DB path; defaults to configured board or default")
    parser.add_argument("--hermes-home", help="Board root; overrides home variables and ambient DB/board pins")
    parser.add_argument("--done-limit", type=int, default=30, help="Recent completed cards, 1..500 (default 30)")
    parser.add_argument("--show-archived", action="store_true")
    parser.add_argument("--selftest", action="store_true", help="Run the repository's offline unittest suite")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    try:
        if not 1 <= args.done_limit <= 500:
            raise BoardError("--done-limit must be between 1 and 500")
        path = resolve_board(args.project, args.board, args.hermes_home)
        with BoardStore(path) as store:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise BoardError("An interactive terminal is required (or use --help / --selftest)")
            import curses
            import termios
            # Save attributes explicitly in addition to curses.wrapper cleanup.
            # SIGTERM/HUP unwind normally rather than abandoning a raw terminal.
            fd = sys.stdin.fileno()
            attributes = termios.tcgetattr(fd)
            previous = {}

            def interrupted(signum, frame):
                raise KeyboardInterrupt

            try:
                for sig in (signal.SIGTERM, signal.SIGHUP):
                    previous[sig] = signal.signal(sig, interrupted)
                curses.wrapper(run_viewer, store, args.project, args.done_limit, args.show_archived)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
                termios.tcsetattr(fd, termios.TCSANOW, attributes)
        return 0
    except KeyboardInterrupt:
        return 130
    except (BoardError, OSError, sqlite3.Error) as exc:
        sys.stderr.write("herdr-kanban: " + sanitize(exc) + "\n")
        return 2
    except Exception as exc:
        # Avoid traceback/local-variable exposure on terminal failures.
        sys.stderr.write("herdr-kanban: viewer failed: " + sanitize(exc) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
