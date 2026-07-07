#include "mas/CleaningWorker.hpp"
#include "mas/DuckDbEventStore.hpp"
#include "mas/Pipeline.hpp"
#include "mas/ZmqTransport.hpp"
#include <exception>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    if (argc < 4) {
        std::cerr << "usage: mas_worker <work_endpoint> <result_endpoint> <out.duckdb> [machine_id]\n";
        return 2;
    }
    const std::string work_ep = argv[1], result_ep = argv[2], out = argv[3];
    const std::string machine = (argc > 4) ? argv[4] : "MCC";
    try {
        zmq::context_t ctx(1);
        mas::ZmqPullSource work(ctx, work_ep, /*bind=*/false);
        mas::ZmqPushSink results(ctx, result_ep, /*bind=*/false);
        mas::DuckDbEventStore store(out, machine);
        mas::CleaningWorker worker(work, results, store,
            [](const std::string& path, mas::IEventStore& s) {
                return mas::clean_file(path, s);
            });
        const int handled = worker.run();
        std::cerr << "worker done: " << handled << " work items, store holds "
                  << store.count() << " rows\n";
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
    return 0;
}
