"""Read-only Windows device discovery without PowerShell/WMI or USB opens."""
import ctypes as C
import math
import os
import time
import uuid


class GUID(C.Structure):
    _fields_=[('a',C.c_uint32),('b',C.c_uint16),('c',C.c_uint16),('d',C.c_ubyte*8)]


def _instances():
    import winreg
    base=r'SYSTEM\CurrentControlSet\Enum\USB\VID_1D50&PID_606F&MI_00'
    try:
        key=winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,base)
    except FileNotFoundError:
        return
    with key:
        index=0
        while True:
            try:
                child=winreg.EnumKey(key,index)
            except OSError as exc:
                if exc.winerror==259:break  # ERROR_NO_MORE_ITEMS
                raise
            index+=1
            try:
                with winreg.OpenKey(key,child+r'\Device Parameters') as parameters:
                    guids,_=winreg.QueryValueEx(parameters,'DeviceInterfaceGUIDs')
            except FileNotFoundError:
                guids=[]
            if isinstance(guids,str):guids=[guids]
            yield 'USB\\VID_1D50&PID_606F&MI_00\\'+child,guids


class _ConfigurationManager:
    def __init__(self):
        self.dll=C.WinDLL('cfgmgr32',use_last_error=True)
        pointer=C.POINTER(C.c_uint32)
        for name,args in {
            'CM_Locate_DevNodeW':[pointer,C.c_wchar_p,C.c_uint32],
            'CM_Get_Parent':[pointer,C.c_uint32,C.c_uint32],
            'CM_Get_Device_ID_Size':[pointer,C.c_uint32,C.c_uint32],
            'CM_Get_Device_IDW':[C.c_uint32,C.c_wchar_p,C.c_uint32,C.c_uint32],
            'CM_Get_Device_Interface_List_SizeW':[pointer,C.POINTER(GUID),C.c_wchar_p,C.c_uint32],
            'CM_Get_Device_Interface_ListW':[C.POINTER(GUID),C.c_wchar_p,C.c_wchar_p,C.c_uint32,C.c_uint32],
        }.items():
            fn=getattr(self.dll,name)
            fn.argtypes=args;fn.restype=C.c_uint32

    @staticmethod
    def check(code,operation):
        if code:raise OSError(f'USB discovery {operation}: CONFIGRET=0x{code:08X}')

    def locate(self,instance):
        node=C.c_uint32()
        result=self.dll.CM_Locate_DevNodeW(C.byref(node),instance,0)
        if result==0x0D:return None  # CR_NO_SUCH_DEVNODE (nonpresent registry entry)
        self.check(result,'locate present device')
        return node.value

    def serial(self,node):
        parent=C.c_uint32();size=C.c_uint32()
        self.check(self.dll.CM_Get_Parent(C.byref(parent),node,0),'read parent')
        self.check(self.dll.CM_Get_Device_ID_Size(C.byref(size),parent,0),'size parent ID')
        text=C.create_unicode_buffer(size.value+1)
        self.check(self.dll.CM_Get_Device_IDW(parent,text,len(text),0),'read parent ID')
        return text.value.rsplit('\\',1)[-1]

    def paths(self,instance,guid):
        value=GUID.from_buffer_copy(uuid.UUID(guid).bytes_le)
        for _ in range(3):  # bounded retry for hot-plug changing the MULTI_SZ size
            size=C.c_uint32()
            self.check(self.dll.CM_Get_Device_Interface_List_SizeW(
                C.byref(size),C.byref(value),instance,0),'size active interfaces')
            if size.value<=1:return []
            text=C.create_unicode_buffer(size.value)
            result=self.dll.CM_Get_Device_Interface_ListW(
                C.byref(value),instance,text,len(text),0)
            if result==0x1A:continue  # CR_BUFFER_SMALL
            self.check(result,'read active interfaces')
            return [path for path in text[:].split('\0') if path]
        raise OSError('USB device interfaces kept changing during discovery')


def detect(*,timeout=3.0,cancel_event=None):
    """Enumerate present WinUSB CANables only; never starts CAN.

    Deadline/cancellation are checked between native queries. An OS API already
    in progress cannot be forcibly interrupted by this synchronous interface.
    """
    if os.name!='nt':raise OSError('CANable WinUSB discovery requires Windows')
    if not math.isfinite(timeout) or timeout<=0:raise ValueError('positive discovery timeout required')
    until=time.perf_counter()+timeout
    def check():
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError('cancelled during USB discovery')
        if time.perf_counter()>=until:raise TimeoutError('USB discovery deadline exceeded')
    check()
    manager=_ConfigurationManager()
    devices=[]
    for instance,guids in _instances():
        check()
        node=manager.locate(instance)
        if node is None:continue
        if not guids:raise OSError(f'CANable has no WinUSB interface GUID: {instance}')
        paths=[]
        for guid in guids:
            check()
            paths=manager.paths(instance,guid)
            if paths:break
        if not paths:continue  # unplugged/disabled interface, never return a stale path
        serial=manager.serial(node)
        check()
        devices.append({'serial_number':serial,'path':paths[0]})
    check()
    return sorted(devices,key=lambda item:(item['serial_number'],item['path']))
