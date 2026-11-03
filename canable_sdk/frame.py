"""Shared CAN data frame contract."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CanFrame:
    can_id: int
    data: bytes
    is_fd: bool = True
    brs: bool = True
    is_extended: bool = False
    is_error: bool = False
    is_remote: bool = False
    # Host observation time and SDK receive sequence, not device sampling time.
    # Pure codec calls and application-created TX frames leave these unknown.
    timestamp_ns: int | None = field(default=None, compare=False)
    sequence: int | None = field(default=None, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "data", bytes(self.data))
