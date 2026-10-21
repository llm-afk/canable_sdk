"""Windows WinUSB backend for the verified single-channel Candlelight 2.5.

Uses legacy gs_usb frames (no Elmue blobs, no USB timestamps), not SLCAN.
detect() does not start CAN. open() configures and starts CAN.
"""
import ctypes as C
from ctypes import wintypes as W
import json
import math
import os
import struct
import subprocess
import time

from .frame import CanFrame

DLC_LENGTHS = (*range(9), 12, 16, 20, 24, 32, 48, 64)
SUPPORTED_FIRMWARE = ("0x260618", "0x260803", "0x261001")


class UsbFrameError(ValueError):
    """Malformed USB packet, retaining evidence for diagnostics."""
    def __init__(self, message, packet):
        self.packet = bytes(packet)
        self.length = len(packet)
        self.dlc = packet[8] if len(packet) > 8 else None
        self.channel = packet[9] if len(packet) > 9 else None
        self.flags = packet[10] if len(packet) > 10 else None
        super().__init__(f"{message}: length={self.length}, dlc={self.dlc}, "
                         f"channel={self.channel}, flags={self.flags}, raw={self.packet.hex()}")


class Setup(C.Structure):
    _pack_ = 1
    _fields_ = [("kind", C.c_uint8), ("request", C.c_uint8),
                ("value", C.c_uint16), ("index", C.c_uint16), ("length", C.c_uint16)]


class PipeInfo(C.Structure):
    _fields_ = [("kind", C.c_int), ("pipe_id", C.c_uint8),
                ("packet_size", C.c_uint16), ("interval", C.c_uint8)]


class Overlapped(C.Structure):
    _fields_ = [("internal", C.c_size_t), ("internal_high", C.c_size_t),
                ("offset", C.c_uint32), ("offset_high", C.c_uint32), ("event", W.HANDLE)]


class _ReadSlot:
    def __init__(self, event, size):
        self.event = event
        self.buffer = C.create_string_buffer(size)
        self.overlapped = Overlapped(event=event)
        self.pending = False


def _bind(dll, name, args, result=W.BOOL):
    fn = getattr(dll, name)
    fn.argtypes, fn.restype = args, result
    return fn


def _check(ok):
    if not ok:
        raise C.WinError(C.get_last_error())


class _ThreadPriority:
    """Owner-thread boost. Must restore on the same owning thread."""
    def __init__(self):
        if os.name != "nt":
            raise OSError("Thread priority mode requires Windows")
        self.k = C.WinDLL("kernel32", use_last_error=True)
        _bind(self.k, "GetCurrentThread", [], W.HANDLE)
        _bind(self.k, "GetThreadPriority", [W.HANDLE], C.c_int)
        _bind(self.k, "SetThreadPriority", [W.HANDLE, C.c_int])
        self.thread = self.k.GetCurrentThread()  # Pseudo handle: do not close.
        self.original = self.k.GetThreadPriority(self.thread)
        if self.original == 0x7FFFFFFF:
            raise C.WinError(C.get_last_error())
        _check(self.k.SetThreadPriority(self.thread, max(self.original, 2)))

    def restore(self):
        _check(self.k.SetThreadPriority(self.thread, self.original))


class _Usb:
    def __init__(self, path, *, low_latency=True):
        # CPU/latency tradeoff, scoped to this connection. No global
        # timer resolution, process priority or send completion semantics change.
        self._read_spin = .002 if low_latency else .0006
        self._write_spin = .002 if low_latency else .001
        if os.name != "nt":
            raise OSError("Canable backend requires Windows + WinUSB")
        self.k = C.WinDLL("kernel32", use_last_error=True)
        self.w = C.WinDLL("winusb", use_last_error=True)
        h, p, u = W.HANDLE, C.c_void_p, C.c_uint32
        _bind(self.k, "CreateFileW", [W.LPCWSTR, u, u, p, u, u, h], h)
        _bind(self.k, "CloseHandle", [h])
        _bind(self.k, "CreateEventW", [p, W.BOOL, W.BOOL, W.LPCWSTR], h)
        _bind(self.k, "ResetEvent", [h])
        _bind(self.k, "WaitForSingleObject", [h, u], u)
        _bind(self.k, "CancelIoEx", [h, C.POINTER(Overlapped)])
        _bind(self.w, "WinUsb_Initialize", [h, C.POINTER(h)])
        _bind(self.w, "WinUsb_Free", [h])
        _bind(self.w, "WinUsb_ControlTransfer", [h, Setup, p, u, C.POINTER(u), p])
        _bind(self.w, "WinUsb_QueryPipe", [h, C.c_uint8, C.c_uint8, C.POINTER(PipeInfo)])
        _bind(self.w, "WinUsb_SetPipePolicy", [h, C.c_uint8, u, u, p])
        _bind(self.w, "WinUsb_GetPipePolicy", [h, C.c_uint8, u, C.POINTER(u), p])
        _bind(self.w, "WinUsb_QueryDeviceInformation", [h, u, C.POINTER(u), p])
        _bind(self.w, "WinUsb_GetOverlappedResult", [h, C.POINTER(Overlapped), C.POINTER(u), W.BOOL])
        for name in ("WinUsb_ReadPipe", "WinUsb_WritePipe"):
            _bind(self.w, name, [h, C.c_uint8, p, u, C.POINTER(u), p])
        self.handle = self.k.CreateFileW(path, 0xC0000000, 0, None, 3, 0x40000000, None)
        if self.handle == C.c_void_p(-1).value:
            self.handle = None
            raise C.WinError(C.get_last_error())
        self.usb = h()
        self._reads = []
        self._read_head = 0
        self._tx_event = None
        self._tx_pending = False
        self._write_timeouts = {}
        try:
            _check(self.w.WinUsb_Initialize(self.handle, C.byref(self.usb)))
            pipes = []
            for index in range(2):
                info = PipeInfo()
                _check(self.w.WinUsb_QueryPipe(self.usb, 0, index, C.byref(info)))
                pipes.append(info)
            inputs = [x for x in pipes if x.kind == 2 and x.pipe_id & 0x80]
            outputs = [x for x in pipes if x.kind == 2 and not x.pipe_id & 0x80]
            if len(inputs) != 1 or len(outputs) != 1:
                raise OSError("expected one bulk IN and one bulk OUT endpoint")
            self.input, self.output = inputs[0].pipe_id, outputs[0].pipe_id
            self.read_size = max(1024, inputs[0].packet_size * 2)
            self.read_size = math.ceil(self.read_size / inputs[0].packet_size) * inputs[0].packet_size
            maximum, length = u(), u(4)
            _check(self.w.WinUsb_GetPipePolicy(self.usb, self.input, 8, C.byref(length), C.byref(maximum)))
            if self.read_size > maximum.value:
                raise OSError("USB read buffer exceeds MAXIMUM_TRANSFER_SIZE")
            # Keep the verified buffered IN policy, including after another client.
            enabled = C.c_ubyte(0)
            _check(self.w.WinUsb_SetPipePolicy(self.usb, self.input, 7, 1, C.byref(enabled)))
            actual, length = C.c_ubyte(), u(1)
            _check(self.w.WinUsb_GetPipePolicy(self.usb, self.input, 7, C.byref(length), C.byref(actual)))
            if actual.value != 0:
                raise OSError("RAW_IO policy readback mismatch")
            speed, length = C.c_ubyte(), u(1)
            _check(self.w.WinUsb_QueryDeviceInformation(self.usb, 1, C.byref(length), C.byref(speed)))
            self.io_info = dict(speed={1: "full-speed-or-lower", 3: "high-speed-or-higher"}.get(speed.value, "unknown"),
                                speed_code=speed.value,
                                in_packet=inputs[0].packet_size, out_packet=outputs[0].packet_size,
                                read_size=self.read_size, read_queue=8, raw_io=bool(actual.value),
                                maximum_transfer=maximum.value)
            # Keep USB IN requests queued while Python is descheduled or writing.
            # The queue is bounded and consumed in submission order.
            for _ in range(8):
                event = self.k.CreateEventW(None, True, False, None)
                _check(event)
                try:
                    self._reads.append(_ReadSlot(event, self.read_size))
                except BaseException:
                    self.k.CloseHandle(event)
                    raise
            self._tx_event = self.k.CreateEventW(None, True, False, None)
            _check(self._tx_event)
            self._tx_overlapped = Overlapped(event=self._tx_event)
            self._tx_buffer = C.create_string_buffer(76)
            no_timeout = C.c_uint32(0)
            _check(self.w.WinUsb_SetPipePolicy(self.usb, self.input, 3, 4, C.byref(no_timeout)))
        except BaseException:
            self.close()
            raise

    def control(self, request, *, length=0, data=None):
        incoming = data is None
        size = length if incoming else len(data)
        buf = C.create_string_buffer(size) if incoming else C.create_string_buffer(data, size)
        n = C.c_uint32()
        setup = Setup(0xC1 if incoming else 0x41, request, 0, 0, size)
        _check(self.w.WinUsb_ControlTransfer(self.usb, setup, buf, size, C.byref(n), None))
        if n.value != size:
            raise OSError(f"short USB control transfer: {n.value}/{size}")
        return buf.raw

    def transfer(self, pipe, data=None, timeout=0.1):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("USB timeout must be finite and positive")
        if data is None:
            return self._read_async(timeout)
        milliseconds = min(0xFFFFFFFF, max(1, math.ceil(timeout * 1000)))
        if self._write_timeouts.get(pipe) != milliseconds:
            value = C.c_uint32(milliseconds)
            _check(self.w.WinUsb_SetPipePolicy(self.usb, pipe, 3, 4, C.byref(value)))
            self._write_timeouts[pipe] = milliseconds
        return self._write_async(pipe, data, timeout)

    def _write_async(self, pipe, data, timeout):
        """Overlapped OUT, but preserve send's USB-completed return contract."""
        if self._tx_pending:
            raise RuntimeError("USB write already pending")
        size = len(data)
        if size > len(self._tx_buffer):
            raise ValueError("USB frame exceeds preallocated transmit buffer")
        self._issue_read()  # IN can progress while OUT completes.
        C.memmove(self._tx_buffer, data, size)
        _check(self.k.ResetEvent(self._tx_event))
        self._tx_overlapped = Overlapped(event=self._tx_event)
        ok = self.w.WinUsb_WritePipe(self.usb, pipe, self._tx_buffer, size,
                                    None, C.byref(self._tx_overlapped))
        if not ok and C.get_last_error() != 997:
            raise C.WinError(C.get_last_error())
        self._tx_pending = True
        started = time.perf_counter()
        deadline = started + timeout
        try:
            while True:
                state = self.k.WaitForSingleObject(self._tx_event, 0)
                if state == 0:
                    n = C.c_uint32()
                    ok = self.w.WinUsb_GetOverlappedResult(
                        self.usb, C.byref(self._tx_overlapped), C.byref(n), False)
                    self._tx_pending = False
                    _check(ok)
                    if n.value != size:
                        raise OSError("short USB write; CAN transmission outcome uncertain")
                    return data
                if state != 258:
                    raise C.WinError(C.get_last_error())
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise TimeoutError("USB write timed out; CAN transmission outcome uncertain")
                # Poll the usual sub-ms completion without a Windows wait wakeup.
                # Long exceptional waits sleep; low_latency extends the initial
                # polling window from 1 ms to 2 ms.
                if time.perf_counter() - started > self._write_spin:
                    time.sleep(min(remaining, .001))
        finally:
            self._cancel_write()

    def _cancel_write(self):
        if self._tx_pending:
            self.k.CancelIoEx(self.handle, C.byref(self._tx_overlapped))
            n = C.c_uint32()
            self.w.WinUsb_GetOverlappedResult(self.usb, C.byref(self._tx_overlapped), C.byref(n), True)
            self._tx_pending = False

    def _read_async(self, timeout):
        """Caller timeout preserves queued IN; completed data is consumed in order."""
        self._issue_read()
        slot = self._reads[self._read_head]
        deadline = time.perf_counter() + timeout
        while True:
            result = self.k.WaitForSingleObject(slot.event, 0)
            if result == 0:
                n = C.c_uint32()
                ok = self.w.WinUsb_GetOverlappedResult(self.usb, C.byref(slot.overlapped), C.byref(n), False)
                slot.pending = False
                _check(ok)
                data = C.string_at(slot.buffer, n.value)
                self._submit_read(slot)  # Requeue at the USB tail before decoding.
                self._read_head = (self._read_head + 1) % len(self._reads)
                return data
            if result != 258:  # WAIT_TIMEOUT
                raise C.WinError(C.get_last_error())
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return None
            # A 100 us Python sleep measured ~0.54 ms on the target Windows
            # host, and can overshoot the next TX deadline. Keep the final
            # 600 us awake (2 ms with low_latency), polling the overlapped event
            # (ctypes releases GIL). Longer waits still sleep.
            if remaining > self._read_spin:
                time.sleep(min(remaining - self._read_spin, 0.001))

    def _issue_read(self):
        # After suspend all slots are idle; during normal I/O all are pending.
        for offset in range(len(self._reads)):
            slot = self._reads[(self._read_head + offset) % len(self._reads)]
            if not slot.pending:
                self._submit_read(slot)

    def _submit_read(self, slot):
        if not slot.pending:
            _check(self.k.ResetEvent(slot.event))
            slot.overlapped = Overlapped(event=slot.event)
            ok = self.w.WinUsb_ReadPipe(self.usb, self.input, slot.buffer,
                                       self.read_size, None, C.byref(slot.overlapped))
            if not ok and C.get_last_error() != 997:  # ERROR_IO_PENDING
                raise C.WinError(C.get_last_error())
            slot.pending = True

    def suspend_read(self):
        """Complete/cancel IN before an exclusive native owner takes this handle."""
        for slot in self._reads:
            if slot.pending:
                self.k.CancelIoEx(self.handle, C.byref(slot.overlapped))
        for slot in self._reads:
            if slot.pending:
                # Never release or reuse buffers until cancellation completes.
                n = C.c_uint32()
                self.w.WinUsb_GetOverlappedResult(self.usb, C.byref(slot.overlapped), C.byref(n), True)
                slot.pending = False

    def close(self):
        if getattr(self, '_tx_pending', False):
            self._cancel_write()
        self.suspend_read()
        if getattr(self, '_tx_event', None):
            self.k.CloseHandle(self._tx_event)
            self._tx_event = None
        for slot in self._reads:
            self.k.CloseHandle(slot.event)
        self._reads.clear()
        if self.usb:
            self.w.WinUsb_Free(self.usb)
            self.usb = W.HANDLE()
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None


def bit_timing(clock, bitrate, sample_point, limits):
    """Return prop, seg1, seg2, sjw, brp using actual device limits."""
    if type(bitrate) is not int or bitrate <= 0 or not 0 < sample_point < 100:
        raise ValueError("invalid bitrate/sample point")
    s1_min, s1_max, s2_min, s2_max, sjw_max, brp_min, brp_max, inc = limits
    if not (0 < brp_min <= brp_max <= 65536 and inc > 0 and sjw_max > 0):
        raise ValueError("invalid device timing limits")
    choices = []
    for brp in range(brp_min, brp_max + 1, inc):
        if clock % (bitrate * brp):
            continue
        total = clock // (bitrate * brp)
        ideal = total * sample_point / 100 - 1
        lo, hi = max(s1_min, total - 1 - s2_max), min(s1_max, total - 1 - s2_min)
        if lo > hi:
            continue
        for s1 in {max(lo, min(hi, math.floor(ideal))), max(lo, min(hi, math.ceil(ideal)))}:
            s2 = total - 1 - s1
            choices.append((abs((1+s1)/total*100-sample_point), brp, s1, s2))
    if not choices:
        raise ValueError("requested bitrate is not supported by device timing limits")
    _, brp, s1, s2 = min(choices)
    return (0, s1, s2, min(s1, s2, sjw_max), brp)


def encode_usb(frame, echo_id=1):
    if frame.is_error or frame.is_remote:
        raise ValueError("only CAN data frames may be transmitted")
    if (type(frame.can_id) is not int or not 0 <= frame.can_id <= (0x1FFFFFFF if frame.is_extended else 0x7FF)
            or len(frame.data) not in DLC_LENGTHS or (not frame.is_fd and len(frame.data) > 8)
            or (frame.brs and not frame.is_fd)):
        raise ValueError("invalid CAN frame")
    flags = (2 if frame.is_fd else 0) | (4 if frame.brs else 0)
    can_id = frame.can_id | (0x80000000 if frame.is_extended else 0)
    return struct.pack("<IIBBBB", echo_id, can_id, DLC_LENGTHS.index(len(frame.data)), 0, flags, 0) + frame.data.ljust(64 if frame.is_fd else 8, b"\0")


def decode_usb(packet):
    if len(packet) < 12:
        raise UsbFrameError("short gs_usb header", packet)
    echo, can_id, dlc, channel, flags, _ = struct.unpack_from("<IIBBBB", packet)
    fd = bool(flags & 2)
    if len(packet) != (76 if fd else 20) or dlc > (15 if fd else 8) or channel != 0:
        raise UsbFrameError("invalid gs_usb frame length/DLC/channel", packet)
    if flags & 1:
        raise OSError("CANable receive overflow")
    if echo != 0xFFFFFFFF:
        return None  # USB TX echo is not motor feedback.
    return CanFrame(can_id & 0x1FFFFFFF, packet[12:12+DLC_LENGTHS[dlc]], fd,
                    bool(flags & 4), bool(can_id & 0x80000000),
                    bool(can_id & 0x20000000), bool(can_id & 0x40000000))


class Canable:
    """Exclusive single-channel connection; all I/O must have one owner thread."""
    @staticmethod
    def detect():
        """列出已连接适配器，返回包含 serial_number 和 USB path 的字典列表。

        仅枚举设备，不启动 CAN。上层使用 serial_number 配置目标适配器，
        不保存可能随插口变化的 path。
        """
        if os.name != "nt":
            raise OSError("CANable WinUSB discovery requires Windows")
        script = r'''
$ErrorActionPreference = 'Stop'
$items = @(Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\VID_1D50&PID_606F&MI_00\*' } | ForEach-Object {
  $id = $_.InstanceId
  $parent = (Get-PnpDeviceProperty -InstanceId $id -KeyName DEVPKEY_Device_Parent).Data
  $guid = (Get-ItemProperty -LiteralPath ('HKLM:\SYSTEM\CurrentControlSet\Enum\' + $id + '\Device Parameters')).DeviceInterfaceGUIDs[0]
  [pscustomobject]@{serial_number=$parent.Split('\')[-1]; path=('\\?\' + $id.Replace('\','#') + '#' + $guid)}
})
ConvertTo-Json -InputObject $items -Compress
'''
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, timeout=30, creationflags=0x08000000)
        if result.returncode:
            raise OSError(f"USB discovery failed: {result.stderr.strip()}")
        return json.loads(result.stdout)

    def __init__(self, serial_number=None, *, low_latency=True):
        """只保存连接参数，不打开 USB。

        上层应用应传入指定 SN；None 仅在恰好一只适配器时可用。
        显式 SN 未匹配时直接报错，不回退到其他适配器。
        """
        self.serial_number = serial_number
        self.low_latency = bool(low_latency)
        self._fd = True
        self._usb = None
        self._configured = False
        self._started = False
        self._echo = 0
        self._rx = bytearray()

    def _connect(self):
        available = self.detect()
        devices = [d for d in available if self.serial_number is None or d["serial_number"] == self.serial_number]
        if len(devices) != 1:
            serials = ', '.join(d['serial_number'] for d in available) or '(none)'
            raise OSError(f"matched {len(devices)} CANable devices; connected serials: {serials}; specify --serial when multiple adapters are connected")
        return _Usb(devices[0]["path"], low_latency=self.low_latency)

    @staticmethod
    def _info(usb):
        major, minor, patch, count, sw, hw = struct.unpack("<BBBBII", usb.control(5, length=12))
        caps = struct.unpack("<18I", usb.control(11, length=72))
        return dict(firmware=f"0x{sw:06X}", hardware=f"0x{hw:X}", hal=f"{major}.{minor}.{patch}",
                    channels=count+1, features=f"0x{caps[0]:08X}", clock_hz=caps[1],
                    nominal_limits=caps[2:10], data_limits=caps[10:18])

    def _set(self, request, data):
        self._usb.control(request, data=data)
        # Elmue extension GET_LAST_ERROR is supported even in legacy mode.
        result = self._usb.control(22, length=1)[0]
        if result != 2:  # FBK_Success
            raise OSError(f"CANable rejected request {request}: feedback={result}")

    def open(self, bitrate=1_000_000, data_bitrate=4_000_000,
             sample_point=75.0, data_sample_point=64.0, *, fd=True):
        if self._usb:
            raise RuntimeError("CANable already open")
        self._usb = self._connect()
        try:
            self.info = self._info(self._usb)
            self.info["usb_io"] = self._usb.io_info.copy()
            if (self.info["channels"] != 1 or self.info["firmware"] not in SUPPORTED_FIRMWARE
                    or int(self.info["features"], 16) & 0x4500 != 0x4500):
                raise OSError(f"unsupported CANable capabilities/firmware: {self.info}; "
                              f"supported firmware: {', '.join(SUPPORTED_FIRMWARE)}")
            nominal = bit_timing(self.info["clock_hz"], bitrate, sample_point, self.info["nominal_limits"])
            data = bit_timing(self.info["clock_hz"], data_bitrate, data_sample_point, self.info["data_limits"]) if fd else None
            self._fd = bool(fd)
            self._configured = True
            self._set(2, struct.pack("<II", 0, 0))
            self._set(0, struct.pack("<I", 0xBEEF))
            self._set(1, struct.pack("<5I", *nominal))
            if fd:
                self._set(10, struct.pack("<5I", *data))
            self._set(2, struct.pack("<II", 1, 0x100 if fd else 0))
            self._started = True
            return self
        except BaseException:
            self.close()
            raise

    def send(self, frame):
        if not self._started:
            raise RuntimeError("CANable is not open")
        if frame.is_fd and not self._fd:
            raise ValueError("FD frame cannot be sent in classic CAN mode")
        self._echo = self._echo % 255 + 1
        self._usb.transfer(self._usb.output, encode_usb(frame, self._echo), timeout=0.1)

    def recv(self, timeout=0.1):
        if not self._started:
            raise RuntimeError("CANable is not open")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        deadline = time.perf_counter() + timeout
        while True:
            if len(self._rx) >= 12:
                size = 76 if self._rx[10] & 2 else 20
                if len(self._rx) >= size:
                    packet = bytes(self._rx[:size])
                    del self._rx[:size]
                    frame = decode_usb(packet)
                    if frame is not None:
                        return frame
                    continue
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return None
            chunk = self._usb.transfer(self._usb.input, timeout=remaining)
            if chunk is None:
                return None
            self._rx.extend(chunk)

    def close(self):
        usb, self._usb = self._usb, None
        try:
            if usb and self._configured:
                usb.control(2, data=struct.pack("<II", 0, 0))
        finally:
            self._configured = self._started = False
            self._rx.clear()
            if usb:
                usb.close()
