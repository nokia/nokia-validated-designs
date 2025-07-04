#!/bin/bash

hosts=("s1" "s2" "s3" "s4" "s5" "s6" "s7" "s8" "s9")

# Function to get IP from eth1 or bond0 inside container and ping the gateway
ping_gateway() {
  local host="$1"
  echo "Processing $host..."

  # Try to get eth1 IPv6 address
  ip=$(docker exec -i "$host" ip -6 -o addr show eth1 2>/dev/null | awk '$4 !~ /^fe80/ {print $4}' | cut -d/ -f1 | head -n1)

  # If eth1 has no IP, try bond0 IP address
  if [[ -z "$ip" ]]; then
    echo "eth1 has no IPv6 address, checking bond0..."
    ip=$(docker exec -i "$host" ip -6 -o addr show bond0 2>/dev/null | awk '$4 !~ /^fe80/ {print $4}' | cut -d/ -f1 | head -n1)
  fi

  if [[ -z "$ip" ]]; then
    echo "No IPv6 address found on eth1 or bond0 in $host"
    return
  fi

  # Construct the default gateway IP
  prefix=$(echo "$ip" | sed 's/::.*$/::/')
  gw_ip="${prefix}254"
  echo "  ➤ Found IP: $ip | Assumed GW: $gw_ip"

  # Ping the gateway from inside the host
  docker exec -i "$host" ping -6 -c 4 -W 1 "$gw_ip"
  echo ""
}

# Loop through each container
for host in "${hosts[@]}"; do
  ping_gateway "$host"
done