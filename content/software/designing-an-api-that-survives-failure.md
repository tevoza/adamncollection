---
title: "Designing an API That Survives Failure"
date: 2026-08-04T10:00:00+02:00
draft: false
tags: ["software", "csharp", "dotnet", "aspnetcore", "architecture", "dotnet-deep-dives", "ai-generated"]
description: "Idempotency, retries, circuit breakers, and eventual consistency — worked through a payment API, plus the ASP.NET Core machinery and testing strategy that hold it up."
---

Everything so far has been about what code does when it works. This one is about what happens when it doesn't — which is the part that determines whether a system is any good.

The scenario, because it's more useful than a list of patterns:

> A payment-processing API. Requests can arrive twice. Payments must never be processed twice. Downstream services fail intermittently.

Three constraints that between them rule out most naive designs.

## Start with what "arrives twice" means

Duplicate requests aren't an edge case, they're the normal condition of any networked system. The client's connection drops after the server processed the request but before the response arrived. A load balancer retries. A user double-clicks. A mobile client retries on a flaky connection. A message queue delivers at-least-once, which is what almost all of them guarantee.

The important consequence: **the client cannot tell the difference between "the request never arrived" and "the response never came back."** From its side both are a timeout. So a client that retries on timeout is behaving correctly, and it will inevitably send you a request you've already processed.

You can't prevent duplicates. You can only make processing one harmless.

## Idempotency

An operation is idempotent if doing it twice has the same effect as doing it once. `GET`, `PUT`, and `DELETE` are idempotent by definition. `POST /payments` is not — that's the entire problem.

The fix is an **idempotency key**: the client generates a unique key per logical operation and sends it with the request. Retries reuse the same key. The server recognises the key and returns the original result instead of processing again.

```http
POST /payments
Idempotency-Key: 8f14e45f-ea0e-4c9b-a8f3-2b4d7c1e9a01
```

The key must come from the client, because only the client knows that two requests represent the same intent. Hashing the request body is a tempting substitute and it's wrong — two genuinely separate £10 payments to the same payee produce identical bodies.

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

Every guard in application code is advisory. The only thing that reliably prevents a duplicate under concurrency is a **unique constraint in the database**, because that's the one check the database makes atomically as part of the write.

So invert the flow: insert the key *first*, and let the constraint decide.

```csharp
try
{
    _db.IdempotencyKeys.Add(new IdempotencyKey(key, status: InProgress));
    await _db.SaveChangesAsync();          // unique index on Key
}
catch (DbUpdateException ex) when (ex.IsUniqueViolation())
{
    // Someone else owns this key — return their result, or 409 if still running.
    return await AwaitExistingResult(key);
}

var result = await ProcessPayment(request);
await MarkComplete(key, result);
return result;
```

The winner of the insert race owns the operation; everyone else waits or reads. This is the same lesson as the cache: **push the atomicity down to the layer that can actually provide it.**

Three details that matter in practice. Store the *response* against the key, not just the fact of it, or a retry can't be answered. Give keys a TTL — 24 hours is typical — or the table grows forever. And decide what an in-progress key returns: 409 with a `Retry-After` is honest, and better than blocking a request thread waiting for another one to finish.

### Where the transaction goes

Idempotency only works if recording the key and doing the work commit **together**. If the charge succeeds and the key write fails, the retry charges again — which is the exact failure you built this to prevent.

For work inside your own database, that's one transaction and one `SaveChanges`. The hard case is when the work is an external call — charging a card at a payment provider — because there's no shared transaction with them and there can't be.

You cannot make a database write and a remote call atomic. What you can do is make the sequence recoverable:

1. Write intent (`Pending`) and commit. Now the operation is durable and crash-visible.
2. Make the external call, passing **your** idempotency key so the provider deduplicates on their side too.
3. Record the outcome and commit.

Crash between 1 and 3 and you're left with a `Pending` row — recoverable, because a reconciliation job can query the provider by your key and find out what actually happened. The unresolved state is *visible*, which is the property that matters. Compare that with making the call first: a crash before writing leaves you with a charge and no record of it, and nothing in your system knows to go looking.

This is the outbox pattern's core idea, and it's the standard answer to "how do I update my database and call another service atomically" — you don't, you make the gap survivable.

## Downstream services that fail

### Timeouts

Every outbound call needs one. The default `HttpClient` timeout is 100 seconds, which under load means threads and connections held for a minute and a half against a service that's already gone. Timeouts should be derived from the caller's own budget: if your endpoint must respond in 2 seconds, a downstream call cannot be allowed 30.

Timeout budgets should also *decrease* down the call chain. A request with a 5-second budget calling a service that allows itself 5 seconds leaves nothing for your own work or a retry.

### Retries

Retry only what's **transient and idempotent**. A 503 or a connection reset, yes. A 400 will fail identically forever, and retrying it just multiplies load. A timeout on a non-idempotent operation is the dangerous middle case: you don't know whether it succeeded, which is exactly why the downstream call carries an idempotency key.

Retries need **exponential backoff with jitter**. Fixed-interval retries from many clients synchronise into waves that hit the recovering service simultaneously and knock it back over. Jitter — randomising the delay — spreads them out. This matters more than it sounds; synchronised retry storms are a common way a brief blip becomes an outage.

And retries must be **bounded**. Unbounded retry against a service that's genuinely down converts one failure into sustained load, and if every layer retries three times, five layers deep is 243 attempts for one request.

```csharp
builder.Services.AddHttpClient<IPaymentGateway, PaymentGateway>(c =>
{
    c.BaseAddress = new Uri("https://payments.example.com");
    c.Timeout = TimeSpan.FromSeconds(10);
})
.AddStandardResilienceHandler();     // Microsoft.Extensions.Http.Resilience
```

`AddStandardResilienceHandler` gives you rate limiting, total timeout, retry with backoff and jitter, circuit breaker, and per-attempt timeout in one line, built on Polly. Configure the numbers; don't hand-roll the pipeline.

### Circuit breakers

Retries assume the problem is brief. When a service is properly down, retrying is actively harmful: you're adding load to something struggling, and burning your own threads and connections waiting for calls that won't succeed.

A circuit breaker tracks the failure rate and, past a threshold, **stops making calls** — failing immediately for a cooldown, then letting a trial request through to test recovery. Two benefits: the failing service gets breathing room, and your service fails fast instead of having every request block on a doomed call. That second one is what stops a downstream outage becoming your outage, because thread and connection exhaustion is how failures propagate upward.

The pairing to keep in mind: **retry handles a blip, the breaker handles an outage**, and you need both because you can't tell which you're in from a single failure.

### Queues and eventual consistency

Some work shouldn't be in the request path. If a payment must send a receipt, update analytics, and notify a fulfilment service, doing all that synchronously means the request fails when any of them does — and the customer's card is already charged.

Put it on a queue. The request does the minimum that must be atomic — take the payment, record it — and publishes an event. Consumers handle the rest, retry independently, and dead-letter what they can't process.

The cost is **eventual consistency**: for some window, the payment exists and the receipt doesn't. That has to be acceptable to the business, and if it isn't, you need a different design rather than a faster queue. Where it is acceptable — and for receipts and analytics it nearly always is — it's a large gain in resilience.

Two things this brings with it. Consumers must be **idempotent**, because queues deliver at-least-once and will redeliver on a consumer crash. And publishing must be atomic with the database write, which is the outbox pattern again: write the event to an outbox table in the same transaction, and a separate process publishes it. Otherwise a crash between commit and publish loses the event silently.

### Observability

A distributed system you can't observe is one you can't operate. The baseline:

- **Structured logging** with a correlation ID flowing through every service. Without it you have logs from six services and no way to reconstruct one request.
- **Distributed tracing** — OpenTelemetry, `Activity`, and W3C `traceparent` propagation, which ASP.NET Core and `HttpClient` support natively. This is what answers "which of these nine calls took the 4 seconds."
- **Metrics** — rate, errors, duration per endpoint and per dependency, plus circuit breaker state and queue depth. Queue depth in particular is the leading indicator: it climbs before anything else looks wrong.
- **Health checks** — `/health/live` (am I running?) separate from `/health/ready` (can I serve traffic?). Conflating them gets your pods restarted when a downstream dependency hiccups.

## The ASP.NET Core machinery

### The pipeline

A request passes through middleware in registration order and responses come back in reverse — a nesting of delegates, not a list. Order is behaviour: exception handling must be outermost to catch everything after it; authentication must precede authorization; CORS must precede anything that short-circuits.

**Middleware vs filters** comes down to scope. Middleware sits in the raw HTTP pipeline, runs for every request including static files, and knows nothing about MVC. Filters run inside MVC after routing and model binding, so they know the controller, the action, and the bound arguments. Cross-cutting infrastructure — correlation IDs, exception handling, compression — is middleware. Anything needing to know which action is executing or what the model is — validation, action-level authorization, result shaping — is a filter.

### HttpClientFactory

`new HttpClient()` per request is the classic .NET networking bug, and it fails in both directions.

Each `HttpClient` owns a connection pool. Creating one per request and disposing it leaves sockets in `TIME_WAIT` for a couple of minutes, and under load you exhaust ephemeral ports — the symptom is `SocketException: Only one usage of each socket address is normally permitted`, appearing minutes into a load test on a machine that looks otherwise idle.

The naive fix — one static `HttpClient` forever — leaks the opposite problem: the connection is never recycled, so it never picks up DNS changes. Point the hostname at a new IP during a failover and the client keeps talking to the old one indefinitely.

`IHttpClientFactory` solves both by pooling the underlying `HttpMessageHandler` with a rotation lifetime (two minutes by default):

```csharp
builder.Services.AddHttpClient<IPaymentGateway, PaymentGateway>(...);
```

Connections are reused, handlers are recycled so DNS is respected, and you get a natural place to attach resilience and logging. Use typed clients; they make the dependency explicit in the constructor.

### Correlation IDs

Accept an incoming correlation header if present, generate one if not, put it in the logging scope so every log line in the request carries it, propagate it on outbound calls, and return it on the response so a user reporting a problem can quote it.

Middleware is the right place — it must happen before anything that might log:

```csharp
app.Use(async (ctx, next) =>
{
    var id = ctx.Request.Headers["X-Correlation-ID"].FirstOrDefault()
             ?? Activity.Current?.TraceId.ToString()
             ?? Guid.NewGuid().ToString();

    ctx.Response.Headers["X-Correlation-ID"] = id;
    using (logger.BeginScope(new Dictionary<string, object> { ["CorrelationId"] = id }))
        await next();
});
```

With OpenTelemetry you largely get this via the trace ID, and preferring `traceparent` over a bespoke header means it interoperates with everything else.

### Rate limiting

Built in since .NET 7, with four algorithms. **Fixed window** is simplest and allows a burst of 2× the limit across a window boundary. **Sliding window** smooths that. **Token bucket** allows controlled bursts while capping the sustained rate — usually the best fit for an API. **Concurrency** limits simultaneous requests rather than rate, which is the right tool for protecting a scarce downstream resource.

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

Partition by API key or tenant rather than globally, or one noisy client consumes everyone's budget. Always return `Retry-After` — a 429 without it invites an immediate retry, which is precisely what you were trying to stop.

Note that this is per-instance. Behind a load balancer with ten instances the effective limit is ten times what you configured, and a genuinely global limit needs shared state.

### Graceful shutdown

On SIGTERM, the host stops accepting new requests and gives in-flight ones a window to finish — 30 seconds by default, configurable via `HostOptions.ShutdownTimeout`. Requests still running when it expires are killed.

For this to work, `CancellationToken`s must be honoured throughout, `BackgroundService.ExecuteAsync` must observe its stopping token, and — the part usually missed — the readiness probe must start failing *before* shutdown begins, so the load balancer stops sending traffic while the pod is still draining. Without that gap you drop requests during every deployment.

### Streaming a 2GB upload

The default model buffers: `IFormFile` reads the whole body into memory or a temp file before your action runs. At 2GB that's an immediate OOM, or several if requests overlap.

The fix is to never materialise it. Disable form value binding, read the multipart stream yourself, and copy through to the destination in chunks:

```csharp
[HttpPost("upload")]
[DisableRequestSizeLimit]
[RequestFormLimits(MultipartBodyLengthLimit = long.MaxValue)]
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

        await using var target = await _blobs.OpenWriteAsync(cd.FileName.Value!, ct);
        await section.Body.CopyToAsync(target, ct);   // constant memory
    }

    return Ok();
}
```

Memory use is now the copy buffer, not the file size. The same applies in reverse for large downloads: stream from the source to the response body rather than building a byte array. `IAsyncEnumerable<T>` returned from an action streams JSON incrementally, which is the right answer for a large result set — though as the [EF post]({{< relref "linq-ef-core-and-the-sql-underneath.md" >}}) argues, the better question is usually why the result set is that large.

Note also that `[DisableRequestSizeLimit]` removes a DoS protection. Set a real, large limit rather than none.

### Controllers, minimal APIs, and where logic lives

Minimal APIs have less ceremony and marginally less overhead; controllers give you filters, model binding conventions, and a familiar structure for large surfaces. Either is fine. Endpoint filters have closed most of the functional gap.

`ActionResult<T>` over `IActionResult` where you can — it documents the success type for OpenAPI and keeps the compiler involved.

Business logic does not belong in either. The controller's job is HTTP: bind, validate shape, call something, map the result to a status code. Everything else belongs in a service or domain type that has no idea HTTP exists — which is what lets you test it without a web host, and what stops the logic being unreachable from a background job or a queue consumer.

**Validation** splits along the same line. Shape validation — required fields, formats, ranges — belongs at the boundary, via data annotations or FluentValidation, and returns 400. Business-rule validation — sufficient funds, valid state transition — belongs in the domain, because it needs data the boundary doesn't have and it's a rule, not a parse. Trying to do the second at the boundary is what leads to validators with database dependencies.

## Testing this

Testing is part of the design, not a phase after it. A system built as described is testable; one that isn't, isn't.

### The pyramid, and what it's actually saying

Many fast unit tests, fewer integration tests, very few end-to-end tests. The reasoning is economic — cost and flakiness rise with scope while precision falls — and it's guidance rather than law. For a service that is mostly orchestration and database access, an inverted-looking distribution with heavy integration coverage is often the honest one, because that's where the risk lives.

### What to mock

Mock things you don't own and can't run: third-party APIs, payment gateways, email.

Don't mock what you own, and don't mock the database. A test against a mocked `DbContext` verifies that you called the methods you called. It cannot catch a bad query, a missing index, a constraint violation, or a translation failure — the actual bugs. Use Testcontainers to run the real database in Docker; it's fast enough now that the tradeoff has genuinely changed. The in-memory provider is worse than nothing for query correctness, because it doesn't enforce constraints or share the real provider's translation rules, so it passes tests that production fails.

Heavy mocking hurts in a specific way: tests become **coupled to implementation** rather than behaviour. Every refactor breaks them even though nothing observable changed, which teaches the team that failing tests mean "update the test." At that point the suite has negative value.

Prefer real objects where cheap, fakes (a working in-memory implementation) over mocks where not, and mocks for genuine boundaries.

### Testing the hard things

**Async** — just `await` it; the test framework handles `Task`-returning tests. Never `.Result` in a test. Assert on faults with `Assert.ThrowsAsync`.

**Retry logic** — this is why the retry policy should be injected rather than hardcoded. A fake that fails twice then succeeds proves the retry happens; a fake that always fails proves it gives up. Configure the delays to near-zero in tests, or a backoff test takes thirty seconds.

**Concurrent code** — the honest answer is that you mostly can't, not reliably. A passing test proves one interleaving worked. What you can do: run many operations concurrently and assert an invariant (the origin was hit exactly once, the total is correct), which is exactly what the [cache demo]({{< relref "async-concurrency-and-a-thread-safe-cache.md" >}}) does. That catches egregious races. For the rest, prefer designs where the race is impossible over tests that hope to catch it.

**Idempotency** — send the same request twice and assert one charge. This should be one of the first tests written for the payment API, because it's the actual requirement.

### 500 tests and production still breaks

Worth taking seriously, because "write more tests" is the wrong answer.

Usually one of these is true. The tests are **heavily mocked**, so they verify wiring rather than behaviour — every unit passes and the composition is broken. There's **no integration coverage**, so nothing exercises real SQL, real serialisation, or real HTTP. They test the **happy path only**, while production failures are timeouts, partial failures, and malformed input. They don't cover **concurrency**, because the tests are single-threaded and production isn't. Or the failures aren't code at all — configuration, migrations, capacity, a dependency's behaviour change — in which case no unit test was ever going to catch them.

The diagnostic is to take the last ten production incidents and ask, for each, what test would have caught it. The answer is rarely "another unit test," and the pattern in those ten tells you exactly where the suite is thin.

## Trade-offs, and when not to

Patterns are tools with costs. Knowing when to skip one is the more valuable half.

**Clean Architecture** buys isolation of domain logic from infrastructure, at the cost of indirection and mapping between layers. Worth it for a large domain with real business rules and a long life. Overhead for a CRUD service, where it produces four projects and three DTOs per entity to move a row from a table to JSON.

**Repository over EF Core** is the one to be most sceptical about. `DbContext` is already a unit of work and `DbSet<T>` is already a repository. Wrapping them typically produces a class that either exposes `IQueryable` — leaking EF and achieving nothing — or exposes dozens of `GetByXAndY` methods that reimplement querying badly. The usual justification is swapping the ORM, which almost never happens. It does earn its place when you need a genuine domain-level abstraction (`IOrderRepository` speaking in aggregates, not tables) or a seam for a data source EF doesn't cover.

**CQRS** makes sense when reads and writes have genuinely different shapes or scaling needs — complex reporting reads against a normalised transactional model. Separate read models let each be optimised independently. It's overkill when reads and writes are the same shape, and the full version with separate databases and projections adds eventual consistency between your own reads and writes, which is a large price.

**DDD** pays off where there's real domain complexity and a business expert to talk to — the aggregates, invariants, and ubiquitous language are how you tame rules that genuinely are complicated. It's ceremony on a system whose logic is "save this and email someone."

**Microservices vs modular monolith.** Microservices solve organisational scaling — independent deploys by independent teams — at the cost of turning method calls into network calls, and with them partial failure, distributed transactions, eventual consistency, and an operational burden that needs real investment. A modular monolith gets you most of the boundary discipline with none of that, and it's the right default for most teams. Split when a specific module has a genuinely different scaling profile, or when team coordination is the actual bottleneck. Splitting because the architecture diagram looks better is how a team acquires distributed systems problems without the traffic that justifies them.

The pattern in all five: they trade **simplicity for flexibility**, and they're worth it exactly when you need that specific flexibility. Adopting them speculatively pays the cost now for a benefit that may never arrive.

## Where this leaves the payment API

Pulling the thread back through:

- An **idempotency key** from the client, enforced by a **unique constraint**, because application-level checks lose races.
- Intent written and committed **before** the external call, so a crash leaves a recoverable state rather than an invisible one.
- The same key passed **downstream**, so the provider deduplicates too.
- **Timeouts** on every call, **bounded retries with jittered backoff** for blips, a **circuit breaker** for outages.
- Non-essential work on a **queue** with idempotent consumers, published via an **outbox** so it can't be lost.
- **Correlation IDs and tracing** throughout, because you will need to reconstruct what happened.
- Tests that assert the **actual requirement** — the same request twice produces one charge.

The unifying question, and the one worth asking of any design: **what happens when this fails halfway through?** Most of the patterns above are answers to it, and a design that has no answer isn't finished.

That's the end of the series. Back to the [overview]({{< relref "dotnet-deep-dives.md" >}}).
