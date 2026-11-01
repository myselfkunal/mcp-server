# Copyright The Kubeflow Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Agent-facing error contract.

Every failure a client can observe must carry ``success: False`` and a stable
``error_code``, so an agent can decide whether to retry, correct its input, or
ask for permission without parsing the human-readable message.

The paths covered here are the ones that previously returned a plain dict:
rate limited and circuit open in the audit wrapper, and unknown tool, invalid
arguments, circuit open and tool exception in ``execute_tool``.
"""

import time

import pytest

from kubeflow_mcp.common.constants import (
    CALLER_ERROR_CODES,
    RETRIABLE_ERROR_CODES,
    ErrorCode,
    is_retriable_error,
)
from kubeflow_mcp.core import dynamic_tools, server
from kubeflow_mcp.core.resilience import CircuitState, RateLimiter, get_breaker


def probe_tool(name: str) -> dict:
    """Read-only tool used to exercise the wrappers."""
    return {"success": True, "data": {"name": name}}


def failing_tool(name: str) -> dict:
    """Tool that raises, standing in for an SDK or API failure."""
    raise RuntimeError("cluster unreachable")


@pytest.fixture(autouse=True)
def _registry():
    dynamic_tools.init_dynamic_tools([probe_tool, failing_tool], {})
    yield
    dynamic_tools.TOOL_REGISTRY.clear()
    dynamic_tools.TOOL_HIERARCHY.clear()
    dynamic_tools._embedding_cache.reset()


def _trip(breaker):
    """Force the breaker open and keep it there for the duration of the test."""
    breaker.state = CircuitState.OPEN
    breaker.failure_count = breaker.failure_threshold
    breaker.last_failure_time = time.monotonic()
    breaker.recovery_timeout = 3600.0


def assert_error_contract(result, expected_code):
    assert isinstance(result, dict)
    assert result["success"] is False
    assert result["error_code"] == expected_code
    assert isinstance(result["error"], str) and result["error"]
    assert "traceback" not in result["error"]
    assert expected_code in RETRIABLE_ERROR_CODES | CALLER_ERROR_CODES


def test_rate_limited_returns_the_contract(monkeypatch):
    limiter = RateLimiter(rate=1.0, capacity=1.0)
    limiter.acquire()
    monkeypatch.setattr(server, "_rate_limiter", limiter)

    result = server._audit_wrap(probe_tool)(name="x")

    assert_error_contract(result, ErrorCode.RATE_LIMITED)
    assert result["tool"] == "probe_tool"
    assert is_retriable_error(result)


def test_circuit_open_returns_the_contract(monkeypatch):
    monkeypatch.setattr(server, "_rate_limiter", None)
    _trip(get_breaker("probe_tool"))

    result = server._audit_wrap(probe_tool)(name="x")

    assert_error_contract(result, ErrorCode.CIRCUIT_OPEN)
    assert result["tool"] == "probe_tool"
    assert is_retriable_error(result)


def test_unknown_tool_returns_the_contract():
    result = dynamic_tools.execute_tool("delete_runtime", {})

    assert_error_contract(result, ErrorCode.TOOL_NOT_FOUND)
    assert "probe_tool" in result["details"]["available"]
    assert not is_retriable_error(result)


def test_invalid_arguments_return_the_contract():
    result = dynamic_tools.execute_tool("probe_tool", {"nmae": "typo"})

    assert_error_contract(result, ErrorCode.VALIDATION_ERROR)
    assert result["tool"] == "probe_tool"
    assert not is_retriable_error(result)


def test_execute_tool_circuit_open_returns_the_contract():
    _trip(get_breaker("probe_tool"))

    result = dynamic_tools.execute_tool("probe_tool", {"name": "x"})

    assert_error_contract(result, ErrorCode.CIRCUIT_OPEN)
    assert result["tool"] == "probe_tool"


def test_tool_exception_returns_the_contract():
    result = dynamic_tools.execute_tool("failing_tool", {"name": "x"})

    assert_error_contract(result, ErrorCode.SDK_ERROR)
    assert result["tool"] == "failing_tool"
    assert result["details"]["exception"] == "RuntimeError"
    assert "traceback" not in result["details"]
    assert is_retriable_error(result)


def test_successful_call_is_untouched(monkeypatch):
    monkeypatch.setattr(server, "_rate_limiter", None)

    result = dynamic_tools.execute_tool("probe_tool", {"name": "x"})

    assert result["success"] is True
    assert "error_code" not in result
