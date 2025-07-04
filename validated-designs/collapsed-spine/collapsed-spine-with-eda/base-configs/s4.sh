ip link set dev eth1 down
ip link add link eth1 name eth1.50 type vlan id 50
ip link set dev eth1 up
ip link set dev eth1.50 up
ip addr add 172.16.50.4/24 dev eth1.50
ip -6 addr add 2001:db8:0:50::4/64 dev eth1.50
ip route add default via 172.16.50.254 dev eth1.50 table 50
ip -6 route add default via 2001:db8:0:50::254 dev eth1.50 table 50