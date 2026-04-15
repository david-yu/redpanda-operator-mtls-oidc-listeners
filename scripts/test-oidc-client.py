#!/usr/bin/env python3
"""Test OIDC/OAUTHBEARER authentication against a Redpanda Kafka listener."""
import json
import os
import sys
import urllib.request
import urllib.parse
import ssl

# Get OIDC token from Dex
dex_url = os.environ.get("DEX_URL", "http://dex.dex.svc.cluster.local:5556")
token_url = f"{dex_url}/dex/token"
data = urllib.parse.urlencode({
    "grant_type": "password",
    "client_id": "redpanda",
    "client_secret": "redpanda-secret",
    "username": "user@example.com",
    "password": "password",
    "scope": "openid email profile",
}).encode()

req = urllib.request.Request(token_url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
try:
    resp = urllib.request.urlopen(req)
    token_data = json.loads(resp.read())
    access_token = token_data["access_token"]
    print(f"[PASS] Obtained OIDC token from Dex (length={len(access_token)})")
except Exception as e:
    print(f"[FAIL] Could not obtain OIDC token: {e}")
    sys.exit(1)

# Test Kafka connection with OAUTHBEARER
try:
    from kafka import KafkaProducer, KafkaConsumer
    from kafka.oauth.abstract import AbstractTokenProvider

    class DexTokenProvider(AbstractTokenProvider):
        def token(self):
            return access_token

    broker = os.environ.get("KAFKA_BROKER", "redpanda.redpanda.svc.cluster.local:30094")
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    producer = KafkaProducer(
        bootstrap_servers=broker,
        security_protocol="SASL_SSL",
        sasl_mechanism="OAUTHBEARER",
        sasl_oauth_token_provider=DexTokenProvider(),
        ssl_context=ssl_ctx,
    )
    producer.send("oidc-test", value=b"hello from OIDC client").get(timeout=10)
    producer.close()
    print("[PASS] Produced message to oidc-test topic via OAUTHBEARER")

    consumer = KafkaConsumer(
        "oidc-test",
        bootstrap_servers=broker,
        security_protocol="SASL_SSL",
        sasl_mechanism="OAUTHBEARER",
        sasl_oauth_token_provider=DexTokenProvider(),
        ssl_context=ssl_ctx,
        auto_offset_reset="earliest",
        consumer_timeout_ms=10000,
        group_id="oidc-test-group",
    )
    for msg in consumer:
        print(f"[PASS] Consumed message: {msg.value.decode()}")
        break
    else:
        print("[FAIL] No messages consumed")
    consumer.close()

except ImportError:
    print("[SKIP] kafka-python not installed, testing with raw socket TLS handshake only")
    import socket
    broker = os.environ.get("KAFKA_BROKER", "redpanda.redpanda.svc.cluster.local:30094")
    host, port = broker.rsplit(":", 1)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            print(f"[PASS] TLS handshake succeeded to {broker}, cipher={ssock.cipher()}")
except Exception as e:
    print(f"[FAIL] Kafka OAUTHBEARER connection failed: {e}")
    sys.exit(1)
