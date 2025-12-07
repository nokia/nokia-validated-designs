#!/bin/bash

# separator function

print_separator() {
    echo "========================================"
}

# destroy configlets
print_separator
echo "Destroying IPv6 configlet"
kubectl delete -f eda-manifests/configlet-ipv6-multipath.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete configlet-ipv6-multipath.yaml. Exiting."
    exit 1
fi

print_separator
echo "Destroying DLB configlet"
kubectl delete -f eda-manifests/configlet-dlb.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete configlet-dlb.yaml. Exiting."
    exit 1
fi

print_separator
echo "Destroying QOS configlet"
kubectl delete -f eda-manifests/configlet-qos.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete configlet-qos.yaml. Exiting."
    exit 1
fi

# destroy AI fabric
print_separator
echo "Destroying AI fabric"
kubectl delete -f eda-manifests/backend-fabric.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete backend-fabric.yaml. Exiting."
    exit 1
fi

# destroy frontend fabric
print_separator
echo "Destroying frontend/storage fabric"
kubectl delete -f eda-manifests/frontend-fabric.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete frontend-fabric.yaml. Exiting."
    exit 1
fi

# destroy frontend VNETs
print_separator
echo "Destroying frontend/storage VNETs"
kubectl delete -f eda-manifests/frontend-vnets.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete frontend-vnets.yaml. Exiting."
    exit 1
fi

# destroy system0 allocation pool
print_separator
echo "Destroying interfaces"
kubectl delete -f eda-manifests/systemv4-pool.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete systemv4-pool.yaml. Exiting."
    exit 1
fi

# destroy topolinks
print_separator
echo "Destroying interfaces"
kubectl delete -f eda-manifests/topolinks.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete topolinks.yaml. Exiting."
    exit 1
fi


# destroy interfaces
print_separator
echo "Destroying interfaces"
kubectl delete -f eda-manifests/interfaces.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete interfaces.yaml. Exiting."
    exit 1
fi

# destroy EDA AI backend app dependencies in custom namespace
print_separator
echo "Destroying AI fabric app dependencies in custom namespace"
kubectl delete -f eda-manifests/queues.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete queues.yaml. Exiting."
    exit 1
fi

print_separator
kubectl delete -f eda-manifests/forwarding-class.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete forwarding-class.yaml. Exiting."
    exit 1
fi

print_separator
kubectl delete -f eda-manifests/asn-pool.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete asn-pool.yaml. Exiting."
    exit 1
fi

print_separator
kubectl delete -f eda-manifests/leafindex-pool.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete leafindex-pool.yaml. Exiting."
    exit 1
fi

# destroy Containerlab nodes
print_separator
echo "Destroying toponodes"
kubectl delete -f eda-manifests/toponodes.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete toponodes.yaml. Exiting."
    exit 1
fi

# delete tunnels, VNIs and LAGs pool index
print_separator
echo "Destroying index pools for tunnels, VNIs and LAGs"
kubectl delete -f eda-manifests/tunnel-vni-lag-index-pool.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete tunnel-vni-lag-index-pool.yaml. Exiting."
    exit 1
fi

# destroy nodeprofile for Containerlab nodes
print_separator
echo "Destroying nodeprofile for Containerlab nodes"
kubectl delete -f eda-manifests/nodeprofile.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete nodeprofile.yaml. Exiting."
    exit 1
fi

# destroy nodeuser
print_separator
echo "Destroying nodeuser"
kubectl delete -f eda-manifests/nodeusers.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete nodeusers.yaml. Exiting."
    exit 1
fi

# delete nodegroup
print_separator
echo "Destroying nodegroup"
kubectl delete -f eda-manifests/nodegroup.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete nodegroup.yaml. Exiting."
    exit 1
fi

# destroy init-base
print_separator
echo "Destroying init-base"
kubectl delete -f eda-manifests/init.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete init.yaml. Exiting."
    exit 1
fi

# destroy topology view for AI fabrics
print_separator
echo "Destroying topology view for collapsed spine designs"
kubectl delete -f eda-manifests/topology-group-view.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete topology-group-view.yaml. Exiting."
    exit 1
fi

# destroy namespace
print_separator
echo "Destroying namespace"
kubectl delete -f eda-manifests/namespace.yaml
if [ $? -ne 0 ]; then
    echo "Failed to delete namespace.yaml. Exiting."
    exit 1
fi




















