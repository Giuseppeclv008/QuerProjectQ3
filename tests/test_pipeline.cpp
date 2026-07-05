#include "mas/Pipeline.hpp"
#include <gtest/gtest.h>
#include <cstdio>
#include <fstream>
#include <sstream>
#include <string>

namespace {

void writeFile(const std::string& path, const std::string& body) {
    std::ofstream o(path);
    o << body;
}

// One raw row: head 1 gets head1_count; all other columns 0. Head 1 torque/status = 2.0.
std::string rawLine(const std::string& ts, long long head1_count) {
    std::string s = ts;
    for (int i = 0; i < 36; ++i) s += (i == 0) ? ("," + std::to_string(head1_count) + ".0") : ",0.0";
    for (int i = 0; i < 36; ++i) s += (i == 0) ? ",2.0" : ",0.0";
    for (int i = 0; i < 36; ++i) s += (i == 0) ? ",2.0" : ",0.0";
    return s;
}

size_t countDataLines(const std::string& path) {
    std::ifstream in(path);
    std::string line;
    size_t n = 0;
    bool first = true;
    while (std::getline(in, line)) {
        if (first) { first = false; continue; }   // skip header
        if (!line.empty()) ++n;
    }
    return n;
}

TEST(Pipeline, CleanFileEmitsOneEventPerIncrement) {
    const std::string in = "pipe_in.csv", out = "pipe_out.csv";
    std::string header = "timestamp";
    for (int i = 0; i < 108; ++i) header += ",c";

    std::ostringstream body;
    body << header << "\n";
    body << rawLine("t0", 100) << "\n";   // seed
    body << rawLine("t1", 100) << "\n";   // held
    body << rawLine("t2", 101) << "\n";   // +1
    body << rawLine("t3", 104) << "\n";   // +3 (aggregated)
    writeFile(in, body.str());

    const long long n = mas::clean_file(in, out, "MCCtest");
    EXPECT_EQ(n, 2);                       // two increment events
    EXPECT_EQ(countDataLines(out), 2u);

    std::remove(in.c_str());
    std::remove(out.c_str());
}

TEST(Pipeline, CleanFileReturnsMinusOneOnMissingInput) {
    const long long n = mas::clean_file("no_such_input_file.csv", "pipe_unused_out.csv", "MCC");
    EXPECT_EQ(n, -1);
    std::remove("pipe_unused_out.csv");
}

} // namespace
