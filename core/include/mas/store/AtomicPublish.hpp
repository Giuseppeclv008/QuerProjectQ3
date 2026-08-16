#pragma once
#include <atomic>
#include <filesystem>
#include <stdexcept>
#include <string>
#ifdef _WIN32
#include <process.h>
#else
#include <unistd.h>
#endif

namespace mas {

// Writing a Parquet file where a reader will look for it, without a window in
// which a partial one is visible under the real name.
//
// Both Parquet writers need this and only one had it. ParquetEventStore::close()
// grew write-to-temp-then-rename because a re-dispatched work item puts two
// processes on one output path; export_store_to_parquet kept COPYing straight to
// the destination, so an out-of-space export left 2.9 MB of truncated Parquet
// under the user's chosen name -- unreadable, fatal to any glob over that
// directory, and, because the overwrite guard then refuses a destination that
// exists, an obstacle to the retry that would have fixed it. A shared helper
// makes it structurally hard to have one and not the other.
//
// The temp name is private to this process AND to this call: the pid separates
// a tombstoned worker from its replacement, the counter separates two writers
// inside one process. It deliberately does not match the *.parquet glob readers
// use. It sits in the destination's own directory, so the rename is within one
// filesystem and therefore atomic.

inline std::string temp_sibling_of(const std::string& final_path) {
    static std::atomic<unsigned long long> next_token{0};
#ifdef _WIN32
    const int pid = _getpid();
#else
    const int pid = static_cast<int>(::getpid());
#endif
    return final_path + ".tmp." + std::to_string(pid) + "." +
           std::to_string(next_token.fetch_add(1));
}

// Calls write_to(tmp) -- which must produce the finished file at that path --
// then renames it onto final_path. Anything thrown by write_to propagates with
// the temp removed, so a failed publish leaves the destination untouched and no
// litter behind. Verification belongs inside write_to, against the temp: a file
// checked before the rename is a file that cannot be published wrong.
template <class F>
void publish_atomically(const std::string& final_path, F&& write_to) {
    const std::string tmp = temp_sibling_of(final_path);
    try {
        write_to(tmp);
        // What write_to left behind has to be a file before it is given the
        // destination's name. rename() does not care: handed a directory it
        // renames the directory, and `store/day.parquet` becomes a directory
        // that every reader then globs. DuckDB's COPY writes a directory rather
        // than a file when partitioning is on, so that is one option away from
        // reachable rather than hypothetical -- and a helper on both write paths
        // should have no way to publish something that is not a file. It also
        // turns "write_to produced nothing" from a rename ENOENT into a sentence
        // that says what went wrong.
        std::error_code st_ec;
        if (!std::filesystem::is_regular_file(tmp, st_ec))
            throw std::runtime_error(
                "refusing to publish " + final_path + ": nothing wrote a file at " +
                tmp + (st_ec ? " (" + st_ec.message() + ")" : ""));

        std::error_code ec;
        std::filesystem::rename(tmp, final_path, ec);
        if (ec)
            throw std::runtime_error("cannot rename " + tmp + " to " + final_path +
                                     ": " + ec.message());
    } catch (...) {
        // remove_all, not remove: the check above rejects a directory at tmp,
        // and this is what clears it. On a regular file the two are identical.
        std::error_code ignored;
        std::filesystem::remove_all(tmp, ignored);
        throw;
    }
}

} // namespace mas
