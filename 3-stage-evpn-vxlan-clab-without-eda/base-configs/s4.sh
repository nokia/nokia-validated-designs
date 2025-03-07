# © 2025 Nokia  
# Licensed under the BSD 3-Clause License  
# SPDX-License-Identifier: BSD-3-Clause  

ip link add bond0 type bond mode 802.3ad
ip link set eth1 down
ip link set eth2 down
ip link set eth1 master bond0
ip link set eth2 master bond0
ip addr add 172.16.30.4/24 dev bond0
ip link set eth1 up
ip link set eth2 up
ip link set bond0 up
ip route add 172.16.10.0/24 via 172.16.30.254
ip route add 172.16.20.0/24 via 172.16.30.254
ip route add 172.16.92.0/22 via 172.16.30.254
