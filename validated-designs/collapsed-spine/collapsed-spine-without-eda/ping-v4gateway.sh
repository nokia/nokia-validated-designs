#!/bin/bash

hosts=("s1" "s2" "s3" "s4" "s5" "s6" "s7" "s8" "s9")

# Function to get IP from eth1 or bond0 inside container and ping the gateway
ping_gateway() {
  local host="$1"
  echo "Processing $host..."

  # Try to get eth1 IP address
  ip=$(docker exec -i "$host" sh -c "ip -4 addr show eth1 | awk '/inet / {print \$2}' | cut -d/ -f1")

  # If eth1 has no IP, try bond0 IP address
  if [[ -z "$ip" ]]; then
    echo "eth1 has no IP, checking bond0..."
    ip=$(docker exec -i "$host" sh -c "ip -4 addr show bond0 | awk '/inet / {print \$2}' | cut -d/ -f1")
  fi

  if [[ -z "$ip" ]]; then
    echo "No IPv4 address found on eth1 or bond0 in $host"
    return
  fi

  # Construct the default gateway IP
  gw_ip="${ip%.*}.254"
  echo "  ➤ Found IP: $ip | Assumed GW: $gw_ip"

  # Ping the gateway from inside the host
  docker exec -i "$host" ping -c 4 -W 1 "$gw_ip"
  echo ""
}

# Loop through each container
for host in "${hosts[@]}"; do
  ping_gateway "$host"
done