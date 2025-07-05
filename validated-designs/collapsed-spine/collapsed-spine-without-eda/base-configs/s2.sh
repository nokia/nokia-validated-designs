ip link set dev eth1 down
ip link add link eth1 name eth1.10 type vlan id 10
ip link add link eth1 name eth1.30 type vlan id 30
ip link add link eth1 name eth1.100 type vlan id 100
ip link set dev eth1 up
ip link set dev eth1.10 up
ip link set dev eth1.30 up
ip link set dev eth1.100 up
ip addr add 172.16.10.2/24 dev eth1.10
ip addr add 172.16.30.2/24 dev eth1.30
ip addr add 172.16.100.1/24 dev eth1.100
ip -6 addr add 2001:db8:0:10::2/64 dev eth1.10
ip -6 addr add 2001:db8:0:30::1/64 dev eth1.30
ip -6 addr add 2001:db8:0:100::1/127 dev eth1.100
ip route add default via 172.16.10.254 dev eth1.10 table 10
ip route add default via 172.16.30.254 dev eth1.30 table 30
ip route add default via 172.16.100.0 dev eth1.100 table 100
ip -6 route add default via 2001:db8:0:10::254 dev eth1.10 table 10
ip -6 route add default via 2001:db8:0:30::254 dev eth1.30 table 30
ip -6 route add default via 2001:db8:0:100::0 dev eth1.100 table 100