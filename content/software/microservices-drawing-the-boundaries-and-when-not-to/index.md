---
title: "Microservices: Drawing the Boundaries, and When Not To"
date: 2026-11-01T12:00:00+02:00
draft: true
tags: ["software", "architecture", "microservices", "ddd", "distributed-systems", "dotnet", "ai-generated"]
description: "The smells of a badly split system — a distributed monolith, a shared database, five-hop call chains, transactions across services — what fixes each, and why most systems should start as a modular monolith."
---

Microservices are usually sold on what they make possible: teams that deploy independently, parts that scale independently, failures that stay contained. Each of those depends on where the boundaries are. Draw them badly and you keep every cost of a distributed system — the network, partial failure, eventual consistency — and none of the independence.

This post works like the [code smells post]({{< relref "code-smells-and-the-patterns-that-fix-them/index.md" >}}): each section shows a smell in a payments platform, what it makes expensive, and the fix. Part 1 is smells in how the system is split. Part 2 is the larger question of whether to split it at all.

## The vocabulary, briefly

Lewis and Fowler's 2014 definition: "an approach to developing a single application as a suite of small services," each running in its own process, "organized around business capability," and preferring to "manage its own database."[^lewisfowler] Two older ideas decide where the lines go:

- **Bounded contexts** (from domain-driven design): the region where one model and its language hold. Microsoft's .NET architecture guide puts the relationship precisely: a bounded context "could be composed of several physical services, but not vice versa."[^msboundaries]
- **Conway's law** (1968): an organisation that designs a system produces a design whose structure copies the organisation's communication structure.[^conway] 
The alternative this post keeps returning to is the **modular monolith**: one deployable, divided internally along the same boundaries.

## Part 1: smells in the split

The running example is a payments platform: merchants are onboarded, customers pay, payments are screened for fraud, posted to a ledger, and receipts are sent.

### One feature, four services, one release train

**The smell.** The first split followed the database tables:

```text
CustomerService     owns customers, addresses
PaymentService      owns payments
AccountService      owns merchant accounts, balances
TransactionService  owns ledger rows
```

Adding "refund to the original card" needs a new field in `PaymentService`, a balance rule in `AccountService`, a reversal type in `TransactionService` and a lookup in `CustomerService`. Four pull requests, four teams, and they must deploy in a specific order on the same night.

**Why it hurts.** The services are separate processes but not independent: every business change cuts across all of them. This is a **distributed monolith** — the coupling of a monolith with the failure modes of a network. Entity services make it almost inevitable, because a business capability (refunds) uses many entities, and a split by entity scatters each capability across every service.

**The fix.** Split by business capability, so most changes land in one service:

![Before: four entity services, Customer, Payment, Account and Transaction, with the refund feature drawn as one red line touching all four. After: services by capability, including Onboarding, Payments, Risk, Ledger and Notifications; the refund feature touches only Payments, which publishes an event that Ledger consumes](boundaries.svg)

```text
Merchant Onboarding   KYC, merchant accounts, settlement details
Payments              accepting, capturing and refunding payments
Risk                  fraud screening, limits
Wallets               stored customer balances
Ledger                double-entry postings, balances, statements
Notifications         receipts, merchant webhooks
```

Refunds are now a Payments change. Ledger learns about them from a `PaymentRefunded` event it already consumes for ordinary payments.

**What it's called.** Boundaries along **bounded contexts**. The test of a good boundary is in Microsoft's guide: "few strong relationships between domain models," and typical operations that don't "merge information from multiple domain models."[^msboundaries] The same customer can be a `Merchant` in Onboarding, a `Payer` in Risk and an `AccountHolder` in Ledger, each holding only the fields that context needs. Trying to share one `Customer` model across all of them recreates the coupling.

**When not to.** If you can't yet say where the capability boundaries are, you're not ready to make them network boundaries. That's Part 2.

### Two services, one database

**The smell.** Ledger's nightly statement job reads Payments' tables directly, because it's faster than calling an API:

```sql
SELECT p.Id, p.Amount, p.Currency, p.CapturedAt
FROM payments.dbo.Payments p
WHERE p.MerchantId = @merchantId AND p.Status = 'Captured';
```

The Payments team renames `CapturedAt` to `SettledAt` in a routine migration. Statements fail that night, and nobody on the Payments team knew Ledger existed as a consumer.

**Why it hurts.** A shared table is an API nobody designed, versioned or tested. Every column is part of the contract, so no service can change its own schema without coordinating with everyone who reads it — the release train again, through the database.

**The fix.** Each service owns its data and exposes it only through its API or its events — microservices.io's **database per service**: "Keep each microservice's persistent data private to that service and accessible only via its API."[^dbperservice] Ledger subscribes to `PaymentCaptured` and stores what *it* needs in its own tables, shaped for statements. The Payments schema becomes an implementation detail again.

This is the onion architecture's dependency rule from the code smells post, applied between services: depend on a published contract, never on someone else's storage.

**What it costs.** The pattern's own drawbacks: "queries that join data that is now in multiple databases" and "business transactions that span multiple services" both get harder.[^dbperservice] The next two sections are those costs. Sharing a database *server* is fine — the pattern allows private tables or a schema per service[^dbperservice] — as long as no service reads another's.

### Five hops to take a payment

**The smell.** Accepting a payment calls five dependencies synchronously — four internal services and the card provider — before it can answer:

```csharp
public async Task<PaymentResult> CreateAsync(CreatePayment cmd, CancellationToken ct)
{
    var merchant = await onboarding.GetMerchantAsync(cmd.MerchantId, ct);      // hop 1
    var verdict  = await risk.ScreenAsync(cmd, merchant.RiskTier, ct);         // hop 2
    if (verdict.Declined) return PaymentResult.Declined(verdict);
    var capture  = await provider.CaptureAsync(cmd, ct);                       // hop 3
    await ledger.PostAsync(capture, ct);                                       // hop 4
    await notifications.SendReceiptAsync(capture, ct);                         // hop 5
    return PaymentResult.From(capture);
}
```

**Why it hurts.** Availability multiplies along a synchronous chain. If each of five services is up 99.9% of the time, the chain is up about 0.999⁵ ≈ 99.5% — from roughly 43 minutes of downtime a month to over 3.5 hours. Latency adds up, and the slowest hop sets the tail. The receipt service being down now fails a payment that was already captured.

**The fix.** Ask which calls the *response* actually needs:

- **Risk screening and the provider capture** are what the response reports. Keep them synchronous, with timeouts, and give Risk a fallback decision (the [resilience post]({{< relref "designing-an-api-that-survives-failure.md" >}}) covers timeouts and circuit breakers).
- **The merchant's risk tier** changes rarely. Keep a local copy in Payments, updated from Onboarding's `MerchantUpdated` events, instead of calling for it on every payment. Decide how stale is acceptable: a tier change can lag by seconds, but a suspension may need to be pushed with priority or re-checked by Risk at screening time.
- **Ledger posting and the receipt** happen *because* a payment was captured. Publish `PaymentCaptured` through an [outbox]({{< relref "designing-an-api-that-survives-failure.md#queues-and-eventual-consistency" >}}) — written in the same transaction as the payment — and let them react asynchronously.

![Before: Payments calling Onboarding, Risk, the card provider, Ledger and Notifications in turn, with the combined availability 0.999 to the fifth power, about 99.5 percent. After: Payments calls only Risk and the provider synchronously, reads merchant data from a local copy, and publishes PaymentCaptured through an outbox; Ledger and Notifications consume it asynchronously](call-chain.svg)

The response path drops to two dependencies, and an outage in Ledger or Notifications delays postings and receipts instead of failing payments.

**What it costs.** Ledger and the merchant's dashboard are now *eventually* consistent with Payments. That's usually acceptable — card payments already separate authorisation from settlement — but it has to be a decision, and the UI has to tolerate a payment that's captured but not yet on the statement.

### A transaction across three services

**The smell.** A wallet top-up charges the customer's card in Payments, credits their balance in Wallets and posts to Ledger. The first version calls them in sequence and hopes:

```csharp
await payments.CaptureAsync(topUp, ct);
await wallets.CreditAsync(topUp.WalletId, topUp.Amount, ct);   // throws: wallet frozen
await ledger.PostAsync(topUp, ct);
```

The card is charged; the wallet isn't credited. Someone adds a `try/catch` that refunds the card, which itself sometimes fails, and support reconciles the rest by hand.

**Why it hurts.** There's no transaction spanning three databases. Two-phase commit could in principle, but it holds locks across services while waiting for the slowest participant and needs every database and broker to support it — microservices.io's saga page states flatly that "2PC is not an option."[^saga] Ad-hoc compensation in a `catch` block isn't durable: if the process dies mid-way, nothing remembers what was done.

**The fix.** A **saga**: "a reliably coordinated sequence of local transactions," where a failure is undone by "compensating transactions."[^saga] Each step is a local transaction in one service; the saga's progress is persisted, so it resumes after a crash.

```csharp
public sealed class TopUpSaga
{
    public TopUpState State { get; private set; } = TopUpState.New;

    public SagaCommand Start() => Step(TopUpState.Authorising, new AuthoriseCard());

    public SagaCommand Next(SagaEvent evt) => (State, evt) switch
    {
        (TopUpState.Authorising, CardAuthorised)       => Step(TopUpState.Crediting, new CreditWallet()),
        (TopUpState.Authorising, CardDeclined)         => Step(TopUpState.Failed,    SagaCommand.None),
        (TopUpState.Crediting,   WalletCredited)       => Step(TopUpState.Capturing, new CaptureCard()),
        (TopUpState.Crediting,   WalletCreditRejected) => Step(TopUpState.Voiding,   new VoidAuthorisation()),   // compensate
        (TopUpState.Capturing,   CardCaptured)         => Step(TopUpState.Posting,   new PostToLedger()),
        (TopUpState.Posting,     LedgerPosted)         => Step(TopUpState.Completed, SagaCommand.None),
        (TopUpState.Voiding,     AuthorisationVoided)  => Step(TopUpState.Failed,    SagaCommand.None),
        _ => throw new InvalidSagaTransitionException(State, evt)
    };

    private SagaCommand Step(TopUpState next, SagaCommand command) { State = next; return command; }
}
```

The order is deliberate. The card is only *authorised* before the risky step; it's captured once the wallet is credited. If the credit is rejected, the compensation is voiding the authorisation: the hold is released, rather than the card being charged and then refunded.

![A top-up saga. The happy path runs Authorise card in Payments, Credit wallet in Wallets, Capture card in Payments, Post to ledger in Ledger, then Completed. When Credit wallet is rejected, a compensating step, Void authorisation in Payments, runs and the saga ends as Failed. A note says that from the wallet credit on, steps are retried, never compensated. Each box is a local transaction in one service; the orchestrator persists the saga's state between steps](saga.svg)

That's **orchestration**: one component, here `TopUpSaga`, tells each participant what to do. The alternative is **choreography**, where "each local transaction publishes domain events that trigger local transactions in other services."[^saga] Choreography has no central coordinator, which suits two or three steps; orchestration keeps the whole flow readable in one place, which matters once there are compensations.

**What it costs:**

- **No isolation.** Between steps, other code can see the wallet credited before the card is captured. Design for those states: a `Pending` top-up the UI can show, or credited funds held until capture.
- **Compensations are business actions, not rollbacks.** Here it's a void; with a different order it would be a refund the customer sees on their statement.
- **Some steps can't be compensated.** From the wallet credit on, capture and posting are retried, not reversed. A capture only fails for good if the authorisation lapses, and that goes to a person.
- **Everything must be idempotent**, because messages are redelivered — the participants (see the [resilience post]({{< relref "designing-an-api-that-survives-failure.md" >}})) and the orchestrator itself, which dedupes events by message ID before calling `Next`. Saving the new state and sending the command must also happen together, which is the outbox again.

The idea predates microservices: Garcia-Molina and Salem described sagas for long-lived database transactions in 1987.[^sagapaper]

**When not to.** A saga is the price of putting steps that must succeed together into different services. If they almost always change together, that's often a sign they belong in one service, where a local transaction does the job.

### `Payments.Contracts.dll`, shared by everyone

**The smell.** To "keep things consistent", a NuGet package holds the domain models every service uses:

```csharp
// Payments.Contracts 4.2.0, referenced by eleven services
public sealed record Payment(Guid Id, decimal Amount, string Currency, PaymentStatus Status, Guid CustomerId, ...);
public enum PaymentStatus { Pending, Captured, Refunded, Failed }
```

Payments adds `PartiallyRefunded` to the enum and publishes 4.3.0. Every consumer still on 4.2.0 that deserialises `Status` with `JsonStringEnumConverter` now throws on the first partial refund. So every consumer must upgrade before Payments can deploy — the release train again.

**Why it hurts.** A shared model package is the shared database smell again, at compile time. It also pushes one model across every context, which bounded contexts exist to avoid: Notifications doesn't need `CustomerId`, and now depends on it.

**The fix.** The provider owns its contract — an API schema or event schema, not a shared domain class — and each consumer declares its own type with only the fields it reads:

```csharp
// In Notifications, not shared
public sealed record PaymentCapturedMessage(Guid PaymentId, decimal Amount, string Currency, string ReceiptEmail);
```

Two practices keep this safe:

- **Tolerant reader**: "be as tolerant as possible when reading data from a service" — take what you need, ignore the rest.[^tolerantreader] System.Text.Json already ignores unknown *properties* by default,[^stjunmapped] but not unknown *enum values*, so read open-ended fields like a status as a string, or map unknown values to a fallback.
- **Consumer-driven contracts**: each consumer's tests record the requests and responses it relies on, and in the provider's build "each request is sent to the provider, and the actual response it generates is compared with the minimal expected response." Anything no consumer uses "is free to change without breaking tests."[^pact]

Changes then follow **expand and contract**: add the new field, migrate consumers, remove the old field once no contract uses it.

### Every screen calls five services

**The smell.** The merchant mobile app's home screen calls Onboarding, Payments, Ledger, Risk and Notifications directly, then stitches the results together on the phone. Each service exposes a public endpoint, its own auth, its own rate limits.

**Why it hurts.** Five round trips over a mobile network for one screen. Internal boundaries become public API, so splitting or merging services now breaks shipped apps. Cross-cutting concerns — authentication, rate limiting, TLS — are reimplemented in every service.

**The fix.** Two layers, which are often combined:

- An **API gateway** at the edge handles routing, authentication and rate limiting once, so internal services aren't public.
- A **backend for frontend (BFF)**: "one backend per user experience," typically "maintained by the same team as the user interface."[^bff] The mobile BFF serves the home screen in one call, aggregating the services behind it, shaped for that screen. The web dashboard gets its own.

**When not to.** One BFF per client type, not per screen;[^bff] and keep business rules out of it. A BFF that starts deciding whether a refund is allowed has become a service with no bounded context.

### Nobody can follow a request

**The smell.** A merchant reports a payment that "failed with an error". Five services logged something around that time, each with its own request ID, and correlating them by timestamp takes an afternoon.

**Why it hurts.** In a monolith, a stack trace shows the whole call. Across services, nothing does unless you build it.

**The fix.** **Distributed tracing**: one trace ID follows the request across every hop, carried in the W3C `traceparent` header, which OpenTelemetry uses by default.[^otel] Every service exports spans to the same backend, and the whole request — including the asynchronous `PaymentCaptured` handling, if the trace context travels in message headers — shows up as one tree. The [resilience post]({{< relref "designing-an-api-that-survives-failure.md" >}}) covers correlation IDs; tracing adds the parent–child structure across hops.

## Part 2: the smell of splitting too early

**The smell.** Five developers, fourteen services, a [Kubernetes]({{< relref "containers-kubernetes-and-the-linux-underneath/index.md" >}}) cluster and a service mesh. Most features still touch three repositories. Half of each sprint goes to pipelines, contract versions and debugging network calls between components that are always deployed together anyway.

**Why it hurts.** Fowler calls this the **microservice premium**: the operational and design cost "adds a premium to a project's cost and risk," and his advice is "don't even consider microservices unless you have a system that's too complex to manage as a monolith."[^premium] Worse, early boundaries are usually wrong. In *MonolithFirst* he notes that "almost all the successful microservice stories have started with a monolith that got too big and was broken up," because even experienced architects working in familiar domains "have great difficulty getting boundaries right at the beginning."[^monolithfirst] A wrong boundary inside a monolith is a refactor. Between services, it's a migration of data, contracts and deployments.

**The fix.** A **modular monolith**: one deployable, split internally along the same bounded contexts, with the boundaries enforced rather than hoped for:

```text
src/
  Payments/            Payments.csproj         (public: IPaymentsModule, events)
  Risk/                Risk.csproj             (public: IRiskModule)
  Ledger/              Ledger.csproj           (public: ILedgerModule, consumes events)
  Host/                Host.csproj             (composition root, references all modules)
```

```csharp
// Payments/IPaymentsModule.cs — the module's public API
public interface IPaymentsModule
{
    Task<PaymentResult> CreateAsync(CreatePayment command, CancellationToken ct);
}

// Payments/Internal/PaymentsModule.cs — invisible to Risk and Ledger
internal sealed class PaymentsModule(PaymentsDbContext db, IRiskModule risk) : IPaymentsModule { ... }

// Payments/PaymentsModuleRegistration.cs — how Host wires the module without seeing its internals
public static class PaymentsModuleRegistration
{
    public static IServiceCollection AddPaymentsModule(this IServiceCollection services) =>
        services.AddScoped<IPaymentsModule, PaymentsModule>();   // plus its DbContext, handlers, ...
}
```

The rules that make it a modular monolith rather than a big ball of mud:

- **Each module owns its tables**, ideally in its own database schema, with its own `DbContext`. No module queries another's tables — the shared-database smell, kept out from day one.
- **Modules talk through public interfaces and in-process events**, never through each other's internals. `internal` plus project references let the compiler enforce most of it; architecture tests catch the rest.
- **Each module follows the onion layering** from the code smells post inside its own boundary.

![A modular monolith: one Host deployable containing the Payments, Risk and Ledger modules, each with its own schema, talking through public interfaces and in-process events. An arrow shows Risk later extracted into its own service with its own database, with the in-process interface replaced by an HTTP client and the in-process events carried by a message broker](modular-monolith.svg)

When one module genuinely needs to be separate — Risk needs its own release cadence, or scales very differently — it's extracted along a boundary that has already proved itself. The interface becomes an HTTP client, in-process events go through a broker, and the rest of the system barely notices. Doing that one piece at a time around a running system is the **strangler fig** approach: "a gradual process of modernization" rather than a rewrite.[^stranglerfig]

**When microservices do pay.** When teams genuinely need to deploy independently — several teams, each owning a capability, blocked by each other's releases. When parts have very different scaling or availability needs (Risk scoring at 10× the traffic of merchant onboarding). When a component needs different technology or compliance isolation (card data kept in a PCI-scoped service). These are organisational and operational reasons, and Conway's law is why they matter: service boundaries that don't match team boundaries drift until they do. Microsoft's guide suggests sometimes running it in reverse — organising teams around the boundaries you want.[^msboundaries]

## Cheat sheet

| Smell | What it makes expensive | Fix | Name |
|---|---|---|---|
| One feature changes several services, deployed together | Every change | Split by business capability, not entity | Bounded contexts; avoiding the distributed monolith |
| A service reads another's tables | Any schema change | Data private to its service; share via API and events | Database per service |
| Long synchronous call chains | Availability, latency | Sync only what the response needs; local copies; events | Asynchronous messaging |
| Business transaction across services, compensated in a `catch` | Consistency after failures | Persisted, step-by-step local transactions with compensations | Saga (orchestration or choreography) |
| Shared domain-model package | Deploying any contract change | Provider-owned contracts, tolerant readers, contract tests | Consumer-driven contracts |
| Clients calling many services directly | Round trips, changing internals | Gateway at the edge, one backend per client type | API gateway, BFF |
| Logs you can't correlate across services | Debugging | One trace ID across every hop | Distributed tracing |
| More services than the team can run | Everything | One deployable, enforced internal boundaries; extract later | Modular monolith, strangler fig |

The method in one line: **draw the boundaries inside one deployable first, and make them network boundaries only when a team or a workload needs them to be.**

[^lewisfowler]: James Lewis and Martin Fowler, ["Microservices"](https://martinfowler.com/articles/microservices.html) (2014) — the definition, organisation around business capabilities, and decentralised data management.
[^msboundaries]: Microsoft, ["Identifying domain-model boundaries for each microservice"](https://learn.microsoft.com/en-us/dotnet/architecture/microservices/architect-microservice-container-applications/identify-microservice-domain-model-boundaries) — bounded contexts as microservice boundaries, the same identity under different models, and signs of a good boundary.
[^conway]: Melvin E. Conway, ["How Do Committees Invent?"](https://www.melconway.com/Home/Committees_Paper.html), *Datamation*, April 1968 — system structure copying the designing organisation's communication structure.
[^dbperservice]: Chris Richardson, ["Pattern: Database per service"](https://microservices.io/patterns/data/database-per-service.html) — data private to each service, and the drawbacks for cross-service queries and transactions.
[^saga]: Chris Richardson, ["Pattern: Saga"](https://microservices.io/patterns/data/saga.html) — the definition, compensating transactions, choreography and orchestration, and why 2PC is not an option.
[^sagapaper]: Hector Garcia-Molina and Kenneth Salem, ["Sagas"](https://doi.org/10.1145/38713.38742), *Proceedings of ACM SIGMOD*, 1987 — long-lived transactions as sequences of transactions with compensations.
[^tolerantreader]: Martin Fowler, ["TolerantReader"](https://martinfowler.com/bliki/TolerantReader.html).
[^stjunmapped]: Microsoft, ["Handle unmapped members during deserialization"](https://learn.microsoft.com/en-us/dotnet/standard/serialization/system-text-json/missing-members) — unknown JSON properties ignored by default, and `JsonUnmappedMemberHandling.Disallow` from .NET 8.
[^pact]: Pact, ["Introduction"](https://docs.pact.io/) and ["How Pact works"](https://docs.pact.io/getting_started/how_pact_works) — contracts generated from consumer tests, provider verification by replaying them, and unused provider behaviour being free to change.
[^bff]: Sam Newman, ["Backends For Frontends"](https://samnewman.io/patterns/architectural/bff/) — one backend per user experience, owned by the UI team.
[^otel]: OpenTelemetry, ["Context propagation"](https://opentelemetry.io/docs/concepts/context-propagation/) — correlating signals across services, and W3C TraceContext as the default propagator.
[^premium]: Martin Fowler, ["MicroservicePremium"](https://martinfowler.com/bliki/MicroservicePremium.html) (2015).
[^monolithfirst]: Martin Fowler, ["MonolithFirst"](https://martinfowler.com/bliki/MonolithFirst.html) (2015).
[^stranglerfig]: Martin Fowler, ["Strangler Fig"](https://martinfowler.com/bliki/StranglerFigApplication.html) (2024 revision).
