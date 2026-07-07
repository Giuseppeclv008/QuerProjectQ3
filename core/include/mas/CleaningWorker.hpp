#pragma once
#include "mas/EventStore.hpp"
#include "mas/Transport.hpp"
#include <functional>
#include <string>

namespace mas {

// Cleaning agent loop (spec §5.3): PULL work items, clean each day-file into
// the injected store, PUSH one WorkResult per item. Exits on STOP or when the
// source reports no more messages. The clean function is injected so tests
// never touch the filesystem; production wires mas::clean_file.
class CleaningWorker {
public:
    using CleanFn = std::function<long long(const std::string&, IEventStore&)>;

    CleaningWorker(IMessageSource& work, IMessageSink& results,
                   IEventStore& store, CleanFn clean_fn);

    // Blocks; returns the number of work items handled (failures included —
    // their WorkResult carries events == -1).
    int run();

private:
    IMessageSource& work_;
    IMessageSink& results_;
    IEventStore& store_;
    CleanFn clean_fn_;
};

} // namespace mas
