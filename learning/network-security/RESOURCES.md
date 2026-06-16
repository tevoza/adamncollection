# Networking Security Resources

## Knowledge

- [RFC 8446: TLS 1.3](https://www.rfc-editor.org/rfc/rfc8446)
  The authoritative TLS 1.3 spec. Use for handshake flow, record layer behavior, key schedule, and certificate/authentication details.
- [RFC 5280: X.509 PKI Certificate and CRL Profile](https://www.rfc-editor.org/rfc/rfc5280)
  The core certificate profile. Use for certificate fields, extensions, path validation, and trust chains.
- [RFC 2986: PKCS #10 Certification Request Syntax](https://www.rfc-editor.org/rfc/rfc2986)
  The CSR format. Use when explaining how a key pair turns into a certificate request.
- [RFC 6749: OAuth 2.0 Authorization Framework](https://www.rfc-editor.org/rfc/rfc6749)
  The base OAuth 2.0 framework. Use for authorization code flow, client credentials, access tokens, and refresh tokens.
- [RFC 7519: JSON Web Token](https://www.rfc-editor.org/rfc/rfc7519)
  The JWT format and validation rules. Use to separate token format from authorization protocol.
- [RFC 4120: Kerberos V5](https://www.rfc-editor.org/rfc/rfc4120)
  The Kerberos protocol. Use for understanding ticket-based authentication in enterprise environments.
- [.NET: `HttpClientHandler.ClientCertificates`](https://learn.microsoft.com/en-us/dotnet/api/system.net.http.httpclienthandler.clientcertificates?view=net-9.0)
  How .NET presents client certificates for mTLS.
- [.NET: `X509Store`](https://learn.microsoft.com/en-us/dotnet/api/system.security.cryptography.x509certificates.x509store?view=net-9.0)
  How .NET accesses certificate stores on the platform.
- [.NET: `X509Chain`](https://learn.microsoft.com/en-us/dotnet/api/system.security.cryptography.x509certificates.x509chain?view=net-9.0)
  How .NET builds and evaluates certificate chains.
- [OpenSSL: `openssl-s_client`](https://docs.openssl.org/3.0/man1/openssl-s_client/)
  Inspect live TLS sessions and certificate chains. Use for debugging handshakes, trust, and server presentation behavior.
- [OpenSSL: `openssl-req`](https://docs.openssl.org/3.0/man1/openssl-req/)
  Create and inspect CSRs, generate private keys, and produce test certificates.
- [OpenSSL: `openssl-x509`](https://docs.openssl.org/3.0/man1/openssl-x509/)
  Print, inspect, and sign certificates. Use for understanding certificate fields and local test certs.
- [OpenSSL: `openssl-s_server`](https://docs.openssl.org/3.0/man1/openssl-s_server/)
  Run a local TLS server for experiments, including client certificate requests and mTLS tests.

## Gaps

- A current Windows certificate store overview
  Useful for explaining where trust lives on Windows and how LocalMachine vs CurrentUser differs.
