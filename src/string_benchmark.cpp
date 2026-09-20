#include <filesystem>
#include <functional>
#include "benchmark.h"
#include "competitors/art.h"
#include "std_map.h"
#include "checked_index.h"
#include "observed_index.h"
#include "utils/cxxopts.hpp"

template <class Index>
void RunIndex(tli::Benchmark<std::string>& bench, bool verify) {
  if (verify) bench.Run<CheckedIndex<Index>>();
  else bench.Run<ObservedIndex<Index>>();
}

int main(int argc, char** argv) {
  try {
    cxxopts::Options options("string_benchmark", "Single-thread TLI string benchmark setup");
    options.positional_help("<data/*_string> <data/*_ops_...>");
    options.add_options()
      ("data", "TLI binary string dataset", cxxopts::value<std::string>())
      ("ops", "TLI generated workload", cxxopts::value<std::string>())
      ("only", "ART or StdMap", cxxopts::value<std::string>()->default_value("ART"))
      ("through", "Measure throughput (run --verify separately first)")
      ("verify", "Validate lookups, range sums, insert read-back and final keys")
      ("csv", "Append upstream-format CSV in ./results/")
      ("r,repeats", "Throughput repetitions", cxxopts::value<int>()->default_value("1"))
      ("help", "Show usage");
    options.parse_positional({"data", "ops"});
    const auto args = options.parse(argc, argv);
    if (args.count("help")) {
      std::cout << options.help() << '\n';
      return 0;
    }
    if (!args.count("data") || !args.count("ops"))
      throw std::runtime_error("Both data and ops files are required; see --help.");
    if (args.count("through") && args.count("verify"))
      throw std::runtime_error("TLI throughput mode does not verify. Run --verify, then --through separately.");
    const auto data = args["data"].as<std::string>();
    const auto ops = args["ops"].as<std::string>();
    const auto only = args["only"].as<std::string>();
    const int repeats = args["repeats"].as<int>();
    if (repeats < 1 || repeats > 100)
      throw std::runtime_error("repeats must be in [1, 100].");
    if (only != "ART" && only != "StdMap")
      throw std::runtime_error("Unknown index. Available: ART, StdMap.");
    if (!std::filesystem::is_regular_file(data) || !std::filesystem::is_regular_file(ops))
      throw std::runtime_error("Data or workload file is missing.");
    if (util::resolve_type(data) != DataType::STRING)
      throw std::runtime_error("This entrypoint supports string keys only.");
    // TLI infers workload properties and CSV names from this filename convention.
    if (ops.rfind("data/", 0) != 0 || ops.find('/', 5) != std::string::npos ||
        !std::regex_search(ops, std::regex("[0-9]+\\.[0-9]+rq_")) ||
        !std::regex_search(ops, std::regex("[0-9]+\\.[0-9]+i")))
      throw std::runtime_error("Use an upstream generate output directly under ./data/; run from its parent.");
    if (args.count("csv")) std::filesystem::create_directories("results");
    tli::Benchmark<std::string> bench(data, ops,
      args.count("through") ? repeats : 1, args.count("through"), false, false,
      false, false, args.count("csv"), 1, args.count("verify"));
    const bool verify = args.count("verify");
    if (only == "ART") RunIndex<tli_art::ART<std::string>>(bench, verify);
    else RunIndex<StdMap>(bench, verify);
    return tli::run_failed ? 1 : 0;
  } catch (const std::exception& error) {
    std::cerr << "error: " << error.what() << '\n';
    return 2;
  }
}
