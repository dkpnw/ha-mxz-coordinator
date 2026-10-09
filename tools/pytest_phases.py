"""Print ordinary pytest phases; incomplete or skipped evidence cannot pass CI."""

import json
import sys

import pytest


def emit(line):
    """One write per evidence line, then flush: stderr cannot land inside or before it."""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def phase_errors(expected, reports):
    """Collection supplies identity; it cannot substitute for completed phases."""
    errors = []
    if not expected or len(set(expected)) != len(expected):
        errors.append("empty or duplicate collection")
    if set(reports) != set(expected):
        errors.append("missing or unexpected test")
    for node in expected:
        phases = reports.get(node, [])
        if [p[0] for p in phases] != ["setup", "call", "teardown"]:
            errors.append(f"{node}: missing, repeated or reordered phase")
        if any(outcome not in ("passed", "failed") or special for _, outcome, special in phases):
            errors.append(f"{node}: skip/xfail/xpass")
    return errors


def pytest_configure(config):
    config._ordinary_collected = []
    config._ordinary_reports = {}
    config._ordinary_invalid = []
    config._ordinary_classes = {}
    if hasattr(config.option, "asyncio_mode"):
        config.option.asyncio_mode = "auto"


def pytest_collection_finish(session):
    session.config._ordinary_collected = [i.nodeid for i in session.items]
    emit("COLLECTED " + json.dumps(session.config._ordinary_collected))


def pytest_deselected(items):
    if items:
        items[0].config._ordinary_invalid.append("deselected tests")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    result = yield
    report = result.get_result()
    config = item.config
    config._ordinary_reports.setdefault(item.nodeid, []).append(
        (report.when, report.outcome, hasattr(report, "wasxfail")))
    category = "green"
    if report.failed or report.skipped or hasattr(report, "wasxfail"):
        category = "unknown"
        if call.when == "call" and call.excinfo and call.excinfo.type.__name__ == "OwnershipFailure":
            category = "ownership-red"
    if category != "green":
        config._ordinary_classes.setdefault(item.nodeid, []).append(category)
    if category == "unknown" and item.path.parent.name == "issue25":
        item.session.shouldstop = "UNKNOWN diagnostic prerequisite/phase; retain missing cells"
    emit("PHASE " + json.dumps({"node": item.nodeid, "phase": report.when,
                               "outcome": report.outcome, "category": category,
                               "seconds": report.duration}))


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    errors = phase_errors(config._ordinary_collected, config._ordinary_reports)
    errors += config._ordinary_invalid
    # Collection errors and abnormal exits also remain failures, never issue-red.
    if errors and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    emit("PHASES_COMPLETE " + json.dumps({"exit": int(session.exitstatus),
                                        "errors": errors,
                                        "classes": config._ordinary_classes}, sort_keys=True))
    emit("PHASES_VALID=" + ("false" if errors else "true"))
