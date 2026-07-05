#include "mas/Pipeline.hpp"
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    if (argc < 3) {
        std::cerr << "usage: clean <raw_in.csv> <events_out.csv> [machine_id]\n";
        return 2;
    }
    const std::string machine = (argc > 3) ? argv[3] : "MCC";
    const long long n = mas::clean_file(argv[1], argv[2], machine);
    if (n == -1) {
        std::cerr << "error: cannot open input file " << argv[1] << "\n";
        return 1;
    }
    if (n < 0) {
        std::cerr << "error: cannot write output file " << argv[2] << "\n";
        return 1;
    }
    std::cerr << "wrote " << n << " cap events\n";
    return 0;
}
