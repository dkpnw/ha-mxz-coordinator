"""Print ordinary pytest phases; incomplete or skipped evidence cannot pass CI.

``-p tools.pytest_phases`` prints the collected node ids once, one ``PHASE`` line per
reported phase, then ``PHASES_COMPLETE`` and ``PHASES_VALID=true|false``. A run that
skipped, deselected, xfailed or lost a phase of any collected test fails even when
pytest itself would pass it.

The record is kept where reports arrive: in the only process, or, under pytest-xdist,
in the controller. There the workers' collections and reports come in through xdist's
hooks, and a worker that deselected a test says so in its ``workeroutput``.
"""

import json
import sys

import pytest

DESELECTED = "pytest_phases_deselected"


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


class Phases:
    """The record of one run, in the process that receives every report."""

    def __init__(self, distributed, terminal):
        self.distributed = distributed
        self.terminal = terminal
        self.verdict = None
        self.collected = None
        self.reports = {}
        self.invalid = []
        self.classes = {}

    def _invalid(self, reason):
        if reason not in self.invalid:
            self.invalid.append(reason)

    def _collected(self, ids):
        if self.collected is None:
            self.collected = list(ids)
            emit("COLLECTED " + json.dumps(self.collected))
        elif list(ids) != self.collected:
            self._invalid("workers collected different tests")

    def pytest_collection_finish(self, session):
        if not self.distributed:
            self._collected(item.nodeid for item in session.items)

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        self._collected(ids)

    def pytest_deselected(self, items):
        if items:
            self._invalid("deselected tests")

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error):
        if getattr(node, "workeroutput", {}).get(DESELECTED):
            self._invalid("deselected tests")

    def pytest_runtest_logreport(self, report):
        self.reports.setdefault(report.nodeid, []).append(
            (report.when, report.outcome, hasattr(report, "wasxfail")))
        category = "green"
        if report.failed or report.skipped or hasattr(report, "wasxfail"):
            category = "unknown"
            self.classes.setdefault(report.nodeid, []).append(category)
        emit("PHASE " + json.dumps({"node": report.nodeid, "phase": report.when,
                                   "outcome": report.outcome, "category": category,
                                   "seconds": report.duration}))

    def pytest_sessionfinish(self, session):
        errors = phase_errors(self.collected or [], self.reports) + self.invalid
        # Collection errors and abnormal exits also remain failures.
        if errors and session.exitstatus == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        self.verdict = [
            "PHASES_COMPLETE " + json.dumps({"exit": int(session.exitstatus), "errors": errors,
                                             "classes": self.classes}, sort_keys=True),
            "PHASES_VALID=" + ("false" if errors else "true"),
        ]
        if self.terminal is None:
            self._verdict()

    def pytest_terminal_summary(self):
        self._verdict()

    def _verdict(self):
        if self.verdict:
            # pytest's progress line ("[100%]") is still open: the verdict starts its own.
            emit("")
            for line in self.verdict:
                emit(line)
        self.verdict = None


class WorkerDeselection:
    """On an xdist worker: tell the controller when this worker deselected a test."""

    def __init__(self, workeroutput):
        self.workeroutput = workeroutput

    def pytest_deselected(self, items):
        if items:
            self.workeroutput[DESELECTED] = True


def pytest_configure(config):
    if hasattr(config.option, "asyncio_mode"):
        config.option.asyncio_mode = "auto"
    if hasattr(config, "workerinput"):
        config.pluginmanager.register(WorkerDeselection(config.workeroutput), "pytest-phases")
    else:
        distributed = getattr(config.option, "dist", "no") != "no"
        terminal = config.pluginmanager.get_plugin("terminalreporter")
        config.pluginmanager.register(Phases(distributed, terminal), "pytest-phases")
