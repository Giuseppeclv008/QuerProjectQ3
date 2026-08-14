#pragma once
#include <optional>
#include <string>
#include <string_view>

namespace mas {

// The rule every app's argv parser needs and two of them were missing: an
// argument that looks like a flag and was not consumed by the flag block is an
// error, never a positional.
//
// `clean day.csv out.csv --format parquet` used to run: "--format" became the
// machine_id, "parquet" was dropped, the output was CSV, and the exit code was
// 0. Every analytics tool scopes by machine_id, so the store then read as empty
// for the real machine -- a wrong answer that announces itself as a success.
// mas_export already rejects unconsumed flags; this puts clean and mas_worker
// on the same rule, and is a free function so it can be tested without a
// process.
//
// Scans [argi, argc). Returns the error to print, or nullopt when every
// remaining argument is a positional.
inline std::optional<std::string> unconsumed_flag(int argc, char** argv, int argi) {
    for (int i = argi; i < argc; ++i) {
        const std::string_view a(argv[i]);
        if (!a.starts_with("--")) continue;
        // The equals form is worth its own message: --engine=cpu on
        // mas_monolith teaches it, so reaching for --format=parquet here is a
        // reasonable mistake and "unexpected flag" would not explain it.
        if (a.starts_with("--format="))
            return "--format takes a separate value: --format " +
                   std::string(a.substr(9));
        return "unexpected flag " + std::string(a) +
               "; flags must come before the positional arguments";
    }
    return std::nullopt;
}

} // namespace mas
