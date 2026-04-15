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

## Step 4: Install the Redpanda Operator

```bash
helm repo add redpanda https://charts.redpanda.com --force-update

helm install redpanda-operator redpanda/operator \
  --namespace redpanda-operator \
  --create-namespace \
  --set crds.enabled=true \
  --wait
```

## Step 5: Deploy Redpanda with Dual Listeners

The Redpanda CR in [`manifests/redpanda-cr.yaml`](manifests/redpanda-cr.yaml) configures:
- An **oidc** listener on port 9094 with `authenticationMethod: sasl`
- An **mtls** listener on port 9095 with `authenticationMethod: mtls_identity`
- Cluster-level OIDC configuration pointing to Dex
- `sasl_mechanisms_overrides` restricting the oidc listener to OAUTHBEARER only

```bash
kubectl create namespace redpanda
kubectl apply -f manifests/redpanda-cr.yaml
```

Wait for the cluster to become ready:

```bash
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

## Step 6: Extract TLS Certificates

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

## Step 7: Validate the mTLS Listener

The mTLS listener authenticates clients via their TLS certificate. The principal is extracted from the certificate's Common Name (CN) with a `CN=` prefix.

### Grant permissions to the mTLS principal

The mTLS principal includes the `CN=` prefix. Add it as a superuser for testing:

```bash
kubectl exec -n redpanda redpanda-0 -c redpanda -- \
  rpk cluster config set superusers '["kubernetes-controller", "admin", "CN=mtls-client", "user@example.com"]'
```

> **Important**: The mTLS principal is `CN=mtls-client`, NOT `mtls-client`. Redpanda prefixes the certificate's Common Name with `CN=`. This is a common gotcha — if you see `TOPIC_AUTHORIZATION_FAILED`, check the Redpanda logs for the exact principal string.

### Connect with rpk

When connecting via `kubectl port-forward` or Kind NodePorts, the broker TLS cert won't include `localhost` as a SAN. Use `-X tls.insecure_skip_verify=true` for local testing:

```bash
rpk topic list \
  --brokers localhost:30095 \
  -X tls.enabled=true \
  -X tls.insecure_skip_verify=true \
  -X tls.ca=certs/ca.crt \
  -X tls.cert=certs/client.crt \
  -X tls.key=certs/client.key
```

### Produce and consume a test message

```bash
# Create a topic
rpk topic create mtls-test \
  --brokers localhost:30095 \
  -X tls.enabled=true \
  -X tls.insecure_skip_verify=true \
  -X tls.ca=certs/ca.crt \
  -X tls.cert=certs/client.crt \
  -X tls.key=certs/client.key

# Produce a message
echo "hello from mTLS client" | rpk topic produce mtls-test \
  --brokers localhost:30095 \
  -X tls.enabled=true \
  -X tls.insecure_skip_verify=true \
  -X tls.ca=certs/ca.crt \
  -X tls.cert=certs/client.crt \
  -X tls.key=certs/client.key

# Consume the message
rpk topic consume mtls-test --num 1 \
  --brokers localhost:30095 \
  -X tls.enabled=true \
  -X tls.insecure_skip_verify=true \
  -X tls.ca=certs/ca.crt \
  -X tls.cert=certs/client.crt \
  -X tls.key=certs/client.key
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

## Step 8: Validate the OIDC Listener

The OIDC listener authenticates clients via SASL/OAUTHBEARER tokens issued by Dex.

### Set `sasl_mechanisms_overrides` (v25.3+)

The `sasl_mechanisms_overrides` property uses a list-of-objects format that the bootstrap config passthrough may not serialize correctly. Set it via `rpk` after the cluster is running:

```bash
kubectl exec -n redpanda redpanda-0 -c redpanda -- rpk cluster config export --filename /tmp/cfg.yaml

kubectl exec -n redpanda redpanda-0 -c redpanda -- bash -c '
sed -i "s/sasl_mechanisms_overrides: \[\]/sasl_mechanisms_overrides:\n    - listener: oidc\n      sasl_mechanisms:\n        - OAUTHBEARER/" /tmp/cfg.yaml
rpk cluster config import --filename /tmp/cfg.yaml'
```

### Test with the confluent-kafka Python client

rpk does not currently support SASL/OAUTHBEARER. Use the `confluent-kafka` Python client, which has full OAUTHBEARER support:

```bash
kubectl apply -f scripts/oidc-test-pod.yaml

# Wait for the test to complete
kubectl wait pod/oidc-test -n redpanda --for=jsonpath='{.status.phase}'=Succeeded --timeout=120s

# View results
kubectl logs oidc-test -n redpanda | grep -E '^\[PASS\]|\[FAIL\]|\[INFO\]|==='
```

Expected output:

```
[PASS] Got OIDC token (length=801)
[INFO] Token principal: user@example.com
[PASS] list_topics via OAUTHBEARER: ['_schemas', 'mtls-test', '__consumer_offsets']
=== SUMMARY ===
[PASS] OIDC token acquisition from Dex
[PASS] SASL/OAUTHBEARER authentication against Redpanda oidc listener
[PASS] Metadata (list_topics) via OAUTHBEARER
```

> **Note on produce/consume from inside the cluster**: Kafka clients follow the broker's advertised address after the initial metadata request. Since the external listeners advertise `localhost:<NodePort>`, produce/consume operations from a pod _inside_ the cluster will fail with connection refused. This is expected — external listeners are designed for clients _outside_ the cluster. The authentication handshake (list_topics) succeeds because it completes before the address redirect.

## How It Works

### Per-Listener Authentication

Each external listener has its own `authenticationMethod`:

| Listener | Container Port | NodePort | `authenticationMethod` | Accepts |
|----------|---------------|----------|----------------------|---------|
| `oidc` | 30094 | 31094 | `sasl` (default) | OAUTHBEARER tokens |
| `mtls` | 30095 | 30095 | `mtls_identity` | Client certificates |

> **Port selection**: External listener container ports must not collide with the internal Kafka port (9094 by default). This demo uses 30094/30095 to avoid conflicts.

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

`sasl_mechanisms_overrides` restricts which SASL mechanisms each listener accepts. Without it, all SASL listeners would accept both SCRAM and OAUTHBEARER.

The property uses a list-of-objects format that must be set via `rpk cluster config import` after the cluster is running (see Step 8). The format is:

```yaml
sasl_mechanisms_overrides:
  - listener: oidc
    sasl_mechanisms:
      - OAUTHBEARER
```

This ensures the `oidc` listener only accepts OAUTHBEARER tokens, while the internal listener (if SASL-enabled) can still accept SCRAM password authentication.

## End-to-End Test Results

The following results were obtained from a real Kind cluster deployment:

### mTLS Listener (port 30095)

| Test | Result | Notes |
|------|--------|-------|
| TLS handshake with client cert | PASS | Client cert signed by same CA as broker |
| List topics | PASS | Principal: `CN=mtls-client` |
| Create topic | PASS | Requires `CN=` prefix in superusers/ACLs |
| Produce message | PASS | |
| Consume message | PASS | |

### OIDC Listener (port 30094)

| Test | Result | Notes |
|------|--------|-------|
| Obtain OIDC token from Dex | PASS | Password grant flow, token length ~800 bytes |
| SASL/OAUTHBEARER handshake | PASS | Validated via `confluent-kafka` Python client |
| List topics (metadata) | PASS | Principal: `user@example.com` (from `$.email` claim) |
| Create topic | FAIL (in-cluster) | Advertised-address redirect to `localhost:NodePort` unreachable from pod |
| Produce message | FAIL (in-cluster) | Same advertised-address issue |

> The in-cluster produce/create failures are expected for external listeners tested from inside the cluster. When clients connect from outside (where `localhost:NodePort` resolves correctly), all operations work. This is standard Kafka behavior, not specific to OIDC.

### Known Issues Discovered

1. **External listener port collision**: Container ports must differ from the internal Kafka API port (default 9094). Using port 9094 for an external listener causes a duplicate port error in the NodePort service.

2. **mTLS principal includes `CN=` prefix**: Redpanda maps the client certificate CN as `CN=<common-name>`, not just `<common-name>`. ACLs and superuser config must include the prefix. Check `kubectl logs` for the exact principal if you get auth failures.

3. **`sasl_mechanisms_overrides` bootstrap passthrough**: The map-of-lists format used by this property is not serialized correctly through the Helm chart's `config.cluster` passthrough. Set it post-deploy via `rpk cluster config import`.

4. **rpk does not support OAUTHBEARER**: Use `confluent-kafka` (Python), `librdkafka`-based clients, or Java Kafka clients to test the OIDC listener. rpk only supports SCRAM-SHA-256, SCRAM-SHA-512, and PLAIN.

5. **TLS hostname verification on Kind**: The auto-generated broker certificate does not include `localhost` as a SAN. Use `tls.insecure_skip_verify=true` (rpk) or `enable.ssl.certificate.verification=false` (librdkafka) for local testing.

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
