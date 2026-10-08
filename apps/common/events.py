import json
import time

from django.conf import settings
from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError


def publish(channel, payload):
    # The connect timeout matters as much as the exception handling: without it
    # an unreachable broker blocks the caller for the full OS TCP timeout, which
    # is several seconds per publish. update_progress publishes on every phase,
    # so an untimed client turned a whole optimization run into a series of
    # multi-second stalls.
    try:
        client = Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
        try:
            client.publish(channel, json.dumps(payload, default=str))
        finally:
            client.close()
    except RedisError:
        return False
    return True


def _unavailable_event(channel):
    """Single SSE frame telling the client this channel cannot be delivered.

    Redis is documented as optional, so a subscriber that cannot connect must
    not raise: doing so turns the progress endpoint into a 500 and, because the
    browser reconnects on error, into a reload loop. Emitting a named event
    instead lets the page switch to polling and stay usable.
    """
    payload = json.dumps({"channel": channel, "reason": "broker_unreachable"})
    return f"event: unavailable\ndata: {payload}\n\n"


def event_stream(channel, heartbeat_seconds=15):
    try:
        client = Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
        pubsub = client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(channel)
    except RedisError:
        yield _unavailable_event(channel)
        return
    last_heartbeat = time.monotonic()
    try:
        while True:
            message = pubsub.get_message(timeout=1)
            if message and message.get("type") == "message":
                data = message["data"].decode() if isinstance(message["data"], bytes) else message["data"]
                yield f"id: {int(time.time() * 1000)}\nevent: update\ndata: {data}\n\n"
                last_heartbeat = time.monotonic()
            elif time.monotonic() - last_heartbeat >= heartbeat_seconds:
                yield ": heartbeat\n\n"
                last_heartbeat = time.monotonic()
    except RedisError:
        # The broker dropped mid-stream. Report it the same way as a failed
        # connect so the client degrades to polling rather than reconnecting.
        yield _unavailable_event(channel)
    finally:
        pubsub.close()
        client.close()


async def async_event_stream(channel, heartbeat_seconds=15):
    try:
        client = AsyncRedis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
        pubsub = client.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(channel)
    except RedisError:
        yield _unavailable_event(channel)
        return
    last_heartbeat = time.monotonic()
    try:
        while True:
            message = await pubsub.get_message(timeout=1)
            if message and message.get("type") == "message":
                data = message["data"].decode() if isinstance(message["data"], bytes) else message["data"]
                yield f"id: {int(time.time() * 1000)}\nevent: update\ndata: {data}\n\n"
                last_heartbeat = time.monotonic()
            elif time.monotonic() - last_heartbeat >= heartbeat_seconds:
                yield ": heartbeat\n\n"
                last_heartbeat = time.monotonic()
    except RedisError:
        yield _unavailable_event(channel)
    finally:
        await pubsub.aclose()
        await client.aclose()
