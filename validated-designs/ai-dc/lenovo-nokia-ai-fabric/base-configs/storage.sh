ip link add name br0 type bridge
ip link set dev br0 up
ip addr add 198.51.100.3/24 dev br0
ip link set eth1 master br0
ip link set eth2 master br0