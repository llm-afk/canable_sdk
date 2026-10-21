"""Single-connection CANable API. All connection operations use one owner thread."""
import threading as _threading
from .transport import Canable as _Canable, UsbFrameError
from .frame import CanFrame as _Frame
from .transport import _ThreadPriority

__all__ = ['detect', 'open', 'send', 'recv', 'close', 'UsbFrameError']
_bus = None
_owner = None
_fd = False
_priority = None
_lock = _threading.RLock()

def detect():
    """Enumerate USB devices without starting CAN."""
    return _Canable.detect()

def open(serial_number=None, *, bitrate=1_000_000, fd=False,
         data_bitrate=4_000_000, sample_point=75.0, data_sample_point=64.0,
         low_latency=True, high_priority=True):
    """Open one connection and return adapter information.

    Defaults to classic CAN, 1 Mbps. low_latency defaults to True; it does not
    change send's USB-completion contract or provide hard-real-time scheduling.
    high_priority defaults to True and boosts the owning thread to Windows HIGHEST until close().
    """
    global _bus, _owner, _fd, _priority
    with _lock:
        if _bus is not None:
            raise RuntimeError('CANable already open; close it before opening again')
        bus = _Canable(serial_number, low_latency=low_latency)
        # Transport.open performs rollback on failure. Publish only after success.
        bus.open(bitrate=bitrate, data_bitrate=data_bitrate, sample_point=sample_point,
                 data_sample_point=data_sample_point, fd=fd)
        try:
            info = bus.info.copy()
            info["usb_io"] = dict(info["usb_io"])
            priority = _ThreadPriority() if high_priority else None
        except BaseException:
            bus.close()
            raise
        _bus, _owner, _fd = bus, _threading.get_ident(), bool(fd)
        _priority = priority
        return info

def _connection():
    if _bus is None:
        raise RuntimeError('CANable is not open; call canable.open() first')
    if _owner != _threading.get_ident():
        raise RuntimeError('CANable operations must run on the thread that called open()')
    return _bus

def send(can_id, data, *, brs=None, is_extended=False):
    """Send bytes; frame mode follows open(fd=...). USB completion is not CAN ACK."""
    with _lock:
        bus = _connection()
        bus.send(_Frame(can_id, data, is_fd=_fd, brs=_fd if brs is None else brs,
                        is_extended=is_extended))

def recv(timeout=0.1):
    """Return the next frame (can_id, data, flags), or None on timeout."""
    with _lock:
        return _connection().recv(timeout)

def close():
    """Stop CAN and release USB; safe to repeat. Does not disable a motor."""
    global _bus, _owner, _fd, _priority
    with _lock:
        if _bus is None:
            return
        bus = _connection()
        try:
            bus.close()
        finally:
            try:
                if _priority is not None:
                    _priority.restore()
            finally:
                _bus, _owner, _fd, _priority = None, None, False, None
