© 2025 Nokia  
Licensed under the BSD 3-Clause License  
SPDX-License-Identifier: BSD-3-Clause  

The 3-stage EVPN VXLAN digital twin for the corresponding NVD can be deployed using `sudo containerlab deploy -t 3-stage-without-eda.clab.yaml`. This is a non-EDA orchestrated deployment, with the per-node configuration loaded by Containerlab once the node starts.

The username/password for SR Linux nodes is `admin/NokiaSrl1!` and for the hosts, it is `user/multit00l`.

The topology for this deployment, instantiated using Containerlab, is shown below:

![3-stage-evpn-vxlan-no-eda-topology](/static/images/3-stage-evpn-vxlan-no-eda-topology.png)
