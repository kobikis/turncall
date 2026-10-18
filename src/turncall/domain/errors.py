"""Exceptions that survive pickling (#352).

Both log sinks are `enqueue=True` (config/logging.py), so loguru pickles every
record, a logged exception included, and rebuilds it on the writer thread. An
exception unpickles as `cls(*self.args)`, and `args` is only its message: one
whose `__init__` takes anything else fails to rebuild, and loguru drops the
whole line ("Logging error ... Record was: None"), or rebuilds it with a
garbled message.
"""

from typing import Any


class PicklableError(Exception):
    """An exception rebuilt from its `args` and attributes, never by calling
    its `__init__`, whatever that takes. Mix it in first on any exception
    with an `__init__` of its own."""

    def __reduce__(self) -> tuple[Any, ...]:
        return (_rebuild, (type(self), self.args), self.__dict__ or None)


def _rebuild(cls: type[BaseException], args: tuple[Any, ...]) -> BaseException:
    # BaseException.__new__ sets `args`; pickle then restores `__dict__`.
    return cls.__new__(cls, *args)
