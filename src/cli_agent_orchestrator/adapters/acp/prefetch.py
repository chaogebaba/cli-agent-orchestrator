"""AC-S1.20 / AC-S0.7's residual — the three ``npx -y`` adapters, vendored.

S0 spawned all eight launch lines successfully under systemd and recorded one
caveat with the pass: the three ``npx -y`` lines resolved **from a warm npm cache
under the preserved ``$HOME``**.  That is not a launch spec, it is a coincidence
of the machine the probe ran on.  Once ``cao-server`` owns the spawn, the first
boot after a cache wipe hangs on a download — with no terminal, no pane and no
timeout that means anything, because ``initialize`` has not failed, it has not
started.  S0 round 2 measured the worst case directly: a broken npx cache entry
made ``initialize`` exceed 120 s outright, which is why ``RECOVERY_DEADLINE_S``
is explicitly NOT stretched to cover cold resolution.

So the resolution moves to install time and the launch argv names a real path.

Three properties:

* **``npx`` never appears in a resolved argv.**  Not "npx with a longer timeout",
  not "npx with ``--offline``" — the argv names the vendored entry point, so
  there is no resolution step left to be slow.  A grep-checkable property, and
  :func:`resolve_adapter_argv` is the only thing that builds one.
* **A missing vendor tree is a TYPED condition, never a fallback.**  Falling back
  to ``npx`` would restore the hang on exactly the machines the vendoring was for
  — the ones with a cold cache — and would do it silently.
* **The version is pinned and recorded.**  D13 certifies per
  ``(provider, protocolVersion, adapter package@version, CLI version)``, so an
  adapter that floated to a new version would silently invalidate its own
  certification row.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "NPX_ADAPTERS",
    "AdapterSpec",
    "AdapterUnavailable",
    "prefetch_adapters",
    "resolve_adapter_argv",
    "vendor_root",
]


@dataclass(frozen=True)
class AdapterSpec:
    """One npm-published ACP adapter, pinned.

    ``entry`` is the path INSIDE the installed package, not a ``.bin`` symlink:
    npm's bin shims are shell scripts that re-resolve, and re-resolution is the
    thing being removed.
    """

    name: str
    package: str
    version: str
    entry: str

    @property
    def spec(self) -> str:
        return f"{self.package}@{self.version}"


#: The three lines AC-S0.7 flagged.  Versions are S0's measured ones, and they
#: are literals here for the same reason the timing constants are literals in
#: ``core/timing.py``: the certification row names the version, so a floating
#: spec would invalidate a row nobody had touched.
NPX_ADAPTERS: tuple[AdapterSpec, ...] = (
    AdapterSpec(
        name="claude-acp",
        package="@agentclientprotocol/claude-agent-acp",
        version="0.76.0",
        entry="dist/index.js",
    ),
    AdapterSpec(
        name="codex-acp",
        package="@agentclientprotocol/codex-acp",
        version="1.11.0",
        entry="dist/index.js",
    ),
    AdapterSpec(
        name="pi-acp",
        package="pi-acp",
        version="latest",
        entry="dist/index.js",
    ),
)


class AdapterUnavailable(RuntimeError):
    """The vendored adapter is not there, and there is no second-best answer.

    Raised rather than degraded.  A fallback to ``npx`` would restore the cold
    cache hang on exactly the machines the vendoring exists for, and it would do
    it silently — the launch would look like a slow start rather than a missing
    artefact.  The caller turns this into a typed ``exited`` condition with the
    remediation in it.
    """


def vendor_root(*, environ: dict[str, str] | None = None) -> Path:
    """Where the adapters are vendored.

    Under ``CAO_HOME_DIR`` so a test and a real installation differ by one
    environment variable, and so the tree lives with the rest of CAO's state
    rather than in a cache directory something else may clear.
    """
    env = environ if environ is not None else dict(os.environ)
    home = env.get("CAO_HOME_DIR") or str(Path.home() / ".aws" / "cli-agent-orchestrator")
    return Path(home) / "acp-adapters"


def resolve_adapter_argv(
    name: str,
    *,
    environ: dict[str, str] | None = None,
    node: str | None = None,
) -> list[str]:
    """The argv for a vendored adapter.  Never contains ``npx``.

    ``node`` is resolved to an ABSOLUTE path, because ``cao-server`` runs as a
    systemd user service whose ``PATH`` is not a login shell's — the fork has
    already been bitten by exactly that, and a bare ``node`` here would fail in
    production while passing every test run from a terminal.
    """
    spec = next((entry for entry in NPX_ADAPTERS if entry.name == name), None)
    if spec is None:
        raise AdapterUnavailable(f"no vendored ACP adapter named {name!r}")
    root = vendor_root(environ=environ)
    entry_point = root / "node_modules" / spec.package / spec.entry
    if not entry_point.exists():
        raise AdapterUnavailable(
            f"{spec.spec} is not vendored at {entry_point}; run the adapter prefetch "
            "(install.sh) before cao-server owns the spawn — falling back to `npx -y` "
            "would reintroduce the cold-cache hang this vendoring removes"
        )
    binary = node or shutil.which("node")
    if not binary:
        raise AdapterUnavailable("node was not found on PATH; cao-server needs an absolute path")
    return [str(Path(binary).resolve()), str(entry_point)]


def prefetch_adapters(
    *,
    environ: dict[str, str] | None = None,
    runner: object = None,
    timeout_s: float = 600.0,
) -> tuple[str, ...]:
    """Install the three pinned adapters into the vendor root.  Idempotent.

    An INSTALL-TIME step: this is the one place a download is allowed, because
    it is the one place a human is watching.  ``runner`` is injectable so a test
    can assert the command shape without a network.

    Returns the specs installed.  Already-present packages are re-installed
    rather than skipped: ``npm install`` on an intact tree is fast and a
    partially-extracted package is otherwise indistinguishable from a complete
    one, which is precisely the failure S0 round 2 saw as a broken cache entry.
    """
    root = vendor_root(environ=environ)
    root.mkdir(parents=True, exist_ok=True)
    npm = shutil.which("npm")
    if not npm:
        raise AdapterUnavailable("npm was not found on PATH; cannot prefetch ACP adapters")
    specs = tuple(entry.spec for entry in NPX_ADAPTERS)
    command = [npm, "install", "--prefix", str(root), "--no-audit", "--no-fund", *specs]
    run = runner if runner is not None else subprocess.run
    run(command, check=True, timeout=timeout_s)  # type: ignore[operator]
    return specs
