ip link add bond0 type bond mode 802.3ad
ip link set eth9 down
ip link set eth10 down
ip link set eth9 master bond0
ip link set eth10 master bond0
ip addr add 172.16.10.2/24 dev bond0
ip link set eth9 up
ip link set eth10 up
ip link set bond0 up
