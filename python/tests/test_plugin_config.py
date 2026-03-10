"""Tests for the plugin config loader (plugin/scripts/config.py)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "plugin" / "scripts"))

import config as plugin_config
from config import detect_project, load_config


@pytest.fixture(autouse=True)
def reset_project_cache():
    """Clear the detect_project cache between tests to prevent cross-test pollution."""
    plugin_config._project_cache.clear()
    yield
    plugin_config._project_cache.clear()


# ─── load_config ──────────────────────────────────────────────────────────────

class TestLoadConfig:
    def test_returns_none_when_config_file_does_not_exist(self, tmp_path):
        missing = tmp_path / "config.json"
        with patch.object(plugin_config, "CONFIG_FILE", missing):
            assert load_config() is None

    def test_returns_none_when_server_url_is_empty(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"server_url": ""}))
        with patch.object(plugin_config, "CONFIG_FILE", cfg_file):
            assert load_config() is None

    def test_returns_none_when_server_url_is_missing(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"api_key": "key123"}))
        with patch.object(plugin_config, "CONFIG_FILE", cfg_file):
            assert load_config() is None

    def test_returns_config_dict_when_valid(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"server_url": "http://localhost:8091"}))
        with patch.object(plugin_config, "CONFIG_FILE", cfg_file):
            result = load_config()
        assert result is not None
        assert isinstance(result, dict)
        assert result["server_url"] == "http://localhost:8091"

    def test_merges_user_config_with_defaults(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "server_url": "http://my-server:8091",
            "api_key": "my-api-key",
        }))
        with patch.object(plugin_config, "CONFIG_FILE", cfg_file):
            result = load_config()
        assert result["api_key"] == "my-api-key"
        # Default value from DEFAULT_CONFIG should be present
        assert "skip_tools" in result
        assert "bash_output_max_kb" in result

    def test_user_config_overrides_defaults(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "server_url": "http://srv:8091",
            "bash_output_max_kb": 100,
        }))
        with patch.object(plugin_config, "CONFIG_FILE", cfg_file):
            result = load_config()
        assert result["bash_output_max_kb"] == 100

    def test_returns_none_for_invalid_json(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("not valid json {{{")
        with patch.object(plugin_config, "CONFIG_FILE", cfg_file):
            assert load_config() is None

    def test_returns_none_for_unreadable_file(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"server_url": "http://x"}))
        cfg_file.chmod(0o000)
        with patch.object(plugin_config, "CONFIG_FILE", cfg_file):
            result = load_config()
        cfg_file.chmod(0o644)  # restore so tmp_path cleanup works
        assert result is None


# ─── detect_project ───────────────────────────────────────────────────────────

class TestDetectProject:
    def test_extracts_repo_name_from_https_url(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/user/my-repo.git\n"
        with patch("subprocess.run", return_value=mock_result):
            name = detect_project("/some/path")
        assert name == "my-repo"

    def test_extracts_repo_name_from_ssh_url(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "git@github.com:user/open-brain.git\n"
        with patch("subprocess.run", return_value=mock_result):
            name = detect_project("/some/path")
        assert name == "open-brain"

    def test_strips_dot_git_suffix(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/org/some-project.git\n"
        with patch("subprocess.run", return_value=mock_result):
            name = detect_project("/some/path")
        assert name == "some-project"
        assert not name.endswith(".git")

    def test_works_with_url_without_dot_git(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/user/my-project\n"
        with patch("subprocess.run", return_value=mock_result):
            name = detect_project("/some/path")
        assert name == "my-project"

    def test_falls_back_to_directory_name_when_git_fails(self, tmp_path):
        project_dir = tmp_path / "my-cool-project"
        project_dir.mkdir()
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            name = detect_project(str(project_dir))
        assert name == "my-cool-project"

    def test_falls_back_when_subprocess_raises(self, tmp_path):
        project_dir = tmp_path / "fallback-project"
        project_dir.mkdir()
        with patch("subprocess.run", side_effect=Exception("git not found")):
            name = detect_project(str(project_dir))
        assert name == "fallback-project"

    def test_uses_cwd_when_no_path_given(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            with patch("config.os.getcwd", return_value="/home/user/my-workspace"):
                name = detect_project(None)
        assert name == "my-workspace"
