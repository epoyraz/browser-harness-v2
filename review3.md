# Code review 3 — state of v2 at `9e18c40` (+ branch `corpus-vocabulary-and-fetch-fixes`)

Reviewed 2026-08-28. Scope: the 42 commits on `master` since `a53f40e` (the last full
review), the two commits on my branch, CI history, docs, and repo hygiene.

**Verdict.** The code is in the best structural shape it has been in — the three-package
split (`harness/`, `applications/`, `evidence/`) is real, the dependency direction holds,
three dead subsystems were deleted with reasons, `ruff` is clean and 628 tests collect. But
**the project's own quality gate has been off for a week.** CI on `master` has failed on
27 consecutive pushes since `a53f40e` on 21 Aug; the last green run is `333fb49`. During
that week a real regression could not have been noticed by the mechanism built to notice it.

---

## 1. Blocking — CI has been red on `master` for 27 consecutive pushes

Last green: `333fb49`, 2026-08-21 11:52. First red: `a53f40e`, 2026-08-21 13:09. Every
push since, on every OS leg of the matrix, has failed. Classified from each run's failed
log:

| cause | runs | legs |
| --- | ---: | --- |
| macOS `AF_UNIX` socket path over 104 bytes | 3 | macOS |
| Windows-only pytest failures | 12 | Windows |
| Windows pytest + one flaky macOS test | 8 | Windows, macOS |
| `ruff` `I001` (dead import) | 3 | all three |
| leg not captured in log | 1 | — |

Ubuntu never failed at pytest in any run. The code is healthy on Linux; the suite is not
portable, and the last three pushes fail before pytest even runs.

### 1a. The three `ruff` failures are a dead import, not a sort order

`tests/live/perf_check.py:30` imports `harness.core.trace`; `tests/bench/joblens.py`
imports `harness.core.bench`. Neither module exists — `e8cd4b4` moved the evidence layer to
`evidence/trace.py` and `evidence/bench.py`. `ruff` cannot resolve the import, classifies it
as third-party, and wants it re-sorted; the `I001` is a symptom. Both files are broken;
neither is collected by pytest, so ruff is the only thing that noticed, and only by accident.

Local `ruff check .` passes because `.venv` has ruff **0.16.1** while `uv.lock` pins
**0.16.4**. `uv run ruff check harness tests` reproduces CI exactly. The dev environment is
behind the lockfile.

### 1b. Ten tests encode POSIX assumptions and have never been green on Windows

Verified locally on this machine (Windows, the author's environment):

- `tests/unit/test_endpoint.py` — 8 failures. Tests monkeypatch `HOME` to
  `/home/tester`; on Windows `Path.home()` reads `USERPROFILE` and ignores it. One asserts
  a full path inside a message that abbreviates with `~`. Two expect `application="Brave
  Browser"` from a subprocess (`_mac_application_for_profile`) that returns `""` off macOS.
- `tests/unit/test_client.py` — 2 failures. `test_a_dead_reader_marks_the_connection_
  closed…` and `test_a_call_after_the_reader_dies…` kill the reader with
  `sock.shutdown(SHUT_RD)`, which on POSIX makes `recv()` return `b""`. On Windows a
  blocked `recv()` is not woken by `SHUT_RD`, so the reader never observes anything,
  `_closed` stays `False`, and the 5 s deadline expires. The product code is correct; the
  test's mechanism is platform-specific. **These fail at `acaa69a` itself, the commit that
  introduced them** — verified in a worktree. The commit shipped red.

### 1c. macOS: one flaky test per run, a different one each time

`a025035`: `test_late_owned_popup_is_closed_before_the_worker_is_reused` (1 of 638).
`65d3b60`: `test_a_daemon_whose_browser_died_can_be_used_again` (1 of 610). Timing tests
with no slack for a slow runner.

### 1d. The first red was a change I reviewed and called good

`a53f40e` replaced `/tmp/bhaio{pid}` with pytest's `tmp_path` in the `test_aio` fixture. I
praised it as portable. On the macOS runner `tmp_path` is ~100 characters and the socket
path came out at 133 bytes, over the AF_UNIX limit the file `ipc.py` documents in its own
header. Three runs failed on it until `conftest.py` reverted to `/tmp/bhs{pid}`. The
`test_aio` suite is gone now, but the lesson stands: the `SUN_PATH_MAX` constraint is
written down in the code and I did not check the change against it.

### Fix list, in order

1. `perf_check.py` and `joblens.py`: `harness.core.{trace,bench}` → `evidence.{trace,bench}`.
2. The 10 Windows tests: `monkeypatch.setattr(Path, "home", …)` for the endpoint eight;
   `sock.close()` or a platform skip with a stated reason for the client two.
3. The two flaky macOS tests: a real deadline or `@pytest.mark.slow`.
4. `uv sync` locally so the dev ruff matches the lock.

---

## 2. Structure — the split is real, and it left no landmines

`harness/` 11,007 lines · `applications/` 1,355 · `evidence/` 1,647 · `tools/` 4,518 ·
`tests/` 13,731 (628 tests: 568 unit, 60 evidence).

- `05593e0` states the invariant — `applications/` imports `harness`, nothing under
  `harness/` imports `applications` — and the namespace test enforces it in both
  directions. I re-checked `harness/session.py` for application names (none) but did not
  independently re-verify the `evidence/` direction.
- **No tracked script calls the application layer bare.** After the split, `run_application()`
  is only in a `bh` namespace via `applications.install(globals())`; I grepped
  `dashboard/`, `tests/live/` and `tools/` for bare calls without an install — none.
- Three deletions, each with a body that says why: the async frontend (`84e4bb3`: "393
  lines maintaining a second transport for a frontend nobody imported"), the CDP cassette
  (same commit), automatic endpoint extraction (`5599e38`: 1,241 lines, "no caller", and it
  ran on the reader thread for every CDP event). All three findings from `review.md` on
  `dca0c6f`/`a53f40e` are therefore moot.

---

## 3. The coverage gate does not cover the two new packages

CI's step *"every module has a test file (DESIGN.md §2.2)"* walks `harness/` only. Run
against the new packages:

```
applications/  4 of 5 modules with no tests/unit/test_<module>.py
               (document, ontology, state, workflow)
evidence/      1 of 8   (cli.py)
```

For `applications/` this is a naming mismatch rather than absent tests —
`test_field_ontology.py` and `test_application_decisions.py` exist — but the gate cannot
see them, and `evidence/cli.py` has nothing. The gate is currently enforced on a shrinking
subset of the tree. Extend it, or the rule in DESIGN.md §2.2 no longer means what it says.

---

## 4. Documentation drifted behind the deletions

- `docs/DESIGN.md` lines 816–829 still present the **CDP cassette as a live test mode** in a
  three-row table ("Deterministic: yes … Catches: harness logic"), and `TODO.md` item 27
  marks it `[x]` done. Deleted in `84e4bb3`.
- `README.md` (41 lines) does not mention the three-package layout, the `bh` CLI, or
  `applications.install(globals())` — the one thing a script author now needs to know, and
  it is documented nowhere outside the package docstring. `SKILL.md` does not mention the
  application layer at all.
- `TODO.md` is 900+ lines with dated sections out of order (08-08, 08-09, 08-26, 08-26,
  08-27, 08-27, 08-27, 08-23, then the backlog). 15 open items. One filed known issue
  deserves more prominence than a section header: **"the daemon never idles out"** — 38
  orphaned daemons found, oldest three hours old; any `bh` run whose caller walks away
  leaves one behind.

---

## 5. Three commits, 6,467 insertions, one line of message each

| commit | files | lines | subject |
| --- | ---: | ---: | --- |
| `6b5b209` | 32 | +3,985 | Implement benchmark telemetry follow-ups |
| `7cc604a` | 16 | +1,325 | Harden browser lifecycle and daemon isolation |
| `1efd2fb` | 17 | +1,157 | Optimize browser sessions and bounded page reads |

Same defect flagged on `a53f40e`. `7cc604a` is the commit that added the Windows-red
endpoint tests; `1efd2fb` rewrote 544 lines of `SKILL.md` with no stated reason. The other
39 commits in the range are the opposite — measured claims in the body, which is what makes
this log a design record. Three holes in it are three places the record has nothing to say.

---

## 6. Follow-through on the committed review (`docs/reviews/…2026-08-22.html`)

That review recommended eight repairs. Status against the code now:

| repair | status |
| --- | --- |
| Untrack `jobs.json` / `required.txt` | done, `8ac2873` |
| Opener-aware popup waiting | done, `adca98b` + `_owned_tab_descendants_after_quiet` |
| Bind macOS approval to browser identity | done, `c88b4ad` |
| Bounded quiet windows at async boundaries | done |
| Malformed IPC frames fail with a typed cause | done, `ProtocolMismatch` in `client.py` |
| Daemon reader's blocking peer write | done, bounded `_outbound` queue with eviction |
| Collector fills after deduplication | done — on my branch, `34aa0fa`, five days later |
| Hit-test ownership for click fallback | `8a5fcf1` appears to be it; not verified |

Seven of eight acted on. That is a good record, and it is the reason the code side of this
review is short.

---

## 7. Hygiene

- Working tree clean; my branch pushed. Its CI run is red for the same pre-existing
  `perf_check.py` reason — nothing in the two commits.
- `outputs/` is **2.3 GB**, 1.6 GB of it raw screencast frames (`recordings_100`,
  `recordings_companies`) whose only purpose is to re-render mp4s that are already rendered.
  39 driver scripts, still untracked, still the only record of how the cited numbers were
  produced — raised in the data-value discussion, unchanged.
- `C:\tmp` holds 78 `bhs*` directories: `tests/conftest.py`'s `served` fixture uses a
  hardcoded `/tmp/bhs{pid}` and never removes it, so every test run on Windows leaves one.
  (`/tmp` is the deliberate AF_UNIX fix; the leak is separate — an `rmtree` in teardown.)
- `tmp/` (584 KB orphan PNGs), `review.md`, `review2.md` still present. `review2.md`'s
  OOPIF section is historical — the gate has been rewritten twice since.

---

## Verification performed

- Full unit suite on this machine: 10 failures of 568, all listed in §1b, all Windows
  environment; `tests/evidence`: 60 pass. `ruff check .` clean with 0.16.1; `uv run ruff
  check harness tests` (0.16.4) reproduces CI's `I001`.
- `test_client.py` failures reproduced at `acaa69a` in a scratch worktree (2 failed).
- CI history: last 40 `ci.yml` runs pulled via `gh`; every red `master` run's failed log
  classified by leg and cause; the first-red macOS job's step list and the `AF_UNIX`
  message read from the log; two macOS pytest jobs' `FAILED` lines read directly.
- Dead imports found by checking every `from harness.<x>` in `tests/`, `tools/`,
  `dashboard/` against the module's existence on disk.
- Coverage gate re-run locally with `applications/` and `evidence/` added.
- Scratch worktree removed; working tree left clean.

## What I would do first

1. Two-line import fix → ruff green on all legs.
2. Make the 10 Windows tests honest and give the 2 flaky macOS tests a deadline → pytest
   green on all legs. Green CI is the precondition for everything else being trustworthy.
3. `uv sync`.
4. Extend the coverage gate to `applications/` and `evidence/`.
5. Fix the cassette rows in `DESIGN.md`/`TODO.md`; one README paragraph on the split and
   `install(globals())`.
