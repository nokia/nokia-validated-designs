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

sleep 5

# create topology view for AI fabrics
print_separator
echo "Creating topology view for AI fabric"
kubectl apply -f eda-manifests/topology-group-view.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply topology-group-view.yaml. Exiting."
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
kubectl apply -f eda-manifests/nodeusers.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply nodeusers.yaml. Exiting."
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

# create index pools for tunnels, VNIs and LAGs
print_separator
echo "Creating index pools for tunnels, VNIs and LAGs"
kubectl apply -f eda-manifests/tunnel-vni-lag-index-pool.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply tunnel-vni-lag-index-pool.yaml. Exiting."
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
nodes=("spine1" "spine2" "stripe1-leaf1" "stripe1-leaf2" "stripe1-leaf3" "stripe1-leaf4" "stripe1-leaf5" "stripe1-leaf6" "stripe1-leaf7" "stripe1-leaf8" "stripe2-leaf1" "stripe2-leaf2" "stripe2-leaf3" "stripe2-leaf4" "stripe2-leaf5" "stripe2-leaf6" "stripe2-leaf7" "stripe2-leaf8")
max_retries=30
wait_before_retry=30
retry_count=0
desired_state="Synced"
namespace="dc1-backend"

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

# deploy EDA AI backend app dependencies in custom namespace
print_separator
echo "Deploying AI fabric app dependencies in custom namespace"
kubectl apply -f eda-manifests/queues.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply queues.yaml. Exiting."
    exit 1
fi

print_separator
kubectl apply -f eda-manifests/forwarding-class.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply forwarding-class.yaml. Exiting."
    exit 1
fi

print_separator
kubectl apply -f eda-manifests/asn-pool.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply asn-pool.yaml. Exiting."
    exit 1
fi

print_separator
kubectl apply -f eda-manifests/leafindex-pool.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply leafindex-pool.yaml. Exiting."
    exit 1
fi

# deploy interfaces
print_separator
echo "Deploying interfaces"
kubectl apply -f eda-manifests/interfaces.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply interfaces.yaml. Exiting."
    exit 1
fi

# deploy topolinks
print_separator
echo "Deploying interfaces"
kubectl apply -f eda-manifests/topolinks.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply topolinks.yaml. Exiting."
    exit 1
fi

# deploy system0 allocation pool
print_separator
echo "Deploying interfaces"
kubectl apply -f eda-manifests/systemv4-pool.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply systemv4-pool.yaml. Exiting."
    exit 1
fi

# deploy AI fabric
print_separator
echo "Deploying AI fabric"
kubectl apply -f eda-manifests/backend-fabric.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply backend-fabric.yaml. Exiting."
    exit 1
fi

# configure configlets
print_separator
echo "Configuring IPv6 configlet"
kubectl apply -f eda-manifests/configlet-ipv6-multipath.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply configlet-ipv6-multipath.yaml. Exiting."
    exit 1
fi

print_separator
echo "Configuring DLB configlet"
kubectl apply -f eda-manifests/configlet-dlb.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply configlet-dlb.yaml. Exiting."
    exit 1
fi

print_separator
echo "Configuring QOS configlet"
kubectl apply -f eda-manifests/configlet-qos.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply configlet-qos.yaml. Exiting."
    exit 1
fi

# deploy frontend fabric
print_separator
echo "Configuring frontend/storage fabric"
kubectl apply -f eda-manifests/frontend-fabric.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply frontend-fabric.yaml. Exiting."
    exit 1
fi

# deploy frontend VNETs
print_separator
echo "Configuring frontend/storage VNETs"
kubectl apply -f eda-manifests/frontend-vnets.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply frontend-vnets.yaml. Exiting."
    exit 1
fi

# configure server interfaces with IPv6 addresses
print_separator
cd ansible
echo "Configuring required server interfaces with respective IPv6 addresses"
ansible-playbook -i inventory.yml playbook.yml
