# Network Infrastructure Routing Resources

## Knowledge

- [Kubernetes: Ingress](https://kubernetes.io/docs/concepts/services-networking/ingress/)
  Use for: what Kubernetes means by Ingress, how rules map external HTTP/S traffic to Services, and why an ingress controller is required.
- [Kubernetes: Service](https://kubernetes.io/docs/concepts/services-networking/service/)
  Use for: ClusterIP, NodePort, LoadBalancer, service discovery, and how Kubernetes exposes groups of pods.
- [Kubernetes: Gateway API](https://kubernetes.io/docs/concepts/services-networking/gateway/)
  Use for: the newer Kubernetes traffic-routing model and how Gateway, GatewayClass, and routes split infrastructure and application ownership.
- [NGINX: Reverse Proxy](https://docs.nginx.com/nginx/admin-guide/web-server/reverse-proxy/)
  Use for: concrete reverse-proxy mechanics, upstream groups, proxy headers, and request forwarding behavior.
- [Envoy: Load Balancing](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/load_balancing/load_balancing)
  Use for: modern proxy load-balancing concepts, upstream clusters, priorities, locality, and endpoint selection.
- [HAProxy: Load balancing tutorials](https://www.haproxy.com/documentation/haproxy-configuration-tutorials/load-balancing/)
  Use for: practical layer 4 and layer 7 load-balancing examples, health checks, algorithms, and backend pools.

## Wisdom (Communities)

- [Kubernetes Slack](https://slack.k8s.io/)
  Use for: asking focused Kubernetes networking and ingress-controller questions after forming a minimal reproducible example.
- [CNCF Community Groups](https://community.cncf.io/)
  Use for: finding practitioner talks and community discussions around cloud-native traffic routing, gateways, and proxies.

## Gaps

- Add one provider-specific resource later if the mission shifts toward AWS, Azure, GCP, Cloudflare, or on-prem load balancing.
- Add one debugging-focused resource once the first routing mental model is established.

