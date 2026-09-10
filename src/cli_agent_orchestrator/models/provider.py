from enum import Enum


class ProviderType(str, Enum):
    """Provider type enumeration."""

    KIRO_CLI = "kiro_cli"
    GROK_CLI = "grok_cli"
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    KIMI_CLI = "kimi_cli"
    COPILOT_CLI = "copilot_cli"
    OPENCODE_CLI = "opencode_cli"
    HERMES = "hermes"
    CURSOR_CLI = "cursor_cli"
    ANTIGRAVITY_CLI = "antigravity_cli"
    OMP = "omp"
    CLINE_CLI = "cline_cli"
    PI_CLI = "pi_cli"
    MINIMAX_CODE = "mcode"
    # F862 (#718): ChatGPT-web findings lane. A thin lifecycle/status provider
    # over the chatgpt_web_runner deterministic browser runner (D2/D13). Drives
    # the user's real chatgpt.com Plus web session via cloakbrowser + Playwright;
    # NEVER the OpenAI API. Certified only for the design_findings + general
    # cells (D11); refused on every gate position (D14 backstop).
    CHATGPT_WEB = "chatgpt_web"
    # Credentials-free mock provider for tests/CI (no real CLI binary).
    MOCK_CLI = "mock_cli"
