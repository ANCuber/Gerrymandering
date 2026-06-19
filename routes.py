from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify
from config import USER_REGISTRY, ADMIN_PASSWORD, TOTAL_ALLOWANCE
from db import (
    get_admin_snapshot,
    get_db,
    get_user_remaining_ballots,
    is_reveal_mode,
    save_admin_snapshot,
    fetch_ballot_votes,
)

main_bp = Blueprint('main', __name__)


@main_bp.route('/')
def index():
    if 'username' not in session:
        return render_template('index.html', logged_in=False, error=None)

    username = session['username']
    reveal = is_reveal_mode()
    all_votes_data = {}

    if username == 'admin':
        all_votes_data = get_admin_snapshot()
        return render_template(
            'index.html',
            logged_in=True,
            username='admin',
            user_color='#29292e',
            reveal_mode=reveal,
            all_votes_data=all_votes_data,
        )

    if reveal:
        rows = fetch_ballot_votes()
        for row in rows:
            c_id = row['cell_id']
            if c_id not in all_votes_data:
                all_votes_data[c_id] = []
            all_votes_data[c_id].append({
                'username': row['username'],
                'ballots': row['ballots_spent'],
                'color': USER_REGISTRY.get(row['username'], {}).get('color', '#fff'),
            })

    remaining = get_user_remaining_ballots(username)
    with get_db() as conn:
        user_rows = conn.execute('SELECT cell_id, ballots_spent FROM grid_votes WHERE username = ?', (username,)).fetchall()
        user_votes = {row['cell_id']: row['ballots_spent'] for row in user_rows}

    return render_template(
        'index.html',
        logged_in=True,
        username=username,
        remaining=remaining,
        user_votes=user_votes,
        user_color=USER_REGISTRY[username]['color'],
        reveal_mode=reveal,
        all_votes_data=all_votes_data,
    )


@main_bp.route('/login', methods=['POST'])
def login():
    username = request.form.get('username', '').strip().lower()
    secret = request.form.get('secret', '').strip()

    if username == 'admin' and secret == ADMIN_PASSWORD:
        session['username'] = 'admin'
        return redirect(url_for('main.index'))

    if username in USER_REGISTRY and USER_REGISTRY[username]['secret'] == secret:
        session['username'] = username
        return redirect(url_for('main.index'))

    return render_template('index.html', logged_in=False, error='Invalid Username or Secret Keyword.')


@main_bp.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('main.index'))


@main_bp.route('/api/vote', methods=['POST'])
def api_vote():
    if 'username' not in session or session['username'] == 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    username = session['username']
    data = request.json
    cell_id = data.get('cell_id')
    action = data.get('action')

    remaining = get_user_remaining_ballots(username)

    with get_db() as conn:
        if action == 'add':
            if remaining <= 0:
                return jsonify({'success': False, 'error': 'No ballots left!'}), 400

            conn.execute(
                '''
                INSERT INTO grid_votes (username, cell_id, ballots_spent)
                VALUES (?, ?, 1)
                ON CONFLICT(username, cell_id) DO UPDATE SET ballots_spent = ballots_spent + 1
                ''',
                (username, cell_id),
            )
        elif action == 'clear':
            conn.execute('DELETE FROM grid_votes WHERE username = ? AND cell_id = ?', (username, cell_id))

        conn.commit()
        u_row = conn.execute('SELECT ballots_spent FROM grid_votes WHERE username = ? AND cell_id = ?', (username, cell_id)).fetchone()

    new_remaining = get_user_remaining_ballots(username)

    return jsonify({
        'success': True,
        'remaining': new_remaining,
        'cell_user': u_row['ballots_spent'] if u_row else 0,
    })


@main_bp.route('/api/admin/update_results', methods=['POST'])
def admin_update_results():
    if session.get('username') != 'admin':
        return jsonify({'success': False}), 403

    snapshot = {}
    rows = fetch_ballot_votes()
    for row in rows:
        c_id = row['cell_id']
        if c_id not in snapshot:
            snapshot[c_id] = []
        snapshot[c_id].append({
            'username': row['username'],
            'ballots': row['ballots_spent'],
            'color': USER_REGISTRY.get(row['username'], {}).get('color', '#fff'),
        })

    save_admin_snapshot(snapshot)
    return jsonify({'success': True})


@main_bp.route('/api/admin/grant_ballots', methods=['POST'])
def admin_grant_ballots():
    if session.get('username') != 'admin':
        return jsonify({'success': False}), 403

    with get_db() as conn:
        for user in USER_REGISTRY:
            row = conn.execute('SELECT SUM(ballots_spent) as total_spent FROM grid_votes WHERE username = ?', (user,)).fetchone()
            spent = row['total_spent'] if row and row['total_spent'] else 0
            conn.execute('UPDATE user_allowances SET total_allowance = ? WHERE username = ?', (TOTAL_ALLOWANCE + spent, user))
        conn.commit()

    return jsonify({'success': True})
