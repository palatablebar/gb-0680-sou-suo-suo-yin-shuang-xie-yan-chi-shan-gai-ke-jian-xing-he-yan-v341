import pytest
from app import store, release_store
from app.config import ROOT
from app.pipeline import calculate,load_sample
@pytest.fixture(autouse=True)
def isolated_database(tmp_path,monkeypatch):
    monkeypatch.setattr(store,"DATA_DIR",tmp_path)
    monkeypatch.setattr(release_store,"DATA_DIR",tmp_path)
@pytest.fixture
def sample(): return load_sample("dirty.csv")
@pytest.fixture
def result(sample): return calculate(sample)
@pytest.fixture
def client():
    from app.main import create_app
    return create_app().test_client()
