# © 2025 Nokia  
# Licensed under the BSD 3-Clause License  
# SPDX-License-Identifier: BSD-3-Clause  

#!/bin/bash

# separator function

print_separator() {
    echo "========================================"
}

# destroy system logging configlet
print_separator
echo "Destroying system logging configlet in EDA"
kubectl delete -f eda-manifests/configlet-system-logging.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete configlet-system-logging.yaml. Exiting."
    exit 1
fi

# destroy macvrf-arp-nd configlet
print_separator
echo "Destroying macvrf-arp-nd configlet in EDA"
kubectl delete -f eda-manifests/configlet-macvrf-arp.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete configlet-macvrf-arp.yaml. Exiting."
    exit 1
fi

# destroy routed interfaces
print_separator
echo "Destroying routed interfaces in EDA"
kubectl delete -f eda-manifests/routed-interfaces.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete routed-interfaces.yaml. Exiting."
    exit 1
fi

# destroy virtual networks
print_separator
echo "Destroying virtual network constructs in EDA"
kubectl delete -f eda-manifests/virtual-networks.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete virtual-networks.yaml. Exiting."
    exit 1
fi

# destroy tunnel index pool
print_separator
echo "Destroying tunnel index pool to be used for VXLAN tunnels"
kubectl delete -f eda-manifests/tunnel-index-pool.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete tunnel-index-pool.yaml. Exiting."
    exit 1
fi

# destroy IRB interfaces pool
print_separator
echo "Destroying IRB interfaces pool in EDA"
kubectl delete -f eda-manifests/irb-intf-pool.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete irb-intf-pool.yaml. Exiting."
    exit 1
fi

# delete fabric
print_separator
echo "Deleting collapsed spine fabric in EDA"
kubectl delete -f eda-manifests/fabric.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete fabric.yaml. Exiting."
    exit 1
fi

# delete collapsed spine ASN range
print_separator
echo "Deleting collapsed spine ASN range in EDA"
kubectl delete -f eda-manifests/collapsed-spine-asn.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete collapsed-spine-asn.yaml. Exiting."
    exit 1
fi

# delete system0 allocation
print_separator
echo "Deleting system0 IP allocation range"
kubectl delete -f eda-manifests/system0-allocation.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete system0-allocation.yaml. Exiting."
    exit 1
fi

# delete links
print_separator
echo "Deleting links in EDA"
kubectl delete -f eda-manifests/topolinks.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete topolinks.yaml. Exiting."
    exit 1
fi

# delete interfaces
print_separator
echo "Deleting interfaces in EDA"
kubectl delete -f eda-manifests/interfaces.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete interfaces.yaml. Exiting."
    exit 1
fi

# delete Containerlab nodes
print_separator
echo "Deleting Containerlab nodes from EDA"
kubectl delete -f eda-manifests/toponodes.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete toponodes.yaml. Exiting."
    exit 1
fi

# delete nodeprofile for Containerlab nodes
print_separator
echo "Deleting nodeprofile for Containerlab nodes"
kubectl delete -f eda-manifests/nodeprofile.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete nodeprofile.yaml. Exiting."
    exit 1
fi

# delete nodeuser
print_separator
echo "Delete nodeuser"
kubectl delete -f eda-manifests/nodeuser.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete nodeuser.yaml. Exiting."
    exit 1
fi

# delete nodegroup
print_separator
echo "Deleting nodegroup"
kubectl delete -f eda-manifests/nodegroup.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete nodegroup.yaml. Exiting."
    exit 1
fi

# destroy topology view for collapsed spine designs
print_separator
echo "Destroy topology view for collapsed spine designs"
kubectl delete -f eda-manifests/topology-view-collapsed.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete topology-view-collapsed.yaml. Exiting."
    exit 1
fi

# delete init-base
print_separator
echo "Deleting init-base"
kubectl delete -f eda-manifests/init.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete init.yaml. Exiting."
    exit 1
fi

# delete namespace
print_separator
echo "Deleting namespace"
kubectl delete -f eda-manifests/namespace.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete namespace.yaml. Exiting."
    exit 1
fi









