#include "std_map_adapter.h"

#include <algorithm>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>

namespace {

struct UnsignedByteLess {
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

void validate_parameters(
    const string_index_bench::v1::Parameters& parameters) {
  std::set<std::string> names;
  for (const auto& parameter : parameters) {
    if (!names.insert(parameter.first).second) {
      throw std::invalid_argument("duplicate parameter: " + parameter.first);
    }
  }
  for (const auto& parameter : parameters) {
    throw std::invalid_argument("unknown parameter: " + parameter.first);
  }
}

class StdMapIndex final : public string_index_bench::v1::Index {
 public:
  string_index_bench::v1::Descriptor descriptor() const override {
    string_index_bench::v1::Descriptor descriptor;
    descriptor.implementation = "C++ standard ordered map";
    descriptor.implementation_version = "c++17";
    descriptor.adapter_version = "builtin-std-map-v1";
    descriptor.capabilities.bulk_load =
        string_index_bench::v1::Support::adapted;
    descriptor.capabilities.find = string_index_bench::v1::Support::native;
    descriptor.adaptation_notes =
        "bulk_load is adapted by inserting each sorted input row into "
        "std::map with emplace; all insertion work is included in build time.";
    return descriptor;
  }

  void bulk_load(
      const std::vector<string_index_bench::v1::Row>& rows) override {
    if (loaded_) {
      throw std::logic_error("bulk_load called more than once");
    }
    loaded_ = true;
    for (const auto& row : rows) {
      const auto inserted = values_.emplace(row.key, row.value);
      if (!inserted.second) {
        throw std::invalid_argument("bulk_load rows contain a duplicate key");
      }
    }
  }

  std::optional<string_index_bench::v1::Value> find(
      std::string_view key) const override {
    require_loaded();
    const auto found = values_.find(key);
    if (found == values_.end()) {
      return std::nullopt;
    }
    return found->second;
  }

  string_index_bench::v1::InsertResult insert(
      std::string_view, string_index_bench::v1::Value) override {
    throw string_index_bench::v1::UnsupportedOperation("insert unsupported");
  }

  string_index_bench::v1::UpdateResult update(
      std::string_view, string_index_bench::v1::Value) override {
    throw string_index_bench::v1::UnsupportedOperation("update unsupported");
  }

  string_index_bench::v1::EraseResult erase(std::string_view) override {
    throw string_index_bench::v1::UnsupportedOperation("erase unsupported");
  }

  std::vector<string_index_bench::v1::Row> scan(std::string_view,
                                                 std::size_t) const override {
    throw string_index_bench::v1::UnsupportedOperation("scan unsupported");
  }

  string_index_bench::v1::Value range_sum(std::string_view,
                                           std::string_view) const override {
    throw string_index_bench::v1::UnsupportedOperation(
        "range_sum unsupported");
  }

  string_index_bench::v1::MemoryUsage memory_usage() const override {
    string_index_bench::v1::MemoryUsage usage;
    usage.accounting_notes =
        "All byte fields are null because std::map node allocation, allocator "
        "overhead, and string capacity are implementation-specific; the "
        "adapter retains no harness input and owns no separate allocations.";
    return usage;
  }

 private:
  void require_loaded() const {
    if (!loaded_) {
      throw std::logic_error("find called before bulk_load");
    }
  }

  std::map<std::string, string_index_bench::v1::Value, UnsignedByteLess>
      values_;
  bool loaded_ = false;
};

}  // namespace

std::unique_ptr<string_index_bench::v1::Index> make_std_map_index(
    const string_index_bench::v1::Parameters& parameters) {
  validate_parameters(parameters);
  return std::make_unique<StdMapIndex>();
}
