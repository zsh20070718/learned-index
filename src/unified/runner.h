#pragma once

#include "index_contract.h"

#include <cstddef>
#include <cstdint>
#include <iosfwd>
#include <optional>
#include <string>
#include <vector>

namespace string_index_bench::unified {

struct Query {
  std::string key;
  bool expected_present = false;
  v1::Value expected_value = 0;
};

struct ThroughputSample {
  std::size_t repeat = 0;
  std::uint64_t build_ns = 0;
  // Latency is measured on another fresh instance; its complete build is
  // timed separately so implementation-specific preparation stays visible.
  std::optional<std::uint64_t> latency_build_ns;
  std::uint64_t lookup_ns = 0;
  std::size_t operations = 0;
  double throughput_ops_per_second = 0.0;
};

struct LatencySample {
  std::size_t repeat = 0;
  std::size_t sample = 0;
  std::size_t query_index = 0;
  std::uint64_t latency_ns = 0;
};

struct ValidationEvidence {
  bool descriptor_checked = false;
  bool capability_domain_checked = false;
  bool empty_build_checked = false;
  bool represented_boundaries_checked = false;
  bool absent_key_checked = false;
  bool finite_domain_full = false;
  std::size_t rows_checked = 0;
  std::size_t queries_checked = 0;
};

struct RunReport {
  // One of passed, skipped, unsupported, invalid_input, or failed.
  std::string status = "failed";
  std::string stage = "initialization";
  std::string reason;
  std::optional<v1::Descriptor> descriptor;
  ValidationEvidence validation;
  std::optional<v1::MemoryUsage> memory;
  std::vector<ThroughputSample> samples;
  std::vector<LatencySample> latency_samples;
  std::uint64_t checksum = 0;
};

// The stream overloads make the frozen interchange independently testable.
std::vector<v1::Row> read_rows(std::istream& input);
std::vector<Query> read_queries(std::istream& input);
std::vector<v1::Row> read_rows_file(const std::string& path);
std::vector<Query> read_queries_file(const std::string& path);

RunReport run_static_lookup(v1::Factory factory,
                            const v1::Parameters& parameters,
                            const std::vector<v1::Row>& rows,
                            const std::vector<Query>& queries,
                            std::size_t repeats,
                            std::size_t latency_samples);

void write_result_json(std::ostream& output, const RunReport& report);
void write_samples_csv(std::ostream& output, const RunReport& report);

// Implements the frozen CLI and is called by the macro-selected main.
int run_cli(int argc, char** argv, v1::Factory factory);

}  // namespace string_index_bench::unified
