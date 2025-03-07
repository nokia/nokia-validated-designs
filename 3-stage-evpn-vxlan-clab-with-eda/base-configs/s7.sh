# © 2025 Nokia  
# Licensed under the BSD 3-Clause License  
# SPDX-License-Identifier: BSD-3-Clause  

ip addr add 172.16.40.7/24 dev eth1
ip route add 172.16.50.0/24 via 172.16.40.254
ip route add 172.16.60.0/24 via 172.16.40.254