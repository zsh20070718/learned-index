#include "sorted_vector_adapter.h"

#include <algorithm>
#include <set>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>

namespace {

struct UnsignedByteLess {
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

class SortedVectorIndex final : public string_index_bench::v1::Index {
 public:
  string_index_bench::v1::Descriptor descriptor() const override {
    string_index_bench::v1::Descriptor descriptor;
    descriptor.implementation = "sorted vector binary search";
    descriptor.implementation_version = "builtin-v1";
    descriptor.adapter_version = "builtin-sorted-vector-v1";
    descriptor.capabilities.bulk_load =
        string_index_bench::v1::Support::native;
    descriptor.capabilities.find = string_index_bench::v1::Support::native;
    return descriptor;
  }

  void bulk_load(
      const std::vector<string_index_bench::v1::Row>& rows) override {
    if (loaded_) {
      throw std::logic_error("bulk_load called more than once");
    }
    for (std::size_t i = 1; i < rows.size(); ++i) {
      if (!UnsignedByteLess{}(rows[i - 1].key, rows[i].key)) {
        throw std::invalid_argument(
            "bulk_load rows are not strictly unsigned-byte sorted and unique");
      }
    }
    rows_ = rows;
    loaded_ = true;
  }

  std::optional<string_index_bench::v1::Value> find(
      std::string_view key) const override {
    require_loaded();
    const auto found = std::lower_bound(
        rows_.begin(), rows_.end(), key,
        [](const string_index_bench::v1::Row& row, std::string_view candidate) {
          return UnsignedByteLess{}(row.key, candidate);
        });
    if (found == rows_.end() || UnsignedByteLess{}(key, found->key) ||
        UnsignedByteLess{}(found->key, key)) {
      return std::nullopt;
    }
    return found->value;
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
        "All byte fields are null because vector capacity, string capacity, "
        "and allocator overhead are implementation-specific; the adapter "
        "retains no harness input and owns no separate allocations.";
    return usage;
  }

 private:
  void require_loaded() const {
    if (!loaded_) {
      throw std::logic_error("find called before bulk_load");
    }
  }

  std::vector<string_index_bench::v1::Row> rows_;
  bool loaded_ = false;
};

}  // namespace

std::unique_ptr<string_index_bench::v1::Index> make_sorted_vector_index(
    const string_index_bench::v1::Parameters& parameters) {
  validate_parameters(parameters);
  return std::make_unique<SortedVectorIndex>();
}
