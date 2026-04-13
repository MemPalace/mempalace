"""Tests for the write-path serialisation lock."""
import asyncio

import pytest


@pytest.mark.asyncio
async def test_writer_lock_serialises_writes():
    from mempalace.concurrency import writer_lock

    events = []

    async def writer(name: str, delay: float):
        async with writer_lock:
            events.append(f"{name}-enter")
            await asyncio.sleep(delay)
            events.append(f"{name}-exit")

    await asyncio.gather(writer("a", 0.05), writer("b", 0.05), writer("c", 0.05))

    assert events[0].endswith("-enter")
    assert events[1].endswith("-exit")
    assert events[2].endswith("-enter")
    assert events[3].endswith("-exit")
    assert events[4].endswith("-enter")
    assert events[5].endswith("-exit")


@pytest.mark.asyncio
async def test_writer_lock_is_reentrant_safe_for_single_task():
    from mempalace.concurrency import writer_lock

    async with writer_lock:
        released = writer_lock.locked()
    assert released is True
    assert writer_lock.locked() is False
