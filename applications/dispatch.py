"""Which postings are worth a tab — decided from fetch metadata, before any navigation.

`get_job_details` classifies a posting long before the browser sees it, and the
2026-08-21 100-posting telemetry run (`post-review-100-rerun`, 10 workers, 106 s wall,
1,026 attempt-seconds) says that classification already knows which attempts are dead:

* `declared_mode == "account"`: 23 postings, **21 `no_application_form`**, 1 `generic_form`,
  1 failed, **0 filled** — 228 of 1,026 attempt-seconds.
* Workday (`*.myworkdayjobs.com`): **17/17 `account_wall`**, 0 filled, 87 attempt-seconds.
* iCIMS (`*.icims.com`): **4/4 no form**, 0 filled.

Those postings cost *more* than the ones that fill — a dead end runs into the 25 s locate
timeout (`no_application_form` mean 10.8 s, p95 30.1 s, versus 9.9 s for `form_processed`)
— so skipping them removes ~22% of attempt-seconds and, on that run, loses no filled form.
Nothing here fetches or navigates: it is a string comparison on data already in
`jobs*.json`, which is why the rule table stays this short. Every rule below is one of the
three cross-tabs above; a rule without a measured row does not belong in it.

`BH_APPLICATION_DISPATCH_FILTER=0` turns the whole table off and restores the old
navigate-everything behaviour, so the next telemetry run can A/B forms filled.

That A/B has now been run (2026-08-29, 100 fresh postings, `docs/benchmarks/
corpus-noise-2026-08-29.md`). Of the 20 postings this table skipped as
`declared_mode_account`, **none filled a form** when the filter was off: 19
`no_application_form`, 1 `workflow_failed`, at a cost of 321 attempt-seconds. The skip
loses nothing measurable and the rule table stands. Note what makes that trustworthy — it
is a paired per-posting comparison with a unanimous answer. The two runs' *totals*
disagreed in the opposite direction, because this corpus flips 14 of 80 postings between
identical configurations.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

#: Start-URL hosts that ended every attempt at an account wall. Anchored on the registrable
#: domain so a tenant subdomain (`thomsonreuters.wd5.myworkdayjobs.com`) matches and a
#: lookalike (`notmyworkdayjobs.com`) does not.
_SKIP_HOSTS = (
    (re.compile(r"^(?:.+\.)?myworkdayjobs\.com$", re.IGNORECASE), "workday_host"),
    (re.compile(r"^(?:.+\.)?icims\.com$", re.IGNORECASE), "icims_host"),
)

#: The same two vendors named by `apply.ats` rather than by host: the 17/17 and 4/4 rows
#: are ATS cross-tabs, and an employer-branded career host in front of the same tenant is
#: the same wall. Only these two — JazzHR, Lever and Ashby filled 3/3 each.
_SKIP_ATS = {"workday": "workday_ats", "icims": "icims_ats"}

#: `apply.mode` values that never reached a form. `form` and `unknown` are not skippable:
#: they filled 14/16 and 36/61.
_SKIP_MODES = {"account": "declared_mode_account"}


def _enabled(value: str | None, *, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in ("", "0", "false", "no", "off")


def dispatch_filter_enabled() -> bool:
    """Whether the metadata filter is on — `BH_APPLICATION_DISPATCH_FILTER=0` turns it off."""
    return _enabled(os.environ.get("BH_APPLICATION_DISPATCH_FILTER"))


def _host(url: Any) -> str:
    if not url:
        return ""
    try:
        return (urlsplit(str(url)).hostname or "").lower()
    except ValueError:                      # a malformed URL is not evidence of anything
        return ""


def should_attempt(job: Mapping[str, Any], *,
                   enabled: bool | None = None) -> tuple[bool, str]:
    """`(attempt, reason)` for one `jobs*.json` posting. Pure: no I/O, no navigation.

    `reason` is a slug in both directions so a skipped record and an attempted one can be
    counted the same way. `enabled` overrides the env toggle, which is what the tests and
    an A/B caller use instead of mutating the environment.
    """
    if not (dispatch_filter_enabled() if enabled is None else bool(enabled)):
        return True, "filter_disabled"
    apply = job.get("apply") or {}
    reason = _SKIP_MODES.get(str(apply.get("mode") or "").strip().lower())
    if reason:
        return False, reason
    reason = _SKIP_ATS.get(str(apply.get("ats") or "").strip().lower())
    if reason:
        return False, reason
    host = _host(apply.get("direct_url") or job.get("url"))
    for pattern, host_reason in _SKIP_HOSTS:
        if pattern.fullmatch(host):
            return False, host_reason
    return True, "eligible"
