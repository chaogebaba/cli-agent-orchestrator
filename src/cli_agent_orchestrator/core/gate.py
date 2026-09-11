"""The gate record's pure domain (WP-ARCH Amendment A, slice 2a; §10.2).

Everything here is a value or a total function over values.  ``core-is-pure``
forbids SQLite, tmux, an event loop and an HTTP client; a clock is an argument
where one is needed at all.  That is the same discipline ``core/delivery.py``
keeps, and for the same reason: the two facts slice 2a is most likely to get
wrong — the content address a round is identified by, and the transitions a run
and a round may make — are decidable without a database, so they are decided
here and tested by enumeration.

The section this transcribes is the blueprint's §10.2, which Astra's co-design
r0 (P1-P5) rewrote.  The rows the SERVER owns (F809 boundary) are values here so
``app/gate`` can read and reason about one without a database, and the store
adapter is the only module that knows they are SQLite.  ``core/gate`` is limited
to invariants and transitions (P3); ``app/gate/GateRoundService`` owns the
transactional commands through ports; adapters own the git, CAS, store and
provider effects; ``bootstrap.py`` wires them.

**Increment 2a builds exactly this** (§10.3): the §10.2 aggregates, the effect
intent/result pair, the finding disposition and consumer-coverage value types,
the content-address computation, and the pure transition rules the AC-A1/A2/A8/A9
arms name.  The question PRIMITIVE, its wait adapters and the workflow script are
2b/2c; the question ROWS and ``claim_ownership``'s row transaction live in the
store adapter (they are I/O), and the pure epoch rule the transfer turns on lives
here.

Why the two hashes are separate fields, never one (P1/P7, AC-A8).  ``subject_sha``
is what a report CLAIMS to have reviewed; ``report_bytes_sha`` is what the CAS
actually holds for that report.  ``accept`` verifies each against its own
authority — the subject against the frozen review snapshot, the bytes against the
CAS — because the findings §B2 stale-sha class is precisely a report whose
declared subject is a revision nobody reviewed, and inferring one hash from the
other would let exactly that through.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "AnswerAdmissibility",
    "AnswerDeliveryState",
    "ArtifactManifest",
    "ClaimOwnershipResult",
    "ConsumerCoverage",
    "ConsumerDisposition",
    "ConsumerState",
    "ContinuationKind",
    "DiffEntry",
    "Dispatch",
    "DispatchRole",
    "DispatchState",
    "Disposition",
    "DispositionKind",
    "EffectIntent",
    "EffectKind",
    "EffectOutcome",
    "EffectResult",
    "ExecutionTarget",
    "GateError",
    "GateQuestionError",
    "GateRound",
    "GateRun",
    "NoticeIntentState",
    "OpenFinding",
    "QuestionAnswer",
    "QuestionRefusal",
    "QuestionState",
    "RepoBinding",
    "RoundProjection",
    "RoundQuestion",
    "RoundState",
    "RunState",
    "Severity",
    "answer_admissible",
    "compute_artifact_sha",
    "epoch_supersedes",
    "may_accept_round",
    "next_question_state",
    "next_round_state",
    "next_run_state",
    "render_scratch_root",
    "run_awaiting_answer",
    "validate_ask",
    "validate_disposition",
    "validate_max_rounds",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GateError(ValueError):
    """A domain-rule violation raised by a pure transition or invariant check.

    A ``ValueError`` subclass so a caller can catch it distinctly from a pydantic
    validation error while both still read as "bad input".  The store adapter and
    ``app/gate`` translate this into a typed refusal at their own boundary; it
    never carries I/O state because nothing here does any.
    """


# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------


class RunState(StrEnum):
    """The state of a :class:`GateRun` — the loop a round is one turn of.

    ``AWAITING_ANSWER`` is a PROJECTION over blocking questions, never an
    independently writable verdict (P1): a run reads as awaiting an answer
    because a dispatch under it is, and it returns to ``OPEN`` when that clears.
    :func:`run_awaiting_answer` is the only way to compute it, so no command can
    write it directly.
    """

    OPEN = "open"
    AWAITING_ANSWER = "awaiting_answer"
    CLOSED_YES = "closed_yes"
    CLOSED_NO = "closed_no"
    ABANDONED = "abandoned"


class RoundState(StrEnum):
    """The state of a :class:`GateRound` — one turn of the loop.

    ``OPEN`` at open-round (build inputs frozen, before the builder runs);
    ``BUILT`` once the review snapshot is frozen (after build, before the ledger
    dispatch, P1); ``ADJUDICATING`` while the HIGH-tier review runs; ``YES`` or
    ``NO`` when the adjudication settles; ``ABANDONED`` when a run is torn down.
    """

    OPEN = "open"
    BUILT = "built"
    ADJUDICATING = "adjudicating"
    YES = "yes"
    NO = "no"
    ABANDONED = "abandoned"


class EffectKind(StrEnum):
    """What external operation an :class:`EffectIntent` records BEFORE it runs.

    The intent is written first and the adapter deduplicates on ``effect_id``
    (P1): a crash between recording the intent and the operation settling is
    repaired from the intent row, never re-attempted blindly.
    """

    SPAWN = "spawn"
    MERGE = "merge"
    PUSH = "push"
    REDEPLOY = "redeploy"


class EffectOutcome(StrEnum):
    """How an :class:`EffectResult` settled.

    ``UNCERTAIN`` is reconciled against read-back evidence, not retried blindly
    (R30): an uncertain merge that in fact applied must not be applied twice.
    """

    APPLIED = "applied"
    REFUSED = "refused"
    UNCERTAIN = "uncertain"


class DispatchRole(StrEnum):
    """The role a :class:`Dispatch` plays in a round.

    ``OTHER`` is a non-gate assignment: the same row backs a lane that asks a
    question with no round at all (``round_id`` NULL, P1).
    """

    BUILDER = "builder"
    LEDGER = "ledger"
    ADJUDICATE = "adjudicate"
    OTHER = "other"


class DispatchState(StrEnum):
    """The lifecycle of one assignment.

    ``AWAITING_ANSWER`` is where a suspended lane sits (A2); the store's
    projection reads a run as awaiting an answer when a blocking dispatch is here.
    A dispatch outlives a terminal incarnation, so a reap does not cascade into
    gate history (P1).
    """

    PREPARED = "prepared"
    DISPATCHED = "dispatched"
    AWAITING_ANSWER = "awaiting_answer"
    RETURNED = "returned"
    FAILED = "failed"
    ABANDONED = "abandoned"


class Severity(StrEnum):
    """A finding's severity (§10.2 ``OpenFinding``)."""

    BLOCKER = "blocker"
    SHOULD = "should"
    NIT = "nit"


class DispositionKind(StrEnum):
    """How an :class:`OpenFinding` was closed.

    ``core/findings.py`` defines ``FindingState`` with ``OPEN`` and ``RESOLVED``
    only, so this maps ``RESOLVED`` to ``FIXED`` and ADDS ``WITHDRAWN`` as a third
    member (DESIGN r1 non-blocking 1).  ``FIXED`` carries killer test/mutant
    evidence AT THE REVIEWED REVISION; ``WITHDRAWN`` carries actor and reason
    (P7) — :func:`validate_disposition` is where those two obligations are
    enforced, and AC-A2's mutant is the disposition that carries neither.
    """

    FIXED = "fixed"
    WITHDRAWN = "withdrawn"


class ConsumerState(StrEnum):
    """The disposition of one consumer of a changed symbol (§10.2, AC-A9)."""

    UPDATED = "updated"
    NOT_APPLICABLE = "n/a"
    DEFERRED = "deferred"


# ---------------------------------------------------------------------------
# Value types — the wire shapes over the rows the server owns (§10.2)
# ---------------------------------------------------------------------------

_FROZEN = ConfigDict(frozen=True, extra="forbid")


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("gate record timestamps must be timezone-aware (UTC)")
    return value


class RepoBinding(BaseModel):
    """One repository and the exact commit a manifest was taken at.

    A round can span more than one repository (root and fork), and each carries
    its OWN commit; ``fork_head`` as a single field could not (P1).
    """

    model_config = _FROZEN

    name: str = Field(min_length=1)
    commit: str = Field(min_length=1)


class DiffEntry(BaseModel):
    """One path in a manifest's diff.

    Records pre and post path, mode, pre and post object id and a deletion flag —
    not just the post-image blob.  Identical post-image blobs do not identify
    deletions, modes, symlinks, renames or changed pre-images (P7), so a manifest
    that stored only post blobs would call two genuinely different diffs the same.
    """

    model_config = _FROZEN

    pre_path: str | None = None
    post_path: str | None = None
    mode: str = ""
    pre_object_id: str | None = None
    post_object_id: str | None = None
    deleted: bool = False


class ArtifactManifest(BaseModel):
    """A round's content identity — NOT its merge authorization (§10.2, P7).

    ``compute_artifact_sha`` folds the ordered diff plus the blueprint plus the
    AC list into one address.  A rebase changes the diff and therefore the
    address; merge authorization is the SEPARATELY bound review and merge commits,
    never this hash, because a content address cannot recover which commit a human
    approved.
    """

    model_config = _FROZEN

    manifest_version: int = Field(ge=1)
    repo_bindings: tuple[RepoBinding, ...]
    base_sha: str = Field(min_length=1)
    head_sha: str = Field(min_length=1)
    branch: str = Field(min_length=1)
    worktree_path: str = Field(min_length=1)
    entries: tuple[DiffEntry, ...]
    blueprint_sha: str = Field(min_length=1)
    ac_list_sha: str = Field(min_length=1)


class ExecutionTarget(BaseModel):
    """Where a round runs; scratch paths are GENERATED from it, never typed (AC-A3).

    ``render_scratch_root`` maps ``host`` to a scratch root: a box host to
    ``~/box-scratch/<round>/``, the laptop to ``/data/cao-scratch/<round>/``.  The
    pre-runner class named ``/data/claude-scratch/...`` on a box and lost three
    arms of F783 r1; hard-coding one root is exactly the mutant AC-A3 kills.
    """

    model_config = _FROZEN

    host: str = Field(min_length=1)
    scratch_root: str = ""


class EffectIntent(BaseModel):
    """Written BEFORE the external operation; the adapter dedups on ``effect_id`` (P1)."""

    model_config = _FROZEN

    effect_id: str = Field(min_length=1)
    kind: EffectKind
    round_id: str | None = None
    dispatch_id: str | None = None
    approval_ref: str | None = None
    requested_at: datetime

    _aware = field_validator("requested_at")(classmethod(lambda cls, v: _require_aware(v)))


class EffectResult(BaseModel):
    """The settled outcome of an :class:`EffectIntent`.

    ``settled_at`` is ``None`` while the outcome is ``UNCERTAIN`` and unreconciled;
    an uncertain result is reconciled against ``evidence_ref``, not retried (R30).
    """

    model_config = _FROZEN

    effect_id: str = Field(min_length=1)
    outcome: EffectOutcome
    evidence_ref: str = ""
    settled_at: datetime | None = None

    @field_validator("settled_at")
    @classmethod
    def _aware_optional(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value)


class Dispatch(BaseModel):
    """An independent assignment aggregate (P1).

    ``round_id`` is nullable so a non-gate lane asks a question without a
    synthetic round; a terminal runs several assignments and an assignment
    outlives an incarnation, so round projection JOINS dispatches and a reap does
    not cascade into gate history.
    """

    model_config = _FROZEN

    dispatch_id: str = Field(min_length=1)
    round_id: str | None = None
    role: DispatchRole
    position: str = Field(min_length=1)
    routing_revision: str = ""
    conversation_id: str | None = None
    terminal_incarnation: str | None = None
    request_id: str = Field(min_length=1)
    effect_id: str | None = None
    pins: tuple[str, ...] = ()
    brief_blob_sha: str = ""
    state: DispatchState = DispatchState.PREPARED
    outcome: str | None = None


class Disposition(BaseModel):
    """One appended disposition on an :class:`OpenFinding` (P7).

    ``FIXED`` requires ``killer_test`` and ``killer_mutant`` at the reviewed
    revision; ``WITHDRAWN`` requires ``actor`` and ``reason``.
    :func:`validate_disposition` enforces exactly that, and AC-A2's mutant is a
    ``FIXED`` with no killer evidence.
    """

    model_config = _FROZEN

    kind: DispositionKind
    reviewed_artifact_sha: str = ""
    killer_test: str | None = None
    killer_mutant: str | None = None
    actor: str | None = None
    reason: str | None = None
    at: datetime

    _aware = field_validator("at")(classmethod(lambda cls, v: _require_aware(v)))


class OpenFinding(BaseModel):
    """A finding whose identity travels round to round; membership is snapshotted.

    ``statement`` is immutable once raised.  A round closes a finding by APPENDING
    a disposition that names its killer; an OPEN finding (no terminal disposition)
    is automatically in the next round's brief (AC-A2).
    """

    model_config = _FROZEN

    finding_id: str = Field(min_length=1)
    raised_in_round: str = Field(min_length=1)
    severity: Severity
    statement: str = Field(min_length=1)
    dispositions: tuple[Disposition, ...] = ()

    @property
    def is_open(self) -> bool:
        """True until a disposition (FIXED or WITHDRAWN) has been appended."""
        return len(self.dispositions) == 0


class ConsumerDisposition(BaseModel):
    """One consumer of a changed symbol and what happened to it (§10.2, AC-A9)."""

    model_config = _FROZEN

    symbol: str = Field(min_length=1)
    site: str = Field(min_length=1)
    state: ConsumerState


class ConsumerCoverage(BaseModel):
    """Revision-bound consumer enumeration; replaces the ``consumers_checked`` bool.

    ``xref_sha`` and ``reviewed_artifact_sha`` bind the coverage to one revision,
    so it is NOT a completeness claim (P1/P7): a BLOCKER closes only with a
    revision-bound xref, enumerated dispositions and an ``unresolved_dynamic``
    list of readers AST cannot enumerate.  AC-A9's mutant accepts a completeness
    claim from AST alone — i.e. coverage with no revision binding.
    """

    model_config = _FROZEN

    xref_sha: str = Field(min_length=1)
    reviewed_artifact_sha: str = Field(min_length=1)
    consumers: tuple[ConsumerDisposition, ...] = ()
    unresolved_dynamic: tuple[str, ...] = ()


class GateRun(BaseModel):
    """The loop; a :class:`GateRound` is one turn of it (§10.2).

    ``owner_conversation``/``owner_epoch`` fence the supervisor conversation (P2);
    ``max_rounds`` of 0 is rejected at open (:func:`validate_max_rounds`), and
    exhaustion stops the run without opening an unused successor.  ``row_version``
    is optimistic concurrency: a stale writer is refused by the store.
    """

    model_config = _FROZEN

    run_id: str = Field(min_length=1)
    wp: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    workflow_source_sha: str = ""
    input_sha: str = ""
    owner_conversation: str = Field(min_length=1)
    owner_epoch: int = Field(ge=0)
    max_rounds: int = Field(gt=0)
    row_version: int = Field(ge=1)
    state: RunState = RunState.OPEN


class GateRound(BaseModel):
    """One turn of the loop (§10.2).

    ``build_inputs`` is frozen at open-round, BEFORE the builder runs;
    ``review_snapshot`` is frozen AFTER the build and before the ledger dispatch
    (P1), so a change to the reviewed bytes after the freeze opens a SUCCESSOR
    round rather than mutating this one.  ``subject_sha`` and ``report_bytes_sha``
    are verified SEPARATELY at accept (AC-A8).  ``generation`` is the staleness
    test; ``row_version`` is optimistic concurrency.
    """

    model_config = _FROZEN

    round_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    round_no: int = Field(ge=1)
    predecessor_round_id: str | None = None
    build_inputs: ArtifactManifest
    review_snapshot: ArtifactManifest | None = None
    execution_target: ExecutionTarget
    test_command: str = ""
    evidence_tier: str = ""
    fixture_corpus_sha: str | None = None
    fixture_frame_count: int | None = None
    subject_sha: str | None = None
    report_bytes_sha: str | None = None
    verdict_report_sha: str | None = None
    state: RoundState = RoundState.OPEN
    generation: int = Field(ge=0)
    row_version: int = Field(ge=1)
    created_at: datetime
    closed_at: datetime | None = None

    @field_validator("created_at")
    @classmethod
    def _aware_created(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @field_validator("closed_at")
    @classmethod
    def _aware_closed(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value)


# ---------------------------------------------------------------------------
# Pure functions — invariants, transitions and the content address
# ---------------------------------------------------------------------------


def validate_max_rounds(max_rounds: int) -> int:
    """Return ``max_rounds`` if it is a usable bound, else raise (§10.2, AC-A1 loop).

    Zero is rejected at open-run: a loop with a zero bound would open no round and
    could never close, so the blueprint rejects it rather than admitting a run
    that does nothing.  The ``GateRun`` model's ``gt=0`` field enforces the same
    rule at construction; this function exists so a command can reject the value
    BEFORE building the row and return a typed :class:`GateError`.
    """
    if max_rounds <= 0:
        raise GateError(f"max_rounds must be greater than 0, got {max_rounds}")
    return max_rounds


def compute_artifact_sha(manifest: ArtifactManifest) -> str:
    """The content address a round is identified by (D6, §10.2, AC-A1).

    A sha-256 over the ORDERED ``(path, blob_sha)`` list of the diff, plus the
    blueprint sha and the AC-list sha.  Deterministic and order-preserving: the
    same manifest always yields the same address, and two manifests differing only
    in diff order are DIFFERENT addresses, because order is part of a diff's
    meaning.  Rendered briefs derive their shas from this (never a caller-supplied
    ``head_sha``), which is the whole of AC-A1's guarantee — the mutant is a
    template that accepts a free-text sha slot.

    The pre-image object id and the deletion flag enter the digest too: a diff
    that deletes a path and one that empties it share a post blob but are not the
    same change (P7).
    """
    hasher = hashlib.sha256()

    def _feed(label: str, value: str) -> None:
        hasher.update(label.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(value.encode("utf-8"))
        hasher.update(b"\x1e")  # record separator

    _feed("manifest_version", str(manifest.manifest_version))
    for binding in manifest.repo_bindings:
        _feed("repo", f"{binding.name}@{binding.commit}")
    _feed("base", manifest.base_sha)
    _feed("head", manifest.head_sha)
    _feed("branch", manifest.branch)
    for entry in manifest.entries:
        _feed(
            "entry",
            "|".join(
                (
                    entry.pre_path or "",
                    entry.post_path or "",
                    entry.mode,
                    entry.pre_object_id or "",
                    entry.post_object_id or "",
                    "1" if entry.deleted else "0",
                )
            ),
        )
    _feed("blueprint", manifest.blueprint_sha)
    _feed("ac_list", manifest.ac_list_sha)
    return hasher.hexdigest()


def render_scratch_root(target: ExecutionTarget, round_id: str) -> str:
    """Generate a round's scratch root from its host (AC-A3).

    A box host (anything beginning ``grok-box`` or ``box-``) renders
    ``~/box-scratch/<round>/``; the laptop renders ``/data/cao-scratch/<round>/``.
    The path is DERIVED, never typed: hard-coding one root is the mutant AC-A3
    kills, and the pre-runner's ``/data/claude-scratch/...`` on a box is the
    negative arm.  A round id is required so two rounds on one host never share a
    scratch directory.
    """
    if not round_id:
        raise GateError("a scratch root cannot be generated without a round id")
    host = target.host
    if host.startswith("grok-box") or host.startswith("box-"):
        return f"~/box-scratch/{round_id}/"
    if host == "laptop":
        return f"/data/cao-scratch/{round_id}/"
    # An unknown host is not silently given the laptop root: that is how the
    # pre-runner put a laptop path on a box.  A caller that means the laptop says
    # so; anything else is a typed refusal.
    raise GateError(f"no scratch root is defined for host {host!r}")


def next_run_state(current: RunState, target: RunState) -> RunState:
    """Return ``target`` if ``current -> target`` is a legal run transition, else raise.

    ``AWAITING_ANSWER`` is NOT reachable through this function: it is a projection
    over blocking questions (P1), computed by :func:`run_awaiting_answer`, never a
    written verdict.  A command that tried to move a run to ``AWAITING_ANSWER``
    directly is refused here.
    """
    legal: dict[RunState, frozenset[RunState]] = {
        RunState.OPEN: frozenset({RunState.CLOSED_YES, RunState.CLOSED_NO, RunState.ABANDONED}),
        RunState.AWAITING_ANSWER: frozenset({RunState.OPEN, RunState.ABANDONED}),
        RunState.CLOSED_YES: frozenset(),
        RunState.CLOSED_NO: frozenset(),
        RunState.ABANDONED: frozenset(),
    }
    if target is RunState.AWAITING_ANSWER:
        raise GateError(
            "AWAITING_ANSWER is a projection over blocking questions, not a " "writable run state"
        )
    if target not in legal.get(current, frozenset()):
        raise GateError(f"illegal run transition {current.value} -> {target.value}")
    return target


def next_round_state(current: RoundState, target: RoundState) -> RoundState:
    """Return ``target`` if ``current -> target`` is a legal round transition, else raise.

    The blueprint's sequence is builder, freeze, LOW ledger, HIGH adjudication:
    ``OPEN -> BUILT`` (the review-snapshot freeze) ``-> ADJUDICATING -> YES|NO``.
    ``ABANDONED`` is reachable from any non-terminal state (a run torn down).  A
    ``YES``/``NO`` round is terminal.
    """
    legal: dict[RoundState, frozenset[RoundState]] = {
        RoundState.OPEN: frozenset({RoundState.BUILT, RoundState.ABANDONED}),
        RoundState.BUILT: frozenset({RoundState.ADJUDICATING, RoundState.ABANDONED}),
        RoundState.ADJUDICATING: frozenset({RoundState.YES, RoundState.NO, RoundState.ABANDONED}),
        RoundState.YES: frozenset(),
        RoundState.NO: frozenset(),
        RoundState.ABANDONED: frozenset(),
    }
    if target not in legal.get(current, frozenset()):
        raise GateError(f"illegal round transition {current.value} -> {target.value}")
    return target


def run_awaiting_answer(dispatch_states: Sequence[DispatchState]) -> bool:
    """Compute whether a run reads as ``AWAITING_ANSWER`` (P1).

    True exactly when at least one dispatch under the run is in
    ``AWAITING_ANSWER``.  The projection, not a stored verdict: the store reads a
    run's effective state through this rather than persisting ``AWAITING_ANSWER``
    on the run row, so the state can never be stale relative to its own cause.
    """
    return any(state is DispatchState.AWAITING_ANSWER for state in dispatch_states)


def validate_disposition(disposition: Disposition) -> Disposition:
    """Return the disposition if it carries its required evidence, else raise (P7, AC-A2).

    ``FIXED`` MUST carry killer test and killer mutant evidence at the reviewed
    revision; ``WITHDRAWN`` MUST carry an actor and a reason.  A ``FIXED`` with no
    killer evidence, or a ``WITHDRAWN`` with no actor/reason, is refused — which is
    exactly the mutant AC-A2 kills (allow FIXED with no disposition evidence).
    """
    if disposition.kind is DispositionKind.FIXED:
        if not disposition.reviewed_artifact_sha:
            raise GateError("a FIXED disposition must name the reviewed artifact sha")
        if not disposition.killer_test or not disposition.killer_mutant:
            raise GateError(
                "a FIXED disposition must carry killer test AND killer mutant "
                "evidence at the reviewed revision"
            )
    else:  # WITHDRAWN
        if not disposition.actor or not disposition.reason:
            raise GateError("a WITHDRAWN disposition must carry an actor and a reason")
    return disposition


def may_accept_round(
    round_: GateRound,
    *,
    declared_subject_sha: str,
    declared_report_bytes_sha: str,
    cas_report_bytes_sha: str,
) -> None:
    """Verify a round may be accepted, checking BOTH hashes separately (AC-A8, P1).

    ``accept`` refuses a report whose declared subject hash is not the round's
    frozen review-snapshot address, AND refuses one whose declared report bytes
    hash is not the sha the CAS actually holds.  The two checks are independent:
    inferring one from the other is the mutant AC-A8 kills, and a stale subject
    sha (findings §B2) is caught by the subject check even when the bytes are
    genuinely in the CAS.

    Raises :class:`GateError` on the first check that fails; returns ``None`` when
    both pass.  Pure: the CAS bytes-sha is an argument, read by the caller.
    """
    if round_.state is not RoundState.YES:
        raise GateError(f"a round may be accepted only from YES, not {round_.state.value}")
    if round_.review_snapshot is None:
        raise GateError("a round with no frozen review snapshot cannot be accepted")
    subject = compute_artifact_sha(round_.review_snapshot)
    if declared_subject_sha != subject:
        raise GateError(
            "declared subject hash does not match the frozen review snapshot: "
            f"declared {declared_subject_sha!r}, snapshot {subject!r}"
        )
    if declared_report_bytes_sha != cas_report_bytes_sha:
        raise GateError(
            "declared report bytes hash does not match the CAS: "
            f"declared {declared_report_bytes_sha!r}, CAS {cas_report_bytes_sha!r}"
        )


def epoch_supersedes(current_epoch: int, new_epoch: int) -> bool:
    """Whether ``new_epoch`` may take ownership over ``current_epoch`` (P2, AC-A12).

    Ownership is monotonic in the epoch: a claim succeeds only when its epoch is
    STRICTLY greater than the highest recorded for the outgoing conversation.  A
    lower or equal epoch is refused, which is the mutant AC-A12 kills (accept a
    non-increasing epoch).  The store's ``claim_ownership`` transaction turns on
    exactly this rule.
    """
    return new_epoch > current_epoch


# ---------------------------------------------------------------------------
# The question primitive (WP-ARCH Amendment A, slice B1; §10.2 P2, A2).
#
# A durable question is how a lane STOPS AND ASKS without dying.  The rows it
# turns on already exist (``round_question``, ``question_answer``,
# ``answer_delivery_intent``, created by 2a because ``claim_ownership`` rewrites
# them); B1 adds the pure vocabulary those rows are read and written through, so
# every rule a caller can get wrong — which transitions are legal, when an ask is
# well formed, whether an answer is still admissible — is decidable with no
# database and is tested by enumeration here.
#
# The waiting STATE is deliberately not a new ``TerminalStatus`` member: it is
# ``QuestionState`` plus :class:`DispatchState.AWAITING_ANSWER` plus
# :func:`run_awaiting_answer`.  ``WAITING_USER_ANSWER`` on a terminal is the
# PROVIDER-DIALOG concept (a permission card in a pane) and conflating the two
# would make one word mean two different waits.
# ---------------------------------------------------------------------------


class QuestionState(StrEnum):
    """The lifecycle of one durable question (§10.2 P2, AC-A10).

    ``PENDING`` and ``ESCALATED`` are both OPEN: each holds the one-open-question
    slot the ``ux_question_open`` partial index enforces, which is why escalating
    cannot be modelled as closing and re-asking.  ``ANSWERED`` and ``EXPIRED`` are
    terminal — a question settles exactly once, and an answer racing its own
    expiry is decided by one conditional transaction in the store, never by two
    writes that could both win.
    """

    PENDING = "PENDING"
    ESCALATED = "ESCALATED"
    ANSWERED = "ANSWERED"
    EXPIRED = "EXPIRED"


class ContinuationKind(StrEnum):
    """What the asker will resume INTO when the answer arrives (A2).

    ``ASSIGNMENT`` is a worker lane suspended inside its own tool call: the
    continuation ref is the dispatch, and resuming means the still-open call
    returns.  ``JOURNAL_CHECKPOINT`` is a workflow script (slice C): the
    continuation ref is ``<run_id>:<checkpoint_id>``, and resuming means the
    re-executing script reads the journaled answer instead of asking again.  The
    two differ in WHERE the continuation lives, which is why the kind is stored
    rather than inferred from whether ``round_id`` is NULL.
    """

    ASSIGNMENT = "ASSIGNMENT"
    JOURNAL_CHECKPOINT = "JOURNAL_CHECKPOINT"


class NoticeIntentState(StrEnum):
    """The state of the notification intent committed WITH the question (§10.2 P2).

    The intent is written inside the ask transaction and settled AFTER it: a
    notice that was never sent is therefore a ``PENDING`` row a sweep can find,
    not a question nobody will ever hear about.  ``FAILED`` is retryable and
    deliberately distinct from ``PENDING`` so a sweep can tell "not attempted"
    from "attempted and lost".
    """

    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class AnswerDeliveryState(StrEnum):
    """The state of the answer's delivery back to the asker (§10.2 P2, R28).

    ``CONSUMED`` is a SEPARATE record from ``ANSWERED`` on purpose: a committed
    answer the asker never received is the exact crash window R28 names, and an
    ANSWERED question with a ``PENDING`` delivery intent is how that window is
    visible rather than indistinguishable from success.  The member list matches
    the ``answer_delivery_intent`` CHECK constraint 2a already shipped.
    """

    PENDING = "PENDING"
    SENT = "SENT"
    CONSUMED = "CONSUMED"
    FAILED = "FAILED"


class QuestionRefusal(StrEnum):
    """The typed reasons a question command is refused.

    A code rather than a message because these cross an HTTP boundary and a
    caller branches on them: ``ask_supervisor`` retried with the same
    ``client_request_id`` must be able to tell "you already have an open question"
    from "your epoch was superseded", and a prose string cannot be branched on
    without parsing English.  An ``IntegrityError`` from ``ux_question_open``
    leaking to a lane would be exactly that unbranchable failure.
    """

    DISPATCH_UNKNOWN = "E_DISPATCH_UNKNOWN"
    QUESTION_EMPTY = "E_QUESTION_EMPTY"
    QUESTION_OPEN = "E_QUESTION_OPEN"
    QUESTION_NOT_FOUND = "E_QUESTION_NOT_FOUND"
    QUESTION_SETTLED = "E_QUESTION_SETTLED"
    QUESTION_EXPIRED = "E_QUESTION_EXPIRED"
    OWNER_MISMATCH = "E_OWNER_MISMATCH"
    EPOCH_SUPERSEDED = "E_EPOCH_SUPERSEDED"
    ANSWER_CONFLICT = "E_ANSWER_CONFLICT"
    DEFAULT_REQUIRED = "E_DEFAULT_REQUIRED"
    ILLEGAL_TRANSITION = "E_ILLEGAL_TRANSITION"


class GateQuestionError(GateError):
    """A refused question command, carrying a branchable :class:`QuestionRefusal`.

    A ``GateError`` subclass so every existing ``except GateError`` still catches
    it, with ``code`` added so the API can translate one refusal into one status
    and one machine-readable body instead of flattening every domain refusal into
    the same 400.
    """

    def __init__(self, code: QuestionRefusal, message: str) -> None:
        super().__init__(message)
        self.code = code


class RoundQuestion(BaseModel):
    """One durable question (§10.2 P2).

    ``round_id`` is nullable: a non-gate lane asks with no round at all, which is
    why the question is bound to a DISPATCH and only optionally to a round.
    ``owner_conversation``/``owner_epoch`` are the supervisor fence — they are
    rewritten by ``claim_ownership`` while the question is open, so an answer from
    a superseded conversation is refused rather than silently accepted.
    ``default_answer`` is present only on a NON-blocking ask, where the asker
    continues immediately and needs a caller-owned value to continue WITH.
    """

    model_config = _FROZEN

    question_id: str = Field(min_length=1)
    dispatch_id: str = Field(min_length=1)
    round_id: str | None = None
    client_request_id: str = Field(min_length=1)
    owner_conversation: str = Field(min_length=1)
    owner_epoch: int = Field(ge=0)
    continuation_kind: ContinuationKind
    continuation_ref: str = ""
    asked_at: datetime
    expires_at: datetime
    question: str = Field(min_length=1)
    options: tuple[str, ...] = ()
    answer_schema: str | None = None
    default_answer: str | None = None
    blocking: bool = True
    state: QuestionState = QuestionState.PENDING
    answer_event_id: str | None = None
    consumed_at: datetime | None = None
    user_prompt_id: str | None = None
    row_version: int = Field(ge=1)

    @field_validator("asked_at", "expires_at")
    @classmethod
    def _aware_stamps(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @field_validator("consumed_at")
    @classmethod
    def _aware_consumed(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value)

    @property
    def is_open(self) -> bool:
        """True while the question holds the dispatch's one open-question slot."""
        return self.state in (QuestionState.PENDING, QuestionState.ESCALATED)


class QuestionAnswer(BaseModel):
    """One submitted answer, append-only (§10.2 P2).

    Append-only and keyed by its own event id so an answer is a FACT with a time
    and an author, not a mutable field on the question.  ``client_request_id``
    makes an identical retry return this row; a DIFFERENT answer under the same
    request id is a conflict, because silently overwriting would let a retried
    call change a decision the asker may already have acted on.
    """

    model_config = _FROZEN

    answer_event_id: str = Field(min_length=1)
    question_id: str = Field(min_length=1)
    answer: str
    answered_by: str = Field(min_length=1)
    answered_at: datetime
    client_request_id: str = Field(min_length=1)

    _aware = field_validator("answered_at")(classmethod(lambda cls, v: _require_aware(v)))


class AnswerAdmissibility(BaseModel):
    """Whether an answer may be recorded, and if not, why (AC-A12).

    A value rather than a bare bool because every refusal here becomes a typed
    HTTP body: the caller must be able to distinguish "too late" from "not yours"
    without reading prose.
    """

    model_config = _FROZEN

    admissible: bool
    code: QuestionRefusal | None = None
    reason: str = ""


def next_question_state(current: QuestionState, target: QuestionState) -> QuestionState:
    """Return ``target`` if ``current -> target`` is legal, else raise (AC-A10).

    ``ESCALATED`` is reachable ONLY from ``PENDING``: escalation is a one-way
    step deeper into the same open slot, so a question cannot be de-escalated
    back into the state a sweep treats as un-raised.  ``ANSWERED`` and ``EXPIRED``
    are terminal from either open state and from nothing else — a settled
    question re-settling is the mutant this table kills, because it would let an
    expiry overwrite an answer the asker already consumed.
    """
    legal: dict[QuestionState, frozenset[QuestionState]] = {
        QuestionState.PENDING: frozenset(
            {QuestionState.ESCALATED, QuestionState.ANSWERED, QuestionState.EXPIRED}
        ),
        QuestionState.ESCALATED: frozenset({QuestionState.ANSWERED, QuestionState.EXPIRED}),
        QuestionState.ANSWERED: frozenset(),
        QuestionState.EXPIRED: frozenset(),
    }
    if target not in legal.get(current, frozenset()):
        raise GateQuestionError(
            QuestionRefusal.ILLEGAL_TRANSITION,
            f"illegal question transition {current.value} -> {target.value}",
        )
    return target


def validate_ask(
    *,
    blocking: bool,
    default_answer: str | None,
    question: str,
    asked_at: datetime,
    expires_at: datetime,
) -> None:
    """Check an ask is well formed BEFORE a row is minted (A2).

    Two rules, and the second is the one worth stating.  A BLOCKING ask needs no
    default: the caller is suspended inside its own tool call and will receive the
    real answer or an expiry, so a default would be a value nothing ever reads.  A
    NON-BLOCKING ask MUST carry one, because the caller continues immediately and
    something must decide what it continues WITH — leaving that to the server
    would put a policy decision in the wrong process, and leaving it empty would
    let a lane proceed on a silently-invented answer.

    The expiry window is also checked here: an ``expires_at`` at or before
    ``asked_at`` is a question that is born expired, which the sweep would settle
    before any surface could show it.
    """
    if not question.strip():
        raise GateQuestionError(
            QuestionRefusal.QUESTION_EMPTY, "a question must carry non-empty text"
        )
    if not blocking and (default_answer is None or default_answer == ""):
        raise GateQuestionError(
            QuestionRefusal.DEFAULT_REQUIRED,
            "a non-blocking ask must carry a caller-owned default answer: the "
            "caller continues immediately and something must decide what with",
        )
    if expires_at <= asked_at:
        raise GateQuestionError(
            QuestionRefusal.QUESTION_EXPIRED,
            f"expires_at {expires_at.isoformat()} is not after asked_at "
            f"{asked_at.isoformat()}: the question would be born expired",
        )


def answer_admissible(
    question: RoundQuestion,
    *,
    caller_conversation: str,
    caller_epoch: int,
    now: datetime,
) -> AnswerAdmissibility:
    """Whether ``question`` may still be answered by this caller (AC-A12, P2).

    Three independent checks, in the order a caller most needs them.

    1. **Settled.**  ``ANSWERED``/``EXPIRED`` are terminal; a late answer is
       refused rather than appended, so an asker that already consumed one answer
       can never be handed a second.
    2. **Expired in wall-clock terms.**  A row still reading ``PENDING`` past its
       ``expires_at`` is refused HERE even before the sweep has run, so the answer
       and the expiry cannot both be admitted by a race the sweep's cadence opens.
    3. **Ownership.**  The conversation must match, and the caller's epoch must not
       be SUPERSEDED by the row's.  :func:`epoch_supersedes` is reused rather than
       re-derived: ownership is monotonic in the epoch in exactly one place, and a
       second spelling of that rule is how the two could come to disagree.  A
       caller whose epoch is AHEAD of the row is admitted — that is a claim that
       has not yet rewritten this row, not a stale answer.
    """
    if question.state in (QuestionState.ANSWERED, QuestionState.EXPIRED):
        return AnswerAdmissibility(
            admissible=False,
            code=QuestionRefusal.QUESTION_SETTLED,
            reason=f"question {question.question_id} is already {question.state.value}",
        )
    if now >= question.expires_at:
        return AnswerAdmissibility(
            admissible=False,
            code=QuestionRefusal.QUESTION_EXPIRED,
            reason=(
                f"question {question.question_id} expired at " f"{question.expires_at.isoformat()}"
            ),
        )
    if caller_conversation != question.owner_conversation:
        return AnswerAdmissibility(
            admissible=False,
            code=QuestionRefusal.OWNER_MISMATCH,
            reason=(
                f"question {question.question_id} is owned by "
                f"{question.owner_conversation}, not {caller_conversation}"
            ),
        )
    if epoch_supersedes(caller_epoch, question.owner_epoch):
        return AnswerAdmissibility(
            admissible=False,
            code=QuestionRefusal.EPOCH_SUPERSEDED,
            reason=(
                f"caller epoch {caller_epoch} is superseded by the question's "
                f"owner epoch {question.owner_epoch}"
            ),
        )
    return AnswerAdmissibility(admissible=True)


# ---------------------------------------------------------------------------
# Projection shapes returned BY the store (§10.2).
#
# These live in ``core`` rather than ``app`` for one structural reason: the store
# adapter RETURNS them, and ``adapters-are-leaves`` forbids ``adapters`` from
# importing ``app``.  ``core/ports.py``'s ``StateProjection`` and
# ``core/delivery.py``'s ``QueueMessage`` sit here for the same reason — a value an
# adapter hands back is core vocabulary, not application vocabulary.
# ---------------------------------------------------------------------------


class RoundProjection(BaseModel):
    """A whole round assembled from rows, for AC-A1's zero-markdown re-projection.

    Everything the renderers need to draw a brief/ledger/verdict header without
    reading a single markdown file: the run and round rows, the dispatches under
    the round, the effect intents and their (possibly absent) results, the open
    findings carried into it and the consumer coverage bound to its reviewed
    artifact.  The store builds this by JOIN so the membership snapshot is the
    store's single authority, not something ``app`` reconstructs.
    """

    model_config = _FROZEN

    run: GateRun
    round: GateRound
    dispatches: tuple[Dispatch, ...] = ()
    effect_intents: tuple[EffectIntent, ...] = ()
    effect_results: tuple[EffectResult, ...] = ()
    open_findings: tuple[OpenFinding, ...] = ()
    consumer_coverage: tuple[ConsumerCoverage, ...] = ()
    #: The round's questions, so a re-projected round shows what it is waiting
    #: on.  Defaulted to empty so every 2a construction site stays valid.
    questions: tuple[RoundQuestion, ...] = ()


class ClaimOwnershipResult(BaseModel):
    """The outcome of a ``claim_ownership`` transaction (P2, AC-A12).

    ``accepted`` is False for a refused claim (a non-increasing epoch); the counts
    report how many runs and questions the accepted claim rewrote, which the
    ``ownership_transfer`` audit row also records.  ``transfer_id`` is the id of
    that append-only row, present only on an accepted claim.
    """

    model_config = _FROZEN

    accepted: bool
    runs_rewritten: int = 0
    questions_rewritten: int = 0
    transfer_id: str | None = None
    reason: str = ""
