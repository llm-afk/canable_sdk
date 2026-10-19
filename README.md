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
- 默认启用 `low_latency=True`：接收等待最后 2 ms 持续轮询 USB 事件，发送完成等待前 2 ms 持续轮询，减少短 sleep 唤醒越过发送时刻的问题。可传 False 恢复原等待策略；低延迟模式会增加 CPU 占用，不保证硬实时，也不改变 `send()` 等待 USB 完成的含义。
- 默认启用 `high_priority=True`：将调用 `open()` 的线程提升至 Windows `THREAD_PRIORITY_HIGHEST`（原优先级更高时保留），同线程调用 `close()` 时恢复；其他线程和进程优先级不变。可传 False 关闭。即使 USB 关闭报错也会尝试恢复；启用失败会关闭刚打开的 USB 连接。该选项可减少普通线程竞争导致的延迟，不能消除驱动/中断/系统调度抖动。
- 扩展帧：`canable.send(0x123456, b'\x01', is_extended=True)`。
- `diagnostics()["info"]["usb_io"]` 可查看 USB 端点包长、读取队列、最大传输大小及当前 RAW_IO 状态。`raw_io=True` 可用于 WinUSB IN 管道对照试验；它要求读取缓冲区按端点包长对齐。2026-10-19 的 1 kHz 实机对比未显示明确收益，默认仍为 False。USB speed_code=1 按 WinUSB API 表示 full-speed-or-lower，不应直接解释成 low-speed。
- `recv()` 超时返回 `None`；错误帧通过 `frame.is_error` 判断。
- 单连接；打开、收发、关闭必须在同一线程执行。
- `send()` 返回代表 USB 传输完成，不代表 CAN ACK；`close()` 停止适配器，不会发送电机失能命令。

适用设备：单通道 Candlelight 2.5，USB 1D50:606F / MI_00，固件白名单
0x260618、0x260803、0x261001（实机验证版本为 0x261001）。不支持 SLCAN/串口模式。
保留异步 USB 发送、复用缓冲区及 8 笔 USB 接收队列优化；Windows/Python 不保证硬实时 1 kHz。

文件：`__init__.py` 为调用接口，`frame.py` 为帧结构，`transport.py` 为 USB/CAN 实现。
系统若有其他同名包，请确保导入的是项目旁的 `canable`（可查看 `canable.__file__`）。

## C++ 高频核心（Windows x64 原型）

`canable/native.py` 是 Python 接口，`canable/_native.dll` 是已编译核心；源码在 `native/engine.cpp`，使用 MinGW 的 `native/build.ps1` 重编译。原有 Python API 保留。

```python
from canable.native import NativeBus
import time

with NativeBus() as bus:
    bus.start_sdo_test(node=1, rate=1000)  # mc_tools 协议：只读电机序列号
    try:
        while True:
            time.sleep(1)
            s = bus.stats()
            print(s)
            if not s['running']:
                break
    except KeyboardInterrupt:
        pass
    bus.stop()
    print(bus.stats())
```

通用发送用 `bus.start_periodic(can_id, data, rate=1000)`；`bus.update(data)` 更新下一周期的载荷。原生线程持续发送最新值，不需要 Python 每毫秒调用。仅支持标准 ID、CAN FD+BRS，载荷长度为 0..8、12、16、20、24、32、48、64。通用模式用 `bus.recv()` 非阻塞读取，返回原有 CanFrame；接收队列为 256 帧，应高频排空，不能每秒才读取一次。SDO 测试模式在原生侧匹配回复，不占用该队列。

所有 Python 方法必须在创建对象的线程调用，并用 `with` 或 `finally: bus.close()` 保证回收。每个 NativeBus 只启动一次；stop 后如需重启，重新创建对象。关闭会停止发送、等待 SDO 回复（最多约 1 秒），取消并回收 USB 请求，然后释放设备。读取 `stats()['running']`、`errors`、`timeouts` 判断是否异常停止；队列溢出、USB/CAN 错误不会静默忽略。停止适配器不等于给电机发送失能指令。

默认原生线程最高普通线程优先级、持续忙轮询，约占用一个逻辑核心。桌面 `canable_native_demo.py` 额外默认提升自身进程至 HIGH，每秒打印统计，Ctrl+C 退出，`--seconds 10` 可做定长测试。

统计为本轮累计值，时间指标单位 ms，elapsed 单位秒。submitted 为 USB 提交数，tx 为 USB 完成数，echoes 为适配器回显，rx 在 SDO 模式为匹配的电机回复数；这些完成阶段不能混同。TXdt 测量主机提交间隔，P99 用 10 us 桶向上取整；并非总线实际发帧时间，也不保证 Windows 硬实时。USB 忙或调度延迟时跳过已过期时隙，skipped 显式计数，不无限堆积发送。
