"""Settings come from the repo's .env regardless of the working directory."""

from pathlib import Path

from app.core.config import ENV_FILE, Settings
from app.db.config import DatabaseSettings

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_env_file_is_the_repo_dotenv_not_cwd_relative():
    assert ENV_FILE == REPO_ROOT / ".env"
    assert ENV_FILE.is_absolute()


def test_both_settings_classes_use_the_same_env_file():
    assert Settings.model_config["env_file"] == ENV_FILE
    assert DatabaseSettings.model_config["env_file"] == ENV_FILE
