import json
import sqlite3
from config import DATABASE, USER_REGISTRY, TOTAL_ALLOWANCE


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA busy_timeout = 5000;')
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS grid_votes (
                username TEXT,
                cell_id TEXT,
                ballots_spent INTEGER DEFAULT 0,
                PRIMARY KEY (username, cell_id)
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_allowances (
                username TEXT PRIMARY KEY,
                total_allowance INTEGER DEFAULT 100
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS system_config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS admin_snapshot (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        conn.execute("INSERT OR IGNORE INTO system_config (key, value) VALUES ('reveal_mode', 'false')")

        for user in USER_REGISTRY:
            conn.execute(
                "INSERT OR IGNORE INTO user_allowances (username, total_allowance) VALUES (?, ?)",
                (user, TOTAL_ALLOWANCE)
            )
        conn.commit()


def get_user_allowance(username):
    with get_db() as conn:
        row = conn.execute('SELECT total_allowance FROM user_allowances WHERE username = ?', (username,)).fetchone()
        return row['total_allowance'] if row else TOTAL_ALLOWANCE


def get_user_remaining_ballots(username):
    allowance = get_user_allowance(username)
    with get_db() as conn:
        row = conn.execute('SELECT SUM(ballots_spent) as total_spent FROM grid_votes WHERE username = ?', (username,)).fetchone()
        spent = row['total_spent'] if row['total_spent'] else 0
        return max(0, allowance - spent)


def is_reveal_mode():
    with get_db() as conn:
        row = conn.execute("SELECT value FROM system_config WHERE key = 'reveal_mode'").fetchone()
        if not row:
            return False
        return str(row['value']).lower() in ('true', '1', 'yes')


def get_admin_snapshot():
    with get_db() as conn:
        row = conn.execute("SELECT value FROM admin_snapshot WHERE key = 'votes'").fetchone()
        if not row or not row['value']:
            return {}
        return json.loads(row['value'])


def save_admin_snapshot(snapshot):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO admin_snapshot (key, value) VALUES ('votes', ?)",
            (json.dumps(snapshot),)
        )
        conn.commit()


def fetch_ballot_votes():
    with get_db() as conn:
        return conn.execute('SELECT cell_id, username, ballots_spent FROM grid_votes WHERE ballots_spent > 0').fetchall()
