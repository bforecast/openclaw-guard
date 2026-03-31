#!/bin/bash
export HTTPS_PROXY=http://host.docker.internal:8080
export HTTP_PROXY=http://host.docker.internal:8080
export NODE_EXTRA_CA_CERTS=/root/.mitmproxy/mitmproxy-ca-cert.pem
echo 'Proxy environment configured.'
