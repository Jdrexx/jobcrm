from fastapi.testclient import TestClient
from src.main import app
client = TestClient(app)
def test_health():
    r=client.get('/api/health')
    assert r.status_code == 200
    assert r.json()['ok'] is True

def test_application_board():
    client.post('/api/applications', json={'company':'Acme','role':'Engineer','status':'applied'})
    data=client.get('/api/applications').json()
    assert 'applied' in data['board']
