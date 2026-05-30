import logging
from collections import deque

_buffer: deque[str] = deque(maxlen=500)


class BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        _buffer.append(self.format(record))


def get_lines() -> list[str]:
    return list(_buffer)


def setup(level: int = logging.INFO):
    handler = BufferHandler(level)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s"))
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "app"):
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.addHandler(handler)
        lg.propagate = False
