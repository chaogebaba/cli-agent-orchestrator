import shlex
from unittest.mock import patch

from cli_agent_orchestrator.models.terminal import ForkContext
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider


def context(mode="fork"):
    return ForkContext(mode=mode, session_uuid="11111111-1111-4111-8111-111111111111",
                       base_name="base", provider="codex", initial_preamble="[FRESH]")


@patch("cli_agent_orchestrator.providers.codex.load_agent_profile", side_effect=FileNotFoundError)
def test_codex_fork_uses_subcommand_without_prompt(_load):
    p = CodexProvider("a", "s", "w", fork_context=context())
    argv = shlex.split(p._build_codex_command())
    assert argv[:2] == ["codex", "fork"]
    assert argv[-1] == context().session_uuid


@patch("cli_agent_orchestrator.providers.grok_cli.GrokCliProvider._allocate_session_uuid",
       return_value="22222222-2222-4222-8222-222222222222")
def test_grok_mode_argv(_allocate):
    fork = GrokCliProvider("a", "s", "w", fork_context=context())
    argv = shlex.split(fork._build_grok_command())
    assert argv[-5:] == ["--resume", context().session_uuid, "--fork-session", "--session-id",
                         "22222222-2222-4222-8222-222222222222"]
    resume = GrokCliProvider("b", "s", "w", fork_context=context("resume"))
    rargv = shlex.split(resume._build_grok_command())
    assert rargv[-2:] == ["--resume", context().session_uuid]
    assert "--fork-session" not in rargv and "--session-id" not in rargv
