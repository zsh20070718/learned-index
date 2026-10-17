#include "runner.h"
#include "sorted_vector_adapter.h"
#include "std_map_adapter.h"

#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <exception>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <map>
#include <mutex>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace {

using string_index_bench::unified::MixedRunReport;
using string_index_bench::unified::Operation;
using string_index_bench::unified::OperationKind;
using string_index_bench::unified::Query;
using string_index_bench::unified::RunReport;
using string_index_bench::v1::Descriptor;
using string_index_bench::v1::Index;
using string_index_bench::v1::Parameters;
using string_index_bench::v1::Row;
using string_index_bench::v1::Support;
using string_index_bench::v1::Value;

int failures = 0;

void check(bool condition, const std::string& message) {
  if (!condition) {
    std::cerr << "FAIL: " << message << '\n';
    ++failures;
  }
}

template <class Function>
void expect_throw_contains(Function function, std::string_view needle,
                           const std::string& message) {
  try {
    function();
    check(false, message + " (no exception)");
  } catch (const std::exception& error) {
    check(std::string(error.what()).find(needle) != std::string::npos,
          message + " (unexpected message: " + error.what() + ")");
  }
}

void append_u64(std::string* output, std::uint64_t value) {
  for (int i = 0; i < 8; ++i) {
    output->push_back(static_cast<char>((value >> (8U * i)) & 0xffU));
  }
}

void append_key(std::string* output, std::string_view key) {
  append_u64(output, key.size());
  output->append(key.data(), key.size());
}

std::string encode_rows(const std::vector<Row>& rows) {
  std::string output("SIBROW1\0", 8);
  append_u64(&output, rows.size());
  for (const auto& row : rows) {
    append_key(&output, row.key);
    append_u64(&output, row.value);
  }
  return output;
}

std::string encode_queries(const std::vector<Query>& queries) {
  std::string output("SIBQRY1\0", 8);
  append_u64(&output, queries.size());
  for (const auto& query : queries) {
    output.push_back(query.expected_present ? 1 : 0);
    append_u64(&output, query.expected_value);
    append_key(&output, query.key);
  }
  return output;
}

std::string encode_operations(const std::vector<Operation>& operations) {
  std::string output("SIBOPS1\0", 8);
  append_u64(&output, operations.size());
  for (const auto& operation : operations) {
    output.push_back(static_cast<char>(operation.kind));
    append_u64(&output, operation.value);
    append_u64(&output, operation.limit);
    append_key(&output, operation.key);
  }
  return output;
}

enum class Fault {
  none,
  false_hit,
  false_miss,
  wrong_value,
  empty_build,
  invalid_descriptor,
  unsupported_find,
  adapted_without_notes,
  rejects_nul,
  claimed_find_throws,
  populated_false_hit,
  key_bearing_exception,
  finite_full_domain,
  factory_exception,
  concurrent_ok,
  concurrent_wrong_value,
  concurrent_exception,
};

Fault active_fault = Fault::none;
std::size_t factory_calls = 0;
std::size_t bulk_load_calls = 0;
std::size_t expected_parallel_workers = 2;
std::atomic<bool> parallel_overlap_observed{false};
std::atomic<bool> parallel_completion_observed{false};

class FaultIndex final : public Index {
 public:
  FaultIndex(Fault fault, std::size_t instance)
      : fault_(fault), instance_(instance) {}

  Descriptor descriptor() const override {
    Descriptor descriptor;
    descriptor.implementation = "fault-injection-index";
    descriptor.implementation_version = "test-v1";
    descriptor.adapter_version = "test-adapter-v1";
    descriptor.capabilities.bulk_load = Support::native;
    descriptor.capabilities.find = Support::native;
    descriptor.capabilities.concurrent_find =
        fault_ == Fault::concurrent_ok ||
        fault_ == Fault::concurrent_wrong_value ||
        fault_ == Fault::concurrent_exception;
    if (fault_ == Fault::invalid_descriptor) {
      descriptor.capabilities.keys.min_bytes = 4;
      descriptor.capabilities.keys.max_bytes = 2;
    }
    if (fault_ == Fault::unsupported_find) {
      descriptor.capabilities.find = Support::unsupported;
    }
    if (fault_ == Fault::adapted_without_notes) {
      descriptor.capabilities.find = Support::adapted;
    }
    if (fault_ == Fault::rejects_nul) {
      descriptor.capabilities.keys.allows_nul = false;
    }
    if (fault_ == Fault::finite_full_domain) {
      descriptor.capabilities.keys.fixed_bytes = 0;
      descriptor.capabilities.keys.max_bytes = 0;
    }
    return descriptor;
  }

  void bulk_load(const std::vector<Row>& rows) override {
    ++bulk_load_calls;
    if (fault_ == Fault::empty_build && rows.empty()) {
      throw std::runtime_error("injected empty build failure");
    }
    rows_ = rows;
    loaded_ = true;
  }

  std::optional<Value> find(std::string_view key) const override {
    if (!loaded_) {
      throw std::logic_error("not loaded");
    }
    if (fault_ == Fault::claimed_find_throws) {
      throw string_index_bench::v1::UnsupportedOperation(
          "injected bad find claim");
    }
    if (fault_ == Fault::key_bearing_exception) {
      throw std::runtime_error("private adapter detail for key=" +
                               std::string(key));
    }
    const bool parallel_fault =
        fault_ == Fault::concurrent_ok ||
        fault_ == Fault::concurrent_wrong_value ||
        fault_ == Fault::concurrent_exception;
    if (parallel_fault && instance_ == 4) {
      std::unique_lock<std::mutex> lock(parallel_mutex_);
      if (!parallel_released_) {
        ++parallel_waiting_;
        if (parallel_waiting_ == expected_parallel_workers) {
          parallel_overlap_observed = true;
          parallel_released_ = true;
          parallel_condition_.notify_all();
        } else {
          parallel_condition_.wait(lock, [&] { return parallel_released_; });
        }
      }
      lock.unlock();
      if (fault_ == Fault::concurrent_exception && key.empty()) {
        throw std::runtime_error(
            "private concurrent adapter detail for the empty key");
      }
      if (fault_ == Fault::concurrent_wrong_value) {
        return UINT64_C(0xfeedface);
      }
      if (fault_ == Fault::concurrent_exception &&
          key == std::string_view("\x80", 1)) {
        parallel_completion_observed = true;
      }
    }
    const auto found = std::find_if(rows_.begin(), rows_.end(),
                                    [key](const Row& row) {
                                      return row.key.size() == key.size() &&
                                             std::equal(row.key.begin(),
                                                        row.key.end(),
                                                        key.begin());
                                    });
    if (found == rows_.end()) {
      if (fault_ == Fault::false_hit ||
          (fault_ == Fault::populated_false_hit && !rows_.empty())) {
        return 991;
      }
      return std::nullopt;
    }
    if (fault_ == Fault::false_miss) {
      return std::nullopt;
    }
    if (fault_ == Fault::wrong_value) {
      return found->value + 1;
    }
    return found->value;
  }

  string_index_bench::v1::InsertResult insert(std::string_view,
                                               Value) override {
    throw string_index_bench::v1::UnsupportedOperation("unsupported");
  }
  string_index_bench::v1::UpdateResult update(std::string_view,
                                               Value) override {
    throw string_index_bench::v1::UnsupportedOperation("unsupported");
  }
  string_index_bench::v1::EraseResult erase(std::string_view) override {
    throw string_index_bench::v1::UnsupportedOperation("unsupported");
  }
  std::vector<Row> scan(std::string_view, std::size_t) const override {
    throw string_index_bench::v1::UnsupportedOperation("unsupported");
  }
  Value range_sum(std::string_view, std::string_view) const override {
    throw string_index_bench::v1::UnsupportedOperation("unsupported");
  }
  string_index_bench::v1::MemoryUsage memory_usage() const override {
    string_index_bench::v1::MemoryUsage usage;
    usage.accounting_notes = "test index memory is intentionally unavailable";
    return usage;
  }

 private:
  Fault fault_;
  std::size_t instance_;
  bool loaded_ = false;
  std::vector<Row> rows_;
  mutable std::mutex parallel_mutex_;
  mutable std::condition_variable parallel_condition_;
  mutable std::size_t parallel_waiting_ = 0;
  mutable bool parallel_released_ = false;
};

std::unique_ptr<Index> make_fault_index(const Parameters&) {
  ++factory_calls;
  if (active_fault == Fault::factory_exception) {
    throw std::runtime_error("injected factory failure");
  }
  return std::make_unique<FaultIndex>(active_fault, factory_calls);
}

struct TestUnsignedByteLess {
  using is_transparent = void;
  bool operator()(std::string_view left, std::string_view right) const {
    return std::lexicographical_compare(
        left.begin(), left.end(), right.begin(), right.end(),
        [](char a, char b) {
          return static_cast<unsigned char>(a) <
                 static_cast<unsigned char>(b);
        });
  }
};

enum class MixedFault { none, insert_exception, dropped_insert, wrong_scan };

MixedFault active_mixed_fault = MixedFault::none;
std::vector<Parameters> observed_factory_parameters;

class MixedFixtureIndex final : public Index {
 public:
  MixedFixtureIndex(std::string marker, MixedFault fault)
      : marker_(std::move(marker)), fault_(fault) {}

  Descriptor descriptor() const override {
    Descriptor descriptor;
    descriptor.implementation = "instrumented-mixed-fixture";
    descriptor.implementation_version =
        marker_.empty() ? "test-v1" : "parameter-" + marker_;
    descriptor.adapter_version = "test-mixed-adapter-v1";
    descriptor.capabilities.bulk_load = Support::native;
    descriptor.capabilities.find = Support::native;
    descriptor.capabilities.insert = Support::native;
    descriptor.capabilities.update = Support::native;
    descriptor.capabilities.erase = Support::native;
    descriptor.capabilities.scan = Support::native;
    return descriptor;
  }

  void bulk_load(const std::vector<Row>& rows) override {
    if (loaded_) {
      throw std::logic_error("bulk_load twice");
    }
    loaded_ = true;
    for (const auto& row : rows) {
      values_.emplace(row.key, row.value);
    }
  }

  std::optional<Value> find(std::string_view key) const override {
    require_loaded();
    const auto found = values_.find(key);
    return found == values_.end() ? std::nullopt
                                  : std::optional<Value>(found->second);
  }

  string_index_bench::v1::InsertResult insert(std::string_view key,
                                               Value value) override {
    require_loaded();
    if (fault_ == MixedFault::insert_exception) {
      throw std::runtime_error("secret key=" + std::string(key));
    }
    if (fault_ == MixedFault::dropped_insert) {
      return string_index_bench::v1::InsertResult::inserted;
    }
    return values_.emplace(std::string(key), value).second
               ? string_index_bench::v1::InsertResult::inserted
               : string_index_bench::v1::InsertResult::already_exists;
  }

  string_index_bench::v1::UpdateResult update(std::string_view key,
                                               Value value) override {
    require_loaded();
    const auto found = values_.find(key);
    if (found == values_.end()) {
      return string_index_bench::v1::UpdateResult::not_found;
    }
    found->second = value;
    return string_index_bench::v1::UpdateResult::updated;
  }

  string_index_bench::v1::EraseResult erase(std::string_view key) override {
    require_loaded();
    const auto found = values_.find(key);
    if (found == values_.end()) {
      return string_index_bench::v1::EraseResult::not_found;
    }
    values_.erase(found);
    return string_index_bench::v1::EraseResult::erased;
  }

  std::vector<Row> scan(std::string_view lower_bound,
                        std::size_t limit) const override {
    require_loaded();
    std::vector<Row> result;
    auto found = values_.lower_bound(lower_bound);
    while (found != values_.end() && result.size() < limit) {
      result.push_back({found->first, found->second});
      ++found;
    }
    if (fault_ == MixedFault::wrong_scan && !result.empty()) {
      result.front().value ^= 1;
    }
    return result;
  }

  Value range_sum(std::string_view, std::string_view) const override {
    throw string_index_bench::v1::UnsupportedOperation("unsupported");
  }

  string_index_bench::v1::MemoryUsage memory_usage() const override {
    string_index_bench::v1::MemoryUsage usage;
    usage.accounting_notes = "test fixture memory is intentionally unavailable";
    return usage;
  }

 private:
  void require_loaded() const {
    if (!loaded_) {
      throw std::logic_error("not loaded");
    }
  }

  std::string marker_;
  MixedFault fault_;
  bool loaded_ = false;
  std::map<std::string, Value, TestUnsignedByteLess> values_;
};

std::unique_ptr<Index> make_mixed_fixture_index(const Parameters& parameters) {
  observed_factory_parameters.push_back(parameters);
  std::set<std::string> names;
  std::string marker;
  for (const auto& parameter : parameters) {
    if (!names.insert(parameter.first).second) {
      throw std::invalid_argument("duplicate parameter");
    }
    if (parameter.first != "fixture" && parameter.first != "second") {
      throw std::invalid_argument("unknown parameter");
    }
    if (parameter.first == "fixture") {
      marker = parameter.second;
    }
  }
  return std::make_unique<MixedFixtureIndex>(marker, active_mixed_fault);
}

std::vector<Row> boundary_rows() {
  return {{"", 17},
          {"a", 9001},
          {std::string("a\0", 2), 42},
          {std::string("\x80", 1), UINT64_C(0xffffffffffffffff)}};
}

std::vector<Query> boundary_queries() {
  return {{"", true, 17},
          {"a", true, 9001},
          {std::string("a\0", 2), true, 42},
          {"missing", false, 0},
          {std::string("\x80", 1), true,
           UINT64_C(0xffffffffffffffff)}};
}

std::vector<Operation> mixed_operations() {
  return {
      {OperationKind::find, "", 0, 0},
      {OperationKind::find, "missing", 0, 0},
      {OperationKind::insert, "b", 0, 0},
      {OperationKind::insert, "b", UINT64_MAX, 0},
      {OperationKind::update, "a", UINT64_MAX, 0},
      {OperationKind::update, "missing", 1, 0},
      {OperationKind::scan, "", 0, 0},
      {OperationKind::scan, "a", 0, 8},
      {OperationKind::erase, std::string("a\0", 2), 0, 0},
      {OperationKind::erase, "missing", 0, 0},
      {OperationKind::find, std::string("a\0", 2), 0, 0},
      {OperationKind::scan, std::string("\x80", 1), 0, 2},
  };
}

RunReport run_fault(Fault fault, std::size_t repeats = 1,
                    std::size_t latency_samples = 2,
                    std::size_t threads = 1) {
  active_fault = fault;
  factory_calls = 0;
  bulk_load_calls = 0;
  return string_index_bench::unified::run_static_lookup(
      &make_fault_index, {}, boundary_rows(), boundary_queries(), repeats,
      latency_samples, threads);
}

void test_interchange() {
  const auto rows = boundary_rows();
  const auto queries = boundary_queries();
  std::istringstream row_input(encode_rows(rows));
  std::istringstream query_input(encode_queries(queries));
  const auto decoded_rows = string_index_bench::unified::read_rows(row_input);
  const auto decoded_queries =
      string_index_bench::unified::read_queries(query_input);
  check(decoded_rows.size() == rows.size(), "row interchange preserves count");
  check(decoded_rows[2].key == rows[2].key,
        "row interchange preserves embedded NUL");
  check(decoded_rows.back().key == rows.back().key,
        "row interchange preserves high-bit byte");
  check(decoded_rows.back().value == rows.back().value,
        "row interchange preserves uint64 maximum");
  check(decoded_queries.size() == queries.size(),
        "query interchange preserves count");
  check(!decoded_queries[3].expected_present,
        "query interchange preserves misses");

  const std::string long_key(10000, 'k');
  std::istringstream long_row_input(encode_rows({{long_key, 7}}));
  check(string_index_bench::unified::read_rows(long_row_input).front().key ==
            long_key,
        "interchange accepts long keys when their bytes are present");

  expect_throw_contains(
      [] {
        std::istringstream input(std::string("BADROW1\0", 8));
        (void)string_index_bench::unified::read_rows(input);
      },
      "wrong rows magic", "bad row magic is rejected");

  expect_throw_contains(
      [rows] {
        std::string data = encode_rows(rows);
        data.push_back('x');
        std::istringstream input(data);
        (void)string_index_bench::unified::read_rows(input);
      },
      "trailing bytes", "row trailing bytes are rejected");

  expect_throw_contains(
      [] {
        std::string data("SIBQRY1\0", 8);
        append_u64(&data, 1);
        data.push_back(2);
        append_u64(&data, 0);
        append_key(&data, "x");
        std::istringstream input(data);
        (void)string_index_bench::unified::read_queries(input);
      },
      "must be 0 or 1", "invalid query presence is rejected");

  expect_throw_contains(
      [] {
        std::string data("SIBROW1\0", 8);
        append_u64(&data, 1);
        append_u64(&data, 5);
        data.push_back('x');
        std::istringstream input(data);
        (void)string_index_bench::unified::read_rows(input);
      },
      "row count exceeds remaining input bytes", "truncated row is rejected");

  expect_throw_contains(
      [] {
        std::string data("SIBROW1\0", 8);
        append_u64(&data, UINT64_MAX);
        std::istringstream input(data);
        (void)string_index_bench::unified::read_rows(input);
      },
      "row count exceeds remaining input bytes",
      "hostile row count is rejected before record allocation");

  expect_throw_contains(
      [] {
        std::string data("SIBROW1\0", 8);
        append_u64(&data, 1);
        append_u64(&data, UINT64_MAX);
        data.append(8, '\0');
        std::istringstream input(data);
        (void)string_index_bench::unified::read_rows(input);
      },
      "row key length exceeds remaining input bytes",
      "hostile row key length is rejected before string allocation");

  expect_throw_contains(
      [] {
        std::string data("SIBQRY1\0", 8);
        append_u64(&data, UINT64_MAX);
        std::istringstream input(data);
        (void)string_index_bench::unified::read_queries(input);
      },
      "query count exceeds remaining input bytes",
      "hostile query count is rejected before record allocation");

  expect_throw_contains(
      [] {
        std::istringstream input(encode_rows({{"b", 1}, {"a", 2}}));
        (void)string_index_bench::unified::read_rows(input);
      },
      "not strictly increasing", "unsorted rows are rejected");

  const auto operations = mixed_operations();
  std::istringstream operation_input(encode_operations(operations));
  const auto decoded_operations =
      string_index_bench::unified::read_operations(operation_input);
  check(decoded_operations.size() == operations.size(),
        "operation interchange preserves count");
  check(decoded_operations[3].kind == OperationKind::insert &&
            decoded_operations[3].value == UINT64_MAX,
        "operation interchange preserves kind and uint64 maximum");
  check(decoded_operations[8].key == std::string("a\0", 2),
        "operation interchange preserves embedded NUL");
  check(decoded_operations.back().key == std::string("\x80", 1) &&
            decoded_operations.back().limit == 2,
        "operation interchange preserves high bytes and scan limits");

  expect_throw_contains(
      [] {
        std::istringstream input(std::string("BADOPS1\0", 8));
        (void)string_index_bench::unified::read_operations(input);
      },
      "wrong operations magic", "bad operation magic is rejected");
  expect_throw_contains(
      [] {
        std::string data("SIBOPS1\0", 8);
        append_u64(&data, 1);
        data.push_back(9);
        append_u64(&data, 0);
        append_u64(&data, 0);
        append_key(&data, "");
        std::istringstream input(data);
        (void)string_index_bench::unified::read_operations(input);
      },
      "unknown operation kind", "unknown operation kinds are rejected");
  expect_throw_contains(
      [] {
        std::string data("SIBOPS1\0", 8);
        append_u64(&data, 1);
        data.push_back(static_cast<char>(OperationKind::find));
        append_u64(&data, 1);
        append_u64(&data, 0);
        append_key(&data, "x");
        std::istringstream input(data);
        (void)string_index_bench::unified::read_operations(input);
      },
      "unused operation value", "invalid unused values are rejected");
  expect_throw_contains(
      [] {
        std::string data("SIBOPS1\0", 8);
        append_u64(&data, 1);
        data.push_back(static_cast<char>(OperationKind::insert));
        append_u64(&data, 1);
        append_u64(&data, 1);
        append_key(&data, "x");
        std::istringstream input(data);
        (void)string_index_bench::unified::read_operations(input);
      },
      "unused operation limit", "invalid unused limits are rejected");
  expect_throw_contains(
      [] {
        std::string data("SIBOPS1\0", 8);
        append_u64(&data, UINT64_MAX);
        std::istringstream input(data);
        (void)string_index_bench::unified::read_operations(input);
      },
      "operation count exceeds remaining input bytes",
      "hostile operation counts are rejected before allocation");
  expect_throw_contains(
      [] {
        std::string data("SIBOPS1\0", 8);
        append_u64(&data, 1);
        data.push_back(static_cast<char>(OperationKind::scan));
        append_u64(&data, 0);
        append_u64(&data, 1);
        append_u64(&data, 4);
        data.push_back('x');
        std::istringstream input(data);
        (void)string_index_bench::unified::read_operations(input);
      },
      "operation key length exceeds remaining input bytes",
      "truncated operation keys are rejected");
  expect_throw_contains(
      [operations] {
        std::string data = encode_operations(operations);
        data.push_back('x');
        std::istringstream input(data);
        (void)string_index_bench::unified::read_operations(input);
      },
      "trailing bytes", "operation trailing bytes are rejected");
}

void test_correctness_and_measurement() {
  const RunReport normal = run_fault(Fault::none, 2, 3);
  check(normal.status == "passed", "valid index passes: " + normal.reason);
  check(normal.validation.empty_build_checked, "empty build is checked");
  check(normal.validation.represented_boundaries_checked,
        "represented key boundaries are checked");
  check(normal.validation.absent_key_checked &&
            !normal.validation.finite_domain_full,
        "a populated absent key is checked");
  check(normal.validation.rows_checked == boundary_rows().size(),
        "every row is checked");
  check(normal.validation.queries_checked == boundary_queries().size(),
        "every query is checked");
  check(normal.samples.size() == 2, "every throughput repeat is retained");
  check(normal.latency_samples.size() == 6,
        "every requested latency sample is retained");
  check(factory_calls == 7,
        "descriptor, correctness, throughput, and latency use fresh instances");
  check(bulk_load_calls == 6,
        "each correctness or measurement instance receives one bulk load");
  check(normal.memory && !normal.memory->index_owned_bytes,
        "unknown memory remains null");
  check(normal.checksum != 0, "lookup returns are consumed into a checksum");

  std::ostringstream json;
  string_index_bench::unified::write_result_json(json, normal);
  check(json.str().find("string-index-static-run/v3") != std::string::npos &&
            json.str().find("\"parameters\": []") != std::string::npos,
        "result JSON carries the frozen schema");
  check(json.str().find("\"execution\": {\"threads\": 1") !=
            std::string::npos &&
            json.str().find("\"concurrent_find\": false") !=
                std::string::npos,
        "serial result JSON carries explicit execution and capability data");
  check(json.str().find("latency_build_ns") != std::string::npos,
        "fresh latency build timing is emitted");
  check(json.str().find(std::string("a\0", 2)) == std::string::npos,
        "result JSON does not emit key contents");

  std::ostringstream csv;
  string_index_bench::unified::write_samples_csv(csv, normal);
  check(csv.str().find("sample_kind") != std::string::npos,
        "samples CSV identifies sample kinds");
  check(csv.str().find("latency_build") != std::string::npos,
        "samples CSV preserves latency-instance build timing");

  const RunReport false_hit = run_fault(Fault::false_hit);
  check(false_hit.status == "failed" &&
            false_hit.reason.find("false hit") != std::string::npos,
        "independent harness catches false hits before timing");
  const RunReport false_miss = run_fault(Fault::false_miss);
  check(false_miss.status == "failed" &&
            false_miss.reason.find("false miss") != std::string::npos,
        "independent harness catches false misses before timing");
  const RunReport wrong_value = run_fault(Fault::wrong_value);
  check(wrong_value.status == "failed" &&
            wrong_value.reason.find("wrong value") != std::string::npos,
        "independent harness catches wrong values before timing");
  const RunReport empty_build = run_fault(Fault::empty_build);
  check(empty_build.status == "failed" &&
            empty_build.reason.find("adapter bulk_load call failed") !=
                std::string::npos &&
            empty_build.reason.find("injected empty build failure") ==
                std::string::npos,
        "independent harness catches and sanitizes empty-build failure");
  const RunReport bad_descriptor = run_fault(Fault::invalid_descriptor);
  check(bad_descriptor.status == "invalid_input" &&
            bad_descriptor.stage == "descriptor" && bulk_load_calls == 0,
        "invalid capability declaration is rejected before build");
  const RunReport missing_required = run_fault(Fault::unsupported_find);
  check(missing_required.status == "invalid_input" &&
            missing_required.stage == "descriptor" && bulk_load_calls == 0,
        "unsupported required capability is invalid input");
  const RunReport undocumented_adaptation =
      run_fault(Fault::adapted_without_notes);
  check(undocumented_adaptation.status == "invalid_input" &&
            undocumented_adaptation.stage == "descriptor" &&
            bulk_load_calls == 0,
        "adapted capability without notes is invalid input");
  const RunReport unsupported_domain = run_fault(Fault::rejects_nul);
  check(unsupported_domain.status == "unsupported" &&
            unsupported_domain.stage == "capability" && bulk_load_calls == 0,
        "complete domain incompatibility is reported as unsupported");
  const RunReport lying_find = run_fault(Fault::claimed_find_throws);
  check(lying_find.status == "failed" && lying_find.stage == "correctness",
        "bad find capability claim fails before timing");
  const RunReport factory_failure = run_fault(Fault::factory_exception);
  check(factory_failure.status == "failed" &&
            factory_failure.stage == "descriptor" &&
            factory_failure.reason.find("injected factory failure") ==
                std::string::npos &&
            factory_failure.reason.find("adapter factory call failed") !=
                std::string::npos,
        "factory exceptions are sanitized runtime failures");

  active_fault = Fault::key_bearing_exception;
  factory_calls = 0;
  bulk_load_calls = 0;
  const RunReport private_exception =
      string_index_bench::unified::run_static_lookup(
          &make_fault_index, {}, {{"private-key", 7}},
          {{"private-key", true, 7}}, 1, 0);
  check(private_exception.status == "failed" &&
            private_exception.reason.find("private adapter detail") ==
                std::string::npos &&
            private_exception.reason.find("private-key") ==
                std::string::npos &&
            private_exception.reason.find("adapter find call failed") !=
                std::string::npos &&
            private_exception.reason.find("index 0") != std::string::npos,
        "adapter exception text and probed keys never reach diagnostics");

  auto hit_only_queries = boundary_queries();
  hit_only_queries.erase(
      std::remove_if(hit_only_queries.begin(), hit_only_queries.end(),
                     [](const Query& query) {
                       return !query.expected_present;
                     }),
      hit_only_queries.end());
  active_fault = Fault::populated_false_hit;
  const RunReport state_sensitive =
      string_index_bench::unified::run_static_lookup(
          &make_fault_index, {}, boundary_rows(), hit_only_queries, 1, 0);
  check(state_sensitive.status == "failed" &&
            state_sensitive.reason.find("populated absent-key probe") !=
                std::string::npos &&
            state_sensitive.reason.find("false hit") != std::string::npos,
        "a populated absent-key probe catches state-sensitive false hits");

  active_fault = Fault::finite_full_domain;
  const RunReport finite_full =
      string_index_bench::unified::run_static_lookup(
          &make_fault_index, {}, {{"", 9}}, {{"", true, 9}}, 1, 0);
  check(finite_full.status == "passed" &&
            finite_full.validation.finite_domain_full &&
            !finite_full.validation.absent_key_checked,
        "a full finite key domain is explicitly recorded");

  const RunReport zero_latency = run_fault(Fault::none, 1, 0);
  std::ostringstream zero_latency_json;
  string_index_bench::unified::write_result_json(zero_latency_json,
                                                  zero_latency);
  std::ostringstream zero_latency_csv;
  string_index_bench::unified::write_samples_csv(zero_latency_csv,
                                                  zero_latency);
  check(zero_latency.status == "passed" &&
            zero_latency.latency_samples.empty() &&
            !zero_latency.samples.front().latency_build_ns &&
            zero_latency_json.str().find("\"latency_build_ns\": null") !=
                std::string::npos &&
            zero_latency_csv.str().find("latency_build,") ==
                std::string::npos,
        "zero requested latency samples emit no fabricated latency build");

  auto bad_queries = boundary_queries();
  bad_queries.front().expected_value = 99;
  active_fault = Fault::none;
  factory_calls = 0;
  const RunReport mismatched =
      string_index_bench::unified::run_static_lookup(
          &make_fault_index, {}, boundary_rows(), bad_queries, 1, 1);
  check(mismatched.status == "invalid_input" &&
            mismatched.stage == "interchange" && factory_calls == 0,
        "query expectations are checked against an independent oracle");
}

void reset_parallel_fixture(Fault fault, std::size_t workers = 2) {
  active_fault = fault;
  factory_calls = 0;
  bulk_load_calls = 0;
  expected_parallel_workers = workers;
  parallel_overlap_observed = false;
  parallel_completion_observed = false;
}

void test_parallel_execution() {
  const RunReport default_serial = run_fault(Fault::none, 1, 0);
  check(default_serial.status == "passed" &&
            default_serial.execution.threads == 1 &&
            default_serial.execution.mode == "serial" &&
            !default_serial.validation.concurrent_find_checked &&
            default_serial.validation.concurrent_queries_checked == 0 &&
            default_serial.samples.size() == 1 &&
            default_serial.samples.front().workers.size() == 1 &&
            default_serial.samples.front().workers.front().operations ==
                boundary_queries().size(),
        "omitted thread count retains the direct one-thread execution path");

  active_fault = Fault::none;
  factory_calls = 0;
  bulk_load_calls = 0;
  const RunReport unsupported = string_index_bench::unified::run_static_lookup(
      &make_fault_index, {}, boundary_rows(), boundary_queries(), 1, 0, 2);
  check(unsupported.status == "unsupported" &&
            unsupported.stage == "capability" && bulk_load_calls == 0 &&
            unsupported.samples.empty() && unsupported.latency_samples.empty() &&
            unsupported.checksum == 0,
        "parallel execution rejects adapters without concurrent-find support");

  for (const std::size_t invalid_threads : {std::size_t{0},
                                             std::size_t{257},
                                             std::size_t{6}}) {
    factory_calls = 0;
    const RunReport invalid = string_index_bench::unified::run_static_lookup(
        &make_fault_index, {}, boundary_rows(), boundary_queries(), 1, 0,
        invalid_threads);
    check(invalid.status == "invalid_input" &&
              invalid.stage == "execution" && factory_calls == 0 &&
              invalid.execution.threads == invalid_threads &&
              invalid.samples.empty() && invalid.latency_samples.empty() &&
              invalid.checksum == 0,
          "invalid thread count is rejected before adapter construction");
  }

  reset_parallel_fixture(Fault::concurrent_ok);
  const RunReport parallel = string_index_bench::unified::run_static_lookup(
      &make_fault_index, {}, boundary_rows(), boundary_queries(), 1, 0, 2);
  check(parallel.status == "passed",
        "parallel-capable fixture passes: " + parallel.reason);
  check(parallel_overlap_observed &&
            parallel.validation.concurrent_find_checked &&
            parallel.validation.concurrent_queries_checked ==
                boundary_queries().size(),
        "parallel correctness validation performs overlapping shared-index reads");
  check(parallel.execution.threads == 2 &&
            parallel.execution.mode == "shared-index-read-only" &&
            parallel.execution.partition == "contiguous-balanced" &&
            parallel.execution.latency_mode == "separate-single-thread" &&
            parallel.samples.size() == 1 &&
            parallel.samples.front().workers.size() == 2 &&
            parallel.samples.front().workers[0].worker == 0 &&
            parallel.samples.front().workers[0].operations == 3 &&
            parallel.samples.front().workers[1].worker == 1 &&
            parallel.samples.front().workers[1].operations == 2,
        "uneven query counts use ordered contiguous balanced partitions");
  std::ostringstream parallel_json;
  string_index_bench::unified::write_result_json(parallel_json, parallel);
  check(parallel_json.str().find("string-index-static-run/v3") !=
            std::string::npos &&
            parallel_json.str().find("\"mode\": \"shared-index-read-only\"") !=
                std::string::npos &&
            parallel_json.str().find("\"concurrent_find_checked\": true") !=
                std::string::npos &&
            parallel_json.str().find("\"workers\": [{\"worker\": 0, \"operations\": 3") !=
                std::string::npos,
        "parallel JSON emits strict v3 execution, validation, and worker evidence");

  reset_parallel_fixture(Fault::concurrent_exception);
  const RunReport exception = string_index_bench::unified::run_static_lookup(
      &make_fault_index, {}, boundary_rows(), boundary_queries(), 1, 0, 2);
  check(exception.status == "failed" && exception.stage == "correctness" &&
            exception.reason.find("adapter find call failed") !=
                std::string::npos &&
            exception.reason.find("private concurrent adapter detail") ==
                std::string::npos &&
            parallel_completion_observed && exception.samples.empty() &&
            exception.latency_samples.empty() && exception.checksum == 0,
        "parallel exceptions are redacted and all launched workers are joined");

  reset_parallel_fixture(Fault::concurrent_wrong_value);
  const RunReport wrong = string_index_bench::unified::run_static_lookup(
      &make_fault_index, {}, boundary_rows(), boundary_queries(), 1, 0, 2);
  check(wrong.status == "failed" && wrong.stage == "correctness" &&
            wrong.reason.find("wrong value") != std::string::npos &&
            parallel_overlap_observed && wrong.samples.empty() &&
            wrong.latency_samples.empty() && wrong.checksum == 0,
        "synchronized correctness validation catches concurrency-only errors");
}

void test_mixed_execution() {
  const auto rows = boundary_rows();
  const auto operations = mixed_operations();
  const MixedRunReport report =
      string_index_bench::unified::run_mixed_operations(
          &make_std_map_index, {}, rows, operations, 2, 0);
  check(report.status == "passed",
        "std::map mixed run passes: " + report.reason);
  check(report.configured_operations.find == 3 &&
            report.configured_operations.insert == 2 &&
            report.configured_operations.update == 2 &&
            report.configured_operations.erase == 2 &&
            report.configured_operations.scan == 3 &&
            report.configured_operations.total == operations.size(),
        "mixed report preserves configured operation counts");
  check(report.outcomes.find_hits == 1 &&
            report.outcomes.find_misses == 2 &&
            report.outcomes.insert_inserted == 1 &&
            report.outcomes.insert_already_exists == 1 &&
            report.outcomes.update_updated == 1 &&
            report.outcomes.update_not_found == 1 &&
            report.outcomes.erase_erased == 1 &&
            report.outcomes.erase_not_found == 1 &&
            report.outcomes.scan_calls == 3 &&
            report.outcomes.scan_rows == 5,
        "independent mixed oracle records every success and failure branch");
  check(report.validation.descriptor_checked &&
            report.validation.capability_domain_checked &&
            report.validation.empty_build_checked &&
            report.validation.represented_boundaries_checked &&
            report.validation.trace_replay_checked &&
            report.validation.affected_keys_checked &&
            report.validation.final_state_checked &&
            report.validation.initial_rows_checked == rows.size() &&
            report.validation.operations_checked == operations.size() &&
            report.validation.touched_keys_checked == 6,
        "mixed correctness evidence covers trace, affected keys, and final state");
  check(report.initial_key_count == rows.size() &&
            report.final_key_count == std::optional<std::size_t>(4) &&
            report.touched_keys == 6 && report.samples.size() == 2 &&
            report.samples[0].operations == operations.size() &&
            report.samples[0].workers.size() == 1 &&
            report.samples[0].workers[0].operations == operations.size() &&
            !report.samples[0].latency_build_ns &&
            report.latency_samples.empty() && report.checksum != 0,
        "mixed timing uses fresh serial full-trace samples and consumes results");

  std::ostringstream json;
  string_index_bench::unified::write_mixed_result_json(json, report);
  check(json.str().find("\"schema\": \"string-index-mixed-run/v1\"") !=
                std::string::npos &&
            json.str().find("\"partition\": \"serial-trace\"") !=
                std::string::npos &&
            json.str().find("\"latency_samples\": []") !=
                std::string::npos &&
            json.str().find("\"scan_rows\": 5") != std::string::npos &&
            json.str().find(std::string("a\0", 2)) == std::string::npos,
        "mixed JSON emits the frozen shape without key contents");

  const std::vector<Operation> read_and_scan = {
      {OperationKind::scan, "a", 0, 2},
      {OperationKind::find, std::string("\x80", 1), 0, 0}};
  const MixedRunReport sorted_scan =
      string_index_bench::unified::run_mixed_operations(
          &make_sorted_vector_index, {}, rows, read_and_scan, 1, 0);
  check(sorted_scan.status == "passed" &&
            sorted_scan.outcomes.scan_calls == 1 &&
            sorted_scan.outcomes.scan_rows == 2,
        "sorted vector supports native owning scans in mixed runs: " +
            sorted_scan.reason);
  const MixedRunReport sorted_write =
      string_index_bench::unified::run_mixed_operations(
          &make_sorted_vector_index, {}, rows,
          {{OperationKind::insert, "new", 3, 0}}, 1, 0);
  check(sorted_write.status == "unsupported" &&
            sorted_write.stage == "capabilities" &&
            sorted_write.reason == "unsupported operation kinds: insert" &&
            sorted_write.samples.empty() && sorted_write.checksum == 0 &&
            !sorted_write.final_key_count,
        "static adapters receive a value-free capability skip for writes");

  const MixedRunReport parallel =
      string_index_bench::unified::run_mixed_operations(
          &make_std_map_index, {}, rows, operations, 1, 0, 2);
  check(parallel.status == "unsupported" && parallel.stage == "execution" &&
            parallel.execution.threads == 2 &&
            parallel.execution.mode == "unsupported-mixed-parallel" &&
            parallel.samples.empty() && parallel.checksum == 0,
        "mixed parallel requests are structured unsupported results");
  const MixedRunReport latency =
      string_index_bench::unified::run_mixed_operations(
          &make_std_map_index, {}, rows, operations, 1, 1);
  check(latency.status == "invalid_input" &&
            latency.stage == "configuration" && latency.samples.empty() &&
            latency.checksum == 0,
        "mixed per-operation latency requests are rejected before construction");

  active_mixed_fault = MixedFault::insert_exception;
  const MixedRunReport redacted =
      string_index_bench::unified::run_mixed_operations(
          &make_mixed_fixture_index, {}, rows,
          {{OperationKind::insert, "private-key", 7, 0}}, 1, 0);
  check(redacted.status == "failed" && redacted.stage == "correctness" &&
            redacted.reason.find("adapter insert call failed") !=
                std::string::npos &&
            redacted.reason.find("operation ordinal 0") != std::string::npos &&
            redacted.reason.find("private-key") == std::string::npos &&
            redacted.reason.find("secret") == std::string::npos &&
            redacted.samples.empty() && redacted.checksum == 0 &&
            !redacted.final_key_count,
        "mixed adapter exceptions are redacted by kind and ordinal");
  active_mixed_fault = MixedFault::dropped_insert;
  const MixedRunReport dropped =
      string_index_bench::unified::run_mixed_operations(
          &make_mixed_fixture_index, {}, rows,
          {{OperationKind::insert, "new-key", 7, 0}}, 1, 0);
  check(dropped.status == "failed" &&
            dropped.reason.find("false miss") != std::string::npos,
        "affected-key validation catches a fake successful insert");
  active_mixed_fault = MixedFault::wrong_scan;
  const MixedRunReport wrong_scan =
      string_index_bench::unified::run_mixed_operations(
          &make_mixed_fixture_index, {}, rows,
          {{OperationKind::scan, "a", 0, 2}}, 1, 0);
  check(wrong_scan.status == "failed" &&
            wrong_scan.reason.find("wrong rows") != std::string::npos,
        "independent ordered scan oracle catches wrong scan values");
  active_mixed_fault = MixedFault::none;

  observed_factory_parameters.clear();
  const Parameters parameters = {{"fixture", "ordered=value"}};
  const MixedRunReport parameterized =
      string_index_bench::unified::run_mixed_operations(
          &make_mixed_fixture_index, parameters, rows, read_and_scan, 1, 0);
  check(parameterized.status == "passed" &&
            parameterized.parameters == parameters &&
            parameterized.descriptor &&
            parameterized.descriptor->implementation_version ==
                "parameter-ordered=value" &&
            !observed_factory_parameters.empty() &&
            std::all_of(observed_factory_parameters.begin(),
                        observed_factory_parameters.end(),
                        [&](const Parameters& seen) {
                          return seen == parameters;
                        }),
        "parameters reach every mixed factory construction in input order");
}

void expect_writes_unsupported(Index* index) {
  expect_throw_contains([index] { (void)index->insert("x", 1); }, "unsupported",
                        "insert reports unsupported");
  expect_throw_contains([index] { (void)index->update("x", 1); }, "unsupported",
                        "update reports unsupported");
  expect_throw_contains([index] { (void)index->erase("x"); }, "unsupported",
                        "erase reports unsupported");
  expect_throw_contains([index] { (void)index->range_sum("x", "y"); },
                        "unsupported", "range_sum reports unsupported");
}

void test_adapters() {
  for (const auto factory : {&make_std_map_index, &make_sorted_vector_index}) {
    const RunReport report = string_index_bench::unified::run_static_lookup(
        factory, {}, boundary_rows(), boundary_queries(), 1, 3);
    check(report.status == "passed",
          "baseline adapter passes arbitrary-byte run: " + report.reason);
    check(report.descriptor &&
              report.descriptor->capabilities.find == Support::native,
          "baseline adapter declares native find");
    check(report.descriptor && report.descriptor->capabilities.concurrent_find,
          "baseline adapter opts into concurrent shared-index find");
    check(report.descriptor &&
              report.descriptor->capabilities.scan == Support::native &&
              report.descriptor->capabilities.range_sum ==
                  Support::unsupported,
          "baseline adapters advertise native owning scan support");

    auto index = factory({});
    index->bulk_load(boundary_rows());
    check(index->find(std::string("a\0", 2)) == std::optional<Value>(42),
          "baseline adapter preserves embedded NUL keys");
    check(index->find(std::string("\x80", 1)) ==
              std::optional<Value>(UINT64_C(0xffffffffffffffff)),
          "baseline adapter preserves high-bit keys and uint64 maximum");
    auto scan = index->scan("a", 2);
    check(scan.size() == 2 && scan[0].key == "a" &&
              scan[0].value == 9001 && scan[1].key == std::string("a\0", 2) &&
              scan[1].value == 42,
          "baseline adapter scan is unsigned-byte ordered and bounded");
    scan[0].key = "mutated";
    scan[0].value = 0;
    check(index->find("a") == std::optional<Value>(9001),
          "baseline adapter scan results own their keys and values");
    expect_throw_contains([&] { (void)index->range_sum("x", "y"); },
                          "unsupported", "range_sum reports unsupported");

    expect_throw_contains([factory] { (void)factory({{"unknown", "1"}}); },
                          "unknown parameter",
                          "baseline adapter rejects unknown parameters");
    expect_throw_contains(
        [factory] {
          (void)factory({{"duplicate", "1"}, {"duplicate", "2"}});
        },
        "duplicate parameter",
        "baseline adapter rejects duplicate parameters before unknowns");

    const RunReport parallel_report =
        string_index_bench::unified::run_static_lookup(
            factory, {}, boundary_rows(), boundary_queries(), 1, 0, 2);
    check(parallel_report.status == "passed" &&
              parallel_report.validation.concurrent_find_checked &&
              parallel_report.samples.front().workers.size() == 2,
          "baseline adapter passes shared-index parallel lookup: " +
              parallel_report.reason);
  }

  const auto std_map = make_std_map_index({})->descriptor();
  check(std_map.capabilities.bulk_load == Support::adapted &&
            std_map.capabilities.insert == Support::native &&
            std_map.capabilities.update == Support::native &&
            std_map.capabilities.erase == Support::native &&
            std_map.capabilities.scan == Support::native &&
            std_map.adapter_version == "builtin-std-map-v2" &&
            std_map.adaptation_notes.find("emplace") != std::string::npos,
        "std::map v2 honestly declares adapted load and native mutations");
  auto mutable_map = make_std_map_index({});
  mutable_map->bulk_load(boundary_rows());
  check(mutable_map->insert("new", 3) ==
                string_index_bench::v1::InsertResult::inserted &&
            mutable_map->insert("new", 4) ==
                string_index_bench::v1::InsertResult::already_exists &&
            mutable_map->find("new") == std::optional<Value>(3) &&
            mutable_map->update("new", UINT64_MAX) ==
                string_index_bench::v1::UpdateResult::updated &&
            mutable_map->update("absent", 1) ==
                string_index_bench::v1::UpdateResult::not_found &&
            mutable_map->erase("new") ==
                string_index_bench::v1::EraseResult::erased &&
            mutable_map->erase("new") ==
                string_index_bench::v1::EraseResult::not_found,
        "std::map implements exact duplicate and missing mutation semantics");
  const auto sorted_vector = make_sorted_vector_index({})->descriptor();
  check(sorted_vector.capabilities.bulk_load == Support::native &&
            sorted_vector.capabilities.insert == Support::unsupported &&
            sorted_vector.capabilities.update == Support::unsupported &&
            sorted_vector.capabilities.erase == Support::unsupported &&
            sorted_vector.capabilities.scan == Support::native &&
            sorted_vector.adapter_version == "builtin-sorted-vector-v2" &&
            sorted_vector.adaptation_notes.empty(),
        "sorted vector v2 stays write-static and adds native scan");
  auto static_vector = make_sorted_vector_index({});
  static_vector->bulk_load(boundary_rows());
  expect_writes_unsupported(static_vector.get());
}

void write_binary_file(const std::filesystem::path& path,
                       const std::string& contents) {
  std::ofstream output(path, std::ios::binary);
  if (!output) {
    throw std::runtime_error("cannot create CLI fixture");
  }
  output.write(contents.data(), static_cast<std::streamsize>(contents.size()));
  if (!output) {
    throw std::runtime_error("cannot write CLI fixture");
  }
}

std::string read_text_file(const std::filesystem::path& path) {
  std::ifstream input(path);
  if (!input) {
    throw std::runtime_error("cannot read CLI output");
  }
  return std::string(std::istreambuf_iterator<char>(input),
                     std::istreambuf_iterator<char>());
}

int invoke_cli(const std::filesystem::path& rows,
               const std::filesystem::path& queries,
               const std::filesystem::path& result,
               const std::filesystem::path& samples,
               const std::string& threads = "",
               const Parameters& parameters = {},
               string_index_bench::v1::Factory factory = &make_fault_index) {
  std::vector<std::string> arguments = {
      "unified-test-runner", "--rows", rows.string(), "--queries",
      queries.string(),      "--result", result.string(), "--samples",
      samples.string(),      "--repeats", "2", "--latency-samples", "3"};
  if (!threads.empty()) {
    arguments.push_back("--threads");
    arguments.push_back(threads);
  }
  for (const auto& parameter : parameters) {
    arguments.push_back("--parameter");
    arguments.push_back(parameter.first + "=" + parameter.second);
  }
  std::vector<char*> argv;
  argv.reserve(arguments.size());
  for (auto& argument : arguments) {
    argv.push_back(argument.data());
  }
  return string_index_bench::unified::run_cli(
      static_cast<int>(argv.size()), argv.data(), factory);
}

int invoke_mixed_cli(
    const std::filesystem::path& rows,
    const std::filesystem::path& operations,
    const std::filesystem::path& result,
    const std::filesystem::path& samples,
    const std::string& threads = "", const Parameters& parameters = {},
    string_index_bench::v1::Factory factory = &make_std_map_index) {
  std::vector<std::string> arguments = {
      "unified-test-runner", "--rows", rows.string(), "--operations",
      operations.string(),   "--result", result.string(), "--samples",
      samples.string(),      "--repeats", "2", "--latency-samples", "0"};
  if (!threads.empty()) {
    arguments.push_back("--threads");
    arguments.push_back(threads);
  }
  for (const auto& parameter : parameters) {
    arguments.push_back("--parameter");
    arguments.push_back(parameter.first + "=" + parameter.second);
  }
  std::vector<char*> argv;
  argv.reserve(arguments.size());
  for (auto& argument : arguments) {
    argv.push_back(argument.data());
  }
  return string_index_bench::unified::run_cli(
      static_cast<int>(argv.size()), argv.data(), factory);
}

void test_cli(const std::filesystem::path& executable_path) {
  const auto output_dir = executable_path.parent_path();
  const auto rows_path = output_dir / "unified-runner-test-rows.bin";
  const auto queries_path = output_dir / "unified-runner-test-queries.bin";
  const auto operations_path =
      output_dir / "unified-runner-test-operations.bin";
  const auto malformed_path =
      output_dir / "unified-runner-test-malformed-queries.bin";
  const auto result_path = output_dir / "unified-runner-test-result.json";
  const auto samples_path = output_dir / "unified-runner-test-samples.csv";
  const auto invalid_result_path =
      output_dir / "unified-runner-test-invalid-result.json";
  const auto invalid_samples_path =
      output_dir / "unified-runner-test-invalid-samples.csv";
  const auto parallel_result_path =
      output_dir / "unified-runner-test-parallel-result.json";
  const auto parallel_samples_path =
      output_dir / "unified-runner-test-parallel-samples.csv";
  const auto mixed_result_path =
      output_dir / "unified-runner-test-mixed-result.json";
  const auto mixed_samples_path =
      output_dir / "unified-runner-test-mixed-samples.csv";
  const auto parameter_result_path =
      output_dir / "unified-runner-test-parameter-result.json";
  const auto parameter_samples_path =
      output_dir / "unified-runner-test-parameter-samples.csv";
  const auto mixed_parallel_result_path =
      output_dir / "unified-runner-test-mixed-parallel-result.json";
  const auto mixed_parallel_samples_path =
      output_dir / "unified-runner-test-mixed-parallel-samples.csv";
  const auto collision_path =
      output_dir / "unified-runner-test-collision-output";
  const auto collision_directory =
      output_dir / "unified-runner-test-collision-directory";
  const auto exclusive_samples_path =
      output_dir / "unified-runner-test-exclusive-samples.csv";

  try {
    write_binary_file(rows_path, encode_rows(boundary_rows()));
    write_binary_file(queries_path, encode_queries(boundary_queries()));
    write_binary_file(operations_path, encode_operations(mixed_operations()));
    write_binary_file(malformed_path, std::string("NOTQRY1\0", 8));

    active_fault = Fault::none;
    check(invoke_cli(rows_path, queries_path, result_path, samples_path) == 0,
          "frozen CLI succeeds for a valid interchange");
    const std::string result = read_text_file(result_path);
    const std::string samples = read_text_file(samples_path);
    check(result.find("\"status\": \"passed\"") != std::string::npos &&
              result.find("string-index-static-run/v3") !=
                  std::string::npos &&
              result.find("\"parameters\": []") != std::string::npos,
          "CLI emits a passed v3 terminal result with parameters");
    check(samples.find("throughput") != std::string::npos &&
              samples.find("latency") != std::string::npos,
          "CLI emits raw throughput and latency samples");

    reset_parallel_fixture(Fault::concurrent_ok);
    check(invoke_cli(rows_path, queries_path, parallel_result_path,
                     parallel_samples_path, "2") == 0,
          "CLI accepts an explicit parallel thread count");
    const std::string parallel_result = read_text_file(parallel_result_path);
    check(parallel_result.find("\"threads\": 2") != std::string::npos &&
              parallel_result.find("\"mode\": \"shared-index-read-only\"") !=
                  std::string::npos &&
              parallel_result.find("\"workers\": [{\"worker\": 0") !=
                  std::string::npos,
          "CLI carries the requested parallel execution into v3 JSON");
    active_fault = Fault::none;

    check(invoke_mixed_cli(rows_path, operations_path, mixed_result_path,
                           mixed_samples_path) == 0,
          "CLI accepts the mixed operations interchange");
    const std::string mixed_result = read_text_file(mixed_result_path);
    const std::string mixed_samples = read_text_file(mixed_samples_path);
    check(mixed_result.find("string-index-mixed-run/v1") !=
                  std::string::npos &&
              mixed_result.find("\"status\": \"passed\"") !=
                  std::string::npos &&
              mixed_result.find("\"operations\": 12") !=
                  std::string::npos &&
              mixed_samples.find("throughput") != std::string::npos &&
              mixed_samples.find("latency,") == std::string::npos,
          "mixed CLI emits exact result and aggregate-only timing samples");

    observed_factory_parameters.clear();
    const Parameters cli_parameters = {
        {"fixture", "A=B"}, {"second", ""}};
    check(invoke_mixed_cli(rows_path, operations_path, parameter_result_path,
                           parameter_samples_path, "", cli_parameters,
                           &make_mixed_fixture_index) == 0,
          "CLI forwards ordered parameters to the instrumented factory");
    const std::string parameter_result = read_text_file(parameter_result_path);
    check(parameter_result.find(
              "\"parameters\": [{\"name\": \"fixture\", \"value\": \"A=B\"},{\"name\": \"second\", \"value\": \"\"}]") !=
                  std::string::npos &&
              parameter_result.find(
                  "\"implementation_version\": \"parameter-A=B\"") !=
                  std::string::npos &&
              !observed_factory_parameters.empty() &&
              std::all_of(observed_factory_parameters.begin(),
                          observed_factory_parameters.end(),
                          [&](const Parameters& seen) {
                            return seen == cli_parameters;
                          }),
          "parameter values keep order, emptiness, and embedded equals signs");

    check(invoke_mixed_cli(rows_path, operations_path,
                           mixed_parallel_result_path,
                           mixed_parallel_samples_path, "2") == 0,
          "mixed CLI serializes unsupported parallel requests");
    const std::string mixed_parallel_result =
        read_text_file(mixed_parallel_result_path);
    check(mixed_parallel_result.find("\"status\": \"unsupported\"") !=
                  std::string::npos &&
              mixed_parallel_result.find(
                  "\"mode\": \"unsupported-mixed-parallel\"") !=
                  std::string::npos &&
              mixed_parallel_result.find("\"samples\": []") !=
                  std::string::npos,
          "mixed CLI retains configured counts but emits no parallel measurements");

    check(invoke_mixed_cli(
              rows_path, operations_path, collision_path,
              mixed_parallel_samples_path, "",
              {{"fixture", "one"}, {"fixture", "two"}},
              &make_mixed_fixture_index) == 2 &&
              !std::filesystem::exists(collision_path),
          "CLI rejects duplicate parameter names before execution");
    check(invoke_mixed_cli(rows_path, operations_path, collision_path,
                           mixed_parallel_samples_path, "",
                           {{"1bad", "value"}},
                           &make_mixed_fixture_index) == 2 &&
              !std::filesystem::exists(collision_path),
          "CLI rejects malformed parameter names before execution");

    auto invoke_raw = [](std::vector<std::string> arguments) {
      std::vector<char*> argv;
      for (auto& argument : arguments) {
        argv.push_back(argument.data());
      }
      return string_index_bench::unified::run_cli(
          static_cast<int>(argv.size()), argv.data(), &make_std_map_index);
    };
    const std::vector<std::string> common_arguments = {
        "unified-test-runner", "--rows", rows_path.string(), "--result",
        collision_path.string(), "--samples", exclusive_samples_path.string(),
        "--repeats", "1", "--latency-samples", "0"};
    auto both_arguments = common_arguments;
    both_arguments.insert(both_arguments.end(),
                          {"--queries", queries_path.string(), "--operations",
                           operations_path.string()});
    check(invoke_raw(std::move(both_arguments)) == 2 &&
              !std::filesystem::exists(collision_path) &&
              !std::filesystem::exists(exclusive_samples_path),
          "CLI rejects simultaneous query and operation inputs");
    check(invoke_raw(common_arguments) == 2 &&
              !std::filesystem::exists(collision_path) &&
              !std::filesystem::exists(exclusive_samples_path),
          "CLI rejects a missing query/operation input");

    check(invoke_cli(rows_path, queries_path, collision_path,
                     parallel_samples_path, "0") == 2 &&
              !std::filesystem::exists(collision_path),
          "CLI rejects a thread count outside the supported range");

    check(invoke_cli(rows_path, queries_path, result_path, samples_path) == 2,
          "CLI rejects existing output paths before opening them");
    check(read_text_file(result_path) == result &&
              read_text_file(samples_path) == samples,
          "existing CLI outputs remain unchanged after rejection");

    check(invoke_cli(rows_path, queries_path, collision_path,
                     collision_path) == 2 &&
              !std::filesystem::exists(collision_path),
          "CLI rejects equal result and samples paths without creating them");
    std::filesystem::create_directory(collision_directory);
    const auto canonical_alias =
        collision_directory / ".." / collision_path.filename();
    check(invoke_cli(rows_path, queries_path, collision_path,
                     canonical_alias) == 2 &&
              !std::filesystem::exists(collision_path),
          "CLI rejects canonically colliding output paths");

    const std::string original_rows = read_text_file(rows_path);
    check(invoke_cli(rows_path, queries_path, rows_path, collision_path) == 2 &&
              read_text_file(rows_path) == original_rows &&
              !std::filesystem::exists(collision_path),
          "CLI rejects output collisions with rows and preserves the input");
    check(invoke_cli(rows_path, queries_path, collision_path, queries_path) ==
                  2 &&
              read_text_file(queries_path) == encode_queries(boundary_queries()) &&
              !std::filesystem::exists(collision_path),
          "CLI rejects output collisions with queries and preserves the input");

    check(invoke_cli(rows_path, malformed_path, invalid_result_path,
                     invalid_samples_path) == 1,
          "malformed interchange produces a non-success CLI exit");
    const std::string invalid_result = read_text_file(invalid_result_path);
    check(invalid_result.find("\"status\": \"invalid_input\"") !=
              std::string::npos &&
              invalid_result.find("\"stage\": \"interchange\"") !=
                  std::string::npos,
          "malformed interchange is recorded as invalid_input");
  } catch (const std::exception& error) {
    check(false, std::string("CLI exercise failed: ") + error.what());
  }

  for (const auto& path : {
           rows_path, queries_path, operations_path, malformed_path,
           result_path, samples_path, invalid_result_path,
           invalid_samples_path, parallel_result_path, parallel_samples_path,
           mixed_result_path, mixed_samples_path, parameter_result_path,
           parameter_samples_path, mixed_parallel_result_path,
           mixed_parallel_samples_path, collision_path,
           exclusive_samples_path}) {
    std::error_code ignored;
    std::filesystem::remove(path, ignored);
  }
  {
    std::error_code ignored;
    std::filesystem::remove(collision_directory, ignored);
  }
}

}  // namespace

int main(int argc, char** argv) {
  test_interchange();
  test_correctness_and_measurement();
  test_parallel_execution();
  test_mixed_execution();
  test_adapters();
  if (argc > 0) {
    test_cli(std::filesystem::absolute(argv[0]));
  }
  if (failures != 0) {
    std::cerr << failures << " unified runner checks failed\n";
    return EXIT_FAILURE;
  }
  std::cout << "unified runner checks passed\n";
  return EXIT_SUCCESS;
}
