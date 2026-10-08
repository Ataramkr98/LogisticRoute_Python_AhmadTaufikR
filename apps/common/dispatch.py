"""Broker-independent task dispatch.

Celery's ``Task.delay()`` assumes a reachable broker. When none is listening it
does not raise promptly: the Redis result backend retries its reconnect twenty
times over roughly twenty seconds and only then raises
``RuntimeError: Retry limit exceeded``. Because every mutating code path in this
project dispatches a task after commit — geocoding a new address, importing a
CSV, running the optimizer, delivering a webhook, exporting a report — that
turned a missing optional dependency into a user-visible hang and a 500 on
otherwise successful requests.

Redis is documented as an optional accelerator, so dispatch must not be a hard
dependency. ``dispatch`` queues the task when a broker is available and
otherwise runs it inline in the request. The returned object exposes ``id`` and
``status`` in both cases, so callers persist the task id exactly as before and
nothing downstream needs to know which path ran.

Inline execution is slower than a worker but it is bounded by the task's own
time limit, and correctness beats availability here: an order that was
accepted must still be geocoded.
"""

import logging

from celery.exceptions import CeleryError

logger = logging.getLogger(__name__)

# Errors that mean "the broker is not usable right now". ConnectionError and
# OSError cover socket-level failures from kombu's transport; CeleryError covers
# the retry-limit RuntimeError surfaced by the Redis result backend.
BROKER_UNAVAILABLE = (CeleryError, ConnectionError, OSError)


def dispatch(task, *args, **kwargs):
    """Send ``task`` to the broker, falling back to inline execution.

    Returns an ``AsyncResult`` (queued) or an ``EagerResult`` (inline); both
    provide the ``id`` and ``status`` attributes callers rely on.
    """
    try:
        return task.apply_async(args=args, kwargs=kwargs)
    except BROKER_UNAVAILABLE as exc:
        logger.warning(
            "task_broker_unavailable task=%s error=%s running_inline=true",
            getattr(task, "name", task),
            exc.__class__.__name__,
        )

    # ``throw=False`` keeps a failing task from masking the request that
    # triggered it. The task records its own failure state, and the original
    # exception is already visible in the structured log.
    return task.apply(args=args, kwargs=kwargs, throw=False)
