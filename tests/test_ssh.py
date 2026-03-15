from __future__ import annotations

from types import SimpleNamespace

from sysadmin_mcp_kit.config import AppSettings
from sysadmin_mcp_kit.ssh import (
    PasswordPromptRequest,
    PersistentShellSession,
    SSHService,
    _prepare_command_for_password_prompts,
    _prepare_command_for_persistent_password_prompts,
)


def test_prepare_command_for_password_prompts_rewrites_plain_sudo() -> None:
    rewritten = _prepare_command_for_password_prompts("sudo docker ps --format 'table {{.Names}}'")

    assert rewritten == "sudo -S -p '[sysadmin-mcp] password: ' docker ps --format 'table {{.Names}}'"


def test_prepare_command_for_password_prompts_respects_existing_sudo_modes() -> None:
    assert _prepare_command_for_password_prompts("sudo -n docker ps") == "sudo -n docker ps"
    assert _prepare_command_for_password_prompts("sudo -A docker ps") == "sudo -A docker ps"
    assert _prepare_command_for_password_prompts("sudo -S docker ps") == "sudo -p '[sysadmin-mcp] password: ' -S docker ps"


def test_prepare_command_for_password_prompts_preserves_shell_operators() -> None:
    rewritten = _prepare_command_for_password_prompts("sudo docker ps | cat")

    assert rewritten == "sudo -S -p '[sysadmin-mcp] password: ' docker ps | cat"


def test_prepare_command_for_persistent_password_prompts_uses_askpass_wrapper() -> None:
    prompts: list[PasswordPromptRequest] = []

    def callback(request: PasswordPromptRequest) -> str:
        prompts.append(request)
        return "opensesame"

    prepared = _prepare_command_for_persistent_password_prompts(
        "sudo docker ps",
        timeout_seconds=90,
        progress_callback=lambda _progress, _message: None,
        password_prompt_callback=callback,
    )

    assert len(prompts) == 1
    assert prompts[0].prompt == "[sysadmin-mcp] password:"
    assert 'export SUDO_ASKPASS="$__sysadmin_mcp_askpass"' in prepared
    assert "sudo -A -p '[sysadmin-mcp] password: ' docker ps" in prepared
    assert "printf '%s\\n' opensesame" in prepared


def test_persistent_shell_build_script_embeds_prepared_askpass_command() -> None:
    channel = SimpleNamespace(closed=False, exit_status_ready=lambda: False)
    session = PersistentShellSession(
        target_id="cheetan",
        client=SimpleNamespace(),
        sock=None,
        channel=channel,
        current_directory="/etc",
        progress_interval_seconds=0.1,
    )
    prepared = _prepare_command_for_persistent_password_prompts(
        "sudo docker ps",
        timeout_seconds=90,
        progress_callback=lambda _progress, _message: None,
        password_prompt_callback=lambda _request: "opensesame",
    )

    script = session._build_script(prepared, "TOKEN", "/etc")

    assert "SUDO_ASKPASS" in script
    assert "sudo -A -p" in script
    assert "__SYSADMIN_MCP_STATUS_TOKEN__" in script


def test_validate_readable_file_path_allows_paths_outside_allowlist(settings: AppSettings) -> None:
    service = SSHService(settings)
    target = service.get_target("cheetan")

    assert service._validate_readable_file_path(target, "/var/log/app.log") == "/var/log/app.log"
