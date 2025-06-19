ip addr add 172.16.10.3/24 dev eth1
ip route add 172.16.20.0/24 via 172.16.10.254
ip route add 172.16.40.0/24 via 172.16.10.254
ip route add 172.16.60.0/24 via 172.16.10.254
ip route add 172.16.92.0/22 via 172.16.10.254