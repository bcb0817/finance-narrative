import pytest


@pytest.fixture(autouse=True)
def legacy_collection_regressions(monkeypatch):
    """Existing tests retain the rollback contract; collection tests opt in explicitly."""
    monkeypatch.setenv('COLLECTION_QUALITY_ENABLED', 'false')
