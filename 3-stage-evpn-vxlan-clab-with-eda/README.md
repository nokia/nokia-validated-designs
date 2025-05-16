© 2025 Nokia  
Licensed under the BSD 3-Clause License  
SPDX-License-Identifier: BSD-3-Clause  

# 3-stage EVPN VXLAN NVD with EDA  
> **Pre-requisite requirements:** This repository assumes that the user already has EDA deployed (using the kind installation method) with the SIMULATE flag set to false, which allows onboarding of external nodes (containerlab or real hardware). 

The 3-stage EVPN VXLAN NVD digital twin deployment with EDA uses a two spine, six leaf topology deployed/instantiated using Containerlab and orchestrated by EDA. It covers the following server connectivity use cases:

- Layer 2 untagged server connectivity
- Layer 2 tagged server connectivity
- Layer 3 server connectivity with static routes on the leaf, injected into BGP EVPN
- All-active ES-based LAG multihoming with 802.3ad on server side
- Single-active ES-based multihoming with active/backup server NICs and LACP standby signaling
- Active/backup server NICs with no LAG 

## Deployment using Containerlab and EDA

EDA (with the kind installation) and the Containerlab topology are deployed on the same physical server, with each creating their own respective bridges. Once the lab is deployed using `sudo containerlab deploy -t 3-stage-with-eda.clab.yaml`, you must ensure that iptables rules are correctly in place for communication between these two bridges. Example shown below, where 172.19.0.0/16 is the EDA Kind bridge and 172.21.21.0/24 is the Containerlab bridge to which all nodes are connected.

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

Once you have confirmed that the EDA license is installed and valid (using `kubectl describe license -A`) and the fabric nodes are reachable via the bootstrap server pod `bsvr`, you can then use the script `deploy-3-stage-nvd.sh`. This is a prescriptive bash script built specifically for the Containerlab NVD topology found in this repository. It deploys all the necessary EDA components to bring the fabric to life (the script simply does a `kubectl apply` for the individual manifest files and ensures that they are successfully applied before proceeding to the next file).

A snippet of the script deployment is shown below:

```
:~/nokia-validated-designs/3-stage-evpn-vxlan-with-eda$ ./deploy-3-stage-nvd.sh 
========================================
Creating NodeUser for Containerlab nodes
nodeuser.core.eda.nokia.com/clab-admin created
========================================
Creating NodeProfile for Containerlab nodes
nodeprofile.core.eda.nokia.com/clab-srlinux-24.10.1 created
========================================
Onboarding Containerlab nodes into EDA
toponode.core.eda.nokia.com/leaf1 created
toponode.core.eda.nokia.com/leaf2 created
toponode.core.eda.nokia.com/leaf3 created
toponode.core.eda.nokia.com/leaf4 created
toponode.core.eda.nokia.com/leaf5 created
toponode.core.eda.nokia.com/leaf6 created
toponode.core.eda.nokia.com/spine1 created
toponode.core.eda.nokia.com/spine2 created
Checking status for node: leaf1
Node leaf1 is not in the desired state.
Checking status for node: leaf2
Node leaf2 is not in the desired state.
Checking status for node: leaf3
Node leaf3 is not in the desired state.
Checking status for node: leaf4
Node leaf4 is not in the desired state.
Checking status for node: leaf5
Node leaf5 is not in the desired state.
Checking status for node: leaf6
Node leaf6 is not in the desired state.
Checking status for node: spine1
Node spine1 is not in the desired state.
Checking status for node: spine2
Node spine2 is not in the desired state.
Not all nodes are synced yet. Waiting for 30 seconds before retrying.

*snip*

========================================
Deploying virtual network constructs in EDA
bridgedomain.services.eda.nokia.com/macvrf-v10 created
bridgedomain.services.eda.nokia.com/macvrf-v20 created
bridgedomain.services.eda.nokia.com/macvrf-v30 created
bridgedomain.services.eda.nokia.com/macvrf-v40 created
bridgedomain.services.eda.nokia.com/macvrf-v50 created
bridgedomain.services.eda.nokia.com/macvrf-v60 created
irbinterface.services.eda.nokia.com/irb-v10 created
irbinterface.services.eda.nokia.com/irb-v20 created
irbinterface.services.eda.nokia.com/irb-v30 created
irbinterface.services.eda.nokia.com/irb-v40 created
irbinterface.services.eda.nokia.com/irb-v50 created
irbinterface.services.eda.nokia.com/irb-v60 created
router.services.eda.nokia.com/vrf1 created
router.services.eda.nokia.com/vrf2 created
vlan.services.eda.nokia.com/untagged-v10 created
vlan.services.eda.nokia.com/untagged-v20 created
vlan.services.eda.nokia.com/untagged-v30 created
vlan.services.eda.nokia.com/untagged-v40 created
vlan.services.eda.nokia.com/untagged-v50 created
vlan.services.eda.nokia.com/untagged-v60 created
========================================
Deploying routed interfaces in EDA
routedinterface.services.eda.nokia.com/leaf1-s5 created
========================================
Deploying necessary static routes as per the design in EDA
staticroute.protocols.eda.nokia.com/static-s5 created
========================================
Deploying necessary configlets
configlet.config.eda.nokia.com/lacp-standby-leaf5-leaf6 created
========================================
All resources applied successfully.
========================================
```

### Destroying the fabric

For quick teardown, another bash script called `destroy-3-stage-nvd` is provided as well. Again, this is a prescriptive script, specific to what is deployed using the `deploy-3-stage-nvd` script.

### Revision history

| EDA Version  | SRL version | Date tested |
|--------------|-------------|-------------|
| 24.12.1 | 24.10.2 | March 2025 |
| 25.4.1  | 24.10.2 | May 2025   |
