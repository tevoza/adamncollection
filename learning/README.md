# Learning Workspace

This folder is for experimenting with agent workflows before we turn anything into blog content or project changes.

Current intent:

- try Matt Pocock's teach-oriented skill here
- keep notes, examples, and practice runs separate from the Hugo site
- avoid publishing anything from this folder to the blog

Suggested flow:

1. Pick a topic you want to understand better.
2. Ask the agent to teach it back using the skill's style.
3. Capture the explanation, questions, and any follow-up notes in this folder.

If we want, we can later add subfolders like `topics/`, `sessions/`, or `exercises/` as the learning workspace grows.

## Current workspaces

- `network-security`: trust, TLS, certificates, identity-adjacent application security, and secure delivery.
- `network-infrastructure-routing`: ingress, egress, load balancers, proxies, routing rules, and traffic-flow debugging.

## Topic folder pattern

Each topic under `learning/` has converged on the same shape. Follow it when starting a new one:

- `MISSION.md` — why this topic, what success looks like, constraints, and out-of-scope items.
- `README.md` — topic-specific intro and current state.
- `RESOURCES.md` — links/references used for this topic.
- `NOTES.md` — running working notes.
- `lessons/` — rendered lesson output (e.g. from a teach-back skill), numbered (`0001-...`, `0002-...`).
- `reference/` — durable reference material distilled from lessons (e.g. a glossary).

Not every topic needs every piece on day one (`lessons/`/`reference/` show up once there's something to put in them), but new topics should start from this shape rather than inventing a new one.
