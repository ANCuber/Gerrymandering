import json
import queue
import threading

from flask import Blueprint, Response, jsonify, redirect, render_template, request, session, stream_with_context, url_for

from config import ADMIN_COLOR, ADMIN_PASSWORD, ADMIN_USERNAME, MAX_BALLOTS_PER_CELL_PER_USER, TOTAL_ALLOWANCE, USER_REGISTRY, get_active_user_registry
from db import (
	add_cluster_score,
	advance_placement_turn,
	fetch_ballot_votes,
	get_admin_snapshot,
	get_cluster_placements,
	get_cluster_scores,
	get_current_placement_user,
	get_db,
	get_locked_ballots,
	get_placement_order,
	get_user_remaining_ballots,
	get_user_used_shape_ids,
	is_final_stage_active,
	is_reveal_mode,
	save_admin_snapshot,
	save_cluster_placement,
	snapshot_locked_votes,
	start_final_stage,
)
from placement_order import SHAPE_CATALOG, get_blocked_cells, get_row_lengths, is_cell_blocked, order_users_by_variance, placement_cells

main_bp = Blueprint('main', __name__)

_dashboard_listeners = set()
_dashboard_lock = threading.Lock()
_dashboard_revision = 0


def _dashboard_votes_for_user(username):
	if username == ADMIN_USERNAME:
		# Admin sees the frozen snapshot; it is refreshed by the Next Round action.
		return get_admin_snapshot(), {}

	rows = fetch_ballot_votes()
	votes_by_cell = {}
	user_votes = {}
	for row in rows:
		cell_id = row['cell_id']
		votes_by_cell.setdefault(cell_id, []).append({
			'username': row['username'],
			'ballots': row['ballots_spent'],
			'color': USER_REGISTRY.get(row['username'], {}).get('color', '#ffffff'),
		})
		if row['username'] == username:
			user_votes[cell_id] = row['ballots_spent']
	return votes_by_cell, user_votes


def _dashboard_cluster_maps():
	placements = get_cluster_placements()
	cluster_cell_owner = {}
	cluster_cell_shape = {}
	cluster_cell_color = {}
	for placement in placements:
		for cell in placement['cells']:
			cluster_cell_owner[cell] = placement['username']
			cluster_cell_shape[cell] = placement['shape_id']
			if placement.get('winner'):
				cluster_cell_color[cell] = USER_REGISTRY.get(placement['winner'], {}).get('color', '#ffffff')
			else:
				cluster_cell_color[cell] = '#ffffff'
	return placements, cluster_cell_owner, cluster_cell_shape, cluster_cell_color


def _build_dashboard_state(username):
	is_admin = username == ADMIN_USERNAME
	reveal_mode = is_reveal_mode()
	final_stage = is_final_stage_active()
	show_results = is_admin or reveal_mode
	votes_by_cell, user_votes = _dashboard_votes_for_user(username)
	cluster_placements, cluster_cell_owner, cluster_cell_shape, cluster_cell_color = _dashboard_cluster_maps()
	cluster_scores = get_cluster_scores()
	remaining = None if is_admin else get_user_remaining_ballots(username)
	current_turn = get_current_placement_user() if final_stage else None
	used_shapes = get_user_used_shape_ids(username) if final_stage and not is_admin else []
	placement_order = get_placement_order() if is_admin else []
	blocked_cells = sorted(get_blocked_cells())
	row_lengths = get_row_lengths()

	cells = {}
	for row_index, row_length in enumerate(row_lengths, start=1):
		for col_index in range(1, row_length + 1):
			cell_id = f'R{row_index}C{col_index}'
			votes_list = votes_by_cell.get(cell_id, [])
			total_votes = sum(item['ballots'] for item in votes_list)
			cluster_owner = cluster_cell_owner.get(cell_id)
			pie_gradient = ''
			if show_results and total_votes > 0 and not cluster_owner:
				current_pct = 0.0
				gradient_parts = []
				for item in votes_list:
					item_pct = (item['ballots'] / total_votes) * 100
					next_pct = current_pct + item_pct
					gradient_parts.append(f"{item['color']} {current_pct:.2f}% {next_pct:.2f}%")
					current_pct = next_pct
				pie_gradient = f"conic-gradient({', '.join(gradient_parts)})"

			cells[cell_id] = {
				'total_votes': total_votes,
				'votes_list': votes_list,
				'my_votes': user_votes.get(cell_id, 0),
				'show_results': show_results,
				'cluster_owner': cluster_owner,
				'cluster_shape': cluster_cell_shape.get(cell_id),
				'cluster_color': cluster_cell_color.get(cell_id),
				'blocked': cell_id in blocked_cells,
				'pie_gradient': pie_gradient,
			}

	return {
		'username': username,
		'is_admin': is_admin,
		'reveal_mode': reveal_mode,
		'final_stage': final_stage,
		'show_results': show_results,
		'remaining': remaining,
		'current_turn': current_turn,
		'is_user_turn': bool(final_stage and not is_admin and current_turn == username),
		'used_shapes': used_shapes,
		'placement_order': placement_order,
		'cluster_scores': cluster_scores,
		'cluster_placements': cluster_placements,
		'cluster_cell_owner': cluster_cell_owner,
		'cluster_cell_shape': cluster_cell_shape,
		'cluster_cell_color': cluster_cell_color,
		'shape_catalog': SHAPE_CATALOG,
		'row_lengths': row_lengths,
		'blocked_cells': blocked_cells,
		'cells': cells,
		'all_votes_data': votes_by_cell,
		'user_votes': user_votes,
	}


def _broadcast_dashboard_update(event_name='dashboard_update', payload=None):
	global _dashboard_revision
	with _dashboard_lock:
		_dashboard_revision += 1
		listeners = list(_dashboard_listeners)
	message = {
		'event': event_name,
		'revision': _dashboard_revision,
		'payload': payload or {},
	}
	for listener in listeners:
		listener.put(message)


@main_bp.route('')
@main_bp.route('/')
def index():
	if 'username' not in session:
		return render_template('login.html', logged_in=False, error=None)

	username = session['username']
	state = _build_dashboard_state(username)

	if state['is_admin']:
		return render_template(
			'admin.html',
			logged_in=True,
			user_color=ADMIN_COLOR,
			**state,
		)

	return render_template(
		'user.html',
		logged_in=True,
		user_color=USER_REGISTRY[username]['color'],
		**state,
	)


@main_bp.route('/login', methods=['POST'])
def login():
	username = request.form.get('username', '').strip().lower()
	secret = request.form.get('secret', '').strip()

	if username == ADMIN_USERNAME and secret == ADMIN_PASSWORD:
		session['username'] = ADMIN_USERNAME
		return redirect(url_for('main.index'))

	active = get_active_user_registry()
	if username in active and active[username]['secret'] == secret:
		session['username'] = username
		return redirect(url_for('main.index'))

	return render_template('login.html', logged_in=False, error='Invalid Username or Secret Keyword.')


@main_bp.route('/logout')
def logout():
	session.pop('username', None)
	return redirect(url_for('main.index'))


@main_bp.route('/api/state')
def api_state():
	username = session.get('username')
	if not username:
		return jsonify({'success': False, 'error': 'Unauthorized'}), 401
	return jsonify({'success': True, 'state': _build_dashboard_state(username)})


@main_bp.route('/api/events')
def api_events():
	username = session.get('username')
	if not username:
		return jsonify({'success': False, 'error': 'Unauthorized'}), 401

	event_queue = queue.Queue()
	with _dashboard_lock:
		_dashboard_listeners.add(event_queue)

	def event_stream():
		try:
			yield 'event: ready\ndata: {}\n\n'
			while True:
				message = event_queue.get()
				yield f"event: {message['event']}\ndata: {json.dumps(message)}\n\n"
		finally:
			with _dashboard_lock:
				_dashboard_listeners.discard(event_queue)

	response = Response(stream_with_context(event_stream()), mimetype='text/event-stream')
	response.headers['Cache-Control'] = 'no-cache'
	response.headers['X-Accel-Buffering'] = 'no'
	return response


@main_bp.route('/api/vote', methods=['POST'])
def api_vote():
	if 'username' not in session or session['username'] == ADMIN_USERNAME:
		return jsonify({'success': False, 'error': 'Unauthorized'}), 401

	username = session['username']
	data = request.json or {}
	cell_id = data.get('cell_id')
	action = data.get('action')

	if is_cell_blocked(cell_id):
		return jsonify({'success': False, 'error': 'This cell is unavailable'}), 400

	remaining = get_user_remaining_ballots(username)

	with get_db() as conn:
		if action == 'add':
			if remaining <= 0:
				return jsonify({'success': False, 'error': 'No ballots left!'}), 400

			current_row = conn.execute(
				'SELECT ballots_spent FROM grid_votes WHERE username = ? AND cell_id = ?',
				(username, cell_id),
			).fetchone()
			current_ballots = current_row['ballots_spent'] if current_row else 0
			if current_ballots >= MAX_BALLOTS_PER_CELL_PER_USER:
				return jsonify({
					'success': False,
					'error': f'You can place at most {MAX_BALLOTS_PER_CELL_PER_USER} ballots on a single grid.',
				}), 400

			conn.execute(
				'''
				INSERT INTO grid_votes (username, cell_id, ballots_spent)
				VALUES (?, ?, 1)
				ON CONFLICT(username, cell_id) DO UPDATE SET ballots_spent = ballots_spent + 1
				''',
				(username, cell_id),
			)
		elif action == 'clear':
			locked_ballots = get_locked_ballots(username, cell_id)
			current_row = conn.execute(
				'SELECT ballots_spent FROM grid_votes WHERE username = ? AND cell_id = ?',
				(username, cell_id),
			).fetchone()
			current_ballots = current_row['ballots_spent'] if current_row else 0

			if locked_ballots > 0:
				if current_ballots > locked_ballots:
					conn.execute(
						'UPDATE grid_votes SET ballots_spent = ? WHERE username = ? AND cell_id = ?',
						(locked_ballots, username, cell_id),
					)
			else:
				conn.execute('DELETE FROM grid_votes WHERE username = ? AND cell_id = ?', (username, cell_id))

		conn.commit()
		updated_row = conn.execute(
			'SELECT ballots_spent FROM grid_votes WHERE username = ? AND cell_id = ?',
			(username, cell_id),
		).fetchone()

	new_remaining = get_user_remaining_ballots(username)
	_broadcast_dashboard_update('dashboard_update', {'reason': 'vote', 'username': username, 'cell_id': cell_id})

	return jsonify({
		'success': True,
		'remaining': new_remaining,
		'cell_user': updated_row['ballots_spent'] if updated_row else 0,
	})


@main_bp.route('/api/admin/update_results', methods=['POST'])
def admin_update_results():
	if session.get('username') != ADMIN_USERNAME:
		return jsonify({'success': False}), 403

	snapshot_locked_votes()

	with get_db() as conn:
		active = get_active_user_registry()
		for user in active:
			row = conn.execute('SELECT SUM(ballots_spent) as total_spent FROM grid_votes WHERE username = ?', (user,)).fetchone()
			spent = row['total_spent'] if row and row['total_spent'] else 0
			conn.execute('UPDATE user_allowances SET total_allowance = ? WHERE username = ?', (TOTAL_ALLOWANCE + spent, user))
		conn.commit()

	snapshot = {}
	rows = fetch_ballot_votes()
	for row in rows:
		cell_id = row['cell_id']
		snapshot.setdefault(cell_id, []).append({
			'username': row['username'],
			'ballots': row['ballots_spent'],
			'color': USER_REGISTRY.get(row['username'], {}).get('color', '#fff'),
		})

	save_admin_snapshot(snapshot)
	_broadcast_dashboard_update('dashboard_update', {'reason': 'admin_update_results'})
	return jsonify({'success': True, 'message': 'Results updated and ballots reset.'})


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

	snapshot = {}
	rows = fetch_ballot_votes()
	for row in rows:
		cell_id = row['cell_id']
		snapshot.setdefault(cell_id, []).append({
			'username': row['username'],
			'ballots': row['ballots_spent'],
			'color': USER_REGISTRY.get(row['username'], {}).get('color', '#fff'),
		})
	save_admin_snapshot(snapshot)
	_broadcast_dashboard_update('dashboard_update', {'reason': 'final_stage_started', 'placement_order': placement_order})

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

	if any(is_cell_blocked(cell) for cell in cells):
		return jsonify({'success': False, 'error': 'Placement overlaps an unavailable cell'}), 400

	existing = get_cluster_placements()
	occupied = {cell for placement in existing for cell in placement['cells']}
	if any(cell in occupied for cell in cells):
		return jsonify({'success': False, 'error': 'Placement overlaps existing cluster'}), 400

	with get_db() as conn:
		placeholders = ','.join('?' for _ in cells)
		rows = conn.execute(
			f'SELECT username, SUM(ballots_spent) AS ballots FROM grid_votes WHERE cell_id IN ({placeholders}) GROUP BY username',
			tuple(cells),
		).fetchall()
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
	_broadcast_dashboard_update('dashboard_update', {
		'reason': 'cluster_placed',
		'username': username,
		'shape_id': shape_id,
		'next_user': next_user,
	})

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
		active = get_active_user_registry()
		for user in active:
			row = conn.execute('SELECT SUM(ballots_spent) as total_spent FROM grid_votes WHERE username = ?', (user,)).fetchone()
			spent = row['total_spent'] if row and row['total_spent'] else 0
			conn.execute('UPDATE user_allowances SET total_allowance = ? WHERE username = ?', (TOTAL_ALLOWANCE + spent, user))
		conn.commit()

	_broadcast_dashboard_update('dashboard_update', {'reason': 'grant_ballots'})
	return jsonify({'success': True})