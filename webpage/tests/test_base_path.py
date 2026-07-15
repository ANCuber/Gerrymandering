import pytest

from app import app
from db import get_db
from placement_order import SHAPE_CATALOG, get_row_lengths, is_cell_blocked, placement_cells


@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


def test_index_is_available_under_prefixed_base_path(client):
    response = client.get('/gerrymandering')
    assert response.status_code == 200
    assert b'Enter Secure Game Space' in response.data


def test_login_and_logout_work_under_prefixed_base_path(client):
    response = client.post(
        '/gerrymandering/login',
        data={'username': 'admin', 'secret': 'admin'},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/gerrymandering/')

    with client.session_transaction() as session:
        assert session['username'] == 'admin'

    logout_response = client.get('/gerrymandering/logout', follow_redirects=False)
    assert logout_response.status_code == 302
    assert logout_response.headers['Location'].endswith('/gerrymandering/')

    with client.session_transaction() as session:
        assert 'username' not in session


def test_dashboard_state_is_available_under_prefixed_base_path(client):
    client.post(
        '/gerrymandering/login',
        data={'username': 'admin', 'secret': 'admin'},
        follow_redirects=False,
    )

    response = client.get('/gerrymandering/api/state')
    assert response.status_code == 200

    payload = response.get_json()
    assert payload['success'] is True
    assert payload['state']['is_admin'] is True


def test_admin_dashboard_renders_under_prefixed_base_path(client):
    client.post(
        '/gerrymandering/login',
        data={'username': 'admin', 'secret': 'admin'},
        follow_redirects=False,
    )

    response = client.get('/gerrymandering/')
    assert response.status_code == 200
    assert b'Admin Dashboard' in response.data


def test_user_dashboard_renders_under_prefixed_base_path(client):
    client.post(
        '/gerrymandering/login',
        data={'username': 'group1', 'secret': 'group1'},
        follow_redirects=False,
    )

    response = client.get('/gerrymandering/')
    assert response.status_code == 200
    assert b'Ballots Remaining' in response.data


def test_user_state_hides_other_users_moves(client):
    with get_db() as conn:
        conn.execute('DELETE FROM grid_votes')
        conn.commit()

    client.post(
        '/gerrymandering/login',
        data={'username': 'group1', 'secret': 'group1'},
        follow_redirects=False,
    )
    vote_response_1 = client.post(
        '/gerrymandering/api/vote',
        json={'cell_id': 'R8C8', 'action': 'add', 'amount': 3},
    )
    assert vote_response_1.status_code == 200
    assert vote_response_1.get_json()['success'] is True

    client.get('/gerrymandering/logout', follow_redirects=False)

    client.post(
        '/gerrymandering/login',
        data={'username': 'group2', 'secret': 'group2'},
        follow_redirects=False,
    )
    vote_response_2 = client.post(
        '/gerrymandering/api/vote',
        json={'cell_id': 'R8C8', 'action': 'add', 'amount': 5},
    )
    assert vote_response_2.status_code == 200
    assert vote_response_2.get_json()['success'] is True

    client.get('/gerrymandering/logout', follow_redirects=False)

    client.post(
        '/gerrymandering/login',
        data={'username': 'group1', 'secret': 'group1'},
        follow_redirects=False,
    )
    state_response = client.get('/gerrymandering/api/state')
    assert state_response.status_code == 200
    payload = state_response.get_json()
    assert payload['success'] is True

    cell_state = payload['state']['cells']['R8C8']
    assert cell_state['total_votes'] == 8
    assert cell_state['my_votes'] == 3
    assert cell_state['votes_list'] == []


def test_user_state_shows_distribution_in_final_stage(client):
    with get_db() as conn:
        conn.execute('DELETE FROM grid_votes')
        conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('final_stage', 'true')")
        conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('placement_order', '[\"group1\", \"group2\"]')")
        conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('placement_index', '0')")
        conn.execute(
            'INSERT OR REPLACE INTO grid_votes (username, cell_id, ballots_spent) VALUES (?, ?, ?)',
            ('group1', 'R8C8', 3),
        )
        conn.execute(
            'INSERT OR REPLACE INTO grid_votes (username, cell_id, ballots_spent) VALUES (?, ?, ?)',
            ('group2', 'R8C8', 5),
        )
        conn.commit()

    client.post(
        '/gerrymandering/login',
        data={'username': 'group1', 'secret': 'group1'},
        follow_redirects=False,
    )
    state_response = client.get('/gerrymandering/api/state')
    assert state_response.status_code == 200

    payload = state_response.get_json()
    assert payload['success'] is True
    assert payload['state']['final_stage'] is True

    cell_state = payload['state']['cells']['R8C8']
    assert cell_state['total_votes'] == 8
    assert cell_state['my_votes'] == 3
    assert cell_state['votes_list'] == []
    assert cell_state['pie_gradient'].startswith('conic-gradient(')


def test_cluster_preview_shows_winner_without_cells(client):
    with get_db() as conn:
        conn.execute('DELETE FROM grid_votes')
        conn.execute('DELETE FROM cluster_placements')
        conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('final_stage', 'true')")
        conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('placement_order', '[\"group1\"]')")
        conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('placement_index', '0')")
        conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('final_stage_turn_deadline', '9999999999')")
        conn.execute(
            'INSERT OR REPLACE INTO grid_votes (username, cell_id, ballots_spent) VALUES (?, ?, ?)',
            ('group1', 'R8C8', 5),
        )
        conn.execute(
            'INSERT OR REPLACE INTO grid_votes (username, cell_id, ballots_spent) VALUES (?, ?, ?)',
            ('group2', 'R8C9', 3),
        )
        conn.commit()

    client.post(
        '/gerrymandering/login',
        data={'username': 'group1', 'secret': 'group1'},
        follow_redirects=False,
    )

    valid_payload = None
    row_lengths = get_row_lengths()
    for shape in SHAPE_CATALOG:
        if valid_payload:
            break
        for orientation in range(6):
            if valid_payload:
                break
            for row_index, row_length in enumerate(row_lengths, start=1):
                if valid_payload:
                    break
                for col_index in range(1, row_length + 1):
                    anchor = f'R{row_index}C{col_index}'
                    cells = placement_cells(shape['id'], anchor, orientation)
                    if len(cells) != 5:
                        continue
                    if any(is_cell_blocked(cell) for cell in cells):
                        continue
                    valid_payload = {
                        'shape_id': shape['id'],
                        'anchor': anchor,
                        'orientation': orientation,
                    }
                    break

    assert valid_payload is not None

    preview_response = client.post(
        '/gerrymandering/api/cluster_preview',
        json=valid_payload,
    )
    assert preview_response.status_code == 200

    payload = preview_response.get_json()
    assert payload['success'] is True
    assert 'winner' in payload
    assert 'total_ballots' in payload
    assert 'score_gain' in payload
    assert 'win_margin' in payload
    assert 'cells' not in payload


def test_user_can_skip_own_turn_in_final_stage(client):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('final_stage', 'true')")
        conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('placement_order', '[\"group1\", \"group2\"]')")
        conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('placement_index', '0')")
        conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('final_stage_turn_deadline', '9999999999')")
        conn.commit()

    client.post(
        '/gerrymandering/login',
        data={'username': 'group1', 'secret': 'group1'},
        follow_redirects=False,
    )

    skip_response = client.post('/gerrymandering/api/skip_turn', json={})
    assert skip_response.status_code == 200
    skip_payload = skip_response.get_json()
    assert skip_payload['success'] is True
    assert skip_payload['next_user'] == 'group2'

    state_response = client.get('/gerrymandering/api/state')
    assert state_response.status_code == 200
    state_payload = state_response.get_json()
    assert state_payload['success'] is True
    assert state_payload['state']['current_turn'] == 'group2'
