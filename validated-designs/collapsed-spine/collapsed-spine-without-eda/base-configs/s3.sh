ip link add bond0 type bond mode 802.3ad
ip link set dev eth1 down
ip link set dev eth2 down
ip link set eth1 master bond0
ip link set eth2 master bond0
ip link add link bond0 name bond0.40 type vlan id 40
ip link set dev eth1 up
ip link set dev eth2 up
ip link set dev bond0 up
ip link set dev bond0.40 up
ip addr add 172.16.10.3/24 dev bond0
ip addr add 172.16.40.3/24 dev bond0.40
ip -6 addr add 2001:db8:0:10::3/64 dev bond0
ip -6 addr add 2001:db8:0:40::3/64 dev bond0.40
ip route add default via 172.16.10.254 dev bond0 table 10
ip route add default via 172.16.40.254 dev bond0.40 table 40
ip -6 route add default via 2001:db8:0:10::254 dev bond0 table 10
ip -6 route add default via 2001:db8:0:40::254 dev bond0.40 table 40