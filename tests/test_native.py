"""Native ABI and Python wrappers; all device creation is mocked."""
import ctypes as C
from pathlib import Path
import threading
import unittest
from unittest.mock import MagicMock, patch

from canable_sdk import native


class NativeTests(unittest.TestCase):
    def bus(self):
        bus = object.__new__(native.NativeBus)
        bus._owner = threading.get_ident()
        bus._ctx = 1
        bus._bus = MagicMock()
        bus._dll = MagicMock()
        return bus

    def test_built_dll_abi_without_usb(self):
        dll = C.CDLL(str(Path(native.__file__).with_name('_native.dll')))
        dll.native_version.restype = C.c_uint
        dll.native_size.argtypes = [C.c_uint]
        dll.native_size.restype = C.c_uint
        self.assertEqual(dll.native_version(), 3)
        self.assertEqual(dll.native_size(0), C.sizeof(native._Stats))
        self.assertEqual(dll.native_size(1), C.sizeof(native._Frame))

    def test_old_abi_fails_before_opening_usb(self):
        dll = MagicMock()
        dll.native_version.return_value = 2
        with patch.object(native.C, 'CDLL', return_value=dll), patch.object(native, 'Canable') as adapter:
            with self.assertRaisesRegex(RuntimeError, 'ABI mismatch'):
                native.NativeBus()
            adapter.assert_not_called()

    def test_batch_metadata_and_payload_ownership(self):
        bus = self.bus()
        def receive(ctx, output, capacity):
            for i in range(2):
                output[i].id = i + 1
                output[i].size = 1
                output[i].data[0] = i + 10
                output[i].flags = 6 | (0x400 if i else 0)
                output[i].timestamp_ns = 100 + i
                output[i].sequence = i + 1
            return 2
        bus._dll.native_recv_many.side_effect = receive
        frames = bus.recv_many(4)
        self.assertEqual([f.sequence for f in frames], [1, 2])
        self.assertEqual([f.timestamp_ns for f in frames], [100, 101])
        self.assertFalse(frames[0].is_error)
        self.assertTrue(frames[1].is_error)
        buffer = bus._batch
        bus.recv_many(2)
        self.assertIs(bus._batch, buffer)
        buffer[0].data[0] = 0
        self.assertEqual(frames[0].data, bytes([10]))

    def test_invalid_batch_limit(self):
        bus = self.bus()
        for value in (0, -1, True, 1.5, 65537):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bus.recv_many(value)
        bus._dll.native_recv_many.assert_not_called()

    def test_single_receive_uses_same_frame_contract(self):
        bus = self.bus()
        def receive(ctx, output):
            frame = output._obj
            frame.id, frame.size, frame.flags = 3, 0, 6
            frame.timestamp_ns, frame.sequence = 500, 7
            return 1
        bus._dll.native_recv.side_effect = receive
        frame = bus.recv()
        self.assertEqual((frame.can_id, frame.timestamp_ns, frame.sequence), (3, 500, 7))

    def test_close_orders_stop_free_and_usb_release(self):
        bus = self.bus()
        actions = []
        bus._dll.native_stop.side_effect = lambda ctx: actions.append('stop')
        bus._dll.native_free.side_effect = lambda ctx: actions.append('free')
        bus._bus.close.side_effect = lambda: actions.append('close')
        bus.close()
        bus.close()
        self.assertEqual(actions, ['stop', 'free', 'close'])

    def test_wrong_owner_rejected_before_batch_call(self):
        bus = self.bus()
        errors = []
        def other_thread():
            try:
                bus.recv_many()
            except RuntimeError as error:
                errors.append(str(error))
        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join()
        self.assertEqual(len(errors), 1)
        bus._dll.native_recv_many.assert_not_called()


if __name__ == '__main__':
    unittest.main()
