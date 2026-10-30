// Offline source-level harness. main never opens USB or starts a worker.
#include "../native/engine.cpp"
#include <cassert>
#include <iostream>
#include <vector>

static std::array<unsigned char, 76> packet(unsigned id, unsigned flags = 6) {
    std::array<unsigned char, 76> p{};
    uint32_t echo = 0xffffffff;
    memcpy(p.data(), &echo, 4);
    memcpy(p.data() + 4, &id, 4);
    p[8] = 1;
    p[10] = static_cast<unsigned char>(flags);
    p[12] = static_cast<unsigned char>(id);
    return p;
}

int main() {
    Context c{};
    constexpr unsigned count = 100000;
    for (unsigned i = 0; i < count; ++i) {
        auto p = packet(i % 0x7ff);
        assert(consume(&c, p.data(), 1000 + i));
    }
    assert(c.frames.size() == count && c.stats.errors == 0);
    assert(c.stats.rx == count && c.stats.queue_high_water == count);
    std::vector<Frame> batch(1024);
    unsigned delivered = 0;
    while (unsigned n = native_recv_many(&c, batch.data(), static_cast<unsigned>(batch.size()))) {
        for (unsigned i = 0; i < n; ++i) {
            assert(batch[i].sequence == delivered + 1);
            assert(batch[i].timestamp_ns == 1000 + delivered);
            assert(batch[i].id == delivered % 0x7ff);
            ++delivered;
        }
    }
    assert(delivered == count && c.stats.delivered == count && c.frames.empty());

    auto error = packet(0x20000004);
    assert(consume(&c, error.data(), 7));
    Frame out{};
    assert(native_recv(&c, &out) && (out.flags & 0x400));
    assert(c.stats.can_errors == 1 && c.stats.errors == 0);

    Context fragmented{};
    Stream stream;
    auto p = packet(7);
    assert(accept(&fragmented, stream, p.data(), 9, 100));
    assert(fragmented.frames.empty());
    assert(accept(&fragmented, stream, p.data() + 9, p.size() - 9, 200));
    assert(native_recv(&fragmented, &out) && out.timestamp_ns == 200 && out.id == 7);
    finish_stream(&fragmented, stream);
    assert(fragmented.stats.errors == 0 && fragmented.stats.unparsed_bytes == 0);

    Context partial{};
    Stream tail;
    assert(accept(&partial, tail, p.data(), 5, 1));
    finish_stream(&partial, tail);
    assert(partial.stats.errors == 1 && partial.stats.unparsed_bytes == 5);

    Context invalid{};
    p[9] = 1;
    assert(!consume(&invalid, p.data(), 0));
    assert(invalid.frames.empty() && invalid.stats.errors == 1);
    assert(!native_recv(nullptr, &out));
    assert(native_recv_many(&c, nullptr, 10) == 0);
    assert(native_size(2) == 0 && native_version() == 3);
    std::cout << "PASS: 100000 queued frames, FIFO batches, metadata, CAN errors and split/tail validation\n";
}
