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

# modify init-base CR to ensure node configuration is saved on commit

print_separator
echo "Modifying init bootstrap CR to save on commit"
kubectl apply -f init.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply init.yaml. Exiting."
    exit 1
fi

# create new nodeuser for Containerlab nodes

print_separator
echo "Creating NodeUser for Containerlab nodes"
kubectl apply -f nodeusers.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply nodeusers.yaml. Exiting."
    exit 1
fi

# create new nodeprofile for Containerlab nodes

print_separator
echo "Creating NodeProfile for Containerlab nodes"
kubectl apply -f nodeprofile.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply nodeprofile.yaml. Exiting."
    exit 1
fi

# onboard Containerlab nodes

print_separator
echo "Onboarding Containerlab nodes into EDA"
kubectl apply -f toponodes.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply nodes.yaml. Exiting."
    exit 1
fi

# before proceeding, we must wait to see if all nodes have been synced with EDA
nodes=("leaf1" "leaf2" "leaf3" "leaf4" "leaf5" "leaf6" "spine1" "spine2")
max_retries=30
wait_before_retry=30
retry_count=0
desired_state="Synced"

while [ $retry_count -lt $max_retries ]; do
    all_synced=true
    for node in "${nodes[@]}"; do
        echo "Checking status for node: $node"
        
        # Get the current state of the node
        current_state=$(kubectl describe toponodes "$node" -n eda | grep 'Node - State:' | awk '{print $NF}')
        
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

# create interfaces

print_separator
echo "Creating interfaces in EDA"
kubectl apply -f interfaces.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply interfaces.yaml. Exiting."
    exit 1
fi

# create links

print_separator
echo "Creating links in EDA"
kubectl apply -f links.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply links.yaml. Exiting."
    exit 1
fi

# create leaf ASN range

print_separator
echo "Creating leaf ASN range in EDA"
kubectl apply -f leaf-asn.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply leaf-asn.yaml. Exiting."
    exit 1
fi

# create spine ASN range

print_separator
echo "Creating spine ASN range in EDA"
kubectl apply -f spine-asn.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply spine-asn.yaml. Exiting."
    exit 1
fi

# create IP address range for system0 interfaces

print_separator
echo "Creating system0 IP address range"
kubectl apply -f system0-ipallocation.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply system0-ipallocation.yaml. Exiting."
    exit 1
fi

# deploy fabric

print_separator
echo "Deploying base fabric in EDA"
kubectl apply -f fabric.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply fabric.yaml. Exiting."
    exit 1
fi

sleep 15

# deploy virtual networks

print_separator
echo "Deploying virtual network constructs in EDA"
kubectl apply -f virtual-networks.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply virtual-networks.yaml. Exiting."
    exit 1
fi

sleep 15

# deploy routed interfaces

print_separator
echo "Deploying routed interfaces in EDA"
kubectl apply -f routed-interfaces.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply routed-interfaces.yaml. Exiting."
    exit 1
fi

# deploy static routes 

print_separator
echo "Deploying necessary static routes as per the design in EDA"
kubectl apply -f static-routes.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply static-routes.yaml. Exiting."
    exit 1
fi

# deploy node isolation configlet

print_separator
echo "Deploying node isolation configlet"
kubectl apply -f configlet-node-isolation.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply configlet-node-isolation.yaml. Exiting."
    exit 1
fi

# deploy configlet for BGP rapid advertisement and withdrawal

print_separator
echo "Deploying configlet for BGP rapid advertisement and withdrawal"
kubectl apply -f configlet-bgp-rapid.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply configlet-bgp-rapid.yaml. Exiting."
    exit 1
fi

# deploy configlet for DF election activation timer

print_separator
echo "Deploying configlet for DF election activation timer"
kubectl apply -f configlet-esi-df-activation.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply configlet-esi-df-activation.yaml. Exiting."
    exit 1
fi

print_separator
echo "All resources applied successfully."
print_separator

echo "Generating traffic in the fabric for host learning. Please wait."
print_separator

# login to container s1 and ping other hosts to trigger host learning
docker exec -it s1 ping -c 4 172.16.10.254
docker exec -it s2 ping -c 4 172.16.10.254
docker exec -it s3 ping -c 4 172.16.20.254
docker exec -it s4 ping -c 4 172.16.30.254
docker exec -it s6 ping -c 4 172.16.30.254
docker exec -it s7 ping -c 4 172.16.40.254
docker exec -it s8 ping -c 4 172.16.50.254
docker exec -it s9 ping -c 4 172.16.60.254