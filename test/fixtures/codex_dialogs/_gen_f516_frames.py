"""F516 commit 1 fixture generator (kept in-tree for reproducibility).

Cuts frozen per-incident frame sequences from the reapable incident logs under
~/.aws/cli-agent-orchestrator/logs/terminal/. Run once with:

    uv run python test/fixtures/codex_dialogs/_gen_f516_frames.py

Produces <incident>-frame-NN.ansi.txt (raw escapes, cumulative byte prefixes),
<incident>-manifest.json (per-frame monotonic offsets), and appends to
FIXTURE-SOURCES.md / SHA256SUMS. Deterministic: same logs → same frames.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from cli_agent_orchestrator.utils.terminal_render import compose_ansi_to_lines

LOG_DIR = Path(os.path.expanduser("~/.aws/cli-agent-orchestrator/logs/terminal"))
OUT = Path(__file__).parent
COLS, ROWS = 220, 50

# incident -> (source log id, list of (byte_fraction, offset_s)). Cumulative
# prefixes at each fraction render a distinct frame; offsets are the fake-clock
# monotonic timestamps the replay harness advances to.
CHOOSER_FRACTIONS = [(0.6, 0.0), (0.8, 0.2), (0.99, 0.4)]
INCIDENTS = {
    "resume-chooser-61e1b848": ("61e1b848", CHOOSER_FRACTIONS),
    "resume-chooser-c8c767ca": ("c8c767ca", CHOOSER_FRACTIONS),
    # Banner fixture ends mid-motion (AC2(b) delivery arm): the last frame is a
    # still-scrolling capture, not a settled banner.
    "content-policy-banner-f02ce13e": (
        "f02ce13e",
        [(0.985, 0.0), (0.992, 0.2), (0.998, 0.35)],
    ),
}


def main() -> None:
    sources_lines = ["# F516 fixture sources (commit 1)\n"]
    sums_lines = []
    for incident, (log_id, cuts) in INCIDENTS.items():
        raw = (LOG_DIR / f"{log_id}.log").read_text(encoding="utf-8", errors="replace")
        frames = []
        for idx, (frac, offset_s) in enumerate(cuts):
            end = max(1, int(len(raw) * frac))
            prefix = raw[:end]
            fname = f"{incident}-frame-{idx:02d}.ansi.txt"
            (OUT / fname).write_text(prefix, encoding="utf-8")
            digest = hashlib.sha256(prefix.encode("utf-8")).hexdigest()
            sums_lines.append(f"{digest}  {fname}")
            frames.append({"file": fname, "offset_s": offset_s})
        manifest = {"incident": incident, "cols": COLS, "rows": ROWS, "frames": frames}
        (OUT / f"{incident}-manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        final_rows = compose_ansi_to_lines(raw[: int(len(raw) * cuts[-1][0])], COLS, ROWS)
        preview = next((l.strip() for l in final_rows if l.strip()), "")
        sources_lines.append(
            f"- {incident}: from logs/terminal/{log_id}.log "
            f"(reaped 2026-08-26); final-frame first-row: {preview[:70]!r}\n"
        )
    (OUT / "FIXTURE-SOURCES.md").write_text("".join(sources_lines), encoding="utf-8")
    (OUT / "SHA256SUMS.f516").write_text("\n".join(sums_lines) + "\n", encoding="utf-8")
    print("wrote", len(INCIDENTS), "incident frame-sequences")


if __name__ == "__main__":
    main()
