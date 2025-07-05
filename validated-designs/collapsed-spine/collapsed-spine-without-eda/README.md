 # Collapsed spine NVD without EDA  

The collapsed spine NVD digital twin deployment without EDA uses a two spine and three ToR switches topology deployed/instantiated using Containerlab. This topology is shown below, with server-facing interfaces marked: 

![collapsed-spine-without-eda-topology](/static/images/collapsed-spine-lld.png)

It covers the following server connectivity use cases (for IPv4 and IPv6 traffic across all use cases):

- Layer 2 untagged server connectivity
- Layer 2 tagged server connectivity
- Layer 3 server connectivity 
- All-active ES-based LAG multihoming with 802.3ad on server side
- Single-active ES-based multihoming with active/backup server NICs and LACP standby signaling

## Deployment using Containerlab

 The Containerlab topology can be deployed using `sudo containerlab deploy -t 2-way-collapsed-spine.clab.yaml.yaml -c`. This instantiates the complete lab with all configuration in the SR Linux nodes.

> Minimum required containerlab version: 0.61.0

### Revision history

| SRL version | Date tested |
|-------------|-------------|
| 25.3.2 | July 2025 |

