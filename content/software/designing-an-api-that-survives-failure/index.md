---
title: "Designing an API That Survives Failure"
date: 2026-08-04T10:00:00+02:00
lastmod: 2026-11-01T15:16:00+02:00
draft: false
tags: ["software", "csharp", "dotnet", "aspnetcore", "architecture", "dotnet-deep-dives", "ai-generated"]
description: "Idempotency, retries, circuit breakers, and eventual consistency — worked through a payment API, plus the ASP.NET Core machinery and testing strategy that hold it up."
---

Everything so far has been about what code does when it works. This one is about what happens when it doesn't, worked through one scenario rather than a list of patterns:

> A payment-processing API. Requests can arrive twice. Payments must never be processed twice. Downstream services fail intermittently.

Between them, those three constraints rule out most naive designs.

## Start with what "arrives twice" means

Duplicate requests are the normal condition of any networked system. The connection drops after the server processed the request but before the response arrived; a load balancer retries; a user double-clicks; a message queue delivers at-least-once, as almost all of them do.

![Two failed requests that look the same to the client. In the first, the request is lost on the way and the server never sees it. In the second, the server processes the payment but the response is lost on the way back. In both cases the client sees only a timeout, so it retries, and in the second case the retry is a duplicate](arrives-twice.svg)

**The client can't tell "the request never arrived" from "the response never came back".** Both are a timeout, so a client that retries on timeout is behaving correctly, and will inevitably send a request you've already processed. You can't prevent duplicates, only make processing one harmless.

## Idempotency

An operation is idempotent if doing it twice has the same effect as doing it once. HTTP defines `PUT`, `DELETE` and the safe methods such as `GET` as idempotent;[^rfc9110] `POST /payments` isn't, which is the whole problem.

The fix is an **idempotency key**: the client generates a unique key per logical operation and sends it with the request, retries reuse it, and the server returns the original result instead of processing again.

```http
POST /payments
Idempotency-Key: 8f14e45f-ea0e-4c9b-a8f3-2b4d7c1e9a01
```

The key must come from the client, because only the client knows two requests represent the same intent. Hashing the request body is a tempting substitute and wrong: two separate £10 payments to the same payee have identical bodies.

The naive server implementation has the same shape as the cache stampede from the [async post]({{< relref "async-concurrency-and-a-thread-safe-cache.md" >}}):

```csharp
if (await _db.IdempotencyKeys.AnyAsync(k => k.Key == key))
    return await GetPreviousResult(key);       // race lives here

var result = await ProcessPayment(request);
await _db.IdempotencyKeys.AddAsync(new(key, result));
await _db.SaveChangesAsync();
```

Check-then-act, with a window between them. Two concurrent retries both see no key and both charge the card.

### The unique constraint is the actual mechanism

Every guard in application code is advisory. The only thing that reliably prevents a duplicate under concurrency is a **unique constraint in the database**, the one check made atomically as part of the write. So invert the flow: insert the key *first*, and let the constraint decide.

```csharp
try
{
    _db.IdempotencyKeys.Add(new IdempotencyKey(key, status: InProgress));
    await _db.SaveChangesAsync();          // unique index on Key
}
catch (DbUpdateException ex) when (IsUniqueViolation(ex))   // your own helper
{
    // Someone else owns this key — return their result, or 409 if still running.
    return await AwaitExistingResult(key);
}

var result = await ProcessPayment(request);
await MarkComplete(key, result);
return result;
```

The winner of the insert race owns the operation; everyone else waits or reads. It's the cache lesson again: **push the atomicity down to the layer that can actually provide it.**

Three details matter in practice. Store the *response* against the key, or a retry can't be answered. Give keys a TTL (24 hours is common), or the table grows forever. Decide what an in-progress key returns: 409 with a `Retry-After` is honest, and better than blocking a thread on another request. And if `ProcessPayment` fails definitely, mark the key failed or delete it, or every retry gets 409 forever; a key stuck in progress is a job for reconciliation.

### Where the transaction goes

Idempotency only works if the outcome and the key's completion commit **together**. If the charge succeeds and the key's completion is never recorded, the retry charges again.

For work inside your own database, that's one transaction and one `SaveChanges`. The hard case is an external call, such as charging a card at a payment provider: there's no shared transaction with them, and you can't make a database write and a remote call atomic. What you can do is make the sequence recoverable:

1. Write intent (`Pending`) and commit. Now the operation is durable and crash-visible.
2. Make the external call, passing **your** idempotency key so the provider deduplicates on their side too.
3. Record the outcome and commit.

![Two orderings of a payment that involves an external provider. Recoverable: write Pending and commit, call the provider with your idempotency key, then record the outcome; a crash after the call leaves a Pending row that a reconciliation job can resolve by asking the provider. Unrecoverable: call the provider first and write afterwards; a crash between the two leaves a charge with no record in your system](recoverable-sequence.svg)

Crash after the call and you're left with a `Pending` row for a card that may have been charged, which a reconciliation job can resolve by querying the provider with your key. The unresolved state is *visible*. Make the call first instead, and a crash before writing leaves a charge nobody in your system knows to look for.

It's the same idea as the outbox pattern, and the standard answer to "how do I update my database and call another service atomically": you don't, you make the gap survivable.

## Downstream services that fail

### Timeouts

Every outbound call needs one. The default `HttpClient` timeout is 100 seconds,[^httpclient-timeout] so under load a dead dependency ties up connections and pending requests for over a minute and a half each. Derive timeouts from the caller's budget (an endpoint that must respond in 2 seconds can't give a downstream call 30), and make them *decrease* down the call chain, or nothing is left for your own work or a retry.

### Retries

Retry only what's **transient and idempotent**. A 503 or a connection reset, yes; a 400 fails identically forever. A timeout on a non-idempotent operation is the dangerous middle case: you don't know whether it succeeded, which is exactly why the downstream call carries an idempotency key.

Retries need **exponential backoff with jitter**. Fixed-interval retries from many clients synchronise into waves that hit the recovering service at once and knock it back over; randomising the delay spreads them out. And retries must be **bounded**: if every layer retries three times, five layers deep is 243 attempts for one request.

```csharp
builder.Services.AddHttpClient<IPaymentGateway, PaymentGateway>(c =>
{
    c.BaseAddress = new Uri("https://payments.example.com");
    // Leave HttpClient.Timeout (100 s) above the handler's total, or it cuts retries short.
})
.AddStandardResilienceHandler(o =>   // Microsoft.Extensions.Http.Resilience
{
    o.TotalRequestTimeout.Timeout = TimeSpan.FromSeconds(8);   // from the caller's budget
    o.AttemptTimeout.Timeout = TimeSpan.FromSeconds(3);
    // Only safe if the gateway honours our idempotency key; otherwise:
    // o.Retry.DisableForUnsafeHttpMethods();
});
```

`AddStandardResilienceHandler` gives you, in order, a rate limiter, a total request timeout, retry with exponential backoff and jitter, a circuit breaker, and a per-attempt timeout, built on Polly.[^resilience] It retries *every* HTTP method by default, `POST` included, which for a payment call is safe only because the provider deduplicates on your key; `DisableForUnsafeHttpMethods()` turns that off where it isn't. Configure the numbers; don't hand-roll the pipeline.

### Circuit breakers

Retries assume the problem is brief. When a service is properly down, retrying adds load to something struggling and burns your own connections waiting for calls that won't succeed.

A circuit breaker tracks the failure rate and, past a threshold, **stops making calls**: it fails immediately for a cooldown, then lets a trial request through to test recovery. The failing service gets breathing room, and your service fails fast instead of every request blocking on a doomed call, which is what stops a downstream outage becoming yours.

The pairing to keep in mind: **retry handles a blip, the breaker handles an outage**, and you need both because you can't tell which you're in from a single failure.

### Queues and eventual consistency

Some work shouldn't be in the request path. If a payment must send a receipt, update analytics and notify fulfilment, doing it all synchronously means the request fails when any of them does, after the card is charged.

Put it on a queue. The request does the minimum that must be atomic (take the payment, record it) and publishes an event; consumers handle the rest, retry independently, and dead-letter what they can't process. The cost is **eventual consistency**: for a while the payment exists and the receipt doesn't. For receipts and analytics that's nearly always acceptable; where the business can't accept it, you need a different design, not a faster queue.

Consumers must be **idempotent**, because queues redeliver. And publishing must be atomic with the database write, which is the outbox pattern again: write the event to an outbox table in the same transaction and let a separate process publish it, or a crash between commit and publish silently loses the event.

### Observability

The baseline for operating any of this:

- **Structured logging** with a correlation ID flowing through every service. Without it you have logs from six services and no way to reconstruct one request.
- **Distributed tracing** — OpenTelemetry, `Activity`, and W3C `traceparent` propagation, which ASP.NET Core and `HttpClient` support natively. This is what answers "which of these nine calls took the 4 seconds."
- **Metrics** — rate, errors, duration per endpoint and per dependency, plus circuit breaker state and queue depth. Queue depth is the leading indicator: it climbs before anything else looks wrong.
- **Health checks** — `/health/live` (am I running?) separate from `/health/ready` (can I serve traffic?). Conflating them gets your pods restarted when a downstream dependency hiccups.

## The ASP.NET Core machinery

### The pipeline

A request passes through middleware in registration order and the response comes back in reverse: a nesting of delegates, not a list. Order is behaviour: exception handling must be outermost to catch everything after it; authentication must precede authorization; CORS must precede anything that short-circuits.[^middleware-order]

**Middleware vs filters** comes down to scope. Middleware sits in the raw HTTP pipeline and runs for every request; filters run inside MVC after routing and model binding, so they know the action and its bound arguments. Cross-cutting infrastructure (correlation IDs, exception handling, compression) is middleware; anything that needs the action or model (validation, action-level authorization, result shaping) is a filter.

### HttpClientFactory

`new HttpClient()` per request is the classic .NET networking bug. Each `HttpClient` owns a connection pool; creating and disposing one per request leaves sockets in `TIME_WAIT`, and under load you exhaust ephemeral ports, with a `SocketException` ("only one usage of each socket address … is normally permitted") minutes into a load test on an otherwise idle machine.

The naive fix, one static `HttpClient` forever, has the opposite problem: connections are never recycled, so they never pick up DNS changes, and after a failover the client keeps talking to the old IP. `IHttpClientFactory` solves both by pooling the underlying `HttpMessageHandler` and rotating it on a lifetime that defaults to two minutes:[^httpclientfactory]

```csharp
builder.Services.AddHttpClient<IPaymentGateway, PaymentGateway>(...);
```

Connections are reused, handlers recycled so DNS is respected, and you get a place to attach resilience and logging. Typed clients make the dependency explicit in the constructor.

### Correlation IDs

Accept an incoming correlation header or generate one, put it in the logging scope so every log line carries it, propagate it on outbound calls, and return it on the response so a user reporting a problem can quote it. It belongs in middleware, before anything that might log:

```csharp
app.Use(async (ctx, next) =>
{
    var id = ctx.Request.Headers["X-Correlation-ID"].FirstOrDefault()
             ?? Activity.Current?.TraceId.ToString()
             ?? Guid.NewGuid().ToString();

    ctx.Response.Headers["X-Correlation-ID"] = id;
    using (app.Logger.BeginScope(new Dictionary<string, object> { ["CorrelationId"] = id }))
        await next();
});
```

With OpenTelemetry you largely get this from the trace ID, and `traceparent` interoperates with everything else where a bespoke header doesn't.

### Rate limiting

Built in since .NET 7, with four algorithms.[^ratelimiting] **Fixed window** is simplest and allows a burst of 2× the limit across a window boundary; **sliding window** smooths that; **token bucket** allows controlled bursts while capping the sustained rate, usually the best fit for an API; **concurrency** limits simultaneous requests, the right tool for protecting a scarce downstream resource.

```csharp
builder.Services.AddRateLimiter(o =>
{
    o.AddTokenBucketLimiter("payments", opt =>
    {
        opt.TokenLimit = 100;
        opt.TokensPerPeriod = 10;
        opt.ReplenishmentPeriod = TimeSpan.FromSeconds(1);
        opt.QueueLimit = 50;
    });
    o.RejectionStatusCode = StatusCodes.Status429TooManyRequests;
});
```

The snippet is one global bucket; it applies to endpoints that call `.RequireRateLimiting("payments")`, once `app.UseRateLimiter()` is in the pipeline. In production, partition by API key or tenant (`AddPolicy` with a `RateLimitPartition`), or one noisy client consumes everyone's budget. Return `Retry-After` (set it in `OnRejected`), because a 429 without it invites an immediate retry. And the limit is per instance: behind a load balancer with ten instances it's ten times what you configured, and a global limit needs shared state.

### Graceful shutdown

On SIGTERM, the host stops accepting new requests and gives in-flight ones a window to finish — 30 seconds by default, configurable via `HostOptions.ShutdownTimeout`.[^shutdown] Requests still running when it expires are killed.

For this to work, `CancellationToken`s must be honoured throughout, `BackgroundService.ExecuteAsync` must observe its stopping token, and (the part usually missed) traffic must stop arriving *before* the app stops accepting it. Kubernetes removes a terminating pod from its endpoints at the same time as it sends SIGTERM, not before, so a short `preStop` sleep is what covers the gap; the [containers post]({{< relref "containers-kubernetes-and-the-linux-underneath/index.md" >}}) walks through it. Without that delay you drop requests during every deployment.

### Streaming a 2GB upload

By default an upload never gets that far: Kestrel rejects request bodies over about 28.6 MB, and multipart bodies are capped at 128 MB.[^uploads] Raise those, and `IFormFile` buffers the whole body before your action runs, in memory up to 64 KB and in a temp file beyond that, so each concurrent 2 GB upload costs 2 GB of disk and a full copy before you touch it.

The fix is to never materialise it: read the multipart stream yourself and copy it through to the destination in chunks.

```csharp
[HttpPost("upload")]
[DisableRequestSizeLimit]
public async Task<IActionResult> Upload(CancellationToken ct)
{
    var boundary = MediaTypeHeaderValue.Parse(Request.ContentType)
        .Boundary.Value!.Trim('"');
    var reader = new MultipartReader(boundary, Request.Body);

    while (await reader.ReadNextSectionAsync(ct) is { } section)
    {
        if (!ContentDispositionHeaderValue.TryParse(
                section.ContentDisposition, out var cd) || !cd.IsFileDisposition())
            continue;

        // Never store under the client's file name (path traversal); keep it as metadata.
        await using var target = await _blobs.OpenWriteAsync(Guid.NewGuid().ToString("N"), ct);
        await section.Body.CopyToAsync(target, ct);   // constant memory
    }

    return Ok();
}
```

Memory use is now the copy buffer, not the file size. `[DisableRequestSizeLimit]` keeps the example short but removes a DoS protection; set a real, large limit (`[RequestSizeLimit]`) instead. The same applies in reverse for downloads: stream to the response body rather than building a byte array. `IAsyncEnumerable<T>` returned from an action streams JSON incrementally, though as the [EF post]({{< relref "linq-ef-core-and-the-sql-underneath.md" >}}) argues, the better question is usually why the result set is that large.

### Controllers, minimal APIs, and where logic lives

Minimal APIs have less ceremony; controllers give you filters, model-binding conventions and a familiar structure for large surfaces. Either is fine, and endpoint filters have closed most of the gap. Prefer `ActionResult<T>` over `IActionResult`: it documents the success type for OpenAPI and keeps the compiler involved.

Business logic belongs in neither. The endpoint's job is HTTP: bind, validate shape, call something, map the result to a status code. Everything else belongs in a service or domain type that has no idea HTTP exists, so you can test it without a web host and call it from a background job or a queue consumer.

**Validation** splits along the same line. Shape validation (required fields, formats, ranges) belongs at the boundary and returns 400. Business-rule validation (sufficient funds, a valid state transition) belongs in the domain, because it needs data the boundary doesn't have; doing it at the boundary is what leads to validators with database dependencies.

## Testing this

### The pyramid, and what it's actually saying

Many fast unit tests, fewer integration tests, very few end-to-end tests. The reasoning is economic (cost and flakiness rise with scope while precision falls), and it's guidance rather than law. For a service that's mostly orchestration and database access, heavy integration coverage is often the honest distribution, because that's where the risk lives.

### What to mock

Mock things you don't own and can't run: third-party APIs, payment gateways, email.

Don't mock what you own, and don't mock the database. A test against a mocked `DbContext` verifies that you called the methods you called; it can't catch a bad query, a constraint violation or a translation failure, which are the actual bugs. Run the real database in Docker with Testcontainers. EF Core's in-memory provider is no substitute: Microsoft says using it for testing is "strongly discouraged", and it doesn't support transactions or share a relational provider's translation rules.[^ef-testing]

Heavy mocking also couples tests to implementation rather than behaviour. Every refactor breaks them although nothing observable changed, which teaches the team that a failing test means "update the test", and at that point the suite has negative value.

Prefer real objects where cheap, fakes (a working in-memory implementation) over mocks where not, and mocks for genuine boundaries.

### Testing the hard things

**Async** — just `await` it; the test framework handles `Task`-returning tests. Never `.Result` in a test. Assert on faults with `Assert.ThrowsAsync`.

**Retry logic** — this is why the retry policy should be injected rather than hardcoded. A fake that fails twice then succeeds proves the retry happens; a fake that always fails proves it gives up. Configure the delays to near-zero in tests, or a backoff test takes thirty seconds.

**Concurrent code** — mostly you can't, not reliably: a passing test proves one interleaving worked. Run many operations concurrently and assert an invariant (the origin was hit exactly once), as the [cache demo]({{< relref "async-concurrency-and-a-thread-safe-cache.md" >}}) does, to catch egregious races, and prefer designs where the race is impossible.

**Idempotency** — send the same request twice, concurrently as well as in sequence, and assert one charge. It should be among the first tests written, because it's the actual requirement.

### 500 tests and production still breaks

"Write more tests" is the wrong answer. Usually one of these is true. The tests are **heavily mocked**, so they verify wiring rather than behaviour — every unit passes and the composition is broken. There's **no integration coverage**, so nothing exercises real SQL, real serialisation, or real HTTP. They test the **happy path only**, while production failures are timeouts, partial failures, and malformed input. They don't cover **concurrency**, because the tests are single-threaded and production isn't. Or the failures aren't code at all — configuration, migrations, capacity, a dependency's behaviour change — in which case no unit test was ever going to catch them.

Take the last ten production incidents and ask what test would have caught each. The answer is rarely "another unit test", and the pattern tells you where the suite is thin.

## Trade-offs, and when not to

Patterns are tools with costs, and knowing when to skip one is the more valuable half.

**Clean Architecture** buys isolation of domain logic from infrastructure, at the cost of indirection and mapping between layers. Worth it for a large domain with real business rules and a long life. Overhead for a CRUD service, where it produces four projects and three DTOs per entity to move a row from a table to JSON.

**Repository over EF Core** is the one to be most sceptical about. `DbContext` is already a unit of work and `DbSet<T>` is already a repository. Wrapping them typically produces a class that either exposes `IQueryable` (leaking EF and achieving nothing) or dozens of `GetByXAndY` methods that reimplement querying badly. The usual justification is swapping the ORM, which almost never happens. It does earn its place when you need a genuine domain-level abstraction (`IOrderRepository` speaking in aggregates, not tables) or a seam for a data source EF doesn't cover.

**CQRS** makes sense when reads and writes have genuinely different shapes or scaling needs, such as complex reporting reads against a normalised transactional model. It's overkill when they're the same shape, and the full version with separate databases and projections adds eventual consistency between your own reads and writes.

**DDD** pays off where there's real domain complexity and a business expert to talk to: aggregates, invariants and a ubiquitous language are how you tame rules that genuinely are complicated. It's ceremony on a system whose logic is "save this and email someone".

**Microservices vs modular monolith.** Microservices solve organisational scaling (independent deploys by independent teams) at the cost of turning method calls into network calls, with partial failure, distributed transactions, eventual consistency and a real operational burden. A modular monolith gets most of the boundary discipline without that, and is the right default for most teams. Split when a module has a genuinely different scaling profile or team coordination is the actual bottleneck, not because the diagram looks better.

All five trade **simplicity for flexibility**, and are worth it exactly when you need that flexibility. Adopted speculatively, they pay the cost now for a benefit that may never arrive.

## Where this leaves the payment API

- An **idempotency key** from the client, enforced by a **unique constraint**, because application-level checks lose races.
- Intent written and committed **before** the external call, so a crash leaves a recoverable state rather than an invisible one.
- The same key passed **downstream**, so the provider deduplicates too.
- **Timeouts** on every call, **bounded retries with jittered backoff** for blips, a **circuit breaker** for outages.
- Non-essential work on a **queue** with idempotent consumers, published via an **outbox** so it can't be lost.
- **Correlation IDs and tracing** throughout, because you will need to reconstruct what happened.
- Tests that assert the **actual requirement** — the same request twice produces one charge.

The question worth asking of any design: **what happens when this fails halfway through?** Most of the patterns above are answers to it, and a design with no answer isn't finished.

That's the end of the series. Back to the [overview]({{< relref "dotnet-deep-dives.md" >}}).

[^rfc9110]: IETF, [RFC 9110, "HTTP Semantics", §9.2.2 "Idempotent Methods"](https://www.rfc-editor.org/rfc/rfc9110#section-9.2.2) — "PUT, DELETE, and safe request methods are idempotent".
[^httpclient-timeout]: Microsoft, ["HttpClient.Timeout Property"](https://learn.microsoft.com/en-us/dotnet/api/system.net.http.httpclient.timeout) — "The default value is 100,000 milliseconds (100 seconds)."
[^resilience]: Microsoft, ["Build resilient HTTP apps"](https://learn.microsoft.com/en-us/dotnet/core/resilience/http-resilience) — the standard resilience handler's five strategies, their order and defaults, that it retries all HTTP methods by default, and `DisableForUnsafeHttpMethods`.
[^httpclientfactory]: Microsoft, ["IHttpClientFactory with .NET"](https://learn.microsoft.com/en-us/dotnet/core/extensions/httpclient-factory) — handler pooling, the default two-minute handler lifetime, and the socket-exhaustion and stale-DNS problems it addresses.
[^ratelimiting]: Microsoft, ["Rate limiting middleware in ASP.NET Core"](https://learn.microsoft.com/en-us/aspnet/core/performance/rate-limit) — the fixed window, sliding window, token bucket and concurrency limiters.
[^shutdown]: Microsoft, ["HostOptions.ShutdownTimeout Property"](https://learn.microsoft.com/en-us/dotnet/api/microsoft.extensions.hosting.hostoptions.shutdowntimeout) — the default shutdown timeout.
[^uploads]: Microsoft, ["Upload files in ASP.NET Core"](https://learn.microsoft.com/en-us/aspnet/core/mvc/models/file-uploads) — buffered uploads held in memory up to 64 KB and in a temp file beyond; Kestrel's default 30,000,000-byte request body limit and the 128 MB `MultipartBodyLengthLimit`; streaming with `MultipartReader`.
[^middleware-order]: Microsoft, ["ASP.NET Core Middleware"](https://learn.microsoft.com/en-us/aspnet/core/fundamentals/middleware/) — the documented middleware ordering and why it matters.
[^ef-testing]: Microsoft, ["Testing EF Core Applications"](https://learn.microsoft.com/en-us/ef/core/testing/) and ["Testing without your production database system"](https://learn.microsoft.com/en-us/ef/core/testing/testing-without-the-database) — using the in-memory provider for testing "is strongly discouraged", and it doesn't support transactions.
