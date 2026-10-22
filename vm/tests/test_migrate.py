"""vm/migrate.py: file validation and the pending list (pure)."""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("migrate", Path(__file__).resolve().parents[1] / "migrate.py")
migrate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrate)

FILES = ["002_b.sql", "001_base.sql", "003_c.sql"]


class PendingTest(unittest.TestCase):
    def test_all_pending_on_an_empty_table_in_order(self):
        self.assertEqual(migrate.pending(FILES, []), ["001_base.sql", "002_b.sql", "003_c.sql"])

    def test_applied_are_skipped(self):
        self.assertEqual(migrate.pending(FILES, ["001_base", "002_b"]), ["003_c.sql"])

    def test_nothing_pending_is_a_no_op(self):
        self.assertEqual(migrate.pending(FILES, ["001_base", "002_b", "003_c"]), [])

    def test_a_gap_below_the_newest_applied_is_an_error(self):
        with self.assertRaises(ValueError) as e:
            migrate.pending(FILES, ["001_base", "003_c"])
        self.assertIn("002_b.sql", str(e.exception))

    def test_unknown_applied_versions_are_ignored(self):
        self.assertEqual(migrate.pending(FILES, ["001_base", "000_gone"]), ["002_b.sql", "003_c.sql"])

    def test_bad_name_and_duplicate_number(self):
        for bad in (["1_x.sql"], ["001_X.sql"], ["001-x.sql"], ["001_x.txt"], ["001_a.sql", "001_b.sql"]):
            with self.assertRaises(ValueError, msg=bad):
                migrate.validate(bad)

    def test_the_repo_migrations_are_valid(self):
        folder = Path(__file__).resolve().parents[2] / "db" / "appdata" / "migrations"
        names = migrate.validate([p.name for p in folder.glob("*.sql")])
        self.assertEqual(names[0], "001_base.sql")
        for n in names:
            text = (folder / n).read_text()
            self.assertNotIn("CONCURRENTLY", text.upper(), n + ": not allowed inside a transaction")
            self.assertNotIn("\\c ", text, n + ": no \\c in a single transaction")
            self.assertNotIn("\\getenv", text, n + ": passwords belong in bootstrap.sql")


if __name__ == "__main__":
    unittest.main()
