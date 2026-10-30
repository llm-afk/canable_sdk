"""Windows x64 C++ periodic CAN FD engine; all methods belong to one Python thread."""
import ctypes as C
import math
from pathlib import Path
import threading

from .transport import Canable, DLC_LENGTHS
from .frame import CanFrame

__all__ = ["NativeBus"]


class _Stats(C.Structure):
    _fields_ = [(k, C.c_uint64) for k in
                'submitted tx rx echoes skipped errors pending running win_error delivered queued queue_high_water can_errors unparsed_bytes'.split()] + [
                (k, C.c_double) for k in
                'elapsed interval_avg interval_min interval_median interval_p99 interval_max interval_std usb_avg usb_max'.split()]


class _Frame(C.Structure):
    _fields_ = [('id', C.c_uint32), ('size', C.c_uint32), ('flags', C.c_uint32),
                ('data', C.c_ubyte * 64), ('timestamp_ns', C.c_uint64), ('sequence', C.c_uint64)]


class NativeBus:
    """Exclusive USB ownership; continuous busy polling consumes about one CPU core.

    TX is standard-ID CAN FD+BRS. RX storage grows as needed; drain it regularly
    and monitor queued/queue_high_water. CAN error reports are returned as frames.
    stop() collects successful pending reads and joins I/O; drain before close().
    Memory exhaustion and USB/protocol failures remain explicit fatal errors.
    """
    def __init__(self, serial_number=None, bitrate=1_000_000, data_bitrate=4_000_000):
        self._owner = threading.get_ident()
        self._ctx = None
        self._bus = None
        self._dll = C.CDLL(str(Path(__file__).with_name('_native.dll')))
        d = self._dll
        specs = {
            'native_version': ([], C.c_uint),
            'native_size': ([C.c_uint], C.c_uint),
            'native_start': ([C.c_void_p, C.c_void_p, C.c_uint, C.c_uint, C.c_uint, C.c_void_p, C.c_uint, C.c_double, C.c_double], C.c_void_p),
            'native_stop': ([C.c_void_p], None), 'native_free': ([C.c_void_p], None),
            'native_update': ([C.c_void_p, C.c_void_p, C.c_uint], C.c_int),
            'native_recv': ([C.c_void_p, C.POINTER(_Frame)], C.c_int),
            'native_recv_many': ([C.c_void_p, C.POINTER(_Frame), C.c_uint], C.c_uint),
            'native_stats': ([C.c_void_p, C.POINTER(_Stats)], C.c_int),
        }
        for name, (args, result) in specs.items():
            try:
                fn = getattr(d, name)
            except AttributeError as exc:
                raise RuntimeError('Native DLL ABI mismatch; rebuild native/engine.cpp') from exc
            fn.argtypes, fn.restype = args, result
        if (d.native_version() != 3 or d.native_size(0) != C.sizeof(_Stats)
                or d.native_size(1) != C.sizeof(_Frame)):
            raise RuntimeError('Native DLL ABI mismatch; rebuild native/engine.cpp')
        self._bus = Canable(serial_number).open(bitrate, data_bitrate)
        try:
            self.info = self._bus.info.copy()
        except BaseException:
            self._bus.close()
            self._bus = None
            raise

    def _check(self, started=True):
        if threading.get_ident() != self._owner:
            raise RuntimeError('Use NativeBus on its owning Python thread')
        if self._bus is None or (started and not self._ctx):
            raise RuntimeError('Bus closed or engine not started')

    def start_periodic(self, can_id, data, rate=1000, seconds=0):
        """Send latest payload at native deadlines; seconds=0 runs until stop()."""
        self._check(False)
        if self._ctx:
            raise RuntimeError('Create a new NativeBus for a new run')
        data = bytes(data)
        if len(data) not in DLC_LENGTHS or type(can_id) is not int or not 0 <= can_id <= 0x7ff:
            raise ValueError('Invalid standard ID or CAN FD payload length')
        if (type(rate) not in (int, float) or not math.isfinite(rate) or not 0 < rate <= 10000
                or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0):
            raise ValueError('Rate must be finite in (0,10000]; duration finite and >= 0')
        self._bus._usb.suspend_read()
        u = self._bus._usb
        self._ctx = self._dll.native_start(u.handle, u.usb, u.input, u.output,
                                          can_id, data, len(data), rate, seconds)
        if not self._ctx:
            raise RuntimeError('Cannot start native engine; USB connection remains owned until close()')
        return self

    def update(self, data):
        self._check()
        data = bytes(data)
        if len(data) not in DLC_LENGTHS:
            raise ValueError('Invalid CAN FD payload length')
        if not self._dll.native_update(self._ctx, data, len(data)):
            raise RuntimeError('Native engine stopped; inspect stats()')

    def recv(self):
        """Nonblocking; returns CanFrame or None. Poll frequently in generic mode."""
        self._check()
        f = _Frame()
        if not self._dll.native_recv(self._ctx, C.byref(f)):
            return None
        return self._frame(f)

    @staticmethod
    def _frame(f):
        return CanFrame(f.id, bytes(f.data[:f.size]), bool(f.flags & 2), bool(f.flags & 4),
                        bool(f.flags & 0x100), bool(f.flags & 0x400), bool(f.flags & 0x200),
                        f.timestamp_ns, f.sequence)

    def recv_many(self, max_frames=1024):
        """Nonblocking FIFO batch. Reduces DLL crossings; does not limit RX storage."""
        self._check()
        if type(max_frames) is not int or not 1 <= max_frames <= 65536:
            raise ValueError('max_frames must be an integer in [1,65536]')
        if not hasattr(self, '_batch') or len(self._batch) < max_frames:
            self._batch = (_Frame * max_frames)()
        count = self._dll.native_recv_many(self._ctx, self._batch, max_frames)
        return [self._frame(self._batch[i]) for i in range(count)]

    def stats(self):
        """Cumulative statistics; durations in milliseconds, elapsed in seconds.

        tx counts USB completions, rx queued CAN data frames, echoes
        counts adapter echoes. Interval timestamps are host USB submissions.
        delivered counts frames removed by recv/recv_many; queued is current RX
        depth. queue_high_water observes backlog. can_errors counts CAN reports;
        unparsed_bytes flags an incomplete/invalid USB tail at stop.
        p99 uses 10 us histogram bins. Inspect errors/running every poll.
        """
        self._check()
        s = _Stats()
        self._dll.native_stats(self._ctx, C.byref(s))
        result = {k: getattr(s, k) for k, _ in s._fields_}
        return result

    def stop(self):
        self._check(False)
        if self._ctx:
            self._dll.native_stop(self._ctx)

    def close(self):
        if self._bus is None:
            return
        self._check(False)
        self.stop()
        if self._ctx:
            self._dll.native_free(self._ctx)
            self._ctx = None
        bus, self._bus = self._bus, None
        bus.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.close()
        except Exception as cleanup:
            if exc is None:
                raise
            exc.add_note(f'CANable cleanup failed: {cleanup}')
