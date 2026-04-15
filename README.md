# Redpanda Dual-Listener Demo: OIDC + mTLS on Kubernetes

This guide demonstrates configuring a Redpanda cluster with two external Kafka listeners using different authentication methods:

- **OIDC listener** (port 31094): SASL/OAUTHBEARER backed by an OIDC provider (Dex)
- **mTLS listener** (port 31095): Mutual TLS with client certificate authentication

Both listeners coexist on the same cluster. The OIDC configuration is cluster-level, while the per-listener `authenticationMethod` and `sasl_mechanisms_overrides` (v25.3+) control which listeners accept which auth mechanism.

## Architecture

```
 CLIENTS (localhost)                    KIND CLUSTER
 ==================                    ============

 +------------------+
 | OIDC Client      |    NodePort
 |                  |    :31094         +---------------------------------------------+
 | rpk              +------------------>|                                             |
 |  --sasl-mechanism|    TLS + SASL    ||  Redpanda Broker (redpanda-0)               |
 |    OAUTHBEARER   |    OAUTHBEARER   ||  +---------+  +---------+  +----------+    |
 |  --sasl-password |                  ||  | :9092   |  | :9094   |  | :9095    |    |
 |    token:<JWT>   |                  ||  |internal |  | oidc    |  | mtls     |    |
 +------------------+                  ||  |auth:sasl|  |auth:sasl|  |auth:mtls |    |
                                       ||  |         |  |OAUTHBR  |  |identity  |    |
                                       ||  +---------+  +---------+  +----------+    |
 +------------------+                  ||       ^            ^             ^          |
 | mTLS Client      |    NodePort      ||       |            |             |          |
 |                  |    :31095        ||  TLS certs    TLS certs    TLS certs +      |
 | rpk              +------------------>|  (cert-mgr)   (cert-mgr)  client CA verify |
 |  --tls-cert      |    TLS + client  ||                                             |
 |    client.crt    |    certificate   |+---------------------------------------------+
 |  --tls-key       |                  |
 |    client.key    |                  |  +-------------------+
 +------------------+                  |  | Dex (namespace:   |
                                       |  |      dex)         |
                                       |  |                   |
           JWT validation              |  | OIDC IdP          |
           .well-known/openid  <----------+ :5556             |
                                       |  |                   |
                                       |  | Static user:      |
                                       |  | user@example.com  |
                                       |  +-------------------+
                                       |
                                       +---------------------------+
                                       | cert-manager (namespace:  |
                                       |   cert-manager)           |
                                       |                           |
                                       | Issues:                   |
                                       |  - Broker TLS certs       |
                                       |  - mTLS client cert       |
                                       +---------------------------+


 AUTHENTICATION FLOW
 ===================

 OIDC Listener (port 31094):
 +---------+     +----------+     +-----------+     +--------+
 | Client  |---->| Redpanda |---->| Validate  |---->| Accept |
 | sends   |     | receives |     | JWT via   |     | user:  |
 | SASL    |     | OAUTHBR  |     | Dex OIDC  |     | email  |
 | token   |     | token    |     | discovery |     | claim  |
 +---------+     +----------+     +-----------+     +--------+

 mTLS Listener (port 31095):
 +---------+     +----------+     +-----------+     +--------+
 | Client  |---->| Redpanda |---->| Verify    |---->| Accept |
 | sends   |     | receives |     | client    |     | user:  |
 | TLS     |     | client   |     | cert      |     | cert   |
 | cert    |     | cert     |     | against   |     | CN     |
 +---------+     +----------+     | trusted   |     +--------+
                                  | CA        |
                                  +-----------+
```

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/)
- [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [Helm](https://helm.sh/docs/intro/install/)
- [rpk](https://docs.redpanda.com/current/get-started/rpk-install/)
- [jq](https://jqlang.github.io/jq/download/)

## Step 1: Create a Kind Cluster

Create a Kind cluster with NodePort mappings for both Kafka listeners:

```bash
kind create cluster --name redpanda-demo --config manifests/kind-config.yaml
```

Verify:

```bash
kubectl cluster-info --context kind-redpanda-demo
```

## Step 2: Install cert-manager

cert-manager generates TLS certificates for the Redpanda brokers and mTLS client certs.

```bash
helm repo add jetstack https://charts.jetstack.io --force-update

helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --set crds.enabled=true \
  --wait
```

## Step 3: Deploy Dex (OIDC Provider)

Dex acts as the OIDC identity provider. It runs in-cluster with a static user for testing.

```bash
kubectl create namespace dex
kubectl apply -f manifests/dex.yaml
kubectl rollout status deployment/dex -n dex --timeout=120s
```

Verify Dex is running:

```bash
kubectl port-forward -n dex svc/dex 5556:5556 &
sleep 2
curl -s http://localhost:5556/dex/.well-known/openid-configuration | jq .issuer
# Expected: "http://dex.dex.svc.cluster.local:5556/dex"
kill %1 2>/dev/null
```

## Step 4: Install Redpanda with Dual Listeners

### Option A: Helm Chart (recommended for this demo)

```bash
helm repo add redpanda https://charts.redpanda.com --force-update

kubectl create namespace redpanda

helm install redpanda redpanda/redpanda \
  --namespace redpanda \
  --values manifests/redpanda-values.yaml \
  --wait --timeout 10m
```

### Option B: Redpanda Operator CRD

If you prefer using the operator:

```bash
# Install the operator first
helm repo add redpanda https://charts.redpanda.com --force-update

helm install redpanda-operator redpanda/operator \
  --namespace redpanda-operator \
  --create-namespace \
  --set crds.enabled=true \
  --wait

# Deploy Redpanda via the CRD
kubectl create namespace redpanda
kubectl apply -f manifests/redpanda-cr.yaml

# Wait for the cluster to become ready
kubectl wait redpanda/redpanda -n redpanda --for=condition=Ready --timeout=10m
```

### Verify Redpanda is Running

```bash
kubectl get pods -n redpanda -l app.kubernetes.io/name=redpanda
```

Check that both external listeners are configured:

```bash
kubectl exec -n redpanda redpanda-0 -c redpanda -- \
  rpk cluster config get kafka_api
```

You should see entries for `oidc` (port 9094) and `mtls` (port 9095) alongside the internal listener.

## Step 5: Extract TLS Certificates

Extract the CA certificate (needed by both clients) and the mTLS client certificate:

```bash
# Create a local certs directory
mkdir -p certs

# CA certificate (trusted by both listeners)
kubectl get secret -n redpanda redpanda-default-cert -o jsonpath='{.data.ca\.crt}' | base64 -d > certs/ca.crt

# mTLS client certificate and key
kubectl apply -f manifests/mtls-client-cert.yaml
sleep 5  # wait for cert-manager to issue the cert
kubectl get secret -n redpanda mtls-client-cert -o jsonpath='{.data.tls\.crt}' | base64 -d > certs/client.crt
kubectl get secret -n redpanda mtls-client-cert -o jsonpath='{.data.tls\.key}' | base64 -d > certs/client.key
```

Verify the client cert was issued:

```bash
openssl x509 -in certs/client.crt -noout -subject
# Expected: subject=CN = mtls-client
```

## Step 6: Validate the mTLS Listener

The mTLS listener on port 31095 authenticates clients via their TLS certificate. The principal is extracted from the certificate's Common Name (CN).

### Create an ACL for the mTLS principal

The mTLS principal will be the certificate CN. Grant it permissions:

```bash
kubectl exec -n redpanda redpanda-0 -c redpanda -- \
  rpk security acl create \
    --allow-principal "User:mtls-client" \
    --operation all \
    --topic '*' \
    --group '*' \
    --cluster
```

### Connect with rpk

```bash
rpk topic list \
  --brokers localhost:31095 \
  --tls-enabled \
  --tls-truststore certs/ca.crt \
  --tls-cert certs/client.crt \
  --tls-key certs/client.key
```

### Produce and consume a test message

```bash
# Create a topic
rpk topic create mtls-test \
  --brokers localhost:31095 \
  --tls-enabled \
  --tls-truststore certs/ca.crt \
  --tls-cert certs/client.crt \
  --tls-key certs/client.key

# Produce a message
echo "hello from mTLS client" | rpk topic produce mtls-test \
  --brokers localhost:31095 \
  --tls-enabled \
  --tls-truststore certs/ca.crt \
  --tls-cert certs/client.crt \
  --tls-key certs/client.key

# Consume the message
rpk topic consume mtls-test --num 1 \
  --brokers localhost:31095 \
  --tls-enabled \
  --tls-truststore certs/ca.crt \
  --tls-cert certs/client.crt \
  --tls-key certs/client.key
```

Expected output:

```json
{
  "topic": "mtls-test",
  "value": "hello from mTLS client",
  "timestamp": 1234567890000,
  "partition": 0,
  "offset": 0
}
```

## Step 7: Validate the OIDC Listener

The OIDC listener on port 31094 authenticates clients via SASL/OAUTHBEARER tokens issued by Dex.

### Create an ACL for the OIDC principal

The OIDC principal is derived from the JWT claim configured in `oidc_principal_mapping` (the email field):

```bash
kubectl exec -n redpanda redpanda-0 -c redpanda -- \
  rpk security acl create \
    --allow-principal "User:user@example.com" \
    --operation all \
    --topic '*' \
    --group '*' \
    --cluster
```

### Obtain an OIDC Token

Port-forward Dex and request a token:

```bash
kubectl port-forward -n dex svc/dex 5556:5556 &
DEX_PID=$!
sleep 2

OIDC_TOKEN=$(./scripts/get-oidc-token.sh)
echo "Token obtained: ${OIDC_TOKEN:0:20}..."

kill $DEX_PID 2>/dev/null
```

### Connect with rpk

```bash
rpk topic list \
  --brokers localhost:31094 \
  --tls-enabled \
  --tls-truststore certs/ca.crt \
  --sasl-mechanism OAUTHBEARER \
  --sasl-password "token:${OIDC_TOKEN}"
```

### Produce and consume a test message

```bash
# Create a topic
rpk topic create oidc-test \
  --brokers localhost:31094 \
  --tls-enabled \
  --tls-truststore certs/ca.crt \
  --sasl-mechanism OAUTHBEARER \
  --sasl-password "token:${OIDC_TOKEN}"

# Produce
echo "hello from OIDC client" | rpk topic produce oidc-test \
  --brokers localhost:31094 \
  --tls-enabled \
  --tls-truststore certs/ca.crt \
  --sasl-mechanism OAUTHBEARER \
  --sasl-password "token:${OIDC_TOKEN}"

# Consume
rpk topic consume oidc-test --num 1 \
  --brokers localhost:31094 \
  --tls-enabled \
  --tls-truststore certs/ca.crt \
  --sasl-mechanism OAUTHBEARER \
  --sasl-password "token:${OIDC_TOKEN}"
```

## How It Works

### Per-Listener Authentication

Each external listener has its own `authenticationMethod`:

| Listener | Port | `authenticationMethod` | Accepts |
|----------|------|----------------------|---------|
| `oidc` | 9094 / 31094 | `sasl` (default) | OAUTHBEARER tokens |
| `mtls` | 9095 / 31095 | `mtls_identity` | Client certificates |

### OIDC Configuration

OIDC is configured at the cluster level (it is not a per-listener setting):

```yaml
config:
  cluster:
    oidc_discovery_url: "http://dex.dex.svc.cluster.local:5556/dex/.well-known/openid-configuration"
    oidc_token_audience: "redpanda"
    oidc_principal_mapping: "$.email"
```

### Per-Listener SASL Mechanism Control (v25.3+)

`sasl_mechanisms_overrides` restricts which SASL mechanisms each listener accepts. Without it, all SASL listeners would accept both SCRAM and OAUTHBEARER:

```yaml
config:
  cluster:
    sasl_mechanisms_overrides:
      oidc: ["OAUTHBEARER"]        # Only OIDC tokens, no passwords
```

This ensures the `oidc` listener only accepts OAUTHBEARER tokens, while the internal listener (if SASL-enabled) can still accept SCRAM password authentication.

## Cleanup

```bash
kind delete cluster --name redpanda-demo
rm -rf certs/
```

## Troubleshooting

### "SASL handshake failed" on the OIDC listener

- Verify the token is valid: `echo "$OIDC_TOKEN" | cut -d. -f2 | base64 -d 2>/dev/null | jq .`
- Check the `aud` claim matches `oidc_token_audience` in your config
- Verify Dex is reachable from inside the cluster: `kubectl exec -n redpanda redpanda-0 -c redpanda -- curl -s http://dex.dex.svc.cluster.local:5556/dex/.well-known/openid-configuration`

### "TLS handshake failed" on the mTLS listener

- Verify the client cert is signed by the same CA: `openssl verify -CAfile certs/ca.crt certs/client.crt`
- Check `requireClientAuth: true` is set on the `mtls` listener

### "Not authorized" after successful auth

- Check ACLs: `kubectl exec -n redpanda redpanda-0 -c redpanda -- rpk security acl list`
- Verify the principal matches: for mTLS it's the cert CN (`mtls-client`), for OIDC it's the email claim (`user@example.com`)

### Listener not reachable on NodePort

- Verify Kind port mappings: `docker port redpanda-demo-control-plane`
- Check the NodePort service: `kubectl get svc -n redpanda | grep external`
