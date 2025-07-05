# © 2025 Nokia  
# Licensed under the BSD 3-Clause License  
# SPDX-License-Identifier: BSD-3-Clause  

ip link add link eth1 name eth1.10 type vlan id 10
ip addr add 172.16.10.1/24 dev eth1.10
ip link set eth1.10 up
ip route add 172.16.20.0/24 via 172.16.10.254
ip route add 172.16.30.0/24 via 172.16.10.254
ip route add 172.16.92.0/22 via 172.16.10.254