#include "mas/CleaningWorker.hpp"
#include "mas/Message.hpp"
#include <chrono>
#include <utility>

namespace mas {

CleaningWorker::CleaningWorker(IMessageSource& work, IMessageSink& results,
                               IEventStore& store, CleanFn clean_fn)
    : work_(work), results_(results), store_(store),
      clean_fn_(std::move(clean_fn)) {}

int CleaningWorker::run() {
    int handled = 0;
    while (auto msg = work_.recv()) {
        if (is_stop(*msg)) break;
        const auto item = decode_work(*msg);
        if (!item) continue;   // malformed frame: drop it, keep serving
        const auto t0 = std::chrono::steady_clock::now();
        const long long events = clean_fn_(item->in_path, store_);
        const std::chrono::duration<double> dt =
            std::chrono::steady_clock::now() - t0;
        results_.send(encode(WorkResult{item->in_path, events, dt.count()}));
        ++handled;
    }
    return handled;
}

} // namespace mas
