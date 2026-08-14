#pragma once
#include <string>
#include <string_view>

namespace mas {

// Which cleaning engine a run uses. The choice is explicit and never falls
// back: a benchmark number whose engine has to be guessed from context is the
// class of defect the 2026-08-13 review spent a day rooting out, so an engine
// that cannot honor the request refuses the run instead of substituting.
enum class Engine { Cpu, Cuda };

struct EngineChoice {
    Engine engine = Engine::Cpu;
    bool ok = false;
    std::string error;   // set when !ok; states the remedy, not just the fact
};

// The token the summary line carries after "engine " — the provenance the
// harnesses (and anyone reading a pasted log) see.
inline std::string_view engine_name(Engine e) {
    return e == Engine::Cuda ? "cuda" : "cpu";
}

// Parses the value of --engine=<value>.
inline EngineChoice parse_engine(std::string_view value) {
    if (value == "cpu") return {Engine::Cpu, true, {}};
    if (value == "cuda") return {Engine::Cuda, true, {}};
    return {Engine::Cpu, false,
            "unknown engine \"" + std::string(value) +
                "\" (valid: cpu, cuda)"};
}

// Decides whether this binary can run the requested engine.
// `compiled_with_cuda` is passed in (the caller mirrors its MAS_CUDA_ENABLED
// state) so the refusal policy stays testable from a build that has no CUDA.
inline EngineChoice resolve_engine(Engine requested, bool compiled_with_cuda) {
    if (requested == Engine::Cuda && !compiled_with_cuda)
        return {Engine::Cpu, false,
                "this binary was built without CUDA support; reconfigure with "
                "-DMAS_ENABLE_CUDA=ON and rebuild to use --engine=cuda"};
    return {requested, true, {}};
}

} // namespace mas
