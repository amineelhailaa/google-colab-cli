from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.client import RuntimeProxyInfo

runner = CliRunner()


def assignment(endpoint="external-runtime"):
    return SimpleNamespace(
        endpoint=endpoint,
        runtime_proxy_info=RuntimeProxyInfo(
            token="fresh-token",
            tokenExpiresInSeconds=3600,
            url="https://example.invalid",
        ),
        variant=SimpleNamespace(name="GPU"),
        accelerator=SimpleNamespace(value="T4"),
        machine_shape=SimpleNamespace(name="STANDARD"),
    )


@patch("colab_cli.commands.files.ContentsClient")
def test_active_runtime_registers_and_downloads(contents, mock_common_state):
    mock_common_state.client.list_assignments.return_value = [assignment()]
    result = runner.invoke(
        app, ["download", "--active-runtime", "-s", "pycharm", "remote", "local"]
    )
    assert result.exit_code == 0, result.output
    saved = mock_common_state.store.add.call_args.args[0]
    assert saved.name == "pycharm"
    assert saved.endpoint == "external-runtime"
    assert saved.token == "fresh-token"
    assert saved.token_expires_at is not None
    contents.assert_called_once_with(saved)
    contents.return_value.download.assert_called_once_with("remote", "local")
    mock_common_state.resolve_session.assert_not_called()
    mock_common_state.client.assign.assert_not_called()


@pytest.mark.parametrize("runtimes", [[], [assignment("a"), assignment("b")]])
@patch("colab_cli.commands.files.ContentsClient")
def test_active_runtime_requires_unambiguous_selection(
    contents, runtimes, mock_common_state
):
    mock_common_state.client.list_assignments.return_value = runtimes
    result = runner.invoke(app, ["download", "--active-runtime", "remote", "local"])
    assert result.exit_code == 1
    assert "active runtime" in result.output.lower()
    mock_common_state.store.add.assert_not_called()
    contents.assert_not_called()


@patch("colab_cli.commands.files.ContentsClient")
def test_active_runtime_endpoint_selects_one(contents, mock_common_state):
    mock_common_state.client.list_assignments.return_value = [
        assignment("a"),
        assignment("b"),
    ]
    result = runner.invoke(
        app, ["download", "--active-runtime", "--endpoint", "b", "remote", "local"]
    )
    assert result.exit_code == 0, result.output
    saved = mock_common_state.store.add.call_args.args[0]
    assert saved.name == "active"
    assert saved.endpoint == "b"


@pytest.mark.parametrize(
    "args", [["--endpoint", "missing"], ["--active-runtime", "--endpoint", "missing"]]
)
@patch("colab_cli.commands.files.ContentsClient")
def test_invalid_endpoint_does_not_download(contents, args, mock_common_state):
    mock_common_state.client.list_assignments.return_value = [assignment()]
    result = runner.invoke(app, ["download", *args, "remote", "local"])
    assert result.exit_code != 0
    contents.assert_not_called()
    mock_common_state.store.add.assert_not_called()


@patch("colab_cli.commands.files.ContentsClient")
def test_active_runtime_preserves_existing_kernel(contents, mock_common_state):
    from colab_cli.state import SessionState

    existing = SessionState(
        name="pycharm",
        endpoint="external-runtime",
        token="old",
        url="old",
        kernel_id="kernel-1",
    )
    mock_common_state.store.get.return_value = existing
    mock_common_state.client.list_assignments.return_value = [assignment()]
    result = runner.invoke(
        app, ["download", "--active-runtime", "-s", "pycharm", "remote", "local"]
    )
    assert result.exit_code == 0, result.output
    saved = mock_common_state.store.add.call_args.args[0]
    assert saved.kernel_id == "kernel-1"
    assert saved.token == "fresh-token"


@patch("colab_cli.commands.files.ContentsClient")
def test_active_runtime_rebind_discards_old_kernel(contents, mock_common_state):
    from colab_cli.state import SessionState

    mock_common_state.store.get.return_value = SessionState(
        name="pycharm",
        endpoint="old-runtime",
        token="old",
        url="old",
        kernel_id="old-kernel",
    )
    mock_common_state.client.list_assignments.return_value = [assignment()]
    result = runner.invoke(
        app, ["download", "--active-runtime", "-s", "pycharm", "remote", "local"]
    )
    assert result.exit_code == 0, result.output
    saved = mock_common_state.store.add.call_args.args[0]
    assert saved.endpoint == "external-runtime"
    assert saved.kernel_id is None
