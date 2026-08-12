#!/usr/bin/env bash
# Bounded fault controller for the ai_ops_v2 Hyper-V benchmark.
# It only touches resources whose names start with md-aiopsv2 and the
# explicitly named Online Boutique services below.

set -euo pipefail

ACTION="${1:-}"
FIXTURE="${2:-}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
RELEASE_ROOT="${MINI_DROP_RELEASE_ROOT:-$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)}"
HELPERS="$RELEASE_ROOT/benchmarks/online_boutique_vm/fault_helpers"
STATE_DIR="/run/md-aiopsv2"
DATA_DIR="/var/tmp/md-aiopsv2"
NETWORK_NS="md-aiopsv2-net"
NETWORK_COMMENT="md-aiopsv2-net"

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "vm_faultctl.sh must run as root" >&2
    exit 3
  fi
  mkdir -p "$STATE_DIR" "$DATA_DIR"
}

container_id() {
  docker ps --filter "name=boutique_${1}\.1" --format '{{.ID}}' | head -1
}

wait_service() {
  local service="$1"
  local deadline=$((SECONDS + 90))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if [ "$(docker service inspect -f '{{if .UpdateStatus}}{{.UpdateStatus.State}}{{else}}completed{{end}}' "$service" 2>/dev/null || true)" != "updating" ] \
      && docker service ps --filter desired-state=running --format '{{.CurrentState}}' "$service" | grep -q '^Running'; then
      return 0
    fi
    sleep 2
  done
  echo "service did not converge: $service" >&2
  return 1
}

stop_unit() {
  systemctl stop "$1" >/dev/null 2>&1 || true
  systemctl reset-failed "$1" >/dev/null 2>&1 || true
}

cleanup_network() {
  stop_unit md-aiopsv2-net.service
  while iptables -t nat -C POSTROUTING -s 10.203.0.0/24 -m comment --comment "$NETWORK_COMMENT" -j MASQUERADE >/dev/null 2>&1; do
    iptables -t nat -D POSTROUTING -s 10.203.0.0/24 -m comment --comment "$NETWORK_COMMENT" -j MASQUERADE || true
  done
  ip netns del "$NETWORK_NS" >/dev/null 2>&1 || true
  ip link del mdv2-host >/dev/null 2>&1 || true
}

cleanup_partition() {
  stop_unit md-aiopsv2-partition-rollback.timer
  stop_unit md-aiopsv2-partition-rollback.service
  while iptables -C OUTPUT -d 192.168.10.12 -p udp --dport 4789 -m comment --comment md-aiopsv2-overlay -j DROP >/dev/null 2>&1; do
    iptables -D OUTPUT -d 192.168.10.12 -p udp --dport 4789 -m comment --comment md-aiopsv2-overlay -j DROP || true
  done
}

cleanup_disk() {
  stop_unit md-aiopsv2-disk.service
  umount "$DATA_DIR/disk" >/dev/null 2>&1 || true
  rm -f "$DATA_DIR/disk.img"
  rmdir "$DATA_DIR/disk" >/dev/null 2>&1 || true
}

cleanup_all() {
  local cid
  local stall_pid
  stall_pid="$(systemctl show md-aiopsv2-stall.service -p MainPID --value 2>/dev/null || true)"
  if [ -n "$stall_pid" ] && [ "$stall_pid" -gt 0 ] 2>/dev/null; then kill -CONT "$stall_pid" >/dev/null 2>&1 || true; fi
  for unit in md-aiopsv2-cpu.service md-aiopsv2-io.service md-aiopsv2-memory.service \
              md-aiopsv2-oom.service md-aiopsv2-java.service md-aiopsv2-go.service \
              md-aiopsv2-python.service md-aiopsv2-stall.service md-aiopsv2-transient.service; do
    stop_unit "$unit"
  done
  cleanup_network
  cleanup_partition
  cleanup_disk
  if [ "$(hostname)" = "worker2" ]; then
    cid="$(container_id redis-cart || true)"; [ -z "$cid" ] || docker unpause "$cid" >/dev/null 2>&1 || true
    cid="$(container_id paymentservice || true)"; [ -z "$cid" ] || docker unpause "$cid" >/dev/null 2>&1 || true
  fi
  if [ "$(hostname)" = "worker1" ]; then
    cid="$(container_id productcatalogservice || true)"; [ -z "$cid" ] || docker kill --signal USR2 "$cid" >/dev/null 2>&1 || true
    if docker service inspect -f '{{range .Spec.TaskTemplate.ContainerSpec.Env}}{{println .}}{{end}}' \
      boutique_productcatalogservice 2>/dev/null | grep -q '^EXTRA_LATENCY='; then
      docker service update --env-rm EXTRA_LATENCY boutique_productcatalogservice >/dev/null 2>&1 || true
      wait_service boutique_productcatalogservice || true
    fi
  fi
  rm -f "$DATA_DIR/io.bin" "$DATA_DIR"/*.log "$STATE_DIR"/*
}

start_cpu_noise() {
  systemd-run --unit=md-aiopsv2-cpu --property=RuntimeMaxSec=240 \
    /bin/bash -c 'yes >/dev/null & yes >/dev/null & yes >/dev/null & yes >/dev/null & wait' >/dev/null
}

start_io() {
  systemd-run --unit=md-aiopsv2-io --property=RuntimeMaxSec=240 \
    /bin/bash -c "while :; do dd if=/dev/zero of=$DATA_DIR/io.bin bs=1M count=1024 oflag=direct conv=notrunc status=none; done" >/dev/null
}

start_memory_pressure() {
  systemd-run --unit=md-aiopsv2-memory --property=RuntimeMaxSec=240 \
    /usr/bin/python3 -c 'import time; x=bytearray(1200*1024*1024); x[::4096]=b"x"*len(x[::4096]); time.sleep(230)' >/dev/null
}

start_oom() {
  # Keep a small shell supervisor alive while its child is repeatedly killed
  # by the bounded cgroup.  The stable supervisor PID lets a 15-second probe
  # observe memory.events.oom_kill without chasing a restarted PID.
  systemd-run --unit=md-aiopsv2-oom --property=RuntimeMaxSec=240 \
    --property=MemoryMax=96M --property=MemorySwapMax=0 --property=OOMPolicy=continue \
    /bin/bash -c "echo -1000 > /proc/\$\$/oom_score_adj; while :; do /usr/bin/python3 -c 'import time; open(\"/proc/self/oom_score_adj\",\"w\").write(\"1000\"); time.sleep(.2); x=bytearray(512*1024*1024)' || echo 'ERROR out of memory OOM child killed' >>$DATA_DIR/oom.log; sleep 2; done" >/dev/null
  sleep 5
}

start_disk() {
  mkdir -p "$DATA_DIR/disk"
  truncate -s 192M "$DATA_DIR/disk.img"
  mkfs.ext4 -q -F "$DATA_DIR/disk.img"
  mount -o loop,nodev,nosuid,noexec "$DATA_DIR/disk.img" "$DATA_DIR/disk"
  systemd-run --unit=md-aiopsv2-disk --property=RuntimeMaxSec=240 \
    /usr/bin/python3 "$HELPERS/disk_fill_fault.py" --path "$DATA_DIR/disk" --duration 220 >/dev/null
  local deadline=$((SECONDS + 30))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if [ "$(df -P "$DATA_DIR/disk" | awk 'NR==2 {gsub(/%/,"",$5); print $5}')" -ge 95 ]; then return 0; fi
    sleep 1
  done
  echo "isolated filesystem did not reach 95%" >&2
  return 1
}

prepare_runtimes() {
  if command -v go >/dev/null 2>&1; then
    GO111MODULE=off go build -o "$DATA_DIR/md-aiopsv2-go-lock" "$HELPERS/go_lock_fault.go"
  fi
  if command -v javac >/dev/null 2>&1; then
    javac -d "$DATA_DIR" "$HELPERS/JavaLockFault.java"
  fi
}

start_java_lock() {
  command -v java >/dev/null 2>&1 || { echo "java unavailable" >&2; return 4; }
  [ -f "$DATA_DIR/JavaLockFault.class" ] || prepare_runtimes
  systemd-run --unit=md-aiopsv2-java --property=RuntimeMaxSec=240 \
    /usr/bin/java -cp "$DATA_DIR" JavaLockFault 230 24 >/dev/null
}

start_go_lock() {
  [ -x "$DATA_DIR/md-aiopsv2-go-lock" ] || prepare_runtimes
  [ -x "$DATA_DIR/md-aiopsv2-go-lock" ] || { echo "go unavailable" >&2; return 4; }
  systemd-run --unit=md-aiopsv2-go --property=RuntimeMaxSec=240 \
    "$DATA_DIR/md-aiopsv2-go-lock" 230 24 >/dev/null
}

start_python() {
  local mode="$1" chunk="${2:-4}" interval="${3:-1.5}"
  systemd-run --unit=md-aiopsv2-python --property=RuntimeMaxSec=240 \
    /usr/bin/python3 "$HELPERS/python_runtime_fault.py" --mode "$mode" --duration 230 \
    --threads 24 --chunk-mb "$chunk" --interval "$interval" >/dev/null
}

start_stall() {
  systemd-run --unit=md-aiopsv2-stall --property=RuntimeMaxSec=240 \
    /usr/bin/python3 "$HELPERS/python_runtime_fault.py" --mode lock --duration 230 --threads 32 >/dev/null
  sleep 2
  local pid
  pid="$(systemctl show -p MainPID --value md-aiopsv2-stall.service)"
  kill -STOP "$pid"
}

start_network_loss() {
  cleanup_network
  ip netns add "$NETWORK_NS"
  ip link add mdv2-host type veth peer name mdv2-ns
  ip link set mdv2-ns netns "$NETWORK_NS"
  ip addr add 10.203.0.1/24 dev mdv2-host
  ip link set mdv2-host up
  ip netns exec "$NETWORK_NS" ip addr add 10.203.0.2/24 dev mdv2-ns
  ip netns exec "$NETWORK_NS" ip link set lo up
  ip netns exec "$NETWORK_NS" ip link set mdv2-ns up
  ip netns exec "$NETWORK_NS" ip route add default via 10.203.0.1
  sysctl -w net.ipv4.ip_forward=1 >/dev/null
  iptables -t nat -A POSTROUTING -s 10.203.0.0/24 -m comment --comment "$NETWORK_COMMENT" -j MASQUERADE
  ip netns exec "$NETWORK_NS" tc qdisc add dev mdv2-ns root netem loss 35% delay 180ms 80ms
  systemd-run --unit=md-aiopsv2-net --property=RuntimeMaxSec=240 \
    /usr/sbin/ip netns exec "$NETWORK_NS" /usr/bin/python3 "$HELPERS/network_client_fault.py" \
    --url http://192.168.10.11:8080 --duration 230 --timeout 1 --interval 0.08 >/dev/null
}

start_overlay_partition() {
  cleanup_partition
  cat > /run/systemd/system/md-aiopsv2-partition-rollback.service <<'EOF'
[Unit]
Description=Rollback bounded Mini-Drop overlay partition
[Service]
Type=oneshot
ExecStart=/bin/sh -c '/usr/sbin/iptables -D OUTPUT -d 192.168.10.12 -p udp --dport 4789 -m comment --comment md-aiopsv2-overlay -j DROP || true'
EOF
  cat > /run/systemd/system/md-aiopsv2-partition-rollback.timer <<'EOF'
[Unit]
Description=Automatic Mini-Drop overlay partition rollback
[Timer]
OnActiveSec=90
Unit=md-aiopsv2-partition-rollback.service
[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl start md-aiopsv2-partition-rollback.timer
  iptables -I OUTPUT 1 -d 192.168.10.12 -p udp --dport 4789 -m comment --comment md-aiopsv2-overlay -j DROP
}

inject_fixture() {
  local cid
  case "$FIXTURE" in
    productcatalog_cpu_hotspot_v1)
      cid="$(container_id productcatalogservice)"; test -n "$cid"; docker kill --signal USR1 "$cid" >/dev/null ;;
    productcatalog_latency_v1)
      docker service update --env-add EXTRA_LATENCY=1.5s boutique_productcatalogservice >/dev/null; wait_service boutique_productcatalogservice ;;
    redis_pause_v1)
      cid="$(container_id redis-cart)"; test -n "$cid"; docker pause "$cid" >/dev/null ;;
    payment_pause_v1)
      cid="$(container_id paymentservice)"; test -n "$cid"; docker pause "$cid" >/dev/null ;;
    worker_cpu_noise_v1) start_cpu_noise ;;
    worker_io_contention_v1) start_io ;;
    worker_memory_pressure_v1) start_memory_pressure ;;
    overlay_partition_v1) start_overlay_partition ;;
    process_oom_v1|oom_after_restart_v1) start_oom ;;
    loopback_enospc_v1) start_disk ;;
    java_lock_v1) start_java_lock ;;
    go_lock_v1) start_go_lock ;;
    python_lock_v1) start_python lock 1 2 ;;
    python_memory_growth_v1) start_python memory 4 1.5 ;;
    runtime_stall_v1) start_stall ;;
    network_loss_v1|stale_plus_network_v1) start_network_loss ;;
    python_memory_lock_v1) start_python compound 4 1.5 ;;
    disk_network_v1) start_disk; start_network_loss ;;
    noisy_downstream_v1) start_cpu_noise; cid="$(container_id paymentservice)"; test -n "$cid"; docker pause "$cid" >/dev/null ;;
    cross_worker_two_roots_v1)
      if [ "$(hostname)" = "worker1" ]; then start_network_loss; else start_memory_pressure; fi ;;
    payment_redis_pause_v1)
      cid="$(container_id paymentservice)"; test -n "$cid"; docker pause "$cid" >/dev/null
      cid="$(container_id redis-cart)"; test -n "$cid"; docker pause "$cid" >/dev/null ;;
    transient_spike_v1)
      systemd-run --unit=md-aiopsv2-transient --property=RuntimeMaxSec=8 /bin/bash -c 'yes >/dev/null & yes >/dev/null & wait' >/dev/null
      sleep 12 ;;
    healthy_baseline_v1|stale_evidence_replay_v1|duplicate_evidence_replay_v1|collector_failure_replay_v1|conflicting_sources_replay_v1|missing_scope_v1)
      : ;;
    *) echo "unknown fixture: $FIXTURE" >&2; return 2 ;;
  esac
}

status_fixture() {
  echo "fixture=$FIXTURE"
  docker service ls --format '{{.Name}}={{.Replicas}}' | sort
  systemctl list-units 'md-aiopsv2-*' --no-legend --no-pager || true
  if mountpoint -q "$DATA_DIR/disk"; then df -P "$DATA_DIR/disk"; fi
  ip netns list | grep -F "$NETWORK_NS" || true
}

require_root
case "$ACTION" in
  prepare) prepare_runtimes ;;
  clean) cleanup_all ;;
  inject) inject_fixture ;;
  status) status_fixture ;;
  *) echo "usage: $0 {prepare|inject|status|clean} [fixture]" >&2; exit 2 ;;
esac
