# Frigate Notify - Kubernetes Deployment

This directory contains Kubernetes manifests for deploying Frigate Notify in a Kubernetes cluster.

## Prerequisites

- Kubernetes cluster (1.19+)
- `kubectl` configured to access your cluster
- Tailscale operator (recommended) or Tailscale sidecar

**Note:** The `deployment.yaml` is already configured to use the published image from GitHub Container Registry (`ghcr.io/rv10guy/frigate-notify:latest`). No need to build the image unless you want to customize it.

## Quick Start

### 1. (Optional) Use Published Image or Build Your Own

**Option A: Use Published Image (Recommended)**

The deployment is already configured to pull from `ghcr.io/rv10guy/frigate-notify:latest`. No action needed!

**Option B: Build Custom Image**

If you need to customize the code:

```bash
# Build the image
docker build -t ghcr.io/YOUR-USERNAME/frigate-notify:latest .

# Push to your registry
docker push ghcr.io/YOUR-USERNAME/frigate-notify:latest

# Update deployment.yaml with your image name
```

### 2. Create Secret

```bash
# Copy the secret template
cp k8s/secret.yaml.example k8s/secret.yaml

# Edit secret.yaml and add your base64-encoded secrets
# Tip: echo -n 'your-secret' | base64

# Apply the secret
kubectl apply -f k8s/secret.yaml
```

### 3. Customize ConfigMap

Edit `k8s/configmap.yaml` to match your configuration:
- Update camera names
- Update door sensor topics and mappings
- Adjust notification cooldown period
- Set your Tailscale domain URLs

### 4. Deploy to Kubernetes

```bash
# Apply all manifests
kubectl apply -f k8s/

# Or apply individually in order:
kubectl apply -f k8s/persistentvolumeclaim.yaml
kubectl apply -f k8s/secret.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
```

### 5. Verify Deployment

```bash
# Check pod status
kubectl get pods -l app=frigate-notify

# Check logs
kubectl logs -f deployment/frigate-notify

# Check service
kubectl get svc frigate-notify
```

## Tailscale Integration

Frigate Notify is designed to run entirely on your Tailscale network, with no public internet exposure required.

### Option 1: Tailscale Kubernetes Operator (Recommended)

The Tailscale Kubernetes operator makes pods directly accessible on your Tailscale network.

1. **Install Tailscale Operator:**
   ```bash
   # Follow official docs: https://tailscale.com/kb/1236/kubernetes-operator
   ```

2. **Expose Service via Tailscale:**

   Create `k8s/tailscale-ingress.yaml`:
   ```yaml
   apiVersion: networking.k8s.io/v1
   kind: Ingress
   metadata:
     name: frigate-notify
     annotations:
       tailscale.com/expose: "true"
       tailscale.com/hostname: "frigate-notify"
   spec:
     rules:
     - host: frigate-notify.your-tailnet.ts.net
       http:
         paths:
         - path: /
           pathType: Prefix
           backend:
             service:
               name: frigate-notify
               port:
                 number: 5050
   ```

   Apply:
   ```bash
   kubectl apply -f k8s/tailscale-ingress.yaml
   ```

3. **Access your app:**
   - URL: `https://frigate-notify.your-tailnet.ts.net`
   - Automatically gets a TLS certificate from Tailscale
   - Only accessible to devices on your Tailnet

### Option 2: Tailscale Sidecar Container

If you can't use the operator, run Tailscale as a sidecar container:

1. Add to `deployment.yaml`:
   ```yaml
   containers:
   # ... existing frigate-notify container ...

   - name: tailscale
     image: tailscale/tailscale:latest
     env:
     - name: TS_AUTHKEY
       valueFrom:
         secretKeyRef:
           name: tailscale-auth
           key: authkey
     - name: TS_KUBE_SECRET
       value: frigate-notify-tailscale-state
     - name: TS_USERSPACE
       value: "true"
     - name: TS_HOSTNAME
       value: frigate-notify
     securityContext:
       runAsUser: 1000
       runAsGroup: 1000
   ```

2. Create Tailscale auth key secret:
   ```bash
   kubectl create secret generic tailscale-auth \
     --from-literal=authkey=tskey-auth-xxxxx
   ```

### Option 3: External Node with Tailscale

If running Kubernetes on a node already connected to Tailscale:

1. Use NodePort service (uncomment in `service.yaml`)
2. Access via: `http://node-tailscale-hostname:30050`

## Configuration Files

- **deployment.yaml**: Main application deployment with probes and resource limits
- **service.yaml**: ClusterIP service (use with Tailscale)
- **configmap.yaml**: Non-sensitive configuration (cameras, doors, etc.)
- **secret.yaml.example**: Template for secrets (API keys, passwords)
- **persistentvolumeclaim.yaml**: Storage for SQLite database

## Environment Variables

All secrets are loaded via Kubernetes Secrets and injected as environment variables:

- `PUSHOVER_API_KEY` - Pushover API token
- `PUSHOVER_USER_KEY` - Pushover user/group key
- `MQTT_USERNAME` - MQTT broker username
- `MQTT_PASSWORD` - MQTT broker password
- `HEALTHCHECKS_UUID` - Healthchecks.io UUID (optional)
- `FRIGATE_SERVER_HOST` - Frigate server URL

## Health Checks

The deployment includes:
- **Liveness probe**: Checks if the app is running (HTTP GET to `/silence_settings`)
- **Readiness probe**: Checks if the app is ready to receive traffic
- **Startup probe**: Gives app time to start before other probes begin

## Resource Limits

Default resource configuration:
- **Requests**: 100m CPU, 128Mi memory
- **Limits**: 500m CPU, 256Mi memory

Adjust based on your needs in `deployment.yaml`.

## Persistent Storage

The SQLite database is stored on a PersistentVolume:
- Default size: 1Gi (more than enough for silence settings)
- Access mode: ReadWriteOnce
- Mount path: `/data`

## Troubleshooting

### Check logs:
```bash
kubectl logs -f deployment/frigate-notify
```

### Check configuration:
```bash
kubectl describe configmap frigate-notify-config
```

### Check secrets (without revealing values):
```bash
kubectl describe secret frigate-notify-secrets
```

### Exec into pod:
```bash
kubectl exec -it deployment/frigate-notify -- /bin/bash
```

### Test MQTT connectivity:
```bash
kubectl exec -it deployment/frigate-notify -- python -c "
import paho.mqtt.client as mqtt
import os

client = mqtt.Client()
client.username_pw_set(os.getenv('MQTT_USERNAME'), os.getenv('MQTT_PASSWORD'))
client.connect('mqtt.txsww.com', 1883, 60)
print('MQTT connection successful!')
"
```

## Updating the Deployment

```bash
# Update image
kubectl set image deployment/frigate-notify frigate-notify=your-registry/frigate-notify:new-tag

# Or edit directly
kubectl edit deployment frigate-notify

# Restart pods
kubectl rollout restart deployment/frigate-notify

# Check rollout status
kubectl rollout status deployment/frigate-notify
```

## Security Considerations

- All secrets stored in Kubernetes Secrets
- Pod runs as non-root user (UID 1000)
- No privilege escalation allowed
- All unnecessary capabilities dropped
- Network access restricted to Tailscale network
- No public internet exposure required

## Backup and Restore

### Backup Database:
```bash
kubectl cp frigate-notify-pod-name:/data/silence_settings.db ./backup-silence_settings.db
```

### Restore Database:
```bash
kubectl cp ./backup-silence_settings.db frigate-notify-pod-name:/data/silence_settings.db
```

## Uninstall

```bash
kubectl delete -f k8s/
```

Note: This will delete the PVC and all data. Back up first if needed!
