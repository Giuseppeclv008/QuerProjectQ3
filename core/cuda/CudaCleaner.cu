#include "CudaCleaner.hpp"
#include "mas/domain/CapEventExtractorFlat.hpp"   // expected_header()
#include <cub/cub.cuh>
#include <cuda_runtime.h>
#include <cstdio>
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
// exact power of ten. Correctly rounded for the <=3 decimal places this pool
// uses, unlike repeated fused multiply-add. Stops at any non-digit, which is
// how a trailing '\r' is absorbed for free.
__device__ __forceinline__ double parse_num(const char* p, const char* e) {
    bool neg = false;
    if (p < e && (*p == '-' || *p == '+')) { neg = (*p == '-'); ++p; }
    long long mant = 0;
    int scale = 0;
    bool frac = false;
    for (; p < e; ++p) {
        const char c = *p;
        if (c == '.') { frac = true; continue; }
        if (c < '0' || c > '9') break;
        mant = mant * 10 + (c - '0');
        if (frac) ++scale;
    }
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
__global__ void parse_rows(const char* buf, const unsigned long long* nl,
                           unsigned int n_rows, double* count, double* torque,
                           double* status, char* ts_out) {
    const unsigned int r = blockIdx.x * blockDim.x + threadIdx.x;
    if (r >= n_rows) return;

    // nl[k] is the offset of the k-th '\n'. nl[0] ends the header, so data row r
    // spans (nl[r], nl[r + 1]). A final row with no trailing newline is not
    // indexed and so is dropped; the pool's files all end with one, and
    // --verify is what would catch a file that does not.
    const char* p = buf + nl[r] + 1;
    const char* e = buf + nl[r + 1];
    while (e > p && (e[-1] == '\r' || e[-1] == '\n')) --e;

    const char* q = p;
    while (q < e && *q != ',') ++q;
    const int tslen = static_cast<int>(q - p);
    for (int i = 0; i < TS_LEN; ++i)
        ts_out[static_cast<size_t>(r) * TS_LEN + i] = (i < tslen && i < TS_LEN - 1) ? p[i] : '\0';
    p = (q < e) ? q + 1 : e;

    for (int f = 0; f < 3 * NUM_HEADS; ++f) {
        q = p;
        while (q < e && *q != ',') ++q;
        const double v = parse_num(p, q);
        const int h = f % NUM_HEADS;
        const size_t idx = static_cast<size_t>(r) * NUM_HEADS + h;
        if (f < NUM_HEADS)            count[idx] = v;
        else if (f < 2 * NUM_HEADS)   torque[idx] = v;
        else                          status[idx] = v;
        p = (q < e) ? q + 1 : e;
    }
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

struct Timer {
    cudaEvent_t a{}, b{};
    Timer()  { cudaEventCreate(&a); cudaEventCreate(&b); }
    ~Timer() { cudaEventDestroy(a); cudaEventDestroy(b); }
    void start() { cudaEventRecord(a); }
    double stop() {
        cudaEventRecord(b);
        cudaEventSynchronize(b);
        float ms = 0;
        cudaEventElapsedTime(&ms, a, b);
        return ms * 1e-3;
    }
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

    char* h_buf = nullptr;
    CUDA_TRY(cudaHostAlloc(&h_buf, n_bytes, cudaHostAllocDefault), "cudaHostAlloc");
    Timer t;
    t.start();
    in.read(h_buf, static_cast<std::streamsize>(n_bytes));
    times.read_s = t.stop();

    char* d_buf = nullptr;
    unsigned char* d_flags = nullptr;
    unsigned long long* d_nl = nullptr;
    unsigned int* d_nl_count = nullptr;
    double *d_count = nullptr, *d_torque = nullptr, *d_status = nullptr;
    char* d_ts = nullptr;
    CapEventDevice *d_slots = nullptr, *d_dense = nullptr;
    unsigned char* d_evflags = nullptr;
    unsigned int* d_ev_count = nullptr;
    void* d_tmp = nullptr;
    auto cleanup = [&] {
        cudaFree(d_buf); cudaFree(d_flags); cudaFree(d_nl); cudaFree(d_nl_count);
        cudaFree(d_count); cudaFree(d_torque); cudaFree(d_status); cudaFree(d_ts);
        cudaFree(d_slots); cudaFree(d_dense); cudaFree(d_evflags);
        cudaFree(d_ev_count); cudaFree(d_tmp); cudaFreeHost(h_buf);
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
    unsigned int n_lines = 0;
    t.start();
    {
        const int blk = 256;
        const size_t grid = (n_bytes + blk - 1) / blk;
        flag_newlines<<<static_cast<unsigned int>(grid), blk>>>(d_buf, n_bytes,
                                                                d_flags, d_nl_count);
    }
    FAIL_IF(cudaGetLastError(), "S2 flag");
    FAIL_IF(cudaMemcpy(&n_lines, d_nl_count, sizeof(unsigned int),
                       cudaMemcpyDeviceToHost), "D2H nl_count");
    if (n_lines >= 2) {
        FAIL_IF(cudaMalloc(&d_nl, static_cast<size_t>(n_lines) * sizeof(unsigned long long)),
                "cudaMalloc nl");
        cub::CountingInputIterator<unsigned long long> idx(0);
        size_t tmp_bytes = 0;
        cub::DeviceSelect::Flagged(nullptr, tmp_bytes, idx, d_flags, d_nl,
                                   d_nl_count, static_cast<int>(n_bytes));
        FAIL_IF(cudaMalloc(&d_tmp, tmp_bytes), "cudaMalloc cub tmp");
        cub::DeviceSelect::Flagged(d_tmp, tmp_bytes, idx, d_flags, d_nl,
                                   d_nl_count, static_cast<int>(n_bytes));
    }
    times.index_s = t.stop();
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
    t.start();
    {
        const int blk = 128;
        const unsigned int grid = (n_rows + blk - 1) / blk;
        parse_rows<<<grid, blk>>>(d_buf, d_nl, n_rows, d_count, d_torque, d_status, d_ts);
    }
    times.parse_s = t.stop();
    FAIL_IF(cudaGetLastError(), "S3 parse");

    // ---- S4: delta ----------------------------------------------------------
    const size_t slots = static_cast<size_t>(n_rows - 1) * NUM_HEADS;
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
    t.start();
    {
        void* tmp2 = nullptr;
        size_t tmp2_bytes = 0;
        cub::DeviceSelect::Flagged(nullptr, tmp2_bytes, d_slots, d_evflags, d_dense,
                                   d_ev_count, static_cast<int>(slots));
        FAIL_IF(cudaMalloc(&tmp2, tmp2_bytes), "cudaMalloc cub tmp2");
        cub::DeviceSelect::Flagged(tmp2, tmp2_bytes, d_slots, d_evflags, d_dense,
                                   d_ev_count, static_cast<int>(slots));
        cudaFree(tmp2);
    }
    times.compact_s = t.stop();
    FAIL_IF(cudaGetLastError(), "S5 compact");

    unsigned int n_events = 0;
    FAIL_IF(cudaMemcpy(&n_events, d_ev_count, sizeof(unsigned int),
                       cudaMemcpyDeviceToHost), "D2H ev_count");

    // ---- S6: download -------------------------------------------------------
    std::vector<CapEventDevice> host_ev(n_events);
    std::vector<char> host_ts(static_cast<size_t>(n_rows) * TS_LEN);
    t.start();
    if (n_events)
        FAIL_IF(cudaMemcpy(host_ev.data(), d_dense, n_events * sizeof(CapEventDevice),
                           cudaMemcpyDeviceToHost), "D2H events");
    FAIL_IF(cudaMemcpy(host_ts.data(), d_ts, host_ts.size(),
                       cudaMemcpyDeviceToHost), "D2H ts");
    times.d2h_s = t.stop();

    // ---- host: materialize --------------------------------------------------
    out.reserve(out.size() + n_events);
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
        out.push_back(e);
    }
#undef FAIL_IF
    cleanup();
    return true;
}

} // namespace mas
