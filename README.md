# 字符串学习型索引 Benchmark

张圣皓负责的字符串内存索引 benchmark 工作区。以 [TLI](https://github.com/curtis-sun/TLI) 和 [GRE](https://github.com/gre4index/GRE) 为参考，提供可复现的开发环境、外部数据与实现的接入入口，以及小数据测试。

项目定位是**数据集与字符串索引实现之间的评测层**：用户指定数据集位置和实现位置，运行统一脚本，得到详细评测结果。数据来源与实现均独立于本项目；我们负责接入、负载、正确性校验、公平测量和结果复现，不自行设计索引算法，也不对用户原始数据设统一键长或字节数上限。

这两侧的规定已写入 [数据接入约定](docs/DATASET_CONTRACT.md) 和 [实现接口契约](docs/INDEX_CONTRACT.md)，实现接口的 C++17 签名在 [index_contract.h](src/index_contract.h)。**统一入口支持静态点查询、共享实例的并发读取，以及单线程增删改查与扫描混合负载**，可接入外部数据和 C++17 适配包，比较均匀、热点、Zipf 访问，并提供独立校验和详细报告。使用方式与当前边界见 [统一入口说明](docs/UNIFIED_BENCHMARK.md)，本周改进见 [2026-10-17 改进记录](docs/WEEKLY_UPDATE_2026-10-17.md)，上一版见 [2026-10-11 记录](docs/WEEKLY_UPDATE_2026-10-11.md)。完整目标见 [评测范围](docs/BENCHMARK.md#产品目标与两侧边界)。

## 快速开始：统一入口

需要 Python 3.9+、CMake 和支持 C++17 的编译器；本例无需下载 TLI/GRE、安装 Boost 或准备专用二进制数据：

```bash
python3 -B scripts/benchmark.py \
  --dataset examples/keys.txt \
  --implementation adapters/std_map \
  --implementation adapters/sorted_vector \
  --output results/my-first-evaluation
```

把数据和适配包路径换成自己的位置即可运行；输出目录必须尚不存在。先看生成的 `report.md` 中文报告，再按需查看 `report.json`、`summary.csv` 和 `samples.csv`。两个自带基线用于验证接入，完整参数见 `python3 -B scripts/benchmark.py --help` 和 [使用说明](docs/UNIFIED_BENCHMARK.md)。

评测层还提供三份可直接使用的访问场景。把 `--workload examples/workloads/hotspot.json` 加入上述命令即可测热点访问；`uniform.json` 和 `zipf.json` 分别对应均匀和 Zipf 访问。三份配置使用相同的查询数、种子、未命中率和重复次数，便于在相同数据及实现上逐项对照。原始数据和索引源码无需修改。

## 多线程只读与扩展性对照

在统一入口加上 `--threads 1 2 4`，即可在同一数据和查询上测量共享索引实例的并发读取：

```bash
python3 -B scripts/benchmark.py \
  --dataset examples/keys.txt \
  --implementation adapters/std_map \
  --implementation adapters/sorted_vector \
  --workload examples/workloads/parallel-read.json \
  --threads 1 2 4 \
  --output results/parallel-read
```

每轮总操作数固定，各线程均分查询；每个适配器只编译一次，各配置依次运行。两个自带基线已声明并发读取能力，外部实现需显式设置 `concurrent_find = true`；未声明的实现仍能单线程运行，并行组合记为 `unsupported`。`--threads` 控制查询线程，`--jobs` 控制编译并行度。

`report.md`、`summary.csv` 和新增的 `scaling.csv` 给出吞吐量、相对单线程的加速比和并行效率；`workers.csv` 保留逐线程操作数与计时。延迟来自独立单线程抽样，不代表并发尾延迟。并发实验采用静态读取；单线程写入和扫描另见下方混合场景。完整口径、接入例子和限制见 [并行评测说明](docs/PARALLEL_BENCHMARK.md)。

两个同学的现有模型适配包也已完成固定版本的静态查询接入实测，接入方式与并发边界见 [同学实现接入说明](docs/TEAMMATE_INTEGRATION.md)；源码核对见 [兼容性报告](docs/TEAMMATE_COMPATIBILITY_2026-10-17.md)。三类真实键 × 三种访问分布的基线扩展性结果保留在 [结果 CSV](docs/experiments/parallel-read-2026-10-17.csv)。

## 混合读写、扫描与模型参数

用同一入口选择 `mixed-all.json`，即可依次评测查询、插入、更新、删除和扫描：

```bash
python3 -B scripts/benchmark.py \
  --dataset examples/keys.txt \
  --implementation adapters/std_map \
  --implementation adapters/sorted_vector \
  --workload examples/workloads/mixed-all.json \
  --output results/mixed-all
```

另有读插、更新删除、带扫描读插和纯扫描四份配置。操作先经独立参照逐项校验，并检查写后读回及最终状态，再从相同初始状态重复测量。std-map 支持全部五种操作，sorted-vector 支持静态查询和扫描；不支持的组合保留状态。动态混合目前按单线程执行。

`--parameter 实现ID:参数名=值` 可重复传递外部适配器已有的模型参数，静态和混合场景均适用；参数随结果记录。完整配置、计时口径与同学模型能力见 [混合评测说明](docs/MIXED_BENCHMARK.md)。三类真实键及字节边界数据的 [80 个混合组合](docs/experiments/mixed-operations-2026-10-17.csv) 已完成实测，各自能力范围内通过。

## 旧 TLI 开发入口

以下 setup、smoke、负载生成和真实数据示例属于保留的 TLI 开发入口，包含 ART 和旧 StdMap。它们的格式和规模限制不作为新统一入口的通用限制。

已验证环境：Ubuntu 24.04 x86-64、GCC 13、CMake 3.28、Python 3.13。需要 `git`、`g++`、`cmake`、`ninja`、Python 3.9+；构建使用 C++17 和本机指令集。默认双任务编译，小规模 smoke 单线程运行。

```bash
git clone https://github.com/zsh20070718/learned-index.git
cd learned-index
python3 -B scripts/setup.py
ctest --test-dir build --output-on-failure
```

`setup.py` 将固定版本的 TLI/GRE 拉到 `upstream/`，检查现有源码版本，准备 Boost 头文件并编译。Ubuntu 24.04 缺少 Boost 时仅下载发行版开发包并解压至 `.deps/`，不执行系统安装；已有系统 Boost 也可使用。GRE 保留为参考源码，其子模块及完整程序不在默认构建范围内。

环境已准备好后，离线重建：

```bash
python3 -B scripts/setup.py --offline --jobs 2
```

Smoke 会生成两类确定性合成字符串，各 10,000 个键；对 8 种原有负载和 7 种点查询负载分别运行 ART 和 StdMap，共 60 个组合。新增负载覆盖均匀访问、50%/100% 未命中、热点及 Zipf 偏斜访问。每个组合先执行独立校验，再执行 2,000 次操作的吞吐量测试。运行记录、带表头的 `summary.csv`、逐轮 `samples.csv` 和带输入哈希的版本清单保存在 `results/smoke-*/`；最近一次通过的路径见 `results/latest-smoke.txt`。CTest 另有校验器故障注入、负载生成器及真实数据预处理测试。

这些小数据结果用于确认环境和执行链可用，不能据此得出普遍性能结论。`--verify` 使用独立 map 参照检查查询、每次插入后读回，以及操作完成后所有预期键的值；校验不会进入吞吐量计时路径。

## 重复测量

运行一个有重复轮次的本地对照实验：

```bash
python3 -B scripts/smoke.py --keys 50000 --operations 10000 --repeats 5
```

`summary.csv` 的构建时间和吞吐量取中位数，并保留吞吐量最小值、最大值和标准差。每轮都重建索引；原始样本在 `samples.csv`。这仍是未绑核的合成数据实验，不能替代真实数据和受控环境下的正式比较。

## 自己运行一个负载

以下命令在仓库根目录执行，首次运行时使用尚不存在的数据文件名：

```bash
python3 -B scripts/make_dataset.py data/demo_string --shape prefix --count 10000
./build/generate data/demo_string 2000 --scan-ratio 0.025 --insert-ratio 0.5 --mix
./build/string_benchmark data/demo_string data/demo_string_ops_2K_0.025000rq_0.000000nl_0.500000i_0m_mix --only ART --verify
./build/string_benchmark data/demo_string data/demo_string_ops_2K_0.025000rq_0.000000nl_0.500000i_0m_mix --only ART --through --csv
```

将 `--only ART` 改成 `--only StdMap` 可运行对照。TLI 的吞吐量模式不执行校验，因此入口拒绝同时传入 `--through --verify`。原始 CSV 沿用上游的无表头格式；列定义见 [指标说明](docs/BENCHMARK.md)。

## 未命中与偏斜点查询

上游生成器的字符串分支会忽略未命中比例。使用本地负载生成器可以控制这部分，生成的二进制文件直接交给同一个 benchmark：

```bash
python3 -B scripts/make_lookup_workload.py data/demo_string 2000 --miss-ratio 0.5 --distribution hotspot
./build/string_benchmark data/demo_string data/demo_string_ops_2000_0.000000rq_0.500000nl_0.000000i_hotspot_s42_hf0.1_hp0.9 --only ART --verify
```

`--distribution uniform` 按键均匀抽样；`hotspot` 默认以 90% 概率访问字节排序后前 10% 的源键，其余访问冷区。`--hotset-fraction`、`--hot-probability` 和 `--seed` 可调整这些设置。未命中次数为 `floor(操作数 × --miss-ratio)`，生成的缺失键经过完整数据集成员检查；它们由源键后缀变异产生，代表一种合成未命中模式。

`--distribution zipf --zipf-exponent 1.1` 按有限 Zipf 分布查询，字节排序后的第 r 个键权重为 `r^-exponent`；指数范围 0–3，0 为均匀访问。该排名是可控实验设置，不是从真实访问日志估计的热度。

每份负载旁的 `.json` 记录实际命中/未命中数量、热点源键访问次数、唯一查询键数及数据/负载哈希。缺失键的热点指标描述它来自哪个源键，不表示这个缺失键属于已有数据。当前新生成器只支持静态点查询；原有插入和区间负载继续使用 TLI。单份输入上限 128 MiB，输出保守估计上限 256 MiB；比例最多 128 位十进制数字、指数绝对值最多 256，避免精确计算引入超大整数。已有输出不会覆盖。

## 原项目关联的真实数据

下载入口沿用 TLI 内嵌 Wormhole README 的词表链接及其 benchmark 记录中的 MemeTracker 来源，版本、大小、SHA-256 和出处锁定在 `datasets.lock.json`。约 5 MB 英文词表和 230 MB MemeTracker 压缩文件完整下载到 `data/raw/`，提取词条、URL、自然语言短语三类键：

```bash
python3 -B scripts/prepare_real_datasets.py --count 10000
python3 -B scripts/smoke.py \
  --dataset data/real/english_words_10000_s42_string \
  --dataset data/real/memetracker_urls_10000_s42_string \
  --dataset data/real/memetracker_phrases_10000_s42_string
```

这会保留两组合成对照，共运行 **5 类数据 × 15 种负载 × 2 个索引 = 150 组**。所有数据键数须与 `--keys` 一致。每次运行均归档来源、样本哈希、键长及相邻最长公共前缀统计；CSV 的 `key_shape` 列也是数据集标识，可与 manifest 的 `datasets` 对应。

预处理扫描完整源文件，以固定种子 hash 选择唯一键样本，不取词典开头，也不按 URL 在语料中出现次数加权。每类默认 10,000 个键，支持 200–100,000；保留原字节，过滤并计数空键、NUL、超过 4096 字节的键。MemeTracker 使用 phrase-cluster 版本，**不是 TLI 原始的 90M URL 文件**，也不是完整月份语料。

已有 raw 文件复用前校验哈希；`--offline` 禁止下载。已有样本不覆盖，重新采样可选新的 `--seed`、`--count` 或 `--output-dir`。仅取词表可加 `--datasets english_words`。来源边界和处理规则见 [数据集说明](docs/DATASETS.md)。

## 导入真实文本数据

真实文本数据可转换为 TLI 的二进制字符串格式：

```bash
python3 -B scripts/make_dataset.py data/sample_string --input /path/to/strings.txt --count 10000
```

每行一个键，保留行内空格，去除 LF/CRLF 行尾；按字节排序、去重，拒绝空键、NUL 和超过 4096 字节的键。`--count` 限制读取行数，默认 10,000，最多 1,000,000；不会自动下载真实数据集。运行上游生成器时，插入操作数必须小于数据中的键数，并为查询保留足够的初始键。

## 目录

| 路径 | 用途 |
| --- | --- |
| `scripts/benchmark.py` | 统一评测入口：数据读取、静态/混合负载、校验、测量与报告 |
| `src/unified/` | 使用 v1 接口的独立 C++17 评测程序 |
| `adapters/` | map 与排序数组的示例适配包 |
| `examples/workloads/` | 静态均匀/热点/Zipf、多线程读取及五类混合场景配置 |
| `docs/UNIFIED_BENCHMARK.md` | 统一入口使用说明与覆盖边界 |
| `src/string_benchmark.cpp` | TLI 字符串执行入口，索引选择与错误返回 |
| `src/std_map.h` | 与 TLI 接口兼容的对照索引 |
| `src/checked_index.h` | 独立校验适配器、插入及最终键值读回 |
| `src/observed_index.h` | 消费查询结果，防止计时路径被编译器消除 |
| `tests/benchmark_contract_test.cpp` | benchmark 校验器的故障注入测试 |
| `scripts/setup.py` | 固定版本源码、本地依赖、受限并行构建 |
| `scripts/make_dataset.py` | 合成字符串或转换文本数据 |
| `scripts/prepare_real_datasets.py` | 原项目关联数据的下载、校验、抽样及形态统计 |
| `datasets.lock.json` | 真实数据来源、版本、大小与 SHA-256 |
| `scripts/make_lookup_workload.py` | 均匀/热点/Zipf 点查询、可控未命中及实际负载元数据 |
| `tests/lookup_workload_test.py` | 二进制格式、成员关系、比例与复现检查 |
| `scripts/smoke.py` | 小规模校验、吞吐量测试、结果归档 |
| `upstream.lock.json` | TLI/GRE 源码提交锁定 |
| `docs/BENCHMARK.md` | 实验语义、当前限制、后续接入任务 |

`upstream/`、`.deps/`、`build/`、`data/`、`results/` 和本地工作日志均不提交。附件、原始对话和真实数据也不放入公开仓库。

## 来源与许可

TLI：*Testbed for Learned Indexes*；GRE：*Are Updatable Learned Indexes Ready?*, PVLDB 2022。具体来源、版本和许可边界见 [NOTICE](NOTICE.md)。本仓库原创代码采用 GPL-3.0，见 [LICENSE](LICENSE)。
