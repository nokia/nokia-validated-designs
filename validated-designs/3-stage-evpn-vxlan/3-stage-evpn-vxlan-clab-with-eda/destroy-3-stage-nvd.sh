# © 2025 Nokia  
# Licensed under the BSD 3-Clause License  
# SPDX-License-Identifier: BSD-3-Clause  

#!/bin/bash

# separator function

print_separator() {
    echo "========================================"
}

# delete configlets

print_separator
echo "Deleting configlet for node isolation"
kubectl delete -f configlet-node-isolation.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete configlet-node-isolation.yaml. Exiting."
    exit 1
fi

# deploy configlet for BGP rapid advertisement and withdrawal

print_separator
echo "Destroying configlet for BGP rapid advertisement and withdrawal"
kubectl delete -f configlet-bgp-rapid.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete configlet-bgp-rapid.yaml. Exiting."
    exit 1
fi

# deploy configlet for DF election activation timer

print_separator
echo "Destroying configlet for DF election activation timer"
kubectl delete -f configlet-esi-df-activation.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete configlet-esi-df-activation.yaml. Exiting."
    exit 1
fi

# delete static routes 

print_separator
echo "Deleting necessary static routes as per the design in EDA"
kubectl delete -f static-routes.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete static-routes.yaml. Exiting."
    exit 1
fi

# delete routed interfaces

print_separator
echo "Deleting routed interfaces in EDA"
kubectl delete -f routed-interfaces.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete routed-interfaces.yaml. Exiting."
    exit 1
fi

# delete virtual networks

print_separator
echo "Deleting virtual network constructs in EDA"
kubectl delete -f virtual-networks.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete virtual-networks.yaml. Exiting."
    exit 1
fi

# deploy fabric

print_separator
echo "Deleting base fabric in EDA"
kubectl delete -f fabric.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete fabric.yaml. Exiting."
    exit 1
fi


# delete IP address range for system0 interfaces

print_separator
echo "Deleting system0 IP address range"
kubectl delete -f system0-ipallocation.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete system0-ipallocation.yaml. Exiting."
    exit 1
fi

# delete leaf ASN range

print_separator
echo "Deleting leaf ASN range in EDA"
kubectl delete -f leaf-asn.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete leaf-asn.yaml. Exiting."
    exit 1
fi

# delete spine ASN range

print_separator
echo "Deleting spine ASN range in EDA"
kubectl delete -f spine-asn.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete spine-asn.yaml. Exiting."
    exit 1
fi

# delete links

print_separator
echo "Deleting links in EDA"
kubectl delete -f links.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete links.yaml. Exiting."
    exit 1
fi

# delete interfaces

print_separator
echo "Deleting interfaces in EDA"
kubectl delete -f interfaces.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete interfaces.yaml. Exiting."
    exit 1
fi

# delete Containerlab nodes

print_separator
echo "Deleting Containerlab nodes from EDA"
kubectl delete -f toponodes.yaml
if [ $? -ne 0 ]; then
    echo "Failed to apply toponodes.yaml. Exiting."
    exit 1
fi

# delete nodeprofile for Containerlab nodes

print_separator
echo "Deleting NodeProfile for Containerlab nodes"
kubectl delete -f nodeprofile.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete nodeprofile.yaml. Exiting."
    exit 1
fi

print_separator
echo "All resources deleted successfully."
print_separator







