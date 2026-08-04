"""Tests for the shared helpers added to the SDK: execute_procedure and async_from_sync."""
import asyncio

import pytest

from vantiqsdk import VantiqException, VantiqResponse, execute_procedure, async_from_sync


class _FakeError:
    def __init__(self, message):
        self.message = message


class _FakeClient:
    """Minimal async-context-manager stand-in for a Vantiq client."""

    def __init__(self, response):
        self._response = response
        self.entered = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, procedure_id, params, headers=None):
        self.last_call = (procedure_id, params)
        return self._response


def _response(success, errors=None):
    r = VantiqResponse(success, 200, 'application/json')
    r.errors = errors
    return r


def test_execute_procedure_returns_response_on_success():
    resp = _response(True)
    client = _FakeClient(resp)
    result = asyncio.run(execute_procedure(client, 'my.proc', {'a': 1}))
    assert result is resp
    assert client.entered is True
    assert client.last_call == ('my.proc', {'a': 1})


def test_execute_procedure_raises_on_failure_with_server_message():
    client = _FakeClient(_response(False, [_FakeError('boom happened')]))
    with pytest.raises(VantiqException) as ei:
        asyncio.run(execute_procedure(client, 'my.proc', {}))
    assert 'boom happened' in str(ei.value)
    assert ei.value.code == 'io.vantiq.sdk.procedure.execution.failed'


def test_execute_procedure_raises_when_no_error_detail():
    client = _FakeClient(_response(False, None))
    with pytest.raises(VantiqException):
        asyncio.run(execute_procedure(client, 'my.proc', {}))


def test_async_from_sync_runs_coroutine_with_args_and_kwargs():
    @async_from_sync
    async def combine(a, b, sep='-'):
        return f"{a}{sep}{b}"

    # Exercises kwargs too — the earlier implementation splatted kwargs positionally.
    assert combine('x', 'y', sep='+') == 'x+y'


def test_async_from_sync_works_from_within_a_running_loop():
    @async_from_sync
    async def double(x):
        return x * 2

    # Call the sync wrapper from inside a running event loop. The old implementation raised
    # "event loop is already running" here; the thread-offload path handles it.
    async def caller():
        return double(21)

    assert asyncio.run(caller()) == 42
