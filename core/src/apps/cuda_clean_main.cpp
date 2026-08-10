#include "../../cuda/CudaCleaner.hpp"
#include "mas/domain/CapEventExtractorFlat.hpp"
#include "mas/util/platform_metrics.hpp"
#include <algorithm>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <string>
#include <vector>

namespace {

// Bitwise on the doubles, not EXPECT_NEAR: a GPU parse that is one ulp off is a
// bug to find now, not a tolerance to widen (spec §10 R2).
bool sameEvent(const mas::CapEvent& a, const mas::CapEvent& b) {
    return a.head_id == b.head_id && a.ts == b.ts && a.cap_seq == b.cap_seq &&
           std::memcmp(&a.app_torque, &b.app_torque, sizeof(double)) == 0 &&
           std::memcmp(&a.status, &b.status, sizeof(double)) == 0 &&
           a.delta == b.delta && a.is_fault == b.is_fault &&
           a.aggregated == b.aggregated && a.reset == b.reset;
}

void dump(const char* which, const mas::CapEvent& e) {
    std::cerr << "    " << which << ": head=" << e.head_id << " ts=" << e.ts
              << " cap_seq=" << e.cap_seq << " torque=" << std::setprecision(17)
              << e.app_torque << " status=" << e.status << " delta=" << e.delta
              << " fault=" << e.is_fault << " agg=" << e.aggregated
              << " reset=" << e.reset << "\n";
}

// Spec §8: a failure on a machine the author cannot reach must be diagnosable
// from one paste. Ten events with every field is that artifact.
int reportMismatch(const std::vector<mas::CapEvent>& cpu,
                   const std::vector<mas::CapEvent>& gpu) {
    std::cerr << "VERIFY FAILED: cpu " << cpu.size() << " events, gpu "
              << gpu.size() << " events\n";
    int shown = 0;
    const std::size_t n = std::min(cpu.size(), gpu.size());
    for (std::size_t i = 0; i < n && shown < 10; ++i) {
        if (sameEvent(cpu[i], gpu[i])) continue;
        std::cerr << "  index " << i << ":\n";
        dump("cpu", cpu[i]);
        dump("gpu", gpu[i]);
        ++shown;
    }
    if (shown == 0) std::cerr << "  events agree; only the counts differ\n";
    return 1;
}

} // namespace

int main(int argc, char** argv) {
    mas::metrics_init();
    bool verify = false;
    int argi = 1;
    while (argi < argc && std::string(argv[argi]).rfind("--", 0) == 0) {
        if (std::string(argv[argi]) == "--verify") verify = true;
        else { std::cerr << "unknown flag " << argv[argi] << "\n"; return 2; }
        ++argi;
    }
    if (argi >= argc) {
        std::cerr << "usage: mas_cuda_clean [--verify] <day1.csv> [day2.csv ...]\n";
        return 2;
    }

    long long events = 0;
    mas::CudaStageTimes total;
    for (int i = argi; i < argc; ++i) {
        std::vector<mas::CapEvent> gpu;
        mas::CudaStageTimes t;
        std::string error;
        if (!mas::cuda_clean_file(argv[i], gpu, t, error)) {
            std::cerr << "error: " << error << "\n";
            return 1;
        }
        total.read_s += t.read_s;   total.h2d_s += t.h2d_s;
        total.index_s += t.index_s; total.parse_s += t.parse_s;
        total.delta_s += t.delta_s; total.compact_s += t.compact_s;
        total.d2h_s += t.d2h_s;
        events += static_cast<long long>(gpu.size());

        if (verify) {
            mas::RawColumns cols;
            std::string err;
            if (!mas::load_columns(argv[i], cols, err)) {
                std::cerr << "error: " << err << "\n";
                return 1;
            }
            std::vector<mas::CapEvent> cpu;
            mas::extract_flat(cols.ts, cols.count.data(), cols.torque.data(),
                              cols.status.data(), cols.n_rows, cpu);
            if (cpu.size() != gpu.size()) return reportMismatch(cpu, gpu);
            for (std::size_t k = 0; k < cpu.size(); ++k)
                if (!sameEvent(cpu[k], gpu[k])) return reportMismatch(cpu, gpu);
            std::cerr << "verify ok: " << argv[i] << " (" << cpu.size() << " events)\n";
        }
    }

    const double clean_s = total.read_s + total.h2d_s + total.index_s +
                           total.parse_s + total.delta_s + total.compact_s + total.d2h_s;
    std::cerr << "cuda_clean: " << (argc - argi) << " files, " << events
              << " events, clean " << std::fixed << std::setprecision(3)
              << clean_s << " s\n";
    std::cerr << "stages: read_s=" << total.read_s << " h2d_s=" << total.h2d_s
              << " index_s=" << total.index_s << " parse_s=" << total.parse_s
              << " delta_s=" << total.delta_s << " compact_s=" << total.compact_s
              << " d2h_s=" << total.d2h_s << "\n";
    std::cerr << mas::metrics_line("clean", mas::read_metrics()) << "\n";
    return 0;
}
