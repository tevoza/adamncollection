---
title: "Code Smells and the Patterns That Fix Them"
date: 2026-10-31T14:00:00+02:00
draft: true
tags: ["software", "architecture", "csharp", "dotnet", "design-patterns", "solid", "clean-architecture", "ai-generated"]
description: "SOLID, the Gang of Four patterns, onion architecture, DDD and CQRS, learned backwards: start from a smell in a C# payments codebase, refactor it, then name the principle the fix turned out to be."
---

Design patterns are usually taught as a catalogue: memorise Strategy, Adapter and Decorator, then look for places to put them. They were discovered the other way round. Someone kept hitting the same problem, kept reaching for the same fix, and eventually named it.

So this post starts from **smells** — Kent Beck's term, which Martin Fowler defines as "a surface indication that usually corresponds to a deeper problem in the system."[^codesmell] Each section shows one in a running C# payments codebase, says what it makes expensive, refactors it, and only then names the fix. Part 1 is code-level (SOLID and the classic patterns). Part 2 is architecture (onion, hexagonal, DDD aggregates, CQRS). Part 3 is the smell these fixes create when overapplied.

## The vocabulary, briefly

**The Gang of Four** are the authors of *Design Patterns* (1994): Gamma, Helm, Johnson and Vlissides.[^gof] The book catalogues 23 patterns as **creational** (Builder, Factory Method, Abstract Factory, Prototype, Singleton), **structural** (Adapter, Decorator, Composite, Facade, Proxy, Bridge, Flyweight) and **behavioural** (Strategy, State, Observer, Template Method, Visitor, Command, Chain of Responsibility and others). Its two underlying principles matter more than any single pattern: *program to an interface, not an implementation*, and *favour object composition over class inheritance*.

**SOLID** is five class-design principles — Single Responsibility, Open/Closed, Liskov Substitution, Interface Segregation, Dependency Inversion — collected by Robert C. Martin in the late 1990s and early 2000s[^dppaper][^asd] and named by Michael Feathers around 2004.[^cleanarchbook] Open/Closed comes from Bertrand Meyer[^oosc] and Liskov Substitution from Barbara Liskov. They're tests for whether a design will be cheap to change.

**Clean, onion and hexagonal architecture** apply the same idea to a whole application: business rules don't depend on databases, frameworks or the web.

**Domain-driven design** (Eric Evans, 2003)[^evans] is about what goes inside that protected centre. Strategically, it's about language and boundaries: a **bounded context** is where one model holds, and "you need a different model when the language changes."[^boundedcontext] Tactically, it's building blocks — value objects, aggregates, domain events, repositories — several of which turn up below as fixes.

**CQRS** is Greg Young's name for using one model to change data and another to read it,[^cqrs] an extension of Meyer's command–query separation.[^cqs]

Several GoF patterns are now language features. A Strategy can be a `Func<>`, Observer is `event`, Iterator is `IEnumerable<T>` and `yield`. The pattern still tells you *why* the feature exists; the book's class diagram is often the long way round.

## Part 1: smells in the code

### The same switch, in six places

**The smell.** Three payment methods, and every operation that differs between them switches on which one it's handling:

```csharp
public enum PaymentMethod { Card, BankTransfer, Wallet }

public sealed class PaymentService(CardApiClient card, BankApiClient bank, WalletApiClient wallet)
{
    public async Task<ChargeResult> ChargeAsync(Payment payment, CancellationToken ct)
    {
        switch (payment.Method)
        {
            case PaymentMethod.Card:
                var auth = await card.AuthoriseAndCaptureAsync(payment.Id, payment.Amount, ct);
                return new ChargeResult(auth.Approved, auth.Reference);
            case PaymentMethod.BankTransfer:
                var transfer = await bank.InitiateTransferAsync(payment.Id, payment.Amount, ct);
                return new ChargeResult(transfer.Accepted, transfer.EndToEndId);
            case PaymentMethod.Wallet:
                var push = await wallet.RequestPaymentAsync(payment.Id, payment.Amount, ct);
                return new ChargeResult(push.Ok, push.TransactionId);
            default:
                throw new ArgumentOutOfRangeException(nameof(payment));
        }
    }

    public decimal CalculateFee(Payment payment) => payment.Method switch
    {
        PaymentMethod.Card         => payment.Amount * 0.029m + 0.30m,
        PaymentMethod.BankTransfer => 0.50m,
        PaymentMethod.Wallet       => payment.Amount * 0.015m,
        _ => throw new ArgumentOutOfRangeException(nameof(payment))
    };

    public TimeSpan SettlementDelay(Payment payment) => payment.Method switch
    {
        PaymentMethod.Card         => TimeSpan.FromDays(2),
        PaymentMethod.BankTransfer => TimeSpan.Zero,
        PaymentMethod.Wallet       => TimeSpan.FromDays(1),
        _ => throw new ArgumentOutOfRangeException(nameof(payment))
    };

    // ...and the same switch again in RefundAsync, in the webhook handler,
    // and in the reconciliation job
}
```

**Why it hurts.** One switch is fine. The smell is the *same* switch repeated — Fowler renamed "Switch Statements" to "Repeated Switches" for this reason.[^refactoring] A fourth payment method means finding all six. The compiler won't help much: switch expressions warn when a value is unhandled (CS8509), but the `_ => throw` arm everyone adds to silence the unnamed-enum warning (CS8524) switches that off,[^exhaustive] and nothing finds switches in other classes. And there's no single place to read how card payments work.

**The fix.** Turn each case into an implementation of one interface:

```csharp
public interface IPaymentProvider
{
    PaymentMethod Method { get; }
    TimeSpan SettlementDelay { get; }
    decimal CalculateFee(decimal amount);
    Task<ChargeResult> ChargeAsync(Payment payment, CancellationToken ct);
    // RefundAsync belongs here too, and turns out to be trickier: see the Liskov section
}

public sealed class CardProvider(CardApiClient card) : IPaymentProvider
{
    public PaymentMethod Method => PaymentMethod.Card;
    public TimeSpan SettlementDelay => TimeSpan.FromDays(2);
    public decimal CalculateFee(decimal amount) => amount * 0.029m + 0.30m;

    public async Task<ChargeResult> ChargeAsync(Payment payment, CancellationToken ct)
    {
        var auth = await card.AuthoriseAndCaptureAsync(payment.Id, payment.Amount, ct);
        return new ChargeResult(auth.Approved, auth.Reference);
    }
}

// BankTransferProvider and WalletProvider follow the same shape
```

Register them all and build a lookup once:

```csharp
builder.Services.AddSingleton<IPaymentProvider, CardProvider>();
builder.Services.AddSingleton<IPaymentProvider, BankTransferProvider>();
builder.Services.AddSingleton<IPaymentProvider, WalletProvider>();
builder.Services.AddSingleton<PaymentProviders>();

public sealed class PaymentProviders(IEnumerable<IPaymentProvider> providers)
{
    private readonly Dictionary<PaymentMethod, IPaymentProvider> _byMethod =
        providers.ToDictionary(p => p.Method);

    public IPaymentProvider For(PaymentMethod method) =>
        _byMethod.TryGetValue(method, out var provider)
            ? provider
            : throw new NotSupportedException($"No provider registered for {method}");
}
```

`ChargeAsync` becomes `providers.For(payment.Method).ChargeAsync(payment, ct)`. `ToDictionary` throws on a duplicate method; resolve `PaymentProviders` once after `builder.Build()` to make that fail at startup rather than on the first request. The switch didn't vanish — it moved into three registration lines, the one place that enumerates payment methods.

![Before: PaymentService has a switch on Method in ChargeAsync, CalculateFee, SettlementDelay and RefundAsync, and WebhookHandler and ReconciliationJob each have another. After: Program.cs registers CardProvider, BankTransferProvider and WalletProvider, PaymentProviders.For() looks one up, and each provider class holds its own charge, fee and settlement logic](switch-to-strategy.svg)

Two notes. The providers are singletons, so per the [DI post]({{< relref "di-lifetimes-and-failure-boundaries.md" >}}) nothing shorter-lived may live inside them — including a typed `HttpClient`, which is transient; use `IHttpClientFactory` or `PooledConnectionLifetime` instead.[^typedclients] And .NET 8's **keyed services** can do the lookup too,[^keyed] but with a runtime key that means calling `IServiceProvider` from business code; the dictionary keeps the container at the edge.

**What it's called.** *Replace Conditional with Polymorphism*.[^refactoring] Each provider is a **Strategy** — "define a family of algorithms, encapsulate each one, and make them interchangeable"[^gof] — and the design satisfies **Open/Closed**: a new method is a new class and a registration line, with no existing class edited. `PaymentProviders` is a simple registry; GoF's Factory Method and Abstract Factory cover the related job of creating objects without naming concrete classes, which DI containers now mostly do.

**When not to.** If the cases differ only in data (three fee rates), use a table. If the *types* are stable but *operations* keep growing, polymorphism is the wrong way round — see Visitor in the next section.

### Every caller translating the same foreign payload

**The smell.** Each provider has its own webhook format — minor units, lowercase currencies, its own status strings — and every consumer translates inline:

```csharp
// WebhookController
var hook = JsonSerializer.Deserialize<CardWebhook>(body)!;
if (hook.Type == "charge.succeeded")
{
    var amount = hook.Data.AmountMinor / 100m;
    var currency = hook.Data.Currency.ToUpperInvariant();
    await payments.MarkCompletedAsync(hook.Data.MerchantReference, amount, currency, ct);
}

// ReconciliationJob
foreach (var charge in await cardClient.ListChargesAsync(from, to, ct))
{
    var amount = charge.AmountMinor / 100m;
    var status = charge.Status switch
    {
        "succeeded" => PaymentStatus.Completed,
        "failed" or "canceled" => PaymentStatus.Failed,
        _ => PaymentStatus.Pending
    };
    // compare with our record...
}
```

**Why it hurts.** `/ 100m` is wrong for yen (no minor unit) and Bahraini dinar (three).[^iso4217] The fix has to be made everywhere, and the copies already disagree about `"canceled"`. That's **Shotgun Surgery** — one change, many edits[^refactoring] — and the provider's vocabulary has leaked into code that should only speak yours.

**The fix.** Translate once, at the boundary, into a type you own:

```csharp
public enum PaymentEventKind { Completed, Failed, Refunded }

public sealed record PaymentEvent(
    Guid PaymentId, PaymentEventKind Kind, decimal Amount, string Currency, DateTimeOffset OccurredAt);

public interface IProviderEventTranslator
{
    PaymentMethod Method { get; }
    PaymentEvent? Translate(string rawBody);   // null for event types we don't care about
}

public sealed class CardEventTranslator : IProviderEventTranslator
{
    public PaymentMethod Method => PaymentMethod.Card;

    public PaymentEvent? Translate(string rawBody)
    {
        var hook = JsonSerializer.Deserialize<CardWebhook>(rawBody)!;
        var kind = hook.Type switch
        {
            "charge.succeeded" => PaymentEventKind.Completed,
            "charge.failed" or "charge.canceled" => PaymentEventKind.Failed,
            "charge.refunded" => PaymentEventKind.Refunded,
            _ => (PaymentEventKind?)null
        };
        if (kind is null) return null;

        var currency = hook.Data.Currency.ToUpperInvariant();
        return new PaymentEvent(
            Guid.Parse(hook.Data.MerchantReference),
            kind.Value,
            hook.Data.AmountMinor / CurrencyInfo.MinorUnitFactor(currency),   // 1, 100 or 1000
            currency,
            DateTimeOffset.FromUnixTimeSeconds(hook.Created));
    }
}
```

The status switch remains, but it's the only one, and translating provider strings is exactly a translator's job. Everything downstream consumes `PaymentEvent`.

![Three provider payloads on the left, each in its own vocabulary: a card webhook with type charge.succeeded, an amount of 1999 in minor units and a lowercase currency; a bank status report with TxSts ACSC and InstdAmt 19.99; a wallet event with state PAID and amount_cents 1999. Each passes through its own translator inside a dashed boundary and comes out as a PaymentEvent with PaymentId, Kind, Amount, Currency and OccurredAt, which the webhooks endpoint, reconciliation job and support tool consume](translation-boundary.svg)

**What it's called.** `CardProvider` from the previous section is a textbook **Adapter** — "convert the interface of a class into another interface clients expect"[^gof] — and the translator is the same idea applied to data. Together they form what Evans calls an **anti-corruption layer**.[^evans]

There's no GoF "reader" or "mapper" pattern. What matters is who owns the types:

- **You own them** and keep converting to a common shape: don't convert — give them a shared interface.
- **You don't own them**: adapt once at the boundary, as above.
- **You own a closed set** and keep adding *operations* over it: that's **Visitor**.

The Visitor smell: a `PaymentOutcome` hierarchy (`Captured`, `Declined`), and ledger, notification and analytics code each with its own `if (outcome is Captured) … else if …` chain. Someone adds `PendingReview` and analytics silently drops it. Visitor "lets you define a new operation without changing the classes of the elements on which it operates":[^gof] one visitor per operation, one `Visit` per subtype, so a new subtype is a compile error everywhere. Modern C# usually uses pattern matching instead:

```csharp
public abstract record PaymentOutcome(Guid PaymentId);
public sealed record Captured(Guid PaymentId, decimal Amount) : PaymentOutcome(PaymentId);
public sealed record Declined(Guid PaymentId, string Reason) : PaymentOutcome(PaymentId);

static LedgerEntry? ToLedger(PaymentOutcome outcome) => outcome switch
{
    Captured c => new LedgerEntry(c.PaymentId, c.Amount),
    Declined   => null,
    _ => throw new UnreachableException()
};
```

The `_` arm is the catch: the compiler can't know the hierarchy is closed, so a new subtype fails at runtime. C# 15 (with .NET 11, November 2026) fixes this with **union types**: make the cases plain records, declare `public union PaymentOutcome(Captured, Declined);`, and the compiler checks the switch is exhaustive. Use a `null` arm rather than `_`, since a default union holds null.[^unions]

### A constructor with eleven parameters

**The smell.**

```csharp
public sealed class PaymentService(
    PaymentDbContext db,
    PaymentProviders providers,
    IFraudScreening fraud,
    IFxRates fx,
    IEmailSender email,
    ISmsSender sms,
    ILedger ledger,
    IAnalytics analytics,
    IPdfRenderer pdf,
    IBlobStorage blobs,
    ILogger<PaymentService> logger)
{
    public Task<PaymentResult> CreateAsync(CreatePayment command, CancellationToken ct) { ... }
    public Task RefundAsync(Guid paymentId, decimal amount, CancellationToken ct) { ... }
    public Task HandleProviderEventAsync(PaymentEvent evt, CancellationToken ct) { ... }
    public Task<byte[]> RenderReceiptAsync(Guid paymentId, CancellationToken ct) { ... }
    public Task<Statement> MonthlyStatementAsync(string merchantId, DateOnly month, CancellationToken ct) { ... }
}
```

**Why it hurts.** Draw a grid of methods against the dependencies they use: receipts use `db`, `pdf`, `blobs`; creation uses `db`, `providers`, `fraud`, `fx`; statements use `db`, `fx`. Nothing uses all eleven — it's several classes sharing a constructor. Testing a refund means building nine collaborators it never calls, and finance's and risk's changes collide in the same file. Long Parameter List and Large Class are the symptoms; **Divergent Change** — one class changed for many reasons — is the cause.[^refactoring]

**The fix.** Split along the clusters:

```csharp
public sealed class CreatePaymentHandler(PaymentDbContext db, PaymentProviders providers, IFraudScreening fraud, IFxRates fx) { ... }
public sealed class RefundPaymentHandler(PaymentDbContext db, PaymentProviders providers) { ... }
public sealed class ProviderEventHandler(PaymentDbContext db, ILedger ledger, IEmailSender email, ISmsSender sms, IAnalytics analytics) { ... }
public sealed class ReceiptRenderer(PaymentDbContext db, IPdfRenderer pdf, IBlobStorage blobs) { ... }
public sealed class MerchantStatements(PaymentDbContext db, IFxRates fx) { ... }
```

`ProviderEventHandler` is still long; that's a separate smell, fixed a few sections down. A request object plus a class that carries it out is a lightweight GoF **Command** ("encapsulate a request as an object"),[^gof] the shape CQRS and MediatR are built on.

**What it's called.** The **Single Responsibility Principle**, often misquoted as "do one thing." Martin's version is "one and only one reason to change," where reasons are *people*: "it is people who request changes."[^srp] His other phrasing is more useful: "Gather together the things that change for the same reasons. Separate those things that change for different reasons." Finance owns statements, support owns receipts, risk owns fraud screening — those are the seams.

**When not to.** SRP read as "small" gives you five one-method classes that always change together. Split by who asks for the change, not by line count.

### A subtype that throws `NotSupportedException`

**The smell.** `RefundAsync` joins the provider interface. Bank transfers can't be reversed by the bank — a refund is a new outbound payment — so:

```csharp
public interface IPaymentProvider
{
    // ...as before
    Task<RefundResult> RefundAsync(Payment payment, decimal amount, CancellationToken ct);
}

public sealed class BankTransferProvider(BankApiClient bank) : IPaymentProvider
{
    // ...
    public Task<RefundResult> RefundAsync(Payment payment, decimal amount, CancellationToken ct) =>
        throw new NotSupportedException("Bank transfers can't be refunded through the provider.");
}
```

Soon the callers look like this:

```csharp
if (payment.Method != PaymentMethod.BankTransfer)
    await providers.For(payment.Method).RefundAsync(payment, amount, ct);
else
    await payouts.SendRefundPayoutAsync(payment, amount, ct);
```

**Why it hurts.** That `if` is the first section's switch, back again. The interface promised every provider can refund; one can't, so callers must know the concrete type the interface was meant to hide.

**What it's called.** A **Liskov Substitution** violation. Liskov and Wing: a property provable about objects of type T "should be true for objects y of type S where S is a subtype of T."[^liskov] Code written against an interface must work with any implementation. It's about behaviour, so the compiler can't check it — the signature is fine; the contract (throws instead of refunding, or rejects inputs the interface implies are valid) isn't.

**The fix.** Make the interface tell the truth. Refunding is a separate capability:

```csharp
public interface IPaymentProvider
{
    PaymentMethod Method { get; }
    TimeSpan SettlementDelay { get; }
    decimal CalculateFee(decimal amount);
    Task<ChargeResult> ChargeAsync(Payment payment, CancellationToken ct);
}

public interface IRefundProvider
{
    PaymentMethod Method { get; }
    Task<RefundResult> RefundAsync(Payment payment, decimal amount, CancellationToken ct);
}

public sealed class CardProvider(CardApiClient card) : IPaymentProvider, IRefundProvider { ... }
public sealed class BankTransferProvider(BankApiClient bank) : IPaymentProvider { ... }
```

The refund handler looks up `IRefundProvider`s; a missing entry is now a business decision (the outbound payout) in one place, not a crash. Register both interfaces against the same instance, or you get two card providers:

```csharp
builder.Services.AddSingleton<CardProvider>();
builder.Services.AddSingleton<IPaymentProvider>(sp => sp.GetRequiredService<CardProvider>());
builder.Services.AddSingleton<IRefundProvider>(sp => sp.GetRequiredService<CardProvider>());
```

That's the **Interface Segregation Principle**: "clients should not be forced to depend upon interfaces that they do not use."[^asd] LSP tells you something's wrong; ISP is usually the fix.

**When not to.** `Stream.Seek` throws `NotSupportedException` on a network stream, but `Stream` also exposes `CanSeek`. An optional capability advertised up front is part of the contract; one discovered by exception isn't.

### `new` in the middle of business logic

**The smell.** A job expires payments pending for over thirty minutes:

```csharp
public sealed class PaymentExpiryJob
{
    public async Task RunAsync(CancellationToken ct)
    {
        using var conn = new SqlConnection("Server=payments-sql;Database=payments;...");
        var cutoff = DateTime.UtcNow.AddMinutes(-30);

        var stale = await conn.QueryAsync<Guid>(
            "SELECT Id FROM Payments WHERE Status = 'Pending' AND CreatedAt < @cutoff", new { cutoff });

        foreach (var id in stale)
        {
            await conn.ExecuteAsync("UPDATE Payments SET Status = 'Expired' WHERE Id = @id", new { id });

            using var http = new HttpClient();
            await http.PostAsJsonAsync("https://notify.internal/payment-expired", new { id }, ct);
        }
    }
}
```

**Why it hurts.** The class holds one business rule, and testing it needs a real SQL Server, a real endpoint and thirty minutes (or doctored timestamps). The plumbing is also wrong on its own terms: an `HttpClient` per call can exhaust ports under load,[^httpclient] and the connection string is hard-coded.

**The fix.** Depend on things the job owns, in its own vocabulary:

```csharp
public interface IPaymentRepository
{
    Task<IReadOnlyList<Payment>> FindPendingCreatedBeforeAsync(DateTimeOffset cutoff, CancellationToken ct);
    Task SaveAsync(CancellationToken ct);
}

public interface IPaymentNotifier
{
    Task PaymentExpiredAsync(Guid paymentId, CancellationToken ct);
}

public sealed class PaymentExpiryJob(IPaymentRepository payments, IPaymentNotifier notifier, TimeProvider time)
{
    private static readonly TimeSpan PendingTimeout = TimeSpan.FromMinutes(30);

    public async Task RunAsync(CancellationToken ct)
    {
        var stale = await payments.FindPendingCreatedBeforeAsync(time.GetUtcNow() - PendingTimeout, ct);
        foreach (var payment in stale)
            payment.Expire();
        await payments.SaveAsync(ct);

        foreach (var payment in stale)
            await notifier.PaymentExpiredAsync(payment.Id, ct);
    }
}
```

The rule's test now runs in a millisecond with an in-memory repository and `FakeTimeProvider` from `Microsoft.Extensions.TimeProvider.Testing`.[^timeprovider] Production wires in EF Core and a typed `HttpClient`. (Run from a `BackgroundService`, create a DI scope per run. And saving before notifying leaves a gap the Observer section closes.)

**What it's called.** The **Dependency Inversion Principle**: "High level modules should not depend upon low level modules. Both should depend upon abstractions," and "Details should depend upon abstractions."[^dip] The *inversion* is ownership: the SQL code now implements an interface defined by the rule's side, in its terms (`FindPendingCreatedBeforeAsync`, not `ExecuteQuery`). That's why **DI isn't DIP** — injecting a `SqlConnection` is DI, but the rule still depends on SQL Server.

**When not to.** `new List<>()` and `new PaymentEvent(...)` are fine. Abstract what touches the outside world: network, disk, database, clock, randomness, configuration.

### The same try/catch around every call

**The smell.** Every provider's `ChargeAsync` has grown the same wrapping:

```csharp
public async Task<ChargeResult> ChargeAsync(Payment payment, CancellationToken ct)
{
    logger.LogInformation("Charging {PaymentId} via card", payment.Id);
    var started = Stopwatch.GetTimestamp();
    try
    {
        var auth = await card.AuthoriseAndCaptureAsync(payment.Id, payment.Amount, ct);
        return new ChargeResult(auth.Approved, auth.Reference);
    }
    finally
    {
        metrics.RecordChargeDuration(PaymentMethod.Card, Stopwatch.GetElapsedTime(started));
    }
}
```

The bank provider forgot the metrics; the wallet provider added a retry loop.

**Why it hurts.** Logging and timing have nothing to do with charging a card, yet they're most of each class, they drift, and a log-format change touches every provider.

**The fix.** Implement the interface again, wrapping another instance of it:

```csharp
public sealed class InstrumentedProvider(
    IPaymentProvider inner, ILogger<InstrumentedProvider> logger, PaymentMetrics metrics) : IPaymentProvider
{
    public PaymentMethod Method => inner.Method;
    public TimeSpan SettlementDelay => inner.SettlementDelay;
    public decimal CalculateFee(decimal amount) => inner.CalculateFee(amount);

    public async Task<ChargeResult> ChargeAsync(Payment payment, CancellationToken ct)
    {
        logger.LogInformation("Charging {PaymentId} via {Method}", payment.Id, inner.Method);
        var started = Stopwatch.GetTimestamp();
        try
        {
            return await inner.ChargeAsync(payment, ct);
        }
        finally
        {
            metrics.RecordChargeDuration(inner.Method, Stopwatch.GetElapsedTime(started));
        }
    }
}
```

The built-in container has no decoration support, so wrap in a factory registration (or use Scrutor's `Decorate`):

```csharp
builder.Services.AddSingleton<CardProvider>();
builder.Services.AddSingleton<IPaymentProvider>(sp => new InstrumentedProvider(
    sp.GetRequiredService<CardProvider>(),
    sp.GetRequiredService<ILogger<InstrumentedProvider>>(),
    sp.GetRequiredService<PaymentMetrics>()));
```

A decorator wraps one interface, so `IRefundProvider` needs its own.

![A ChargeAsync call enters the outermost InstrumentedProvider, which logs and starts a timer, then calls the inner CardProvider, which calls the provider's card API. The result returns back out through each layer, and InstrumentedProvider records the duration on the way out. Both providers implement IPaymentProvider](decorator-chain.svg)

**What it's called.** **Decorator**: "attach additional responsibilities to an object dynamically."[^gof] `HttpClient`'s `DelegatingHandler`s, ASP.NET Core middleware and MediatR behaviours share the shape; because they can short-circuit, they're also close to **Chain of Responsibility**. The difference is intent: a decorator wraps a call that normally goes through; a chain passes a request along until someone handles it.

**When not to.** Be careful with a retry decorator. Retrying a timed-out charge double-charges unless the call carries an idempotency key (see the [API post]({{< relref "designing-an-api-that-survives-failure.md" >}})). And decorators are invisible at the call site, so keep the wiring in one obvious place.

### Status checks scattered everywhere

**The smell.** `Status` has a public setter, and its rules live wherever someone needed them:

```csharp
// RefundPaymentHandler
if (payment.Status != PaymentStatus.Completed && payment.Status != PaymentStatus.PartiallyRefunded)
    return RefundResult.NotRefundable;

// PaymentsController, deciding whether to show "Cancel"
var canCancel = payment.Status == PaymentStatus.Pending || payment.Status == PaymentStatus.Authorised;

// ProviderEventHandler
if (evt.Kind == PaymentEventKind.Completed)
    payment.Status = PaymentStatus.Completed;
```

**Why it hurts.** Nobody can see the whole lifecycle, and the last snippet has a real bug: webhooks arrive late and out of order, so a "completed" event after a refund flips the payment back to `Completed` — refundable again.

**The fix.** Remove the setter and route every transition through one table:

```csharp
public enum PaymentStatus { Pending, Authorised, Completed, PartiallyRefunded, Refunded, Failed, Cancelled, Expired }

public sealed class Payment
{
    private static readonly Dictionary<PaymentStatus, PaymentStatus[]> Allowed = new()
    {
        [PaymentStatus.Pending]           = [PaymentStatus.Authorised, PaymentStatus.Completed, PaymentStatus.Failed, PaymentStatus.Cancelled, PaymentStatus.Expired],
        [PaymentStatus.Authorised]        = [PaymentStatus.Completed, PaymentStatus.Cancelled, PaymentStatus.Expired],
        [PaymentStatus.Completed]         = [PaymentStatus.PartiallyRefunded, PaymentStatus.Refunded],
        [PaymentStatus.PartiallyRefunded] = [PaymentStatus.PartiallyRefunded, PaymentStatus.Refunded],
    };

    public PaymentStatus Status { get; private set; } = PaymentStatus.Pending;

    public bool CanTransitionTo(PaymentStatus next) =>
        Allowed.TryGetValue(Status, out var targets) && targets.Contains(next);

    public bool CanCancel => CanTransitionTo(PaymentStatus.Cancelled);

    public void MarkCompleted() => TransitionTo(PaymentStatus.Completed);
    public void Cancel() => TransitionTo(PaymentStatus.Cancelled);
    public void Expire() => TransitionTo(PaymentStatus.Expired);

    private void TransitionTo(PaymentStatus next)
    {
        if (!CanTransitionTo(next))
            throw new InvalidPaymentTransitionException(Id, Status, next);
        Status = next;
    }

    // Id, Amount, Refund(...) and the rest omitted
}
```

The lifecycle is now one readable table, the controller asks `CanCancel`, and the late webhook is rejected. When the event handler hits a rejected transition, it should tell two cases apart:

- **Stale or duplicate** (already completed or refunded): acknowledge and ignore.
- **Conflict** (we expired or cancelled it, but the provider says money moved — the expiry job above can cause exactly this): acknowledge, then record and alert, or the customer's money is lost.

Either way, return success, or the provider redelivers.

![The transition table drawn as a state diagram. Pending goes to Authorised, Completed, Failed, Expired or Cancelled; Authorised goes to Completed, Cancelled or Expired; Completed goes to PartiallyRefunded or Refunded; PartiallyRefunded loops to itself or goes to Refunded. A dashed red arrow from Refunded back to Completed, labelled as a late completed webhook, is rejected](payment-states.svg)

**What it's called.** The GoF **State** pattern goes further — "allow an object to alter its behavior when its internal state changes"[^gof] — with one object per state:

```csharp
public abstract class PaymentState
{
    public virtual PaymentState Complete(Payment p) => throw Invalid(p, nameof(Complete));
    public virtual PaymentState Refund(Payment p, decimal amount) => throw Invalid(p, nameof(Refund));
    // ...
}

public sealed class CompletedState : PaymentState
{
    public override PaymentState Refund(Payment p, decimal amount) =>
        amount == p.Amount ? new RefundedState() : new PartiallyRefundedState();
}
```

Use it when *behaviour* differs a lot by state. When the difference is mostly "allowed or not", the table is less code and persists as a plain enum column.

### One method that does everything that should happen next

**The smell.**

```csharp
public async Task HandleAsync(PaymentEvent evt, CancellationToken ct)
{
    var payment = await db.Payments.SingleAsync(p => p.Id == evt.PaymentId, ct);
    payment.MarkCompleted();
    await db.SaveChangesAsync(ct);

    await ledger.PostAsync(payment.Id, payment.Amount, ct);
    await email.SendReceiptAsync(payment.CustomerId, payment.Id, ct);
    await sms.SendPaymentConfirmationAsync(payment.CustomerId, payment.Id, ct);
    await analytics.TrackAsync("payment_completed", payment.Id, ct);
    await loyalty.AwardPointsAsync(payment.CustomerId, payment.Amount, ct);   // added last sprint
}
```

**Why it hurts.** Every new reaction edits this method, and payments complete from three places — loyalty only got added to one. If email is down, the method throws *after* the commit: the caller sees failure for something that succeeded.

**The fix.** The payment announces what happened; reactions subscribe:

```csharp
public sealed record PaymentCompleted(Guid PaymentId, Guid CustomerId, decimal Amount) : IDomainEvent;

public sealed class Payment
{
    private readonly List<IDomainEvent> _events = [];
    public IReadOnlyList<IDomainEvent> Events => _events;

    public void MarkCompleted()
    {
        TransitionTo(PaymentStatus.Completed);
        _events.Add(new PaymentCompleted(Id, CustomerId, Amount));
    }
}

public sealed class SendReceiptOnPaymentCompleted(IEmailSender email) : IDomainEventHandler<PaymentCompleted>
{
    public Task HandleAsync(PaymentCompleted evt, CancellationToken ct) =>
        email.SendReceiptAsync(evt.CustomerId, evt.PaymentId, ct);
}
```

Every completion path raises the event, and loyalty points become a new handler. Dispatching in-process after `SaveChangesAsync` still loses events on a crash, so for anything that must happen, write them to an **outbox** in the same transaction (see the [API post]({{< relref "designing-an-api-that-survives-failure.md" >}})) and make handlers idempotent.

**What it's called.** The intent is **Observer**: "when one object changes state, all its dependents are notified and updated automatically."[^gof] The mechanics are **publish–subscribe** — a dispatcher routes by event type, and the payment doesn't know its listeners. C#'s `event` and `IObservable<T>` are in-memory Observer, the wrong tool here.

**When not to.** Events trade readable control flow for decoupling. Use them for reactions owned by someone else; call directly anything that's part of completing the payment.

### Tests that build objects with nine positional arguments

**The smell.**

```csharp
var payment = new Payment(Guid.NewGuid(), Guid.NewGuid(), 100m, "EUR", PaymentMethod.Card, null, null, false, DateTimeOffset.UtcNow);
```

Two hundred of these. Nobody can tell which `null` matters, a new parameter breaks every test, and since the State section there's no way to construct a refunded payment directly.

**The fix.** A builder with defaults, so each test states only what it cares about:

```csharp
public sealed class PaymentBuilder
{
    private decimal _amount = 100m;
    private string _currency = "EUR";
    private PaymentMethod _method = PaymentMethod.Card;
    private PaymentStatus _target = PaymentStatus.Pending;

    public PaymentBuilder WithAmount(decimal amount, string currency = "EUR")
    {
        (_amount, _currency) = (amount, currency);
        return this;
    }

    public PaymentBuilder ByBankTransfer() { _method = PaymentMethod.BankTransfer; return this; }
    public PaymentBuilder Completed() { _target = PaymentStatus.Completed; return this; }
    public PaymentBuilder Refunded() { _target = PaymentStatus.Refunded; return this; }

    public Payment Build()
    {
        var payment = Payment.Create(customerId: Guid.NewGuid(), _amount, _currency, _method);
        if (_target is PaymentStatus.Completed or PaymentStatus.Refunded) payment.MarkCompleted();
        if (_target is PaymentStatus.Refunded) payment.Refund(payment.Amount);
        return payment;
    }
}

var payment = new PaymentBuilder().WithAmount(250m).Refunded().Build();
```

`Build()` replays the real transitions, so test objects are always ones the domain could have produced.

**What it's called.** A *test data builder*, from Freeman and Pryce's *Growing Object-Oriented Software*.[^goos] It borrows the shape of GoF **Builder** ("separate the construction of a complex object from its representation"[^gof]) for a different goal: readable defaults. In production code, `required` properties and object initialisers usually make a builder unnecessary.

### `decimal amount, string currency`, everywhere

**The smell.** Every sample so far passes money as a `decimal`, sometimes with a `string` beside it:

```csharp
public decimal CalculateFee(decimal amount) => amount * 0.029m + 0.30m;   // 0.30 of what?

var total = payment.Amount + fee;   // payment is in GBP; the fee table is in EUR

if (amount <= 0) throw new ArgumentException(...);              // in CreatePaymentHandler
if (amount <= 0 || amount > payment.Amount) throw ...;          // in RefundPaymentHandler
if (currency.Length != 3) throw ...;                            // in two of the five places that take a currency
```

**Why it hurts.** Pounds plus euros compiles. Validity rules (real currency code, correct precision) are re-checked inconsistently wherever an amount enters. That's **Primitive Obsession**.[^refactoring]

**The fix.** A small immutable type that owns those rules:

```csharp
public sealed record Money
{
    public decimal Amount { get; }
    public string Currency { get; }

    public Money(decimal amount, string currency)
    {
        if (currency is not { Length: 3 })
            throw new ArgumentException("Expected an ISO 4217 code.", nameof(currency));
        currency = currency.ToUpperInvariant();
        if (decimal.Round(amount, CurrencyInfo.MinorUnits(currency)) != amount)
            throw new ArgumentException($"Too many decimal places for {currency}.", nameof(amount));

        (Amount, Currency) = (amount, currency);
    }

    public static Money Zero(string currency) => new(0m, currency);

    public static Money FromMinorUnits(long minor, string currency)
    {
        var code = currency.ToUpperInvariant();
        return new(minor / CurrencyInfo.MinorUnitFactor(code), code);
    }

    public Money MultiplyRounded(decimal factor) =>
        new(decimal.Round(Amount * factor, CurrencyInfo.MinorUnits(Currency), MidpointRounding.AwayFromZero), Currency);

    public static Money operator +(Money a, Money b) =>
        a.Currency == b.Currency
            ? new Money(a.Amount + b.Amount, a.Currency)
            : throw new CurrencyMismatchException(a.Currency, b.Currency);
}
```

From here on every amount is `Money`, and the translator's conversion becomes `Money.FromMinorUnits`. Get-only properties stop a `with` expression bypassing the constructor. A 2.9% fee on €19.99 (€0.57971) is now rejected until the code says how it rounds — a business rule that finally has one home, `MultiplyRounded`. "Must be positive" is deliberately absent: fees can be adjusted negatively, so "charge > 0" and "refund ≤ remaining" stay in the use cases.

**What it's called.** A **value object** (from DDD, not GoF):[^evans] defined by its values, no identity, guarding its own invariants. Probably the best return on effort in this post — some bugs stop compiling, the rest fail at creation instead of month-end. EF Core 8 maps these as **complex types**, stored inline in the owner's columns; configure them explicitly, since they aren't discovered by convention.[^complextypes]

### Two classes that are the same except for two steps

**The smell.** Bank B's settlement export was copied from Bank A's:

```csharp
public sealed class BankASettlementExport { public async Task RunAsync(DateOnly day, CancellationToken ct) { /* load, format as CSV, sign, SFTP */ } }
public sealed class BankBSettlementExport { public async Task RunAsync(DateOnly day, CancellationToken ct) { /* load, format as XML, sign, HTTPS */ } }
```

Months later, A excludes refunded payments after a bug fix. B doesn't; nobody remembered the copy.

**The fix, first version.** Pull the shared sequence into a base class:

```csharp
public abstract class SettlementExport(IPaymentRepository payments, IFileSigner signer)
{
    public async Task RunAsync(DateOnly day, CancellationToken ct)
    {
        var settled = await payments.FindSettledOnAsync(day, ct);   // excludes refunded, in one place
        var file = Format(settled);
        var signed = signer.Sign(file);
        await UploadAsync(signed, ct);
    }

    protected abstract byte[] Format(IReadOnlyList<Payment> settled);
    protected abstract Task UploadAsync(byte[] file, CancellationToken ct);
}
```

That's **Template Method**: "define the skeleton of an algorithm in an operation, deferring some steps to subclasses."[^gof]

**The fix, second version.** Bank C wants A's CSV and B's HTTPS. Inheritance can't mix one subclass's `Format` with another's `UploadAsync` — two independent choices are forced together. Make each step a collaborator:

```csharp
public interface ISettlementFormat { byte[] Format(IReadOnlyList<Payment> settled); }
public interface ISettlementUpload { Task UploadAsync(byte[] file, CancellationToken ct); }

public sealed class SettlementExport(
    IPaymentRepository payments, IFileSigner signer, ISettlementFormat format, ISettlementUpload upload)
{
    public async Task RunAsync(DateOnly day, CancellationToken ct)
    {
        var settled = await payments.FindSettledOnAsync(day, ct);
        await upload.UploadAsync(signer.Sign(format.Format(settled)), ct);
    }
}

// Bank C: new SettlementExport(payments, signer, new CsvSettlementFormat(), new HttpsSettlementUpload(...))
```

Each step is a Strategy and combinations are free — *favour composition over inheritance* in action. Template Method is fine with one axis of variation; with two, compose. (Two dimensions varying independently is also what GoF **Bridge** addresses.[^gof])

## Part 2: smells in the architecture

The same smells, one level up: between projects and layers, and between the model that writes and the screens that read.

### A domain that knows about the database and the web

**The smell.** `Payments.Domain` references EF Core and the API contracts:

```csharp
using System.ComponentModel.DataAnnotations.Schema;
using Microsoft.EntityFrameworkCore;
using Payments.Api.Contracts;

[Table("payments"), Index(nameof(ProviderReference))]
public class Payment
{
    [Column(TypeName = "decimal(18,2)")]
    public decimal Amount { get; set; }

    public virtual ICollection<Refund> Refunds { get; set; } = [];   // virtual for lazy loading

    public PaymentResponse ToResponse() => new(Id, Amount, Status.ToString());
}
```

**Why it hurts.** The most important rules now change when EF Core is upgraded or an API field is renamed, can't be tested without both, and nothing stops someone injecting a `DbContext` into an entity.

**The fix.** Make dependency direction a rule the compiler enforces through project references:

![Three concentric rings. At the centre, Domain: entities, value objects, domain events. Around it, Application: use cases and the ports they own, such as IPaymentRepository and IPaymentNotifier. The outer ring holds Infrastructure (EF Core DbContext, provider adapters, email) on one side and Presentation (API controllers, webhooks endpoints) on the other. Arrows from every outer ring point inward; none point outward. Program.cs, the composition root, sits outside all of them and references everything](onion-layers.svg)

- **Domain** — entities, value objects, events. References only the BCL.
- **Application** — use cases and the interfaces they need (`IPaymentRepository`, `IPaymentNotifier`). References Domain.
- **Infrastructure** — EF Core, provider adapters, email: implementations of Application's interfaces.
- **Presentation** — controllers and webhook endpoints.
- **Composition root** — `Program.cs`, the only place that sees everything.

EF mapping moves to fluent `IEntityTypeConfiguration<Payment>` classes in Infrastructure; `ToResponse` moves to Presentation.

**What it's called.** Palermo's **onion architecture** (2008): "all code can depend on layers more central, but code cannot depend on layers further out from the core."[^onion] Martin's **clean architecture** (2012) calls it the Dependency Rule — "source code dependencies can only point inwards" — and explicitly unifies hexagonal and onion under it.[^cleanarch] Same rule, three drawings.

**When not to.** Persistence ignorance is a spectrum. EF Core supports private constructors and backing fields, but you'll still sometimes shape an entity for the mapper. That's fine; putting `[Table]` back on it isn't.

### Business logic you can only run through its framework

**The smell.** Testing refund rules means starting the web host and sending HTTP requests. The nightly batch job either calls your own API or copies the logic.

**The fix.** The expiry job from Part 1, applied everywhere. Use cases are plain classes that declare the ports they need; Infrastructure supplies adapters; controllers, jobs, consumers and tests all call the use case directly. Swapping databases comes free, but the everyday win is testing and reuse.

![Before: a use case in Application points directly at SqlPaymentRepository in Infrastructure, so the business rule depends on the database code. After: the use case points at IPaymentRepository, which also lives in Application, and SqlPaymentRepository in Infrastructure points up at the same interface to implement it. The arrow crossing the layer boundary has reversed direction](dependency-flip.svg)

**What it's called.** Cockburn's **hexagonal architecture**, or **ports and adapters** (2005): let an application "equally be driven by users, programs, automated test or batch scripts, and to be developed and tested in isolation from its eventual run-time devices and databases."[^hexagonal] Ports are interfaces the application owns; adapters plug technology into them. It's DIP and the GoF Adapter at project scale.

### Business rules in controllers, and repositories that return `IQueryable`

**The smell.** Rules in the controller:

```csharp
[HttpPost("{id:guid}/refunds")]
public async Task<IActionResult> Refund(Guid id, RefundRequest request, CancellationToken ct)
{
    var payment = await db.Payments.Include(p => p.Refunds).SingleOrDefaultAsync(p => p.Id == id, ct);
    if (payment is null) return NotFound();
    if (payment.Status is not (PaymentStatus.Completed or PaymentStatus.PartiallyRefunded)) return Conflict();
    if (request.Amount > payment.Amount - payment.Refunds.Sum(r => r.Amount)) return UnprocessableEntity();
    // ...call the provider, add the refund, save
}
```

The support tool and batch job re-implement these checks and already disagree — **Feature Envy**, a method more interested in another object's data than its own.[^refactoring] Meanwhile a repository returning `IQueryable<Payment>` spreads query logic, and EF's translation limits, into every layer (see the [LINQ post]({{< relref "linq-ef-core-and-the-sql-underneath.md" >}})).

**The fix.** Rules in the domain (`payment.Refund(amount)`, shown next), orchestration in a use case (`RefundPaymentHandler`), and a controller that only maps HTTP to a command and a result to a status code. Repositories expose intent-named methods returning materialised results.

**When not to.** On a small service, a repository over EF Core is often ceremony — EF's docs call `DbContext` "a combination of the Unit Of Work and Repository patterns."[^dbcontext] Using it directly in handlers is a fair compromise. Rules in controllers aren't.

### Refunds written behind the payment's back

**The smell.** `Refund` has its own `DbSet`, and the batch job writes rows directly:

```csharp
// BatchRefundJob
foreach (var line in file.Lines)
    db.Refunds.Add(new Refund { PaymentId = line.PaymentId, Amount = line.Amount });
await db.SaveChangesAsync(ct);
```

**Why it hurts.** "Refunds can't exceed the payment" is a rule about a payment and *all* its refunds, so anything writing a `Refund` directly can break it. Even correct code breaks it under concurrency: two agents refund the same payment at once, both see enough balance, both save.

**The fix.** Make the cluster one consistency boundary with a single entry point, loaded and saved whole:

```csharp
public sealed class Payment
{
    private readonly List<Refund> _refunds = [];
    public IReadOnlyList<Refund> Refunds => _refunds;
    public Guid Version { get; private set; } = Guid.NewGuid();   // concurrency token

    public Money RefundedTotal => _refunds.Aggregate(Money.Zero(Amount.Currency), (sum, r) => sum + r.Amount);

    public Refund Refund(Money amount)
    {
        if (!CanTransitionTo(PaymentStatus.PartiallyRefunded))
            throw new InvalidPaymentTransitionException(Id, Status, PaymentStatus.PartiallyRefunded);
        if (amount.Amount <= 0 || (RefundedTotal + amount).Amount > Amount.Amount)
            throw new RefundExceedsPaymentException(Id, amount);

        var refund = new Refund(Guid.NewGuid(), amount);
        _refunds.Add(refund);
        TransitionTo(RefundedTotal == Amount ? PaymentStatus.Refunded : PaymentStatus.PartiallyRefunded);
        Version = Guid.NewGuid();
        return refund;
    }
}
```

No `DbSet<Refund>`, no public `Refund` constructor; the batch job loops over `RefundPaymentHandler`. `Version`, configured as a concurrency token, makes EF Core's `UPDATE` include `WHERE Version = @original` and throw `DbUpdateConcurrencyException` for the second agent.[^concurrency] It's set by the application rather than a database `rowversion` because a second partial refund only *inserts* a row — without a deliberate change, the payment row wouldn't change at all.

![A dashed boundary labelled Payment aggregate contains the Payment root, holding Status, Amount and Version, and two Refund entities beneath it. RefundPaymentHandler calls Refund() on the root. BatchRefundJob's direct arrow to a Refund is crossed out, labelled db.Refunds.Add, no longer possible. Outside the boundary, Ledger is its own aggregate, reached by a PaymentCompleted event via the outbox](payment-aggregate.svg)

**What it's called.** An **aggregate**: "a cluster of domain objects that can be treated as a single unit," where "any references from outside the aggregate should only go to the aggregate root" and "transactions should not cross aggregate boundaries."[^aggregate] That last rule is why the ledger, a separate aggregate, is updated via an event and the outbox rather than in the payment's transaction.

**When not to.** Don't draw aggregates too big — make `Merchant` the root and every refund locks ten thousand payments. Keep them as small as their invariants allow, and reference other aggregates by ID.

### One model for every screen

**The smell.** The merchant dashboard loads full aggregates and maps them:

```csharp
var payments = await repository.FindForMerchantAsync(merchantId, page, ct);   // full Payments, with refunds
return payments.Select(p => new PaymentRow(
    p.Id, p.Amount.Amount, p.Amount.Currency, p.Status, p.Method, p.RefundedTotal.Amount, p.CreatedAt)).ToList();
```

Then support, finance and risk each want a screen, and the repository grows `FindForSupportSearchAsync`, `FindForStatementAsync`, `FindRecentlyRefundedAsync` — while `Payment` grows getters that exist only for the UI.

**Why it hurts.** Aggregates are shaped to protect writes: small, consistent, loaded whole. Screens want flat, joined, paged rows. Reading through the write model loads too much (the [LINQ post]({{< relref "linq-ef-core-and-the-sql-underneath.md" >}}) shows the cost) and bends the domain toward the UI.

**The fix.** Commands go through the aggregate. Queries skip the domain and project straight to the screen's shape:

```csharp
public sealed record PaymentRow(
    Guid Id, decimal Amount, string Currency, PaymentStatus Status, PaymentMethod Method, decimal Refunded, DateTimeOffset CreatedAt);

public sealed class MerchantPaymentsQuery(PaymentDbContext db)
{
    public Task<List<PaymentRow>> RunAsync(string merchantId, int page, CancellationToken ct) =>
        db.Payments.AsNoTracking()
            .Where(p => p.MerchantId == merchantId)
            .OrderByDescending(p => p.CreatedAt)
            .Skip(page * 50).Take(50)
            .Select(p => new PaymentRow(
                p.Id, p.Amount.Amount, p.Amount.Currency, p.Status, p.Method,
                p.Refunds.Sum(r => r.Amount.Amount), p.CreatedAt))
            .ToListAsync(ct);
}
```

Reads have no invariants, so the domain has nothing to do. The refunded total is a SQL `SUM`, only seven columns leave the database, and the repository shrinks back to what commands need.

**What it's called.** **CQRS**: "a different model to update information than the model you use to read information."[^cqrs] The command half has been here since the SRP section. It's a ladder, and most systems should stop at the first rung:

1. **Separate query classes, same database** — as above. Cheap.
2. **Separate read models** updated by domain events — fast, but eventually consistent.
3. **Separate stores or event sourcing** — a large commitment.

**When not to.** Fowler: "for most systems CQRS adds risky complexity," and it "should only be used on specific portions of a system."[^cqrs] Climb a rung only when the one below has demonstrably failed.

## Part 3: the smell these fixes create

Every fix above adds indirection, and indirection has to earn its place. Applied by reflex, these fixes produce their own smells:

- **An interface for every class**, one implementation each, only for mocking. Interfaces at real boundaries (I/O, the clock, other teams) earn their keep; `IPaymentFeeCalculator` doesn't.
- **Flexibility for variations that don't exist yet** — **Speculative Generality**[^refactoring], or YAGNI. Fowler lists its costs as build, delay, repair and *carry*: the complexity that makes every later change harder, even when the guess was right.[^yagni]
- **Seven projects for a CRUD service.** With no real domain logic, the onion has nothing at its centre.
- **A generic `IRepository<T>`** over EF Core that hides nothing and loses `Include`, projections and `AsNoTracking`.
- **Event sourcing for an admin screen.** CQRS's upper rungs are for workloads that need them.
- **Every table an aggregate.** DDD's tactical patterns pay off where there are rules to protect; its strategic side (language, boundaries) is useful everywhere.

The rule: **refactor toward a pattern when the duplication or variation is real.** Don Roberts's rule of three, via Fowler: do it once, duplicate it once, refactor the third time.[^refactoring] Most of Part 1's smells started as reasonable code — one switch, one provider, one export — and became smells when the second and third cases arrived. The smell is what tells you it's time.

## Cheat sheet

| Smell | What it makes expensive | Fix | Name |
|---|---|---|---|
| Same `switch` on a type code in many places | Adding a case | One implementation per case, lookup at the edge | Strategy, Open/Closed |
| Callers translating the same foreign format | Fixing the translation | Translate once at the boundary into your own type | Adapter, anti-corruption layer |
| Same type-check chain over your own closed hierarchy | Adding a subtype safely | Visitor, or a C# 15 union with an exhaustive switch | Visitor |
| Huge constructor, methods using disjoint dependencies | Testing, parallel work | Split by who requests changes | Single Responsibility |
| Subtype throws `NotSupportedException`; callers type-check | Adding implementations | Split the capability into its own interface | Liskov, Interface Segregation |
| `new SqlConnection`, `DateTime.UtcNow`, `new HttpClient` in rules | Testing the rule, changing infrastructure | Depend on interfaces the rule owns | Dependency Inversion |
| Same logging/timing/retry around every call | Changing cross-cutting behaviour | Wrap the interface | Decorator |
| Status checks and assignments scattered | Protecting the lifecycle | Private setter + transition table, or state objects | State |
| One method triggering every downstream reaction | Adding a reaction; failure isolation | Raise a domain event; handlers subscribe via an outbox | Observer |
| Long positional constructors in tests | Reading and maintaining tests | Builder with defaults | Test data builder |
| `decimal` + `string` for money | Correctness | Immutable type owning its rules | Value object |
| Copy-pasted class differing in a few steps | Keeping copies in sync | Template Method; composition once two things vary | Template Method → Strategy |
| Domain references EF and web projects | Changing infrastructure | Dependencies point inward, enforced by project references | Onion / clean architecture |
| Use cases only runnable through the web host | Testing, second drivers | Plain use-case classes; ports owned by Application | Ports and adapters |
| Rules in controllers, `IQueryable` from repositories | Second entry points | Rules in domain, orchestration in use cases | Application layer |
| Child rows written directly, bypassing their parent | Invariants under concurrency | One root as entry point; concurrency token | Aggregate |
| Screens served by loading aggregates | Read performance, domain purity | Queries project straight to DTOs | CQRS |
| Abstractions with nothing behind them | Every change | Wait for the second or third case | YAGNI, rule of three |

[^codesmell]: Martin Fowler, ["CodeSmell"](https://martinfowler.com/bliki/CodeSmell.html) — the definition, Kent Beck's coining of the term, and the point that "smells don't always indicate a problem."
[^gof]: Erich Gamma, Richard Helm, Ralph Johnson and John Vlissides, *Design Patterns: Elements of Reusable Object-Oriented Software* (Addison-Wesley, 1994) — the 23-pattern catalogue, the two principles from its introduction, and the intent statements quoted.
[^dppaper]: Robert C. Martin, *Design Principles and Design Patterns* (2000) — Open/Closed, Liskov Substitution, Dependency Inversion and Interface Segregation as class-design principles, with OCP credited to Bertrand Meyer.
[^asd]: Robert C. Martin, *Agile Software Development: Principles, Patterns, and Practices* (Prentice Hall, 2002) — a chapter on each of the five principles, including the Interface Segregation wording quoted.
[^cleanarchbook]: Robert C. Martin, *Clean Architecture: A Craftsman's Guide to Software Structure and Design* (Prentice Hall, 2017), introduction to Part III — the history of the principles and Michael Feathers's naming of SOLID.
[^oosc]: Bertrand Meyer, *Object-Oriented Software Construction* (Prentice Hall, 1988) — the Open/Closed Principle.
[^exhaustive]: Microsoft Learn, ["Resolve pattern matching errors and warnings"](https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/compiler-messages/pattern-matching-warnings) — CS8509 for a non-exhaustive switch expression, and CS8524 for unnamed enum values.
[^refactoring]: Martin Fowler, *Refactoring: Improving the Design of Existing Code*, 2nd edition (Addison-Wesley, 2018) — Repeated Switches, Shotgun Surgery, Long Parameter List, Large Class, Divergent Change, Feature Envy, Primitive Obsession, Speculative Generality; Replace Conditional with Polymorphism; and Don Roberts's rule of three.
[^keyed]: Microsoft Learn, ["Dependency injection in .NET"](https://learn.microsoft.com/en-us/dotnet/core/extensions/dependency-injection) — keyed services with `AddKeyedSingleton` and `[FromKeyedServices]`.
[^typedclients]: Microsoft Learn, ["Use the IHttpClientFactory"](https://learn.microsoft.com/en-us/dotnet/core/extensions/httpclient-factory) — typed clients as transient, and "Avoid typed clients in singleton services."
[^iso4217]: ISO, ["ISO 4217 — Currency codes"](https://www.iso.org/iso-4217-currency-codes.html) — currency codes and minor-unit digits (0 for JPY, 2 for EUR, 3 for BHD).
[^unions]: Microsoft Learn, ["Union types"](https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/builtin-types/union) — the `union` declaration, .NET 11 runtime support, exhaustive switches, and null handling.
[^srp]: Robert C. Martin, ["The Single Responsibility Principle"](https://blog.cleancoder.com/uncle-bob/2014/05/08/SingleReponsibilityPrinciple.html) (2014).
[^liskov]: Barbara H. Liskov and Jeannette M. Wing, ["A Behavioral Notion of Subtyping"](https://doi.org/10.1145/197320.197383), *ACM TOPLAS* 16(6), 1994.
[^dip]: Robert C. Martin, "The Dependency Inversion Principle," *C++ Report*, May 1996.
[^httpclient]: Microsoft Learn, ["HttpClient guidelines for .NET"](https://learn.microsoft.com/en-us/dotnet/fundamentals/networking/http/httpclient-guidelines) — per-instance connection pools, port exhaustion, and the recommended lifetimes.
[^timeprovider]: Microsoft Learn, ["What is the TimeProvider class"](https://learn.microsoft.com/en-us/dotnet/standard/datetime/timeprovider-overview) — built in from .NET 8, and `FakeTimeProvider`.
[^goos]: Steve Freeman and Nat Pryce, *Growing Object-Oriented Software, Guided by Tests* (Addison-Wesley, 2009).
[^complextypes]: Microsoft Learn, ["What's New in EF Core 8"](https://learn.microsoft.com/en-us/ef/core/what-is-new/ef-core-8.0/whatsnew) — complex types: no key, stored inline, not discovered by convention.
[^dbcontext]: Microsoft Learn, ["DbContext Class"](https://learn.microsoft.com/en-us/dotnet/api/microsoft.entityframeworkcore.dbcontext).
[^boundedcontext]: Martin Fowler, ["BoundedContext"](https://martinfowler.com/bliki/BoundedContext.html).
[^cqrs]: Martin Fowler, ["CQRS"](https://martinfowler.com/bliki/CQRS.html) — the definition, Greg Young as its source, and the warnings quoted.
[^cqs]: Martin Fowler, ["CommandQuerySeparation"](https://martinfowler.com/bliki/CommandQuerySeparation.html) — the term coined by Bertrand Meyer.
[^aggregate]: Martin Fowler, ["DDD_Aggregate"](https://martinfowler.com/bliki/DDD_Aggregate.html).
[^concurrency]: Microsoft Learn, ["Handling Concurrency Conflicts"](https://learn.microsoft.com/en-us/ef/core/saving/concurrency) — concurrency tokens, `DbUpdateConcurrencyException`, and application-managed tokens.
[^evans]: Eric Evans, *Domain-Driven Design: Tackling Complexity in the Heart of Software* (Addison-Wesley, 2003) — bounded contexts, value objects, aggregates, and the anti-corruption layer.
[^onion]: Jeffrey Palermo, ["The Onion Architecture: part 1"](https://jeffreypalermo.com/2008/07/the-onion-architecture-part-1/) (2008).
[^cleanarch]: Robert C. Martin, ["The Clean Architecture"](https://blog.cleancoder.com/uncle-bob/2012/08/13/the-clean-architecture.html) (2012).
[^hexagonal]: Alistair Cockburn, ["Hexagonal Architecture"](https://alistair.cockburn.us/hexagonal-architecture/) (2005).
[^yagni]: Martin Fowler, ["Yagni"](https://martinfowler.com/bliki/Yagni.html) — the costs of build, delay, carry and repair.
