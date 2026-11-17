import pytest
from pydantic import ValidationError

from app.db.config import DatabaseSettings


@pytest.fixture(autouse=True)
def clean_database_environment(monkeypatch):
    for key in ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"]:
        monkeypatch.delenv(key, raising=False)


def test_settings_load_from_explicit_env_file(tmp_path):
    path = tmp_path / "database.env"
    path.write_text(
        "DB_HOST=db.example.invalid\nDB_PORT=5433\nDB_NAME=test_database\nDB_USER=test_user\nDB_PASSWORD=synthetic-password\n"
    )
    settings = DatabaseSettings(_env_file=path)
    assert settings.db_host == "db.example.invalid"
    assert settings.db_port == 5433
    assert settings.db_name == "test_database"
    assert settings.db_user == "test_user"
    assert settings.url().password == "synthetic-password"
    assert "synthetic-password" not in repr(settings)


def test_missing_database_config_has_no_hardcoded_fallback():
    with pytest.raises(ValidationError) as error:
        DatabaseSettings(_env_file=None)
    assert {item["loc"][0] for item in error.value.errors()} == {
        "db_host",
        "db_port",
        "db_name",
        "db_user",
    }


def test_environment_overrides_env_file(tmp_path, monkeypatch):
    path = tmp_path / "database.env"
    path.write_text(
        "DB_HOST=localhost\nDB_PORT=5432\nDB_NAME=test_database\nDB_USER=test_user\n"
    )
    monkeypatch.setenv("DB_NAME", "override_database")
    assert DatabaseSettings(_env_file=path).db_name == "override_database"
