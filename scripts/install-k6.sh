#!/usr/bin/env bash
set -euo pipefail

# גרסה קבועה כדי שמבנה הדוח לא ישתנה בין ריצות; טביעת SHA256 מההפצה הרשמית.
task_temp_dir="${RUNNER_TEMP:-$(mktemp -d)}"
curl --fail --location --retry 3 --connect-timeout 15 --max-time 120 \
  https://github.com/grafana/k6/releases/download/v1.4.0/k6-v1.4.0-linux-amd64.tar.gz \
  --output "$task_temp_dir/k6.tar.gz"
echo "58fa87283f5d5b11bc6824d9204e8a38266c9977f2c4d0a960ce1b5beeaae517  $task_temp_dir/k6.tar.gz" | sha256sum --check
tar -xzf "$task_temp_dir/k6.tar.gz" -C "$task_temp_dir"
sudo install "$task_temp_dir/k6-v1.4.0-linux-amd64/k6" /usr/local/bin/k6
k6 version
