#!/bin/bash
# © 2025 Nokia - BSD-3-Clause
#
# DC1 2nd MULTI-HOMED client emulator (separate Ethernet-Segment from mh-dc1).
# eth1 -> leaf3, eth2 -> leaf4 bonded (802.3ad/LACP) into bond0; leaf3+leaf4 form
# one EVPN all-active Ethernet-Segment (mh-dc1b). Two logical clients per service.
# The extra attachment point gives DCI traffic more flows to hash over both DCGWs.
#
#   VLAN 100  = L2-DCI   (stretched bridge-domain, subnet 10.100.0.0/24)
#   VLAN 200  = L3-DCI   (DC1 subnet 10.200.1.0/24, anycast GW 10.200.1.254)

set -x

for i in $(seq 1 30); do
  ip link show eth1 >/dev/null 2>&1 && ip link show eth2 >/dev/null 2>&1 && break
  sleep 1
done

ip link set eth1 down
ip link set eth2 down
ip link add bond0 type bond mode 802.3ad miimon 100 lacp_rate fast xmit_hash_policy layer3+4
ip link set eth1 master bond0
ip link set eth2 master bond0
ip link set eth1 up
ip link set eth2 up
ip link set bond0 up

ip link add link bond0 name bond0.100 type vlan id 100
ip link add link bond0 name bond0.200 type vlan id 200
ip link set bond0.100 up
ip link set bond0.200 up

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
make_client mh1b-l2a bond0.100 00:00:00:00:01:14 10.100.0.14/24
make_client mh1b-l2b bond0.100 00:00:00:00:01:15 10.100.0.15/24
# L3-DCI logical clients (2x multi-homed)
make_client mh1b-l3a bond0.200 00:00:00:00:02:14 10.200.1.14/24 10.200.1.254
make_client mh1b-l3b bond0.200 00:00:00:00:02:15 10.200.1.15/24 10.200.1.254

echo "mh-dc1b client emulation ready"
