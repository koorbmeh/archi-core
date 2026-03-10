"""
capabilities/event_loop.py — DEPRECATED

THIS MODULE IS DEPRECATED AND NOT USED BY ARCHI.

The real runtime is ArchiDaemon in run.py, which manages:
  - Message task (Discord listener polling)
  - Generation task (self-development cycles)
  - Periodic tasks (loaded from data/periodic_registry.json)
  - On-demand commands (loaded from data/command_registry.json)

DO NOT wire capabilities into this module. Instead:
  - For periodic tasks: register in data/periodic_registry.json via
    src.kernel.periodic_registry.register()
  - For on-demand tasks: register in data/command_registry.json via
    src.kernel.command_registry.register()

This file is kept as a stub so existing imports don't crash. All classes
and functions are no-ops that log deprecation warnings.

PROTECTED FILE — Archi's generation loop must NOT rewrite this file.
"""

import logging

logger = logging.getLogger(__name__)

_DEPRECATION_MSG = (
    "event_loop.py is deprecated. Use periodic_registry or command_registry "
    "instead. See data/periodic_registry.json and data/command_registry.json."
)


class PeriodicTask:
    """DEPRECATED — stub for backward compatibility."""
    def __init__(self, name="", coro_factory=None, interval=0):
        self.name = name
        self.coro_factory = coro_factory
        self.interval = interval


class EventLoop:
    """DEPRECATED — stub for backward compatibility."""
    def __init__(self, **kwargs):
        logger.warning(_DEPRECATION_MSG)

    def register_task(self, task):
        logger.warning("EventLoop.register_task() is deprecated: %s", _DEPRECATION_MSG)

    add_periodic_task = register_task

    async def run(self):
        logger.warning("EventLoop.run() is deprecated: %s", _DEPRECATION_MSG)

    def stop(self):
        pass


def create_event_loop(**kwargs):
    logger.warning("create_event_loop() is deprecated: %s", _DEPRECATION_MSG)
    return EventLoop()


def start():
    logger.warning("event_loop.start() is deprecated: %s", _DEPRECATION_MSG)
