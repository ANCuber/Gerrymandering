import json
import math
import queue
import threading
import time

from flask import Blueprint, Response, jsonify, redirect, render_template, request, session, stream_with_context, url_for

from config import ADMIN_COLOR, ADMIN_PASSWORD, ADMIN_USERNAME, FINAL_STAGE_TURN_SECONDS, MAX_BALLOTS_PER_CELL_PER_USER, TOTAL_ALLOWANCE, USER_REGISTRY, get_active_user_registry
from db import (
	add_cluster_score,
	advance_placement_turn,
	increment_voting_round,
	fetch_ballot_votes,
	get_admin_snapshot,
	get_cluster_placements,
	get_cluster_scores,
	get_current_placement_user,
	get_db,
	get_locked_ballots,
	get_placement_order,
	get_turn_deadline_epoch,
	get_user_remaining_ballots,
	get_user_used_shape_ids,
	get_voting_round,
	is_final_stage_active,
	is_reveal_mode,
	set_turn_deadline_epoch,
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


def _board_cell_ids():
	cell_ids = []
	for r, row_len in enumerate(get_row_lengths(), start=1):
		for c in range(1, row_len + 1):
			cell_ids.append(f'R{r}C{c}')
	return cell_ids


def _user_has_any_valid_cluster_placement(username):
	if not username or _is_example_user(username):
		return False

	used_shapes = set(get_user_used_shape_ids(username))
	available_shapes = [shape['id'] for shape in SHAPE_CATALOG if shape['id'] not in used_shapes]
	if not available_shapes:
		return False

	placements = get_cluster_placements()
	occupied = {cell for placement in placements for cell in placement['cells']}
	blocked = get_blocked_cells()
	anchor_cells = [cell_id for cell_id in _board_cell_ids() if cell_id not in blocked and cell_id not in occupied]
	if not anchor_cells:
		return False

	for shape_id in available_shapes:
		for orientation in range(6):
			for anchor in anchor_cells:
				cells = placement_cells(shape_id, anchor, orientation)
				if len(cells) != 5:
					continue
				if any(cell in blocked or cell in occupied for cell in cells):
					continue
				return True

	return False


def _set_current_turn_deadline():
	current_user = get_current_placement_user()
	if not current_user:
		set_turn_deadline_epoch(0)
		return 0.0
	deadline = time.time() + max(1, int(FINAL_STAGE_TURN_SECONDS))
	set_turn_deadline_epoch(deadline)
	return deadline


def _enforce_final_stage_turn_progress():
	if not is_final_stage_active():
		return

	order = get_placement_order()
	if not order:
		set_turn_deadline_epoch(0)
		return

	max_hops = len(order)
	hops = 0
	while hops < max_hops:
		hops += 1
		current_user = get_current_placement_user()
		if not current_user:
			set_turn_deadline_epoch(0)
			return

		if _is_example_user(current_user):
			next_user = advance_placement_turn()
			_set_current_turn_deadline()
			_broadcast_dashboard_update('dashboard_update', {
				'reason': 'turn_skipped_example',
			})
			continue

		if not _user_has_any_valid_cluster_placement(current_user):
			next_user = advance_placement_turn()
			_set_current_turn_deadline()
			_broadcast_dashboard_update('dashboard_update', {
				'reason': 'turn_skipped_no_valid_placement',
			})
			continue

		deadline = get_turn_deadline_epoch()
		now = time.time()
		if deadline <= 0:
			_set_current_turn_deadline()
			return

		if now >= deadline:
			next_user = advance_placement_turn()
			_set_current_turn_deadline()
			_broadcast_dashboard_update('dashboard_update', {
				'reason': 'turn_timed_out',
			})
			continue

		return

	# All users in the loop were skipped; avoid an infinite skip cycle.
	set_turn_deadline_epoch(0)


def _is_example_user(username):
	return username == 'example'


def _group_sort_key(username):
	if not isinstance(username, str):
		return (1, str(username))
	if username.startswith('group') and username[5:].isdigit():
		return (0, int(username[5:]))
	return (1, username)


def _current_per_cell_cap():
	round_number = get_voting_round()
	return MAX_BALLOTS_PER_CELL_PER_USER + (round_number - 1) * 5


def _dashboard_votes_for_user(username, include_full_breakdown=False):
	if username == ADMIN_USERNAME:
		# Admin sees the frozen snapshot; it is refreshed by the Next Round action.
		votes_by_cell = get_admin_snapshot()
		totals_by_cell = {
			cell_id: sum(item.get('ballots', 0) for item in votes)
			for cell_id, votes in votes_by_cell.items()
		}
		return votes_by_cell, totals_by_cell, {}

	rows = fetch_ballot_votes()
	votes_by_cell = {}
	totals_by_cell = {}
	user_votes = {}
	for row in rows:
		cell_id = row['cell_id']
		totals_by_cell[cell_id] = totals_by_cell.get(cell_id, 0) + row['ballots_spent']
		if include_full_breakdown or row['username'] == username:
			votes_by_cell.setdefault(cell_id, []).append({
				'username': row['username'],
				'ballots': row['ballots_spent'],
				'color': USER_REGISTRY.get(row['username'], {}).get('color', '#ffffff'),
			})
		if row['username'] == username:
			user_votes[cell_id] = row['ballots_spent']
	return votes_by_cell, totals_by_cell, user_votes


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
	_enforce_final_stage_turn_progress()
	is_admin = username == ADMIN_USERNAME
	reveal_mode = is_reveal_mode()
	final_stage = is_final_stage_active()
	show_results = is_admin or reveal_mode or final_stage
	show_distribution = is_admin or final_stage
	user_colors = {user: meta.get('color', '#c1c1c8') for user, meta in USER_REGISTRY.items()}
	user_colors[ADMIN_USERNAME] = ADMIN_COLOR
	votes_by_cell, totals_by_cell, user_votes = _dashboard_votes_for_user(
		username,
		include_full_breakdown=show_distribution,
	)
	cluster_placements, cluster_cell_owner, cluster_cell_shape, cluster_cell_color = _dashboard_cluster_maps()
	cluster_scores = {
		user: score
		for user, score in sorted(get_cluster_scores().items(), key=lambda item: _group_sort_key(item[0]))
		if not _is_example_user(user)
	}
	remaining = None if is_admin else get_user_remaining_ballots(username)
	current_turn = get_current_placement_user() if final_stage else None
	turn_deadline_epoch = get_turn_deadline_epoch() if final_stage else 0.0
	turn_seconds_remaining = max(0, int(math.ceil(turn_deadline_epoch - time.time()))) if final_stage and turn_deadline_epoch > 0 else 0
	used_shapes = get_user_used_shape_ids(username) if final_stage and not is_admin else []
	placement_order = get_placement_order() if is_admin else []
	blocked_cells = sorted(get_blocked_cells())
	row_lengths = get_row_lengths()

	cells = {}
	for row_index, row_length in enumerate(row_lengths, start=1):
		for col_index in range(1, row_length + 1):
			cell_id = f'R{row_index}C{col_index}'
			votes_list = votes_by_cell.get(cell_id, [])
			total_votes = totals_by_cell.get(cell_id, 0)
			cluster_owner = cluster_cell_owner.get(cell_id)
			pie_gradient = ''
			display_votes_list = [item for item in votes_list if not (is_admin and item['username'] == 'example')]
			if show_distribution and show_results and total_votes > 0 and not cluster_owner:
				current_pct = 0.0
				gradient_parts = []
				for item in display_votes_list:
					item_pct = (item['ballots'] / total_votes) * 100
					next_pct = current_pct + item_pct
					gradient_parts.append(f"{item['color']} {current_pct:.2f}% {next_pct:.2f}%")
					current_pct = next_pct
				pie_gradient = f"conic-gradient({', '.join(gradient_parts)})"

			cells[cell_id] = {
				'total_votes': total_votes,
				'votes_list': votes_list if is_admin else [],
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
		'is_example_user': _is_example_user(username),
		'reveal_mode': reveal_mode,
		'final_stage': final_stage,
		'show_results': show_results,
		'remaining': remaining,
		'current_turn': current_turn,
		'turn_deadline_epoch': turn_deadline_epoch if final_stage else None,
		'turn_seconds_remaining': turn_seconds_remaining if final_stage else None,
		'turn_time_limit_seconds': int(FINAL_STAGE_TURN_SECONDS),
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
		'user_votes': user_votes,
		'user_colors': user_colors,
	}


def _evaluate_cluster_placement(shape_id, anchor, orientation):
	cells = placement_cells(shape_id, anchor, orientation)
	if len(cells) != 5:
		return {'success': False, 'error': '放置無效'}

	if any(is_cell_blocked(cell) for cell in cells):
		return {'success': False, 'error': '放置與不可用格重疊'}

	existing = get_cluster_placements()
	occupied = {cell for placement in existing for cell in placement['cells']}
	if any(cell in occupied for cell in cells):
		return {'success': False, 'error': '放置與既有板塊重疊'}

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
		sorted_ballots = sorted(ballots_per_user.values(), reverse=True)
		runner_up_ballots = sorted_ballots[1] if len(sorted_ballots) > 1 else 0
		winners = [user for user, ballots in ballots_per_user.items() if ballots == max_ballots]
	else:
		winners = []
		max_ballots = 0
		runner_up_ballots = 0

	winner = winners[0] if len(winners) == 1 and max_ballots > 0 else None
	score_gain = total_ballots if winner else 0
	win_margin = (max_ballots - runner_up_ballots) if winner else 0
	return {
		'success': True,
		'cells': cells,
		'total_ballots': total_ballots,
		'winner': winner,
		'score_gain': score_gain,
		'win_margin': win_margin,
		'is_tie': len(winners) > 1 and max_ballots > 0,
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
		with get_db() as conn:
			conn.execute(
				'INSERT OR IGNORE INTO user_allowances (username, total_allowance) VALUES (?, ?)',
				(username, TOTAL_ALLOWANCE),
			)
			conn.execute(
				'INSERT OR IGNORE INTO cluster_scores (username, score) VALUES (?, 0)',
				(username,),
			)
			conn.commit()
		return redirect(url_for('main.index'))

	return render_template('login.html', logged_in=False, error='使用者名稱或密碼錯誤。')


@main_bp.route('/logout')
def logout():
	session.pop('username', None)
	return redirect(url_for('main.index'))


@main_bp.route('/api/state')
def api_state():
	username = session.get('username')
	if not username:
		return jsonify({'success': False, 'error': '未授權'}), 401
	return jsonify({'success': True, 'state': _build_dashboard_state(username)})


@main_bp.route('/api/events')
def api_events():
	username = session.get('username')
	if not username:
		return jsonify({'success': False, 'error': '未授權'}), 401

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
		return jsonify({'success': False, 'error': '未授權'}), 401

	username = session['username']
	data = request.json or {}
	cell_id = data.get('cell_id')
	action = data.get('action')
	amount_raw = data.get('amount', 1)
	try:
		amount = int(amount_raw)
	except (TypeError, ValueError):
		return jsonify({'success': False, 'error': '票數數量格式不正確'}), 400

	if amount < 1:
		return jsonify({'success': False, 'error': '票數數量至少為 1'}), 400

	if is_cell_blocked(cell_id):
		return jsonify({'success': False, 'error': '此格不可使用'}), 400

	remaining = get_user_remaining_ballots(username)

	with get_db() as conn:
		if action == 'add':
			if remaining < amount:
				return jsonify({'success': False, 'error': '剩餘猴子不足！'}), 400

			current_row = conn.execute(
				'SELECT ballots_spent FROM grid_votes WHERE username = ? AND cell_id = ?',
				(username, cell_id),
			).fetchone()
			current_ballots = current_row['ballots_spent'] if current_row else 0
			max_per_cell = _current_per_cell_cap()
			if current_ballots + amount > max_per_cell:
				return jsonify({
					'success': False,
					'error': f'本輪每格最多可放置 {max_per_cell} 隻猴子。',
				}), 400

			conn.execute(
				'''
				INSERT INTO grid_votes (username, cell_id, ballots_spent)
				VALUES (?, ?, ?)
				ON CONFLICT(username, cell_id) DO UPDATE SET ballots_spent = ballots_spent + ?
				''',
				(username, cell_id, amount, amount),
			)
		elif action == 'remove':
			locked_ballots = get_locked_ballots(username, cell_id)
			current_row = conn.execute(
				'SELECT ballots_spent FROM grid_votes WHERE username = ? AND cell_id = ?',
				(username, cell_id),
			).fetchone()
			current_ballots = current_row['ballots_spent'] if current_row else 0
			removable = max(0, current_ballots - locked_ballots)

			if removable < amount:
				return jsonify({'success': False, 'error': '此格已無法再減少。'}), 400

			new_ballots = current_ballots - amount
			if new_ballots > 0:
				conn.execute(
					'UPDATE grid_votes SET ballots_spent = ? WHERE username = ? AND cell_id = ?',
					(new_ballots, username, cell_id),
				)
			else:
				conn.execute('DELETE FROM grid_votes WHERE username = ? AND cell_id = ?', (username, cell_id))
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
		else:
			return jsonify({'success': False, 'error': '無效操作'}), 400

		conn.commit()
		updated_row = conn.execute(
			'SELECT ballots_spent FROM grid_votes WHERE username = ? AND cell_id = ?',
			(username, cell_id),
		).fetchone()

	new_remaining = get_user_remaining_ballots(username)
	_broadcast_dashboard_update('dashboard_update', {'reason': 'vote'})

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
	increment_voting_round()

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
		if _is_example_user(row['username']):
			continue
		cell_id = row['cell_id']
		snapshot.setdefault(cell_id, []).append({
			'username': row['username'],
			'ballots': row['ballots_spent'],
			'color': USER_REGISTRY.get(row['username'], {}).get('color', '#fff'),
		})

	save_admin_snapshot(snapshot)
	_broadcast_dashboard_update('dashboard_update', {'reason': 'admin_update_results'})
	return jsonify({'success': True, 'message': '結果已更新，猴子數已重置。'})


@main_bp.route('/api/admin/start_final_stage', methods=['POST'])
def admin_start_final_stage():
	if session.get('username') != ADMIN_USERNAME:
		return jsonify({'success': False}), 403

	ballot_rows = fetch_ballot_votes()
	ballots_by_user = {username: {} for username in USER_REGISTRY if not _is_example_user(username)}
	for row in ballot_rows:
		if _is_example_user(row['username']):
			continue
		ballots_by_user.setdefault(row['username'], {})[row['cell_id']] = row['ballots_spent']

	placement_order = order_users_by_variance(ballots_by_user)
	start_final_stage(placement_order)
	_set_current_turn_deadline()
	_enforce_final_stage_turn_progress()

	snapshot = {}
	rows = fetch_ballot_votes()
	for row in rows:
		if _is_example_user(row['username']):
			continue
		cell_id = row['cell_id']
		snapshot.setdefault(cell_id, []).append({
			'username': row['username'],
			'ballots': row['ballots_spent'],
			'color': USER_REGISTRY.get(row['username'], {}).get('color', '#fff'),
		})
	save_admin_snapshot(snapshot)
	_broadcast_dashboard_update('dashboard_update', {'reason': 'final_stage_started'})

	return jsonify({'success': True, 'placement_order': placement_order})


@main_bp.route('/api/place_cluster', methods=['POST'])
def api_place_cluster():
	if 'username' not in session or session['username'] == ADMIN_USERNAME:
		return jsonify({'success': False, 'error': '未授權'}), 401

	username = session['username']
	if _is_example_user(username):
		return jsonify({
			'success': True,
			'next_user': get_current_placement_user() if is_final_stage_active() else None,
			'winner': None,
			'cluster_cells': [],
			'total_ballots': 0,
			'noop': True,
		})

	if not is_final_stage_active():
		return jsonify({'success': False, 'error': '最終階段尚未開始'}), 400

	_enforce_final_stage_turn_progress()

	current_user = get_current_placement_user()
	if username != current_user:
		return jsonify({'success': False, 'error': '尚未輪到你'}), 400

	data = request.json or {}
	shape_id = data.get('shape_id')
	anchor = data.get('anchor')
	orientation = int(data.get('orientation', 0))

	used_shapes = get_user_used_shape_ids(username)
	if shape_id in used_shapes:
		return jsonify({'success': False, 'error': '此板塊已使用'}), 400

	evaluation = _evaluate_cluster_placement(shape_id, anchor, orientation)
	if not evaluation.get('success'):
		return jsonify({'success': False, 'error': evaluation.get('error', '放置無效')}), 400

	cells = evaluation['cells']
	total_ballots = evaluation['total_ballots']
	winner = evaluation['winner']
	if winner:
		add_cluster_score(winner, total_ballots)

	save_cluster_placement(username, shape_id, anchor, orientation, cells, winner, total_ballots if winner else 0)
	next_user = advance_placement_turn()
	_set_current_turn_deadline()
	_enforce_final_stage_turn_progress()
	_broadcast_dashboard_update('dashboard_update', {
		'reason': 'cluster_placed',
	})

	return jsonify({
		'success': True,
		'next_user': next_user,
		'winner': winner,
		'cluster_cells': cells,
		'total_ballots': total_ballots,
	})


@main_bp.route('/api/cluster_preview', methods=['POST'])
def api_cluster_preview():
	if 'username' not in session or session['username'] == ADMIN_USERNAME:
		return jsonify({'success': False, 'error': '未授權'}), 401

	username = session['username']
	if _is_example_user(username):
		return jsonify({'success': False, 'error': '範例帳號不可預覽'}), 400

	if not is_final_stage_active():
		return jsonify({'success': False, 'error': '最終階段尚未開始'}), 400

	_enforce_final_stage_turn_progress()
	current_user = get_current_placement_user()
	if username != current_user:
		return jsonify({'success': False, 'error': '尚未輪到你'}), 400

	data = request.json or {}
	shape_id = data.get('shape_id')
	anchor = data.get('anchor')
	orientation = int(data.get('orientation', 0))

	used_shapes = get_user_used_shape_ids(username)
	if shape_id in used_shapes:
		return jsonify({'success': False, 'error': '此板塊已使用'}), 400

	evaluation = _evaluate_cluster_placement(shape_id, anchor, orientation)
	if not evaluation.get('success'):
		return jsonify({'success': False, 'error': evaluation.get('error', '放置無效')}), 400

	return jsonify({
		'success': True,
		'winner': evaluation['winner'],
		'total_ballots': evaluation['total_ballots'],
		'score_gain': evaluation['score_gain'],
		'win_margin': evaluation['win_margin'],
		'is_tie': evaluation['is_tie'],
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