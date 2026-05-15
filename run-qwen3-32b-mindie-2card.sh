#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

docker rm -f qwen3-32b-mindie >/dev/null 2>&1 || true

docker run -itd --privileged \
  --name=qwen3-32b-mindie \
  --net=host \
  --shm-size=64g \
  --device=/dev/davinci3 \
  --device=/dev/davinci4 \
  --device=/dev/davinci_manager \
  --device=/dev/devmm_svm \
  --device=/dev/hisi_hdc \
  -e ASCEND_VISIBLE_DEVICES=3,4 \
  -e ASCEND_RT_VISIBLE_DEVICES=3,4 \
  -e PYTORCH_NPU_ALLOC_CONF=max_split_size_mb:256 \
  -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
  -v /usr/local/Ascend/add-ons/:/usr/local/Ascend/add-ons/ \
  -v /usr/local/sbin/:/usr/local/sbin/ \
  -v /var/log/npu/slog/:/var/log/npu/slog \
  -v /var/log/npu/profiling/:/var/log/npu/profiling \
  -v /var/log/npu/dump/:/var/log/npu/dump \
  -v /var/log/npu/:/usr/slog \
  -v /etc/hccn.conf:/etc/hccn.conf \
  -v /etc/ascend_install.info:/etc/ascend_install.info \
  -v /opt/models:/opt/models \
  -v "${SCRIPT_DIR}/qwen3-32b-mindie-2card-config.json:/tmp/mindie-config.json:ro" \
  -v "${SCRIPT_DIR}/qwen3_template_overrides/Qwen3-32B/tokenizer_config.json:/opt/models/MindSDK/Qwen3-32B/tokenizer_config.json:ro" \
  swr.cn-south-1.myhuaweicloud.com/ascendhub/mindie:2.1.RC1-800I-A2-py311-openeuler24.03-lts \
  bash -lc 'cp /tmp/mindie-config.json /usr/local/Ascend/mindie/latest/mindie-service/conf/config.json && export MINDIE_LOG_TO_STDOUT=1 MINDIE_LOG_TO_FILE=0 && export ASCEND_GLOBAL_LOG_LEVEL=3 && export ASCEND_SLOG_PRINT_TO_STDOUT=1 && cd /usr/local/Ascend/mindie/latest/mindie-service && nohup ./bin/mindieservice_daemon >/tmp/mindie.out 2>&1 & tail -F /tmp/mindie.out'
