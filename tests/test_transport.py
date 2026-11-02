"""Offline checks only. No discovery, USB handle or CAN transmission."""
import dataclasses
import struct
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import canable
from canable.frame import CanFrame
from canable.transport import Canable, DLC_LENGTHS, UsbFrameError, bit_timing, decode_usb, encode_usb


def received(frame):
    return encode_usb(frame, echo_id=0xFFFFFFFF)


class DiscoveryTests(unittest.TestCase):
    def test_discovery_uses_explicit_utf8_and_retains_localized_failure(self):
        with patch('canable.transport.subprocess.run', return_value=SimpleNamespace(
                returncode=1, stdout='', stderr='无法从客户端中访问 CIM 资源。')) as run:
            with self.assertRaisesRegex(OSError, 'CIM'):
                Canable.detect()
        self.assertEqual(run.call_args.kwargs['encoding'], 'utf-8')
        self.assertEqual(run.call_args.kwargs['errors'], 'replace')
        self.assertIn('OutputEncoding', run.call_args.args[0][-1])

    def test_discovery_empty_stderr_does_not_mask_original_failure(self):
        with patch('canable.transport.subprocess.run', return_value=SimpleNamespace(
                returncode=1, stdout='', stderr=None)):
            with self.assertRaisesRegex(OSError, 'USB discovery failed'):
                Canable.detect()


class CodecTests(unittest.TestCase):
    def test_all_lengths_and_modes_roundtrip(self):
        for fd in (False, True):
            for extended in (False, True):
                for size in DLC_LENGTHS if fd else range(9):
                    for brs in (False, True) if fd else (False,):
                        frame = CanFrame(0x12345 if extended else 0x123, bytes(range(size)),
                                         fd, brs, extended)
                        with self.subTest(fd=fd, extended=extended, size=size, brs=brs):
                            self.assertEqual(decode_usb(received(frame)), frame)

    def test_frame_is_immutable_and_owns_payload(self):
        payload = bytearray(b'abc')
        frame = CanFrame(1, payload)
        payload[0] = 0
        self.assertEqual(frame.data, b'abc')
        with self.assertRaises(dataclasses.FrozenInstanceError):
            frame.can_id = 2

    def test_receive_metadata_preserves_frame_content_equality(self):
        frame = CanFrame(1, b'a')
        observed = dataclasses.replace(frame, timestamp_ns=100, sequence=2)
        self.assertEqual(frame, observed)
        self.assertEqual(hash(frame), hash(observed))

    def test_tx_echo_is_not_rx(self):
        self.assertIsNone(decode_usb(encode_usb(CanFrame(1, b'a'))))

    def test_can_error_is_returned(self):
        packet = bytearray(received(CanFrame(1, b'12345678', False, False)))
        struct.pack_into('<I', packet, 4, 0x20000004)
        frame = decode_usb(packet)
        self.assertTrue(frame.is_error)
        self.assertEqual(frame.can_id, 4)

    def test_usb_validation_preserves_evidence(self):
        valid = received(CanFrame(1, b'a'))
        invalid = [b'bad', valid[:-1], valid + b'x']
        for index, value in ((8, 16), (9, 1)):
            packet = bytearray(valid)
            packet[index] = value
            invalid.append(bytes(packet))
        for packet in invalid:
            with self.subTest(packet=packet), self.assertRaises(UsbFrameError) as error:
                decode_usb(packet)
            self.assertEqual(error.exception.packet, packet)

    def test_hardware_overflow_is_explicit(self):
        packet = bytearray(received(CanFrame(1, b'a')))
        packet[10] |= 1
        with self.assertRaisesRegex(OSError, 'overflow'):
            decode_usb(packet)

    def test_invalid_tx_rejected(self):
        invalid = [CanFrame(0x800, b''), CanFrame(1, b'123456789', False, False),
                   CanFrame(1, b'', False, True), CanFrame(1, b'', is_error=True),
                   CanFrame(1, b'', is_remote=True), CanFrame(1, bytes(9))]
        for frame in invalid:
            with self.subTest(frame=frame), self.assertRaises(ValueError):
                encode_usb(frame)

    def test_bit_timing_exact_bitrate_and_limits(self):
        limits = (1, 256, 1, 128, 128, 1, 512, 1)
        prop, s1, s2, sjw, brp = bit_timing(160_000_000, 1_000_000, 75, limits)
        self.assertEqual(160_000_000, 1_000_000 * brp * (1 + prop + s1 + s2))
        self.assertLessEqual(sjw, min(s1, s2))
        for bitrate in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                bit_timing(160_000_000, bitrate, 75, limits)


class FakeUsb:
    input = 0x81
    output = 2

    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.last_read_ns = None
        self.closed = False

    def transfer(self, pipe, data=None, timeout=.1):
        self.last_read_ns = (self.last_read_ns or 0) + 100
        return self.chunks.pop(0) if self.chunks else None

    def close(self):
        self.closed = True


class ReceiveTests(unittest.TestCase):
    def bus(self, chunks):
        bus = Canable('offline')
        bus._usb = FakeUsb(chunks)
        bus._started = True
        return bus

    def test_coalesced_frames_keep_order_sequence_and_observation(self):
        frames = [CanFrame(i, bytes([i])) for i in range(1, 4)]
        bus = self.bus([b''.join(received(frame) for frame in frames)])
        actual = [bus.recv() for _ in frames]
        self.assertEqual([f.can_id for f in actual], [1, 2, 3])
        self.assertEqual([f.sequence for f in actual], [1, 2, 3])
        self.assertEqual([f.timestamp_ns for f in actual], [100, 100, 100])
        self.assertIsNone(bus.recv())

    def test_split_frame_uses_completion_observation(self):
        packet = received(CanFrame(1, b'abc'))
        bus = self.bus([packet[:10], packet[10:]])
        frame = bus.recv()
        self.assertEqual((frame.data, frame.sequence, frame.timestamp_ns), (b'abc', 1, 200))

    def test_echo_does_not_consume_sequence(self):
        frame = CanFrame(1, b'a')
        bus = self.bus([encode_usb(frame) + received(frame)])
        self.assertEqual(bus.recv().sequence, 1)

    def test_timeout_validation(self):
        bus = self.bus([])
        for timeout in (0, -1, float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                bus.recv(timeout)

    def test_close_releases_fake_backend(self):
        bus = self.bus([])
        usb = bus._usb
        bus.close()
        bus.close()
        self.assertTrue(usb.closed)
        self.assertFalse(bus._started)


class ModuleApiTests(unittest.TestCase):
    def setUp(self):
        self.saved = (canable._bus, canable._owner, canable._fd, canable._priority)
        canable._bus = canable._owner = canable._priority = None
        canable._fd = False

    def tearDown(self):
        canable._bus, canable._owner, canable._fd, canable._priority = self.saved

    def fake(self):
        class Fake:
            def __init__(self, *args, **kwargs):
                self.info = {'usb_io': {'offline': True}}
                self.sent = []
                self.closed = False

            def open(self, **kwargs):
                return self

            def send(self, frame):
                self.sent.append(frame)

            def close(self):
                self.closed = True
        return Fake

    def test_legacy_module_api_and_default_classic_mode(self):
        with patch.object(canable, '_Canable', self.fake()):
            info = canable.open(high_priority=False)
            bus = canable._bus
            info['usb_io']['offline'] = False
            self.assertTrue(bus.info['usb_io']['offline'])
            canable.send(1, b'a')
            self.assertFalse(bus.sent[0].is_fd)
            canable.close()
            canable.close()
            self.assertTrue(bus.closed)

    def test_cross_thread_call_rejected_before_io(self):
        with patch.object(canable, '_Canable', self.fake()):
            canable.open(high_priority=False)
            errors = []
            def other_thread():
                try:
                    canable.send(1, b'a')
                except RuntimeError as error:
                    errors.append(str(error))
            thread = threading.Thread(target=other_thread)
            thread.start()
            thread.join()
            self.assertEqual(len(errors), 1)
            self.assertFalse(canable._bus.sent)
            canable.close()

    def test_priority_failure_does_not_publish_connection(self):
        created = []
        fake = self.fake()
        def factory(*args, **kwargs):
            bus = fake()
            created.append(bus)
            return bus
        with patch.object(canable, '_Canable', factory), patch.object(
                canable, '_ThreadPriority', side_effect=OSError('offline failure')):
            with self.assertRaises(OSError):
                canable.open()
        self.assertIsNone(canable._bus)
        self.assertTrue(created[0].closed)


if __name__ == '__main__':
    unittest.main()
