"""Separate durable worker: python -m app.worker [--once].

Configure the same database, storage root, model allowlist and credential key as
the API process. No in-process task queues or long-lived raw keys are used.
"""

import argparse
import signal
import threading


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backend B durable job worker")
    parser.add_argument("--once", action="store_true", help="Process at most one queued job and exit")
    args = parser.parse_args(argv)
    from .main import build_context
    from .maintenance import drain_deletions

    ctx = build_context()
    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    while not stopped.is_set():
        drain_deletions(ctx)
        did_work = ctx.jobs.run_once()
        drain_deletions(ctx)
        if args.once:
            break
        if not did_work:
            stopped.wait(1)


if __name__ == "__main__":
    main()
