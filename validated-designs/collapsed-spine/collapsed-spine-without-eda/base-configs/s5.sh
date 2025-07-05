ip link set dev eth1 down
ip link set dev eth1 up
ip addr add 172.16.10.5/24 dev eth1
ip -6 addr add 2001:db8:0:10::5/64 dev eth1
ip route add default via 172.16.10.254 dev eth1 table 10
ip -6 route add default via 2001:db8:0:10::254 dev eth1 table 10