"""Pi CLI provider implementation — persistent regular-TUI in a tmux pane.

This provider drives Pi (``pi`` 0.85.1+, https://github.com/possibilities/pi or
the vendored coding agent) as a long-lived interactive TUI in a tmux window, one
window per worker.  Unlike the one-shot dispatcher used by ``cline_cli``, Pi's
non-interactive ``--print`` mode buffers all output until completion and was
observed to stall for well over two minutes on the ``cline-pass/glm-5.3-flash``
model (live-probed 2026-09-07); the regular TUI, by contrast, is responsive
(~10s/turn) AND keeps a single long-lived MCP connection warm across many turns.
That persistence is what lets a parked Pi worker keep reading IDLE and keep
receiving inbox mail across turns (the F794 lesson) — a fresh ``--print`` process
per turn would tear the MCP connection down between messages.

Architecture
------------
* **Launch** (``initialize``): after the shell is ready, ``pi --tui-mode regular``
  is sent to the pane with an explicit, shell-safe argv (absolute ``pi`` path,
  resolved model + thinking level, a per-worker system-prompt file, and a
  per-worker MCP config file).  We then wait for the pane to render its idle
  chrome (two box rules + a footer) before reporting ready.

* **Status** (``get_status``): the fork's native-status fusion runs first
  (``_resolve_native_status``); on tmux it returns None, so we parse the
  Pi TUI chrome from the rolling pane buffer:
    - a ``── ⠧ Working ──`` line (spinner + "Working" between box rules)
      → PROCESSING;
    - the idle chrome (two ``────`` rules + a ``…%/…(auto)`` footer, no
      "Working") → COMPLETED if a task was dispatched, else IDLE;
    - a startup/authorization error banner → ERROR.

* **MCP** (``send_message`` callbacks): Pi has native MCP support via the
  ``pi-mcp-adapter`` package (already listed in ``~/.pi/agent/settings.json``),
  which registers a ``--mcp-config <path>`` flag accepting a standard
  ``{"mcpServers": {...}}`` JSON document.  We materialize a per-worker
  ``mcp.json`` with the bundled ``cao-mcp-server`` command (resolved
  PATH-independently) plus this worker's ``CAO_TERMINAL_ID`` / ``CAO_TERMINAL_TOKEN``
  / ``CAO_ENDPOINT`` — the exact same identity injection ``cline_cli`` performs
  for its ``cline_mcp_settings.json``.  The config is passed via ``--mcp-config``
  WITHOUT ``PI_MCP_CONFIG_MODE=exclusive`` (exclusive mode ignores the override
  path — live-probed 2026-09-07).  Headless tool calls require no human approval
  under ``--no-approve`` (live-probed: the worker connected the server lazily and
  called the tool unattended).  The message/callback path is therefore MCP
  stdio→HTTP only — it never touches ``/data`` (the F797 lesson).

Key flags (verified via ``pi --help``, pi 0.85.1, live-probed 2026-09-07):
  --tui-mode regular       : line-oriented TUI (default), scrollback-friendly
  --model <provider/id>    : model override (e.g. cline-pass/glm-5.3-flash)
  --thinking <level>       : reasoning effort (off|minimal|low|medium|high|xhigh|max)
  --no-approve, -na        : do not trust project-local files / auto-approve run
  --no-context-files, -nc  : do not auto-load AGENTS.md / CLAUDE.md
  --session-id <id>        : exact project session id (created if missing)
  --session-dir <dir>      : session storage/lookup directory
  --append-system-prompt <text|file> : append system prompt (file path accepted)
  --mcp-config <path>      : MCP config override (pi-mcp-adapter flag; requires
                             extension discovery ON, so we do NOT pass --no-extensions)
  --exclude-tools <a,b>    : native denylist of tool names (hard enforcement)
  --no-skills, -ns         : disable skills discovery
  --no-prompt-templates, -np : disable prompt-template discovery

Startup config files live under ``CAO_HOME_DIR`` (``~/.aws/cli-agent-orchestrator``
by default) — never ``/data`` — and are removed on ``cleanup``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.services.settings_service import (
    get_provider_defaults,
    get_provider_profile_defaults,
    get_server_settings,
    resolve_provider_string_option,
    resolve_reasoning_effort,
)
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.mcp_resolution import resolve_cao_mcp_command
from cli_agent_orchestrator.utils.terminal import wait_for_shell
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)

# Absolute path is required because cao-server runs from systemd, where the
# user's shell PATH (with ~/.bun/bin) is not present (memory rule 2026-09-07).
PI_BINARY = str(Path.home() / ".bun" / "bin" / "pi")

# Per-worker runtime root under CAO_HOME_DIR (NOT /data — see module docstring
# and the F797 lesson).  One subdirectory per terminal holds the transient
# system-prompt and MCP-config files.
PI_RUNTIME_ROOT = CAO_HOME_DIR / "pi"

# ─── Status detection ─────────────────────────────────────────────────────────
# All patterns run against ``strip_terminal_escapes(buffer)`` output, which
# normalizes the raw pipe-pane byte stream into clean, line-oriented text
# (calibrated against live pi 0.85.1 captures, 2026-09-07).

# A Pi composer/editor box rule: a run of box-drawing horizontals (or ASCII
# dashes as a fallback).  Pi draws two of these around the footer when idle.
_EDITOR_RULE = re.compile(r"^\s*[─━—-]{20,}\s*$")

# The braille spinner frames Pi cycles through on its active "Working" rule
# row.  The ten canonical frames (#703 F847) are ``⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏``; we anchor on
# the full braille-patterns Unicode block (U+2800–U+28FF) so every frame — and
# the wider ``⠿``-style glyphs Pi has been observed to draw (#700/#701 corpus) —
# counts, while ordinary ASCII prose that merely says "Working" does not.
_BRAILLE_SPINNER = r"\u2800-\u28ff"

# The active spinner line while Pi is working, e.g. "── ⠧ Working ──────" or
# (mid-redraw) "⠴ Working ────".  This is a WHOLE ROW the TUI draws — a braille
# spinner glyph adjacent to ``Working`` with a Pi box rule (``──…`` run) on the
# SAME row, on EITHER side of the glyph.  The spinner glyph varies frame to
# frame, so all frames in the braille block are accepted; the rule may LEAD
# (``── ⠧ Working ──``) or TRAIL (mid-redraw ``⠴ Working ────``, spinner-first)
# so a redraw that draws the trailing rule first still matches (#703 F847, the
# genuine false-idle frame the base rule-must-lead regex missed).
#
# F847 r2 (#703): the match is now BRAILLE-ONLY and a WHOLE-ROW anchor
# (``^…$`` under MULTILINE), and a same-row box rule is REQUIRED, for two
# reasons the r1 shape got wrong (codex EMPIRICAL-GATE-NO):
#   1. r1's first alternative made the rule OPTIONAL, so bare
#      ``⠦ Working on the summary now`` PROSE fired PROCESSING. Requiring a
#      same-row rule and anchoring the row fixes that overmatch.
#   2. r1 also carried permissive ``\S? Working`` NON-braille alternatives, so a
#      stray non-spinner glyph matched and — worse — those alternatives MASKED
#      the braille class entirely (narrowing the braille set to one glyph still
#      matched every spinner-first frame via ``\S?``, so the ledger's glyph
#      mutant would survive). Every real pi spinner in the capture corpus uses a
#      braille frame, so the non-braille alternatives are dropped: the braille
#      class is now the sole gate on which glyphs count as a live spinner.
# Positional scoping to the live bottom-of-viewport status region (and
# fence-dropping) is done by ``_live_working_spinner`` (r1's whole-buffer
# ``.search`` overmatched a fenced quote and a stale spinner 40 rows above the
# composer).
_RULE_RUN = r"[─━—\-]{2,}"
# F847 r3 (#703): the row is now anchored at BOTH ends (``^…$`` under MULTILINE).
# r2 left the row unanchored at the tail (``Working\b`` with no ``$``), and
# ``_live_working_spinner`` matches with ``re.match`` — which accepts a matching
# PREFIX — so a transcript row that merely OPENS with the spinner chrome, e.g.
# ``── ⠦ Working ── was quoted in the previous answer.``, fired PROCESSING even
# though the report and comments both claim a WHOLE-ROW anchor (codex r2
# EMPIRICAL-GATE-NO, the unmutated missing end anchor). The genuine live rows
# end in the composer box rule with no trailing prose:
#   rule-leading:  ── ⠧ Working ──────────  (trailing rule optional in r2 draw)
#   spinner-first: ⠴ Working ────────────   (trailing rule required)
# so after ``Working`` the ONLY thing a live row may carry to end-of-line is
# whitespace and an optional box-rule run. ``_WORKING_TAIL`` encodes exactly
# that and pins ``$``; any trailing prose (a sentence, a quote, more words) now
# fails the whole-row match and is treated as transcript.
_WORKING_TAIL = r"[ \t]*(?:" + _RULE_RUN + r"[ \t]*)?$"
_WORKING_ROW = re.compile(
    # rule-leading: ── ⠧ Working ─(─…)?  then whitespace/optional-rule to EOL
    r"^[ \t]*" + _RULE_RUN + r"[ \t]*[" + _BRAILLE_SPINNER + r"][ \t]*Working\b" + _WORKING_TAIL,
    re.IGNORECASE | re.MULTILINE,
)
# Backwards-compatible module alias: ``_WORKING`` is referenced by the r1 tests
# and by ``_has_idle_chrome``/``extract_last_message_from_script`` as a cheap
# "does this pane show a working spinner at all" predicate. It now points at the
# whole-row anchor; positional/fence scoping is applied only in get_status via
# ``_live_working_spinner``.
_WORKING = _WORKING_ROW

# How many trailing (non-blank-stripped) rows count as Pi's LIVE status region.
# Pi's live working spinner sits in the composer box at the bottom of the
# viewport, directly above the footer/context readout; a spinner glyph far above
# that region is stale transcript, not the live state (#703 overmatch). 12 rows
# comfortably covers the composer box + footer + MCP line while excluding a
# spinner scrolled tens of rows up.
_LIVE_TAIL_ROWS = 12

# The footer context/budget readout, e.g. "0.3%/1.0M (auto)" or "?/1.0M".
# Presence of this plus two rules is Pi's idle/completed chrome.
_FOOTER_CONTEXT = re.compile(r"(?:\d+(?:\.\d+)?%|\?)/\d+(?:\.\d+)?[kKmM]?\b")

# Startup / authorization / crash banners that mean the launch never reached a
# usable prompt.  Kept narrow so ordinary agent output mentioning "error" is not
# misread as a launch failure.
_STARTUP_ERROR = re.compile(
    r"(?:command not found:.*\bpi\b"
    r"|No such file or directory:.*\bpi\b"
    r"|^\s*(?:Error|ERROR|Fatal|FATAL):\s+\S"
    r"|Traceback \(most recent call last\):"
    r"|pi:\s+error:)",
    re.IGNORECASE | re.MULTILINE,
)


class PiCliProvider(BaseProvider):
    """Provider for Pi's persistent regular-TUI, driven inside a tmux pane.

    Status detection parses the Pi TUI chrome; the callback path uses Pi's
    native MCP (``pi-mcp-adapter``) pointed at a per-worker ``cao-mcp-server``
    config, so ``send_message`` works over stdio→HTTP with no ``/data``
    dependency.
    """

    # F808 (#665): opt into the cached-UNKNOWN-at-rest self-heal in the
    # StatusMonitor (get_raw_status). Pi's persistent alt-screen TUI stops
    # feeding the tmux FIFO once its idle frame is drawn, so a parked worker's
    # cached status stays UNKNOWN and IDLE-gated inbox delivery never fires.
    # Setting this lets get_raw_status re-detect from a fresh pane capture via
    # get_status(), which is line-oriented and safe on a rendered snapshot (the
    # same detector _resolve_buffer already runs on a live pane read).
    supports_direct_status_probe: bool = True

    # F843 (#700): opt into the F611 condition classifier so pi's ClinePass 429
    # INFERENCE_CAP_ERROR banner is detected as a CAPPED condition and the ONE
    # [CONDITION] notice reaches the supervisor seat (the same delivery seam
    # codex's usage_limit_hard uses). SEPARATE from get_status/fusion — a
    # condition is never a TerminalStatus member (D1).
    condition_provider_key = "pi_cli"

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list[str]] = None,
        skill_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._agent_profile = agent_profile
        self._model = model
        self._initialized = False
        self._resolved_model: Optional[str] = None
        self._resolved_reasoning_effort: Optional[str] = None
        self._tui_processing_seen = False

        self.runtime_dir = PI_RUNTIME_ROOT / terminal_id
        self.session_dir = self.runtime_dir / "sessions"
        self.prompt_path = self.runtime_dir / "system-prompt.md"
        self.mcp_config_path = self.runtime_dir / "mcp.json"

    # ── Effective-resolution properties (F777 #634) ──────────────────────────

    @property
    def resolved_model(self) -> Optional[str]:
        """Return the effective model resolved during command build."""
        return self._resolved_model

    @property
    def resolved_reasoning_effort(self) -> Optional[str]:
        """F777 (#634): the effective reasoning effort resolved at command build."""
        return self._resolved_reasoning_effort

    @property
    def paste_enter_count(self) -> int:
        """Pi submits a bracketed paste with a single Enter."""
        return 1

    # ── Resolution helpers (mirror cline_cli's precedence chain) ─────────────

    def _load_profile(self) -> Any | None:
        if not self._agent_profile:
            return None
        try:
            return load_agent_profile(self._agent_profile)
        except (FileNotFoundError, RuntimeError) as exc:
            logger.debug(
                "Profile '%s' not loadable; falling back to providers.toml: %s",
                self._agent_profile,
                exc,
            )
            return None

    def _resolve_model(self, profile: Any | None) -> Optional[str]:
        """Resolve model: spawn override > providers.toml > profile field.

        Mirrors cline_cli's chain: explicit ``model`` kwarg (from assign/handoff)
        > ``[pi_cli.profiles.<name>] model`` > ``[pi_cli] model`` > profile.model.
        """
        if self._model:
            return self._model
        provider_defaults = get_provider_defaults("pi_cli")
        profile_name = getattr(profile, "name", None) or self._agent_profile
        profile_defaults = get_provider_profile_defaults(provider_defaults, profile_name)
        return resolve_provider_string_option(
            profile_defaults,
            provider_defaults,
            profile,
            "model",
            "model",
        )

    def _resolve_thinking(self, profile: Any | None) -> Optional[str]:
        """Resolve the ``--thinking`` level via the F777 shared resolver.

        Precedence: ``[pi_cli.profiles.<name>].reasoning_effort`` >
        ``[pi_cli].reasoning_effort`` > ``profile.reasoningEffort`` > the
        ``pi_cli`` built-in default (``high``).  An explicit empty string at a
        TOML layer clears the flag (persisted None), exactly like every other
        provider that routes through :func:`resolve_reasoning_effort`.

        Valid levels: off|minimal|low|medium|high|xhigh|max (pi --help).
        """
        provider_defaults = get_provider_defaults("pi_cli")
        profile_name = getattr(profile, "name", None) or self._agent_profile
        profile_defaults = get_provider_profile_defaults(provider_defaults, profile_name)
        return resolve_reasoning_effort("pi_cli", profile_defaults, provider_defaults, profile)

    # ── Config materialization ───────────────────────────────────────────────

    @staticmethod
    def _ensure_private_dir(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _write_prompt(self, profile: Any | None) -> Path:
        """Write the per-worker system prompt (profile prompt + skill catalog)."""
        base_prompt = ""
        if profile is not None:
            base_prompt = (
                getattr(profile, "system_prompt", None) or getattr(profile, "prompt", None) or ""
            ).strip()
        prompt = self._apply_skill_prompt(base_prompt)
        self._ensure_private_dir(self.prompt_path.parent)
        self.prompt_path.write_text(prompt, encoding="utf-8")
        try:
            self.prompt_path.chmod(0o600)
        except OSError:
            pass
        return self.prompt_path

    def _write_mcp_config(self) -> Path:
        """Materialize the per-worker ``mcp.json`` for pi-mcp-adapter.

        Emits a standard ``{"mcpServers": {...}}`` document naming
        ``cao-mcp-server`` with this worker's identity injected into ``env``.
        The command is resolved PATH-independently via
        :func:`resolve_cao_mcp_command` (``persisted=True`` — the file is read
        by pi at a later launch, so prefer the stable on-PATH launcher over the
        versioned interpreter sibling).
        """
        profile = self._load_profile()
        mcp_servers = getattr(profile, "mcpServers", None) if profile is not None else None
        cao_entry: dict[str, Any] = {}
        if isinstance(mcp_servers, dict):
            raw = mcp_servers.get("cao-mcp-server")
            if isinstance(raw, dict):
                cao_entry = raw
            elif raw is not None and hasattr(raw, "model_dump"):
                cao_entry = raw.model_dump(exclude_none=True)
        raw_command = str(cao_entry.get("command", "cao-mcp-server"))
        raw_args = list(cao_entry.get("args", []) or [])
        command, args = resolve_cao_mcp_command(raw_command, raw_args, persisted=True)

        env_block: dict[str, str] = {"CAO_TERMINAL_ID": self.terminal_id}
        terminal_token = os.environ.get("CAO_TERMINAL_TOKEN", "")
        if terminal_token:
            env_block["CAO_TERMINAL_TOKEN"] = terminal_token
        from cli_agent_orchestrator.utils.http import resolve_endpoint

        env_block["CAO_ENDPOINT"] = resolve_endpoint()
        instance_id = os.environ.get("CAO_INSTANCE_ID", "")
        if instance_id:
            env_block["CAO_INSTANCE_ID"] = instance_id

        payload = {
            "mcpServers": {
                "cao-mcp-server": {
                    "command": command,
                    "args": args,
                    "env": env_block,
                }
            },
            "settings": {"requestTimeoutMs": _resolve_pi_mcp_timeout_ms()},
        }
        self._ensure_private_dir(self.mcp_config_path.parent)
        tmp = self.mcp_config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(str(tmp), str(self.mcp_config_path))
        logger.info(
            "pi worker %s: mcp config materialized (cao-mcp-server resolved: %s)",
            self.terminal_id,
            command,
        )
        return self.mcp_config_path

    def _build_pi_command(self) -> str:
        """Build Pi's explicit, shell-safe regular-TUI launch command."""
        profile = self._load_profile()
        self._ensure_private_dir(self.runtime_dir)
        self._ensure_private_dir(self.session_dir)
        self._write_prompt(profile)
        self._write_mcp_config()

        command_parts = [
            PI_BINARY,
            "--tui-mode",
            "regular",
            "--no-approve",
            "--no-context-files",
            # NOTE: do NOT pass --no-extensions. The cao-mcp-server bridge is
            # exposed through the pi-mcp-adapter extension (npm:pi-mcp-adapter),
            # which is loaded via extension discovery and REGISTERS the
            # ``--mcp-config`` flag. ``--no-extensions`` disables that discovery
            # and pi then rejects ``--mcp-config`` as an unknown option
            # (live-probed 2026-09-07), breaking the send_message callback path.
            "--no-skills",
            "--no-prompt-templates",
            "--session-id",
            self.terminal_id,
            "--session-dir",
            str(self.session_dir),
            "--append-system-prompt",
            str(self.prompt_path),
            "--mcp-config",
            str(self.mcp_config_path),
        ]

        model = self._resolve_model(profile)
        self._resolved_model = model if (isinstance(model, str) and model) else None
        if isinstance(model, str) and model:
            command_parts.extend(["--model", model])

        thinking = self._resolve_thinking(profile)
        self._resolved_reasoning_effort = (
            thinking if isinstance(thinking, str) and thinking else None
        )
        if isinstance(thinking, str) and thinking:
            command_parts.extend(["--thinking", thinking])

        if self._allowed_tools is not None and "*" not in self._allowed_tools:
            from cli_agent_orchestrator.utils.tool_mapping import get_disallowed_tools

            disallowed = get_disallowed_tools("pi_cli", self._allowed_tools)
            if disallowed:
                command_parts.extend(["--exclude-tools", ",".join(disallowed)])

        return shlex.join(command_parts)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> bool:
        """Wait for the shell, launch Pi's TUI, and wait for its idle chrome."""
        import asyncio

        from cli_agent_orchestrator.services.status_monitor import status_monitor

        profile = self._load_profile()
        init_timeout = self.get_init_timeout(profile)
        if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        self.shell_baseline = get_backend().get_pane_current_command(
            self.session_name, self.window_name
        )

        command = await asyncio.to_thread(self._build_pi_command)
        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, command)

        # Wait until Pi has rendered a usable frame: either its idle chrome
        # (rules + footer) or an early error banner. We poll the pane buffer
        # directly rather than trusting a fixed sleep, because model-catalog and
        # MCP init can add a few seconds before the first frame.
        deadline = time.time() + float(init_timeout)
        while time.time() < deadline:
            buffer = self._read_pane()
            clean = strip_terminal_escapes(buffer)
            if _STARTUP_ERROR.search(clean):
                raise RuntimeError(
                    f"Pi failed to launch for terminal {self.terminal_id}: startup error banner"
                )
            if self._has_idle_chrome(clean):
                self._initialized = True
                return True
            await asyncio.sleep(0.5)

        raise TimeoutError(f"Pi TUI initialization timed out after {init_timeout}s")

    def mark_input_received(self) -> None:
        """Reset the per-turn processing latch after a message is dispatched."""
        super().mark_input_received()
        self._tui_processing_seen = False

    # ── Status detection ──────────────────────────────────────────────────────

    def _read_pane(self) -> str:
        """Best-effort read of the current pane content (never raises)."""
        try:
            return get_backend().get_history(self.session_name, self.window_name) or ""
        except Exception as exc:  # pragma: no cover - backend hiccup
            logger.debug("pi worker %s: pane read failed: %s", self.terminal_id, exc)
            return ""

    def _resolve_buffer(self, buffer: Optional[str]) -> str:
        """Resolve the buffer ``get_status()`` parses; live-read when empty.

        Pi runs as a persistent alt-screen TUI and does NOT feed the tmux FIFO
        ``pipe-pane`` push pipeline once it has drawn its frame — so at rest the
        StatusMonitor's pushed buffer for a parked pi worker is empty. The base
        resolver only falls back to a live ``get_history()`` read for
        event-inbox backends (herdr); on tmux it passes the empty buffer through
        unchanged, and ``get_status`` then returns UNKNOWN even though the live
        pane shows idle chrome. Because inbox delivery is IDLE-gated, that parked
        worker would never receive its callbacks (F798 r2 B-1).

        Override: when the pushed buffer is empty/whitespace, do ONE live pane
        read before classifying. A non-empty pushed buffer is passed through
        unchanged (no extra backend round-trip), so this only costs a capture
        when there was nothing to classify anyway.
        """
        if buffer and buffer.strip():
            return buffer
        return self._read_pane()

    @staticmethod
    def _live_working_spinner(clean: str) -> bool:
        """Return whether the LIVE bottom-of-viewport status region shows Pi's
        working spinner row.

        F847 r2 (#703): the r1 code matched ``_WORKING`` anywhere in the whole
        cleaned buffer, which overmatched (a) a fenced/quoted spinner row a human
        pasted, and (b) a stale spinner scrolled tens of rows above the current
        composer. Pi's genuine live spinner is a WHOLE ROW drawn in the composer
        box at the bottom of the viewport, directly above the footer. So the
        match is scoped, with the SAME discipline as the #693 footer classifier
        (position-anchored, fence-excluded, whole-row):

        1. Drop rows inside a Markdown code fence (a pasted/quoted spinner is
           transcript, never the live row — mirrors ``condition._pi_live_rows``).
        2. Strip trailing whitespace-only padding (pi pads the pane bottom).
        3. Only the last ``_LIVE_TAIL_ROWS`` rows — the live status region — are
           eligible; a spinner further up is stale transcript.
        4. A row in that window is the live spinner only if it matches the
           whole-row ``_WORKING_ROW`` anchor (a braille/marker + ``Working`` with
           a box rule on the same row), never bare prose that merely says
           "Working".
        """
        lines = clean.splitlines()
        # 1) drop fenced rows (quoted spinner is not live)
        unfenced: list[str] = []
        in_fence = False
        for row in lines:
            if row.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            unfenced.append(row)
        # 2) strip trailing blank padding so the tail lands on real chrome
        while unfenced and not unfenced[-1].strip():
            unfenced.pop()
        # 3) bottom-of-viewport window only
        tail = unfenced[-_LIVE_TAIL_ROWS:]
        # 4) whole-row spinner anchor within the live window
        return any(_WORKING_ROW.match(row) for row in tail)

    @staticmethod
    def _has_idle_chrome(clean: str) -> bool:
        """Return whether the stripped buffer shows Pi's idle/completed chrome.

        Idle chrome = at least two composer box rules in the tail AND a footer
        context/budget readout, with no active "Working" spinner.

        Pi's regular TUI renders the composer/footer near the TOP when the
        conversation is short and PADS the pane with trailing blank lines (a real
        box capture showed ~24 whitespace-only lines after the footer). A naive
        ``lines[-25:]`` tail is then all blanks and misses the chrome entirely,
        so we STRIP trailing blank/whitespace-only lines BEFORE taking the tail
        window. Blank lines interspersed within the chrome are preserved.
        """
        if PiCliProvider._live_working_spinner(clean):
            return False
        lines = clean.splitlines()
        # Drop trailing whitespace-only lines (pi's bottom padding) so the tail
        # window lands on the real chrome rather than the blank pad.
        while lines and not lines[-1].strip():
            lines.pop()
        tail = lines[-25:]
        rule_count = sum(bool(_EDITOR_RULE.match(line)) for line in tail)
        has_footer = bool(_FOOTER_CONTEXT.search("\n".join(tail)))
        return rule_count >= 2 and has_footer

    def get_status(self, buffer: str) -> TerminalStatus:
        """Detect Pi's terminal state from the pane buffer.

        Native-status fusion (herdr) runs first; on tmux it returns None and we
        parse the Pi TUI chrome:
          - "Working" spinner present            → PROCESSING
          - idle chrome (rules + footer, no spinner):
                task dispatched + a processing frame seen → COMPLETED
                otherwise                                 → IDLE
          - startup/authorization error banner    → ERROR
          - no recognizable chrome yet            → UNKNOWN (pre-init/transient)

        F844 (#701): status is RE-DERIVED from the live pane every poll and an
        ``error`` verdict is NOT sticky. A RUNTIME error banner (notably the
        ClinePass 429 ``Error: 429: {…}`` / ``Error: Retry failed after 3
        attempts`` lines — see #700, which classifies it as a CAPPED *condition*,
        not a status) scrolls into the rolling buffer and stays there while the
        pane keeps working. So the live liveness of the pane — a ``Working``
        spinner (PROCESSING) or the idle composer chrome (IDLE/COMPLETED) — is
        decided BEFORE the error-banner scan, and the ERROR verdict is reserved
        for a genuine launch failure: pi never reached a usable frame (no idle
        chrome AND no working spinner). Once pi has drawn its TUI, an ``Error:``
        line is transcript, never a terminal ERROR — so a nudged worker that
        resumes real work re-derives PROCESSING/IDLE instead of latching error.
        """
        native = self._resolve_native_status(buffer)
        if native is not None:
            return native

        if not self._initialized:
            return TerminalStatus.UNKNOWN

        clean = strip_terminal_escapes(self._resolve_buffer(buffer))
        if not clean.strip():
            return TerminalStatus.UNKNOWN

        # Live liveness first (F844 #701): a working spinner or the idle composer
        # chrome re-derives the true state every poll, so a runtime error banner
        # left in scrollback (the 429 cap, #700) can never latch a sticky ERROR
        # over a pane that is in fact working or waiting at its composer. The
        # ``⠴ Working`` spinner row (F847 #703) is matched only in the LIVE
        # bottom-of-viewport status region (whole-row anchor, fence-excluded —
        # ``_live_working_spinner``) and BEFORE the idle/composer chrome, so a
        # long-running tool's ``Elapsed``/``(timeout Ns)`` block printed above the
        # spinner cannot flip a working pane to a false IDLE (the delivery-
        # eligible state), while a quoted/fenced or stale-far-above spinner glyph
        # in transcript can no longer flip an idle pane to a false PROCESSING.
        if self._live_working_spinner(clean):
            self._tui_processing_seen = True
            return TerminalStatus.PROCESSING

        if self._has_idle_chrome(clean):
            if self._task_dispatched and self._tui_processing_seen:
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE

        # No live TUI chrome AND no spinner: pi never reached (or has lost) a
        # usable frame — a genuine startup/authorization failure. Only here does
        # the error banner mean a terminal ERROR.
        if _STARTUP_ERROR.search(clean):
            return TerminalStatus.ERROR

        return TerminalStatus.UNKNOWN

    def classify_injection_hazard(self, rows: list[str]) -> str | None:
        """Regular-TUI Pi has no blocking modal that would eat pasted task text."""
        return None

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract Pi's last response from captured scrollback.

        Pi renders the assistant reply above the composer's two box rules; we
        take the text block immediately preceding the LAST pair of rules,
        skipping the footer chrome. Raises ``ValueError`` when no completed
        response is present (e.g. Pi is still working, or the buffer only holds
        the initial banner).
        """
        clean = strip_terminal_escapes(script_output)
        if self._live_working_spinner(clean):
            raise ValueError("No completed Pi response found while Pi is working")

        lines = clean.splitlines()
        rule_indices = [idx for idx, line in enumerate(lines) if _EDITOR_RULE.match(line)]
        if len(rule_indices) < 2:
            raise ValueError("No completed Pi response found in terminal output")

        # Everything above the top rule of the last composer box is transcript.
        transcript = lines[: rule_indices[-2]]
        while transcript and not transcript[-1].strip():
            transcript.pop()
        block: list[str] = []
        while transcript and transcript[-1].strip():
            block.append(transcript.pop().rstrip())
        block.reverse()
        response = "\n".join(line.strip() for line in block).strip()
        if not response or response.lower().startswith(("pi v", "warning:", "press ctrl")):
            raise ValueError("No completed Pi response found in terminal output")
        return response

    def exit_cli(self) -> str:
        """Return the tmux special key that exits Pi (Ctrl-D EOF)."""
        return "C-d"

    def cleanup(self) -> None:
        """Remove the per-worker runtime dir (prompt + MCP config + sessions)."""
        self._initialized = False
        self._tui_processing_seen = False
        rd = self.runtime_dir
        # Guard: only remove our own terminal's subdirectory under PI_RUNTIME_ROOT.
        if rd.parent == PI_RUNTIME_ROOT and rd.name == self.terminal_id and rd.exists():
            try:
                shutil.rmtree(rd)
                logger.info("pi worker %s: runtime dir removed: %s", self.terminal_id, rd)
            except OSError as exc:
                logger.warning(
                    "pi worker %s: failed to remove runtime dir %s: %s",
                    self.terminal_id,
                    rd,
                    exc,
                )


# ─── providers.toml knobs ──────────────────────────────────────────────────────

# Default per-request MCP timeout (ms) written into the worker's mcp.json.
# cao-mcp-server startup (Python import + HTTP round-trip to the CAO API) can be
# slow under concurrent worker load, so this is generous but bounded. Overridable
# via ``[pi_cli] mcp_request_timeout_ms`` in providers.toml, floored to keep a
# too-small value from re-introducing an init race.
_PI_MCP_TIMEOUT_MS_DEFAULT = 60_000
_PI_MCP_TIMEOUT_MS_FLOOR = 30_000


def _resolve_pi_mcp_timeout_ms() -> int:
    """Resolve the pi MCP per-request timeout (ms) from providers.toml."""
    value: int = _PI_MCP_TIMEOUT_MS_DEFAULT
    try:
        raw = get_provider_defaults("pi_cli").get("mcp_request_timeout_ms")
    except Exception:
        raw = None
    if isinstance(raw, bool):
        raw = None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
    if value < _PI_MCP_TIMEOUT_MS_FLOOR:
        value = _PI_MCP_TIMEOUT_MS_FLOOR
    return value
