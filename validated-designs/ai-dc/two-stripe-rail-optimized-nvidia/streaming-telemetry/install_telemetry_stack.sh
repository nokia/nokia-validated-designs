# Install helm chart
echo "Installing telemetry-stack helm chart..."

proxy_var="${https_proxy:-$HTTPS_PROXY}"
if [[ -n "$proxy_var" ]]; then
    echo "Using proxy for grafana deployment: $proxy_var"
    noproxy="localhost\,127.0.0.1\,.local\,.internal\,.svc"

    helm install telemetry-stack ./charts/telemetry-stack \
    --set https_proxy="$proxy_var" \
    --set no_proxy="$noproxy" \
    --create-namespace -n eda-telemetry
else
    helm install telemetry-stack ./charts/telemetry-stack \
    --create-namespace -n eda-telemetry
fi

# Port-forward Prometheus and Grafana
echo ""

echo "Step-1: Run the following command to access Grafana:"
echo "kubectl port-forward -n eda-telemetry service/grafana 3000:3000 --address=0.0.0.0 >/dev/null 2>&1 & disown"
echo "kubectl port-forward -n eda-telemetry svc/victoria-metrics 8428:8428 --address=0.0.0.0 >/dev/null 2>&1 & disown"
echo ""

echo "Step-2: Open your browser and access the following URLs:"
echo "You can open Grafana at http://<eda-host-ip>:3000"
echo "You can open Victoria UI at http://<eda-host-ip>:8428/vmui"