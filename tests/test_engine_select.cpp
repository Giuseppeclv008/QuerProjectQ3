#include "mas/util/engine.hpp"
#include <gtest/gtest.h>
#include <string>

// The engine flag exists so that a number can always be traced to the engine
// that produced it. Two properties are load-bearing and both are asserted
// here: an unknown or unavailable engine is a hard error (never a silent
// CPU fallback), and the "unavailable" error tells the user how to get the
// engine, not merely that it is missing.

TEST(EngineSelect, ParsesCpu) {
    const auto c = mas::parse_engine("cpu");
    ASSERT_TRUE(c.ok) << c.error;
    EXPECT_EQ(c.engine, mas::Engine::Cpu);
}

TEST(EngineSelect, ParsesCuda) {
    const auto c = mas::parse_engine("cuda");
    ASSERT_TRUE(c.ok) << c.error;
    EXPECT_EQ(c.engine, mas::Engine::Cuda);
}

TEST(EngineSelect, RejectsUnknownValueNamingTheAlternatives) {
    const auto c = mas::parse_engine("gpu");
    ASSERT_FALSE(c.ok);
    // "gpu" is the guessable near-miss; the error must teach the real values.
    EXPECT_NE(c.error.find("cpu"), std::string::npos) << c.error;
    EXPECT_NE(c.error.find("cuda"), std::string::npos) << c.error;
}

TEST(EngineSelect, RejectsEmptyValueNamingTheAlternatives) {
    const auto c = mas::parse_engine("");
    ASSERT_FALSE(c.ok);
    EXPECT_NE(c.error.find("cpu"), std::string::npos) << c.error;
    EXPECT_NE(c.error.find("cuda"), std::string::npos) << c.error;
}

TEST(EngineSelect, CpuIsAlwaysAvailable) {
    EXPECT_TRUE(mas::resolve_engine(mas::Engine::Cpu, false).ok);
    EXPECT_TRUE(mas::resolve_engine(mas::Engine::Cpu, true).ok);
}

TEST(EngineSelect, CudaAvailableWhenCompiledIn) {
    const auto c = mas::resolve_engine(mas::Engine::Cuda, true);
    ASSERT_TRUE(c.ok) << c.error;
    EXPECT_EQ(c.engine, mas::Engine::Cuda);
}

TEST(EngineSelect, CudaWithoutSupportFailsWithTheRebuildRemedy) {
    const auto c = mas::resolve_engine(mas::Engine::Cuda, false);
    ASSERT_FALSE(c.ok);
    // The point of the message is the remedy: the exact configure flag that
    // produces a binary where the request would have worked.
    EXPECT_NE(c.error.find("-DMAS_ENABLE_CUDA=ON"), std::string::npos)
        << c.error;
}

TEST(EngineSelect, NamesMatchTheSummaryLineTokens) {
    // run_bench.sh and run_bench_cuda.py read the summary line; these are the
    // tokens they would see after "engine ".
    EXPECT_EQ(mas::engine_name(mas::Engine::Cpu), "cpu");
    EXPECT_EQ(mas::engine_name(mas::Engine::Cuda), "cuda");
}
