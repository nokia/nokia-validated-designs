#!/bin/bash
# © 2025 Nokia - BSD-3-Clause
#
# DC1 MULTI-HOMED client emulator.
# eth1 -> leaf1, eth2 -> leaf2 are bonded (802.3ad / LACP) into bond0. leaf1+leaf2
# present one EVPN all-active Ethernet-Segment (shared LACP system-id), so bond0 is
# a single multi-homed attachment. Per service VLAN we spawn 2 logical clients, each
# in its own network namespace via a macvlan child, and start an iperf3 server in it.
#
#   VLAN 100  = L2-DCI   (stretched bridge-domain, subnet 10.100.0.0/24)
#   VLAN 200  = L3-DCI   (DC1 subnet 10.200.1.0/24, anycast GW 10.200.1.254)

set -x

# --- wait for the fabric-facing interfaces to appear ---
for i in $(seq 1 30); do
  ip link show eth1 >/dev/null 2>&1 && ip link show eth2 >/dev/null 2>&1 && break
  sleep 1
done

# --- LACP bond across the two leaves ---
ip link set eth1 down
ip link set eth2 down
ip link add bond0 type bond mode 802.3ad miimon 100 lacp_rate fast xmit_hash_policy layer3+4
ip link set eth1 master bond0
ip link set eth2 master bond0
ip link set eth1 up
ip link set eth2 up
ip link set bond0 up

# --- per-service 802.1q sub-interfaces on the bond ---
ip link add link bond0 name bond0.100 type vlan id 100
ip link add link bond0 name bond0.200 type vlan id 200
ip link add link bond0 name bond0.110 type vlan id 110
ip link set bond0.100 up
ip link set bond0.200 up
ip link set bond0.110 up

# make_client <ns> <parent> <mac> <cidr> [gw]
IDX=0
make_client() {
  ns=$1; parent=$2; mac=$3; cidr=$4; gw=$5
  IDX=$((IDX+1))
  ip netns add "$ns"
  ip link add link "$parent" name "mv$IDX" type macvlan mode bridge
  ip link set "mv$IDX" address "$mac"
  ip link set "mv$IDX" netns "$ns"
  ip netns exec "$ns" ip link set lo up
  ip netns exec "$ns" ip link set "mv$IDX" name eth0
  ip netns exec "$ns" ip link set eth0 up
  ip netns exec "$ns" ip addr add "$cidr" dev eth0
  [ -n "$gw" ] && ip netns exec "$ns" ip route replace default via "$gw"
  ip netns exec "$ns" iperf3 -s -D
}

# L2-DCI logical clients (2x multi-homed)
make_client mh1-l2a bond0.100 00:00:00:00:01:11 10.100.0.11/24
make_client mh1-l2b bond0.100 00:00:00:00:01:12 10.100.0.12/24
# L3-DCI logical clients (2x multi-homed)
make_client mh1-l3a bond0.200 00:00:00:00:02:11 10.200.1.11/24 10.200.1.254
make_client mh1-l3b bond0.200 00:00:00:00:02:12 10.200.1.12/24 10.200.1.254
# L2-DCI BD-B logical clients (2x multi-homed) - 2nd stretched bridge-domain, prefers dcgw2
make_client mh1-l2c bond0.110 00:00:00:00:0b:11 10.110.0.11/24
make_client mh1-l2d bond0.110 00:00:00:00:0b:12 10.110.0.12/24

echo "mh-dc1 client emulation ready"
