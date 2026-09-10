# 字符串学习型索引 Benchmark

张圣皓负责的字符串内存索引 benchmark 工作区。以 [TLI](https://github.com/curtis-sun/TLI) 和 [GRE](https://github.com/gre4index/GRE) 为参考，先提供可复现的开发环境、小数据测试和接入入口。

当前可运行的是 **TLI 字符串测试的单线程子集**：复用其数据格式、工作负载生成器、计时及校验逻辑，接入上游 ART 和本地 `std::map` 对照。完整的研究 benchmark 还需逐步加入字符串学习型索引、更多分布及并发实验，详见 [范围与扩展](docs/BENCHMARK.md)。

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

Smoke 会生成两类确定性合成字符串，各 10,000 个键；对 8 种负载分别运行 ART 和 StdMap，共 32 个组合。每个组合先执行独立校验，再执行 2,000 次操作的吞吐量测试。运行记录、带表头的 `summary.csv` 和版本清单保存在 `results/smoke-*/`；最近一次通过的路径见 `results/latest-smoke.txt`。

这些小数据结果用于确认环境和执行链可用，不能据此得出索引性能结论。纯插入负载中的 `--verify` 仅确认执行完成；带读操作的混合负载会检查查找和区间结果。

## 自己运行一个负载

以下命令在仓库根目录执行，首次运行时使用尚不存在的数据文件名：

```bash
python3 -B scripts/make_dataset.py data/demo_string --shape prefix --count 10000
./build/generate data/demo_string 2000 --scan-ratio 0.025 --insert-ratio 0.5 --mix
./build/string_benchmark data/demo_string data/demo_string_ops_2K_0.025000rq_0.000000nl_0.500000i_0m_mix --only ART --verify
./build/string_benchmark data/demo_string data/demo_string_ops_2K_0.025000rq_0.000000nl_0.500000i_0m_mix --only ART --through --csv
```

将 `--only ART` 改成 `--only StdMap` 可运行对照。TLI 的吞吐量模式不执行校验，因此入口拒绝同时传入 `--through --verify`。原始 CSV 沿用上游的无表头格式；列定义见 [指标说明](docs/BENCHMARK.md)。

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
| `scripts/setup.py` | 固定版本源码、本地依赖、受限并行构建 |
| `scripts/make_dataset.py` | 合成字符串或转换文本数据 |
| `scripts/smoke.py` | 小规模校验、吞吐量测试、结果归档 |
| `upstream.lock.json` | TLI/GRE 源码提交锁定 |
| `docs/BENCHMARK.md` | 实验语义、当前限制、后续接入任务 |

`upstream/`、`.deps/`、`build/`、`data/`、`results/` 和本地工作日志均不提交。附件、原始对话和真实数据也不放入公开仓库。

## 来源与许可

TLI：*Testbed for Learned Indexes*；GRE：*Are Updatable Learned Indexes Ready?*, PVLDB 2022。具体来源、版本和许可边界见 [NOTICE](NOTICE.md)。本仓库原创代码采用 GPL-3.0，见 [LICENSE](LICENSE)。
