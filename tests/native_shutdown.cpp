// Stop/cancel race regression with mocked WinUSB only. No device is opened.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <winusb.h>
#include <array>
#include <cassert>
#include <cstring>
#include <iostream>
#include <vector>

struct Result {
    OVERLAPPED *ov;
    ULONG bytes;
    bool complete;
};
static std::vector<Result> results;
static bool partial = false;
static unsigned writes = 0;

static BOOL WINAPI fake_read(WINUSB_INTERFACE_HANDLE, UCHAR, PUCHAR buffer, ULONG,
                             PULONG, LPOVERLAPPED ov) {
    unsigned i = static_cast<unsigned>(results.size());
    std::array<unsigned char, 76> p{};
    unsigned echo = 0xffffffff, id = i + 1;
    memcpy(p.data(), &echo, 4);
    memcpy(p.data() + 4, &id, 4);
    p[8] = 1;
    p[10] = 6;
    p[12] = static_cast<unsigned char>(id);
    ULONG size = partial ? 5 : 76;
    memcpy(buffer, p.data(), size);
    results.push_back({ov, size, partial ? i == 0 : i < 3});
    SetLastError(ERROR_IO_PENDING);
    return FALSE;
}
static BOOL WINAPI fake_result(WINUSB_INTERFACE_HANDLE, LPOVERLAPPED ov, PULONG n, BOOL) {
    for (const auto &result : results) {
        if (result.ov == ov) {
            if (result.complete) {
                *n = result.bytes;
                return TRUE;
            }
            break;
        }
    }
    *n = 0;
    SetLastError(ERROR_OPERATION_ABORTED);
    return FALSE;
}
static BOOL WINAPI fake_write(WINUSB_INTERFACE_HANDLE, UCHAR, PUCHAR, ULONG, PULONG, LPOVERLAPPED) {
    ++writes;
    SetLastError(ERROR_ACCESS_DENIED);
    return FALSE;
}
static BOOL WINAPI fake_policy(WINUSB_INTERFACE_HANDLE, UCHAR, ULONG, ULONG, PVOID) { return TRUE; }
static BOOL WINAPI fake_cancel(HANDLE, LPOVERLAPPED) { return TRUE; }
static BOOL WINAPI fake_priority(HANDLE, int) { return TRUE; }

#define WinUsb_ReadPipe fake_read
#define WinUsb_WritePipe fake_write
#define WinUsb_GetOverlappedResult fake_result
#define WinUsb_SetPipePolicy fake_policy
#define CancelIoEx fake_cancel
#define SetThreadPriority fake_priority
#include "../native/engine.cpp"

static void stopped_run(Context &c) {
    c.file = reinterpret_cast<HANDLE>(1);
    c.usb = reinterpret_cast<WINUSB_INTERFACE_HANDLE>(2);
    c.in = 0x81;
    c.out = 2;
    c.rate = 1000;
    c.duration = 0;
    c.stop = true;
    run(&c);
}

int main() {
    Context c{};
    stopped_run(c);
    assert(results.size() == 8 && writes == 0);
    assert(c.stats.rx == 3 && c.stats.errors == 0 && c.stats.running == 0);
    Frame frames[3]{};
    assert(native_recv_many(&c, frames, 3) == 3);
    for (unsigned i = 0; i < 3; ++i) {
        assert(frames[i].id == i + 1 && frames[i].sequence == i + 1);
        assert(frames[i].timestamp_ns > 0);
    }

    results.clear();
    partial = true;
    Context truncated{};
    stopped_run(truncated);
    assert(truncated.stats.rx == 0 && truncated.stats.errors == 1);
    assert(truncated.stats.unparsed_bytes == 5 && writes == 0);
    std::cout << "PASS: stop preserves completed reads, cancellation ordering and partial-tail reporting\n";
}
