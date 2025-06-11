ip addr add 172.16.50.6/24 dev eth1
ip route add 172.16.30.0/24 via 172.16.50.254
ip route add 172.16.70.0/24 via 172.16.50.254