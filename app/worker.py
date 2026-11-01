"""Separate durable worker: python -m app.worker [--once].

Configure the same database, storage root, model allowlist and credential key as
the API process. No in-process task queues or long-lived raw keys are used.
"""

import argparse
import logging
import signal
import threading


def run_loop(ctx, stopped, *, once=False, max_consecutive_failures=5):
    """Retry transient iteration faults without retrying a provider dispatch.

    Every database operation owns a transaction context, which rolls back and
    closes before an exception arrives here. Recovery uses the durable dispatch
    marker, so a failed worker iteration cannot repeat an uncertain paid call.
    """
    from .maintenance import drain_deletions

    if type(max_consecutive_failures) is not int or max_consecutive_failures < 1:
        raise ValueError("Worker failure limit must be a positive integer")
    failures = 0
    logger = logging.getLogger("backend.worker")
    while not stopped.is_set():
        failed, did_work = False, False
        # Outbox faults must not starve job processing, and vice versa.
        for operation, diagnostic in (
            (lambda: drain_deletions(ctx), "Worker deletion pass failed"),
            (ctx.jobs.run_once, "Worker job pass failed"),
            (lambda: drain_deletions(ctx), "Worker deletion pass failed"),
        ):
            try:
                result = operation()
                if diagnostic == "Worker job pass failed":
                    did_work = result
            except Exception:
                # Never include exception objects, traceback, SQL, keys or input.
                logger.error(diagnostic)
                failed = True
        failures = failures + 1 if failed else 0
        if failures >= max_consecutive_failures:
            logger.error("Worker stopped after repeated iteration failures")
            return 1
        if once:
            # A one-shot supervisor receives failure explicitly; it must not
            # unknowingly process a second job after a partial first iteration.
            return 1 if failed else 0
        if failed:
            stopped.wait(min(30, 2 ** (failures - 1)))
        elif not did_work:
            stopped.wait(1)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Integrated learning backend durable job worker")
    parser.add_argument("--once", action="store_true", help="Process at most one queued job and exit")
    args = parser.parse_args(argv)
    from .main import build_context

    ctx = build_context()
    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    return run_loop(ctx, stopped, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
