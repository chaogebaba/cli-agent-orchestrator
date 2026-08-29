"""F568 D12c: `cao agents` fleet listing renders `delegating (N)` in the STATUS
cell for a seat the projection marked delegating; a non-delegating seat is
unaffected."""

import click.testing

from cli_agent_orchestrator.cli.commands.agents import _render_fleet


def _row(**over):
    base = {
        "window_index": 0,
        "id": "aaaaaaaa",
        "profile": "chao_supervisor",
        "status": "idle",
        "delegating": False,
        "children_count": 0,
        "fusion_changed": False,
        "since_last_input": 1.0,
        "parent_id": None,
        "window_name": "w-aaaaaaaa",
        "orphan": False,
        "reparented_from": None,
    }
    base.update(over)
    return base


def _render(rows):
    payload = {"session_name": "cao-d12c", "terminals": rows}
    out = []
    original = click.echo

    def _capture(message="", **_kw):
        out.append(str(message))

    click.echo = _capture
    try:
        _render_fleet(payload)
    finally:
        click.echo = original
    return "\n".join(out)


def test_delegating_row_renders_delegating_n():
    text = _render([_row(status="idle", delegating=True, children_count=2)])
    assert "delegating (2)" in text
    # The raw enum value is not the cell for a delegating seat.
    assert "\nidle" not in text


def test_non_delegating_row_unchanged():
    text = _render([_row(status="idle", delegating=False, children_count=0)])
    assert "delegating" not in text
    assert "idle" in text


def test_processing_with_children_is_not_delegating_in_render():
    text = _render([_row(status="processing", delegating=False, children_count=3)])
    assert "delegating" not in text
    assert "processing" in text
