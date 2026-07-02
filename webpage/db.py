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
        conn.execute('''
            CREATE TABLE IF NOT EXISTS cluster_placements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                shape_id TEXT,
                anchor_cell_id TEXT,
                orientation INTEGER,
                cells TEXT,
                winner TEXT,
                cluster_score INTEGER DEFAULT 0,
                UNIQUE(username, shape_id)
            )
        ''')
        rows = conn.execute("PRAGMA table_info(cluster_placements)").fetchall()
        if rows and not any(col['name'] == 'id' for col in rows):
            conn.execute('ALTER TABLE cluster_placements RENAME TO cluster_placements_old')
            conn.execute('''
                CREATE TABLE cluster_placements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT,
                    shape_id TEXT,
                    anchor_cell_id TEXT,
                    orientation INTEGER,
                    cells TEXT,
                    winner TEXT,
                    cluster_score INTEGER DEFAULT 0,
                    UNIQUE(username, shape_id)
                )
            ''')
            conn.execute('''
                INSERT INTO cluster_placements (username, shape_id, anchor_cell_id, orientation, cells, winner, cluster_score)
                SELECT username, shape_id, anchor_cell_id, orientation, cells, winner, cluster_score
                FROM cluster_placements_old
            ''')
            conn.execute('DROP TABLE cluster_placements_old')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS cluster_scores (
                username TEXT PRIMARY KEY,
                score INTEGER DEFAULT 0
            )
        ''')
        conn.execute("INSERT OR IGNORE INTO system_config (key, value) VALUES ('reveal_mode', 'false')")
        conn.execute("INSERT OR IGNORE INTO system_config (key, value) VALUES ('final_stage', 'false')")

        for user in USER_REGISTRY:
            conn.execute(
                "INSERT OR IGNORE INTO user_allowances (username, total_allowance) VALUES (?, ?)",
                (user, TOTAL_ALLOWANCE)
            )
            conn.execute(
                "INSERT OR IGNORE INTO cluster_scores (username, score) VALUES (?, 0)",
                (user,)
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


def get_config_value(key, default=None):
    with get_db() as conn:
        row = conn.execute('SELECT value FROM system_config WHERE key = ?', (key,)).fetchone()
        return row['value'] if row else default


def set_config_value(key, value):
    with get_db() as conn:
        conn.execute('INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)', (key, value))
        conn.commit()


def is_reveal_mode():
    value = get_config_value('reveal_mode', 'false')
    return str(value).lower() in ('true', '1', 'yes')


def is_final_stage_active():
    value = get_config_value('final_stage', 'false')
    return str(value).lower() in ('true', '1', 'yes')


def get_placement_order():
    raw = get_config_value('placement_order', '[]')
    try:
        return json.loads(raw)
    except Exception:
        return []


def get_placement_index():
    raw = get_config_value('placement_index', '0')
    try:
        return int(raw)
    except Exception:
        return 0


def get_current_placement_user():
    order = get_placement_order()
    index = get_placement_index()
    if not order or index >= len(order):
        return None
    return order[index]


def collapse_ballots_to_majority():
    with get_db() as conn:
        rows = conn.execute('SELECT cell_id, username, ballots_spent FROM grid_votes WHERE ballots_spent > 0 ORDER BY cell_id').fetchall()
        cell_groups = {}
        for row in rows:
            cell_groups.setdefault(row['cell_id'], []).append((row['username'], row['ballots_spent']))

        for cell_id, votes in cell_groups.items():
            totals = {username: ballots for username, ballots in votes}
            max_ballots = max(totals.values())
            winners = [username for username, ballots in totals.items() if ballots == max_ballots]
            if len(winners) == 1:
                winner = winners[0]
                total_ballots = sum(totals.values())
                conn.execute('DELETE FROM grid_votes WHERE cell_id = ?', (cell_id,))
                conn.execute(
                    'INSERT INTO grid_votes (username, cell_id, ballots_spent) VALUES (?, ?, ?)',
                    (winner, cell_id, total_ballots),
                )
            else:
                conn.execute('DELETE FROM grid_votes WHERE cell_id = ?', (cell_id,))
        conn.commit()


def start_final_stage(order_list):
    collapse_ballots_to_majority()
    with get_db() as conn:
        conn.execute('DELETE FROM cluster_placements')
        conn.execute('UPDATE cluster_scores SET score = 0')
        for user in USER_REGISTRY:
            conn.execute('INSERT OR IGNORE INTO cluster_scores (username, score) VALUES (?, ?)', (user, 0))
        conn.execute('INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)', ('final_stage', 'true'))
        conn.execute('INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)', ('placement_order', json.dumps(order_list)))
        conn.execute('INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)', ('placement_index', '0'))
        conn.commit()


def advance_placement_turn():
    order = get_placement_order()
    if not order:
        return None
    index = get_placement_index()
    next_index = (index + 1) % len(order)
    set_config_value('placement_index', str(next_index))
    return order[next_index]


def cancel_final_stage():
    with get_db() as conn:
        conn.execute('DELETE FROM cluster_placements')
        conn.execute('UPDATE cluster_scores SET score = 0')
        conn.execute('INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)', ('final_stage', 'false'))
        conn.execute('INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)', ('placement_order', '[]'))
        conn.execute('INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)', ('placement_index', '0'))
        conn.commit()


def get_cluster_placements():
    with get_db() as conn:
        rows = conn.execute('SELECT username, shape_id, anchor_cell_id, orientation, cells, winner, cluster_score FROM cluster_placements').fetchall()
        result = []
        for row in rows:
            result.append({
                'username': row['username'],
                'shape_id': row['shape_id'],
                'anchor_cell_id': row['anchor_cell_id'],
                'orientation': row['orientation'],
                'cells': json.loads(row['cells']),
                'winner': row['winner'],
                'cluster_score': row['cluster_score'],
            })
        return result


def get_used_shape_ids():
    with get_db() as conn:
        rows = conn.execute('SELECT shape_id FROM cluster_placements').fetchall()
        return [row['shape_id'] for row in rows]


def get_user_used_shape_ids(username):
    with get_db() as conn:
        rows = conn.execute('SELECT shape_id FROM cluster_placements WHERE username = ?', (username,)).fetchall()
        return [row['shape_id'] for row in rows]


def save_cluster_placement(username, shape_id, anchor_cell_id, orientation, cells, winner, cluster_score):
    with get_db() as conn:
        conn.execute(
            'INSERT INTO cluster_placements (username, shape_id, anchor_cell_id, orientation, cells, winner, cluster_score) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (username, shape_id, anchor_cell_id, orientation, json.dumps(cells), winner, cluster_score),
        )
        conn.commit()


def get_cluster_scores():
    with get_db() as conn:
        rows = conn.execute('SELECT username, score FROM cluster_scores').fetchall()
        return {row['username']: row['score'] for row in rows}


def add_cluster_score(username, score):
    with get_db() as conn:
        conn.execute('INSERT OR IGNORE INTO cluster_scores (username, score) VALUES (?, ?)', (username, 0))
        conn.execute('UPDATE cluster_scores SET score = score + ? WHERE username = ?', (score, username))
        conn.commit()


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
