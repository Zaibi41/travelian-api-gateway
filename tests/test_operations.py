from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


def test_health_and_readiness_lifecycle():
    with patch('app.main.load_parameter_store_settings') as load:
        with TestClient(app) as client:
            for prefix in ('', '/v1'):
                response = client.get(f'{prefix}/health')
                assert response.status_code == 200
                assert response.json() == {'status': 'ok'}
                response = client.get(f'{prefix}/ready')
                assert response.status_code == 200
                assert response.json() == {'status': 'ready'}
            load.assert_called_once()
        assert client.get('/ready').status_code == 503
        assert client.get('/health').status_code == 200


def test_startup_failure_keeps_application_unready():
    with patch('app.main.load_parameter_store_settings', side_effect=RuntimeError('startup failed')):
        with pytest.raises(RuntimeError, match='startup failed'):
            with TestClient(app):
                pass
    assert app.state.ready is False
