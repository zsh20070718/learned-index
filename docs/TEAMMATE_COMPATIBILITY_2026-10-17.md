# 同学实现与统一 benchmark 的兼容性核对（2026-10-17）

本文只回答“现有公开源码怎样接入统一评测”。benchmark 负责数据、负载、参照校验、计时和报告，不替同学设计索引算法，也不把串行测试通过解释成线程安全。

## 结论

| 来源 | 固定版本 | 可直接复用的入口 | 本轮并发判定 | 下一步 |
| --- | --- | --- | --- | --- |
| Zephyr761 / BlockLearn | [`60837a886b7040828e6f0bb5382d7950d8eed4fa`](https://github.com/Zephyr761/Learned-Index-Model/commit/60837a886b7040828e6f0bb5382d7950d8eed4fa) | `blocklearn_adapter`、`benchmark/factories.h`、`make_block_residual_delta`；静态主比较可改选 `make_block_residual` | **关闭**。源码、README 和 descriptor 明确写单线程；没有并发测试或线程协议 | 以外部 checkout 接入，保留输入 `Row` 到实例析构；先跑串行，等待实现侧明确并发承诺 |
| czr112 / Context16 | [`564df24461e70cc0a4270351a42d807a8f9902a9`](https://github.com/czr112/string-learned-index/commit/564df24461e70cc0a4270351a42d807a8f9902a9) | `our_index_adapter`、`our_index_adapter.h`、`make_our_index_index` | **关闭**。未找到锁、原子同步、线程注册或并发测试；候选路由还会在 `const find` 中懒更新 | 只接默认 Context16 与冻结开关；记录实际 Git/内容摘要，不能信任当前陈旧的自报摘要 |
| 官方 XIndex-R 上游 | [`7098dd9642afc7c94f0bb486890f72bee49349cb`](https://ipads.se.sjtu.edu.cn:1312/opensource/xindex/-/commit/7098dd9642afc7c94f0bb486890f72bee49349cb) | `XIndex(keys, values, worker_num, bg_n)` 与 `get(key,value,worker_id)` | **有条件可做，但不能套现有布尔能力**。每个前台线程必须有稳定且唯一的 worker ID；有全局 RCU 状态与后台线程生命周期 | 先设计线程上下文/退出协议和进程隔离；只做固定宽度键的点查原型。当前证据不能代表潘梓睿的实现 |

两个同学仓库已经自带 `string-index/v1` 适配包，无需重写算法或复制源码。两份 CMake 都接受 `STRING_INDEX_CONTRACT_INCLUDE_DIR`，工厂头也只包含 `index_contract.h`；让生成的顶层工程在 `add_subdirectory` 前指向本 benchmark 的 `src/`，即可让 runner 与适配器使用同一份、含新增 `Capabilities::concurrent_find` 的接口，避免两份类定义造成 ODR/ABI 风险。[BlockLearn CMake](https://github.com/Zephyr761/Learned-Index-Model/blob/60837a886b7040828e6f0bb5382d7950d8eed4fa/src/CMakeLists.txt#L1-L27)；[Context16 CMake](https://github.com/czr112/string-learned-index/blob/564df24461e70cc0a4270351a42d807a8f9902a9/07_Eval_Integration/adapters/our_index/CMakeLists.txt)

两仓库根目录都没有 `LICENSE` / `COPYING`。本项目可从用户提供的外部 checkout 做评测，但不应把实现源码复制、改写或再发布到 benchmark 仓库；公开可读不等于得到再分发许可。官方 XIndex-R 则带 [Apache-2.0 许可](https://ipads.se.sjtu.edu.cn:1312/opensource/xindex/-/blob/7098dd9642afc7c94f0bb486890f72bee49349cb/XIndex-R/LICENCE#L1-5)。

## Zephyr761 / BlockLearn

原链接 `Zephyr761/Learned-Index` 当前重定向到 `Learned-Index-Model`。固定提交的公开 v1 接口使用 `uint64_t` value、拥有字符串的 `Row`，并提供 `bulk_load`、`find`、`insert/update/erase`、`scan`、`range_sum` 和 `memory_usage`。[接口定义](https://github.com/Zephyr761/Learned-Index-Model/blob/60837a886b7040828e6f0bb5382d7950d8eed4fa/src/benchmark/contract/index_contract.h#L18-L103)；[工厂符号](https://github.com/Zephyr761/Learned-Index-Model/blob/60837a886b7040828e6f0bb5382d7950d8eed4fa/src/benchmark/model_factories.cpp#L1-L44)

键按无符号字节字典序比较，支持空键、NUL、高位字节和前缀键，没有声明长度上限；记录 ID 为 32 位，因此记录数上限是 `UINT32_MAX`。`bulk_load` 只允许调用一次，输入必须严格递增且唯一。实现借用传入的 `vector<Row>`，所以适配器不能在 build 返回后释放或移动它；插入键另存于稳定的 `deque`。[字节序](https://github.com/Zephyr761/Learned-Index-Model/blob/60837a886b7040828e6f0bb5382d7950d8eed4fa/src/key_bytes.h#L10-L34)；[build 与借用关系](https://github.com/Zephyr761/Learned-Index-Model/blob/60837a886b7040828e6f0bb5382d7950d8eed4fa/src/learned_index.cpp#L158-L186)

四个工厂的用途需要区分：

- `make_block_plain`、`make_block_hash`、`make_block_residual` 是静态/消融入口，不支持 insert；静态主比较优先 `make_block_residual`。
- `make_block_residual_delta` 支持同步 insert，可参加后续写入负载；update/erase 仍不支持。
- 当前公开 manifest 已给出 `blocklearn_adapter` / `benchmark/factories.h` / `make_block_residual_delta`，但 `source_identity` 仍是 `source-uncommitted`，结果必须另记实际 Git 提交。[manifest](https://github.com/Zephyr761/Learned-Index-Model/blob/60837a886b7040828e6f0bb5382d7950d8eed4fa/src/experiments/block-residual-delta/benchmark-adapter.json)

构建边界为 CMake 3.16、C++17；BlockLearn 核心不需要外部包，ART/HOT 是可选基线。标签探测有标量、ARM NEON、x86 AVX2 路径；启用 native HOT 时上游 CMake 会把 `-mavx2` 也加到核心目标，正式比较要记录实际 ISA。[构建目标](https://github.com/Zephyr761/Learned-Index-Model/blob/60837a886b7040828e6f0bb5382d7950d8eed4fa/src/CMakeLists.txt#L1-L67)

并发不能打开。README、设计文档和 descriptor 都把当前实现定义为单线程；没有线程注册、锁、原子同步或并发测试。更具体地，`instrument=1` 时 `const find` 会修改非原子的 `mutable Statistics`，并行读会发生数据竞争；`instrument=0` 看起来只读，也没有形成可依赖的线程安全契约。[单线程声明](https://github.com/Zephyr761/Learned-Index-Model/blob/60837a886b7040828e6f0bb5382d7950d8eed4fa/README.md#L23-L39)；[状态字段](https://github.com/Zephyr761/Learned-Index-Model/blob/60837a886b7040828e6f0bb5382d7950d8eed4fa/src/learned_index.h#L46-L99)；[查询中的统计更新](https://github.com/Zephyr761/Learned-Index-Model/blob/60837a886b7040828e6f0bb5382d7950d8eed4fa/src/learned_index.cpp#L188-L270)

## czr112 / Context16

固定提交已经提供完整薄适配器：

```text
07_Eval_Integration/adapters/our_index/
  benchmark-adapter.json
  CMakeLists.txt
  our_index_adapter.h      -> make_our_index_index(...)
  our_index_adapter.cpp
```

它把统一 `Row` 一次性复制成原生 `Pair{string,uint64_t}` 后调用 `validation::Index::bulk_load`，build 结束即释放转换缓冲；查询 `string_view` 只在调用期间使用，索引内部拥有持久键，scan 返回拥有字符串的结果。默认 Context16 支持空键、NUL、高位字节、前缀关系和完整 `uint64_t` 值域；LitsHpt 是另一可选后端，限制为无 NUL、无高位字节且最长 4096 字节，不能与默认能力混报。[适配器能力与生命周期](https://github.com/czr112/string-learned-index/blob/564df24461e70cc0a4270351a42d807a8f9902a9/07_Eval_Integration/adapters/our_index/our_index_adapter.cpp#L190-L338)

原生接口包含 `bulk_load/find/insert/update/erase/upsert/scan/size/memory`；统一适配器把 build/find/insert/update/erase/scan 声明为 native，`range_sum` 明确 unsupported。默认 `Config` 与适配包均选择 Context16；LitsHpt、BlockLinearRank、EqualDepthDir、EqualDepthDirTrunc 及 routing/reclaim 等候选没有成为默认项。独立 `StaticPhysicalIndex` 是只读实验类型，也没有替换 Context16。[原生入口](https://github.com/czr112/string-learned-index/blob/564df24461e70cc0a4270351a42d807a8f9902a9/02_Code_and_Experiments/validation_e25_20260918/index.hpp#L6037-L6125)；[默认与候选开关](https://github.com/czr112/string-learned-index/blob/564df24461e70cc0a4270351a42d807a8f9902a9/02_Code_and_Experiments/validation_e25_20260918/switches.hpp#L29-L103)

Context16 的更新存储路径具体为：region 内有预留主块与向下增长的 suffix arena；insert 先尝试主块，必要时同步 compact，再落入拥有字符串的有序 overflow；前缀不匹配会同步重建 region，容量超过阈值会同步 split/rebuild。这些是同步更新路径，不是后台维护、无锁发布或有界增量空间。[region 存储](https://github.com/czr112/string-learned-index/blob/564df24461e70cc0a4270351a42d807a8f9902a9/02_Code_and_Experiments/validation_e25_20260918/index.hpp#L1354-L1374)；[insert/rebuild 路径](https://github.com/czr112/string-learned-index/blob/564df24461e70cc0a4270351a42d807a8f9902a9/02_Code_and_Experiments/validation_e25_20260918/index.hpp#L6752-L6797)

构建使用 CMake 3.16、C++17、64 位 GCC/Clang 风格工具链、`-O3`、保留断言和 `-march=x86-64-v3`。Context16 无需另装运行库，但 `index.hpp` 无条件包含可选模型头，因此固定源码目录里的 model/vendor 头仍须存在。比较结果应记录这个 ISA，而不是把它隐去。[适配包 CMake](https://github.com/czr112/string-learned-index/blob/564df24461e70cc0a4270351a42d807a8f9902a9/07_Eval_Integration/adapters/our_index/CMakeLists.txt)

并发仍须关闭。已检查的动态索引、静态索引、适配器和验收程序中没有线程注册、锁、原子同步或并发 harness。默认 build 后的读取路径从源码看大体为只读，但这不是线程安全证据；可选 `compact_region_routing` 还会在 `const locate/find` 中懒重建可变 vector，尤其不能直接并发。[默认查询入口](https://github.com/czr112/string-learned-index/blob/564df24461e70cc0a4270351a42d807a8f9902a9/02_Code_and_Experiments/validation_e25_20260918/index.hpp#L6037-L6059)

版本身份还有一个实质问题：固定提交的 `index.hpp` 实际 SHA-256 是 `c8a1ae3c7373714842f724887a6f45eea7ce5ab7b6e9cdbd677ab87fed0e7265`，manifest 的机器字段仍写 `61592294...`，运行时 descriptor 写 `b20c83ca...`。长说明虽提到新摘要，机器可读身份和真实字节仍不一致。接入时保留原声明用于审计，同时必须增加实际 checkout Git 提交、修改状态和内容摘要；独立 static 包在 CMake 配置阶段计算摘要的做法更可靠。[陈旧 manifest 字段](https://github.com/czr112/string-learned-index/blob/564df24461e70cc0a4270351a42d807a8f9902a9/07_Eval_Integration/adapters/our_index/benchmark-adapter.json#L8-L15)；[陈旧 descriptor 字符串](https://github.com/czr112/string-learned-index/blob/564df24461e70cc0a4270351a42d807a8f9902a9/07_Eval_Integration/adapters/our_index/our_index_adapter.cpp#L61-L66)

## 潘梓睿 / XIndex 的证据边界

没有提供潘梓睿自己的仓库、提交、API 或许可证，因此不能把官方 XIndex 上游的状态写成他的实现进度。以下内容只用于确定未来适配器需要的协议。论文为 [PPoPP 2020 原文](https://chuzhe.me/assets/pdf/2020%20-%20XIndex-%20A%20Scalable%20Learned%20Index%20for%20Multicore%20Data%20Storage.pdf)，官方源码固定在上表提交。

XIndex-R 的核心入口为：

```cpp
XIndex(const std::vector<key_t>& keys,
       const std::vector<val_t>& values,
       std::size_t worker_num,
       std::size_t bg_n);
bool get(const key_t& key, val_t& value, std::uint32_t worker_id);
```

`put/remove/scan/range_scan` 同样接收 `worker_id`，但 `range_scan` 的底层实现会直接报“not implemented yet”，因此首个适配器只能承诺 point get。[公开 API](https://ipads.se.sjtu.edu.cn:1312/opensource/xindex/-/blob/7098dd9642afc7c94f0bb486890f72bee49349cb/XIndex-R/xindex.h#L35-54)；[range_scan 限制](https://ipads.se.sjtu.edu.cn:1312/opensource/xindex/-/blob/7098dd9642afc7c94f0bb486890f72bee49349cb/XIndex-R/xindex_root_impl.h#L214-248)

这里的线程参数不能丢掉。`worker_num` 决定进程级 RCU 槽数组大小；每个并发前台线程都要持有唯一、稳定、范围内的 ID，官方 microbench 采用 `[0, fg_n)` 的稠密编号。源码没有 register/unregister API，也没有可以让现有 `find(string_view) const` 自动补齐的 TLS 上下文。[RCU 槽](https://ipads.se.sjtu.edu.cn:1312/opensource/xindex/-/blob/7098dd9642afc7c94f0bb486890f72bee49349cb/XIndex-R/xindex_util.h#L48-123)；[官方线程编号](https://ipads.se.sjtu.edu.cn:1312/opensource/xindex/-/blob/7098dd9642afc7c94f0bb486890f72bee49349cb/XIndex-R/microbench.cpp#L149-273)

构造函数总会启动 master pthread，`bg_n>0` 再启动维护线程；把 `bg_n=0` 用作只读模式只是从控制流推得，并非 README 承诺。析构只设置退出标志，没有 join master 或释放 root；全局 RCU/配置状态也让重复的进程内实例不稳妥。当前 runner 在一个配置中会先后创建校验、吞吐和延迟实例，因此只隔离整个 CLI 进程仍不能解决重复实例问题。接入前必须先补齐并复核 teardown/全局状态重置，或把**每个实例生命周期**放进独立子进程；所有前台线程均应先停止并 join。[后台线程与析构](https://ipads.se.sjtu.edu.cn:1312/opensource/xindex/-/blob/7098dd9642afc7c94f0bb486890f72bee49349cb/XIndex-R/xindex_impl.h#L107-222)

键类型还必须提供 `min/max/model_key_size/to_model_key` 和全序比较；官方例子是 `uint64_t`。不能把任意字符串哈希成 `uint64_t` 后声称仍符合字符串有序索引语义，因为碰撞和次序都会改变。可接受的首版是固定宽度整数键，或另有证明的无碰撞保序编码。构建依赖 C++14、Intel MKL、pthread、jemalloc 与 x86/GCC 风格代码；本轮没有安装这些依赖或执行 XIndex。[键包装示例](https://ipads.se.sjtu.edu.cn:1312/opensource/xindex/-/blob/7098dd9642afc7c94f0bb486890f72bee49349cb/XIndex-R/microbench.cpp#L75-111)；[构建文件](https://ipads.se.sjtu.edu.cn:1312/opensource/xindex/-/blob/7098dd9642afc7c94f0bb486890f72bee49349cb/XIndex-R/CMakeLists.txt#L1-L52)

## 已有证据与尚未证明的内容

源码审查证实了固定提交、函数签名、键/值域、输入与结果所有权、构建依赖、默认配置、显式/缺失的线程协议以及许可证状态。审查没有重跑两个仓库自报的 ART/HOT 百分比、消融实验或 sanitizer 结果，也没有证明未加同步的 `const find` 可并发。

本地接入验证由 benchmark 主任务另行执行并记录：两个固定 checkout 在三套真实键 × 三种分布下共 18 个单线程组合通过；请求 2/4 线程的 36 个组合全部明确为 `unsupported`，没有生成伪并行性能。命令、路径与 CSV 见 [同学实现接入说明](TEAMMATE_INTEGRATION.md) 和 `docs/experiments/peer-integration-2026-10-17.csv`。这证明现有适配包能进入统一的静态点查流程，并不证明索引对所有操作、所有数据或多线程调度均正确。

执行顺序因此固定为：先从外部 checkout 使用现成 manifest；由顶层 CMake 强制共享当前 contract；串行完成键域、命中/未命中与结果校验；`concurrent_find` 默认 false；只有实现侧给出明确线程承诺并通过共享实例并发正确性检查后，才进入线程扩展测量。XIndex 类实现还需要先扩充线程上下文与生命周期接口，不能只加一个布尔值。
