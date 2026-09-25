"""Anthropic capture wrapper."""

from __future__ import annotations

import time
from collections.abc import Iterable
from importlib import import_module
from typing import Any, cast

from .events import AgentClock, record_event


def _dump_response(response: Any) -> Any:
    if isinstance(response, (list, tuple)):
        return [_dump_response(item) for item in response]
    if hasattr(response, "model_dump"):
        return response.model_dump()
    if hasattr(response, "dict"):
        return response.dict()
    if isinstance(response, dict):
        return response
    return getattr(response, "__dict__", repr(response))


class _CapturedStream:
    def __init__(
        self,
        owner: CapturedClient,
        stream: Any,
        *,
        model: Any,
        messages: Any,
        start: float,
        causal_parent_ids: Iterable[str],
        idempotency_key: str | None,
    ) -> None:
        self._owner = owner
        self._stream = stream
        self._active_stream = stream
        self._iterator = iter(stream)
        self._model = model
        self._messages = messages
        self._start = start
        self._causal_parent_ids = list(causal_parent_ids)
        self._idempotency_key = idempotency_key
        self._chunks: list[Any] = []
        self._finalized = False

    def __iter__(self) -> _CapturedStream:
        return self

    def __next__(self) -> Any:
        try:
            chunk = next(self._iterator)
        except StopIteration:
            self._finish()
            raise
        except Exception as exc:
            self._fail(exc)
            raise
        self._chunks.append(chunk)
        return chunk

    def __enter__(self) -> _CapturedStream:
        enter = getattr(self._stream, "__enter__", None)
        if enter is not None:
            self._active_stream = enter()
            self._iterator = iter(self._active_stream)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        exit_method = getattr(self._stream, "__exit__", None)
        try:
            return exit_method(exc_type, exc, traceback) if exit_method is not None else None
        except BaseException as cleanup_error:
            self._fail(cleanup_error)
            raise
        finally:
            if not self._finalized:
                if exc is None:
                    self._finish()
                else:
                    self._fail(exc)

    def close(self) -> None:
        close = getattr(self._active_stream, "close", None)
        try:
            if close is not None:
                close()
        except BaseException as close_error:
            self._fail(close_error, status="close_error")
            raise
        finally:
            if not self._finalized:
                self._fail(RuntimeError("stream closed before completion"), status="closed")

    def _finish(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        _, _ = record_event(
            agent_id=self._owner.agent_id,
            clock=self._owner.clock,
            log=self._owner.log,
            event_type="model_call",
            payload={
                "model": self._model,
                "input": self._messages,
                "output": _dump_response(self._chunks),
                "latency_ms": int((time.monotonic() - self._start) * 1000),
                "stream": True,
            },
            causal_parent_ids=self._causal_parent_ids,
            idempotency_key=self._idempotency_key,
            run_id=self._owner.run_id,
        )

    def _fail(self, exc: BaseException, *, status: str = "failed") -> None:
        if self._finalized:
            return
        self._finalized = True
        _, _ = record_event(
            agent_id=self._owner.agent_id,
            clock=self._owner.clock,
            log=self._owner.log,
            event_type="agent_error",
            payload={
                "operation": "model_call",
                "error": str(exc),
                "input": self._messages,
                "stream": True,
                "stream_status": status,
            },
            causal_parent_ids=self._causal_parent_ids,
            idempotency_key=self._idempotency_key,
            run_id=self._owner.run_id,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._active_stream, name)


class _CapturedMessages:
    def __init__(self, owner: CapturedClient) -> None:
        self._owner = owner

    def create(
        self,
        *,
        causal_parent_ids: Iterable[str] = (),
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Call Anthropic unchanged, then append the observable model event."""
        start = time.monotonic()
        try:
            response = self._owner._client.messages.create(**kwargs)
        except Exception as exc:
            _, _ = record_event(
                agent_id=self._owner.agent_id,
                clock=self._owner.clock,
                log=self._owner.log,
                event_type="agent_error",
                payload={
                    "operation": "model_call",
                    "error": str(exc),
                    "input": kwargs.get("messages"),
                },
                causal_parent_ids=causal_parent_ids,
                idempotency_key=idempotency_key,
                run_id=self._owner.run_id,
            )
            raise

        if kwargs.get("stream") is True:
            return _CapturedStream(
                self._owner,
                response,
                model=kwargs.get("model"),
                messages=kwargs.get("messages"),
                start=start,
                causal_parent_ids=causal_parent_ids,
                idempotency_key=idempotency_key,
            )

        latency_ms = int((time.monotonic() - start) * 1000)
        _, _ = record_event(
            agent_id=self._owner.agent_id,
            clock=self._owner.clock,
            log=self._owner.log,
            event_type="model_call",
            payload={
                "model": kwargs.get("model"),
                "input": kwargs.get("messages"),
                "output": _dump_response(response),
                "latency_ms": latency_ms,
            },
            causal_parent_ids=causal_parent_ids,
            idempotency_key=idempotency_key,
            run_id=self._owner.run_id,
        )
        return response


class CapturedClient:
    """Drop-in-shaped wrapper exposing ``client.messages.create``."""

    def __init__(
        self,
        api_key: str | None,
        agent_id: str,
        clock: AgentClock,
        log: Any,
        *,
        client: Any | None = None,
        run_id: str | None = None,
    ) -> None:
        if client is None:
            try:
                anthropic = import_module("anthropic")
            except ImportError as exc:  # pragma: no cover - deployment setup
                raise RuntimeError("anthropic is required when client is not injected") from exc
            if api_key is None:
                raise ValueError("api_key is required when client is not injected")
            client = cast(Any, anthropic).Anthropic(api_key=api_key)
        self._client = client
        self.agent_id = agent_id
        self.clock = clock
        self.log = log
        self.run_id = run_id
        self.messages = _CapturedMessages(self)

    def messages_create(self, **kwargs: Any) -> Any:
        """Compatibility alias for the original Phase 1 sketch."""
        return self.messages.create(**kwargs)
