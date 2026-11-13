"""Small regression checks for restore inventory false positives and schema drift."""

import gzip
import hashlib
import tempfile
import unittest
from pathlib import Path

from check_drill_dump import DumpError, inspect_dump


class DumpInventoryTests(unittest.TestCase):
    def inspect(self, text, database="ventasync_center"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dump.sql.gz"
            path.write_bytes(gzip.compress(text.encode()))
            sql = Path(directory) / "out.sql"
            result = inspect_dump(path, database, sql)
            self.assertEqual(sql.read_bytes(), text.encode())
            self.assertEqual(result["sql_sha256"], hashlib.sha256(text.encode()).hexdigest())
            return result

    def test_realistic_30_tables_and_schema_comments(self):
        text = """-- dump header
CREATE DATABASE /*!32312 IF NOT EXISTS*/ `ventasync_center` /*!40100 DEFAULT CHARACTER SET utf8mb4 */;
USE `ventasync_center`;
"""
        text += "\n".join(f"DROP TABLE IF EXISTS `t{i}`; CREATE TABLE `t{i}` (`id` int);" for i in range(30))
        result = self.inspect(text)
        self.assertEqual(result["table_count"], 30)
        self.assertEqual(result["schema_declarations"], [
            {"kind": "CREATE DATABASE", "database": "ventasync_center", "line": 2},
            {"kind": "USE", "database": "ventasync_center", "line": 3},
        ])

    def test_use_cannot_override_target(self):
        with self.assertRaisesRegex(DumpError, "非预期 schema"):
            self.inspect("USE `ventasync_center`; CREATE TABLE a(id int);", "drilltest")

    def test_comments_and_multiline_data_not_counted(self):
        result = self.inspect("""/* CREATE TABLE fake0 (id int); */
-- CREATE TABLE fake1 (id int);
CREATE TABLE real_table (id int, value text);
INSERT INTO real_table VALUES (1, 'a;\nCREATE TABLE fake2 (id int);\nUSE mysql;');
INSERT INTO real_table VALUES (2, 'escaped \\\' quote; CREATE TABLE fake3(id int);');
""")
        self.assertEqual(result["table_names"], ["real_table"])

    def test_view_standin_is_not_base_table(self):
        result = self.inspect("""CREATE TABLE base_table (id int);
/*!50001 CREATE TABLE `view_a` (`id` int) */;
/*!50001 DROP TABLE IF EXISTS `view_a`*/;
/*!50001 CREATE ALGORITHM=UNDEFINED */
/*!50013 DEFINER=`root`@`%` SQL SECURITY DEFINER */
/*!50001 VIEW `view_a` AS SELECT id FROM base_table */;
""")
        self.assertEqual(result["table_names"], ["base_table"])
        self.assertEqual(result["view_names"], ["view_a"])
        self.assertEqual(result["create_table_count"], 2)

    def test_qualified_cross_schema_and_duplicate_rejected(self):
        for sql in (
            "CREATE TABLE `mysql`.`x` (id int);",
            "CREATE TABLE x(id int); CREATE TABLE x(id int);",
            "USE CONCAT('ventasync_center'); CREATE TABLE x(id int);",
        ):
            with self.subTest(sql=sql), self.assertRaises(DumpError):
                self.inspect(sql)

    def test_custom_delimiter_routine_does_not_create_fake_tables(self):
        result = self.inspect("""CREATE TABLE base_table(id int);
DELIMITER ;;
CREATE DEFINER=`root`@`localhost` PROCEDURE proc()
BEGIN
CREATE TABLE routine_table(id int);
END ;;
DELIMITER ;
""")
        self.assertEqual(result["table_names"], ["base_table"])

    def test_truncated_gzip_rejected_and_partial_sql_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "bad.sql.gz"
            source.write_bytes(gzip.compress(b"CREATE TABLE a(id int);")[:-5])
            with self.assertRaises((EOFError, OSError)):
                inspect_dump(source, "ventasync_center", root / "out.sql")
            self.assertFalse((root / "out.sql").exists())
            self.assertEqual(list(root.glob(".drill-sql-*")), [])

    def test_zero_tables_and_unfinished_statement_rejected(self):
        for sql in ("SELECT 1;", "CREATE TABLE a(id int)"):
            with self.subTest(sql=sql), self.assertRaises(DumpError):
                self.inspect(sql)


if __name__ == "__main__":
    unittest.main()
