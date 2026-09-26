#include "runner.h"
#include "sorted_vector_adapter.h"
#include "std_map_adapter.h"

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <exception>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace {

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
};

Fault active_fault = Fault::none;
std::size_t factory_calls = 0;
std::size_t bulk_load_calls = 0;

class FaultIndex final : public Index {
 public:
  explicit FaultIndex(Fault fault) : fault_(fault) {}

  Descriptor descriptor() const override {
    Descriptor descriptor;
    descriptor.implementation = "fault-injection-index";
    descriptor.implementation_version = "test-v1";
    descriptor.adapter_version = "test-adapter-v1";
    descriptor.capabilities.bulk_load = Support::native;
    descriptor.capabilities.find = Support::native;
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
  bool loaded_ = false;
  std::vector<Row> rows_;
};

std::unique_ptr<Index> make_fault_index(const Parameters&) {
  ++factory_calls;
  if (active_fault == Fault::factory_exception) {
    throw std::runtime_error("injected factory failure");
  }
  return std::make_unique<FaultIndex>(active_fault);
}

std::vector<Row> boundary_rows() {
  return {{"", 0},
          {"a", 1},
          {std::string("a\0", 2), 2},
          {std::string("\x80", 1), UINT64_C(0xffffffffffffffff)}};
}

std::vector<Query> boundary_queries() {
  return {{"", true, 0},
          {"a", true, 1},
          {std::string("a\0", 2), true, 2},
          {"missing", false, 0},
          {std::string("\x80", 1), true,
           UINT64_C(0xffffffffffffffff)}};
}

RunReport run_fault(Fault fault, std::size_t repeats = 1,
                    std::size_t latency_samples = 2) {
  active_fault = fault;
  factory_calls = 0;
  bulk_load_calls = 0;
  return string_index_bench::unified::run_static_lookup(
      &make_fault_index, {}, boundary_rows(), boundary_queries(), repeats,
      latency_samples);
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
  check(json.str().find("string-index-static-run/v1") != std::string::npos,
        "result JSON carries the frozen schema");
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

void expect_optional_unsupported(Index* index) {
  expect_throw_contains([index] { (void)index->insert("x", 1); }, "unsupported",
                        "insert reports unsupported");
  expect_throw_contains([index] { (void)index->update("x", 1); }, "unsupported",
                        "update reports unsupported");
  expect_throw_contains([index] { (void)index->erase("x"); }, "unsupported",
                        "erase reports unsupported");
  expect_throw_contains([index] { (void)index->scan("x", 1); }, "unsupported",
                        "scan reports unsupported");
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
    check(report.descriptor &&
              report.descriptor->capabilities.insert == Support::unsupported &&
              report.descriptor->capabilities.update == Support::unsupported &&
              report.descriptor->capabilities.erase == Support::unsupported &&
              report.descriptor->capabilities.scan == Support::unsupported &&
              report.descriptor->capabilities.range_sum ==
                  Support::unsupported,
          "baseline adapter declares all optional operations unsupported");

    auto index = factory({});
    index->bulk_load(boundary_rows());
    check(index->find(std::string("a\0", 2)) == std::optional<Value>(2),
          "baseline adapter preserves embedded NUL keys");
    check(index->find(std::string("\x80", 1)) ==
              std::optional<Value>(UINT64_C(0xffffffffffffffff)),
          "baseline adapter preserves high-bit keys and uint64 maximum");
    expect_optional_unsupported(index.get());

    expect_throw_contains([factory] { (void)factory({{"unknown", "1"}}); },
                          "unknown parameter",
                          "baseline adapter rejects unknown parameters");
    expect_throw_contains(
        [factory] {
          (void)factory({{"duplicate", "1"}, {"duplicate", "2"}});
        },
        "duplicate parameter",
        "baseline adapter rejects duplicate parameters before unknowns");
  }

  const auto std_map = make_std_map_index({})->descriptor();
  check(std_map.capabilities.bulk_load == Support::adapted &&
            std_map.adaptation_notes.find("emplace") != std::string::npos,
        "std::map labels insertion-loop bulk_load as adapted with notes");
  const auto sorted_vector = make_sorted_vector_index({})->descriptor();
  check(sorted_vector.capabilities.bulk_load == Support::native &&
            sorted_vector.adaptation_notes.empty(),
        "sorted vector labels its direct sorted-row load as native");
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
               const std::filesystem::path& samples) {
  std::vector<std::string> arguments = {
      "unified-test-runner", "--rows", rows.string(), "--queries",
      queries.string(),      "--result", result.string(), "--samples",
      samples.string(),      "--repeats", "2", "--latency-samples", "3"};
  std::vector<char*> argv;
  argv.reserve(arguments.size());
  for (auto& argument : arguments) {
    argv.push_back(argument.data());
  }
  return string_index_bench::unified::run_cli(
      static_cast<int>(argv.size()), argv.data(), &make_fault_index);
}

void test_cli(const std::filesystem::path& executable_path) {
  const auto output_dir = executable_path.parent_path();
  const auto rows_path = output_dir / "unified-runner-test-rows.bin";
  const auto queries_path = output_dir / "unified-runner-test-queries.bin";
  const auto malformed_path =
      output_dir / "unified-runner-test-malformed-queries.bin";
  const auto result_path = output_dir / "unified-runner-test-result.json";
  const auto samples_path = output_dir / "unified-runner-test-samples.csv";
  const auto invalid_result_path =
      output_dir / "unified-runner-test-invalid-result.json";
  const auto invalid_samples_path =
      output_dir / "unified-runner-test-invalid-samples.csv";
  const auto collision_path =
      output_dir / "unified-runner-test-collision-output";
  const auto collision_directory =
      output_dir / "unified-runner-test-collision-directory";

  try {
    write_binary_file(rows_path, encode_rows(boundary_rows()));
    write_binary_file(queries_path, encode_queries(boundary_queries()));
    write_binary_file(malformed_path, std::string("NOTQRY1\0", 8));

    active_fault = Fault::none;
    check(invoke_cli(rows_path, queries_path, result_path, samples_path) == 0,
          "frozen CLI succeeds for a valid interchange");
    const std::string result = read_text_file(result_path);
    const std::string samples = read_text_file(samples_path);
    check(result.find("\"status\": \"passed\"") != std::string::npos,
          "CLI emits a passed terminal result");
    check(samples.find("throughput") != std::string::npos &&
              samples.find("latency") != std::string::npos,
          "CLI emits raw throughput and latency samples");

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

  for (const auto& path : {rows_path, queries_path, malformed_path, result_path,
                           samples_path, invalid_result_path,
                           invalid_samples_path, collision_path}) {
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
