#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <mutex>
#include <new>
#include <thread>
#include <windows.h>
#include <winusb.h>

#define API extern "C" __declspec(dllexport)
struct Stats {
    uint64_t submitted, tx, rx, echoes, skipped, errors, pending, running, win_error;
    double elapsed, interval_avg, interval_min, interval_median, interval_p99, interval_max, interval_std,
        usb_avg, usb_max;
};
struct Frame {
    uint32_t id, size, flags;
    unsigned char data[64];
};
struct Metric {
    uint64_t count = 0;
    double sum = 0, squares = 0, minimum = 0, maximum = 0;
    std::array<uint64_t, 2001> hist{}; // 10 us buckets, final bucket overflow.
    void add(double seconds) {
        double ms = seconds * 1000;
        minimum = count ? std::min(minimum, ms) : ms;
        ++count;
        sum += ms;
        squares += ms * ms;
        maximum = std::max(maximum, ms);
        ++hist[std::min(2000, static_cast<int>(std::ceil(ms * 100)))];
    }
    double avg() const {
        return count ? sum / count : 0;
    }
    double deviation() const {
        return count ? std::sqrt(std::max(0., squares / count - avg() * avg())) : 0;
    }
    double percentile(unsigned percent) const {
        uint64_t target = (count * percent + 99) / 100, n = 0;
        if (!count)
            return 0;
        for (size_t i = 0; i < hist.size(); ++i) {
            n += hist[i];
            if (n >= target)
                return i == 2000 ? maximum : i / 100.;
        }
        return maximum;
    }
};
struct Slot {
    OVERLAPPED ov{};
    std::array<unsigned char, 1024> data{};
    bool pending = false;
};
struct Context {
    HANDLE file;
    WINUSB_INTERFACE_HANDLE usb;
    UCHAR in, out;
    uint32_t id, size;
    unsigned char payload[64]{};
    double rate, duration;
    std::atomic<bool> stop{false};
    std::thread worker;
    std::mutex mutex;
    Stats stats{};
    Metric intervals, writes;
    std::array<Frame, 256> frames{};
    unsigned head = 0, tail = 0, count = 0;
    double freq = 0, origin = 0;
    double clock() const {
        LARGE_INTEGER x;
        QueryPerformanceCounter(&x);
        return x.QuadPart / freq;
    }
    void fail(DWORD code) {
        std::lock_guard<std::mutex> lock(mutex);
        ++stats.errors;
        stats.win_error = code;
    }
};
static uint32_t u32(const unsigned char *p) {
    uint32_t v;
    memcpy(&v, p, 4);
    return v;
}
static int dlc(unsigned size) {
    static const unsigned sizes[] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64};
    for (int i = 0; i < 16; ++i)
        if (sizes[i] == size)
            return i;
    return -1;
}
// One complete legacy gs_usb frame; same bounded queue for all CAN IDs.
static bool consume(Context *c, const unsigned char *p) {
    bool fd = (p[10] & 2) != 0;
    if (p[9] != 0 || p[8] > (fd ? 15 : 8) || (p[10] & 1)) {
        c->fail(ERROR_INVALID_DATA);
        return false;
    }
    uint32_t id = u32(p + 4);
    static const unsigned sizes[] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64};
    unsigned payload = sizes[p[8]];
    if (u32(p) != 0xffffffff) {
        std::lock_guard<std::mutex> lock(c->mutex);
        ++c->stats.echoes;
    } else if (id & 0x20000000) {
        c->fail(ERROR_IO_DEVICE);
        return false;
    } else {
        std::lock_guard<std::mutex> lock(c->mutex);
        if (c->count == c->frames.size()) {
            ++c->stats.errors;
            c->stats.win_error = ERROR_BUFFER_OVERFLOW;
            return false;
        }
        Frame &f = c->frames[c->tail];
        f.id = id & 0x1fffffff;
        f.size = payload;
        f.flags = p[10] | ((id & 0x80000000) ? 0x100 : 0) | ((id & 0x40000000) ? 0x200 : 0);
        memcpy(f.data, p + 12, payload);
        c->tail = (c->tail + 1) % c->frames.size();
        ++c->count;
        ++c->stats.rx;
    }
    return true;
}
static bool issue(Context *c, Slot &s) {
    ResetEvent(s.ov.hEvent);
    HANDLE e = s.ov.hEvent;
    s.ov = {};
    s.ov.hEvent = e;
    BOOL ok =
        WinUsb_ReadPipe(c->usb, c->in, s.data.data(), static_cast<ULONG>(s.data.size()), nullptr, &s.ov);
    if (!ok && GetLastError() != ERROR_IO_PENDING) {
        c->fail(GetLastError());
        return false;
    }
    s.pending = true;
    return true;
}
static void run(Context *c) {
    LARGE_INTEGER freq;
    QueryPerformanceFrequency(&freq);
    {
        std::lock_guard<std::mutex> lock(c->mutex);
        c->freq = static_cast<double>(freq.QuadPart);
        c->origin = c->clock();
    }
    std::array<Slot, 8> reads{};
    unsigned readhead = 0;
    OVERLAPPED txov{};
    bool txpending = false;
    std::array<unsigned char, 76> tx{};
    std::array<unsigned char, 8192> stream{};
    size_t used = 0;
    unsigned echo = 1;
    double due = c->origin, previous = 0, txstart = 0, period = 1 / c->rate;
    bool good = true;
    {
        std::lock_guard<std::mutex> lock(c->mutex);
        c->stats.running = 1;
    }
    if (!SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_HIGHEST)) {
        c->fail(GetLastError());
        good = false;
    }
    ULONG timeout = 100;
    if (good && !WinUsb_SetPipePolicy(c->usb, c->out, PIPE_TRANSFER_TIMEOUT, sizeof(timeout), &timeout)) {
        c->fail(GetLastError());
        good = false;
    }
    txov.hEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!txov.hEvent) {
        c->fail(GetLastError());
        good = false;
    }
    for (auto &s : reads) {
        if (!good)
            break;
        s.ov.hEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        if (!s.ov.hEvent) {
            c->fail(GetLastError());
            good = false;
            break;
        }
        good = issue(c, s);
    }
    while (good) {
        double now = c->clock();
        bool sending =
            !c->stop.load(std::memory_order_relaxed) && (c->duration == 0 || now - c->origin < c->duration);
        if (txpending) {
            DWORD state = WaitForSingleObject(txov.hEvent, 0);
            if (state == WAIT_OBJECT_0) {
                ULONG n = 0;
                BOOL ok = WinUsb_GetOverlappedResult(c->usb, &txov, &n, FALSE);
                txpending = false;
                if (!ok || n != 76) {
                    c->fail(ok ? ERROR_WRITE_FAULT : GetLastError());
                    break;
                }
                std::lock_guard<std::mutex> lock(c->mutex);
                ++c->stats.tx;
                c->writes.add(now - txstart);
            } else if (state != WAIT_TIMEOUT) {
                c->fail(GetLastError());
                break;
            } else if (now - txstart > .1) {
                c->fail(ERROR_TIMEOUT);
                break;
            }
        }
        if (!sending && !txpending)
            break;
        // Submission deadlines are owned entirely by this native thread.
        if (sending && now >= due && !txpending) {
            auto missed = static_cast<uint64_t>((now - due) / period);
            due += missed * period;
            {
                std::lock_guard<std::mutex> lock(c->mutex);
                c->stats.skipped += missed;
                tx.fill(0);
                memcpy(tx.data(), &echo, 4);
                memcpy(tx.data() + 4, &c->id, 4);
                tx[8] = static_cast<unsigned char>(dlc(c->size));
                tx[10] = 6;
                memcpy(tx.data() + 12, c->payload, c->size);
            }
            {
                ResetEvent(txov.hEvent);
                HANDLE event = txov.hEvent;
                txov = {};
                txov.hEvent = event;
                txstart = c->clock();
                BOOL ok = WinUsb_WritePipe(c->usb, c->out, tx.data(), 76, nullptr, &txov);
                if (!ok && GetLastError() != ERROR_IO_PENDING) {
                    c->fail(GetLastError());
                    break;
                }
                txpending = true;
                {
                    std::lock_guard<std::mutex> lock(c->mutex);
                    ++c->stats.submitted;
                    if (previous)
                        c->intervals.add(txstart - previous);
                }
                previous = txstart;
                echo = echo % 255 + 1;
            }
            due += period;
        }
        Slot &s = reads[readhead];
        DWORD state = WaitForSingleObject(s.ov.hEvent, 0);
        if (state == WAIT_OBJECT_0) {
            ULONG n = 0;
            BOOL ok = WinUsb_GetOverlappedResult(c->usb, &s.ov, &n, FALSE);
            s.pending = false;
            if (!ok) {
                c->fail(GetLastError());
                break;
            }
            if (n > s.data.size() || used + n > stream.size()) {
                c->fail(ERROR_BUFFER_OVERFLOW);
                break;
            }
            memcpy(stream.data() + used, s.data.data(), n);
            used += n;
            if (!issue(c, s))
                break;
            readhead = (readhead + 1) % 8;
            size_t offset = 0;
            while (used - offset >= 12) {
                const unsigned char *p = stream.data() + offset;
                bool fd = p[10] & 2;
                unsigned length = fd ? 76 : 20;
                if (used - offset < length)
                    break;
                if (!consume(c, p)) {
                    good = false;
                    break;
                }
                offset += length;
            }
            if (offset) {
                memmove(stream.data(), stream.data() + offset, used - offset);
                used -= offset;
            }
        } else if (state != WAIT_TIMEOUT) {
            c->fail(GetLastError());
            break;
        }
        YieldProcessor();
    }
    // Cancel and JOIN all I/O before buffers or Python-owned handles can die.
    if (txpending) {
        CancelIoEx(c->file, &txov);
        ULONG n = 0;
        WinUsb_GetOverlappedResult(c->usb, &txov, &n, TRUE);
    }
    for (auto &s : reads)
        if (s.pending)
            CancelIoEx(c->file, &s.ov);
    for (auto &s : reads) {
        if (s.pending) {
            ULONG n = 0;
            WinUsb_GetOverlappedResult(c->usb, &s.ov, &n, TRUE);
        }
        if (s.ov.hEvent)
            CloseHandle(s.ov.hEvent);
    }
    if (txov.hEvent)
        CloseHandle(txov.hEvent);
    {
        std::lock_guard<std::mutex> lock(c->mutex);
        c->stats.elapsed = c->clock() - c->origin;
        c->stats.pending = c->stats.submitted - c->stats.tx;
        c->stats.running = 0;
    }
}
API unsigned native_version() {
    return 2;
}
API unsigned native_size(unsigned i) {
    return i == 0 ? sizeof(Stats) : sizeof(Frame);
}
API void *native_start(void *file, void *usb, unsigned input, unsigned output, unsigned id,
                       const unsigned char *data, unsigned size, double rate, double duration) {
    if (!file || !usb || id > 0x7ff || dlc(size) < 0 || (!data && size) || !std::isfinite(rate) ||
        rate <= 0 || rate > 10000 || !std::isfinite(duration) || duration < 0)
        return nullptr;
    Context *c = nullptr;
    try {
        c = new Context;
        c->file = file;
        c->usb = usb;
        c->in = static_cast<UCHAR>(input);
        c->out = static_cast<UCHAR>(output);
        c->id = id;
        c->size = size;
        if (size)
            memcpy(c->payload, data, size);
        c->rate = rate;
        c->duration = duration;
        c->stats.running = 1;
        c->worker = std::thread(run, c);
        return c;
    } catch (...) {
        delete c;
        return nullptr;
    }
}
API void native_stop(void *ptr) {
    auto *c = static_cast<Context *>(ptr);
    if (c) {
        c->stop = true;
        if (c->worker.joinable())
            c->worker.join();
    }
}
API void native_free(void *ptr) {
    auto *c = static_cast<Context *>(ptr);
    if (c) {
        native_stop(c);
        delete c;
    }
}
API int native_update(void *ptr, const unsigned char *data, unsigned size) {
    auto *c = static_cast<Context *>(ptr);
    if (!c || dlc(size) < 0 || (!data && size))
        return 0;
    std::lock_guard<std::mutex> lock(c->mutex);
    if (!c->stats.running)
        return 0;
    c->size = size;
    if (size)
        memcpy(c->payload, data, size);
    return 1;
}
API int native_recv(void *ptr, Frame *out) {
    auto *c = static_cast<Context *>(ptr);
    if (!c || !out)
        return 0;
    std::lock_guard<std::mutex> lock(c->mutex);
    if (!c->count)
        return 0;
    *out = c->frames[c->head];
    c->head = (c->head + 1) % c->frames.size();
    --c->count;
    return 1;
}
API int native_stats(void *ptr, Stats *out) {
    auto *c = static_cast<Context *>(ptr);
    if (!c || !out)
        return 0;
    std::lock_guard<std::mutex> lock(c->mutex);
    *out = c->stats;
    if (out->running && c->origin)
        out->elapsed = c->clock() - c->origin;
    out->interval_avg = c->intervals.avg();
    out->interval_min = c->intervals.minimum;
    out->interval_median = c->intervals.percentile(50);
    out->interval_p99 = c->intervals.percentile(99);
    out->interval_max = c->intervals.maximum;
    out->interval_std = c->intervals.deviation();
    out->usb_avg = c->writes.avg();
    out->usb_max = c->writes.maximum;
    if (out->running)
        out->pending = out->submitted - out->tx;
    return 1;
}
