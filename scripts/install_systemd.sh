#!/usr/bin/env bash
set -euo pipefail

cd /root/bilibili-processor
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

install -m 0644 systemd/bilibili-api.service /etc/systemd/system/bilibili-api.service
install -m 0644 systemd/bilibili-worker.service /etc/systemd/system/bilibili-worker.service

systemctl daemon-reload
systemctl enable --now bilibili-api bilibili-worker
systemctl --no-pager --full status bilibili-api bilibili-worker || true
