from sysadmin_mcp_kit.config import CommandPolicySettings
from sysadmin_mcp_kit.policy import CommandPolicy


def test_command_policy_detects_sensitive_and_blocked_patterns() -> None:
    policy = CommandPolicy(
        CommandPolicySettings(
            sensitive_patterns=[r"(?i)\bsudo\b"],
            blocked_patterns=[r"(?i)rm\s+-rf\s+/\s*$"],
            confirmation_token_length=8,
        )
    )

    sensitive = policy.evaluate("sudo systemctl restart nginx")
    blocked = policy.evaluate("rm -rf /")

    assert sensitive.sensitive is True
    assert sensitive.blocked is False
    assert blocked.blocked is True
    assert blocked.sensitive is False
    assert len(policy.confirmation_token("sudo systemctl restart nginx")) == 8
