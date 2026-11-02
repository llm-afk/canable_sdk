"""Native discovery checks use synthetic registry/devnodes, never USB handles."""
import threading
import unittest
from unittest.mock import MagicMock,patch

import canable
from canable.discovery import detect,GUID
from canable.transport import Canable


class NativeDiscoveryTests(unittest.TestCase):
    def test_present_only_interface_paths_and_real_parent_serial(self):
        manager=MagicMock()
        manager.locate.side_effect=[None,7,8]
        manager.paths.side_effect=[[],['active-device-path']]
        manager.serial.return_value='REAL-PARENT-SN'
        entries=[('stale',['guid']),('disabled',['guid']),('present',['guid'])]
        with patch('canable.discovery._instances',return_value=entries), \
             patch('canable.discovery._ConfigurationManager',return_value=manager):
            self.assertEqual(detect(),[{'serial_number':'REAL-PARENT-SN','path':'active-device-path'}])
        manager.serial.assert_called_once_with(8)

    def test_multiple_devices_remain_distinct(self):
        manager=MagicMock()
        manager.locate.side_effect=[1,2]
        manager.paths.side_effect=[['path-b'],['path-a']]
        manager.serial.side_effect=['B','A']
        with patch('canable.discovery._instances',return_value=[('b',['g']),('a',['g'])]), \
             patch('canable.discovery._ConfigurationManager',return_value=manager):
            self.assertEqual([d['serial_number'] for d in detect()],['A','B'])

    def test_present_device_without_driver_guid_is_not_hidden(self):
        manager=MagicMock();manager.locate.return_value=1
        with patch('canable.discovery._instances',return_value=[('device',[])]), \
             patch('canable.discovery._ConfigurationManager',return_value=manager):
            with self.assertRaisesRegex(OSError,'WinUSB interface GUID'):detect()

    def test_cancelled_scan_does_not_query_devices(self):
        event=threading.Event();event.set()
        with patch('canable.discovery._ConfigurationManager') as manager:
            with self.assertRaises(InterruptedError):detect(cancel_event=event)
        manager.assert_not_called()

    def test_deadline_and_bad_timeouts_do_not_scan(self):
        for timeout in (0,-1,float('inf'),float('nan')):
            with self.assertRaises(ValueError):detect(timeout=timeout)
        with patch('canable.discovery.time.perf_counter',side_effect=[0,4]), \
             patch('canable.discovery._ConfigurationManager') as manager:
            with self.assertRaises(TimeoutError):detect(timeout=3)
        manager.assert_not_called()

    def test_open_discovers_once_and_reports_stages(self):
        progress=[];usb=MagicMock();usb.io_info={}
        info={'firmware':'0x261001','channels':1,'features':'0x4500','clock_hz':160000000,
              'nominal_limits':(1,256,1,128,128,1,512,1),'data_limits':(1,256,1,128,128,1,512,1)}
        with patch.object(Canable,'detect',return_value=[{'serial_number':'S','path':'P'}]) as scan, \
             patch('canable.transport._Usb',return_value=usb), \
             patch.object(Canable,'_info',return_value=info),patch.object(Canable,'_set'):
            result=canable.open(high_priority=False,on_progress=progress.append)
            try:self.assertEqual(result['serial_number'],'S')
            finally:canable.close()
        scan.assert_called_once()
        self.assertEqual(progress,['discovering','opening_usb','reading_adapter','starting_can'])
