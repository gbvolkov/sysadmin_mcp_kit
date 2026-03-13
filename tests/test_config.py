from pathlib import Path

import pytest

from sysadmin_mcp_kit.config import AppSettings, _resolve_config_path



def test_resolve_config_path_finds_repo_config_from_src_directory(monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(repo_root / "src")

    resolved = _resolve_config_path()

    assert resolved == repo_root / "config" / "server.toml"

def test_app_settings_rejects_json_response(settings) -> None:
    data = settings.model_dump(mode="json")
    data["server"]["json_response"] = True

    with pytest.raises(ValueError, match=r"server\.json_response=true"):
        AppSettings.model_validate(data)

