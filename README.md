# Herdr Kanban

A small, **read-only Hermes Kanban viewer** for a terminal pane in Herdr—or any
ordinary terminal. Browse status groups, open task details, see comments and
prerequisites, and watch updates without leaving your workspace.

No pip packages, Hermes plugin, API key or Herdr automation required. It never
starts agents, moves panes, edits cards or creates a missing board.

## Start in 30 seconds

You need **Python 3.11+ with SQLite and curses**, a Linux or macOS terminal,
and an existing local Hermes Kanban database. Clone this repository, then:

```sh
git clone https://github.com/cgoddin/hermes-kanban-tui.git
cd hermes-kanban-tui
python3 -c 'import curses, sqlite3'  # prerequisite check
./bin/herdr-kanban --help
./bin/herdr-kanban --selftest       # fictional fixtures; no existing DB needed
./bin/herdr-kanban
```

The repository is named `hermes-kanban-tui`; the command remains `herdr-kanban`.

That is all you need to run it. Optional: make the command available everywhere
while keeping this directory at a stable location:

```sh
mkdir -p "$HOME/.local/bin"
ln -s "$(pwd -P)/bin/herdr-kanban" "$HOME/.local/bin/herdr-kanban"
export PATH="$HOME/.local/bin:$PATH"
herdr-kanban
```

Run these install commands from the cloned source directory. `ln -s` deliberately
refuses an existing destination: do not add `-f` or replace another installation.
Add the `export PATH=...` line to your shell startup file only if needed.
Moving/deleting the source directory breaks the link; no files are copied into Hermes.

### In Herdr

Open or choose an **idle terminal pane** and run `herdr-kanban` (or the absolute
path to `bin/herdr-kanban`). Size the pane however you like; `q` returns to the
shell. Do not send it into an occupied agent session. There are no layout
commands, background services or Herdr-specific settings to install.

The actual curses UI and terminal cleanup have been tested in real Linux PTYs;
live Herdr pane integration was **not** exercised. No particular Herdr version
is required by the code because it makes no Herdr calls.

## Choose your board

```sh
herdr-kanban --board default
herdr-kanban --board studio
herdr-kanban --board "/path/to/kanban.db"
herdr-kanban --hermes-home "$HOME/.hermes" --board studio
herdr-kanban --hermes-home "/path/to/custom-hermes-home"
```

Home resolution: `--hermes-home` → `HERMES_KANBAN_HOME` → `HERMES_HOME` →
`~/.hermes`. Paths may contain spaces; quote shell arguments. `~` and `~/...`
are expanded, but literal `$VARIABLE` text inside a path is not expanded by the
viewer. Use your shell's `"$VARIABLE"` instead.

Within that home:

- `default` is `kanban.db`.
- Other board slugs are `kanban/boards/SLUG/kanban.db`.
- Slugs allow 1–64 letters/digits/underscores/hyphens, start with a letter/digit,
  and are lowercased. No personal project mapping is needed.

Without `--board` (or compatibility `--project SLUG`), selection is:
`HERMES_KANBAN_DB` path → `HERMES_KANBAN_BOARD` slug → one-line UTF-8 slug in
`HOME/kanban/current` → `default` if that file is absent. An explicit
`--hermes-home` ignores the ambient DB/board pins, using that home's current
file or default instead. Explicit `--board` always wins over those pins.
Invalid selections and missing boards **error, never silently fall back**.
`--project` accepts any valid slug; if combined with `--board`, both must identify
the same database. The old special `misc` alias is not used: `misc` now means
an actual board named `misc`; use `default` for the default board.

**Profiles:** current Hermes normally shares boards across profiles. If a
profile sets `HERMES_HOME` to a profile directory but your board lives at the
shared root, use `--hermes-home "$HOME/.hermes" --board default` (or the correct
custom shared root), or set `HERMES_KANBAN_HOME` for the viewer's shell. Only
point at `~/.hermes/profiles/NAME` if that directory really owns your DB. This
standalone resolver does not import Hermes or read its configuration, board
metadata, archive tombstones or in-process context. It is not an exact clone of
all Hermes board routing rules. Verify the database path in the header.

## Keys

| Key | Action |
| --- | --- |
| `j` / `k`, ↓ / ↑ | Move between cards; scroll in details |
| `h` / `l`, ← / → | Previous / next status group |
| Enter / `d` | Open selected card |
| Esc / `b` | Back to board |
| PgUp / PgDn | Page |
| `g` / `G`, Home / End | First / last |
| `r` | Refresh now |
| `q` | Quit |

Auto-refresh runs every five seconds and retains the selected card by ID.
Details show body, comments, parents/prerequisites, children, result and latest
run summary. Missing optional details are labeled unavailable. Archived cards
are hidden; use `--show-archived` to include them. Recent done cards are limited
to 30; `--done-limit 100` changes the bound (1–500).

## Troubleshooting

- **Missing DB:** check the header/error path and use `--board /absolute/path`
  or `--hermes-home`. This viewer does not initialize Hermes or make empty boards.
- **Unsupported schema / not a database:** select a Hermes Kanban DB, not
  `state.db`. The required `tasks` columns are `id`, `title`, `status`,
  `created_at`. Optional comments, links and runs must have compatible tables
  when present. Views are rejected. Schema changes may need a viewer update.
- **No curses / SQLite:** use a Python build with those standard-library
  modules. On Debian/Ubuntu, `sudo apt install python3` normally supplies both.
  Native Windows is unsupported; use WSL with a Linux Python and terminal.
- **Interactive terminal / TERM error:** launch directly in a terminal, not
  a pipe or redirected output. Use your terminal's correct `TERM` setting.
- **Small pane:** enlarge it to see details. Even a tiny pane accepts `q`.
- **Locked / permission denied:** the viewer reports the error without changing
  the DB. Check access and the owning Hermes process; do not loosen permissions
  or delete SQLite journal files to make it work.

## Safety and verification

SQLite is opened with `mode=ro`, `query_only=ON` and an allowlist authorizer.
SQLite's normal locking/WAL shared-memory coordination still applies: read-only
refers to database/task contents, not a promise of zero filesystem activity.
Only task/detail columns are read; config, notifications, raw event/metadata
and process fields are excluded. Terminal controls are stripped and likely
credentials redacted, but redaction is heuristic: do not put secrets in task
prose or treat this as a security boundary for sharing screenshots.

```sh
./bin/herdr-kanban --selftest
# Equivalent from the source directory:
python3 -B -m unittest discover -s tests -v
```

Tests use **fictional disposable SQLite fixtures**, never your board. They
cover read-only denials, selection, errors, safety, navigation, refresh, resize,
terminal restoration and fresh user-bin install/uninstall. Verified on Linux
with Python 3.11.16 and 3.14.9. PTY tests run on Linux only. macOS is intended
but untested; other Python/Hermes versions are not certified. Compatibility is
schema-based, not a claim that every Hermes release is supported. Source is
published at [cgoddin/hermes-kanban-tui](https://github.com/cgoddin/hermes-kanban-tui).
The verification above is local test evidence, not a hosted CI result.

## Uninstall

If you created the optional symlink above, remove **only that link**:

```sh
# First confirm this points at the source directory you installed:
ls -l "$HOME/.local/bin/herdr-kanban"
rm "$HOME/.local/bin/herdr-kanban"
```

You can then delete the cloned source directory if no longer needed. Leave
all Hermes directories and databases alone; there are no services, migrations
or Hermes configuration changes to undo.

## Source and permissions

This is a curated, TUI-only source export of the original workstation tools,
not a copy of their operational history or layout automation. See
[PROVENANCE.md](PROVENANCE.md) for the original identity, changes and licensing
status. That record describes preparation of the export before publication;
this repository is its public destination. No new public/open-source license
is granted by publication.
