#include "mas/Coordinator.hpp"

namespace mas {

DispatchSummary run_coordinator(const std::vector<WorkItem>& items,
                                IMessageSink& work, IMessageSource& results,
                                int num_workers) {
    for (const auto& item : items) work.send(encode(item));

    DispatchSummary s;
    std::size_t received = 0;
    while (received < items.size()) {
        const auto msg = results.recv();
        if (!msg) break;   // sink went silent: give up on the stragglers
        ++received;
        const auto r = decode_result(*msg);
        if (r && r->events >= 0) {
            ++s.files_ok;
            s.total_events += r->events;
        } else {
            ++s.files_failed;
        }
    }
    s.files_failed += static_cast<int>(items.size() - received);

    for (int i = 0; i < num_workers; ++i) work.send(make_stop());
    return s;
}

} // namespace mas
