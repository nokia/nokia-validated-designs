ip addr add 172.16.60.9/24 dev eth1
ip route add 172.16.40.0/24 via 172.16.60.254
ip route add 172.16.50.0/24 via 172.16.60.254