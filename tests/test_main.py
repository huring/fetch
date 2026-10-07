from unittest.mock import MagicMock, patch

from watcher import main


def test_once_mode_requires_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    assert main.main(["--once"]) == 1


def test_once_mode_runs_pipeline_and_exits(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))

    with patch("watcher.main.run_once", return_value={"run_id": 1}) as mock_run_once:
        with patch("watcher.main.Anthropic") as mock_anthropic:
            exit_code = main.main(["--once", "--dry-run"])

    assert exit_code == 0
    mock_run_once.assert_called_once()
    _, kwargs = mock_run_once.call_args
    assert kwargs.get("dry_run") is True or mock_run_once.call_args[0][-1] is True
