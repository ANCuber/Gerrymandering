import pytest

from app import app


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
