---
title: "SSL, TLS and Certificates: What Backend Developers Actually Need to Understand"
date: 2026-10-31T20:00:00+02:00
draft: true
tags: ["software", "security", "tls", "ssl", "certificates", "pki", "mtls", "openssl", "cryptography", "ai-generated"]
description: "Keys, signatures, certificates, chains of trust, the TLS 1.3 handshake, mutual TLS and the OpenSSL commands that go with them, explained for developers who use HTTPS every day and want to know what it's actually doing."
---

Most backend developers use TLS every day and think about it only when it breaks. When it does break, the error messages are terse ("unable to get local issuer certificate", "PKIX path building failed", "the remote certificate is invalid according to the validation procedure"), the files have extensions that mean nothing (`.cer`, `.crt`, `.pem`, `.pfx`), and the fix someone finds on Stack Overflow usually turns validation off.

This post builds the mental model that makes those errors readable: keys and signatures, what a certificate is and why anyone trusts one, the TLS handshake, mutual TLS, and the OpenSSL commands you'll actually use. It's organised around one realistic job:

> **The bank integration.** Your payments service is ASP.NET Core in a Linux container. It has to call a bank's payment-initiation API. The bank's onboarding pack contains a sandbox URL, a file called `bank-ca-bundle.pem`, and an instruction: *"Generate a 2048-bit RSA or P-256 key, send us a CSR with CN=payments-client.example.com, and we'll return your client certificate."* A week later you get `client.cer` back. The first call fails with `AuthenticationException: The remote certificate is invalid according to the validation procedure`. A colleague suggests `ServerCertificateCustomValidationCallback = (_, _, _, _) => true`.

Don't take the colleague's suggestion. By the end there's a proper fix, and every file and step in it will make sense.

The OpenSSL output shown (trimmed at `...`) comes from OpenSSL 3.1.4 against a throwaway CA. Commands aimed at the bank's sandbox are the same commands with a fictional hostname, so they weren't run.

## Two problems, two kinds of cryptography

Secure communication between two parties who've never met has to solve two separate problems:

- **Confidentiality.** Nobody else can read what we're saying.
- **Authenticity.** I'm talking to who I think I'm talking to, and the message hasn't been altered.

Developers tend to fold these into "encryption", but they're different properties, solved with different operations. A connection that's encrypted but not authenticated is a private conversation with someone who may be an attacker, which is what you get when you turn certificate validation off.

### Symmetric encryption: fast, but needs a shared secret

Symmetric encryption uses **one key** for both encrypting and decrypting. AES and ChaCha20 are the ones you'll meet. In TLS they're used in AEAD modes (AES-GCM, ChaCha20-Poly1305), which encrypt and detect tampering in one step: change a byte of ciphertext and decryption fails rather than returning garbage. TLS 1.3 allows only AEAD ciphers for protecting records.[^tls13records]

Symmetric ciphers are fast (modern CPUs have AES instructions built in). Their problem is the key: both sides need the same secret, and if you could send a secret safely over the network you wouldn't need encryption in the first place.

### Asymmetric cryptography: a key pair

Asymmetric (public-key) cryptography uses a **key pair**: two mathematically linked keys.

- The **public key** can be given to anyone. It goes in certificates and gets emailed around.
- The **private key** must never leave its owner. Whoever holds it *is* that identity as far as cryptography is concerned.

You can derive the public key from the private key, but not the other way round.

Asymmetric operations are much slower, so they're never used for bulk data. In TLS they do two narrow jobs: **agreeing a shared secret** with a stranger over an open network (key exchange, using Diffie–Hellman on elliptic curves, written ECDHE), and **proving identity** (signatures, using RSA, ECDSA or Ed25519). The agreed secret then feeds the fast symmetric cipher. So the answer to "symmetric or asymmetric?" is both. Newer clients increasingly do the key exchange as a hybrid of X25519 and the post-quantum ML-KEM (`X25519MLKEM768`, which OpenSSL 3.5 offers by default), but the shape of the handshake is the same.[^openssl35]

### Two operations, and which key does what

A key pair supports two operations that are easy to mix up, and this is the most important distinction in the post:

![Two uses of a key pair. Top: Alice encrypts a message with Bob's public key, producing ciphertext that only Bob's private key can decrypt. Bottom: Alice hashes the message with SHA-256 and signs the digest with her private key; Bob re-hashes the message and verifies the signature with Alice's public key](encrypt-vs-sign.svg)

**Encrypting for confidentiality uses the *recipient's* public key.** Anyone can encrypt a message for Bob, because his public key is public. Only Bob can decrypt it, because only he has the private key.

**Signing for authenticity uses the *sender's* private key, and verification uses the sender's public key.** A valid signature proves the message came from whoever holds Alice's private key and hasn't changed since. Anyone can check it, and nobody can forge it.

You'll often hear signing described as "encrypting with the private key". That's loosely how textbook RSA works, and misleading in general: ECDSA and Ed25519 are signature-only algorithms with no encryption operation at all, and real RSA signatures use padding schemes that make them a separate operation from RSA encryption.[^rfc8017] Think of **sign** and **verify** as their own operations.

### Hashing, and why signatures sign a hash

A cryptographic hash function (SHA-256, SHA-384) turns input of any size into a fixed-size **digest**: the same input always gives the same digest, you can't work back to the input, and you can't find two inputs with the same digest (collision resistance). There's no key and nothing to decrypt; it's a fingerprint, not encryption.

Signatures sign the **digest** of a message, not the message itself. That makes the private-key operation cost the same for a 2KB certificate as for a 2GB file, and collision resistance makes a signature over the digest effectively a signature over the content. It's also why weak hashes are dangerous: after the first practical SHA-1 collision in 2017, an attacker who could influence what got signed could prepare two documents with the same digest, get one signed, and reuse the signature on the other.[^shattered] Publicly trusted CAs no longer sign certificates with SHA-1.[^cabbr]

One sentence to keep: **the hash says "this exact content", and the private key says "I vouch for it".**

## Certificates: binding a name to a key

Signatures leave one problem. To verify that a message came from `api.bank.example`, you need `api.bank.example`'s public key. If you fetch that key over the network, an attacker in the middle can hand you *their* public key instead, and every signature they produce will verify perfectly.

A **certificate** solves this. It's a document that says *"the public key below belongs to `api.bank.example`"*, signed by someone you already trust.

### What's actually in one

Certificates follow the X.509 format.[^rfc5280] Here's a client certificate like the one the bank will return, issued here by the throwaway demo CA and printed with `openssl x509 -in client.crt -noout -text` (trimmed):

```text
Certificate:
    Data:
        Version: 3 (0x2)
        Serial Number:
            05:53:db:9f:b4:d9:78:ab:90:c1:f7:b0:3c:0a:ee:37:8e:54:f9:e1
        Signature Algorithm: ecdsa-with-SHA256
        Issuer: CN = Demo Issuing CA 1
        Validity
            Not Before: Oct 31 18:38:38 2026 GMT
            Not After : Dec  2 18:38:38 2027 GMT
        Subject: C = ZA, O = Example Payments Ltd, CN = payments-client.example.com
        Subject Public Key Info:
            Public Key Algorithm: id-ecPublicKey
                Public-Key: (256 bit)
                pub:
                    04:94:71:93:b8:00:f7:2d:68:cd:bc:a1:44:a9:34:
                    ...
                NIST CURVE: P-256
        X509v3 extensions:
            X509v3 Basic Constraints: critical
                CA:FALSE
            X509v3 Key Usage: critical
                Digital Signature
            X509v3 Extended Key Usage:
                TLS Web Client Authentication
            X509v3 Subject Alternative Name:
                DNS:payments-client.example.com
            X509v3 Authority Key Identifier:
                71:A5:9D:A2:28:64:E4:7F:04:8F:64:C7:DD:3E:07:BE:ED:18:4C:A0
            ...
    Signature Algorithm: ecdsa-with-SHA256
    Signature Value:
        30:45:02:20:22:25:b7:57:64:e1:ec:5c:e1:ae:c2:49:4b:94:
        ...
```

Field by field:

- **Subject**: who the certificate is about, as a distinguished name (`CN=`, `O=`, `C=`).
- **Subject Alternative Name (SAN)**: the names the certificate is actually valid for: DNS names, IP addresses, URIs. For TLS server identity, **this is the field that matters**: hostname checking uses the SAN, and current rules forbid falling back to the subject's CN.[^rfc9525] Browsers enforce this, but some backend stacks still fall back to the CN when there's no DNS SAN (OpenSSL's hostname check does by default[^checkhost]).
- **Issuer**: who signed it. It's the Subject of the next certificate up the chain.
- **Validity**: `Not Before` and `Not After`. Outside that window the certificate is invalid.
- **Subject Public Key Info**: the public key. **Only the public key.**
- **Basic Constraints**: `CA:TRUE` means this certificate may sign other certificates, and `CA:FALSE` means it's an end-entity (leaf). `pathlen` limits how many CAs may sit below it.
- **Key Usage / Extended Key Usage**: what the key may be used for. `serverAuth` lets a certificate identify a TLS server, and `clientAuth` lets it identify a TLS client. A certificate with no EKU extension isn't restricted by it,[^rfc5280eku] and one with only `serverAuth` is rejected as a client certificate (a real failure mode, covered under mTLS).
- **Serial number**: unique per issuer, and used for revocation.
- **Signature**: the issuer's signature over everything above it, made with the **issuer's** private key.

Two consequences:

**A certificate contains a public key, never its private key.** Servers send their certificate to anyone who connects. If you've been given "the certificate" and something asks for a private key, the key is somewhere else: on the machine that generated the CSR, in a key vault, or inside a `.pfx`. Nobody can recreate it from the certificate; if it's lost, you generate a new key and get a new certificate.

**A CA doesn't sign your messages or your traffic.** It signs the certificate once, at issuance, and isn't involved after that. During a handshake your server proves it holds the private key by signing with it, and the client checks that signature with the public key from the certificate. The CA's signature is only what makes the client believe that public key is yours.

### Certificate Authorities, and why there are two kinds

A **Certificate Authority** is an organisation whose job is to check that an applicant controls a name and then sign a certificate saying so. For public web certificates that means proving control of the domain. "Organisation validated" certificates also check the legal entity.

Your machine trusts a CA because its certificate is in a **trust store**:

- the operating system store (Windows certificate store, macOS Keychain, `/etc/ssl/certs` on Debian, Ubuntu and Alpine, `/etc/pki/tls/certs` on RHEL and Fedora, maintained by packages like `ca-certificates`);
- the Java runtime's own `cacerts` file, which is separate from the OS store;
- whatever your container base image happens to ship. A slim image with no `ca-certificates` package trusts nothing. On Debian-family images, install the package, and to add a private CA machine-wide, copy its PEM into `/usr/local/share/ca-certificates/` with a `.crt` extension and run `update-ca-certificates`.[^updateca]

The certificates in a trust store are **root CA** certificates. A root is self-signed (Issuer and Subject are the same), which proves nothing by itself. It's trusted because someone (your OS vendor, the JDK maintainers, your platform team) put it in the store.

A compromised root would mean replacing it on billions of devices, so roots sign very little. They sign **intermediate CA** certificates, and the intermediates sign customer certificates every day. Let's Encrypt, for example, keeps its root key material offline and issues every end-entity certificate from intermediates.[^lechain] If an intermediate is compromised, the CA revokes it with the root key and signs a new one, and no trust store anywhere needs changing.

### The chain of trust

So a typical server presents not one certificate but a chain:

![A certificate chain: the leaf for api.bank.example is signed by Issuing CA 1; Issuing CA 1 is signed by Demo Root CA; the root is self-signed and sits in the client's trust store. The server sends the leaf and the intermediate. The client checks each signature with the issuer's public key, then validity dates, CA flags, the SAN against the hostname, key usage, and optionally revocation](chain-of-trust.svg)

Validation works up the chain one link at a time: each certificate's signature is verified with its issuer's public key, until the client reaches a certificate already in its trust store. Along the way it checks validity dates, that every issuer has `CA:TRUE`, path length limits, key usage, and finally that the leaf's SAN matches the hostname it connected to. RFC 5280 specifies the path-validation part, and hostname matching is RFC 9525's.[^rfc5280path][^rfc9525]

Two practical rules follow:

1. **The server must send its intermediates.** The client has the root but usually not the intermediate. If the server sends only its leaf you get `unable to get local issuer certificate` (OpenSSL), `PKIX path building failed` (Java), or a `PartialChain` status (.NET). Chrome and .NET by default cover for this by downloading the missing intermediate from the certificate's Authority Information Access URL[^rfc5280aia][^downloads] (Firefox uses a preloaded list of known intermediates instead), so a broken server can work from a browser and your .NET service and still fail from Java or curl. .NET reports `PartialChain` only when that download fails, for example from a container with no outbound access. Point the server config at a **full chain** file (leaf, then intermediates).
2. **The server needn't send the root.** The client must already have it; a root that arrives over the wire isn't trusted because it arrived.

Here's the first rule reproduced with OpenSSL: a leaf verified against the root with and without its intermediate.

```text
$ openssl verify -CAfile root.crt client.crt
C = ZA, O = Example Payments Ltd, CN = payments-client.example.com
error 20 at 0 depth lookup: unable to get local issuer certificate
error client.crt: verification failed

$ openssl verify -CAfile root.crt -untrusted int.crt client.crt
client.crt: OK
```

`-untrusted` means "intermediates you may use to build the path, but not trust as anchors", which is exactly their role in a handshake.[^verify]

### Expiry is getting shorter

The maximum lifetime of a publicly trusted TLS certificate is shrinking on a fixed schedule. CA/Browser Forum ballot SC-081 took it from 398 days to 200 days on 15 March 2026, and it drops to 100 days from 15 March 2027 and 47 days from 15 March 2029.[^sc081][^digicert] Short lifetimes limit the damage from a mis-issued or compromised certificate, and in practice force renewal to be automated (ACME, cert-manager, your cloud's managed certificates).

So **certificate expiry is an operational event, not a rare surprise.** Monitor it and automate renewal, especially for the certificates nobody automates: partner client certificates, internal CAs, and the `.pfx` someone copied onto a VM two years ago. Private and partner CAs aren't bound by the public rules, so a bank client certificate may last a year or two, long enough for everyone to forget it exists.

### Revocation

Sometimes a certificate must be invalidated before it expires, usually because its private key leaked. The CA then **revokes** it, and there are two ways to find out:

- **CRL (Certificate Revocation List)**: a signed list of revoked serial numbers that the CA publishes at a URL given in the certificate.[^rfc5280]
- **OCSP (Online Certificate Status Protocol)**: ask the CA's responder about one certificate and get a signed "good" or "revoked" back.[^rfc6960] With **OCSP stapling**, the server fetches that response itself and attaches it to the handshake, so clients don't have to make the call.[^rfc6066]

On the public web revocation is weaker than the textbooks suggest: online checks add latency and leak browsing data to the CA, and clients often "soft-fail" when the responder can't be reached. Let's Encrypt shut its OCSP responders down on 6 August 2025 and now publishes revocation only through CRLs,[^leocsp] and shorter lifetimes are partly a response to the same problem.

For your own systems: know whether your client checks revocation at all, and if you run a private CA for mTLS, revocation is your job (publish a CRL or keep certificates short-lived). .NET shows how easy this is to get wrong. `HttpClient` doesn't check revocation by default (`CertificateRevocationCheckMode` is `NoCheck`), but a hand-built `X509ChainPolicy` defaults to `RevocationMode.Online`, so a client configured to trust a private CA suddenly needs to reach that CA's CRL or OCSP endpoint, and fails if it can't.[^revocationmode]

## SSL vs TLS

SSL was Netscape's protocol from the 1990s. It was standardised and renamed **TLS** (Transport Layer Security) in 1999.[^rfc2246] Every version of SSL is now broken and formally prohibited: SSL 2.0 by RFC 6176 and SSL 3.0 by RFC 7568.[^rfc6176][^rfc7568] TLS 1.0 and 1.1 were formally deprecated in 2021 by RFC 8996.[^rfc8996] That leaves **TLS 1.2 and TLS 1.3**. Accept both, prefer 1.3, and treat anything older as a finding in an audit.

The name "SSL" survives everywhere (`SslStream`, `javax.net.ssl`, OpenSSL, nginx's `ssl_certificate`) without meaning the obsolete protocol is in use. There's no "SSL certificate" as opposed to a "TLS certificate" either: a certificate is an X.509 document and works with any protocol version.

## What actually happens in a TLS handshake

Here's a full TLS 1.3 handshake, the one your `HttpClient` performs on a fresh connection to an HTTPS endpoint:[^rfc8446]

![TLS 1.3 full handshake. The client sends ClientHello with SNI, supported versions, cipher suites and its key share. The server replies with ServerHello and its key share; both sides derive handshake keys. The server then sends, encrypted, EncryptedExtensions, Certificate with its chain, CertificateVerify signing the transcript, and Finished. The client validates the chain and hostname, both derive traffic keys, the client sends Finished, and application data flows encrypted with AES-GCM or ChaCha20-Poly1305](tls13-handshake.svg)

Step by step:

1. **ClientHello.** The client lists the TLS versions and cipher suites it supports, includes **SNI** (Server Name Indication: the hostname it wants, so a server hosting many sites can pick the right certificate), and, new in 1.3, sends a **key share** straight away: the public half of a freshly generated, single-use ECDHE key pair.
2. **ServerHello.** The server picks the version and cipher suite and sends its own ephemeral key share.
3. **Key agreement.** Each side combines its own ephemeral private key with the other's public key share and gets the same shared secret. Someone watching the network sees both public shares and still can't compute it. That's the Diffie–Hellman property. The secret is run through a key derivation function (HKDF) to produce **handshake keys**.[^rfc8446keys][^rfc5869] From here on, everything is encrypted.
4. **EncryptedExtensions.** The remaining negotiated settings, such as ALPN (Application-Layer Protocol Negotiation) choosing HTTP/2.
5. **Certificate.** The server's certificate chain, leaf first.
6. **CertificateVerify.** A signature, made with the **server's private key**, over a hash of the entire handshake so far.[^rfc8446cv] This proves the server *holds* the private key for the certificate it just sent: anyone could replay `api.bank.example`'s public certificate, but only the real server can sign this handshake. Because the signature covers both key shares, it also proves no one in the middle swapped them.
7. **Server Finished.** A MAC (a keyed checksum) over the whole transcript using a key derived from the handshake secret, confirming the server saw the same handshake as the client.
8. **Client validates.** As the server's messages arrive, the client builds and checks the chain, matches the SAN against the hostname it dialled, and checks CertificateVerify against the leaf's public key. Any failure aborts the connection; this is where your `AuthenticationException` comes from.
9. **Client Finished**, and then **application data**, encrypted with symmetric **traffic keys** derived from the same secret, using an AEAD cipher.

**The certificate's key never encrypts anything.** In TLS 1.3 it only *signs*; confidentiality comes entirely from the ephemeral ECDHE exchange. That gives **forward secrecy**: the session keys came from single-use key pairs that were thrown away, so stealing the server's private key next year doesn't decrypt traffic recorded today.

**This changed in TLS 1.3.** TLS 1.2 also allowed "RSA key exchange", where the client encrypted a secret with the server certificate's public key,[^rfc5246rsa] which is where the folklore "TLS encrypts the session key with the server's public key" comes from. Anyone with the server's private key could decrypt every recorded session. TLS 1.3 removed it, so every public-key exchange is forward-secret (PSK-only resumption is the narrow exception).[^rfc8446diff] TLS 1.2 with ECDHE suites is fine; plain RSA key exchange is what "disable non-PFS ciphers" turns off.

**The certificate travels encrypted in 1.3.** In TLS 1.2 the Certificate message, including any *client* certificate in mTLS, went over the wire in the clear. In 1.3 everything after ServerHello is encrypted.[^rfc8446diff]

**It's one round trip.** The client sends its key share before it knows what the server will pick, so a full TLS 1.3 handshake takes one round trip. A full TLS 1.2 handshake took two.[^rfc5246flow]

**Asymmetric is the slow part, and it happens once per connection.** That's why connection reuse matters: a new `HttpClient` per request pays for a fresh handshake every time (and can exhaust sockets).

You can watch a handshake's results with `openssl s_client` against a local test server:

```text
$ openssl s_client -connect localhost:8443 -CAfile root.crt -brief
CONNECTION ESTABLISHED
Protocol version: TLSv1.3
Ciphersuite: TLS_AES_256_GCM_SHA384
Peer certificate: CN = localhost
Hash used: SHA256
Signature type: ECDSA
Verification: OK
Server Temp Key: X25519, 253 bits
```

That's TLS 1.3, AES-256-GCM for the traffic, an ECDSA signature from the server's certificate key, and X25519 as the ephemeral key exchange.[^sclient]

## Mutual TLS: when the server wants to know who you are

In ordinary HTTPS, **only the server authenticates**. The server doesn't know who the client is at the TLS layer; the application handles that with an API key, an OAuth token or a session cookie.

**Mutual TLS (mTLS)** has both sides present a certificate and prove they hold the private key. In TLS 1.3 it adds three messages to the handshake:

![TLS 1.3 handshake with client authentication: the normal handshake plus CertificateRequest from the server listing acceptable CAs and signature algorithms, then the client's own Certificate chain and a CertificateVerify signed with the client's private key before its Finished. The server validates the client chain against its client-CA list](mtls-handshake.svg)

- **CertificateRequest** (server → client): "send me a certificate". It can list the CAs the server will accept, which helps a client holding several certificates choose.[^rfc8446cr]
- **Certificate** (client → server): the client's leaf and intermediates.
- **CertificateVerify** (client → server): a signature over the transcript with the **client's private key**.

The server then does what a client does with a server certificate: builds the chain, checks dates and extended key usage (`clientAuth`), and checks that the chain ends at a CA *it* trusts for clients. Then it maps the certificate's subject or SAN to an identity.

The client CA is usually private, not one of the public web roots, and increasingly it has to be: publicly trusted CAs are dropping `clientAuth` because Chrome's root programme now requires separate server and client hierarchies, and Let's Encrypt stopped issuing certificates with it in 2026.[^leclientauth]

**Both parties prove possession of a private key.** Presenting a certificate isn't enough, since certificates are public. The CertificateVerify signature proves you hold the key, which is why a client needs its private key, not just the `.cer` the bank sent back.

### Where you'll meet it

- **Banking and payments.** Banks and payment networks commonly require mTLS for API access. The client certificate is how the bank knows the request came from your registered system, independent of any token. Open-banking-style APIs often combine both: the OAuth access token is **bound to the client certificate**, so a stolen token is useless without the private key (RFC 8705).[^rfc8705]
- **Service-to-service inside a platform.** Service meshes such as Istio give each workload a short-lived certificate and run mTLS between services automatically, so the service's identity comes from its certificate rather than its IP address.[^istio]
- **B2B integrations and internal APIs** where you want strong client identity without managing shared secrets.

### Failures specific to mTLS

**The client sent no certificate.** In TLS 1.3 the client finishes its side of the handshake *before* the server checks the client certificate, so the error often turns up on the first request rather than at connect time. A test server requiring a client certificate, called without one:

```text
$ printf 'GET / HTTP/1.0\r\n\r\n' | openssl s_client -connect localhost:8443 -CAfile root.crt -quiet
...
error:0A00045C:SSL routines:ssl3_read_bytes:tlsv13 alert certificate required:...:SSL alert number 116
```

`certificate_required` is the alert TLS 1.3 defines for exactly this case.[^rfc8446alert] Forced to TLS 1.2, the same call gets a generic `handshake failure` (alert 40).[^alerts] If your code reports a "connection reset" or an `IOException` on the *first request* to an mTLS endpoint, suspect this. The usual causes: the certificate was loaded without its private key (loading `client.cer` on its own does exactly this), the key is in a store the process can't read, or the certificate's CA isn't in the server's acceptable list.

**The server doesn't trust your certificate's issuer.** You get `unknown ca` (alert 48): the server lacks the right client CA, or, more often, you sent your leaf without its intermediate.

**Wrong certificate purpose.** Present a certificate whose EKU says `serverAuth` only, and a strict server rejects it:

```text
$ openssl s_client -connect localhost:8443 -cert server.crt -key server.key ...
error:0A000413:SSL routines:ssl3_read_bytes:sslv3 alert unsupported certificate:...:SSL alert number 43
```

with the server logging `verify error:num=26:unsuitable certificate purpose`. Client certificates need `clientAuth` in their EKU.

**The TLS terminates somewhere else.** If a load balancer, API gateway or ingress terminates TLS, your application never sees the client certificate. The proxy does the mTLS handshake and forwards the certificate in a request header, and the app must trust that header *only* from the proxy, or anyone can forge it. ASP.NET Core supports this with certificate forwarding middleware (`AddCertificateForwarding` / `UseCertificateForwarding`).[^aspnetcertauth]

### The code, in .NET and Java

**Client side in .NET.** Load the certificate *with* its private key, send its intermediate along with it, and trust the bank's CA for this one client rather than disabling validation. `client.pfx` and `bank-server-root.pem` are built from the bank's files in the walkthrough at the end:

```csharp
// client.pfx holds the client cert, its private key and the CA that issued it.
var pfx           = X509CertificateLoader.LoadPkcs12CollectionFromFile("client.pfx", pfxPassword);
var leaf          = pfx.Single(c => c.HasPrivateKey);
var intermediates = new X509Certificate2Collection(pfx.Where(c => !c.HasPrivateKey).ToArray());
var bankRoot      = X509CertificateLoader.LoadCertificateFromFile("bank-server-root.pem");

var handler = new SocketsHttpHandler
{
    SslOptions = new SslClientAuthenticationOptions
    {
        // Sends the leaf and its intermediates (.NET 8+, see below).
        ClientCertificateContext = SslStreamCertificateContext.Create(leaf, intermediates, offline: true),
        CertificateChainPolicy = new X509ChainPolicy
        {
            TrustMode        = X509ChainTrustMode.CustomRootTrust,
            CustomTrustStore = { bankRoot },
            RevocationMode   = X509RevocationMode.NoCheck, // or Online, if the bank's CA publishes a CRL you can reach
        },
    },
};

var http = new HttpClient(handler) { BaseAddress = new Uri("https://sandbox.api.bank.example/") };
```

`X509CertificateLoader` is the .NET 9+ API for loading certificates and PFX files, available to older targets through the `Microsoft.Bcl.Cryptography` package.[^x509loader] `LoadPkcs12FromFile` returns only the leaf, and `LoadCertificateFromFile` loads a single certificate, so pointed at a bundle it gives you one CA; use `X509Certificate2Collection.ImportFromPemFile` for bundles.

`ClientCertificateContext` (.NET 8+) is there so the intermediates actually get sent.[^clientcertctx] Certificates passed through the older `ClientCertificates` property are chained by the platform, which doesn't see your PFX's other certificates, so the intermediate can silently stay behind and a bank that trusts only its root answers `unknown ca`. On Windows, where the handshake runs outside your process, Microsoft's guidance for clients is that the intermediate must be in the Windows certificate store;[^sslstreamts] on Linux, where this container runs, the context is enough. Step 6 of the walkthrough is how you confirm it.

`CertificateChainPolicy` (.NET 7+) lets one handler trust a specific root without touching the machine's trust store or using an "accept everything" callback.[^chainpolicy] With `CustomRootTrust`, only the roots you list are trusted, which for a bank integration is arguably better than the system store anyway. The explicit `RevocationMode` matters: a new `X509ChainPolicy` checks online by default, and a sandbox CA with no reachable CRL fails with `RevocationStatusUnknown`, which looks exactly as if the trust fix didn't work. In a real service, build the handler once (or register it with `IHttpClientFactory`) and keep the PFX password in a secret store.

**Server side in ASP.NET Core.** Kestrel asks for the certificate, and the certificate authentication handler validates it:

```csharp
builder.WebHost.ConfigureKestrel(o =>
    o.ConfigureHttpsDefaults(https =>
    {
        https.ClientCertificateMode = ClientCertificateMode.RequireCertificate;
        https.AllowAnyClientCertificate(); // leave validation to the handler below
    }));

builder.Services
    .AddAuthentication(CertificateAuthenticationDefaults.AuthenticationScheme)
    .AddCertificate(o =>
    {
        o.AllowedCertificateTypes  = CertificateTypes.Chained;
        o.ChainTrustValidationMode = X509ChainTrustMode.CustomRootTrust;
        o.CustomTrustStore.AddRange(clientCaChain); // issuing CA(s) + root, from ImportFromPemFile
        o.RevocationMode = X509RevocationMode.NoCheck; // or Online, if your CA publishes a CRL
    });
```

By default Kestrel rejects, during the handshake, any client certificate that doesn't chain to the machine's trust store, which is every certificate from a private client CA. `AllowAnyClientCertificate()` hands validation to the handler instead, which then has to be what rejects bad certificates, so require authentication on every endpoint (a fallback authorization policy does this).[^kestrelallowany]

The handler, in turn, validates against the *system* trust store by default, so any client with a publicly trusted certificate would pass chain validation. `CustomRootTrust` restricts it to your client CA; the custom store must contain every issuing certificate including the root, which is why the snippet loads the whole client-CA chain.[^aspnetcertauth]

**Java.** The JDK separates the two roles explicitly: a **keystore** holds *your* certificate and private key, and a **truststore** holds the CAs *you* trust. Both can be PKCS#12 files, and PKCS#12 has been the default keystore type since JDK 9.[^jep229] For a quick setup, the standard system properties are enough:[^jsse]

```bash
java \
  -Djavax.net.ssl.keyStore=client.p12 -Djavax.net.ssl.keyStorePassword=changeit -Djavax.net.ssl.keyStoreType=PKCS12 \
  -Djavax.net.ssl.trustStore=bank-truststore.p12 -Djavax.net.ssl.trustStorePassword=changeit \
  -jar payments.jar
```

and a truststore is built with `keytool`:[^keytool]

```bash
keytool -importcert -alias bank-server-root -file bank-server-root.pem \
  -keystore bank-truststore.p12 -storetype PKCS12 -storepass changeit -noprompt
```

Setting the global `javax.net.ssl.trustStore` *replaces* the JDK's default `cacerts` for the whole process, so every other HTTPS call the service makes fails unless those CAs are in your file too. Beyond a single-purpose service, build an `SSLContext` for the bank client and leave the defaults alone:

```java
var keyStore = KeyStore.getInstance("PKCS12");
try (var in = Files.newInputStream(Path.of("client.p12"))) { keyStore.load(in, keyPass); }
var kmf = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
kmf.init(keyStore, keyPass);

var trustStore = KeyStore.getInstance("PKCS12");
try (var in = Files.newInputStream(Path.of("bank-truststore.p12"))) { trustStore.load(in, trustPass); }
var tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
tmf.init(trustStore);

var ssl = SSLContext.getInstance("TLS");
ssl.init(kmf.getKeyManagers(), tmf.getTrustManagers(), null);
var http = HttpClient.newBuilder().sslContext(ssl).build();
```

When a Java handshake fails and the exception isn't enough, `-Djavax.net.debug=ssl:handshake` prints every handshake message, including which certificate (if any) the client chose to send.[^jsse]

## OpenSSL: the toolkit

OpenSSL is a C library implementing TLS and the cryptography underneath (used by nginx, curl, Python, Node, .NET on Linux and many others), plus the `openssl` command-line tool, the multitool for creating keys and CSRs, inspecting certificates, converting formats and testing connections.[^openssl] On Windows, Git Bash rewrites arguments beginning with `/` as paths, which breaks `-subj "/CN=..."`; set `MSYS_NO_PATHCONV=1` or start the subject with `//`.

### Keys, CSRs and certificates

![Getting a certificate. On your machine: 1, generate a private key, which never leaves; 2, create a CSR containing the public key and requested subject and SAN, signed with the private key. Only the CSR goes to the CA, which 3, checks your identity and 4, returns a certificate with the same public key, the subject, its issuer name, and the CA's signature. 5, the certificate and intermediates are deployed together with the private key](csr-flow.svg)

**A CSR (Certificate Signing Request)** is a PKCS#10 document containing your public key and the identity you're asking for, signed with your private key to prove you hold it.[^rfc2986] **The private key never goes to the CA.** If a partner asks you to email them a private key, the process is wrong.

Generate a key. EC P-256 is small and fast; RSA 3072 is the conservative choice when a counterparty insists on RSA, and 2048 is the minimum publicly trusted CAs accept.[^cabbr]

```bash
# EC P-256
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out client.key

# or RSA 3072
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out client.key
```

Create a CSR with the subject and SAN you need. `-addext` puts the SAN in the request:[^req]

```bash
openssl req -new -key client.key -out client.csr \
  -subj "/C=ZA/O=Example Payments Ltd/CN=payments-client.example.com" \
  -addext "subjectAltName=DNS:payments-client.example.com"
```

Check it before sending it to anyone:

```text
$ openssl req -in client.csr -noout -text -verify
Certificate request self-signature verify OK
Certificate Request:
    Data:
        Subject: C = ZA, O = Example Payments Ltd, CN = payments-client.example.com
        ...
        Requested Extensions:
            X509v3 Subject Alternative Name:
                DNS:payments-client.example.com
```

For local development and tests, you can be your own CA. A self-signed root (`-nodes`, "no DES", writes the private key unencrypted, which is fine for a throwaway test CA and wrong for anything real; OpenSSL 3 also spells it `-noenc`[^req]):

```bash
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
  -keyout root.key -out root.crt -days 3650 -subj "/CN=Demo Root CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign"
```

Then sign a CSR with it (or with an intermediate). The extensions file is where `CA:FALSE`, the EKU and the SAN for the issued certificate get decided:[^x509]

```bash
cat > client.ext <<'EOF'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=clientAuth
subjectAltName=DNS:payments-client.example.com
authorityKeyIdentifier=keyid
EOF

openssl x509 -req -in client.csr -CA int.crt -CAkey int.key -CAcreateserial \
  -days 397 -extfile client.ext -out client.crt
```

### Inspecting things

These are the commands you'll use most:

```bash
# The whole certificate, human-readable
openssl x509 -in cert.pem -noout -text

# Just the parts you usually want
openssl x509 -in cert.pem -noout -subject -issuer -dates -ext subjectAltName,extendedKeyUsage

# SHA-256 fingerprint (what partners quote when "pinning" a certificate)
openssl x509 -in cert.pem -noout -fingerprint -sha256

# Exit code 1 if the certificate expires within 30 days, which makes a cheap monitoring check
openssl x509 -in cert.pem -noout -checkend 2592000
```

**Does this key match this certificate?** Compare the public keys; all three must print the same hash:

```text
$ openssl pkey -in client.key -pubout | openssl sha256
SHA2-256(stdin)= a5d96762051f12da2008d7400d919ff04af54d48c470d6b698288c6f1e1873e5
$ openssl req -in client.csr -noout -pubkey | openssl sha256
SHA2-256(stdin)= a5d96762051f12da2008d7400d919ff04af54d48c470d6b698288c6f1e1873e5
$ openssl x509 -in client.crt -noout -pubkey | openssl sha256
SHA2-256(stdin)= a5d96762051f12da2008d7400d919ff04af54d48c470d6b698288c6f1e1873e5
```

This works for RSA and EC keys, unlike the older `-modulus` trick.

**What is a server actually sending?** `-showcerts` prints every certificate the server sent:

```text
$ openssl s_client -connect localhost:8444 -showcerts
Certificate chain
 0 s:CN = localhost
   i:CN = Demo Issuing CA 1
...
Verify return code: 21 (unable to verify the first certificate)
```

One certificate whose issuer the client doesn't have: a server missing its intermediate. A correct server shows the leaf at depth 0 and its issuer at depth 1, each `i:` matching the next `s:`. Add `-servername host` when connecting by IP or through a tunnel so the server gets SNI, and `-cert`/`-key` to test an mTLS endpoint before writing any code.[^sclient]

### File formats, and why the extension tells you nothing

There are really only two **encodings** of a certificate, plus a couple of **container** formats:

![What each file can hold. A .key holds a private key. A .csr holds a public key and the requested name, signed by the private key. A .crt or .cer holds one certificate, with its public key, in PEM or DER. A chain file such as fullchain.pem holds several certificates, leaf first. A .pfx or .p12 is a password-protected container holding a private key, its certificate and usually the chain. A .p7b holds certificates with no private key](file-formats.svg)

| You see | What it actually is |
|---|---|
| **DER** | The raw binary ASN.1 encoding of the certificate. Starts with byte `0x30`. |
| **PEM** | DER, base64-encoded, between `-----BEGIN CERTIFICATE-----` / `-----END CERTIFICATE-----` lines.[^rfc7468] Text, so it's safe to paste and diff. One file can hold several PEM blocks. |
| `.crt`, `.cer` | A certificate in **either** PEM or DER. The extension is a convention, not a format. Windows tends to use `.cer`. Open it in a text editor: readable `BEGIN` lines mean PEM, binary noise means DER. |
| `.pem` | Anything PEM-encoded: a certificate, a chain, a key, or all three. |
| `.key` | A private key, usually PEM. The header tells you which format: `BEGIN PRIVATE KEY` (PKCS#8, the modern default), `BEGIN ENCRYPTED PRIVATE KEY` (PKCS#8, password-protected), `BEGIN RSA PRIVATE KEY` or `BEGIN EC PRIVATE KEY` (older algorithm-specific formats). |
| `.csr` | A PKCS#10 certificate signing request, usually PEM. |
| `.pfx`, `.p12` | **PKCS#12**: a password-protected binary container holding a private key *and* its certificate, usually with the chain.[^rfc7292] It's the native format for Windows, IIS, Azure and .NET, and works directly as a Java keystore. |
| `.jks` | Java's older proprietary keystore format. PKCS#12 replaced it as the default in JDK 9.[^jep229] |
| `.p7b`, `.p7c` | PKCS#7: certificates (a chain) with **no** private key. Sometimes what a Windows CA hands back. |

A **bundle** or **chain** file is several PEM certificates concatenated. For the chain a server presents, order matters: leaf first, then each issuer (`fullchain.pem` in Let's Encrypt terms). A **CA bundle** given to clients, like the bank's `bank-ca-bundle.pem`, is a set of CAs to trust or build chains with, and order doesn't matter. Count what's inside with `grep -c "BEGIN CERTIFICATE"`, and look at each before assuming which is the root.

Conversions:

```bash
# DER -> PEM, and back
openssl x509 -in cert.cer -inform DER -out cert.pem
openssl x509 -in cert.pem -outform DER -out cert.der

# PKCS#7 (.p7b) -> PEM certificates
openssl pkcs7 -in certs.p7b -inform DER -print_certs -out certs.pem   # drop -inform DER if the file is PEM

# Normalise an old "BEGIN RSA PRIVATE KEY" to PKCS#8
openssl pkey -in old.key -out new.key

# PEM key + cert + chain -> PFX
openssl pkcs12 -export -inkey client.key -in client.crt -certfile ca-chain.pem \
  -name payments-client -out client.pfx

# PFX -> PEM pieces
openssl pkcs12 -in client.pfx -clcerts -nokeys -out client.crt   # just the leaf
openssl pkcs12 -in client.pfx -cacerts -nokeys -out chain.pem    # just the CAs
openssl pkcs12 -in client.pfx -nocerts -nodes  -out client.key   # the key, unencrypted
```

**The OpenSSL 3 PFX gotcha.** OpenSSL 3 encrypts PKCS#12 files with AES-256-CBC and PBKDF2 by default. OpenSSL 1.x used RC2/3DES, and `-legacy` exists to produce the old format for consumers that can't read the new one.[^pkcs12] You can see the difference with `-info`:

```text
$ openssl pkcs12 -in client.pfx -info -noout
MAC: sha256, Iteration 2048
...
PKCS7 Encrypted data: PBES2, PBKDF2, AES-256-CBC, Iteration 2048, PRF hmacWithSHA256
...
Shrouded Keybag: PBES2, PBKDF2, AES-256-CBC, Iteration 2048, PRF hmacWithSHA256

$ openssl pkcs12 -in client-legacy.pfx -info -noout -legacy
MAC: sha1, Iteration 2048
...
PKCS7 Encrypted data: pbeWithSHA1And40BitRC2-CBC, Iteration 2048
...
Shrouded Keybag: pbeWithSHA1And3-KeyTripleDES-CBC, Iteration 2048
```

If a consumer says "invalid password" or "unsupported algorithm" for a PFX you know is correct, re-export with `-legacy` for that consumer only, and treat that file like the bare key, since the legacy algorithms are weak.

## Resolving the bank integration

Back to the scenario, now with all the pieces.

**1. Generate the key and CSR on a machine you control,** with the commands under [Keys, CSRs and certificates](#keys-csrs-and-certificates). Add the SAN even though the bank only asked for the CN; it costs nothing. Send `client.csr`, and keep `client.key` in a key vault or encrypted secret store: lose it and you go back to the bank for a new certificate.

**2. Inspect what comes back.** `openssl x509 -in client.cer -noout -text` (on OpenSSL 1.x, add `-inform DER` for a binary file). Confirm the subject and SAN are what you asked for, `Extended Key Usage` includes `TLS Web Client Authentication`, the dates are sensible, and the public key matches your private key (the three-hash check).

**3. Work out what's in the CA bundle.** List it with `openssl storeutl -noout -text -certs bank-ca-bundle.pem` and note each certificate's subject and issuer. Typically it holds the CA that issued *your* client certificate (the bank needs your chain to end there) and the CA behind the *bank's server* certificate (you need to trust it), which may or may not be the same. Save them separately; the rest of this walkthrough calls them `bank-client-issuing-ca.pem` and `bank-server-root.pem`.

**4. Fix the first error, which is about the bank's server, not your certificate.** `The remote certificate is invalid according to the validation procedure` is *your* client rejecting the *bank's* sandbox certificate, typically issued from a private CA that isn't in any public trust store. Confirm with OpenSSL:

```bash
openssl s_client -connect sandbox.api.bank.example:443 -servername sandbox.api.bank.example -showcerts
```

If the chain ends at the bank's private root, trust *that root for this client*: the `CertificateChainPolicy` with `CustomRootTrust` from the .NET example. Turning validation off would also make the error go away, and make the encryption pointless: anyone who can intercept the connection could impersonate the bank and read your payment instructions and access tokens.

**5. Build the PFX your service loads** from the key, your certificate and its issuing CA:

```bash
openssl pkcs12 -export -inkey client.key -in client.cer -certfile bank-client-issuing-ca.pem \
  -name payments-client -out client.pfx
```

OpenSSL 3.1 reads a DER `client.cer` here as happily as PEM; on older versions, convert it first. If the service had been loading `client.cer` on its own, it had no private key and sent no client certificate: the `certificate required` failure, surfacing as an I/O error on the first request.

**6. Test outside your code first:**

```bash
openssl s_client -connect sandbox.api.bank.example:443 -servername sandbox.api.bank.example \
  -CAfile bank-server-root.pem -cert client.cer -key client.key -cert_chain bank-client-issuing-ca.pem
```

If this connects and verifies but your service doesn't, the problem is in how the service loads or presents the certificate, not the certificates themselves.

**7. Put the expiry date in a calendar and a monitor.** This client certificate won't renew itself.

## Cheat sheet

**Key ideas**

- Public keys can be shared; private keys must stay secret. Whoever holds a private key *is* that identity.
- Encrypting for confidentiality uses the **recipient's public key**.
- Signing uses the **sender's private key**; verifying uses the **sender's public key**.
- Signatures are made over a **hash** of the content.
- A certificate binds an identity (the SAN) to a public key, signed by an issuer. It contains the public key, never the private key.
- A CA signs **certificates**, not messages or traffic.
- Roots sit in trust stores; intermediates do the day-to-day signing; servers must send their intermediates.
- In TLS 1.3, ephemeral ECDHE provides the session keys and forward secrecy; the certificate key only signs (CertificateVerify) to prove possession.
- Asymmetric cryptography sets up the connection, and symmetric AEAD encrypts the traffic.
- mTLS: both sides present a certificate **and** prove possession of its private key.
- SSL is dead; use TLS 1.2 and 1.3. "SSL certificate" is just a name.

**What goes where**

| Thing | Secret? | Who has it |
|---|---|---|
| Private key | Yes, never shared | Its owner only (file, key vault, HSM, i.e. a hardware security module) |
| Public key | No | Inside the CSR and certificate |
| CSR | No | You → CA |
| Certificate | No | Sent to every peer in the handshake |
| Intermediate certificate | No | Sent by the server alongside its leaf |
| Root certificate | No | Pre-installed in the peer's trust store |
| `.pfx` / `.p12` | Yes (it contains the key) | Wherever the service runs |

**Commands**

```bash
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out key.pem      # new key
openssl req -new -key key.pem -out req.csr -subj "/CN=host" -addext "subjectAltName=DNS:host"
openssl req -in req.csr -noout -text -verify                                    # check a CSR
openssl x509 -in cert.pem -noout -text                                          # read a cert
openssl x509 -in cert.pem -noout -subject -issuer -dates -ext subjectAltName
openssl x509 -in cert.pem -noout -checkend 2592000                              # expires in 30d?
openssl x509 -in cert.pem -noout -pubkey | openssl sha256                       # key match (compare with...)
openssl pkey -in key.pem -pubout | openssl sha256                               # ...this
openssl verify -CAfile root.pem -untrusted intermediates.pem cert.pem           # chain check
openssl s_client -connect host:443 -servername host -showcerts                  # what's served
openssl s_client -connect host:443 -cert client.pem -key client.key -cert_chain chain.pem   # test mTLS
openssl x509 -in cert.der -inform DER -out cert.pem                             # DER -> PEM
openssl pkcs12 -export -inkey key.pem -in cert.pem -certfile chain.pem -out bundle.pfx
openssl pkcs12 -in bundle.pfx -nodes -out everything.pem                        # PFX -> PEM
openssl pkcs12 -in bundle.pfx -info -noout                                      # what's in a PFX
```

**Error → likely cause**

| Error | Likely cause |
|---|---|
| `unable to get local issuer certificate` / `PKIX path building failed` / `PartialChain` | Server isn't sending its intermediate, or you don't trust the root (private CA, slim container image). |
| `self-signed certificate in certificate chain` / `UntrustedRoot` | The chain ends at a root that isn't in your trust store. Trust that root deliberately, or fix the server's certificate. |
| `certificate has expired` / `CertificateExpiredException` / `NotTimeValid` | Exactly that, or the machine's clock is wrong. Check both. |
| `hostname mismatch` / `RemoteCertificateNameMismatch` / `No subject alternative DNS name matching` | The name you dialled isn't in the SAN. Calling by IP, an internal alias, or a missing SNI are the usual causes. |
| `tlsv13 alert certificate required` (alert 116), or a reset on the first request to an mTLS endpoint | No client certificate was sent: the certificate was loaded without its key, or not attached to the handler. |
| `unsuitable certificate purpose` / `unsupported certificate` (alert 43) | EKU doesn't allow this use, e.g. a `serverAuth`-only cert used as a client certificate. |
| `unknown ca` (alert 48) | The peer doesn't trust the CA that issued the certificate you presented, or you sent your leaf without its intermediate. |
| `RevocationStatusUnknown` after configuring a custom trust root in .NET | `X509ChainPolicy` defaults to online revocation checking and the CA's CRL can't be reached. Set `RevocationMode` deliberately. |
| PFX "invalid password" or "unsupported algorithm" on an old platform | OpenSSL 3 AES-encrypted PKCS#12. Re-export with `-legacy` for that consumer. |
| `key values mismatch` | The private key and certificate aren't a pair. Compare public-key hashes. |

[^tls13records]: IETF, [RFC 8446, "The Transport Layer Security (TLS) Protocol Version 1.3", §5.2 "Record Payload Protection"](https://www.rfc-editor.org/rfc/rfc8446#section-5.2) — TLS 1.3 records protected only with AEAD algorithms.
[^rfc8017]: IETF, [RFC 8017, "PKCS #1: RSA Cryptography Specifications Version 2.2"](https://www.rfc-editor.org/rfc/rfc8017) — RSA encryption schemes (RSAES-OAEP, RSAES-PKCS1-v1_5) and signature schemes (RSASSA-PSS, RSASSA-PKCS1-v1_5) as separately defined operations with their own encodings.
[^cabbr]: CA/Browser Forum, ["Baseline Requirements for TLS Server Certificates"](https://cabforum.org/working-groups/server/baseline-requirements/documents/) — the rules publicly trusted CAs follow, including permitted signature hash algorithms (no SHA-1), minimum RSA modulus of 2048 bits, and the requirement that server certificates carry a subjectAltName.
[^rfc5280]: IETF, [RFC 5280, "Internet X.509 Public Key Infrastructure Certificate and CRL Profile"](https://www.rfc-editor.org/rfc/rfc5280) — certificate fields, basic constraints, key usage, CRL distribution points and the CRL format.
[^rfc9525]: IETF, [RFC 9525, "Service Identity in TLS"](https://www.rfc-editor.org/rfc/rfc9525) — "The Common Name RDN MUST NOT be used to identify a service"; identities come from subjectAltName; wildcard rules. Obsoletes RFC 6125.
[^rfc5280eku]: IETF, [RFC 5280 §4.2.1.12, "Extended Key Usage"](https://www.rfc-editor.org/rfc/rfc5280#section-4.2.1.12) — `id-kp-serverAuth` and `id-kp-clientAuth`; "If the extension is present", the certificate must be used only for a purpose it lists.
[^lechain]: Let's Encrypt, ["Chains of Trust"](https://letsencrypt.org/certificates/) — "Our root key material is kept safely offline"; end-entity certificates are issued from intermediates.
[^rfc5280path]: IETF, [RFC 5280 §6, "Certification Path Validation"](https://www.rfc-editor.org/rfc/rfc5280#section-6) — the path validation algorithm: signature, validity, name, basic constraints and path length checks for each certificate up to a trust anchor.
[^verify]: OpenSSL, ["openssl-verify"](https://docs.openssl.org/3.1/man1/openssl-verify/) — `-CAfile` for trusted certificates and `-untrusted` for intermediates used to build the chain but not trusted as anchors, plus the meaning of verification error codes.
[^sc081]: CA/Browser Forum, ["Ballot SC081v3: Introduce Schedule of Reducing Validity and Data Reuse Periods"](https://cabforum.org/2025/04/11/ballot-sc081v3-introduce-schedule-of-reducing-validity-and-data-reuse-periods/) — reduction of the maximum validity period from 398 days to 47 days, from March 2026 to March 2029.
[^digicert]: DigiCert, ["Public TLS certificates: 199-day validity"](https://knowledge.digicert.com/alerts/public-tls-certificates-199-day-validity) — the SC-081 schedule by date: 200 days from 15 March 2026, 100 days from 15 March 2027, 47 days from 15 March 2029.
[^rfc6960]: IETF, [RFC 6960, "X.509 Internet Public Key Infrastructure Online Certificate Status Protocol — OCSP"](https://www.rfc-editor.org/rfc/rfc6960) — querying a responder for one certificate's revocation status.
[^rfc6066]: IETF, [RFC 6066 §8, "Certificate Status Request"](https://www.rfc-editor.org/rfc/rfc6066#section-8) — the `status_request` extension that lets a server staple an OCSP response to the handshake.
[^leocsp]: Let's Encrypt, ["Ending OCSP Support in 2025"](https://letsencrypt.org/2024/12/05/ending-ocsp.html) — OCSP URLs dropped from certificates on 7 May 2025, responders turned off on 6 August 2025, and CRLs used exclusively thereafter.
[^rfc6176]: IETF, [RFC 6176, "Prohibiting Secure Sockets Layer (SSL) Version 2.0"](https://www.rfc-editor.org/rfc/rfc6176).
[^rfc7568]: IETF, [RFC 7568, "Deprecating Secure Sockets Layer Version 3.0"](https://www.rfc-editor.org/rfc/rfc7568).
[^rfc8996]: IETF, [RFC 8996, "Deprecating TLS 1.0 and TLS 1.1"](https://www.rfc-editor.org/rfc/rfc8996) — published March 2021.
[^rfc8446]: IETF, [RFC 8446, "The Transport Layer Security (TLS) Protocol Version 1.3"](https://www.rfc-editor.org/rfc/rfc8446) — §2 "Protocol Overview" for the full handshake message flow, key shares in ClientHello, and the 1-RTT handshake.
[^rfc8446keys]: IETF, [RFC 8446 §7.1, "Key Schedule"](https://www.rfc-editor.org/rfc/rfc8446#section-7.1) — handshake and application traffic secrets derived from the (EC)DHE shared secret via HKDF.
[^rfc5869]: IETF, [RFC 5869, "HMAC-based Extract-and-Expand Key Derivation Function (HKDF)"](https://www.rfc-editor.org/rfc/rfc5869).
[^rfc8446cv]: IETF, [RFC 8446 §4.4.3, "Certificate Verify"](https://www.rfc-editor.org/rfc/rfc8446#section-4.4.3) — a signature over the transcript hash using the private key corresponding to the certificate, providing proof of possession.
[^rfc5246rsa]: IETF, [RFC 5246, "The Transport Layer Security (TLS) Protocol Version 1.2", §7.4.7.1 "RSA-Encrypted Premaster Secret Message"](https://www.rfc-editor.org/rfc/rfc5246#section-7.4.7.1) — the client encrypting the premaster secret with the server certificate's public key.
[^rfc8446diff]: IETF, [RFC 8446 §1.2, "Major Differences from TLS 1.2"](https://www.rfc-editor.org/rfc/rfc8446#section-1.2) — static RSA and Diffie–Hellman suites removed so all public-key key exchange provides forward secrecy, and all handshake messages after ServerHello encrypted.
[^sclient]: OpenSSL, ["openssl-s_client"](https://docs.openssl.org/3.1/man1/openssl-s_client/) — `-showcerts`, `-servername`, `-brief`, `-cert`, `-key` and `-cert_chain`.
[^rfc8446cr]: IETF, [RFC 8446 §4.3.2, "Certificate Request"](https://www.rfc-editor.org/rfc/rfc8446#section-4.3.2) — the server requesting client authentication, with `certificate_authorities` and `signature_algorithms` extensions.
[^rfc8705]: IETF, [RFC 8705, "OAuth 2.0 Mutual-TLS Client Authentication and Certificate-Bound Access Tokens"](https://www.rfc-editor.org/rfc/rfc8705) — authenticating OAuth clients with mTLS and binding access tokens to the client certificate.
[^istio]: Istio, ["Security"](https://istio.io/latest/docs/concepts/security/) — workload identities issued as X.509 certificates, automatic certificate rotation, and mutual TLS between workloads.
[^rfc8446alert]: IETF, [RFC 8446 §4.4.2.4, "Receiving a Certificate Message"](https://www.rfc-editor.org/rfc/rfc8446#section-4.4.2.4) — a server that requires client authentication may abort with a `certificate_required` alert if the client sends no certificate.
[^aspnetcertauth]: Microsoft, ["Configure certificate authentication in ASP.NET Core"](https://learn.microsoft.com/en-us/aspnet/core/security/authentication/certauth) — `ClientCertificateMode`, `AllowedCertificateTypes` defaulting to `Chained`, `ChainTrustValidationMode` defaulting to the system store, `CustomTrustStore` needing every issuing certificate including the root, and certificate forwarding behind proxies.
[^x509loader]: Microsoft, ["X509CertificateLoader Class"](https://learn.microsoft.com/en-us/dotnet/api/system.security.cryptography.x509certificates.x509certificateloader) — loading certificates and PKCS#12 files (single certificate or whole collection); built in from .NET 9 and available to earlier targets through the `Microsoft.Bcl.Cryptography` package.
[^chainpolicy]: Microsoft, ["SslClientAuthenticationOptions.CertificateChainPolicy"](https://learn.microsoft.com/en-us/dotnet/api/system.net.security.sslclientauthenticationoptions.certificatechainpolicy) — a customised `X509ChainPolicy` for remote certificate validation, including `TrustMode` and `CustomTrustStore`; available from .NET 7.
[^jep229]: OpenJDK, ["JEP 229: Create PKCS12 Keystores by Default"](https://openjdk.org/jeps/229) — PKCS#12 as the default keystore type from JDK 9, replacing JKS.
[^jsse]: Oracle, ["Java Secure Socket Extension (JSSE) Reference Guide"](https://docs.oracle.com/en/java/javase/21/security/java-secure-socket-extension-jsse-reference-guide.html) — keystores vs truststores, and the `javax.net.ssl.keyStore` / `trustStore` system properties and their defaults.
[^keytool]: Oracle, ["The keytool Command"](https://docs.oracle.com/en/java/javase/21/docs/specs/man/keytool.html) — `-importcert` and `-storetype`.
[^openssl]: OpenSSL, ["openssl"](https://docs.openssl.org/3.1/man1/openssl/) — the command-line tool and its subcommands.
[^rfc2986]: IETF, [RFC 2986, "PKCS #10: Certification Request Syntax Specification"](https://www.rfc-editor.org/rfc/rfc2986) — a CSR as a distinguished name, public key and attributes, signed by the requester's private key.
[^req]: OpenSSL, ["openssl-req"](https://docs.openssl.org/3.1/man1/openssl-req/) — `-new`, `-x509`, `-subj`, `-addext`, `-nodes` and `-verify`.
[^x509]: OpenSSL, ["openssl-x509"](https://docs.openssl.org/3.1/man1/openssl-x509/) — `-req`, `-CA`, `-CAkey`, `-CAcreateserial`, `-extfile`, `-checkend` and the display options.
[^rfc7468]: IETF, [RFC 7468, "Textual Encodings of PKIX, PKCS, and CMS Structures"](https://www.rfc-editor.org/rfc/rfc7468) — the PEM `BEGIN`/`END` format and its labels.
[^rfc7292]: IETF, [RFC 7292, "PKCS #12: Personal Information Exchange Syntax v1.1"](https://www.rfc-editor.org/rfc/rfc7292).
[^pkcs12]: OpenSSL, ["openssl-pkcs12"](https://docs.openssl.org/3.1/man1/openssl-pkcs12/) — AES-256-CBC with PBKDF2 as the OpenSSL 3 default for keys and certificates, and `-legacy` restoring RC2/3DES via the legacy provider.
[^openssl35]: OpenSSL, ["OpenSSL 3.5 release notes"](https://openssl-library.org/news/openssl-3.5-notes/) — "The default TLS keyshares have been changed to offer X25519MLKEM768 and X25519", with hybrid post-quantum groups preferred in the default supported groups.
[^shattered]: Marc Stevens, Elie Bursztein, Pierre Karpman, Ange Albertini and Yarik Markov, ["SHAttered"](https://shattered.io/) — the first practical SHA-1 collision (two different PDFs with the same SHA-1 digest), announced in 2017.
[^updateca]: Debian, ["update-ca-certificates(8)"](https://manpages.debian.org/bookworm/ca-certificates/update-ca-certificates.8.en.html) — certificates with a `.crt` extension in `/usr/local/share/ca-certificates` are added to the system trust store.
[^rfc5280aia]: IETF, [RFC 5280 §4.2.2.1, "Authority Information Access"](https://www.rfc-editor.org/rfc/rfc5280#section-4.2.2.1) — the `caIssuers` access method pointing at where the issuer's certificate can be downloaded.
[^revocationmode]: Microsoft, ["X509ChainPolicy.RevocationMode"](https://learn.microsoft.com/en-us/dotnet/api/system.security.cryptography.x509certificates.x509chainpolicy.revocationmode) — "Online is the default value"; and ["SslClientAuthenticationOptions.CertificateRevocationCheckMode"](https://learn.microsoft.com/en-us/dotnet/api/system.net.security.sslclientauthenticationoptions.certificaterevocationcheckmode) — "The default is `NoCheck`." The `CertificateChainPolicy` page notes the policy's `RevocationMode` supersedes `CertificateRevocationCheckMode`.
[^rfc2246]: IETF, [RFC 2246, "The TLS Protocol Version 1.0"](https://www.rfc-editor.org/rfc/rfc2246) — published January 1999, based on SSL 3.0.
[^rfc5246flow]: IETF, [RFC 5246 §7.3, "Handshake Protocol Overview"](https://www.rfc-editor.org/rfc/rfc5246#section-7.3) — the full TLS 1.2 message flow, with the client's key exchange and Finished sent only after the server's ServerHelloDone.
[^alerts]: IETF, [RFC 8446 §6, "Alert Protocol"](https://www.rfc-editor.org/rfc/rfc8446#section-6) — alert descriptions and numbers, including `handshake_failure(40)`, `unsupported_certificate(43)`, `unknown_ca(48)` and `certificate_required(116)`.
[^sslstreamts]: Microsoft, ["Troubleshoot SslStream authentication issues"](https://learn.microsoft.com/en-us/dotnet/core/extensions/sslstream-troubleshooting) — intermediates supplied via the authentication options aren't sent on Windows unless they're in the certificate store; `SslStreamCertificateContext` for servers; for clients on Windows, "the only solution is to add the certificates to the certificate store manually."
[^kestrelallowany]: Microsoft, ["HttpsConnectionAdapterOptions.AllowAnyClientCertificate"](https://learn.microsoft.com/en-us/dotnet/api/microsoft.aspnetcore.server.kestrel.https.httpsconnectionadapteroptions.allowanyclientcertificate) — "Overrides the current ClientCertificateValidation callback and allows any client certificate." The default when no callback is set, rejecting certificates with any `SslPolicyErrors`, is in Kestrel's [`HttpsConnectionMiddleware`](https://github.com/dotnet/aspnetcore/blob/main/src/Servers/Kestrel/Core/src/Middleware/HttpsConnectionMiddleware.cs) source rather than the docs.
[^clientcertctx]: Microsoft, ["SslClientAuthenticationOptions.ClientCertificateContext"](https://learn.microsoft.com/en-us/dotnet/api/system.net.security.sslclientauthenticationoptions.clientcertificatecontext) — an `SslStreamCertificateContext` for the client certificate; available from .NET 8.
[^checkhost]: OpenSSL, ["X509_check_host"](https://docs.openssl.org/3.1/man3/X509_check_host/) — "the default is to use the subject DN when no corresponding subject alternative names are present", unless `X509_CHECK_FLAG_NEVER_CHECK_SUBJECT` is set.
[^leclientauth]: Let's Encrypt, ["Ending TLS Client Authentication Certificate Support in 2026"](https://letsencrypt.org/2025/05/14/ending-tls-client-authentication/) — prompted by Chrome root programme rules requiring separate client and server authentication hierarchies by June 2026; the Client Authentication EKU dropped from the default profile on 11 February 2026 and from all issuance after 8 July 2026.
[^downloads]: Microsoft, ["X509ChainPolicy.DisableCertificateDownloads"](https://learn.microsoft.com/en-us/dotnet/api/system.security.cryptography.x509certificates.x509chainpolicy.disablecertificatedownloads) — whether the chain engine may use the AIA extension to locate unknown issuer certificates; the default is `false`, so downloads are allowed.
