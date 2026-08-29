"""Lightweight OpenTelemetry-style observability (in-process spans + counters).

Uses the OpenTelemetry SDK when available; always maintains an in-process
metric/span store that the /observability endpoints expose.
"""
import time
import uuid
from collections import deque
from contextlib import contextmanager

_spans: deque = deque(maxlen=1000)
_counters: dict[str, float] = {}
_agent_invocations: deque = deque(maxlen=300)

try:  # optional
    from opentelemetry import trace as _otel_trace
    _tracer = _otel_trace.get_tracer("cros")
except Exception:  # pragma: no cover
    _tracer = None


def incr(name: str, value: float = 1.0):
    _counters[name] = _counters.get(name, 0.0) + value


def counters() -> dict:
    return dict(_counters)


def new_trace_id() -> str:
    return uuid.uuid4().hex


@contextmanager
def span(name: str, **attrs):
    trace_id = attrs.pop("trace_id", None) or new_trace_id()
    start = time.perf_counter()
    record = {"name": name, "trace_id": trace_id, "attributes": attrs,
              "started_at": time.time(), "status": "ok"}
    otel_cm = _tracer.start_as_current_span(name) if _tracer else None
    if otel_cm:
        otel_cm.__enter__()
    try:
        yield record
    except Exception as exc:
        record["status"] = "error"
        record["error"] = str(exc)
        raise
    finally:
        record["duration_ms"] = round((time.perf_counter() - start) * 1000, 2)
        _spans.append(record)
        incr(f"span.{name}.count")
        incr(f"span.{name}.duration_ms", record["duration_ms"])
        if otel_cm:
            try:
                otel_cm.__exit__(None, None, None)
            except Exception:
                pass


def spans(limit: int = 100) -> list[dict]:
    return list(_spans)[-limit:][::-1]


def record_agent_invocation(entry: dict):
    _agent_invocations.append(entry)


def agent_invocations(limit: int = 100) -> list[dict]:
    return list(_agent_invocations)[-limit:][::-1]
