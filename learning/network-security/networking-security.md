# Networking Security Teach Session

## Goal

Build an engineer-level mental model of how trust and authentication work across TLS, certificates, app authentication, and token-based auth.

## Scope

We will cover:

- TLS and SSL handshakes
- X.509 certificates, CSRs, public/private keys, CAs, and signature algorithms
- Application authentication patterns like HTTPS, mTLS, Kerberos, trust stores, and certificate handling
- OpenSSL demos
- .NET-specific certificate and trust behavior
- OAuth 2.0 and JWTs

## Starting Point

This topic is big, so the right move is to learn it in layers:

1. Transport security: what TLS actually does.
2. Identity material: what lives in certificates and CSRs.
3. Trust decisions: how a client decides whether to trust a server.
4. Mutual auth: how client certificates change the flow.
5. App auth: how OAuth 2.0 and JWTs solve a different problem from TLS.
6. Platform specifics: Windows, Linux, and .NET.

## Core Mental Model

Keep these distinctions separate:

- TLS protects a connection.
- Certificates bind identities to public keys.
- CAs vouch for certificates by signing them.
- Trust stores decide which CAs you accept.
- mTLS proves both sides of a connection.
- OAuth 2.0 authorizes access.
- JWT is a token format, not an authentication protocol by itself.

## Session Prompts

Use these when teaching back:

- Explain TLS as if I understand networking but not crypto.
- Walk me through a certificate from subject to signature.
- Show the difference between a CSR and a certificate.
- Explain how a server decides whether to trust a client certificate.
- Compare TLS authentication, mTLS, Kerberos, and OAuth 2.0.
- Show how Windows and Linux trust stores differ.
- Show how .NET apps load, validate, and present certificates.

## OpenSSL Practice Ideas

- Inspect a live certificate chain with `openssl s_client`.
- Generate a private key and CSR with `openssl req`.
- Self-sign a test certificate and compare it to a CA-issued one.
- Spin up a tiny local TLS endpoint and inspect the handshake.

## Understanding Checks

I should be able to answer:

- What exactly happens during the TLS handshake?
- Why does a certificate need both a public key and a CA signature?
- What problem does a CSR solve?
- When do I need mTLS instead of plain HTTPS?
- Why is JWT not the same thing as OAuth 2.0?
- Where does trust live on Windows, Linux, and in .NET?

## Next Step

Start with TLS 1.3 and certificates, then build outward to mTLS, trust stores, and OAuth 2.0.
