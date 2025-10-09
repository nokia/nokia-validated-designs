# interfaces to leaf1
ip -6 addr add fd00:100:1:1:0:5:0:2/96 dev eth1
ip -6 addr add fd00:100:1:1:0:6:0:1/96 dev eth2
ip -6 addr add fd00:100:1:1:0:7:0:1/96 dev eth3
ip -6 addr add fd00:100:1:1:0:8:0:1/96 dev eth4
# interfaces to leaf2
ip -6 addr add fd00:100:2:1:0:5:0:1/96 dev eth5
ip -6 addr add fd00:100:2:1:0:6:0:1/96 dev eth6
ip -6 addr add fd00:100:2:1:0:7:0:1/96 dev eth7
ip -6 addr add fd00:100:2:1:0:8:0:1/96 dev eth8
# active/backup to storage leafs
ip link add bond0 type bond mode active-backup primary eth10
ip link set bond0 type bond miimon 100
ip link set eth10 down
ip link set eth11 down
ip link set eth10 master bond0
ip link set eth11 master bond0
ip addr add 198.51.100.2/24 dev bond0
ip link set eth10 up
ip link set eth11 up
ip link set bond0 up
# interface to frontend server
ip addr add 172.16.10.2/24 dev eth9
