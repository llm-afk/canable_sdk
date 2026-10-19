"""Windows x64 C++ periodic CAN FD engine; all methods belong to one Python thread."""
import ctypes as C
from pathlib import Path
import threading

from .transport import Canable, DLC_LENGTHS
from .frame import CanFrame


class _Stats(C.Structure):
    _fields_ = [(k, C.c_uint64) for k in
                'submitted tx rx echoes skipped busy timeouts errors other pending running win_error'.split()] + [
                (k, C.c_double) for k in
                'elapsed interval_avg interval_p99 interval_max usb_avg usb_max rtt_avg rtt_p99 rtt_max'.split()] + [('serial', C.c_char * 28)]


class _Frame(C.Structure):
    _fields_ = [('id', C.c_uint32), ('size', C.c_uint32), ('flags', C.c_uint32), ('data', C.c_ubyte * 64)]


class NativeBus:
    """Exclusive USB ownership; continuous busy polling consumes about one CPU core.

    TX is standard-ID CAN FD+BRS. Generic RX queue holds 256 frames: drain it
    frequently. Queue overflow and CAN/USB errors stop transmission, never hide loss.
    stop() joins native I/O; close() then releases USB. No background Python callbacks.
    """
    def __init__(self, serial_number=None, bitrate=1_000_000, data_bitrate=4_000_000):
        self._owner = threading.get_ident()
        self._ctx = None
        self._bus = None
        self._dll = C.CDLL(str(Path(__file__).with_name('_native.dll')))
        d = self._dll
        specs = {
            'native_size': ([C.c_uint], C.c_uint),
            'native_start': ([C.c_void_p, C.c_void_p, C.c_uint, C.c_uint, C.c_uint, C.c_void_p, C.c_uint, C.c_double, C.c_int, C.c_double], C.c_void_p),
            'native_stop': ([C.c_void_p], None), 'native_free': ([C.c_void_p], None),
            'native_update': ([C.c_void_p, C.c_void_p, C.c_uint], C.c_int),
            'native_recv': ([C.c_void_p, C.POINTER(_Frame)], C.c_int),
            'native_stats': ([C.c_void_p, C.POINTER(_Stats)], C.c_int),
        }
        for name, (args, result) in specs.items():
            fn = getattr(d, name)
            fn.argtypes, fn.restype = args, result
        if d.native_size(0) != C.sizeof(_Stats) or d.native_size(1) != C.sizeof(_Frame):
            raise RuntimeError('Native DLL ABI mismatch; rebuild native/engine.cpp')
        self._bus = Canable(serial_number).open(bitrate, data_bitrate)
        self.info = self._bus.info.copy()

    def _check(self, started=True):
        if threading.get_ident() != self._owner:
            raise RuntimeError('Use NativeBus on its owning Python thread')
        if self._bus is None or (started and not self._ctx):
            raise RuntimeError('Bus closed or engine not started')

    def _start(self, can_id, data, rate, node, seconds):
        self._check(False)
        if self._ctx:
            raise RuntimeError('Create a new NativeBus for a new run')
        data = bytes(data)
        if len(data) not in DLC_LENGTHS or not 0 <= can_id <= 0x7ff:
            raise ValueError('Invalid standard ID or CAN FD payload length')
        self._bus._usb.suspend_read()
        u = self._bus._usb
        self._ctx = self._dll.native_start(u.handle, u.usb, u.input, u.output,
                                          can_id, data, len(data), rate, node, seconds)
        if not self._ctx:
            raise ValueError('Cannot start native engine; check rate (0,10000] and duration >= 0')
        return self

    def start_periodic(self, can_id, data, rate=1000, seconds=0):
        """Send latest payload at native deadlines; seconds=0 runs until stop()."""
        return self._start(can_id, data, rate, -1, seconds)

    def start_sdo_test(self, node=1, rate=1000, seconds=0):
        """Read-only SN upload test for the mc_tools motor protocol, no enable."""
        if not isinstance(node, int) or not 1 <= node <= 127:
            raise ValueError('node must be 1..127')
        return self._start(0, b'', rate, node, seconds)

    def update(self, data):
        self._check()
        data = bytes(data)
        if not self._dll.native_update(self._ctx, data, len(data)):
            raise ValueError('Engine stopped, SDO mode, or invalid FD payload length')

    def recv(self):
        """Nonblocking; returns CanFrame or None. Poll frequently in generic mode."""
        self._check()
        f = _Frame()
        if not self._dll.native_recv(self._ctx, C.byref(f)):
            return None
        return CanFrame(f.id, bytes(f.data[:f.size]), bool(f.flags & 2), bool(f.flags & 4),
                        bool(f.flags & 0x100), False, bool(f.flags & 0x200))

    def stats(self):
        """Cumulative statistics; durations in milliseconds, elapsed in seconds.

        tx counts USB completions, rx matched SDO replies (or generic RX), echoes
        counts adapter echoes. Interval timestamps are host USB submissions.
        p99 uses 10 us histogram bins. Inspect errors/timeouts/running every poll.
        """
        self._check()
        s = _Stats()
        self._dll.native_stats(self._ctx, C.byref(s))
        result = {k: getattr(s, k) for k, _ in s._fields_}
        result['serial'] = s.serial.decode('ascii', errors='replace').rstrip('\x00')
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

    def __exit__(self, *exc):
        self.close()
