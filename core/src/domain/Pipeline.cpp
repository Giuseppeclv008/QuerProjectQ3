#include "mas/domain/Pipeline.hpp"
#include "mas/domain/CapEventExtractor.hpp"
#include "mas/store/CsvEventStore.hpp"
#include "mas/store/CsvRawReader.hpp"
#include <vector>

namespace mas {

long long clean_file(const std::string& in_path, IEventStore& store,
                     CleanFileStats* stats) {
    CsvRawReader reader(in_path);
    if (!reader.is_open()) return -1;   // input missing/unreadable

    constexpr std::size_t kBatch = 8192;   // spec §5.3: batched insert
    CapEventExtractor ex;
    RawRow row;
    std::vector<CapEvent> batch;
    long long n = 0;
    while (reader.next(row)) {
        ex.process(row, batch);
        if (batch.size() >= kBatch) {
            store.write(batch);
            n += static_cast<long long>(batch.size());
            batch.clear();
        }
    }
    store.write(batch);                 // final partial batch (may be empty)
    n += static_cast<long long>(batch.size());
    if (stats) *stats = {reader.skipped(), reader.out_of_order()};
    return n;
}

long long clean_file(const std::string& in_path, const std::string& out_path,
                     const std::string& machine_id) {
    {   // probe first: a missing input must never create the output file
        CsvRawReader probe(in_path);
        if (!probe.is_open()) return -1;
    }
    try {
        CsvEventStore store(out_path, machine_id);
        const long long n = clean_file(in_path, store);
        if (n < 0) return n;
        store.close();
        return n;
    } catch (const std::exception&) {
        return -2;
    }
}

} // namespace mas
