# Ascend Qwen MindIE Bundle

这个目录整理了当前可用的 `MindIE` 部署脚本、配置文件和 `Qwen3` 的 `tokenizer_config.json` 覆盖模板。

特点：

- 脚本已改成相对路径，自包含使用
- 不直接修改 `/opt/models` 原始文件
- 通过 `docker run -v` 挂载覆盖版 `tokenizer_config.json`
- 当前这些模型已验证普通 chat 请求不显示 `<think>`：
  - `Qwen3-0.6B-Mindie`
  - `Qwen3-1.7B-Mindie`
  - `Qwen3-4B-Mindie`
  - `Qwen3-14B-Mindie`
  - `Qwen3-32B-Mindie`

使用方式：

```bash
cd /path/to/ascend_qwen_mindie_bundle
bash ./run-qwen3-14b-mindie-card2.sh
```

当前脚本总表：

| 脚本 | 模型名 | 端口 | 物理卡 | worldSize |
|---|---|---:|---|---:|
| `run-qwen3-0.6b-mindie-card1.sh` | `Qwen3-0.6B-Mindie` | `38001` | `davinci1` | `1` |
| `run-qwen3-1.7b-mindie-card1.sh` | `Qwen3-1.7B-Mindie` | `38003` | `davinci1` | `1` |
| `run-qwen3-4b-mindie-card1.sh` | `Qwen3-4B-Mindie` | `38002` | `davinci1` | `1` |
| `run-qwen3-8b-mindie-1card.sh` | `Qwen3-8B-Mindie` | `38000` | `davinci0` | `1` |
| `run-qwen3-14b-mindie-card2.sh` | `Qwen3-14B-Mindie` | `38004` | `davinci2` | `1` |
| `run-qwen3-32b-mindie-2card.sh` | `Qwen3-32B-Mindie` | `38005` | `davinci3,davinci4` | `2` |

目录说明：

- `run-*.sh`
  - 启动脚本
- `*.json`
  - 对应模型的 `MindIE` 配置
- `qwen3_template_overrides/`
  - 覆盖版 `tokenizer_config.json`

说明：

- 模型权重默认仍从 `/opt/models/MindSDK/...` 读取
- 宿主机需已安装 Ascend 驱动和相关运行环境
- 端口、卡号、容器名都写在脚本和配置里，可按需修改
