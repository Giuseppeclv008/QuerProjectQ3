#include "CudaCleaner.hpp"
#include "mas/domain/CapEventExtractorFlat.hpp"   // expected_header()
#include <cub/cub.cuh>
#include <cuda_runtime.h>
// CCCL 3.0 (CUDA 13) removed cub::CountingInputIterator; the unified library's
// replacement is thrust's counting_iterator, which CUB's device algorithms take.
#include <thrust/iterator/counting_iterator.h>
#include <chrono>
#include <climits>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <string>
#include <vector>

namespace mas {
namespace {

constexpr int TS_LEN = 24;      // "2026-02-21T16:00:00.000" is 23 chars + NUL

// Device-side event slot. 40 bytes: torque and status stay double so the
// differential test against CapEventExtractorFlat can compare bitwise rather
// than with a tolerance (spec §9 T3). is_fault and aggregated are derived on
// the host from status and delta -- no reason to spend device bandwidth on them.
struct CapEventDevice {
    long long cap_seq;
    double app_torque;
    double status;
    unsigned int row_index;
    int delta;
    short head_id;
    unsigned char reset;
    unsigned char pad[5];
};

#define CUDA_TRY(expr, what)                                              \
    do {                                                                  \
        const cudaError_t rc_ = (expr);                                   \
        if (rc_ != cudaSuccess) {                                         \
            error = std::string(what) + ": " + cudaGetErrorString(rc_);   \
            return false;                                                 \
        }                                                                 \
    } while (0)

__device__ __forceinline__ double pow10_(int e) {
    double r = 1.0;
    for (int i = 0; i < e; ++i) r *= 10.0;   // exact in double for e <= 22
    return r;
}

// Integer-mantissa parse: accumulate digits as an integer, divide once by an
// exact power of ten. Correctly rounded while the mantissa stays <= 2^53 (both
// operands of the division are then exact, and IEEE division rounds once).
// Stops at any non-digit, which is how a trailing '\r' is absorbed for free.
//
// The design spec (§4) recorded the pool as "plain decimal, <= 3 dp". The real
// pool violates that: ~2% of AppTorque cells carry the full 17-digit repr of a
// double (e.g. "2.0020000000000002"), whose mantissa does not fit in 53 bits --
// the cast rounds it, the division rounds again, and the double rounding landed
// one ulp under strtod on exactly the halfway cases (caught by --verify at
// event 25194 of 2026-02-01). Those cells set *inexact; the host re-parses the
// flagged rows' event cells with strtod, which is exact by construction.
__device__ __forceinline__ double parse_num(const char* p, const char* e,
                                            bool* inexact) {
    constexpr long long kExact = 1LL << 53;          // cast to double is exact
    constexpr long long kGuard = (0x7fffffffffffffffLL - 9) / 10;  // no overflow
    bool neg = false;
    if (p < e && (*p == '-' || *p == '+')) { neg = (*p == '-'); ++p; }
    long long mant = 0;
    int scale = 0;
    bool frac = false;
    for (; p < e; ++p) {
        const char c = *p;
        if (c == '.') { frac = true; continue; }
        if (c < '0' || c > '9') break;
        if (mant > kGuard) { *inexact = true; break; }
        mant = mant * 10 + (c - '0');
        if (frac) ++scale;
    }
    if (mant > kExact) *inexact = true;
    if (scale > 22) *inexact = true;   // pow10_ is exact only to 1e22; beyond
                                       // it the divisor itself is rounded and
                                       // the one-rounding argument fails
    double v = static_cast<double>(mant);
    if (scale > 0) v /= pow10_(scale);
    return neg ? -v : v;
}

// Also counts the newlines it flags. The count is what d_nl is sized from: a
// guessed capacity (say n_bytes/8 entries) is only large enough if lines average
// 64+ bytes, and a file of blank lines would overrun it.
__global__ void flag_newlines(const char* buf, size_t n, unsigned char* flags,
                              unsigned int* n_lines) {
    const size_t i = blockIdx.x * static_cast<size_t>(blockDim.x) + threadIdx.x;
    if (i >= n) return;
    const unsigned char f = (buf[i] == '\n') ? 1u : 0u;
    flags[i] = f;
    if (f) atomicAdd(n_lines, 1u);
}

// One thread per data row. Uncoalesced by construction -- each thread walks its
// own ~650 contiguous bytes -- but the whole file is 58 MB, so even a large
// efficiency loss lands in single-digit milliseconds. Spec §5.5.
// row_flags[r]: bit 0 = a torque/status cell was parse-inexact (host patches
// the affected events from the raw line); bit 1 = a Count cell was (fatal:
// event existence and cap_seq derive from counts, no after-the-fact repair);
// bit 2 = the row does not have the expected column count (fatal: the CPU
// readers skip such a row, but a missing field here would read as 0.0 and
// fabricate a reset plus an aggregated event against the neighbouring rows --
// the one failure mode worse than losing the row).
__global__ void parse_rows(const char* buf, const unsigned long long* nl,
                           unsigned int n_rows, double* count, double* torque,
                           double* status, char* ts_out, unsigned char* row_flags) {
    const unsigned int r = blockIdx.x * blockDim.x + threadIdx.x;
    if (r >= n_rows) return;

    // nl[k] is the offset of the k-th '\n'. nl[0] ends the header, so data row r
    // spans (nl[r], nl[r + 1]). A final row with no trailing newline is not
    // indexed and so is dropped; the pool's files all end with one, and
    // --verify is what would catch a file that does not.
    const char* p = buf + nl[r] + 1;
    const char* e = buf + nl[r + 1];
    while (e > p && (e[-1] == '\r' || e[-1] == '\n')) --e;

    // Column-count guard before any parsing: past the last comma, every
    // remaining field would parse from an empty range, and parse_num returns
    // 0.0 for an empty range without raising inexact.
    int cols = (p < e) ? 1 : 0;
    for (const char* s = p; s < e; ++s)
        if (*s == ',') ++cols;
    if (cols != 1 + 3 * NUM_HEADS) {
        for (int i = 0; i < TS_LEN; ++i)
            ts_out[static_cast<size_t>(r) * TS_LEN + i] = '\0';
        for (int h = 0; h < NUM_HEADS; ++h) {
            const size_t idx = static_cast<size_t>(r) * NUM_HEADS + h;
            count[idx] = 0.0; torque[idx] = 0.0; status[idx] = 0.0;
        }
        row_flags[r] = 4u;
        return;
    }

    const char* q = p;
    while (q < e && *q != ',') ++q;
    const int tslen = static_cast<int>(q - p);
    for (int i = 0; i < TS_LEN; ++i)
        ts_out[static_cast<size_t>(r) * TS_LEN + i] = (i < tslen && i < TS_LEN - 1) ? p[i] : '\0';
    p = (q < e) ? q + 1 : e;

    unsigned char fl = 0;
    for (int f = 0; f < 3 * NUM_HEADS; ++f) {
        q = p;
        while (q < e && *q != ',') ++q;
        bool inexact = false;
        const double v = parse_num(p, q, &inexact);
        if (inexact) fl |= (f < NUM_HEADS) ? 2u : 1u;
        const int h = f % NUM_HEADS;
        const size_t idx = static_cast<size_t>(r) * NUM_HEADS + h;
        if (f < NUM_HEADS)            count[idx] = v;
        else if (f < 2 * NUM_HEADS)   torque[idx] = v;
        else                          status[idx] = v;
        p = (q < e) ? q + 1 : e;
    }
    row_flags[r] = fl;
}

// One thread per (row, head) for rows 1..n_rows-1. Row 0 is the seed.
__global__ void delta_kernel(const double* count, const double* torque,
                             const double* status, unsigned int n_rows,
                             CapEventDevice* slots, unsigned char* flags) {
    const size_t t = blockIdx.x * static_cast<size_t>(blockDim.x) + threadIdx.x;
    const size_t total = static_cast<size_t>(n_rows - 1) * NUM_HEADS;
    if (t >= total) return;

    const unsigned int i = static_cast<unsigned int>(t / NUM_HEADS) + 1;
    const int h = static_cast<int>(t % NUM_HEADS);
    const size_t cur = static_cast<size_t>(i) * NUM_HEADS + h;
    const size_t prv = static_cast<size_t>(i - 1) * NUM_HEADS + h;

    const long long c_cur = llround(count[cur]);
    const long long c_prv = llround(count[prv]);
    if (c_cur == c_prv) { flags[t] = 0; return; }

    CapEventDevice ev;
    ev.cap_seq = c_cur;
    ev.app_torque = torque[cur];
    ev.status = status[cur];
    ev.row_index = i;
    ev.delta = (c_cur > c_prv) ? static_cast<int>(c_cur - c_prv) : 0;
    ev.head_id = static_cast<short>(h + 1);
    ev.reset = (c_cur < c_prv) ? 1u : 0u;
    slots[t] = ev;
    flags[t] = 1;
}

// Sticky-error timer: every cudaEvent call records its first failure instead
// of being ignored, so a stage cannot silently report 0.000 because an event
// failed to create or record. The caller checks ok() once, after the last
// stage -- garbage timings fail the file rather than entering the CSV.
struct Timer {
    cudaEvent_t a{}, b{};
    cudaError_t err = cudaSuccess;
    void note(cudaError_t e) { if (err == cudaSuccess && e != cudaSuccess) err = e; }
    Timer()  { note(cudaEventCreate(&a)); note(cudaEventCreate(&b)); }
    ~Timer() { cudaEventDestroy(a); cudaEventDestroy(b); }
    void start() { note(cudaEventRecord(a)); }
    double stop() {
        note(cudaEventRecord(b));
        note(cudaEventSynchronize(b));
        float ms = 0;
        note(cudaEventElapsedTime(&ms, a, b));
        return ms * 1e-3;
    }
    bool ok() const { return err == cudaSuccess; }
};

bool check_header(const std::string& path, std::string& error) {
    std::ifstream in(path, std::ios::binary);
    if (!in.is_open()) { error = "cannot open " + path; return false; }
    std::string line;
    if (!std::getline(in, line)) { error = path + " is empty"; return false; }
    if (!line.empty() && line.back() == '\r') line.pop_back();
    const auto want = expected_header();
    std::vector<std::string> got;
    std::string cur;
    for (char c : line) {
        if (c == ',') { got.push_back(cur); cur.clear(); }
        else cur.push_back(c);
    }
    got.push_back(cur);
    if (got.size() != want.size()) {
        error = path + ": header has " + std::to_string(got.size()) +
                " columns, expected " + std::to_string(want.size());
        return false;
    }
    for (size_t i = 0; i < want.size(); ++i)
        if (got[i] != want[i]) {
            error = path + ": column " + std::to_string(i) + " is '" + got[i] +
                    "', expected '" + want[i] + "'";
            return false;
        }
    return true;
}

} // namespace

bool cuda_clean_file(const std::string& path, std::vector<CapEvent>& out,
                     CudaStageTimes& times, std::string& error) {
    if (!check_header(path, error)) return false;

    // ---- S0: read into pinned host memory -----------------------------------
    // No mmap: it is POSIX-only and would need a CreateFileMapping branch for
    // Windows. At 58 MB a plain binary read costs the same (spec §5.5).
    std::ifstream in(path, std::ios::binary | std::ios::ate);
    if (!in.is_open()) { error = "cannot open " + path; return false; }
    const size_t n_bytes = static_cast<size_t>(in.tellg());
    in.seekg(0);

    // CUB's DeviceSelect takes num_items as int, so the compaction paths top
    // out at INT_MAX items. A day-file is ~58 MB; refuse loudly rather than
    // truncate silently if one ever is not.
    if (n_bytes > static_cast<size_t>(INT_MAX)) {
        error = path + " is " + std::to_string(n_bytes) +
                " bytes; the CUB compaction path (int num_items) caps a file "
                "at 2 GB -- split the file or lift the CUB calls to i64";
        return false;
    }

    char* h_buf = nullptr;
    // +1: NUL-terminated so the patch path below can strtod straight into the
    // buffer -- it stops at ',' or '\r'/'\n', and the terminator bounds a file
    // that ends mid-number.
    CUDA_TRY(cudaHostAlloc(&h_buf, n_bytes + 1, cudaHostAllocDefault), "cudaHostAlloc");
    Timer t;
    t.start();
    in.read(h_buf, static_cast<std::streamsize>(n_bytes));
    h_buf[n_bytes] = '\0';
    times.read_s = t.stop();
    if (!in || in.gcount() != static_cast<std::streamsize>(n_bytes)) {
        error = path + ": read " + std::to_string(in.gcount()) + " of " +
                std::to_string(n_bytes) + " bytes; refusing to parse the "
                "uninitialized remainder";
        cudaFreeHost(h_buf);
        return false;
    }

    char* d_buf = nullptr;
    unsigned char* d_flags = nullptr;
    unsigned long long* d_nl = nullptr;
    unsigned int* d_nl_count = nullptr;
    double *d_count = nullptr, *d_torque = nullptr, *d_status = nullptr;
    char* d_ts = nullptr;
    unsigned char* d_rowflags = nullptr;
    CapEventDevice *d_slots = nullptr, *d_dense = nullptr;
    unsigned char* d_evflags = nullptr;
    unsigned int* d_ev_count = nullptr;
    void* d_tmp = nullptr;
    auto cleanup = [&] {
        cudaFree(d_buf); cudaFree(d_flags); cudaFree(d_nl); cudaFree(d_nl_count);
        cudaFree(d_count); cudaFree(d_torque); cudaFree(d_status); cudaFree(d_ts);
        cudaFree(d_rowflags); cudaFree(d_slots); cudaFree(d_dense);
        cudaFree(d_evflags); cudaFree(d_ev_count); cudaFree(d_tmp);
        cudaFreeHost(h_buf);
    };
#define FAIL_IF(expr, what)                                               \
    do { const cudaError_t rc_ = (expr);                                  \
         if (rc_ != cudaSuccess) {                                        \
             error = std::string(what) + ": " + cudaGetErrorString(rc_);  \
             cleanup(); return false; } } while (0)

    // ---- S1: upload ---------------------------------------------------------
    FAIL_IF(cudaMalloc(&d_buf, n_bytes), "cudaMalloc raw");
    t.start();
    FAIL_IF(cudaMemcpy(d_buf, h_buf, n_bytes, cudaMemcpyHostToDevice), "H2D raw");
    times.h2d_s = t.stop();

    // ---- S2: newline index --------------------------------------------------
    FAIL_IF(cudaMalloc(&d_flags, n_bytes), "cudaMalloc flags");
    FAIL_IF(cudaMalloc(&d_nl_count, sizeof(unsigned int)), "cudaMalloc nl_count");
    FAIL_IF(cudaMemset(d_nl_count, 0, sizeof(unsigned int)), "memset nl_count");
    // Stage windows time compute and transfers only; every allocation sits
    // outside every window (spec §6.4 -- stages are only comparable if they
    // all account the same kinds of cost). index_s is therefore two segments
    // around the untimed cudaMallocs, whose sizes depend on the first
    // segment's n_lines.
    unsigned int n_lines = 0;
    t.start();
    {
        const int blk = 256;
        const size_t grid = (n_bytes + blk - 1) / blk;
        flag_newlines<<<static_cast<unsigned int>(grid), blk>>>(d_buf, n_bytes,
                                                                d_flags, d_nl_count);
    }
    times.index_s = t.stop();
    FAIL_IF(cudaGetLastError(), "S2 flag");
    FAIL_IF(cudaMemcpy(&n_lines, d_nl_count, sizeof(unsigned int),
                       cudaMemcpyDeviceToHost), "D2H nl_count");
    if (n_lines >= 2) {
        FAIL_IF(cudaMalloc(&d_nl, static_cast<size_t>(n_lines) * sizeof(unsigned long long)),
                "cudaMalloc nl");
        thrust::counting_iterator<unsigned long long> idx(0);
        size_t tmp_bytes = 0;
        cub::DeviceSelect::Flagged(nullptr, tmp_bytes, idx, d_flags, d_nl,
                                   d_nl_count, static_cast<int>(n_bytes));
        FAIL_IF(cudaMalloc(&d_tmp, tmp_bytes), "cudaMalloc cub tmp");
        t.start();
        cub::DeviceSelect::Flagged(d_tmp, tmp_bytes, idx, d_flags, d_nl,
                                   d_nl_count, static_cast<int>(n_bytes));
        times.index_s += t.stop();
    }
    FAIL_IF(cudaGetLastError(), "S2 index");

    // Header only, or a single data row (which is the seed and emits nothing).
    const unsigned int n_rows = (n_lines >= 2) ? n_lines - 1 : 0;
    if (n_rows < 2) { cleanup(); return true; }

    // ---- S3: parse ----------------------------------------------------------
    const size_t cells = static_cast<size_t>(n_rows) * NUM_HEADS;
    FAIL_IF(cudaMalloc(&d_count, cells * sizeof(double)), "cudaMalloc count");
    FAIL_IF(cudaMalloc(&d_torque, cells * sizeof(double)), "cudaMalloc torque");
    FAIL_IF(cudaMalloc(&d_status, cells * sizeof(double)), "cudaMalloc status");
    FAIL_IF(cudaMalloc(&d_ts, static_cast<size_t>(n_rows) * TS_LEN), "cudaMalloc ts");
    FAIL_IF(cudaMalloc(&d_rowflags, n_rows), "cudaMalloc rowflags");
    t.start();
    {
        const int blk = 128;
        const unsigned int grid = (n_rows + blk - 1) / blk;
        parse_rows<<<grid, blk>>>(d_buf, d_nl, n_rows, d_count, d_torque, d_status,
                                  d_ts, d_rowflags);
    }
    times.parse_s = t.stop();
    FAIL_IF(cudaGetLastError(), "S3 parse");

    // ---- S4: delta ----------------------------------------------------------
    const size_t slots = static_cast<size_t>(n_rows - 1) * NUM_HEADS;
    if (slots > static_cast<size_t>(INT_MAX)) {
        error = path + ": " + std::to_string(n_rows) + " rows give " +
                std::to_string(slots) + " event slots, past the CUB "
                "compaction path's int num_items -- split the file or lift "
                "the CUB calls to i64";
        cleanup();
        return false;
    }
    FAIL_IF(cudaMalloc(&d_slots, slots * sizeof(CapEventDevice)), "cudaMalloc slots");
    FAIL_IF(cudaMalloc(&d_evflags, slots), "cudaMalloc evflags");
    t.start();
    {
        const int blk = 256;
        const unsigned int grid = static_cast<unsigned int>((slots + blk - 1) / blk);
        delta_kernel<<<grid, blk>>>(d_count, d_torque, d_status, n_rows, d_slots, d_evflags);
    }
    times.delta_s = t.stop();
    FAIL_IF(cudaGetLastError(), "S4 delta");

    // ---- S5: compact --------------------------------------------------------
    FAIL_IF(cudaMalloc(&d_dense, slots * sizeof(CapEventDevice)), "cudaMalloc dense");
    FAIL_IF(cudaMalloc(&d_ev_count, sizeof(unsigned int)), "cudaMalloc ev_count");
    {
        void* tmp2 = nullptr;
        size_t tmp2_bytes = 0;
        cub::DeviceSelect::Flagged(nullptr, tmp2_bytes, d_slots, d_evflags, d_dense,
                                   d_ev_count, static_cast<int>(slots));
        FAIL_IF(cudaMalloc(&tmp2, tmp2_bytes), "cudaMalloc cub tmp2");
        t.start();
        cub::DeviceSelect::Flagged(tmp2, tmp2_bytes, d_slots, d_evflags, d_dense,
                                   d_ev_count, static_cast<int>(slots));
        times.compact_s = t.stop();
        cudaFree(tmp2);
    }
    FAIL_IF(cudaGetLastError(), "S5 compact");

    unsigned int n_events = 0;
    FAIL_IF(cudaMemcpy(&n_events, d_ev_count, sizeof(unsigned int),
                       cudaMemcpyDeviceToHost), "D2H ev_count");

    // ---- S6: download -------------------------------------------------------
    std::vector<CapEventDevice> host_ev(n_events);
    std::vector<char> host_ts(static_cast<size_t>(n_rows) * TS_LEN);
    std::vector<unsigned char> host_flags(n_rows);
    std::vector<unsigned long long> host_nl(n_lines);
    t.start();
    if (n_events)
        FAIL_IF(cudaMemcpy(host_ev.data(), d_dense, n_events * sizeof(CapEventDevice),
                           cudaMemcpyDeviceToHost), "D2H events");
    FAIL_IF(cudaMemcpy(host_ts.data(), d_ts, host_ts.size(),
                       cudaMemcpyDeviceToHost), "D2H ts");
    FAIL_IF(cudaMemcpy(host_flags.data(), d_rowflags, n_rows,
                       cudaMemcpyDeviceToHost), "D2H rowflags");
    FAIL_IF(cudaMemcpy(host_nl.data(), d_nl, n_lines * sizeof(unsigned long long),
                       cudaMemcpyDeviceToHost), "D2H nl");
    times.d2h_s = t.stop();

    for (unsigned int r = 0; r < n_rows; ++r) {
        if (host_flags[r] & 2u) {
            error = path + ": data row " + std::to_string(r + 1) +
                    " has a Count cell with a mantissa beyond 2^53; cap_seq and "
                    "event existence derive from counts, which the GPU parse "
                    "cannot round correctly and the host cannot repair after "
                    "the fact";
            cleanup();
            return false;
        }
        if (host_flags[r] & 4u) {
            error = path + ": data row " + std::to_string(r + 1) +
                    " does not have the expected 109 columns. The CPU readers "
                    "skip such a row; the GPU pipeline would instead read the "
                    "missing fields as 0.0 and fabricate a counter reset, so "
                    "it refuses the file";
            cleanup();
            return false;
        }
    }

    // ---- S7: host materialize -----------------------------------------------
    // Rows flagged parse-inexact get their event payloads re-read from the raw
    // line with strtod (exact for any digit count). Events arrive in (row asc,
    // head asc) order, so a one-row cache of field offsets amortizes the scan.
    // Timed with the host clock: this is CPU work, and cudaEvent timestamps
    // would measure the idle stream instead of the loop.
    const auto tm0 = std::chrono::steady_clock::now();
    out.reserve(out.size() + n_events);
    long long cached_row = -1;
    const char* fld[1 + 3 * NUM_HEADS];
    for (unsigned int k = 0; k < n_events; ++k) {
        const CapEventDevice& d = host_ev[k];
        CapEvent e;
        e.head_id = d.head_id;
        e.ts = std::string(&host_ts[static_cast<size_t>(d.row_index) * TS_LEN]);
        e.cap_seq = d.cap_seq;
        e.app_torque = d.app_torque;
        e.status = d.status;
        e.delta = d.delta;
        e.is_fault = is_reject(d.status);
        e.aggregated = d.delta > 1;
        e.reset = d.reset != 0;
        if (host_flags[d.row_index] & 1u) {
            if (cached_row != d.row_index) {
                const char* p = h_buf + host_nl[d.row_index] + 1;
                const char* end = h_buf + host_nl[d.row_index + 1];
                int f = 0;
                fld[f++] = p;
                for (; p < end && f < 1 + 3 * NUM_HEADS; ++p)
                    if (*p == ',') fld[f++] = p + 1;
                for (; f < 1 + 3 * NUM_HEADS; ++f) fld[f] = end;  // short row
                cached_row = d.row_index;
            }
            const int h0 = d.head_id - 1;
            e.app_torque = std::strtod(fld[1 + NUM_HEADS + h0], nullptr);
            e.status = std::strtod(fld[1 + 2 * NUM_HEADS + h0], nullptr);
            e.is_fault = is_reject(e.status);
        }
        out.push_back(e);
    }
    times.materialize_s =
        std::chrono::duration<double>(std::chrono::steady_clock::now() - tm0)
            .count();
    if (!t.ok()) {
        error = path + ": stage timing failed: " +
                cudaGetErrorString(t.err) +
                " -- the events are fine but the timings are garbage, and a "
                "benchmark run must not record them";
        cleanup();
        return false;
    }
#undef FAIL_IF
    cleanup();
    return true;
}

} // namespace mas
