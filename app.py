from flask import Flask, render_template, request, session, redirect, url_for, jsonify
import sqlite3
import argparse

# flags
parser = argparse.ArgumentParser()

parser.add_argument("-p", "--port", type=int, help="port number")

args = parser.parse_args()
port_number = args.port;

if port_number == None:
    port_number = 5000

app = Flask(__name__)
app.secret_key = 'hex_grid_secret_key'
DATABASE = 'voting_game.db'
TOTAL_TICKETS = 100

# 10 Friends Account Setup (Username: Secret Keyword)
USER_REGISTRY = {
    "alice": "star55",
    "bob": "matrix99",
    "charlie": "dragon88",
    "david": "quest11",
    "emma": "phoenix",
    "frank": "cyber22",
    "grace": "shadow7",
    "henry": "wizard3",
    "ivy": "crypto4",
    "jack": "gamer00"
}

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA busy_timeout = 5000;')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        # Table columns track who voted, which cell, and how many tickets are assigned
        conn.execute('''
            CREATE TABLE IF NOT EXISTS grid_votes (
                username TEXT,
                cell_id TEXT,
                tickets_spent INTEGER DEFAULT 0,
                PRIMARY KEY (username, cell_id)
            )
        ''')
        conn.commit()

def get_user_remaining_tickets(username):
    with get_db() as conn:
        row = conn.execute('SELECT SUM(tickets_spent) as total_spent FROM grid_votes WHERE username = ?', (username,)).fetchone()
        spent = row['total_spent'] if row['total_spent'] else 0
        return max(0, TOTAL_TICKETS - spent)

@app.route('/')
def index():
    if 'username' not in session:
        return render_template('index.html', logged_in=False, error=None)
    
    username = session['username']
    remaining = get_user_remaining_tickets(username)
    
    # Gather global results to display totals inside each hex grid cell
    with get_db() as conn:
        rows = conn.execute('SELECT cell_id, SUM(tickets_spent) as total FROM grid_votes GROUP BY cell_id').fetchall()
        global_votes = {row['cell_id']: row['total'] for row in rows}
        
        # Gather current user's allocation
        user_rows = conn.execute('SELECT cell_id, tickets_spent FROM grid_votes WHERE username = ?', (username,)).fetchall()
        user_votes = {row['cell_id']: row['tickets_spent'] for row in user_rows}

    return render_template('index.html', logged_in=True, username=username, remaining=remaining, global_votes=global_votes, user_votes=user_votes)

@app.route('/login', methods=['POST'])
def login():
    username = request.form.get('username', '').strip().lower()
    secret = request.form.get('secret', '').strip()
    
    if username in USER_REGISTRY and USER_REGISTRY[username] == secret:
        session['username'] = username
        return redirect(url_for('index'))
    else:
        return render_template('index.html', logged_in=False, error="Invalid Username or Secret Keyword.")

@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('index'))

@app.route('/api/vote', methods=['POST'])
def api_vote():
    if 'username' not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    
    username = session['username']
    data = request.json
    cell_id = data.get('cell_id')
    action = data.get('action') # 'add' or 'clear'
    
    remaining = get_user_remaining_tickets(username)
    
    with get_db() as conn:
        if action == 'add':
            if remaining <= 0:
                return jsonify({"success": False, "error": "No tickets left!"}), 400
            
            conn.execute('''
                INSERT INTO grid_votes (username, cell_id, tickets_spent) 
                VALUES (?, ?, 1)
                ON CONFLICT(username, cell_id) DO UPDATE SET tickets_spent = tickets_spent + 1
            ''', (username, cell_id))
        elif action == 'clear':
            conn.execute('DELETE FROM grid_votes WHERE username = ? AND cell_id = ?', (username, cell_id))
            
        conn.commit()
        
    # Recalculate states to pass back to the frontend dynamically
    new_remaining = get_user_remaining_tickets(username)
    with get_db() as conn:
        g_row = conn.execute('SELECT SUM(tickets_spent) as total FROM grid_votes WHERE cell_id = ?', (cell_id,)).fetchone()
        u_row = conn.execute('SELECT tickets_spent FROM grid_votes WHERE username = ? AND cell_id = ?', (username, cell_id)).fetchone()
        
    return jsonify({
        "success": True, 
        "remaining": new_remaining,
        "cell_total": g_row['total'] if g_row['total'] else 0,
        "cell_user": u_row['tickets_spent'] if u_row else 0
    })

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=port_number, debug=True)
