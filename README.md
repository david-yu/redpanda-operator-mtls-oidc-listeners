# Redpanda Dual-Listener Demo: OIDC + mTLS on Kubernetes

This guide demonstrates configuring a Redpanda cluster with two external Kafka listeners using different authentication methods:

- **OIDC listener** (port 31094): SASL/OAUTHBEARER backed by an OIDC provider (Dex)
- **mTLS listener** (port 30095): Mutual TLS with client certificate authentication

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
 |  --sasl-password |                  ||  | :9093   |  | :30094  |  | :30095   |    |
 |    token:<JWT>   |                  ||  |internal |  | oidc    |  | mtls     |    |
 +------------------+                  ||  |auth:sasl|  |auth:sasl|  |auth:mtls |    |
                                       ||  |         |  |OAUTHBR  |  |identity  |    |
                                       ||  +---------+  +---------+  +----------+    |
 +------------------+                  ||       ^            ^             ^          |
 | mTLS Client      |    NodePort      ||       |            |             |          |
 |                  |    :30095        ||  TLS certs    TLS certs    TLS certs +      |
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

 mTLS Listener (port 30095):
 +---------+     +----------+     +-----------+     +--------+
 | Client  |---->| Redpanda |---->| Verify    |---->| Accept |
 | sends   |     | receives |     | client    |     | user:  |
 | TLS     |     | client   |     | cert      |     | cert   |
 | cert    |     | cert     |     | against   |     | CN     |
 +---------+     +----------+     | trusted   |     +--------+
                                  | CA        |
                                  +-----------+
```

## Redpanda Cluster CRD: Listener Configuration

The Redpanda CR configures two external Kafka listeners with different authentication methods. Each listener independently specifies its `authenticationMethod`:

```yaml
listeners:
  kafka:
    authenticationMethod: sasl          # default for all kafka listeners
    external:
      # Listener 1: OIDC via SASL/OAUTHBEARER
      oidc:
        port: 30094                     # container port (must differ from internal 9094)
        advertisedPorts:
          - 31094                       # NodePort exposed to clients
        tls:
          enabled: true
          cert: default

      # Listener 2: mTLS client certificate authentication
      mtls:
        port: 30095
        authenticationMethod: mtls_identity   # overrides the default sasl
        tls:
          enabled: true
          cert: default
          requireClientAuth: true             # broker validates client cert
```

Cluster-level OIDC and SASL configuration:

```yaml
config:
  cluster:
    # Enable both SCRAM and OAUTHBEARER mechanisms
    sasl_mechanisms:
      - SCRAM
      - OAUTHBEARER

    # OIDC provider (Dex running in-cluster)
    oidc_discovery_url: "http://dex.dex.svc.cluster.local:5556/dex/.well-known/openid-configuration"
    oidc_token_audience: "redpanda"
    oidc_principal_mapping: "$.email"

    # Per-listener SASL mechanism control (v25.3+)
    # Uses the Redpanda list-of-objects format:
    sasl_mechanisms_overrides:
      - listener: oidc
        sasl_mechanisms:
          - OAUTHBEARER
```

> **Important**: `sasl_mechanisms_overrides` must use the list-of-objects format (`[{listener: ..., sasl_mechanisms: [...]}]`), NOT a map format. The full Redpanda CR is at [`manifests/redpanda-cr.yaml`](manifests/redpanda-cr.yaml).

## rpk OAUTHBEARER Support

rpk does not yet support SASL/OAUTHBEARER natively. This is being tracked and implemented in [redpanda-data/redpanda#30169](https://github.com/redpanda-data/redpanda/pull/30169). Once merged, you will be able to validate the OIDC listener with:

```bash
rpk topic list \
  --brokers localhost:31094 \
  -X tls.enabled=true \
  -X tls.insecure_skip_verify=true \
  -X tls.ca=certs/ca.crt \
  -X sasl.mechanism=OAUTHBEARER \
  -X "pass=token:${OIDC_TOKEN}"
```

Until then, use the `confluent-kafka` Python client (see Step 8) or a Java Kafka client to test OAUTHBEARER.

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
- An **oidc** listener on port 30094 with `authenticationMethod: sasl`
- An **mtls** listener on port 30095 with `authenticationMethod: mtls_identity`
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

### Verify the configuration

```bash
# Check listeners
kubectl exec -n redpanda redpanda-0 -c redpanda -- cat /etc/redpanda/redpanda.yaml | grep -A3 "name: oidc\|name: mtls"

# Check OIDC config
kubectl exec -n redpanda redpanda-0 -c redpanda -- rpk cluster config get oidc_discovery_url
kubectl exec -n redpanda redpanda-0 -c redpanda -- rpk cluster config get sasl_mechanisms_overrides
```

Expected `sasl_mechanisms_overrides` output:

```yaml
- listener: oidc
  sasl_mechanisms:
    - OAUTHBEARER
```

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

## Step 7: Validate the mTLS Listener

The mTLS listener authenticates clients via their TLS certificate. The principal is extracted from the certificate's Common Name (CN) with a `CN=` prefix.

### Grant permissions to the mTLS principal

```bash
kubectl exec -n redpanda redpanda-0 -c redpanda -- \
  rpk cluster config set superusers '["kubernetes-controller", "admin", "CN=mtls-client", "user@example.com"]'
```

> **Important**: The mTLS principal is `CN=mtls-client`, NOT `mtls-client`. Redpanda prefixes the certificate's Common Name with `CN=`. If you see `TOPIC_AUTHORIZATION_FAILED`, check the Redpanda logs for the exact principal string.

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

### Test with the confluent-kafka Python client

rpk does not yet support OAUTHBEARER ([tracking PR](https://github.com/redpanda-data/redpanda/pull/30169)). Use the `confluent-kafka` Python client:

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
[INFO] Token principal: user@example.com, audience: redpanda
[PASS] list_topics via OAUTHBEARER: ['_schemas', 'mtls-test', '__consumer_offsets']
=== SUMMARY ===
[PASS] OIDC token acquisition from Dex
[PASS] SASL/OAUTHBEARER authentication against Redpanda oidc listener
[PASS] Metadata (list_topics) via OAUTHBEARER
[PASS] sasl_mechanisms_overrides set via CRD bootstrap config (no manual rpk needed)
```

> **Note on produce/consume from inside the cluster**: Kafka clients follow the broker's advertised address after the initial metadata request. Since the external listeners advertise `localhost:<NodePort>`, produce/consume operations from a pod _inside_ the cluster will fail with connection refused. This is expected behavior. The authentication handshake and metadata operations succeed because they complete on the bootstrap connection. To test produce/consume, connect from _outside_ the cluster where `localhost:NodePort` resolves correctly.

## How It Works

### Per-Listener Authentication

Each external listener has its own `authenticationMethod`:

| Listener | Container Port | NodePort | `authenticationMethod` | Accepts |
|----------|---------------|----------|----------------------|---------|
| `oidc` | 30094 | 31094 | `sasl` (default) | OAUTHBEARER tokens |
| `mtls` | 30095 | 30095 | `mtls_identity` | Client certificates |

> **Port selection**: External listener container ports must not collide with the internal Kafka port (9094 by default). This demo uses 30094/30095 to avoid conflicts.

### OIDC Configuration

OIDC is configured at the cluster level (not per-listener):

```yaml
config:
  cluster:
    oidc_discovery_url: "http://dex.dex.svc.cluster.local:5556/dex/.well-known/openid-configuration"
    oidc_token_audience: "redpanda"
    oidc_principal_mapping: "$.email"
```

### Per-Listener SASL Mechanism Control (v25.3+)

`sasl_mechanisms_overrides` restricts which SASL mechanisms each listener accepts. The property uses the Redpanda list-of-objects format and passes through the CRD `config.cluster` correctly:

```yaml
config:
  cluster:
    sasl_mechanisms_overrides:
      - listener: oidc
        sasl_mechanisms:
          - OAUTHBEARER
```

Without this override, all SASL listeners would accept both SCRAM and OAUTHBEARER.

## End-to-End Test Results

Results from a real Kind cluster deployment:

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
| Obtain OIDC token from Dex | PASS | Password grant flow |
| SASL/OAUTHBEARER handshake | PASS | Via `confluent-kafka` Python client |
| List topics (metadata) | PASS | Principal: `user@example.com` (from `$.email` claim) |
| `sasl_mechanisms_overrides` via CRD | PASS | Correct list-of-objects format passes through bootstrap config |

### Known Issues

1. **External listener port collision**: Container ports must differ from the internal Kafka API port (default 9094). Using 9094 for an external listener causes a duplicate port error in the NodePort service.

2. **mTLS principal includes `CN=` prefix**: Redpanda maps the client certificate CN as `CN=<common-name>`, not `<common-name>`. ACLs and superuser config must include the prefix.

3. **`sasl_mechanisms_overrides` format**: Must use the list-of-objects format (`- listener: ...\n  sasl_mechanisms: [...]`), NOT a map format (`oidc: [OAUTHBEARER]`). The list-of-objects format passes through the Helm chart bootstrap config correctly.

4. **rpk does not support OAUTHBEARER**: Use `confluent-kafka` (Python), `librdkafka`-based clients, or Java Kafka clients. rpk OAUTHBEARER support is tracked in [redpanda-data/redpanda#30169](https://github.com/redpanda-data/redpanda/pull/30169).

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
- Verify the principal matches: for mTLS it's `CN=<cert CN>`, for OIDC it's the email claim

### `sasl_mechanisms_overrides` shows empty `[]`

- Ensure you're using the list-of-objects format, not a map format
- Verify with: `kubectl exec -n redpanda redpanda-0 -c redpanda -- rpk cluster config get sasl_mechanisms_overrides`
