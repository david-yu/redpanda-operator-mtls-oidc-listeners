# Redpanda Dual-Listener Demo: OIDC + mTLS on Kubernetes

This guide demonstrates configuring a Redpanda cluster with two external Kafka listeners using different authentication methods:

- **OIDC listener** (port 31094): SASL/OAUTHBEARER backed by an OIDC provider (Dex)
- **mTLS listener** (port 30095): Mutual TLS with client certificate authentication

Both listeners coexist on the same cluster. The OIDC configuration is cluster-level, while the per-listener `authenticationMethod` and `sasl_mechanisms_overrides` (v25.3+) control which listeners accept which auth mechanism.

## Architecture

```
 CLIENTS (localhost)                      KIND CLUSTER
 ══════════════════                      ════════════

 ┌──────────────────┐
 │ OIDC Client      │    NodePort
 │                  │    :31094          ╔═══════════════════════════════════════════════╗
 │ rpk              ├───────────────────▶║                                               ║
 │  --sasl-mechanism│    TLS + SASL      ║  Redpanda Broker (redpanda-0)                 ║
 │    OAUTHBEARER   │    OAUTHBEARER     ║  ┌─────────┐  ┌─────────┐  ┌──────────┐       ║
 │  --sasl-password │                    ║  │ :9093   │  │ :30094  │  │ :30095   │       ║
 │    token:<JWT>   │                    ║  │internal │  │ oidc    │  │ mtls     │       ║
 └──────────────────┘                    ║  │auth:sasl│  │auth:sasl│  │auth:mtls │       ║
                                         ║  │         │  │OAUTHBR  │  │identity  │       ║
                                         ║  └─────────┘  └─────────┘  └──────────┘       ║
 ┌──────────────────┐                    ║       ▲            ▲             ▲            ║
 │ mTLS Client      │    NodePort        ║       │            │             │            ║
 │                  │    :30095          ║  TLS certs    TLS certs    TLS certs +        ║
 │ rpk              ├───────────────────▶║  (cert-mgr)   (cert-mgr)  client CA verify    ║
 │  --tls-cert      │    TLS + client    ║                                               ║
 │    client.crt    │    certificate     ╚═══════════════════════════════════════════════╝
 │  --tls-key       │
 │    client.key    │                    ┌───────────────────┐
 └──────────────────┘                    │ Dex (namespace:   │
                                         │      dex)         │
            JWT validation               │                   │
            .well-known/openid  ◀────────┤ OIDC IdP          │
                                         │ :5556             │
                                         │                   │
                                         │ Static user:      │
                                         │ user@example.com  │
                                         └───────────────────┘

                                         ┌───────────────────────────┐
                                         │ cert-manager (namespace:  │
                                         │   cert-manager)           │
                                         │                           │
                                         │ Issues:                   │
                                         │  • Broker TLS certs       │
                                         │  • mTLS client cert       │
                                         └───────────────────────────┘


 AUTHENTICATION FLOWS
 ════════════════════

 OIDC Listener (port 31094):
 ┌─────────┐     ┌──────────┐     ┌───────────┐     ┌────────┐
 │ Client  │────▶│ Redpanda │────▶│ Validate  │────▶│ Accept │
 │ sends   │     │ receives │     │ JWT via   │     │ user:  │
 │ SASL    │     │ OAUTHBR  │     │ Dex OIDC  │     │ email  │
 │ token   │     │ token    │     │ discovery │     │ claim  │
 └─────────┘     └──────────┘     └───────────┘     └────────┘

 mTLS Listener (port 30095):
 ┌─────────┐     ┌──────────┐     ┌───────────┐     ┌────────┐
 │ Client  │────▶│ Redpanda │────▶│ Verify    │────▶│ Accept │
 │ sends   │     │ receives │     │ client    │     │ user:  │
 │ TLS     │     │ client   │     │ cert      │     │ cert   │
 │ cert    │     │ cert     │     │ against   │     │ CN     │
 └─────────┘     └──────────┘     │ trusted   │     └────────┘
                                  │ CA        │
                                  └───────────┘
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

## Deploying on AKS with Microsoft Entra ID

This section covers the differences when deploying on Azure Kubernetes Service using Microsoft Entra ID (formerly Azure AD) as the OIDC provider instead of Dex.

### Architecture differences from the Kind demo

| Aspect | Kind + Dex | AKS + Entra ID |
|--------|-----------|----------------|
| OIDC provider | Dex (in-cluster) | Microsoft Entra ID (cloud-hosted) |
| Discovery URL | `http://dex.dex.svc.cluster.local:5556/dex/.well-known/openid-configuration` | `https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration` |
| Token audience | Custom (`redpanda`) | App registration Client ID or custom `api://` URI |
| External access | Kind NodePort + localhost | AKS LoadBalancer or Azure Application Gateway |
| TLS certificates | cert-manager self-signed | cert-manager with Let's Encrypt, or Azure-managed certs |
| mTLS on-prem access | N/A | Via Azure ExpressRoute private peering |
| Network isolation | N/A | Azure NSGs + Kubernetes NetworkPolicies |

### Step 1: Register Redpanda as an application in Entra ID

```bash
# Create the app registration
az ad app create \
  --display-name "Redpanda Kafka OIDC" \
  --sign-in-audience AzureADMyOrg

# Note the Application (client) ID — this becomes oidc_token_audience
APP_CLIENT_ID=$(az ad app list --display-name "Redpanda Kafka OIDC" --query '[0].appId' -o tsv)
echo "Client ID: $APP_CLIENT_ID"

# Note your tenant ID
TENANT_ID=$(az account show --query tenantId -o tsv)
echo "Tenant ID: $TENANT_ID"
```

Optionally, create an App ID URI for a more readable audience claim:

```bash
az ad app update --id $APP_CLIENT_ID --identifier-uris "api://redpanda-kafka"
# Now oidc_token_audience can be "api://redpanda-kafka"
```

### Step 2: Configure token claims

By default, Entra ID tokens include `preferred_username` and `email` claims, but `email` may be empty for accounts without a mailbox. For reliable principal mapping, use `preferred_username` or `upn`:

```bash
# Add optional claims to the access token
az ad app update --id $APP_CLIENT_ID --optional-claims '{
  "accessToken": [
    {"name": "email", "essential": false},
    {"name": "upn", "essential": true}
  ]
}'
```

### Step 3: Create the AKS cluster (if not existing)

```bash
az aks create \
  --resource-group myResourceGroup \
  --name myAKSCluster \
  --node-count 3 \
  --node-vm-size Standard_D4s_v3 \
  --enable-oidc-issuer \
  --generate-ssh-keys

az aks get-credentials --resource-group myResourceGroup --name myAKSCluster
```

### Step 4: Install cert-manager and the Redpanda operator

Same as the Kind demo:

```bash
helm repo add jetstack https://charts.jetstack.io --force-update
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager --create-namespace \
  --set crds.enabled=true --wait

helm repo add redpanda https://charts.redpanda.com --force-update
helm install redpanda-operator redpanda/operator \
  --namespace redpanda-operator --create-namespace \
  --set crds.enabled=true --wait
```

### Step 5: Deploy Redpanda with Entra ID OIDC

The key differences from the Kind demo are in the `config.cluster` section and the `external` access type:

```yaml
apiVersion: cluster.redpanda.com/v1alpha2
kind: Redpanda
metadata:
  name: redpanda
  namespace: redpanda
spec:
  clusterSpec:
    statefulset:
      replicas: 3

    tls:
      enabled: true
      certs:
        default:
          caEnabled: true

    auth:
      sasl:
        enabled: true
        mechanism: SCRAM-SHA-256
        users:
          - name: admin
            password: change-me-in-production
            mechanism: SCRAM-SHA-256

    # Use LoadBalancer for AKS (not NodePort)
    external:
      enabled: true
      type: LoadBalancer
      # For Azure internal LB (ExpressRoute access):
      # annotations:
      #   service.beta.kubernetes.io/azure-load-balancer-internal: "true"

    listeners:
      kafka:
        authenticationMethod: sasl
        external:
          # OIDC listener for cloud clients (internet-facing)
          oidc:
            port: 30094
            advertisedPorts:
              - 9094
            tls:
              enabled: true
              cert: default

          # mTLS listener for on-prem clients (via ExpressRoute)
          mtls:
            port: 30095
            authenticationMethod: mtls_identity
            tls:
              enabled: true
              cert: default
              requireClientAuth: true

    config:
      cluster:
        sasl_mechanisms:
          - SCRAM
          - OAUTHBEARER

        # --- Entra ID OIDC configuration ---
        # Replace <tenant-id> with your Azure tenant ID
        oidc_discovery_url: "https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration"

        # The audience claim Redpanda checks in the JWT.
        # Use the App Registration Client ID, or a custom URI like "api://redpanda-kafka"
        oidc_token_audience: "<app-client-id>"

        # Principal mapping — choose based on your Entra ID token claims:
        #   $.preferred_username  — UPN (user@domain.com), always present
        #   $.email               — email address (may be empty for some accounts)
        #   $.oid                 — object ID (GUID, always unique)
        oidc_principal_mapping: "$.preferred_username"

        sasl_mechanisms_overrides:
          - listener: oidc
            sasl_mechanisms:
              - OAUTHBEARER
```

> **`oidc_principal_mapping` gotcha**: Entra ID does not always populate the `email` claim (it depends on whether the user has an Exchange mailbox). Use `$.preferred_username` (returns `user@domain.com` for most accounts) or `$.oid` (returns the immutable Azure object ID) for reliable principal extraction.

### Step 6: Obtain an Entra ID token

Use the Azure CLI or a direct OAuth2 call:

```bash
# Option A: Azure CLI (requires user login)
az login
OIDC_TOKEN=$(az account get-access-token \
  --resource "api://redpanda-kafka" \
  --query accessToken -o tsv)

# Option B: Client credentials flow (for service accounts / automation)
OIDC_TOKEN=$(curl -s -X POST \
  "https://login.microsoftonline.com/$TENANT_ID/oauth2/v2.0/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "client_id=$APP_CLIENT_ID" \
  -d "client_secret=$APP_CLIENT_SECRET" \
  -d "scope=api://redpanda-kafka/.default" \
  -d "grant_type=client_credentials" | jq -r '.access_token')

# Inspect the token claims
echo "$OIDC_TOKEN" | cut -d. -f2 | base64 -d 2>/dev/null | jq '{aud, preferred_username, email, oid, iss}'
```

### Step 7: Create ACLs for Entra ID principals

The principal name depends on your `oidc_principal_mapping`:

```bash
# If using $.preferred_username:
kubectl exec -n redpanda redpanda-0 -c redpanda -- \
  rpk security acl create \
    --allow-principal "User:alice@contoso.com" \
    --operation all --topic '*' --group '*' --cluster

# If using $.oid (Azure object ID):
kubectl exec -n redpanda redpanda-0 -c redpanda -- \
  rpk security acl create \
    --allow-principal "User:a1b2c3d4-e5f6-7890-abcd-ef1234567890" \
    --operation all --topic '*' --group '*' --cluster
```

### Step 8: Connect to the OIDC listener

Once rpk supports OAUTHBEARER ([PR #30169](https://github.com/redpanda-data/redpanda/pull/30169)):

```bash
BROKER_LB=$(kubectl get svc redpanda-external -n redpanda \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

rpk topic list \
  --brokers "$BROKER_LB:9094" \
  -X tls.enabled=true \
  -X tls.ca=certs/ca.crt \
  -X sasl.mechanism=OAUTHBEARER \
  -X "pass=token:${OIDC_TOKEN}"
```

Until then, use the `confluent-kafka` Python client or a Java Kafka client (see Step 8 in the Kind demo above).

### Differences for the mTLS listener on AKS

For on-prem clients connecting via Azure ExpressRoute:

1. **Internal load balancer**: Add the annotation `service.beta.kubernetes.io/azure-load-balancer-internal: "true"` to route mTLS traffic over ExpressRoute rather than the public internet.

2. **Client certificate CA**: On AKS, you likely want to use a corporate CA (not cert-manager self-signed) for the mTLS client certificates. Configure the Redpanda TLS cert to trust the corporate CA:

   ```yaml
   tls:
     certs:
       corporate-ca:
         secretRef:
           name: corporate-ca-secret   # pre-created Secret with your corporate CA
         caEnabled: true
   ```

   Then reference `cert: corporate-ca` on the mTLS listener.

3. **Azure NSG rules**: Ensure the Network Security Group on the AKS subnet allows:
   - Inbound on port 9094 (OIDC listener) from internet or Application Gateway
   - Inbound on port 9095 (mTLS listener) from the ExpressRoute CIDR only

4. **DNS**: Use Azure DNS or an external DNS to map friendly names to the LoadBalancer IPs:
   - `kafka.contoso.com` → OIDC listener (public LB IP)
   - `kafka-internal.contoso.com` → mTLS listener (internal LB IP)

### Entra ID security considerations

- **Token lifetime**: Entra ID access tokens default to 1 hour. Kafka clients must refresh tokens before expiry. The `confluent-kafka` client handles this via the `oauth_cb` callback; Java clients use `login.refresh.min.period.seconds`.

- **Conditional Access**: Entra ID Conditional Access policies (MFA, device compliance, IP restrictions) apply to token issuance. If your Conditional Access policy blocks token acquisition, the Kafka client will fail to authenticate. Test with a service principal if user-facing policies are restrictive.

- **App roles vs groups**: For fine-grained authorization, configure App Roles in the Entra ID app registration and map them to Redpanda ACLs using `oidc_principal_mapping: "$.roles"` or a custom claim.

- **Multi-tenant**: If you need clients from multiple Entra ID tenants, change the app registration to `--sign-in-audience AzureADMultipleOrgs` and use the `common` or `organizations` endpoint for `oidc_discovery_url`:
  ```
  https://login.microsoftonline.com/organizations/v2.0/.well-known/openid-configuration
  ```

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
