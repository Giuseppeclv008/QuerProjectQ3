#pragma once
#include "mas/domain/CapEvent.hpp"
#include "mas/store/EventStore.hpp"
#include <fstream>
#include <span>
#include <string>

namespace mas {

// Appends cap events to a CSV file. Header row is written on construction.
class CsvEventStore : public IEventStore {
public:
    // Throws std::runtime_error if out_path cannot be created.
    CsvEventStore(const std::string& out_path, const std::string& machine_id);
    void write(std::span<const CapEvent> events) override;
    void close();   // flush; throws std::runtime_error on write error (e.g. disk full)

private:
    std::ofstream out_;
    std::string machine_id_;
};

} // namespace mas
