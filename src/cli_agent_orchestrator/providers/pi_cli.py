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
    - a startup/authorization error banner during LAUNCH → initialize() raises
      (F899 r2: get_status itself never returns ERROR from the buffer).

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
  --session-id <id>        : exact project session id — created if missing, but
                             SILENTLY RE-ATTACHED (whole transcript replayed) when
                             a session with that id already exists in --session-dir
                             (live-probed 2026-09-10, F908 #760)
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
import stat
import time
import unicodedata
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.models.terminal import ForkContext, TerminalStatus
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


def _open_nofollow_chain(base: Path, parts: tuple[str, ...]) -> int:
    """Open ``base/*parts`` as a directory fd, refusing a symlink at EVERY component.

    F908 (#760) r3. ``O_NOFOLLOW`` guards only the FINAL component of a path, so
    ``os.open("<root>/<tid>/sessions", ...|O_NOFOLLOW)`` still follows a ``<tid>``
    that was swapped for a symlink after an earlier check — the window a real
    concurrent racer exploited 3 times in 8627 attempts (EMPIRICAL-GATE-NO r2).
    Walking from a fd on ``base`` and opening each component ``O_NOFOLLOW``
    relative to the previous fd leaves no component re-resolved from a path, so
    the race turns into an ``OSError`` instead of a foreign directory.

    Raises ``OSError`` (``ELOOP`` for a symlinked component) rather than
    returning a fd the caller must re-validate. The caller owns the fd.
    """
    fd = os.open(str(base), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except OSError:
        os.close(fd)
        raise
    return fd


# ─── Status detection ─────────────────────────────────────────────────────────
# All patterns run against ``strip_terminal_escapes(buffer)`` output, which
# normalizes the raw pipe-pane byte stream into clean, line-oriented text
# (calibrated against live pi 0.85.1 captures, 2026-09-07).

# A Pi composer/editor box rule: a run of box-drawing horizontals (or ASCII
# dashes as a fallback).  Pi draws two of these around the footer when idle.
_EDITOR_RULE = re.compile(r"^\s*[─━—-]{20,}\s*$")


def _visible_width(s: str) -> int:
    """Terminal column width of an ANSI-stripped string (East-Asian aware).

    F847 r5 (#703): the composer-width invariant in ``_live_working_spinner``
    compares the live working row against the widest composer rule in the same
    frame. Those rows carry wide glyphs (the braille spinner is narrow, but pane
    content and box glyphs are not uniformly one column), so ``len`` is wrong —
    a fullwidth/wide code point occupies two terminal columns. Count W/F East-
    Asian-width code points as 2 and everything else as 1; combining marks (which
    render zero-width) are treated as 0 so they do not inflate the width.
    """
    width = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


# The braille spinner frames Pi cycles through on its active "Working" rule
# row.  The ten canonical frames (#703 F847) are ``⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏``; we anchor on
# the full braille-patterns Unicode block (U+2800–U+28FF) so every frame — and
# the wider ``⠿``-style glyphs Pi has been observed to draw (#700/#701 corpus) —
# counts, while ordinary ASCII prose that merely says "Working" does not.
_BRAILLE_SPINNER = r"\u2800-\u28ff"

# The active spinner line while Pi is working, e.g. "── ⠧ Working ──────".  This
# is a WHOLE ROW the TUI draws — a Pi box rule (``──…`` run) LEADING a braille
# spinner glyph adjacent to ``Working``, the row then ending on the composer's
# closing border rule (see ``_WORKING_TAIL`` below).  The spinner glyph varies
# frame to frame, so all frames in the braille block are accepted.  The row is
# always RULE-LEADING: a live capture of pi 0.85.1 (#703 r3) found zero
# spinner-first frames, and ``CustomEditor.renderTopBorder`` prepends a rule on
# every branch while ``WorkingStatusIndicator.renderInBorder`` returns the
# glyph+message with no rule of its own — the r2 "spinner-first" alternative was
# removed in r3.
#
# F847 r2 (#703): the match is BRAILLE-ONLY and requires a same-row box rule, for
# two reasons the r1 shape got wrong (codex EMPIRICAL-GATE-NO):
#   1. r1's first alternative made the rule OPTIONAL, so bare
#      ``⠦ Working on the summary now`` PROSE fired PROCESSING. Requiring a
#      same-row rule fixes that overmatch.
#   2. r1 also carried permissive ``\S? Working`` NON-braille alternatives, so a
#      stray non-spinner glyph matched and — worse — those alternatives MASKED
#      the braille class entirely (narrowing the braille set to one glyph still
#      matched via ``\S?``, so the ledger's glyph mutant would survive). Every
#      real pi spinner in the capture corpus uses a braille frame, so the
#      non-braille alternatives are dropped: the braille class is now the sole
#      gate on which glyphs count as a live spinner.
# Positional scoping to the live bottom-of-viewport status region (and
# fence-dropping) is done by ``_live_working_spinner`` (r1's whole-buffer
# ``.search`` overmatched a fenced quote and a stale spinner 40 rows above the
# composer).
_RULE_RUN = r"[─━—\-]{2,}"
# F847 r4 (#703) — the tail is pinned on the CLOSING BORDER RULE, not on ``$``
# after whitespace/optional-rule (Opus r3 EMPIRICAL-GATE-NO). The r3 tail
# ``[ \t]*(?:_RULE_RUN[ \t]*)?$`` asserted "the ONLY thing a live row may carry
# to end-of-line is whitespace and an optional box-rule run" — a descriptive
# absolute that pi 0.85.1's own composer border code contradicts on three live
# paths (all regressed 40/40 live frames from PROCESSING to UNKNOWN on the r3
# head, re-opening the #703 false-idle harm from the other side).
#
# What holds across every live draw is that the row IS the composer TOP BORDER
# and therefore ENDS on the border rule — NOT that nothing follows ``Working``.
# Pi draws real content between the message and that closing rule:
#   ── ⠧ Working ─────────────           (the plain turn/resize frame)
#   ── ⠧ Working ─── ↑ 15 more ───────   (custom-editor.js:37-40 overflow label,
#      drawn whenever the composer holds hidden lines while a turn runs — an
#      operator typing the next message mid-turn is ordinary behaviour)
#   ── ⠧ Working (esc to interrupt) ───  (interactive-mode.js:1768)
#   ── ⠹ Working on tool call ────────   (interactive-mode.js:1906, extension-set;
#      CAO launches pi with --mcp-config, so extensions are on)
# So the tail is pinned on the CLOSING rule instead of on "whitespace only":
# a transcript row ends in a word or punctuation and still fails the whole-row
# match, while a live working row — whatever content precedes it — ends on the
# border rule. This also admits the one-``─`` narrow-width branch
# (custom-editor.js:46-47) that the r3 ``_RULE_RUN{2,}`` tail rejected.
#
# F847 r5 (#703) — the CLOSING run is BOX DRAWING ONLY (``[─━]``: no ASCII
# hyphen, no em dash), Opus r4 EMPIRICAL-GATE-NO. The r4 tail's closing class
# ``[─━—\-]+`` admitted the ASCII ``-`` and the em dash, so any transcript row
# that merely opened with the spinner chrome and happened to END on a dash
# ("run with --", a soft-wrapped "rule-", a markdown table rule, and the r2
# adversary with a box rule appended) classified as a live working row — 9 of 10
# dash-ending adversaries fired PROCESSING, the r1/r2 overmatch class re-opened.
# The LEADING ``_RULE_RUN`` still admits ASCII/em dashes for tolerance (pi's
# composer never leads with them, but a stray one there is harmless); accepting
# them at the row END is what caused the false positives. The composer top
# border is drawn from ``─``/``━`` box-drawing runs, so the closing class is
# exactly those two. The load-bearing half of the r4 amendment is the
# full-composer-width check in ``_live_working_spinner`` below (a quoted spinner
# row in transcript is short); box-only closing is the belt to its braces.
_WORKING_TAIL = r"[^\n]*?[─━]+[ \t]*$"
_WORKING_ROW = re.compile(
    # rule-leading: ── ⠧ Working …<closing rule> — a leading box rule, the braille
    # glyph, ``Working``, then any content ending on the closing composer rule.
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


def _current_composer_width(unfenced: list[str]) -> int | None:
    """Width, in terminal columns, of the CURRENT (bottom-most) composer's rule.

    F847 r9 (#703) — codex r8 EMPIRICAL-GATE-NO. This is the SINGLE, structural
    derivation of the current composer width; both the full-width candidate
    qualifier and the composer-below check consume it and NOTHING reads a global
    maximum any more.

    The blocker it closes: r5-r8 sized the composer from
    ``max(_visible_width(row) for row in <every editor rule in the buffer>)`` —
    the GLOBAL maximum over the whole accumulated rolling buffer. Pi's documented
    input is an accumulated raw pipe-pane buffer whose escape cleanup turns
    redraws into separate logical rows, so a STALE WIDER rule from an OLD frame
    coexists ABOVE the current composer. That stale 120-column rule fixed the
    global maximum at 120; a GENUINE live 100-column working row (whose width IS
    its own composer's) then failed ``_visible_width(row) == composer_width`` and
    was dropped from ``candidates`` entirely, so the idle-chrome fallback read the
    working pane as COMPLETED — a false, delivery-eligible status on a busy pane
    (ADV-stale-wider-live-candidate). Windowing the maximum to the live tail does
    NOT fix it: the stale rule can sit close enough to fall inside the tail.

    The structural truth the current composer is anchored by: the live working
    row IS the current composer's TOP border, and the current composer's rules are
    the ones drawn BELOW it (its editor body's bottom rule, just above the
    footer). Stale/old-frame rules sit ABOVE the live working row, in transcript.
    So the current composer width is:

    - the MAXIMUM editor-rule width among the rules positioned strictly BELOW the
      bottom-most ``_WORKING_ROW`` match. "Below the live working row" excludes
      the stale wider rule (it is above) and an old composer's rule pair (also
      above), while a transient RESIZE double-draw's narrow artifact rows are
      NARROWER than the real rule they bracket, so the max still lands on the real
      composer rule (the committed ``working-resize-3rule`` frame: rules 20, 100,
      20 below the working row → 100, not 20 — the live working row stays a valid
      full-width candidate and classifies PROCESSING);
    - else — when no editor rule is drawn below the bottom-most working row (the
      composer is drawn ABOVE the candidate, e.g. a short quoted-spinner adversary
      at the very bottom) — the maximum editor-rule width within the LIVE TAIL
      window. This is still local to the current viewport, never the whole
      buffer;
    - ``None`` when no editor rule is visible at all (no width evidence).
    """
    working_idxs = [i for i, row in enumerate(unfenced) if _WORKING_ROW.match(row)]
    if working_idxs:
        last_working = working_idxs[-1]
        below = [
            _visible_width(row) for row in unfenced[last_working + 1 :] if _EDITOR_RULE.match(row)
        ]
        if below:
            return max(below)
    # No rule below the live working row → the composer is above the candidate;
    # size it from the tail window (still local to the current viewport).
    tail_widths = [
        _visible_width(row) for row in unfenced[-_LIVE_TAIL_ROWS:] if _EDITOR_RULE.match(row)
    ]
    if tail_widths:
        return max(tail_widths)
    return None


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

    # F829 A1 (D10): pi is PARTIAL (D9) — it RECOVERS after a COMPLETED turn (the
    # transcript is written atomically at turn end), so resume/artifact are
    # declared; a mid-turn kill leaves NO artifact (that boundary is D8's
    # session_artifact_missing, not a capability failure). It cannot FORK.
    declared_capabilities = {
        "fork": False,
        "resume": True,
        "capture": True,
        "artifact_locate": True,
    }

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list[str]] = None,
        skill_prompt: Optional[str] = None,
        model: Optional[str] = None,
        fork_context: Optional["ForkContext"] = None,
    ) -> None:
        super().__init__(
            terminal_id, session_name, window_name, allowed_tools, skill_prompt, fork_context
        )
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

    def _purge_stale_sessions(self) -> None:
        """F908 (#760): empty this terminal's session dir before a COLD spawn.

        ``--session-id <id>`` re-attaches an existing session with that id
        instead of minting a fresh one (live-probed 2026-09-10), so a runtime
        dir that outlived its terminal makes a cold worker continue a dead
        task.  Deleting the per-terminal ``*.jsonl`` transcripts is what makes
        "cold spawn == new session" true.  No-op when the operator sets
        ``[pi_cli] fresh_session_on_spawn = false``, and on a resume spawn
        (never called from that arm).

        Containment: the configured ``session_dir`` must lexically be
        ``PI_RUNTIME_ROOT/<terminal id>/sessions``, and the directory is then
        opened by walking that path one component at a time from
        ``PI_RUNTIME_ROOT``, every component no-follow.  Enumeration, stat and
        unlink all go through that fd.  There is therefore no path re-resolution
        after the check, at any component, so a concurrent rename of the
        terminal dir or the sessions leaf makes the open fail rather than
        redirect a deletion.  Only regular ``*.jsonl`` files are removed.
        """
        if not _resolve_pi_fresh_session_on_spawn():
            logger.info(
                "pi worker %s: fresh_session_on_spawn=false — keeping %s as-is",
                self.terminal_id,
                self.session_dir,
            )
            return
        sd = self.session_dir
        # Guard 1 (ownership): the configured dir must lexically BE ours. This
        # is live, killable code — the walk below derives its path from
        # ``terminal_id``, never from ``sd``, so this check is the only thing
        # tying the directory we open to the one the provider was configured
        # with.  ``test_purge_guard_refuses_paths_outside_our_runtime_dir``
        # kills its deletion.
        if sd != PI_RUNTIME_ROOT / self.terminal_id / "sessions":
            return
        # Guard 2 (no TOCTOU at ANY component): ``O_NOFOLLOW`` constrains only
        # the FINAL component, so opening the whole path in one call still
        # follows a terminal dir swapped for a symlink after any prior check —
        # a real concurrent racer deleted another terminal's transcripts 3
        # times in 8627 attempts against that shape (EMPIRICAL-GATE-NO r2, H4).
        # Walking component-by-component from a fd on PI_RUNTIME_ROOT removes
        # the window: every component is opened no-follow.
        try:
            dir_fd = _open_nofollow_chain(PI_RUNTIME_ROOT, (self.terminal_id, "sessions"))
        except OSError as exc:
            logger.warning("pi worker %s: refusing to purge %s: %s", self.terminal_id, sd, exc)
            return
        try:
            for name in sorted(os.listdir(dir_fd)):
                if not name.endswith(".jsonl"):
                    # Only pi transcripts are ours to delete; anything else an
                    # operator or another tool left here survives.
                    continue
                try:
                    st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    # A symlinked *.jsonl points outside our ownership; leave it.
                    continue
                try:
                    os.unlink(name, dir_fd=dir_fd)
                    logger.info(
                        "pi worker %s: purged stale session transcript %s",
                        self.terminal_id,
                        sd / name,
                    )
                except OSError as exc:
                    logger.warning(
                        "pi worker %s: failed to purge stale session %s: %s",
                        self.terminal_id,
                        sd / name,
                        exc,
                    )
        finally:
            os.close(dir_fd)

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
        ]

        # F829 A1 (D3): pi resume arm. On a resume-mode fork_context carrying a
        # recorded artifact path, re-attach the PRIOR session with
        # ``--session <artifact_locator>`` (D9-confirmed: does NOT create a new
        # session) rather than minting a fresh ``--session-id <terminal_id>``.
        _fc = self._fork_context
        _pi_resume_path = (
            _fc.session_artifact_path
            if _fc is not None and getattr(_fc, "mode", None) == "resume"
            else None
        )
        if _pi_resume_path:
            command_parts.extend(["--session", str(_pi_resume_path)])
        else:
            # F908 (#760): cold spawn — guarantee pi cannot re-attach a
            # transcript left behind under this terminal's session dir.
            self._purge_stale_sessions()
            command_parts.extend(
                [
                    "--session-id",
                    self.terminal_id,
                    "--session-dir",
                    str(self.session_dir),
                ]
            )
        command_parts.extend(
            [
                "--append-system-prompt",
                str(self.prompt_path),
                "--mcp-config",
                str(self.mcp_config_path),
            ]
        )

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
        4. A row in that window is a working-row CANDIDATE only if it matches the
           whole-row ``_WORKING_ROW`` anchor (a braille glyph + ``Working`` with a
           leading box rule and a box-drawing closing rule), never bare prose.
        5. F847 r5 (#703): a candidate is the LIVE working row only if it spans
           the FULL composer width. The live working row IS the composer TOP
           BORDER, so ``renderTopBorder`` sizes its trailing rule from ``width``:
           a label or message shortens the rule and the total width is invariant
           (measured 424/424 live pi 0.85.1 frames + all 40 overflow frames — the
           working row's visible width equals the widest composer rule in the same
           frame). A quoted spinner row pasted into transcript is SHORT, so this
           rejects the dash-ending transcript adversaries the box-only closing
           class alone still let through (Opus r4 EMPIRICAL-GATE-NO). When no
           composer rule is visible there is no width evidence, so a candidate is
           accepted rather than narrowed away.
        6. F847 r6 (#703): width equality is NECESSARY but not SUFFICIENT. A
           logical transcript row one column longer than the composer WRAPS: its
           first physical row is exactly composer-wide and, if it ends on a box
           char, matches ``_WORKING_ROW`` at the full width — a manufactured
           full-width transcript row (codex r5 EMPIRICAL-GATE-NO). The structural
           invariant that separates it: the live working row IS the composer TOP
           border, so there is only ONE composer and the working row belongs to
           the BOTTOM-most one — there is never a COMPLETE idle composer (a
           self-consistent same-width rule PAIR AND a footer) drawn ENTIRELY BELOW
           the live working row. The wrapped transcript row, by contrast, sits
           above the genuine idle composer, so a complete composer follows it.
           Measured on the archive: 0 of 424 live frames have a complete composer
           below their last working row (422 have exactly one rule after, one
           transient resize frame has three rules whose two extra rows are a
           narrow artifact pair bracketing the real rule — a resize double-draw,
           not a composer box — one has zero), so this rejects the wrap without a
           single false negative. A candidate with a complete composer below it is
           disqualified.

           F847 r8 (#703): the "complete idle composer below" test is a
           self-consistent same-width rule pair + footer, NOT two rules matching
           the GLOBAL maximum editor-rule width. A stale wider rule left in the
           accumulated buffer used to poison that maximum and hide the current
           composer's own (narrower) rule pair; see ``_has_complete_composer_below``.

           F847 r9 (#703): codex r8 EMPIRICAL-GATE-NO. r8 removed the global
           maximum from the below-check but left it powering the full-width
           CANDIDATE qualifier in step 5, so the SAME stale wider rule could still
           drop a genuine narrower live working row from ``candidates`` and yield
           a false COMPLETED (ADV-stale-wider-live-candidate). The global maximum
           is now DELETED: the current composer width comes from the single
           structural ``_current_composer_width`` helper (the max editor-rule width
           BELOW the live working row, else the tail-window max), which BOTH the
           candidate qualifier and the composer-below structure consume. No
           consumer reads the whole-buffer maximum any more.

           F847 r10 (#703): codex r9 EMPIRICAL-GATE-NO item 1. r9 wired the
           structural width into the candidate qualifier but left the
           composer-below check width-AGNOSTIC, so an adjacent narrow RESIZE
           redraw pair (two 20-column artifacts) below a genuine 100-column live
           working row was accepted as a complete idle composer and produced a
           false COMPLETED (ADV-adjacent-narrow-resize-pair). Both consumers now
           consume the ONE derived ``composer_width``: the below-check requires the
           idle-composer rule PAIR to equal it, so only the current composer's own
           width counts as its box.
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
        if not any(_WORKING_ROW.match(row) for row in tail):
            return False
        # 5) full-composer-width invariant: the live working row spans the pane.
        # F847 r9 (#703): the composer width is derived STRUCTURALLY from the
        # current (bottom-most) composer via the single ``_current_composer_width``
        # helper — the max editor-rule width BELOW the live working row, else the
        # tail-window max. It is NO LONGER the global maximum over every rule in
        # the accumulated buffer, so a STALE WIDER rule left in scrollback can no
        # longer poison the width and drop a genuine narrower live working row from
        # ``candidates`` (codex r8 EMPIRICAL-GATE-NO: ADV-stale-wider-live-
        # candidate). Both this candidate filter and the composer-below check
        # consume the current composer's structure; nothing reads the global max.
        composer_width = _current_composer_width(unfenced)
        if composer_width is None:
            # No composer rule visible → no width evidence; do not narrow.
            return True
        # Candidate positions in the FULL unfenced buffer (not just the tail), so
        # step 6 can inspect what is drawn BELOW each candidate.
        tail_start = len(unfenced) - len(tail)
        candidates = [
            idx
            for idx, row in enumerate(unfenced)
            if idx >= tail_start
            and _WORKING_ROW.match(row)
            and _visible_width(row) == composer_width
        ]
        if not candidates:
            return False

        # 6) composer-structure check: a candidate is the live TOP BORDER only if
        # it is NOT sitting above a COMPLETE idle composer (a wrapped full-width
        # transcript row does). "Complete idle composer below" = a SELF-CONSISTENT
        # composer box (a pair of editor rules of EQUAL width to EACH OTHER, with
        # a footer) drawn strictly after the candidate.
        #
        # F847 r8 (#703) — codex r7 EMPIRICAL-GATE-NO. The r6/r7 test counted the
        # below rules against ``composer_width``, the MAXIMUM editor-rule width in
        # the WHOLE unfenced rolling buffer. Pi's documented input is an
        # accumulated raw pipe-pane buffer whose escape cleanup turns redraws into
        # separate logical rows, so a STALE wider rule from an OLD frame can
        # coexist above the current composer. Such a stale 120-column rule fixes
        # ``composer_width`` at 120; the current idle composer's own two 100-column
        # rules then no longer equal ``composer_width`` and are NOT counted, so a
        # 120-column wrapped-transcript candidate above a COMPLETE 100-column idle
        # composer read PROCESSING (ADV-stale-wider-rule). Deleting only the stale
        # rule flipped it to COMPLETED.
        #
        # The fix DECOUPLES this check from the poisoned global width: a complete
        # composer below is a pair of editor rules of EQUAL width to EACH OTHER
        # (whatever that width is) plus a footer. The pair is required to be CLEAN
        # — no editor rule WIDER than the pair sandwiched between its two members —
        # which distinguishes a genuine composer box (its two rules bracket the
        # editor body; nothing wider sits between them) from a transient RESIZE
        # double-draw, where a wider rule is redrawn BETWEEN two narrow artifact
        # rows (the committed ``working-resize-3rule`` frame: two 20-column
        # artifacts bracketing the real 100-column rule — NOT a composer, so the
        # live working row there stays PROCESSING). This keeps the full-width
        # candidate qualifier (``_visible_width(row) == composer_width`` above)
        # intact for the resize fixtures.
        #
        # F847 r9 (#703): ``composer_width`` above is the CURRENT composer's width
        # from ``_current_composer_width`` (structural), NOT the whole-buffer
        # maximum — see step 5.
        #
        # F847 r10 (#703) — codex r9 EMPIRICAL-GATE-NO item 1. The frozen repair
        # contract requires BOTH consumers — the full-width candidate qualifier
        # (step 5) AND this complete-composer-below check — to consume the ONE
        # derived current ``composer_width``. r9 left this check width-AGNOSTIC (it
        # matched a same-width pair at ANY width), which is a hole: a genuine live
        # 100-column working row whose current composer draws TWO adjacent 20-column
        # RESIZE redraw artifacts (plus the single real 100-column bottom rule and a
        # footer) below it had that 20/20 artifact pair accepted as a "complete idle
        # composer", so the live working row was disqualified and the pane read a
        # false COMPLETED (ADV-adjacent-narrow-resize-pair). The pair is therefore
        # required to equal the derived ``composer_width``: only a rule pair at the
        # CURRENT composer's own width counts as its idle-composer box. The 20/20
        # artifact pair (20 != 100) no longer qualifies, so the live working row
        # stays PROCESSING; the r7/r8 stale-wider and two-composer COMPLETED
        # contracts keep their genuine width-``composer_width`` idle pair below the
        # wrap candidate and remain COMPLETED. The "nothing WIDER than the pair
        # sandwiched between its members" cleanliness clause is retained (it still
        # separates a genuine box from a resize double-draw at the composer width
        # itself, e.g. the committed ``working-resize-3rule`` frame).
        def _clean_same_width_rule_pair_below(idx: int) -> bool:
            rule_widths = [_visible_width(r) for r in unfenced[idx + 1 :] if _EDITOR_RULE.match(r)]
            for i in range(len(rule_widths)):
                for j in range(i + 1, len(rule_widths)):
                    if rule_widths[i] == rule_widths[j] == composer_width and all(
                        rule_widths[k] <= rule_widths[i] for k in range(i + 1, j)
                    ):
                        return True
            return False

        def _has_complete_composer_below(idx: int) -> bool:
            has_footer = any(_FOOTER_CONTEXT.search(r) for r in unfenced[idx + 1 :])
            return _clean_same_width_rule_pair_below(idx) and has_footer

        return any(not _has_complete_composer_below(idx) for idx in candidates)

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
          - neither chrome nor spinner            → UNKNOWN (F899 r2: never
            ERROR — a launch failure is raised by initialize(), and a quiet
            buffer on a ready terminal is not one)
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

        # No live TUI chrome AND no spinner. F899 (#751) r2 ruling 3: this can
        # NO LONGER mean a launch failure, and the error-banner scan that used to
        # sit here is gone.
        #
        # The scan was unreachable in the only sense that mattered and harmful in
        # the other. Reachable only past `if not self._initialized: return
        # UNKNOWN` above — that is, only on a terminal that HAS rendered a usable
        # frame, so "pi never reached a usable frame" was false by construction
        # every time it fired. What it actually caught was the third 2026-09-10
        # sample: two ready pi lanes published ERROR while their panes rendered
        # the live Working spinner, because the rolling buffer had gone quiet and
        # its last frame carried neither chrome nor spinner but did carry an old
        # `Error:` line (the #700 ClinePass 429 banner).
        #
        # A genuine launch failure is still caught, and always was, by
        # `initialize()`: it polls the pane against the same _STARTUP_ERROR and
        # raises RuntimeError before `_initialized` is ever set. That is the one
        # place inside the launch window, and it is untouched.
        #
        # A quiet buffer now falls to UNKNOWN, which the F808 (#665) cached-UNKNOWN
        # self-heal re-derives from a real capture (pi opts in via
        # supports_direct_status_probe), and which fuse_status will not lower.
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

    def spawn_captured_identity(self) -> "tuple[str, Optional[str], Optional[str]] | None":
        """F867 (#723): pi's session identity is KNOWN AT SPAWN.

        CAO launches a fresh pi worker with ``--session-id <terminal_id>`` and
        ``--session-dir <session_dir>`` (see ``_build_pi_command``); pi then
        writes its transcript ATOMICALLY at the first turn's completion as
        ``<timestamp>_<terminal_id>.jsonl`` under that dir (live-probed pi
        0.85.1). So the recoverable identity is deterministic before any turn:
        the provider_session_id is the terminal id and the namespace is the
        session dir, which ``session_artifact._resolve_pi`` globs
        (``**/*_<uuid>.jsonl``) once the file exists. Binding this at spawn is
        what makes ``provider_session_id`` non-null so a planned hibernate stops
        refusing ``session_artifact_missing`` on a pi lane that HAS completed a
        turn (before the first turn the artifact is correctly MISSING —
        unrecoverable, D8 — and hibernate still refuses, which is right).

        Returns ``None`` on a RESUME spawn: the resumed worker re-attaches a
        PRIOR session via ``--session <artifact_locator>`` and its root is
        re-pointed by the resume publish path, so there is nothing fresh to bind.
        """
        _fc = self._fork_context
        if _fc is not None and getattr(_fc, "mode", None) == "resume":
            return None
        return (self.terminal_id, str(self.session_dir), None)

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


# F908 (#760): a COLD spawn must never continue a prior transcript.  pi's
# ``--session-id <id>`` is documented "creating it if missing" — live-probed
# 2026-09-10, it SILENTLY RE-ATTACHES an existing session with that id under
# ``--session-dir`` and replays its whole transcript.  Our session dir is
# per-terminal, so this only bites when a runtime dir outlives its terminal
# (``cleanup`` skipped on a crash/server bounce — eight stale dirs were found
# under ``$CAO_HOME/pi`` on 2026-09-10) or when a pane relaunches pi with the
# same terminal id.  Purging the per-terminal session dir at cold spawn closes
# that door while keeping F867's spawn-known session identity (terminal id).
# Operator escape hatch: ``[pi_cli] fresh_session_on_spawn = false``.
_PI_FRESH_SESSION_ON_SPAWN_DEFAULT = True


def _resolve_pi_fresh_session_on_spawn() -> bool:
    """Resolve ``[pi_cli] fresh_session_on_spawn`` from providers.toml (default True)."""
    try:
        raw = get_provider_defaults("pi_cli").get("fresh_session_on_spawn")
    except Exception:
        raw = None
    if raw is None:
        return _PI_FRESH_SESSION_ON_SPAWN_DEFAULT
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token in ("true", "1", "yes", "on"):
            return True
        if token in ("false", "0", "no", "off"):
            return False
    # r2 H4 NIT: an unrecognised value falls back to the default (deliberately —
    # a typo must never silently DISABLE a correctness fix), but say so, or the
    # operator can only infer their typo from behaviour.
    logger.warning(
        "providers.toml [pi_cli] fresh_session_on_spawn=%r is not a recognised "
        "boolean; falling back to %s",
        raw,
        _PI_FRESH_SESSION_ON_SPAWN_DEFAULT,
    )
    return _PI_FRESH_SESSION_ON_SPAWN_DEFAULT
