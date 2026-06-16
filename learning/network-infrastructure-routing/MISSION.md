# Mission: Engineer-level traffic routing infrastructure

## Why
I want to confidently explain, design, and debug how production traffic moves from clients to the right backend service. That means understanding ingresses and egresses, load balancers, proxies, and routing rules well enough to reason about real incidents instead of treating the network path like a black box.

## Success looks like
- I can draw the path of a request from client to backend across DNS, load balancers, proxies, ingress controllers, services, and pods or servers
- I can tell when a component is acting at layer 4 versus layer 7, and what routing choices each layer can make
- I can explain the difference between ingress, egress, forward proxy, reverse proxy, and service load balancing
- I can debug common traffic failures involving health checks, host headers, paths, ports, timeouts, retries, and backend availability
- I can read a small routing configuration and predict where traffic will go

## Constraints
- Keep this separate from the existing network-security workspace
- Prefer practical infrastructure reasoning over vendor-specific certification study
- Keep lessons short, visual, and interactive
- Use trusted primary documentation before turning concepts into lessons

## Out of scope
- TLS, certificate chains, mTLS, Kerberos, OAuth 2.0, JWTs, and trust-store behavior
- Cryptography, identity architecture, and authentication policy
- Full service mesh architecture beyond the basic traffic-routing concepts needed to understand proxies

