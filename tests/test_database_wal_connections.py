"""Keep WAL on app connections without reissuing a journal mode transition."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import database


class DatabaseWalConnectionTests(unittest.TestCase):
    def test_wal_is_not_reassigned_but_non_wal_database_is_migrated(self):
        with tempfile.TemporaryDirectory(prefix='dhb-wal-connection-') as folder:
            path = Path(folder) / 'connection.sqlite'
            original_connect = sqlite3.connect
            statements = []

            class TrackingConnection(sqlite3.Connection):
                def execute(self, sql, parameters=()):
                    statements.append(' '.join(sql.split()).casefold())
                    return super().execute(sql, parameters)

            def connect(*args, **kwargs):
                kwargs['factory'] = TrackingConnection
                return original_connect(*args, **kwargs)

            with mock.patch.object(database, 'APP_DB_PATH', path), \
                    mock.patch.object(database.sqlite3, 'connect', side_effect=connect):
                seeded = original_connect(str(path))
                self.assertEqual(seeded.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
                seeded.close()

                conn = database.get_conn()
                self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
                conn.close()
                self.assertIn('pragma journal_mode;', statements)
                self.assertNotIn('pragma journal_mode=wal;', statements)
                self.assertIn('pragma synchronous=normal;', statements)

                reset = original_connect(str(path))
                self.assertEqual(reset.execute('PRAGMA journal_mode=DELETE').fetchone()[0], 'delete')
                reset.close()
                statements.clear()

                conn = database.get_conn()
                self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
                conn.close()
                self.assertIn('pragma journal_mode=wal;', statements)


if __name__ == '__main__':
    unittest.main()
