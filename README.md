# canable_sdk

Windows + WinUSB，Python 3.11+。支持已验证的单通道 Candlelight 2.5，USB 1D50:606F / MI_00，固件0x260618、0x260803、0x261001；不支持SLCAN。SDK只处理CAN帧和适配器，不含电机协议。

## 使用：复制目录，无需安装

把`canable`文件夹放在主程序旁，直接导入。SDK不依赖pip包。`native/`是可选高频引擎源码；Python接口使用附带的Windows x64 `canable/_native.dll`。

```python
import canable

canable.open(serial_number="20923378374350012", fd=True,
             bitrate=1_000_000, data_bitrate=4_000_000)
try:
    canable.send(0x123, b"\x01\x02")
    frame = canable.recv(timeout=0.1)
    if frame is not None:
        print(frame.can_id, frame.data, frame.is_error)
finally:
    canable.close()
```

- `detect()`枚举设备；`inspect(serial_number)`读取能力，两者不启动CAN。
- `open()`默认经典CAN 1Mbps；`fd=True`开启CAN FD，发送默认BRS，可传`brs=False`。
- `send(can_id, data, is_extended=False)`返回表示USB传输完成，不表示CAN ACK或远端执行。
- `recv(timeout)`返回下一帧或`None`；错误帧通过`frame.is_error`交给应用处理。
- `diagnostics()`提供适配器信息；`filter_reports=True`仅汇总厂商无错误类别的低计数报告。实际协议错误不容忍、不自动吞掉。默认False保留原始报告。
- `close()`停止适配器并释放USB，不发送电机失能命令。
- 单连接、单所有者线程：打开、收发、关闭须在同一线程执行。多设备时必须指定序列号。

`low_latency=True`使用短时忙轮询，`high_priority=True`提升调用线程并在关闭时恢复；均可关闭。保留异步发送、8个接收请求和缓冲复用。`raw_io=False`保持原默认策略；实验选项`raw_io=True`不保证提速。CPU占用与延迟需要结合实际运行环境评估，Windows不保证硬实时。

## 脚本入口

`python demo.py`只枚举设备。明确指定ID才开始发送，例如：

```powershell
python demo.py --serial 20923378374350012 --id 0x123 --data 01020304 --rate 1000 --seconds 10
```

这是实际CAN FD+BRS周期发送示例，须按自己的设备协议选择ID/数据；不包含电机使能、失能或协议回复匹配。Ctrl+C停止适配器。

## 可选：C++周期发送

```python
from canable.native import NativeBus

with NativeBus(serial_number="20923378374350012") as bus:
    bus.start_periodic(0x123, b"\x01\x02", rate=1000, seconds=10)
    while bus.stats()["running"]:
        while (frame := bus.recv()) is not None:
            pass  # 这里处理或记录所有收到的帧
    bus.stop()
    while (frame := bus.recv()) is not None:
        pass
    print(bus.stats())
```

- 原生线程按期限发送最新载荷；`update(data)`替换下一周期载荷。只支持标准ID、FD+BRS；合法长度0..8、12、16、20、24、32、48、64。
- `seconds=0`运行到显式`stop()`。超期时跳过旧时隙并计数，不堆积补发。约占用一个逻辑核心。
- `recv()`非阻塞；接收队列256帧，应用须及时排空。队列溢出、USB/CAN错误停止周期发送，`stats()`报告错误。没有电机故障判断或自动重试。
- 每个对象只启动一次。`stop()`等待原生I/O停止，`close()`释放USB。剩余已排入队列的帧可在stop后读取。`close()`不替代电机失能。
- Python方法须由创建对象的线程调用；原生引擎独占该连接，不能同时用模块级接口读写同一适配器。

统计：`submitted`为USB提交次数，`tx`为USB完整写完成数，`rx`为排队的CAN接收帧，`echoes`为适配器发送回显，`skipped`为跳过的时隙，`pending=submitted-tx`，`errors/win_error`为失败计数/原因。

时间单位ms，`elapsed`为秒：`interval_avg/min/median/p99/max/std`测量**主机USB提交间隔**；median/p99使用10us桶向上取整，min/max为直接测量。`usb_avg/max`测量主机提交至检测到USB完成的历时，不是物理CAN发帧时间或远端响应延迟。

## 编译与版本边界

附带DLL可直接用。修改C++源码后，使用MinGW x64运行`native/build.ps1`重建。Python/DLL ABI版本和结构尺寸不一致时，在打开设备前报错。

已删除`start_sdo_test()`、电机序列号/SDO匹配和相关专用统计；旧电机测试脚本应把协议逻辑移至上层。同步接口原有open/send/recv/close用法保留。

测试与原始记录放在SDK目录之外。当前1kHz实测结论见同级`canable_sdk_review_20261021/REPORT.md`，内部回环与实际电机通信分开解释。
