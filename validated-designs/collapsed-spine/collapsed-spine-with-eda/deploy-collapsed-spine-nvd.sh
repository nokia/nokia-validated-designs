# © 2025 Nokia  
# Licensed under the BSD 3-Clause License  
# SPDX-License-Identifier: BSD-3-Clause  

#!/bin/bash

# separator function

print_separator() {
    echo "========================================"
}

# apply iptables rules to ensure kind and clab bridges can communicate with each other

print_separator
echo "Creating iptables rules for Kind and Containerlab bridges to communicate"
sudo iptables -I DOCKER-USER 2 \
-o $(sudo docker network inspect kind -f '{{.Id}}' | cut -c 1-12 | \
awk '{print "br-"$1}') \
-m comment --comment "allow communications to kind bridge (EDA)" -j ACCEPT

# create namespace
print_separator
echo "Creating namespace"
kubectl apply -f eda-manifests/namespace.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply namespace.yaml. Exiting."
    exit 1
fi

# create topology view for collapsed spine designs
print_separator
echo "Creating topology view for collapsed spine designs"
kubectl apply -f eda-manifests/topology-view-collapsed.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply topology-view-collapsed.yaml. Exiting."
    exit 1
fi

sleep 5

# create init-base
print_separator
echo "Creating init-base"
kubectl apply -f eda-manifests/init.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply init.yaml. Exiting."
    exit 1
fi

# create nodegroup
print_separator
echo "Creating nodegroup"
kubectl apply -f eda-manifests/nodegroup.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply nodegroup.yaml. Exiting."
    exit 1
fi

# create nodeuser
print_separator
echo "Creating nodeuser"
kubectl apply -f eda-manifests/nodeuser.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply nodeuser.yaml. Exiting."
    exit 1
fi

# create new nodeprofile for Containerlab nodes
print_separator
echo "Creating nodeprofile for Containerlab nodes"
kubectl apply -f eda-manifests/nodeprofile.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply nodeprofile.yaml. Exiting."
    exit 1
fi

# onboard Containerlab nodes
print_separator
echo "Onboarding Containerlab nodes into EDA"
kubectl apply -f eda-manifests/toponodes.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply toponodes.yaml. Exiting."
    exit 1
fi

# before proceeding, we must wait to see if all nodes have been synced with EDA
nodes=("d3l-29-spine1" "d3l-30-spine2" "d2l-34-tor1" "d2l-35-tor2" "d3l-28-tor3")
max_retries=30
wait_before_retry=30
retry_count=0
desired_state="Synced"
namespace="2-spine-collapsed"

while [ $retry_count -lt $max_retries ]; do
    all_synced=true
    for node in "${nodes[@]}"; do
        echo "Checking status for node: $node"
        
        # Get the current state of the node
        current_state=$(kubectl describe toponodes "$node" -n $namespace | grep 'Node - State:' | awk '{print $NF}')
        
        # Check if the current state matches the desired state
        if [ "$current_state" != "$desired_state" ]; then
            echo "Node $node is not in the desired state."
            all_synced=false  # At least one node is not synced
        else
            echo "Node $node is in the desired state: $current_state"
        fi
    done
    # Check if all nodes are synced
    if $all_synced; then
        echo "All nodes are in the desired state: $desired_state"
        break  # Exit the loop if all nodes are synced
    else
        echo "Not all nodes are synced yet. Waiting for $wait_before_retry seconds before retrying."
        sleep $wait_before_retry  # Wait before checking again
        ((retry_count++))  # Increment retry count
        
        if [ $retry_count -eq $max_retries ]; then
            echo "Timeout reached. Not all nodes are in the desired state. Exiting."
            exit 1  # Exit with error if max retries reached without success
        fi
    fi
done

# create LAG admin key pool
print_separator
echo "Creating LAG admin key pool"
kubectl apply -f eda-manifests/lag-admin-key-pool.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply lag-admin-key-pool.yaml. Exiting."
    exit 1
fi

# create interfaces
print_separator
echo "Creating interfaces in EDA"
kubectl apply -f eda-manifests/interfaces.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply interfaces.yaml. Exiting."
    exit 1
fi

# create links
print_separator
echo "Creating links in EDA"
kubectl apply -f eda-manifests/topolinks.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply links.yaml. Exiting."
    exit 1
fi

# create ASN range for spines
print_separator
echo "Creating ASN range for spines in EDA"
kubectl apply -f eda-manifests/collapsed-spine-asn.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply collapsed-spine-asn.yaml. Exiting."
    exit 1
fi

# create IP address range for system0 interfaces
print_separator
echo "Creating system0 IP address range"
kubectl apply -f eda-manifests/system0-allocation.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply system0-allocation.yaml. Exiting."
    exit 1
fi

# deploy fabric
print_separator
echo "Deploying fabric in EDA"
kubectl apply -f eda-manifests/fabric.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply fabric.yaml. Exiting."
    exit 1
fi

sleep 15

# deploy IRB interfaces pool
print_separator
echo "Deploying IRB interfaces pool in EDA"
kubectl apply -f eda-manifests/irb-intf-pool.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply irb-intf-pool.yaml. Exiting."
    exit 1
fi

# deploy tunnel index pool
print_separator
echo "Deploying tunnel index pool to be used for VXLAN tunnels"
kubectl apply -f eda-manifests/tunnel-index-pool.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply tunnel-index-pool.yaml. Exiting."
    exit 1
fi

# deploy virtual networks
print_separator
echo "Deploying virtual network constructs in EDA"
kubectl apply -f eda-manifests/virtual-networks.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply virtual-networks.yaml. Exiting."
    exit 1
fi

sleep 15

# deploy routed interfaces
print_separator
echo "Deploying routed interfaces in EDA"
kubectl apply -f eda-manifests/routed-interfaces.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply routed-interfaces.yaml. Exiting."
    exit 1
fi

# deploy system logging configlet
print_separator
echo "Deploying system logging configlet in EDA"
kubectl apply -f eda-manifests/configlet-system-logging.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply configlet-system-logging.yaml. Exiting."
    exit 1
fi

# deploy macvrf-arp-nd configlet
print_separator
echo "Deploying macvrf-arp-nd configlet in EDA"
kubectl apply -f eda-manifests/configlet-macvrf-arp.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply configlet-macvrf-arp.yaml. Exiting."
    exit 1
fi

print_separator
echo "All resources applied successfully."
print_separator

echo "Generating traffic in the fabric for IPv4 host learning. Please wait."
print_separator

docker exec -it s1 ping -c 4 -I 172.16.10.1 172.16.10.254
docker exec -it s1 ping -c 4 -I 172.16.20.1 172.16.20.254
docker exec -it s2 ping -c 4 -I 172.16.10.2 172.16.10.254
docker exec -it s2 ping -c 4 -I 172.16.30.2 172.16.30.254
docker exec -it s3 ping -c 4 -I 172.16.10.3 172.16.10.254
docker exec -it s3 ping -c 4 -I 172.16.40.3 172.16.40.254
docker exec -it s4 ping -c 4 -I 172.16.50.4 172.16.50.254
docker exec -it s5 ping -c 4 -I 172.16.10.5 172.16.10.254
docker exec -it s6 ping -c 4 -I 172.16.70.6 172.16.70.254

print_separator
echo "Generating traffic in the fabric for IPv6 host learning. Please wait."
print_separator

docker exec -it s1 ping -6 -c 4 -I 2001:db8:0:10::1 2001:db8:0:10::254
docker exec -it s1 ping -6 -c 4 -I 2001:db8:0:20::1 2001:db8:0:20::254
docker exec -it s2 ping -6 -c 4 -I 2001:db8:0:10::2 2001:db8:0:10::254
docker exec -it s2 ping -6 -c 4 -I 2001:db8:0:30::2 2001:db8:0:30::254
docker exec -it s3 ping -6 -c 4 -I 2001:db8:0:10::3 2001:db8:0:10::254
docker exec -it s3 ping -6 -c 4 -I 2001:db8:0:40::3 2001:db8:0:40::254
docker exec -it s4 ping -6 -c 4 -I 2001:db8:0:50::4 2001:db8:0:50::254
docker exec -it s5 ping -6 -c 4 -I 2001:db8:0:10::5 2001:db8:0:10::254
docker exec -it s6 ping -6 -c 4 -I 2001:db8:0:70::6 2001:db8:0:70::254