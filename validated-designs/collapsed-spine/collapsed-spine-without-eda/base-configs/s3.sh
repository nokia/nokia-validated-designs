ip addr add 172.16.10.3/24 dev eth1
ip -6 addr add 2001:db8:0:10::3/64 dev eth1
ip route add 172.16.20.0/24 via 172.16.10.254
ip route add 172.16.40.0/24 via 172.16.10.254
ip route add 172.16.60.0/24 via 172.16.10.254
ip route add 172.16.92.0/22 via 172.16.10.254
ip -6 route add 2001:db8:0:20::/64 via 2001:db8:0:10::254
ip -6 route add 2001:db8:0:40::/64 via 2001:db8:0:10::254 
ip -6 route add 2001:db8:0:60::/64 via 2001:db8:0:10::254