 # Collapsed spine NVD with EDA  
> **Pre-requisite requirements:** This repository assumes that the user already has EDA deployed (using the kind installation method) with the SIMULATE flag set to false, which allows onboarding of external nodes (containerlab or real hardware). 

The collapsed spine NVD digital twin deployment with EDA uses a two spine and three ToR switches topology deployed/instantiated using Containerlab and orchestrated by EDA. This topology is shown below, with server-facing interfaces marked: 

![collapsed-spine-with-eda-topology](/static/images/collapsed-spine-lld.png)

It covers the following server connectivity use cases (for IPv4 and IPv6 traffic across all use cases):

- Layer 2 untagged server connectivity
- Layer 2 tagged server connectivity
- Layer 3 server connectivity 
- All-active ES-based LAG multihoming with 802.3ad on server side
- Single-active ES-based multihoming with active/backup server NICs and LACP standby signaling

## Deployment using Containerlab and EDA

EDA (with the kind installation) and the Containerlab topology are deployed on the same physical server, with each creating their own respective bridges. Once the lab is deployed using `sudo containerlab deploy -t 2-way-collapsed-spine.clab.yaml.yaml -c`, you must ensure that iptables rules are correctly in place for communication between these two bridges (the `deploy-collapsed-spine-nvd.sh` bash script also does this). Example shown below, where 172.19.0.0/16 is the EDA Kind bridge and 172.21.21.0/24 is the Containerlab bridge to which all nodes are connected.

> Minimum required containerlab version: 0.61.0

```
:~$ sudo iptables -I FORWARD -s 172.21.21.0/24 -d 172.19.0.0/16 -j ACCEPT
:~$ sudo iptables -I FORWARD -s 172.19.0.0/16 -d 172.21.21.0/24 -j ACCEPT

:~$ docker network ls
NETWORK ID     NAME               DRIVER    SCOPE
4a7dcb01ef79   bridge             bridge    local
33bbfeb15718   eda_mgmt           bridge    local
2e6368f8f174   ghost-in-da-edge   bridge    local
108bdfe94c03   host               host      local
5baa438a66d2   kind               bridge    local
9545cfab1ecc   none               null      local

:~$ ifconfig br-5baa438a66d2
br-5baa438a66d2: flags=4163<UP,BROADCAST,RUNNING,MULTICAST>  mtu 1500
        inet 172.19.0.1  netmask 255.255.0.0  broadcast 172.19.255.255
        inet6 fe80::42:2fff:fe11:85cd  prefixlen 64  scopeid 0x20<link>
        inet6 fc00:f853:ccd:e793::1  prefixlen 64  scopeid 0x0<global>
        inet6 fe80::1  prefixlen 64  scopeid 0x20<link>
        ether 02:42:2f:11:85:cd  txqueuelen 0  (Ethernet)
        RX packets 29050912  bytes 2939649196 (2.9 GB)
        RX errors 0  dropped 0  overruns 0  frame 0
        TX packets 47077691  bytes 42606851496 (42.6 GB)
        TX errors 0  dropped 0 overruns 0  carrier 0  collisions 0

:~$ ifconfig br-33bbfeb15718
br-33bbfeb15718: flags=4163<UP,BROADCAST,RUNNING,MULTICAST>  mtu 1500
        inet 172.21.21.1  netmask 255.255.255.0  broadcast 172.21.21.255
        inet6 fe80::42:34ff:feac:93a7  prefixlen 64  scopeid 0x20<link>
        ether 02:42:34:ac:93:a7  txqueuelen 0  (Ethernet)
        RX packets 4882316  bytes 3321053892 (3.3 GB)
        RX errors 0  dropped 0  overruns 0  frame 0
        TX packets 2859960  bytes 300259259 (300.2 MB)
        TX errors 0  dropped 0 overruns 0  carrier 0  collisions 0
```

## Deploying EDA fabric constructs

Once you have confirmed that the EDA license is installed and valid (using `kubectl describe license -A`) and the fabric nodes are reachable via the bootstrap server pod `bsvr`, you can then run the deploy script with `bash ./deploy-collapsed-spine-nvd.sh`. This is a prescriptive bash script built specifically for the Containerlab NVD topology found in this repository. It deploys all the necessary EDA components to bring the fabric to life (the script simply does a `kubectl apply -f [manifest-file]` for the individual manifest files and ensures that they are successfully applied before proceeding to the next file).

### Destroying the fabric

For quick teardown, another bash script called `destroy-collapsed-spine-nvd.sh` is provided as well. Again, this is a prescriptive script, specific to what is deployed using the `destroy-collapsed-spine-nvd.sh` script.

### Revision history

| EDA Version  | SRL version | Date tested |
|--------------|-------------|-------------|
| 25.4.2 | 25.3.2 | July 2025 |

