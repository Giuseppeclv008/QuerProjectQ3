// /usr/bin/time -l stand-in for Windows, for bench/run_bench.sh.
//
// Emits to stderr the same two lines parse_time() already reads from BSD time:
//
//         1.23 real         0.45 user         0.11 sys
//       123456  maximum resident set size
//
// RSS is PeakWorkingSetSize in bytes, matching macOS's byte-unit ru_maxrss, so
// the CSV's peak_rss_mb keeps one meaning across platforms. The child runs
// inside a kill-on-close job object: if this wrapper is killed (the sweep's
// EXIT trap does kill -9), the child dies with it instead of lingering as an
// orphan that would pollute the next run's worker-join gate.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <psapi.h>

#include <cstdio>
#include <cwchar>
#include <string>

// MSVCRT quoting rules: backslashes are literal unless they precede a quote.
static void append_quoted(std::wstring &cl, const std::wstring &arg) {
    if (!cl.empty()) cl += L' ';
    if (!arg.empty() && arg.find_first_of(L" \t\"") == std::wstring::npos) {
        cl += arg;
        return;
    }
    cl += L'"';
    size_t backslashes = 0;
    for (wchar_t c : arg) {
        if (c == L'\\') {
            ++backslashes;
        } else if (c == L'"') {
            cl.append(2 * backslashes + 1, L'\\');
            cl += L'"';
            backslashes = 0;
        } else {
            cl.append(backslashes, L'\\');
            cl += c;
            backslashes = 0;
        }
    }
    cl.append(2 * backslashes, L'\\');
    cl += L'"';
}

static double filetime_seconds(const FILETIME &ft) {
    ULARGE_INTEGER u;
    u.LowPart = ft.dwLowDateTime;
    u.HighPart = ft.dwHighDateTime;
    return static_cast<double>(u.QuadPart) / 1e7;  // 100 ns units
}

int wmain(int argc, wchar_t **argv) {
    if (argc < 2) {
        fwprintf(stderr, L"usage: win_time command [args...]\n");
        return 125;
    }

    // CreateProcessW appends .exe only for bare names: a name that carries a
    // path ("build-sweep/Release/mas_merge" is exactly how run_bench.sh calls
    // these) fails with ERROR_FILE_NOT_FOUND. Resolve it first; forward
    // slashes are fine for the API.
    wchar_t resolved[MAX_PATH];
    std::wstring app = argv[1];
    if (SearchPathW(nullptr, argv[1], L".exe", MAX_PATH, resolved, nullptr))
        app = resolved;

    std::wstring cl;
    append_quoted(cl, app);
    for (int i = 2; i < argc; ++i) append_quoted(cl, argv[i]);

    HANDLE job = CreateJobObjectW(nullptr, nullptr);
    if (job) {
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION lim = {};
        lim.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        SetInformationJobObject(job, JobObjectExtendedLimitInformation, &lim,
                                sizeof lim);
    }

    STARTUPINFOW si = {};
    si.cb = sizeof si;
    PROCESS_INFORMATION pi = {};

    LARGE_INTEGER freq, t0, t1;
    QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&t0);

    // CREATE_SUSPENDED so the job assignment covers the child's whole life.
    if (!CreateProcessW(app.c_str(), cl.data(), nullptr, nullptr, TRUE,
                        CREATE_SUSPENDED, nullptr, nullptr, &si, &pi)) {
        fwprintf(stderr, L"win_time: cannot launch (error %lu): %ls\n",
                 GetLastError(), cl.c_str());
        return 127;
    }
    if (job) AssignProcessToJobObject(job, pi.hProcess);  // best-effort
    ResumeThread(pi.hThread);

    WaitForSingleObject(pi.hProcess, INFINITE);
    QueryPerformanceCounter(&t1);

    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);

    FILETIME created, exited, kernel, user;
    double user_s = 0.0, sys_s = 0.0;
    if (GetProcessTimes(pi.hProcess, &created, &exited, &kernel, &user)) {
        user_s = filetime_seconds(user);
        sys_s = filetime_seconds(kernel);
    }

    PROCESS_MEMORY_COUNTERS pmc = {};
    pmc.cb = sizeof pmc;
    unsigned long long rss = 0;
    if (GetProcessMemoryInfo(pi.hProcess, &pmc, sizeof pmc))
        rss = static_cast<unsigned long long>(pmc.PeakWorkingSetSize);

    double real_s =
        static_cast<double>(t1.QuadPart - t0.QuadPart) / freq.QuadPart;

    fprintf(stderr, "        %.2f real         %.2f user         %.2f sys\n",
            real_s, user_s, sys_s);
    fprintf(stderr, "  %llu  maximum resident set size\n", rss);
    fflush(stderr);

    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    if (job) CloseHandle(job);
    return static_cast<int>(code);
}
