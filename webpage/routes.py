from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify
from config import USER_REGISTRY, ADMIN_USERNAME, ADMIN_PASSWORD, ADMIN_COLOR, TOTAL_ALLOWANCE
from db import (
    get_admin_snapshot,
    save_admin_snapshot,
    get_db,
    get_user_remaining_ballots,
    is_reveal_mode,
    is_final_stage_active,
    get_placement_order,
    get_current_placement_user,
    get_cluster_placements,
    get_used_shape_ids,
    get_user_used_shape_ids,
    get_cluster_scores,
    start_final_stage,
    advance_placement_turn,
    save_cluster_placement,
    add_cluster_score,
    fetch_ballot_votes,
)
from placement_order import order_users_by_variance, placement_cells, SHAPE_CATALOG

main_bp = Blueprint('main', __name__)


@main_bp.route('/')
def index():
    if 'username' not in session:
        return render_template('login.html', logged_in=False, error=None)

    username = session['username']
    reveal = is_reveal_mode()
    final_stage = is_final_stage_active()
    all_votes_data = {}
    cluster_placements = get_cluster_placements()
    cluster_scores = get_cluster_scores()
    cluster_cell_owner = {}
    cluster_cell_shape = {}
    cluster_cell_color = {}
    for placement in cluster_placements:
        for cell in placement['cells']:
            cluster_cell_owner[cell] = placement['username']
            cluster_cell_shape[cell] = placement['shape_id']
            cluster_cell_color[cell] = '#ffffff'

    if username == ADMIN_USERNAME:
        all_votes_data = get_admin_snapshot()
        placement_order = get_placement_order()
        return render_template(
            'admin.html',
            logged_in=True,
            username=ADMIN_USERNAME,
            user_color=ADMIN_COLOR,
            reveal_mode=reveal,
            final_stage=final_stage,
            all_votes_data=all_votes_data,
            placement_order=placement_order,
            cluster_placements=cluster_placements,
            cluster_scores=cluster_scores,
            cluster_cell_color=cluster_cell_color,
            shape_catalog=SHAPE_CATALOG,
        )

    remaining = get_user_remaining_ballots(username)
    with get_db() as conn:
        user_rows = conn.execute('SELECT cell_id, ballots_spent FROM grid_votes WHERE username = ?', (username,)).fetchall()
        user_votes = {row['cell_id']: row['ballots_spent'] for row in user_rows}

    current_turn = None
    used_shapes = []
    if final_stage:
        current_turn = get_current_placement_user()
        used_shapes = get_user_used_shape_ids(username)

    return render_template(
        'user.html',
        logged_in=True,
        username=username,
        remaining=remaining,
        user_votes=user_votes,
        user_color=USER_REGISTRY[username]['color'],
        reveal_mode=reveal,
        final_stage=final_stage,
        current_turn=current_turn,
        used_shapes=used_shapes,
        cluster_placements=cluster_placements,
        cluster_scores=cluster_scores,
        cluster_cell_owner=cluster_cell_owner,
        cluster_cell_shape=cluster_cell_shape,
        cluster_cell_color=cluster_cell_color,
        all_votes_data=all_votes_data,
        shape_catalog=SHAPE_CATALOG,
    )


@main_bp.route('/login', methods=['POST'])
def login():
    username = request.form.get('username', '').strip().lower()
    secret = request.form.get('secret', '').strip()

    if username == ADMIN_USERNAME and secret == ADMIN_PASSWORD:
        session['username'] = ADMIN_USERNAME
        return redirect(url_for('main.index'))

    if username in USER_REGISTRY and USER_REGISTRY[username]['secret'] == secret:
        session['username'] = username
        return redirect(url_for('main.index'))

    return render_template('login.html', logged_in=False, error='Invalid Username or Secret Keyword.')


@main_bp.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('main.index'))


@main_bp.route('/api/vote', methods=['POST'])
def api_vote():
    if 'username' not in session or session['username'] == ADMIN_USERNAME:
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
    if session.get('username') != ADMIN_USERNAME:
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


@main_bp.route('/api/admin/start_final_stage', methods=['POST'])
def admin_start_final_stage():
    if session.get('username') != ADMIN_USERNAME:
        return jsonify({'success': False}), 403

    ballot_rows = fetch_ballot_votes()
    ballots_by_user = {username: {} for username in USER_REGISTRY}
    for row in ballot_rows:
        ballots_by_user.setdefault(row['username'], {})[row['cell_id']] = row['ballots_spent']

    placement_order = order_users_by_variance(ballots_by_user)
    start_final_stage(placement_order)

    # Refresh admin snapshot to reflect collapsed ballots in the final phase.
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

    return jsonify({'success': True, 'placement_order': placement_order})


@main_bp.route('/api/place_cluster', methods=['POST'])
def api_place_cluster():
    if 'username' not in session or session['username'] == ADMIN_USERNAME:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    if not is_final_stage_active():
        return jsonify({'success': False, 'error': 'Final stage is not active'}), 400

    username = session['username']
    current_user = get_current_placement_user()
    if username != current_user:
        return jsonify({'success': False, 'error': 'Not your turn'}), 400

    data = request.json or {}
    shape_id = data.get('shape_id')
    anchor = data.get('anchor')
    orientation = int(data.get('orientation', 0))

    used_shapes = get_user_used_shape_ids(username)
    if shape_id in used_shapes:
        return jsonify({'success': False, 'error': 'Shape already used'}), 400

    cells = placement_cells(shape_id, anchor, orientation)
    if len(cells) != 5:
        return jsonify({'success': False, 'error': 'Invalid placement'}), 400

    existing = get_cluster_placements()
    occupied = {cell for placement in existing for cell in placement['cells']}
    if any(cell in occupied for cell in cells):
        return jsonify({'success': False, 'error': 'Placement overlaps existing cluster'}), 400

    with get_db() as conn:
        placeholders = ','.join('?' for _ in cells)
        rows = conn.execute(f'SELECT username, SUM(ballots_spent) AS ballots FROM grid_votes WHERE cell_id IN ({placeholders}) GROUP BY username', tuple(cells)).fetchall()
        ballots_per_user = {row['username']: row['ballots'] for row in rows}
        total_ballots = sum(ballots_per_user.values())

    if ballots_per_user:
        max_ballots = max(ballots_per_user.values())
        winners = [user for user, ballots in ballots_per_user.items() if ballots == max_ballots]
    else:
        winners = []
        max_ballots = 0

    winner = winners[0] if len(winners) == 1 and max_ballots > 0 else None
    if winner:
        add_cluster_score(winner, total_ballots)

    save_cluster_placement(username, shape_id, anchor, orientation, cells, winner, total_ballots if winner else 0)
    next_user = advance_placement_turn()

    return jsonify({
        'success': True,
        'next_user': next_user,
        'winner': winner,
        'cluster_cells': cells,
        'total_ballots': total_ballots,
    })


@main_bp.route('/api/admin/grant_ballots', methods=['POST'])
def admin_grant_ballots():
    if session.get('username') != ADMIN_USERNAME:
        return jsonify({'success': False}), 403

    with get_db() as conn:
        for user in USER_REGISTRY:
            row = conn.execute('SELECT SUM(ballots_spent) as total_spent FROM grid_votes WHERE username = ?', (user,)).fetchone()
            spent = row['total_spent'] if row and row['total_spent'] else 0
            conn.execute('UPDATE user_allowances SET total_allowance = ? WHERE username = ?', (TOTAL_ALLOWANCE + spent, user))
        conn.commit()

    return jsonify({'success': True})
