# WP-ARCH 2b — laptop flip acceptance checklist

The box round (`live-round.sh`) answers `FLIP-READY-BOX`. That verdict covers
only the criteria a box can reach. **This file covers the rest, and both must be
complete before `CAO_WORKER_TRUTH_STATUS` is turned on.**

It exists because a criterion that moves out of the box round has to land
somewhere that can refuse it. Two criteria were re-scoped here precisely because
no automated box workload can exercise them — and a criterion carried by nothing
but a sentence in a README is a criterion that gets skipped the week everyone is
busy. Fill the evidence fields in; an empty field is a NO.

Run against: fork sha `________________`  ·  date `____________`  ·  operator `____________`

---

## 1. `prompt-awaiting` — a real permission card, answered by a human

Why it cannot be done on a box: every box lane spawns
`--dangerously-skip-permissions` and the add-terminal endpoint has no parameter
to disable it, so no real card renders. A printed card does not substitute,
because `_is_ink_selection_waiting` requires the card to be the live bottom
region at the instant the sampler fires.

Setup: a projected lane (its provider in `CAO_WORKER_TRUTH_STATUS_PROVIDERS`)
spawned WITHOUT a permissions-skip flag, then given work that raises a card.

| Evidence | Value |
|---|---|
| Terminal id | `________` |
| Provider | `________` |
| Card seen at (UTC) | `________` |
| `prompt.awaiting` event id | `________` |
| `status.transition` to `awaiting_input`, event id | `________` |
| Answer given (which option) | `________` |
| `prompt.answered` event id | `________` |
| Fleet row showed `waiting` while the card was up (y/n) | `____` |

Passes when: the card raised exactly ONE `prompt.awaiting` (not one per
repaint), the projection reached `awaiting_input`, answering it produced
`prompt.answered`, and the terminal returned to a normal status afterwards.

```bash
# the two events and the transition between them
cao diag --terminal <id> | grep -E 'prompt\.(awaiting|answered)|awaiting_input'
```

## 2. `capped-parity` — a codex seat that actually hits its cap

Why it cannot be done on a box: no provider available on a box is both
projectable and cappable. `claude_code` has no entry in the cap-pattern table at
all, and codex — the one provider that is both — is not reliably authenticated
on the fleet.

This one cannot be scheduled; it is caught when it happens. Keep the seat's
capture when it does.

| Evidence | Value |
|---|---|
| Terminal id | `________` |
| Cap banner text, verbatim from the pane | `________` |
| `usage.capped` event id | `________` |
| Was the terminal projected at the time (y/n) | `____` |
| Fleet row `condition` showed `CAPPED` (y/n) | `____` |
| Projection row state + `status_since` | `________` |
| Same reading with the cutover OFF (the parity half) | `________` |

Passes when: `usage.capped` fired for a PROJECTED terminal — the half the box
round cannot reach, since `usage.capped` is a `DERIVED_ALWAYS_KIND` that must
bypass source precedence — and the observable result is identical to the
cutover-off reading.

```bash
cao diag --terminal <id> | grep -E 'usage\.capped|CAPPED'
```

## 3. `certified-pane-silence` — deferred, not carried here

Scope `PENDING-COHORT`. No certified herdr cohort exists yet, so neither the box
round nor this checklist can reach it. It is WP-HERDR's to deliver; the box
round's check arms itself automatically once the H1 seam is armed and a cohort
exists. **Do not tick this as done from the laptop** — there is nothing to tick.

---

## Sign-off

Both sections above complete, with the box round's `FLIP-READY-BOX: YES` for the
same sha:

* box round report path: `________________`
* box round sha: `________________` (must equal the sha at the top of this file)
* `FLIP-READY-BOX` line: `________________`
* flip approved by: `____________`  ·  date `____________`

A `FLIP-READY-BOX: YES` is **necessary and not sufficient**. If either section
above is blank, the flip does not happen.
