# canable_sdk

Windows WinUSB 的单通道 CANable 通信 SDK，Python 3.11+。默认 Python 路径提供通用 CAN 帧收发，可选 Windows x64 C++ 引擎负责原始 CAN FD 周期发送和接收。

支持已核对的 Candlelight 2.5：USB `1D50:606F / MI_00`，固件 `0x260618`、`0x260803`、`0x261001`。不支持 SLCAN；硬件名字叫 CANable 2.0 并不自动证明固件兼容。SDK 不包含电机节点、SDO、MIT、使能或故障恢复协议。

## 安装

运行时没有第三方 Python 依赖。复制 `canable/` 到主程序旁仍可使用，也支持在隔离环境中安装：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

发行 wheel 标记为 `py3-none-win_amd64`，包含可选 native DLL。源码、测试与构建入口保留在仓库，测试程序和日志不进入运行软件包。

## 基础接口

```python
import canable

info = canable.open(serial_number="your-serial-number", fd=True,
                    bitrate=1_000_000, data_bitrate=4_000_000)
try:
    canable.send(0x123, b"\x01\x02")
    frame = canable.recv(timeout=0.1)
    if frame is not None:
        print(frame.can_id, frame.data, frame.timestamp_ns, frame.sequence)
finally:
    canable.close()
```

| 函数 | 含义 |
| --- | --- |
| `detect()` | 仅枚举适配器，不启动 CAN |
| `open(serial_number=None, *, bitrate=1000000, fd=False, data_bitrate=4000000, sample_point=75.0, data_sample_point=64.0, low_latency=True, high_priority=True)` | 打开唯一连接并返回适配器信息 |
| `send(can_id, data, *, brs=None, is_extended=False)` | 发送一帧，返回表示 USB 写完成 |
| `recv(timeout=0.1)` | 取下一帧，超时返回 None |
| `close()` | 停止适配器并释放资源，可重复调用 |

基础函数的现有参数与返回语义保持不变；`canable.__version__` 为包版本。经典 CAN 默认 1 Mbps；FD 模式默认 BRS，可显式关闭。ID、长度、能力和固件不满足时明确失败，显式序列号不匹配不会回退到其他设备。

连接操作必须由调用 `open()` 的同一 Python 线程完成。多个电机可以共享这个通道，由上层唯一收发所有者按 CAN ID 和协议分发；不要让多个节点各自抢读同一个 `recv()`。当前模块级接口只管理一个适配器连接。

默认 Python 路径没有后台 Python 采集线程，需要所有者持续收帧。USB 层预提交 8 个 IN 请求并复用缓冲，上层应及时排空，把写 CSV 和耗时计算交给相应任务。

`low_latency=True` 使用短时忙轮询，`high_priority=True` 临时提升所有者线程优先级并在关闭时恢复，均可关闭。CPU、调度和最终时延需要实测，不承诺 Windows 硬实时。

## 接收数据与时间

返回的不可变 `CanFrame` 包含：

- `can_id`、`data`、`is_fd`、`brs`、`is_extended`、`is_error`、`is_remote`；
- `timestamp_ns`：主机观察到相关 USB 数据完成的单调时钟时间，单位 ns；
- `sequence`：当前连接或原生运行内，由 SDK 给交付的数据帧分配的连续序号。

同一 USB 块中的帧可能共享观察时间；跨块的帧使用收齐时的观察时间。这不是设备采样时间或 CAN 线上到达时间。USB 已完成但调用者稍后才取出的延迟，也不能由此消除。

应用构造的发送帧或直接调用纯 codec 时，接收元数据为 None。序号用于检查 SDK 内部交付与记录过程，不能证明设备到主机之前完全没有丢帧。

CAN 错误帧按 `is_error` 返回给上层；发送回显不冒充接收反馈。畸形 USB 帧抛出带原始证据的 `UsbFrameError`。硬件上报接收溢出会明确报错。

`send()` 的 USB 完成、适配器 TX 回显和远端协议回复是不同阶段；不自动重试结果不确定的写入。`close()` 也不发送电机失能命令。

## 可选 C++ 引擎

```python
from canable.native import NativeBus

with NativeBus(serial_number="your-serial-number") as bus:
    bus.start_periodic(0x123, b"\x01\x02", rate=1000, seconds=10)
    try:
        while bus.stats()["running"]:
            frames = bus.recv_many(1024)
            # 将 frames 交给自己的处理或记录链路。
    finally:
        bus.stop()
        while frames := bus.recv_many(1024):
            # 这里处理停止时收回的尾部帧。
            pass
    print(bus.stats())
```

该引擎的 TX 是一个标准 CAN ID 的 FD+BRS 周期任务，不是电机协议调度器。需要同总线多 ID 任意请求时，可使用基础接口的统一收发循环；未来协议层负责节点与请求匹配。

| 方法 | 含义 |
| --- | --- |
| `NativeBus(serial_number=None, bitrate=1000000, data_bitrate=4000000)` | 打开独占连接，适配器信息在 `info` |
| `start_periodic(can_id, data, rate=1000, seconds=0)` | 启动一次周期任务，0 表示运行到 stop |
| `update(data)` | 替换后续周期载荷 |
| `recv()` | 非阻塞读取一帧 |
| `recv_many(max_frames=1024)` | 非阻塞批量读取，单次 1..65536 帧 |
| `stats()` | 读取计数、队列深度与周期统计 |
| `stop()` | 停止原生 I/O，保留已收队列 |
| `close()` | 释放队列、USB 与资源，支持 with |

接收队列按需增长，不再因固定 256 帧阈值停止任务或覆盖旧帧。应持续批量排空并监控积压；这不代表无限内存或任意负载下的保证。内存分配失败、USB 或协议格式错误仍会明确报告为致命错误。CAN 错误报告保留为数据帧，不由通信层替电机协议作故障决策。

`stop()` 会在 USB 提交顺序中收回成功完成的 IN 请求，包括取消时恰好已经完成的数据。随后用 `recv()` 或 `recv_many()` 排空，再关闭连接。未完成的 USB 尾部字节通过 `unparsed_bytes` 和错误状态报告，不冒充完整数据；尚未由主机接收的设备数据不在此保证内。`close()` 会释放未读取的队列，因此记录链路必须先完成排空。

Python 方法仍属于创建对象的线程，原生引擎独占连接。每个对象只启动一次；忙轮询约占用一个逻辑核心。超期跳过旧时隙并计数，不积压补发。通用通信层不处理电机使能、失能、自动重启或协议重试。

### 统计

- `submitted/tx/pending`：USB 提交、完整写完成及未确认写。
- `rx/delivered/queued`：进入 SDK 队列、被取出及当前待取帧数。
- `queue_high_water`：运行内队列最高深度；`echoes`：适配器发送回显。
- `can_errors`：交付的 CAN 错误报告；`errors/win_error`：原生致命错误。
- `unparsed_bytes`：停止后尚未组成完整协议帧的字节数。
- `skipped`：错过的发送时隙。

时间统计为 ms，`elapsed` 为秒。`interval_*` 测量主机 USB 提交间隔，`usb_*` 测量提交到主机检测完成；都不是物理 CAN 发帧或远端响应延迟。median/p99 使用 10 us 桶向上取整。

## 离线验证与构建

```powershell
python -B -m unittest discover -s tests -v
.\test.ps1 -Native
```

默认测试不枚举或打开设备。native 测试包含 10 万帧队列/FIFO/批量元数据检查，以及用假 WinUSB 完成结果验证正常停止与尾部处理；不访问 USB，也不是实际总线吞吐测试。

设备发现使用 Windows PnP／CIM，运行环境必须允许访问该服务；SDK 不自动提升权限。PowerShell 输出与 Python 解码均显式使用 UTF-8，中文系统的发现失败保留原始错误，避免解码异常遮蔽权限／设备问题。2026-11-02 的 Python 离线检查为 26 项通过，包含两项此路径的回归；未改动原生 DLL／ABI。

`test.ps1 -Native` 需要 MinGW x64 的 `g++.exe`，先重建 DLL，再运行 Python 与原生离线检查。也可单独执行：

```powershell
.\native\build.ps1
```

当前 Python/DLL ABI 为 3，结构尺寸与版本在打开设备前检查。旧 DLL 与新 Python 不一致时必须一起重建，不能混用。

## 示例

`python demo.py --help` 不访问设备。`python demo.py` 只枚举。提供 `--id` 才会启动实际 FD+BRS 周期发送：

```powershell
python demo.py --serial your-serial-number --id 0x123 --data 01020304 --rate 1000 --seconds 10
```

按自己的总线协议选择 ID 和数据。示例只演示通信与统计，不实现电机协议，也不替代测试工程的公共记录器。

本仓库的离线检查、构建与实机通信是不同证据。2026-11-02 在使用方 motor_test 中以序号 20923378374350012、固件 0x261001 的适配器完成两个节点的 SDO 与 FD+BRS MIT 收发，具体配置、原始帧和电机行为在使用方逐次记录；没有据此验证通用硬件回环、原生高频吞吐或其他固件／平台。
