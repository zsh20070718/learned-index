#pragma once

#include <map>
#include "competitors/base.h"
#include "utils/tracking_allocator.h"

// Same key -> original row offset and inclusive range-sum contract as TLI ART.
class StdMap : public Base<std::string> {
  using Pair = std::pair<const std::string, uint64_t>;
  uint64_t allocated_ = 0;
  std::map<std::string, uint64_t, std::less<std::string>, TrackingAllocator<Pair>> map_;

 public:
  explicit StdMap(const std::vector<int>&)
      : map_(TrackingAllocator<Pair>(allocated_)) {}
  uint64_t Build(const std::vector<KeyValue<std::string>>& data, size_t) {
    return util::timing([&] {
      for (const auto& item : data) map_.emplace_hint(map_.end(), item.key, item.value);
    });
  }
  size_t EqualityLookup(const std::string& key, uint32_t) const {
    const auto it = map_.find(key);
    return it == map_.end() ? util::NOT_FOUND : it->second;
  }
  uint64_t RangeQuery(const std::string& lo, const std::string& hi, uint32_t) const {
    uint64_t sum = 0;
    for (auto it = map_.lower_bound(lo); it != map_.end() && it->first <= hi; ++it)
      sum += it->second;
    return sum;
  }
  void Insert(const KeyValue<std::string>& item, uint32_t) {
    map_.emplace(item.key, item.value);
  }
  std::string name() const { return "StdMap"; }
  size_t size() const { return allocated_; }
  bool applicable(bool unique, bool, bool, bool multithread, const std::string&) const {
    return unique && !multithread;
  }
};
