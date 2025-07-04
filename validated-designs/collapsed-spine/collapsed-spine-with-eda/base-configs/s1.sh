ip link set dev eth1 down
ip link add link eth1 name eth1.20 type vlan id 20
ip link add link eth1 name eth1.100 type vlan id 100
ip link set dev eth1 up
ip link set dev eth1.20 up
ip link set dev eth1.100 up
ip addr add 172.16.10.1/24 dev eth1
ip addr add 172.16.20.1/24 dev eth1.20
ip addr add 172.16.100.3/24 dev eth1.100
ip -6 addr add 2001:db8:0:10::1/64 dev eth1
ip -6 addr add 2001:db8:0:20::1/64 dev eth1.20
ip -6 addr add 2001:db8:0:100::3/127 dev eth1.100
ip route add default via 172.16.10.254 dev eth1 table 10
ip route add default via 172.16.20.254 dev eth1.20 table 20
ip route add default via 172.16.100.2 dev eth1.100 table 100
ip -6 route add default via 2001:db8:0:10::254 dev eth1 table 10
ip -6 route add default via 2001:db8:0:20::254 dev eth1.20 table 20
ip -6 route add default via 2001:db8:0:100::2 dev eth1.100 table 100