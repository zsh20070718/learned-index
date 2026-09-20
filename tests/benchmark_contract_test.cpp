#include <functional>
#include "std_map.h"
#include "checked_index.h"
#include "observed_index.h"

void Require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}

template <class Action>
void RequireRejection(Action action, const char* message) {
  bool caught = false;
  try { action(); } catch (const std::runtime_error&) { caught = true; }
  Require(caught, message);
}

// Faulty adapters exist only to test the benchmark's verification layer.
class DropsInsert : public StdMap {
 public:
  using StdMap::StdMap;
  void Insert(const KeyValue<std::string>&, uint32_t) {}
};

class CorruptsOldKey : public StdMap {
  bool corrupt_ = false;
 public:
  using StdMap::StdMap;
  void Insert(const KeyValue<std::string>& row, uint32_t thread) {
    StdMap::Insert(row, thread);
    corrupt_ = true;
  }
  size_t EqualityLookup(const std::string& key, uint32_t thread) const {
    return corrupt_ && key == "old" ? util::NOT_FOUND : StdMap::EqualityLookup(key, thread);
  }
};

class FalseHit : public StdMap {
 public:
  using StdMap::StdMap;
  size_t EqualityLookup(const std::string&, uint32_t) const { return 123; }
};

class WrongRange : public StdMap {
 public:
  using StdMap::StdMap;
  uint64_t RangeQuery(const std::string&, const std::string&, uint32_t) const { return 123; }
};

int main() {
  try {
    CheckedIndex<StdMap> good({});
    good.Build({{"old", 1}}, 1);
    good.Insert({"new", 2}, 0);
    Require(good.EqualityLookup("new", 0) == 2, "valid insert rejected");
    Require(good.EqualityLookup("missing", 0) == util::NOT_FOUND, "valid miss rejected");
    Require(good.RangeQuery("new", "old", 0) == 3, "valid range rejected");
    Require(good.size() > 0, "valid final state rejected");

    CheckedIndex<DropsInsert> drops({});
    drops.Build({{"old", 1}}, 1);
    RequireRejection([&] { drops.Insert({"new", 2}, 0); }, "missed dropped insert");
    CheckedIndex<CorruptsOldKey> corrupts({});
    corrupts.Build({{"old", 1}}, 1);
    corrupts.Insert({"new", 2}, 0);
    RequireRejection([&] { corrupts.size(); }, "missed final-state corruption");
    CheckedIndex<FalseHit> false_hit({});
    false_hit.Build({{"old", 1}}, 1);
    RequireRejection([&] { false_hit.EqualityLookup("missing", 0); }, "missed false hit");
    CheckedIndex<WrongRange> wrong_range({});
    wrong_range.Build({{"old", 1}}, 1);
    RequireRejection([&] { wrong_range.RangeQuery("old", "old", 0); }, "missed wrong range sum");

    ObservedIndex<StdMap> observed({});
    observed.Build({{"old", 1}}, 1);
    Require(observed.EqualityLookup("old", 0) == 1, "observer changed point result");
    Require(observed.RangeQuery("old", "old", 0) == 1, "observer changed range result");
    std::cout << "PASS benchmark oracle, insert/final-state checks and result observer\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "FAIL: " << error.what() << '\n';
    return 1;
  }
}
