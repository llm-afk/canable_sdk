# canable_sdk

Python 核心接口，Windows + WinUSB，Python 3.11+，仅依赖标准库。
将 `canable` 文件夹复制到你的 Python 主程序旁，即可直接使用，无需安装。

```python
import canable

canable.open(bitrate=1_000_000)
try:
    canable.send(0x123, b'\x01\x02')
    frame = canable.recv(timeout=0.1)
    if frame is not None:
        print(hex(frame.can_id), frame.data.hex(), frame.is_error)
finally:
    canable.close()
```

- `canable.detect()`：枚举设备；`canable.inspect()`：读取设备信息，均不启动 CAN。
- 多个设备时，使用 `canable.open(serial_number="设备序列号", ...)`。
- CAN FD：`canable.open(fd=True, bitrate=1_000_000, data_bitrate=4_000_000)`；发送默认启用 BRS，可传 `brs=False`。
- 扩展帧：`canable.send(0x123456, b'\x01', is_extended=True)`。
- `recv()` 超时返回 `None`；错误帧通过 `frame.is_error` 判断。
- 单连接；打开、收发、关闭必须在同一线程执行。
- `send()` 返回代表 USB 传输完成，不代表 CAN ACK；`close()` 停止适配器，不会发送电机失能命令。

适用设备：单通道 Candlelight 2.5，USB 1D50:606F / MI_00，固件白名单
0x260618、0x260803、0x261001（实机验证版本为 0x261001）。不支持 SLCAN/串口模式。
保留异步 USB 发送、复用缓冲区及 8 笔 USB 接收队列优化；Windows/Python 不保证硬实时 1 kHz。

文件：`__init__.py` 为调用接口，`frame.py` 为帧结构，`transport.py` 为 USB/CAN 实现。
系统若有其他同名包，请确保导入的是项目旁的 `canable`（可查看 `canable.__file__`）。