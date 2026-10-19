"""Single-connection CANable API. All connection operations use one owner thread."""
import threading as _threading
from .transport import Canable as _Canable, UsbFrameError, SUPPORTED_FIRMWARE
from .frame import CanFrame as _Frame
from .transport import _ThreadPriority

__all__ = ['detect', 'inspect', 'open', 'send', 'recv', 'close', 'diagnostics',
           'UsbFrameError', 'SUPPORTED_FIRMWARE']
_bus = None
_owner = None
_fd = False
_priority = None
_lock = _threading.RLock()

def detect():
    """Enumerate USB devices without starting CAN."""
    return _Canable.detect()

def inspect(serial_number=None):
    """Read adapter information before open(), without starting CAN."""
    with _lock:
        if _bus is not None:
            raise RuntimeError('close CANable before inspect')
        return _Canable(serial_number).inspect()

def open(serial_number=None, *, bitrate=1_000_000, fd=False,
         data_bitrate=4_000_000, sample_point=75.0, data_sample_point=64.0,
         filter_reports=False, low_latency=True, high_priority=True, raw_io=False):
    """Open one connection. low_latency trades CPU for shorter USB polling latency.

    Defaults to classic CAN, 1 Mbps. low_latency defaults to True; it does not
    change send's USB-completion contract or provide hard-real-time scheduling.
    high_priority defaults to True and boosts the owning thread to Windows HIGHEST until close().
    raw_io is an experimental IN-pipe policy, disabled by default.
    """
    global _bus, _owner, _fd, _priority
    with _lock:
        if _bus is not None:
            raise RuntimeError('CANable already open; close it before opening again')
        bus = _Canable(serial_number, filter_reports=filter_reports, low_latency=low_latency, raw_io=raw_io)
        # Transport.open performs rollback on failure. Publish only after success.
        bus.open(bitrate=bitrate, data_bitrate=data_bitrate, sample_point=sample_point,
                 data_sample_point=data_sample_point, fd=fd)
        try:
            priority = _ThreadPriority() if high_priority else None
        except BaseException:
            bus.close()
            raise
        _bus, _owner, _fd = bus, _threading.get_ident(), bool(fd)
        _priority = priority

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

def diagnostics():
    """Return connection information and optional legacy-filter counters."""
    with _lock:
        bus = _connection()
        return dict(info=dict(bus.info), counter_reports=bus.counter_reports,
                    protocol_reports=bus.protocol_reports,
                    last_counter_report=dict(bus.last_counter_report) if bus.last_counter_report else None,
                    last_protocol_report=dict(bus.last_protocol_report) if bus.last_protocol_report else None)

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
