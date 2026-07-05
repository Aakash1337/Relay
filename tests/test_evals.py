"""The eval harness catches reasoning regressions — including injected ones.

Phase 2 exit gate: "an injected scoring regression is caught by the eval
harness." We inject three distinct regressions into an otherwise-healthy
backend and assert each one drags the report below threshold.
"""

from __future__ import annotations

import pytest

from relay.compute.base import ComputeRequest, ComputeResponse
from relay.compute.offline import OfflineBackend
from relay.evals import run_evals
from relay.routing.router import TaskType

pytestmark = pytest.mark.exit_gate


def test_healthy_backend_passes_the_golden_set():
    report = run_evals(OfflineBackend())
    assert report.passed, [f"{r.case_id}: {r.detail}" for r in report.failures]
    assert report.pass_rate == 1.0


class _ScoreRegression(OfflineBackend):
    """Injected regression: scoring becomes sycophantic (always max)."""

    def complete(self, request: ComputeRequest) -> ComputeResponse:
        response = super().complete(request)
        if request.task_type == TaskType.FIT_SCORING:
            output = dict(response.output)
            output["fit_score"] = 1.0  # "everyone is a perfect fit"
            return ComputeResponse(output=output, backend=self.name, model=self.model)
        return response


class _TriageRegression(OfflineBackend):
    """Injected regression: triage manufactures intent (opt-outs become
    'interested') — the worst possible compliance failure."""

    def complete(self, request: ComputeRequest) -> ComputeResponse:
        response = super().complete(request)
        if request.task_type == TaskType.REPLY_TRIAGE:
            return ComputeResponse(
                output={"category": "interested", "confidence": 0.99},
                backend=self.name,
                model=self.model,
            )
        return response


class _ContractRegression(OfflineBackend):
    """Injected regression: outputs stop honoring the JSON contract."""

    def complete(self, request: ComputeRequest) -> ComputeResponse:
        response = super().complete(request)
        if request.task_type == TaskType.FIT_SCORING:
            return ComputeResponse(
                output={"totally": "wrong shape"},
                backend=self.name,
                model=self.model,
            )
        return response


@pytest.mark.parametrize(
    "regression_backend",
    [_ScoreRegression, _TriageRegression, _ContractRegression],
)
def test_injected_regression_is_caught(regression_backend):
    report = run_evals(regression_backend())
    assert not report.passed, "regression slipped past the eval harness"
    assert report.failures  # and it says which cases broke


def test_report_localizes_the_failure_category():
    report = run_evals(_TriageRegression())
    by_cat = report.by_category
    # Triage categories degraded; copy stayed healthy → the report points
    # at the right subsystem instead of a vague overall number.
    passed, total = by_cat["triage_optout"]
    assert passed < total
    passed, total = by_cat["copy"]
    assert passed == total
