"""F880 (#745) B1 — strict-typing parity for the branch-added herdr helper.

The CORE merge gate (``orchestrator/GATE-RULES.md:36``) makes ANY new
``mypy --strict`` diagnostic a blocker. Round r3 shipped
``HerdrBackend._foreground_processes`` with bare ``dict``/``list`` annotations,
which raised one head-only ``[type-arg]`` diagnostic at
``herdr_backend.py:280`` that base did not have.

``mypy --strict`` is too slow to run per-test, so this pins the same property
at runtime: the helper's annotations must be *parameterized* generics, which is
exactly what silences ``[type-arg]``. A regression that drops the parameters
(the r3 shape) fails here without waiting for the static gate.
"""

import typing

from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend


def _hints() -> dict[str, object]:
    return typing.get_type_hints(HerdrBackend._foreground_processes)


class TestF880ForegroundProcessesTypeArgs:
    def test_parsed_parameter_is_a_parameterized_dict(self) -> None:
        """``parsed: dict`` (bare) is the r3 shape that tripped [type-arg]."""
        parsed = _hints()["parsed"]
        assert typing.get_origin(parsed) is dict, f"expected a dict annotation, got {parsed!r}"
        assert typing.get_args(parsed), (
            "F880 B1: `parsed` must be a parameterized dict "
            "(bare `dict` is a new strict-mypy [type-arg] diagnostic)"
        )

    def test_return_is_a_parameterized_list_of_parameterized_dicts(self) -> None:
        """``-> list[dict]`` still leaves the inner dict bare under --strict."""
        ret = _hints()["return"]
        assert typing.get_origin(ret) is list, f"expected a list annotation, got {ret!r}"
        args = typing.get_args(ret)
        assert args, (
            "F880 B1: the return type must be a parameterized list "
            "(bare `list` is a new strict-mypy [type-arg] diagnostic)"
        )
        inner = args[0]
        assert typing.get_origin(inner) is dict, f"expected list[dict[...]], got {ret!r}"
        assert typing.get_args(inner), (
            "F880 B1: the element type must be a parameterized dict "
            "(`list[dict]` is a new strict-mypy [type-arg] diagnostic)"
        )

    def test_helper_still_extracts_the_0_9_0_nested_shape(self) -> None:
        """Guard: the annotation repair must not disturb the F880 behavior."""
        procs: list[dict[str, object]] = [{"name": "grok", "pid": 4242}]
        parsed: dict[str, object] = {"process_info": {"foreground_processes": procs}}
        assert HerdrBackend._foreground_processes(parsed) == procs
