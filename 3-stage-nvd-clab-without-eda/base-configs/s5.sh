ip addr add 172.16.100.1/24 dev eth1
ip link add lo1 type dummy
ip addr add 172.16.92.5/24 dev lo1
ip link set lo1 up
ip link add lo2 type dummy
ip addr add 172.16.93.5/24 dev lo2
ip link set lo2 up
ip link add lo3 type dummy
ip addr add 172.16.93.5/24 dev lo3
ip link set lo3 up
ip link add lo4 type dummy
ip addr add 172.16.94.5/24 dev lo4
ip link set lo4 up
ip link add clab-vrf type vrf table 1
ip link set dev clab-vrf up
ip link set dev eth1 master clab-vrf
ip link set dev lo1 master clab-vrf
ip link set dev lo2 master clab-vrf
ip link set dev lo3 master clab-vrf
ip link set dev lo4 master clab-vrf
ip route add 0.0.0.0/0 via 172.16.100.0 table 1