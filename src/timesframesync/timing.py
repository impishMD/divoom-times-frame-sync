# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

"""Consistent operation timings without logging credentials or exception bodies."""
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
import logging
from time import monotonic


@dataclass
class OperationTimer:
    started: float = field(default_factory=monotonic)
    status: str = "complete"

    @property
    def elapsed(self) -> float:
        return monotonic() - self.started


@contextmanager
def timed_operation(logger, message, *args, level=logging.DEBUG, announce=False):
    """Keep successful stages quiet by default; always surface problems."""
    label = message % args if args else message
    timer = OperationTimer()
    if announce:
        logger.log(level, "%s: started; %.1fs elapsed", label, timer.elapsed)
    try:
        yield timer
    except BaseException as error:
        timer.status = "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed"
        raise
    finally:
        outcome_level = level
        if timer.status != "complete":
            outcome_level = logging.ERROR if timer.status == "failed" else logging.WARNING
        logger.log(outcome_level, "%s: %s; %.1fs", label, timer.status, timer.elapsed)


def timed(logger, message, *, level=logging.DEBUG, announce=False):
    """Time an entire operation, including failure paths, preserving its result."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with timed_operation(logger, message, level=level, announce=announce):
                return function(*args, **kwargs)
        return wrapped
    return decorate
