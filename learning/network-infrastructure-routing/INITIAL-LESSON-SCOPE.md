# Initial Lesson Scope

## Proposed sequence

1. `0001-request-path-map.html`
   Build the basic mental model: client, DNS, load balancer, reverse proxy or ingress controller, service, backend. Skill: given a simple architecture, trace one request hop by hop.

2. `0002-layer-4-vs-layer-7-routing.html`
   Separate TCP-level forwarding from HTTP-aware routing. Skill: classify a routing decision as layer 4 or layer 7 and predict what information the component can inspect.

3. `0003-ingress-vs-service-vs-loadbalancer.html`
   Clarify Kubernetes Service, LoadBalancer Service, Ingress, and ingress controller. Skill: choose the right Kubernetes object for a specific exposure need.

4. `0004-forward-proxy-vs-reverse-proxy.html`
   Distinguish client-side egress control from server-side request entry. Skill: identify which side of a connection a proxy represents and what problem it solves.

5. `0005-health-checks-timeouts-and-retries.html`
   Introduce the operational knobs that decide whether traffic is sent, retried, drained, or failed. Skill: diagnose a basic "works locally, fails through the load balancer" scenario.

## First lesson recommendation

Start with `0001-request-path-map.html`.

Reason: ingress, egress, proxies, and load balancers all become easier once the learner can draw the path of a request and name which component owns each routing decision.

