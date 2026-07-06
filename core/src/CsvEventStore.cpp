#include "mas/CsvEventStore.hpp"
#include <stdexcept>

namespace mas {

CsvEventStore::CsvEventStore(const std::string& out_path, const std::string& machine_id)
    : out_(out_path), machine_id_(machine_id) {
    if (!out_.is_open())
        throw std::runtime_error("cannot create output file " + out_path);
    out_ << "machine_id,head_id,ts,cap_seq,app_torque,status,delta,is_fault,aggregated,reset\n";
}

void CsvEventStore::write(std::span<const CapEvent> events) {
    for (const auto& e : events) {
        out_ << machine_id_ << ',' << e.head_id << ',' << e.ts << ','
             << e.cap_seq << ',' << e.app_torque << ',' << e.status << ','
             << e.delta << ',' << (e.is_fault ? 1 : 0) << ','
             << (e.aggregated ? 1 : 0) << ',' << (e.reset ? 1 : 0) << '\n';
    }
}

void CsvEventStore::close() {
    out_.flush();
    if (out_.fail()) throw std::runtime_error("write error on output CSV");
}

} // namespace mas
