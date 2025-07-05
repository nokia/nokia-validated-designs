ip link set dev eth1 down
ip link add link eth1 name eth1.70 type vlan id 70
ip link set dev eth1 up
ip link set dev eth1.70 up
ip addr add 172.16.70.6/24 dev eth1.70
ip -6 addr add 2001:db8:0:70::6/64 dev eth1.70
ip route add default via 172.16.70.254 dev eth1.70 table 70
ip -6 route add default via 2001:db8:0:70::254 dev eth1.70 table 70