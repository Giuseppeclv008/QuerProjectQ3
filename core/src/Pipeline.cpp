#include "mas/Pipeline.hpp"
#include "mas/CapEventExtractor.hpp"
#include "mas/CsvRawReader.hpp"
#include <fstream>
#include <vector>

namespace mas {

long long clean_file(const std::string& in_path, const std::string& out_path,
                     const std::string& machine_id) {
    CsvRawReader reader(in_path);
    if (!reader.is_open()) return -1;   // input missing/unreadable
    std::ofstream out(out_path);
    out << "machine_id,head_id,ts,cap_seq,app_torque,status,delta,is_fault,aggregated,reset\n";

    CapEventExtractor ex;
    RawRow row;
    std::vector<CapEvent> buf;
    long long n = 0;
    while (reader.next(row)) {
        buf.clear();
        ex.process(row, buf);
        for (const auto& e : buf) {
            out << machine_id << ',' << e.head_id << ',' << e.ts << ','
                << e.cap_seq << ',' << e.app_torque << ',' << e.status << ','
                << e.delta << ',' << (e.is_fault ? 1 : 0) << ','
                << (e.aggregated ? 1 : 0) << ',' << (e.reset ? 1 : 0) << '\n';
            ++n;
        }
    }
    return n;
}

} // namespace mas
