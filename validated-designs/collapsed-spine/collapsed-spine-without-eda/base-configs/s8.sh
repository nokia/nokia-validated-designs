ip addr add 172.16.70.8/24 dev eth1
ip -6 addr add 2001:db8:0:70::8/64 dev eth1
ip route add 172.16.30.0/24 via 172.16.70.254
ip route add 172.16.50.0/24 via 172.16.70.254
ip -6 route add 2001:db8:0:30::/64 via 2001:db8:0:70::254
ip -6 route add 2001:db8:0:50::/64 via 2001:db8:0:70::254