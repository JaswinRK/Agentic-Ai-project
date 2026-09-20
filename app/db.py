"""app/db.py — MySQL connection and transaction helpers.
Both databases live on the same server; the database name picks which one.
Connection settings come from environment variables with local-dev defaults.
"""
import os
from contextlib import contextmanager

import mysql.connector
from mysql.connector import errorcode
from mysql.connector.constants import ClientFlag

def _config(database: str) -> dict:
    return {
        "host":       os.getenv("MYSQL_HOST", "localhost"),
        "port":       int(os.getenv("MYSQL_PORT", "3306")),
        "user":       os.getenv("MYSQL_USER", "support"),
        "password":   os.getenv("MYSQL_PASSWORD", "support"),
        "database":   database,
        "autocommit": False,
        "charset":    "utf8mb4",
        "use_unicode": True,
        "client_flags": [ClientFlag.FOUND_ROWS],
    }


def connect(database: str):
    """Open a connection to one of the two databases: 'support' or 'agent'."""
    try:
        return mysql.connector.connect(**_config(database))
    except mysql.connector.Error as e:
        if e.errno == errorcode.ER_BAD_DB_ERROR:
            raise RuntimeError(
                f"Database {database!r} does not exist. "
                f"Run: mysql -u support -psupport {database} < schema/*.sql"
            ) from e
        raise


@contextmanager
def transaction(conn):
    """A dict cursor inside a transaction. Commits on success, rolls back on error."""
    cur = conn.cursor(dictionary=True)
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


@contextmanager
def cursor(conn):
    """Read-only dict cursor. No commit needed."""
    cur = conn.cursor(dictionary=True)
    try:
        yield cur
    finally:
        cur.close()