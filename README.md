# 字符串学习型索引 Benchmark

张圣皓负责的字符串内存索引 benchmark 工作区。以 [TLI](https://github.com/curtis-sun/TLI) 和 [GRE](https://github.com/gre4index/GRE) 为参考，先提供可复现的开发环境、小数据测试和接入入口。

项目定位是**字符串索引 benchmark**：接入已有索引，构造可复现工作负载，统一校验与测量，研究比较结果；不自行设计索引算法。当前可运行 TLI 字符串测试的单线程子集，包含上游 ART 和 `std::map` 对照。已有学习型索引的适配、更多访问分布和并发实验仍待完成，详见 [范围与扩展](docs/BENCHMARK.md)。

## 快速开始

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

Smoke 会生成两类确定性合成字符串，各 10,000 个键；对 8 种原有负载和 5 种新增点查询负载分别运行 ART 和 StdMap，共 52 个组合。新增负载覆盖均匀访问、50%/100% 未命中及热点访问。每个组合先执行独立校验，再执行 2,000 次操作的吞吐量测试。运行记录、带表头的 `summary.csv`、逐轮 `samples.csv` 和带输入哈希的版本清单保存在 `results/smoke-*/`；最近一次通过的路径见 `results/latest-smoke.txt`。CTest 另有校验器故障注入及负载生成器测试。

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

## 未命中与热点点查询

上游生成器的字符串分支会忽略未命中比例。使用本地负载生成器可以控制这部分，生成的二进制文件直接交给同一个 benchmark：

```bash
python3 -B scripts/make_lookup_workload.py data/demo_string 2000 --miss-ratio 0.5 --distribution hotspot
./build/string_benchmark data/demo_string data/demo_string_ops_2000_0.000000rq_0.500000nl_0.000000i_hotspot_s42_hf0.1_hp0.9 --only ART --verify
```

`--distribution uniform` 按键均匀抽样；`hotspot` 默认以 90% 概率访问字节排序后前 10% 的源键，其余访问冷区。`--hotset-fraction`、`--hot-probability` 和 `--seed` 可调整这些设置。未命中次数为 `floor(操作数 × --miss-ratio)`，生成的缺失键经过完整数据集成员检查；它们由源键后缀变异产生，代表一种合成未命中模式。

每份负载旁的 `.json` 记录实际命中/未命中数量、热点源键访问次数、唯一查询键数及数据/负载哈希。缺失键的热点指标描述它来自哪个源键，不表示这个缺失键属于已有数据。当前新生成器只支持静态点查询；原有插入和区间负载继续使用 TLI。单份输入上限 128 MiB，输出保守估计上限 256 MiB；比例最多 128 位十进制数字、指数绝对值最多 256，避免精确计算引入超大整数。已有输出不会覆盖。

## 导入真实文本数据

真实文本数据可转换为 TLI 的二进制字符串格式：

```bash
python3 -B scripts/make_dataset.py data/sample_string --input /path/to/strings.txt --count 10000
```

每行一个键，保留行内空格，去除 LF/CRLF 行尾；按字节排序、去重，拒绝空键、NUL 和超过 4096 字节的键。`--count` 限制读取行数，默认 10,000，最多 1,000,000；不会自动下载真实数据集。运行上游生成器时，插入操作数必须小于数据中的键数，并为查询保留足够的初始键。

## 目录

| 路径 | 用途 |
| --- | --- |
| `src/string_benchmark.cpp` | TLI 字符串执行入口，索引选择与错误返回 |
| `src/std_map.h` | 与 TLI 接口兼容的对照索引 |
| `src/checked_index.h` | 独立校验适配器、插入及最终键值读回 |
| `src/observed_index.h` | 消费查询结果，防止计时路径被编译器消除 |
| `tests/benchmark_contract_test.cpp` | benchmark 校验器的故障注入测试 |
| `scripts/setup.py` | 固定版本源码、本地依赖、受限并行构建 |
| `scripts/make_dataset.py` | 合成字符串或转换文本数据 |
| `scripts/make_lookup_workload.py` | 均匀/热点点查询、可控未命中及实际负载元数据 |
| `tests/lookup_workload_test.py` | 二进制格式、成员关系、比例与复现检查 |
| `scripts/smoke.py` | 小规模校验、吞吐量测试、结果归档 |
| `upstream.lock.json` | TLI/GRE 源码提交锁定 |
| `docs/BENCHMARK.md` | 实验语义、当前限制、后续接入任务 |

`upstream/`、`.deps/`、`build/`、`data/`、`results/` 和本地工作日志均不提交。附件、原始对话和真实数据也不放入公开仓库。

## 来源与许可

TLI：*Testbed for Learned Indexes*；GRE：*Are Updatable Learned Indexes Ready?*, PVLDB 2022。具体来源、版本和许可边界见 [NOTICE](NOTICE.md)。本仓库原创代码采用 GPL-3.0，见 [LICENSE](LICENSE)。
