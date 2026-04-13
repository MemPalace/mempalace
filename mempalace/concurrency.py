"""Write-path serialisation.

Single asyncio.Lock guards all chromadb / sqlite mutations from the HTTP transport.
The stdio transport does not need this (single-threaded event loop), but acquires it
anyway for consistency.

Design note: this is a coarse lock, not a queue. Writes block concurrently arriving
writes for the duration of the index mutation + WAL write. For a personal-scale system
(<1 Hz sustained writes) this is adequate. Revisit if contention ever becomes visible.
"""
import asyncio

writer_lock: asyncio.Lock = asyncio.Lock()
