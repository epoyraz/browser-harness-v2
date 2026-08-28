"""The metadata dispatch filter: which postings never get a tab.

Every case here is a row of the 2026-08-21 100-posting run. `account` mode filled 0 of 23,
Workday 0 of 17, iCIMS 0 of 4; Lever filled 3 of 3, so a Lever posting reaching the browser
is the property this table exists to preserve. The filter is a string comparison, so these
are the only checks it needs — and the toggle case is what makes the A/B run meaningful.
"""

from applications.dispatch import should_attempt


def test_account_mode_is_skipped_without_a_navigation():
    job = {"apply": {"mode": "account", "ats": "SuccessFactors",
                     "direct_url": "https://jobs.example.ch/job/1"}}
    assert should_attempt(job) == (False, "declared_mode_account")


def test_workday_tenant_host_is_skipped():
    job = {"apply": {"mode": "unknown", "ats": None,
                     "direct_url": "https://thomsonreuters.wd5.myworkdayjobs.com/en-US/x/job/1"}}
    assert should_attempt(job) == (False, "workday_host")


def test_icims_host_is_skipped():
    job = {"apply": {"mode": "unknown", "ats": None,
                     "direct_url": "https://careers-acme.icims.com/jobs/4711/login"}}
    assert should_attempt(job) == (False, "icims_host")


def test_lookalike_host_is_not_a_workday_tenant():
    job = {"apply": {"mode": "form", "ats": None,
                     "direct_url": "https://notmyworkdayjobs.com/apply"}}
    assert should_attempt(job) == (True, "eligible")


def test_vendor_named_by_ats_is_skipped_behind_an_employer_branded_host():
    job = {"apply": {"mode": "unknown", "ats": "Workday",
                     "direct_url": "https://careers.acme.com/job/1"}}
    assert should_attempt(job) == (False, "workday_ats")


def test_form_mode_lever_posting_is_attempted():
    job = {"apply": {"mode": "form", "ats": "Lever",
                     "direct_url": "https://jobs.lever.co/acme/2f8d1c40"}}
    assert should_attempt(job) == (True, "eligible")


def test_missing_apply_block_falls_back_to_the_job_url_and_attempts():
    assert should_attempt({"url": "https://boards.example.com/job/1"}) == (True, "eligible")
    assert should_attempt({}) == (True, "eligible")
    # …but a bare `url` on a skippable host is still skippable.
    assert should_attempt({"url": "https://acme.wd3.myworkdayjobs.com/x"}) == (
        False, "workday_host")


def test_malformed_url_is_not_evidence_of_a_dead_end():
    assert should_attempt({"url": "http://[oops"}) == (True, "eligible")


def test_toggle_off_returns_the_old_navigate_everything_behaviour(monkeypatch):
    job = {"apply": {"mode": "account", "ats": "Workday",
                     "direct_url": "https://acme.wd3.myworkdayjobs.com/x"}}
    monkeypatch.setenv("BH_APPLICATION_DISPATCH_FILTER", "0")
    assert should_attempt(job) == (True, "filter_disabled")
    monkeypatch.setenv("BH_APPLICATION_DISPATCH_FILTER", "1")
    assert should_attempt(job) == (False, "declared_mode_account")
    monkeypatch.delenv("BH_APPLICATION_DISPATCH_FILTER")
    assert should_attempt(job) == (False, "declared_mode_account")
    assert should_attempt(job, enabled=False) == (True, "filter_disabled")
