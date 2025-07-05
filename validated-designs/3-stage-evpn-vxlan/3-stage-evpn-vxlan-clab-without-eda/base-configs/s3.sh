# © 2025 Nokia  
# Licensed under the BSD 3-Clause License  
# SPDX-License-Identifier: BSD-3-Clause  

ip addr add 172.16.20.3/24 dev eth1
ip route add 172.16.10.0/24 via 172.16.20.254
ip route add 172.16.30.0/24 via 172.16.20.254
ip route add 172.16.92.0/22 via 172.16.20.254
