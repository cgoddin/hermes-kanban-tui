"""Deterministic viewer tests; never open live stores, config or environments."""

from contextlib import closing
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import unicodedata
from unittest import mock

from herdr_workstation import kanban
from herdr_workstation.safety import sanitize


ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "bin" / "herdr-kanban"
CHILD_ENV = {"PATH": "/usr/bin:/bin", "TERM": "xterm-256color", "PYTHONDONTWRITEBYTECODE": "1"}

SCHEMA = """
CREATE TABLE tasks (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT,
 status TEXT NOT NULL, priority INTEGER DEFAULT 0, created_at INTEGER NOT NULL,
 started_at INTEGER, completed_at INTEGER, result TEXT, last_failure_error TEXT,
 current_run_id INTEGER, block_kind TEXT,
 claim_lock TEXT, workspace_path TEXT, worker_pid INTEGER, session_id TEXT
);
CREATE TABLE task_comments (
 id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INTEGER
);
CREATE TABLE task_links (parent_id TEXT, child_id TEXT, PRIMARY KEY(parent_id, child_id));
CREATE TABLE task_runs (
 id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT, started_at INTEGER,
 ended_at INTEGER, outcome TEXT, summary TEXT, error TEXT, metadata TEXT, claim_lock TEXT,
 worker_pid INTEGER
);
CREATE TABLE task_events (id INTEGER, task_id TEXT, payload TEXT);
CREATE TABLE environment (name TEXT, value TEXT);
CREATE TABLE config (name TEXT, value TEXT);
CREATE TABLE kanban_notify_subs (task_id TEXT, chat_id TEXT);
"""


def fixture(path, title_prefix=""):
    """Only call with a path inside the test's TemporaryDirectory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(SCHEMA)
        for index, status in enumerate(kanban.STATUSES + ("archived", "future_status")):
            conn.execute(
                "INSERT INTO tasks(id,title,body,assignee,status,created_at,completed_at,result,"
                "last_failure_error,block_kind,claim_lock,workspace_path,worker_pid,session_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (status + "-id", title_prefix + status + " card", "Ordinary prose with details.\nSecond paragraph.",
                 "author", status, index + 1, 100 if status == "done" else None,
                 "Useful result", "api_key=never-display-this", "dependency" if status == "blocked" else None,
                 "never-selected-lock", "never-selected-workspace", 999, "never-selected-session"),
            )
        conn.execute("INSERT INTO task_comments VALUES(1,'todo-id','reviewer','comment prose',20)")
        conn.execute("INSERT INTO task_comments VALUES(2,'todo-id','hostile\x1b[31m','TOKEN=comment-secret',21)")
        conn.execute("INSERT INTO task_links VALUES('blocked-id','todo-id')")
        conn.execute("INSERT INTO task_links VALUES('done-id','todo-id')")
        conn.execute("INSERT INTO task_links VALUES('missing-id','todo-id')")
        conn.execute("INSERT INTO task_links VALUES('todo-id','ready-id')")
        conn.execute("INSERT INTO task_runs VALUES(1,'todo-id','test','failed',1,2,'crashed','old run','old error',"
                     "'raw environment secret','run-lock',123)")
        conn.execute("INSERT INTO task_runs VALUES(2,'todo-id','test','done',3,4,'completed','latest summary',"
                     "'password=run-secret','cmdline: secret --token xyz','run-lock',123)")
        conn.execute("INSERT INTO environment VALUES('TOKEN','never-select-env')")
        conn.execute("INSERT INTO config VALUES('secret','never-select-config')")
        conn.execute("INSERT INTO task_events VALUES(1,'todo-id','never-select-event')")
    return path


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="herdr-viewer-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def db(self, name="fixture.db", prefix=""):
        return fixture(self.home / name, prefix)

    def cli(self, *args, wrapper=WRAPPER):
        return subprocess.run(
            [str(wrapper), "--hermes-home", str(self.home), *map(str, args)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=CHILD_ENV, cwd=self.home, timeout=10,
        )


class PortableResolutionTests(FixtureCase):
    def test_default_and_home_environment(self):
        with mock.patch.dict('os.environ', {'HOME': str(self.home)}, clear=True):
            self.assertEqual(kanban.resolve_board(), self.home / '.hermes/kanban.db')
        with mock.patch.dict('os.environ', {'HERMES_HOME': str(self.home)}, clear=True):
            self.assertEqual(kanban.resolve_board(), self.home / 'kanban.db')
        with mock.patch.dict('os.environ', {'HERMES_KANBAN_HOME': str(self.home),
                                          'HERMES_HOME': str(self.home / 'profile')}, clear=True):
            self.assertEqual(kanban.resolve_board(), self.home / 'kanban.db')

    def test_environment_pins_and_explicit_overrides(self):
        env = {'HERMES_HOME': str(self.home), 'HERMES_KANBAN_BOARD': 'studio',
               'HERMES_KANBAN_DB': str(self.home / 'pinned.db')}
        with mock.patch.dict('os.environ', env, clear=True):
            self.assertEqual(kanban.resolve_board(), self.home / 'pinned.db')
            self.assertEqual(kanban.resolve_board(board='default'), self.home / 'kanban.db')
            self.assertEqual(kanban.resolve_board(hermes_home=self.home), self.home / 'kanban.db')
            self.assertEqual(kanban.resolve_board(board='studio'), self.home / 'kanban/boards/studio/kanban.db')
            del os.environ['HERMES_KANBAN_DB']
            self.assertEqual(kanban.resolve_board(), self.home / 'kanban/boards/studio/kanban.db')

    def test_current_board_file_and_invalid_selections(self):
        current = self.home / 'kanban/current'
        current.parent.mkdir()
        current.write_text('\ufeffstudio\n', encoding='utf-8')
        self.assertEqual(kanban.resolve_board(hermes_home=self.home), self.home / 'kanban/boards/studio/kanban.db')
        for value in ('../escape', '', 'a' * 65, 'studio\nother'):
            current.write_text(value)
            with self.assertRaises(kanban.BoardError):
                kanban.resolve_board(hermes_home=self.home)
        self.assertEqual(kanban.resolve_board(board='default', hermes_home=self.home), self.home / 'kanban.db')
        current.unlink()
        current.mkdir()
        with self.assertRaises(kanban.BoardError):
            kanban.resolve_board(hermes_home=self.home)

    def test_arbitrary_project_alias_and_mismatch(self):
        self.assertEqual(kanban.resolve_board(project='studio', hermes_home=self.home),
                         self.home / 'kanban/boards/studio/kanban.db')
        with self.assertRaisesRegex(kanban.BoardError, 'mismatch'):
            kanban.resolve_board(project='studio', board='other', hermes_home=self.home)
        for value in ('', 'bad name', '--bad', 'a' * 65):
            with self.assertRaises(kanban.BoardError):
                kanban.resolve_board(board=value, hermes_home=self.home)


class ResolutionTests(FixtureCase):
    def test_generic_names_and_default_are_case_insensitive(self):
        for name in ("studio", "research", "my-team", "misc"):
            expected = self.home / "kanban" / "boards" / name / "kanban.db"
            self.assertEqual(kanban.resolve_board(project=name, hermes_home=self.home), expected)
            self.assertEqual(kanban.resolve_board(project=name.upper(), board=name, hermes_home=self.home), expected)
        self.assertEqual(kanban.resolve_board(board="default", hermes_home=self.home), self.home / "kanban.db")
        self.assertEqual(kanban.resolve_board(hermes_home=self.home), self.home / "kanban.db")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_explicit_home_path_and_custom_slug(self):
        path = self.home / "custom?with# uri characters.db"
        self.assertEqual(kanban.resolve_board(board=path, hermes_home=self.home), path)
        self.assertEqual(kanban.resolve_board(board="research", hermes_home=self.home),
                         self.home / "kanban" / "boards" / "research" / "kanban.db")
        other = self.home / "explicit home"
        expected = other / "kanban" / "boards" / "studio" / "kanban.db"
        self.assertEqual(kanban.resolve_board(project="studio", board=expected, hermes_home=other), expected)
        for name in ("bad name", "a.b", "--board", ""):
            with self.assertRaises(kanban.BoardError):
                kanban.resolve_board(board=name, hermes_home=self.home)

    def test_mismatches_and_symlink_identity(self):
        canonical = self.db("kanban/boards/studio/kanban.db")
        alias = self.home / "alias.db"
        alias.symlink_to(canonical)
        self.assertEqual(kanban.resolve_board(project="studio", board=alias, hermes_home=self.home), canonical)
        for board in ("research", "default", self.home / "other.db"):
            with self.assertRaisesRegex(kanban.BoardError, "mismatch"):
                kanban.resolve_board(project="studio", board=board, hermes_home=self.home)


class ReaderTests(FixtureCase):
    def test_wal_reader_sees_commits_without_changing_database_or_wal(self):
        path = self.db()
        with closing(sqlite3.connect(path)) as writer:
            self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
            writer.execute("UPDATE tasks SET title='Fictional WAL commit' WHERE id='todo-id'")
            writer.commit()
            wal = path.with_name(path.name + '-wal')
            before = (path.read_bytes(), wal.read_bytes())
            with kanban.BoardStore(path) as store:
                detail = store.detail('todo-id')
                assert detail is not None
                self.assertEqual(detail['task']['title'], 'Fictional WAL commit')
                self.assertEqual((path.read_bytes(), wal.read_bytes()), before)
                writer.execute("UPDATE tasks SET title='Fictional next commit' WHERE id='todo-id'")
                writer.commit()
                after_writer = (path.read_bytes(), wal.read_bytes())
                detail = store.detail('todo-id')
                assert detail is not None
                self.assertEqual(detail['task']['title'], 'Fictional next commit')
                self.assertEqual((path.read_bytes(), wal.read_bytes()), after_writer)

    def test_readonly_authorizer_and_no_unauthorized_selects(self):
        path = self.db()
        before = path.read_bytes()
        with kanban.BoardStore(path) as store:
            self.assertEqual(store.connection.execute("PRAGMA query_only").fetchone()[0], 1)
            queries = (
                "UPDATE tasks SET title='mutated'", "DELETE FROM tasks", "CREATE TABLE hacked(x)",
                "PRAGMA query_only=OFF", "PRAGMA writable_schema=ON", "PRAGMA user_version=1",
                "ATTACH DATABASE ':memory:' AS other", "SELECT * FROM environment", "SELECT * FROM config",
                "SELECT payload FROM task_events", "SELECT metadata FROM task_runs", "SELECT claim_lock FROM tasks",
                "SELECT workspace_path, worker_pid, session_id FROM tasks", "SELECT * FROM kanban_notify_subs",
                "SELECT sql FROM sqlite_master", "SELECT load_extension('anything')", "PRAGMA table_info(environment)",
                "SELECT * FROM sqlite_master",
            )
            for query in queries:
                with self.subTest(query=query), self.assertRaises(sqlite3.DatabaseError):
                    store.connection.execute(query).fetchall()
            cards = store.tasks()
            self.assertEqual(len(cards), 9)
            detail = store.detail("todo-id")
            displayed = "\n".join(kanban.detail_lines(detail))
            for forbidden in ("never-selected", "never-select", "run-lock", "run-secret", "comment-secret", "raw environment", "cmdline"):
                self.assertNotIn(forbidden, displayed)
        self.assertEqual(path.read_bytes(), before)

    def test_uri_mode_ro_even_without_authorizer_or_query_only(self):
        with kanban.BoardStore(self.db("spaces #?%.db")) as store:
            store.connection.set_authorizer(None)
            store.connection.execute("PRAGMA query_only=OFF")
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                store.connection.execute("INSERT INTO tasks(id,title,status,created_at) VALUES('x','x','todo',0)")

    def test_board_isolation(self):
        one = self.db("one.db", "ONE ")
        two = self.db("two.db", "TWO ")
        with kanban.BoardStore(one) as a, kanban.BoardStore(two) as b:
            self.assertTrue(all(card["title"].startswith("ONE ") for card in a.tasks()))
            self.assertTrue(all(card["title"].startswith("TWO ") for card in b.tasks()))
            self.assertEqual(a.detail("todo-id")["task"]["title"], "ONE todo card")

    def test_missing_wrong_and_views_fail_without_creation_or_changes(self):
        missing = self.home / "missing" / "kanban.db"
        with self.assertRaises(kanban.BoardError):
            kanban.BoardStore(missing)
        self.assertFalse(missing.parent.exists())
        with self.assertRaises(kanban.BoardError):
            kanban.BoardStore(self.home)
        for index, schema in enumerate((
            "CREATE TABLE unrelated(value TEXT)", "CREATE TABLE tasks(id TEXT, title TEXT)",
            "CREATE TABLE data(id,title,status,created_at); CREATE VIEW tasks AS SELECT * FROM data;",
            "CREATE TABLE tasks(id,title,status,created_at); CREATE VIEW task_comments AS SELECT * FROM tasks;",
            "CREATE TABLE tasks(id,title,status,created_at); CREATE TABLE task_runs(id,task_id);",
        )):
            path = self.home / f"wrong{index}.db"
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.executescript(schema)
            before = path.read_bytes()
            with self.assertRaisesRegex(kanban.BoardError, "schema"):
                kanban.BoardStore(path)
            self.assertEqual(before, path.read_bytes())
        corrupt = self.home / "corrupt.db"
        corrupt.write_bytes(b"not a sqlite database\x1b[31m")
        with self.assertRaises(kanban.BoardError):
            kanban.BoardStore(corrupt)
        self.assertEqual(corrupt.read_bytes(), b"not a sqlite database\x1b[31m")

    def test_view_cannot_reach_unapproved_tables_through_allowed_names(self):
        path = self.home / "hostile-view.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.executescript(
                "CREATE TABLE environment(id,title,status,created_at); "
                "CREATE VIEW tasks AS SELECT * FROM environment;"
            )
        with self.assertRaisesRegex(kanban.BoardError, "views"):
            kanban.BoardStore(path)

    def test_locked_db_failure_keeps_navigation_state(self):
        path = self.db()
        with kanban.BoardStore(path) as store, closing(sqlite3.connect(path)) as writer:
            nav = kanban.Navigation(kanban.grouped(store.tasks()))
            nav.move(2)
            writer.execute("BEGIN EXCLUSIVE")
            try:
                with self.assertRaisesRegex(kanban.BoardError, "read failed"):
                    store.tasks()
                self.assertEqual(nav.selected_id, "ready-id")
                self.assertFalse(store.connection.in_transaction)
            finally:
                writer.rollback()
            self.assertEqual(len(store.tasks()), len(nav.cards))

    def test_optional_tables_and_columns_degrade_explicitly(self):
        path = self.home / "minimal.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.executescript("CREATE TABLE tasks(id TEXT PRIMARY KEY,title TEXT,status TEXT,created_at INTEGER);")
            conn.execute("INSERT INTO tasks VALUES('one','Title','todo',1)")
        with kanban.BoardStore(path) as store:
            self.assertEqual(len(store.tasks()), 1)
            self.assertIsNone(store.tasks()[0]["assignee"])
            detail = store.detail("one")
            self.assertEqual(set(detail["unavailable"]), {"task_runs", "task_comments", "task_links"})
            self.assertIn("Optional tables unavailable", "\n".join(kanban.detail_lines(detail)))
            self.assertIsNone(store.detail("nonexistent"))

    def test_statuses_archival_bounded_recent_done(self):
        path = self.db()
        with closing(sqlite3.connect(path)) as conn, conn:
            for index in range(40):
                conn.execute("INSERT INTO tasks(id,title,status,created_at,completed_at) VALUES(?,?,?,?,?)",
                             (f"d{index}", f"Done {index}", "done", index, index + 1000))
        with kanban.BoardStore(path) as store:
            cards = store.tasks(done_limit=3)
            statuses = {c["status"] for c in cards}
            self.assertTrue(set(kanban.STATUSES) <= statuses)
            self.assertNotIn("archived", statuses)
            self.assertEqual([c["id"] for c in cards if c["status"] == "done"], ["d39", "d38", "d37"])
            self.assertIn("archived", {c["status"] for c in store.tasks(show_archived=True)})
            for limit in (0, -1, 501, "3"):
                with self.assertRaises(kanban.BoardError):
                    store.tasks(done_limit=limit)

    def test_detail_all_fields_and_relationship_direction(self):
        with kanban.BoardStore(self.db()) as store:
            detail = store.detail("todo-id")
            self.assertEqual(len(detail["comments"]), 2)
            self.assertEqual({r["id"] for r in detail["parents"]}, {"blocked-id", "done-id", "missing-id"})
            self.assertEqual([r["id"] for r in detail["dependencies"]], ["ready-id"])
            self.assertEqual(detail["last_run"]["id"], 2)
            lines = "\n".join(kanban.detail_lines(detail))
            for expected in ("Body:", "Second paragraph", "Result:", "Useful result", "BLOCKER", "satisfied",
                             "Last failure", "Last run:", "latest summary", "Comments:", "comment prose", "[REDACTED]"):
                self.assertIn(expected, lines)
            self.assertIn("no longer available", kanban.detail_lines(None)[0])

    def test_refresh_reads_external_updates_and_selection_retention(self):
        path = self.db()
        with kanban.BoardStore(path) as store:
            nav = kanban.Navigation(kanban.grouped(store.tasks()))
            nav.move(2)
            self.assertEqual(nav.selected_id, "ready-id")
            nav.detail_open, nav.detail_top = True, 5
            with closing(sqlite3.connect(path)) as writer, writer:
                writer.execute("UPDATE tasks SET title='Updated title',status='review' WHERE id='ready-id'")
                writer.execute("INSERT INTO task_comments VALUES(3,'ready-id','reviewer','fresh comment',99)")
            nav.replace(kanban.grouped(store.tasks()))
            self.assertEqual(nav.selected_id, "ready-id")
            self.assertEqual(nav.detail_top, 5)
            self.assertTrue(nav.detail_open)
            self.assertEqual(store.detail(nav.selected_id)["task"]["title"], "Updated title")
            self.assertEqual(store.detail(nav.selected_id)["comments"][0]["body"], "fresh comment")
            with closing(sqlite3.connect(path)) as writer, writer:
                writer.execute("DELETE FROM tasks WHERE id='ready-id'")
            nav.replace(kanban.grouped(store.tasks()))
            self.assertNotEqual(nav.selected_id, "ready-id")
            self.assertIsNotNone(nav.selected_id)
            nav.replace([])
            nav.move(1)
            nav.group(-1)
            self.assertIsNone(nav.selected_id)


class SafetyTests(unittest.TestCase):
    def test_controls_osc_dcs_bidi_and_incomplete_sequences(self):
        hostile = ("ordinary\x1b[31m red\x1b[0m\x1b]52;c;clipboard-secret\x07"
                   "\x1bPprivate-payload\x1b\\\x9dhidden\x9c\x9b32m text"
                   "\x00\x08\x7f\x85\u202e\u2066\u200b\ud800\tend\r\nnext")
        result = sanitize(hostile)
        self.assertEqual(result, "ordinary red text end \nnext")
        self.assertNotIn("payload", result)
        for char in result:
            self.assertTrue(char == "\n" or unicodedata.category(char) not in {"Cc", "Cf", "Cs"})
        self.assertEqual(sanitize("before\x1b]52;unterminated"), "before")
        self.assertEqual(sanitize("before\x90unfinished dcs"), "before")
        self.assertEqual(sanitize(None), "")

    def test_credentials_environment_assignments_and_process_fields(self):
        secrets = (
            "export HOME=/sensitive/home", "export lower=/sensitive/home", "env lower=hidden-value",
            "TOKEN='multi\nline-secret'", 'API_KEY="quoted secret"',
            "password: hidden-pass", "api_key=hidden-key", '"access_token": "json-secret"',
            "Authorization: Bearer bearer-secret", "Bearer abcdefghijk", "Basic dXNlcjpwYXNz",
            "https://alice:uri-password@example.com/path", "sk-abcdefghijklmno", "ghp_abcdefghijklmno",
            "github_pat_abcdefghijklmno", "AKIA1234567890123456", "eyJabc.def.ghi",
            "-----BEGIN RSA PRIVATE KEY-----\nkey-material\n-----END RSA PRIVATE KEY-----",
            "cmdline: python service --token raw-command", "process environment: NEVER_LEAK=hidden",
            "--token cli-secret", "--api-key cli-key", "--password='flag secret'",
        )
        for text in secrets:
            with self.subTest(text=text):
                result = sanitize(text)
                self.assertIn("REDACTED", result)
                self.assertNotEqual(text, result)
        ordinary = "Build a weather forecast. Alice said: use the ready column.\nratio = 2, a=b; café 雨."
        self.assertEqual(sanitize(ordinary), ordinary)
        self.assertNotIn("hidden-key", sanitize("api\x00_key=hidden-key"))
        for value in ("json-secret", "cli-secret", "cli-key", "flag secret", "bearer-secret", "uri-password"):
            self.assertNotIn(value, sanitize("\n".join(secrets)))

    def test_cell_clipping_and_wrapping(self):
        self.assertEqual(kanban._clip("雨abc", 3), "雨a")
        self.assertEqual(kanban._clip("e\u0301clair", 1), "e\u0301")
        self.assertEqual("".join(kanban._wrap(["雨雨abcd"], 2)), "雨雨abcd")
        self.assertNotIn("\x1b", "".join(kanban._wrap(["\x1b[31mtext"], 5)))

    def test_all_detail_fields_pass_through_sanitizer(self):
        dirty = "public\x1b]52;c;clipboard-secret\x07\u202e TOKEN=field-secret"
        detail = {
            "task": {name: dirty for name in kanban.ALLOWED["tasks"]},
            "parents": [{"id": dirty, "title": dirty, "status": dirty}],
            "dependencies": [{"id": dirty, "title": dirty, "status": dirty}],
            "comments": [{"created_at": dirty, "author": dirty, "body": dirty}],
            "last_run": {name: dirty for name in kanban.ALLOWED["task_runs"]},
            "unavailable": [],
        }
        rendered = "\n".join(kanban.detail_lines(detail))
        for forbidden in ("\x1b", "\u202e", "clipboard-secret", "field-secret"):
            self.assertNotIn(forbidden, rendered)
        self.assertIn("public", rendered)


class NavigationTests(unittest.TestCase):
    def cards(self):
        return [{"id": str(i), "title": "Card " + str(i), "status": "todo" if i < 30 else "ready", "assignee": None}
                for i in range(40)]

    def test_scroll_groups_keys_and_resize_retention(self):
        nav = kanban.Navigation(self.cards())
        nav.move(25)
        visible = nav.visible(5)
        self.assertIn(nav.selected_id, [row[0] for row in visible])
        nav.visible(1)
        self.assertEqual(nav.visible(1)[0][0], "25")
        nav.visible(100)
        self.assertEqual(nav.selected_id, "25")
        nav.group(1)
        self.assertEqual(nav.selected_id, "30")
        nav.group(-1)
        self.assertEqual(nav.selected_id, "29")
        nav.move(1000)
        self.assertEqual(nav.selected_id, "39")
        nav.move(-1000)
        self.assertEqual(nav.selected_id, "0")
        self.assertTrue(all(any(row[1].startswith(status + " (") for row in nav.rows()) for status in kanban.STATUSES))


class CLITests(FixtureCase):
    def test_errors_are_sanitized_no_database_creation(self):
        for args, expected in (
            ([], "does not exist"), (["--project", "bad name"], "Invalid board"),
            (["--project", "studio", "--board", "research"], "mismatch"),
            (["--board", self.home / "missing.db"], "does not exist"),
            (["--board", "default", "--done-limit", "0"], "done-limit"),
            (["--unexpected\x1b[31m"], "unrecognized arguments"),
            (["--done-limit", "SECRET=never-echo\x1b[31m"], "invalid int"),
        ):
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn(expected, result.stderr)
                self.assertNotIn("\x1b", result.stderr)
                self.assertNotIn("never-echo", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_nonterminal_and_wrong_database_are_clear(self):
        result = self.cli("--board", self.db())
        self.assertEqual(result.returncode, 2)
        self.assertIn("interactive terminal", result.stderr)
        path = self.home / "wrong.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE unrelated(x)")
        result = self.cli("--board", path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("schema", result.stderr)

    def test_symlink_wrapper_outside_checkout_and_help(self):
        link1, link2 = self.home / "link1", self.home / "herdr-kanban"
        link1.symlink_to(WRAPPER)
        link2.symlink_to(link1)
        self.assertTrue(WRAPPER.stat().st_mode & 0o111)
        result = self.cli("--help", wrapper=link2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--selftest", result.stdout)
        result = self.cli("--board", self.db(), wrapper=link2)
        self.assertEqual(result.returncode, 2)
        self.assertIn("interactive terminal", result.stderr)

    def test_import_has_no_curses_or_io_side_effects(self):
        result = subprocess.run(
            [sys.executable, "-B", "-c", "import sys; sys.path.insert(0,sys.argv[1]); "
             "import herdr_workstation.kanban; assert 'curses' not in sys.modules", str(ROOT)],
            env=CHILD_ENV, cwd=self.home, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_selftest_loads_only_repository_tests(self):
        suite = unittest.TestSuite()
        runner = mock.Mock()
        runner.run.return_value.wasSuccessful.return_value = True
        with mock.patch("unittest.defaultTestLoader.discover", return_value=suite) as load, \
                mock.patch("unittest.TextTestRunner", return_value=runner):
            self.assertEqual(kanban.main(["--selftest"]), 0)
        load.assert_called_once_with(str(ROOT / "tests"))
        runner.run.assert_called_once_with(suite)


if __name__ == "__main__":
    unittest.main()
