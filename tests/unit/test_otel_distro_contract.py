"""Guard the OTel span-export contract between the CDK runtime env and agents/requirements.txt.

Why this test exists
--------------------
The AgentCore runtimes are launched by the Dockerfiles as
``opentelemetry-instrument python -m <agent>.main``, and the CDK sets
``OTEL_PYTHON_DISTRO=aws_distro`` / ``OTEL_PYTHON_CONFIGURATOR=aws_configurator``
in the runtime environment. Those two entry-point names are provided ONLY by the
``aws-opentelemetry-distro`` package.

When that package is missing from ``agents/requirements.txt``, nothing raises:
``opentelemetry-instrument`` cannot resolve the named distro/configurator, leaves
the global tracer provider as a no-op ``ProxyTracerProvider`` (spans report
``is_recording() == False`` and a zero trace id), and no span ever reaches the
``aws/spans`` log group. Every AgentCore batch evaluation then fails wholesale
with ``evaluatorSummaries: []`` while the agents themselves keep answering
correctly — a silent, expensive-to-diagnose failure.

That is exactly what happened: commit c54186f (2026-07-27) dropped the ADOT pin
believing it only captured HTTP/AWS-SDK spans, and span telemetry was dead for
ALL agents until 2026-08-03. These tests fail loudly if the three sides of the
contract (env var, launcher, dependency) ever drift apart again.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REQUIREMENTS = REPO_ROOT / "agents" / "requirements.txt"
AGENTCORE_STACK = REPO_ROOT / "cdk" / "lib" / "stacks" / "backend" / "agentcore-stack.ts"

# The distro package that supplies the `aws_distro` / `aws_configurator` entry points.
ADOT_PACKAGE = "aws-opentelemetry-distro"


def _requirements_text() -> str:
    """Return the agents requirements file contents.

    Returns:
        Full text of agents/requirements.txt.
    """
    return REQUIREMENTS.read_text(encoding="utf-8")


def _pinned_version(*, package: str, text: str) -> str | None:
    """Return the exact pinned version of a package in a requirements file.

    Args:
        package: Distribution name as it appears in the requirements file.
        text: Full requirements-file text to search.

    Returns:
        The pinned version string, or None when the package is absent or not
        pinned with ``==``. Commented-out lines are ignored.
    """
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip any trailing inline comment before matching the pin.
        spec = line.split("#", 1)[0].strip()
        match = re.fullmatch(rf"{re.escape(package)}==([^\s;]+)", spec)
        if match:
            return match.group(1)
    return None


def test_adot_distro_is_pinned() -> None:
    """aws-opentelemetry-distro must be an active (uncommented) exact pin.

    Without it, `opentelemetry-instrument` silently degrades to a no-op
    ProxyTracerProvider and no spans reach `aws/spans`.
    """
    version = _pinned_version(package=ADOT_PACKAGE, text=_requirements_text())
    assert version is not None, (
        f"{ADOT_PACKAGE} is missing from {REQUIREMENTS.relative_to(REPO_ROOT)} (or is "
        "commented out / not pinned with '=='). It provides the ONLY implementation of "
        "the aws_distro/aws_configurator entry points that the CDK runtime env names, "
        "so removing it silently kills ALL span export and makes every AgentCore batch "
        "evaluation fail with empty evaluatorSummaries. See the module docstring."
    )


def test_adot_otel_sdk_versions_agree() -> None:
    """The ADOT pin and the standalone opentelemetry-sdk pin must not conflict.

    ADOT pins opentelemetry-sdk exactly, so a mismatched sdk pin makes the image
    build unresolvable (pip ResolutionImpossible) rather than failing at runtime.
    ADOT 0.19.0 pins opentelemetry-sdk==1.44.0.
    """
    text = _requirements_text()
    adot = _pinned_version(package=ADOT_PACKAGE, text=text)
    sdk = _pinned_version(package="opentelemetry-sdk", text=text)
    if adot is None or sdk is None:
        pytest.skip("Both pins must be present; covered by test_adot_distro_is_pinned.")
    compatible_sdk_by_adot = {"0.19.0": "1.44.0"}
    expected_sdk = compatible_sdk_by_adot.get(adot)
    if expected_sdk is None:
        pytest.skip(
            f"No recorded opentelemetry-sdk pin for {ADOT_PACKAGE}=={adot}; verify the "
            "distro's own sdk pin and extend compatible_sdk_by_adot."
        )
    assert sdk == expected_sdk, (
        f"{ADOT_PACKAGE}=={adot} pins opentelemetry-sdk=={expected_sdk} but "
        f"requirements.txt pins opentelemetry-sdk=={sdk}. Mismatched pins make the "
        "agent container build fail to resolve."
    )


def test_cdk_runtime_env_names_the_adot_entry_points() -> None:
    """The CDK must still request the entry points ADOT provides.

    This is the other half of the contract: if these env vars are ever dropped
    or renamed, the requirements pin above is no longer what makes spans work,
    and this test's failure is the prompt to re-derive the contract rather than
    trusting a now-stale comment in requirements.txt.
    """
    stack = AGENTCORE_STACK.read_text(encoding="utf-8")
    assert "OTEL_PYTHON_DISTRO: 'aws_distro'" in stack, (
        "agentcore-stack.ts no longer sets OTEL_PYTHON_DISTRO='aws_distro'. If that is "
        f"intentional, re-verify whether {ADOT_PACKAGE} is still required and update "
        "both this test and the requirements.txt comment."
    )
    assert "OTEL_PYTHON_CONFIGURATOR: 'aws_configurator'" in stack, (
        "agentcore-stack.ts no longer sets OTEL_PYTHON_CONFIGURATOR='aws_configurator'. "
        f"Re-verify whether {ADOT_PACKAGE} is still required if this is intentional."
    )


def test_agent_dockerfiles_launch_via_opentelemetry_instrument() -> None:
    """Every agent image must launch through `opentelemetry-instrument`.

    That launcher is what reads OTEL_PYTHON_DISTRO/CONFIGURATOR and installs the
    real TracerProvider. A Dockerfile that runs `python -m ...` directly would
    emit no spans no matter what is pinned.
    """
    dockerfiles = sorted((REPO_ROOT / "agents").glob("Dockerfile.*"))
    assert dockerfiles, "No agent Dockerfiles found — check the expected repo layout."
    missing = [
        path.name
        for path in dockerfiles
        if "opentelemetry-instrument" not in path.read_text(encoding="utf-8")
    ]
    assert not missing, (
        "These agent Dockerfiles do not launch via opentelemetry-instrument, so they "
        f"cannot export spans: {missing}"
    )
