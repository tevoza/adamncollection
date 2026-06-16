# Network Infrastructure Routing

Teach-style workspace for learning how production traffic reaches the right service.

## Workspace name

Use `network-infrastructure-routing` for this workspace.

This keeps it distinct from `learning/network-security`, which is about trust, identity, certificates, and secure application delivery. This workspace is about path selection, request flow, availability, routing layers, and the operational behavior of infrastructure components.

## File naming

- Lessons: `lessons/0001-<dash-case-skill>.html`
- References: `reference/<dash-case-topic>.html`
- Learning records: `learning-records/0001-<dash-case-insight>.md`
- Scratch notes: `NOTES.md`

## Topic boundaries

In scope:

- ingress and egress traffic paths
- reverse proxies and forward proxies
- layer 4 vs layer 7 load balancing
- health checks, retries, timeouts, and connection draining
- host, path, header, and service-based routing
- Kubernetes Ingress, Gateway API, Services, and common controller behavior
- practical traffic debugging from client to backend

Out of scope here:

- TLS, certificates, mTLS, OAuth, Kerberos, JWTs, and trust stores
- firewall policy and zero-trust architecture except where they directly affect routing behavior
- deep cloud-provider billing or compliance design

