# Mission: Engineer-level network security

## Why
I want to confidently design, debug, and explain secure networked systems in real projects. That means understanding TLS, certificates, trust stores, mTLS, Kerberos, OAuth 2.0, JWTs, and how all of this behaves in Windows, Linux, and .NET without treating any of it like magic. In practice, I usually work in a corporate environment with an internal CA, so I need to understand enterprise trust chains as well as public web PKI.

## Success looks like
- I can explain a TLS handshake and know what each side proves
- I can inspect a certificate, CSR, and chain and explain the differences
- I can reason about internal CA chains, intermediates, and corporate trust stores
- I can tell when I need HTTPS, mTLS, Kerberos, or OAuth 2.0
- I can debug certificate and trust issues in Windows, Linux, and .NET
- I can use OpenSSL to inspect and generate the artifacts involved

## Constraints
- Prefer practical, engineer-focused explanations over cryptography theory
- Keep lessons short and interactive
- Use real commands and real platform behavior where possible
- Prioritize enterprise/internal-CA scenarios where they differ from public internet examples

## Out of scope
- Deep cryptography proofs
- PKI governance and compliance policy
- Identity architecture beyond the needs of secure application delivery
