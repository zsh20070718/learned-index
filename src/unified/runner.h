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

enum class OperationKind : std::uint8_t {
  find = 1,
  insert = 2,
  update = 3,
  erase = 4,
  scan = 5,
};

struct Operation {
  OperationKind kind;
  std::string key;
  v1::Value value = 0;
  std::uint64_t limit = 0;
};

struct WorkerSample {
  std::size_t worker = 0;
  std::size_t operations = 0;
  std::uint64_t lookup_ns = 0;
  std::uint64_t checksum = 0;
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
  std::vector<WorkerSample> workers;
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
  bool concurrent_find_checked = false;
  std::size_t concurrent_queries_checked = 0;
};

struct ExecutionMetadata {
  std::size_t threads = 1;
  std::string mode = "serial";
  std::string partition = "contiguous-balanced";
  std::string latency_mode = "separate-single-thread";
};

struct RunReport {
  // One of passed, skipped, unsupported, invalid_input, or failed.
  std::string status = "failed";
  std::string stage = "initialization";
  std::string reason;
  v1::Parameters parameters;
  ExecutionMetadata execution;
  std::optional<v1::Descriptor> descriptor;
  ValidationEvidence validation;
  std::optional<v1::MemoryUsage> memory;
  std::vector<ThroughputSample> samples;
  std::vector<LatencySample> latency_samples;
  std::uint64_t checksum = 0;
};

struct OperationCounts {
  std::size_t find = 0;
  std::size_t insert = 0;
  std::size_t update = 0;
  std::size_t erase = 0;
  std::size_t scan = 0;
  std::size_t total = 0;
};

struct MixedOutcomeCounts {
  std::size_t find_hits = 0;
  std::size_t find_misses = 0;
  std::size_t insert_inserted = 0;
  std::size_t insert_already_exists = 0;
  std::size_t update_updated = 0;
  std::size_t update_not_found = 0;
  std::size_t erase_erased = 0;
  std::size_t erase_not_found = 0;
  std::size_t scan_calls = 0;
  std::size_t scan_rows = 0;
};

struct MixedValidationEvidence {
  bool descriptor_checked = false;
  bool capability_domain_checked = false;
  bool empty_build_checked = false;
  bool represented_boundaries_checked = false;
  bool trace_replay_checked = false;
  bool affected_keys_checked = false;
  bool final_state_checked = false;
  std::size_t initial_rows_checked = 0;
  std::size_t operations_checked = 0;
  std::size_t touched_keys_checked = 0;
};

struct MixedRunReport {
  std::string status = "failed";
  std::string stage = "initialization";
  std::string reason;
  v1::Parameters parameters;
  ExecutionMetadata execution{1, "serial", "serial-trace", "disabled"};
  std::optional<v1::Descriptor> descriptor;
  MixedValidationEvidence validation;
  std::optional<v1::MemoryUsage> memory;
  OperationCounts configured_operations;
  MixedOutcomeCounts outcomes;
  std::size_t initial_key_count = 0;
  std::optional<std::size_t> final_key_count;
  std::size_t touched_keys = 0;
  std::vector<ThroughputSample> samples;
  std::vector<LatencySample> latency_samples;
  std::uint64_t checksum = 0;
};

// The stream overloads make the frozen interchange independently testable.
std::vector<v1::Row> read_rows(std::istream& input);
std::vector<Query> read_queries(std::istream& input);
std::vector<Operation> read_operations(std::istream& input);
std::vector<v1::Row> read_rows_file(const std::string& path);
std::vector<Query> read_queries_file(const std::string& path);
std::vector<Operation> read_operations_file(const std::string& path);

RunReport run_static_lookup(v1::Factory factory,
                            const v1::Parameters& parameters,
                            const std::vector<v1::Row>& rows,
                            const std::vector<Query>& queries,
                            std::size_t repeats,
                            std::size_t latency_samples,
                            std::size_t threads = 1);

MixedRunReport run_mixed_operations(
    v1::Factory factory, const v1::Parameters& parameters,
    const std::vector<v1::Row>& rows,
    const std::vector<Operation>& operations, std::size_t repeats,
    std::size_t latency_samples, std::size_t threads = 1);

void write_result_json(std::ostream& output, const RunReport& report);
void write_mixed_result_json(std::ostream& output,
                             const MixedRunReport& report);
void write_samples_csv(std::ostream& output, const RunReport& report);

// Implements the frozen CLI and is called by the macro-selected main.
int run_cli(int argc, char** argv, v1::Factory factory);

}  // namespace string_index_bench::unified
