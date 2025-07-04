ip link add bond0 type bond mode 802.3ad
ip link set eth1 down
ip link set eth2 down
ip link set eth1 master bond0
ip link set eth2 master bond0
ip addr add 172.16.20.9/24 dev bond0
ip -6 addr add 2001:db8:0:20::9/64 dev bond0
ip link set eth1 up
ip link set eth2 up
ip link set bond0 up
ip route add 172.16.10.0/24 via 172.16.20.254
ip route add 172.16.40.0/24 via 172.16.20.254
ip route add 172.16.60.0/24 via 172.16.20.254
ip route add 172.16.92.0/22 via 172.16.20.254
ip -6 route add 2001:db8:0:10::/64 via 2001:db8:0:20::254
ip -6 route add 2001:db8:0:40::/64 via 2001:db8:0:20::254 
ip -6 route add 2001:db8:0:60::/64 via 2001:db8:0:20::254