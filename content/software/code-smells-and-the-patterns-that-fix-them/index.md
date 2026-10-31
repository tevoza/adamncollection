---
title: "Code Smells and the Patterns That Fix Them"
date: 2026-10-31T14:00:00+02:00
draft: true
tags: ["software", "architecture", "csharp", "dotnet", "design-patterns", "solid", "clean-architecture", "ai-generated"]
description: "SOLID, the Gang of Four patterns and onion architecture, learned backwards: start from a smell you'd recognise in a C# payments codebase, refactor it, and only then name the principle the fix turned out to be."
---

Most people meet design patterns the wrong way round. They read a catalogue — Strategy, Adapter, Decorator, Observer — memorise the class diagrams, and then go looking for places to put them. The result is code with patterns in it rather than code that's better for having them.

The patterns were discovered in the opposite order. Someone kept hitting the same problem, kept reaching for the same shape of fix, and eventually gave the fix a name. So this post starts where you actually are when you need one: looking at code that's become annoying to change. Each section shows a **smell** — Kent Beck's term, which Martin Fowler defines as "a surface indication that usually corresponds to a deeper problem in the system"[^codesmell] — explains what it makes expensive, refactors it, and only then says what the fix is called.

Everything happens in one codebase: a payments API with several payment methods, provider webhooks, refunds, fees and the reactions that follow a completed payment. It starts out the way real code usually starts out — written quickly, by people solving the problem in front of them — and gets refactored smell by smell. Part 1 is code-level smells, where SOLID and the classic patterns live. Part 2 is the same thinking at the scale of projects, layers and models, where clean and onion architecture, domain-driven design's aggregates and CQRS live. Part 3 is the smell that the first two parts produce when overapplied.

Like the rest of the software posts, this one is AI-written, so its specific claims — what a principle's author actually said, what a framework API does — are footnoted against primary sources. Where a statement is reasoning built on those sources rather than something they say, the wording says so.

## The vocabulary, briefly

Several bodies of work overlap here, and it helps to know which is which.

**The Gang of Four** is the four authors of *Design Patterns: Elements of Reusable Object-Oriented Software* (1994) — Erich Gamma, Richard Helm, Ralph Johnson and John Vlissides.[^gof] The book catalogues 23 patterns in three groups: **creational** (how objects get made: Builder, Factory Method, Abstract Factory, Prototype, Singleton), **structural** (how objects are composed: Adapter, Decorator, Composite, Facade, Proxy, Bridge, Flyweight) and **behavioural** (how objects divide up work: Strategy, State, Observer, Template Method, Visitor, Command, Chain of Responsibility, and others). Underneath the catalogue are two principles the book states up front, and which matter more than any single pattern: *program to an interface, not an implementation*, and *favour object composition over class inheritance*.

**SOLID** is five class-design principles — Single Responsibility, Open/Closed, Liskov Substitution, Interface Segregation, Dependency Inversion — that Robert C. Martin set out in a series of articles in the 1990s. Four of them appear together in his 2000 paper *Design Principles and Design Patterns*,[^dppaper] and all five in his 2002 book *Agile Software Development*.[^asd] The acronym came later. Martin credits Michael Feathers, who around 2004 noticed that a reordering of the principles spelled SOLID.[^cleanarchbook] Two of the principles are older than Martin's versions: Open/Closed is Bertrand Meyer's, from 1988,[^oosc] and Liskov Substitution is Barbara Liskov's. They're less a catalogue of fixes than a set of tests for whether a design will be cheap to change.

**Clean, onion and hexagonal architecture** are the same idea applied to a whole application: arrange the code so that business rules don't depend on databases, frameworks or the web.

**Domain-driven design** comes from Eric Evans's 2003 book of that name.[^evans] It's about what goes *inside* that protected centre, and it works at two levels. The strategic level is about language and boundaries. A **ubiquitous language** is shared by developers and domain experts. A **bounded context** is the region where one model and its language hold, and Fowler's summary is that "you need a different model when the language changes."[^boundedcontext] The tactical level is a set of building blocks: entities, **value objects**, **aggregates**, **domain events**, repositories. Several of them turn up below as fixes, without being announced as DDD until they arrive.

**CQRS** — Command Query Responsibility Segregation — is Greg Young's name for using one model to change data and a different one to read it.[^cqrs] It grew out of Bertrand Meyer's older command–query separation: a method should either change state or return a result, not both.[^cqs]

One thing has changed since 1994. Several GoF patterns have since become language features, and in C# you'll often use the pattern without writing the classes. A Strategy can be a `Func<decimal, decimal>`. Observer is the `event` keyword. Iterator is `IEnumerable<T>` and `yield return`. Knowing the pattern still helps — it tells you *why* the feature exists and when to reach for it — but the class diagram in the book is frequently the long way round.

## Part 1: smells in the code

### The same switch, in six places

**The smell.** The payment service supports three methods, and every operation that differs between them switches on which one it's dealing with:

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

**Why it hurts.** A single `switch` is fine. The smell is the *same* switch, on the same discriminator, repeated — Fowler's second edition of *Refactoring* renamed the old "Switch Statements" smell to "Repeated Switches" for exactly this reason.[^refactoring] Adding a fourth payment method means finding every one of them. The compiler would help a little: it warns when a switch *expression* doesn't handle every value of its input (CS8509), and separately about unnamed enum values like `(PaymentMethod)42` (CS8524).[^exhaustive] But that second warning pushes everyone to add a `_ => throw` arm, which switches off the first one too. The `switch` *statement* never had the check. And nothing at all finds the switches in other classes. Miss one and you find out in production, when the new method falls into an arm that throws.

There's a second cost that's easier to miss: everything about the card method — its client, its fee, its settlement rule, its refund behaviour — is spread across six places in three classes, each of which also knows about bank transfers and wallets. There's no one place to read to understand "how card payments work."

**The fix.** Turn the thing being switched on into a type. Each case becomes an implementation of one interface, and the knowledge that was scattered across six switches collects into one class per method:

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

The caller still has to pick the right one. Register all of them, and let something that receives the whole set build a lookup once:

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

`ToDictionary` throws on a duplicate key, so two providers claiming the same method fail loudly rather than silently shadowing each other. A singleton is only constructed the first time something resolves it, though, so to make that failure happen at startup instead of on the first request, resolve it once right after building the app: `app.Services.GetRequiredService<PaymentProviders>();`. And `ChargeAsync` in the service becomes one line: `providers.For(payment.Method).ChargeAsync(payment, ct)`.

Because these providers are singletons, they also have to obey the captive-dependency rules from the [DI post]({{< relref "di-lifetimes-and-failure-boundaries.md" >}}): nothing shorter-lived inside them. That includes typed `HttpClient`s. If `CardApiClient` is registered with `AddHttpClient<CardApiClient>()`, it's transient, and Microsoft's guidance is explicit that capturing one in a singleton stops it reacting to DNS changes. Either inject `IHttpClientFactory` and create clients as needed, or give the typed client a primary handler with `PooledConnectionLifetime` set.[^typedclients]

`PaymentProviders` is a small **registry**, sometimes called a simple factory: the one object that knows how to go from a key to an implementation. It isn't one of the GoF's 23. Their Factory Method and Abstract Factory solve the related problem of *creating* objects (Factory Method) or families of related objects (Abstract Factory) without naming concrete classes, which a DI container now does for most applications.

.NET 8's DI container can also do this lookup itself with **keyed services** — `AddKeyedSingleton<IPaymentProvider, CardProvider>(PaymentMethod.Card)`, resolved with a `[FromKeyedServices]` attribute on a constructor parameter.[^keyed] That's a good fit when the key is known at compile time. Here it isn't — it comes from the payment being processed — so you'd have to call `GetRequiredKeyedService` on an injected `IServiceProvider` from inside business code. The [DI post]({{< relref "di-lifetimes-and-failure-boundaries.md" >}}) allows that for genuinely dynamic resolution, but the explicit dictionary is better here. It keeps `IServiceProvider` out of business code, and it makes the set of supported methods something you can enumerate and test.

Notice that the switch didn't vanish. It moved — into the three registration lines, which are now the only place in the system that enumerates payment methods. That's the actual win: one place, at the edge, instead of six places in the middle.

![Before: PaymentService has a switch on Method in ChargeAsync, CalculateFee, SettlementDelay and RefundAsync, and WebhookHandler and ReconciliationJob each have another. After: Program.cs registers CardProvider, BankTransferProvider and WalletProvider, PaymentProviders.For() looks one up, and each provider class holds its own charge, fee and settlement logic](switch-to-strategy.svg)

**What it's called.** Turning a type code into polymorphism is the refactoring Fowler calls *Replace Conditional with Polymorphism*.[^refactoring] Each provider is a **Strategy** — "define a family of algorithms, encapsulate each one, and make them interchangeable"[^gof] — and the resulting design satisfies the **Open/Closed Principle**: open for extension (add a provider class and a registration line), closed for modification (no existing class changes).

**When not to.** Two cases. If the cases differ only in *data* — three fee rates that are just numbers — then classes are the wrong tool; a table or configuration does it with less code. And if the set of *types* is stable but the set of *operations* keeps growing, polymorphism is the wrong way round: every new operation means editing every class. That trade-off has a name (the expression problem), and the other side of it comes up in the next section.

### Every caller translating the same foreign payload

**The smell.** Each provider sends webhooks and offers a list API, in its own format: amounts in minor units, lowercase currency codes, its own status strings. The code that consumes them translates as it goes — in the webhook controller, in the nightly reconciliation job, and in the support tool that looks up a payment:

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

**Why it hurts.** Look at `/ 100m`. It's wrong for Japanese yen, which has no minor unit, and wrong for currencies like the Bahraini dinar, which has three.[^iso4217] When someone discovers that, the fix has to be made in every place that converts — and the reconciliation job, written by a different person, already maps `"canceled"` to `Failed` while the webhook handler ignores it. Fowler calls this **Shotgun Surgery**: one change of understanding, many edits.[^refactoring] Worse, the provider's vocabulary (`"charge.succeeded"`, minor units, its spelling of "canceled") has leaked into code that should only ever think in your terms.

**The fix.** Translate once, at the boundary, into a type you own. Everything inside the system sees only that type:

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
            hook.Data.AmountMinor / CurrencyInfo.MinorUnitFactor(currency),
            currency,
            DateTimeOffset.FromUnixTimeSeconds(hook.Created));
    }
}
```

The status mapping is still a switch — but it's the *only* one, and it's switching on the provider's strings, which is exactly the job of a translator. The webhook controller, the reconciliation job and the support tool all consume `PaymentEvent`. `CurrencyInfo` is a lookup of each currency's ISO 4217 minor units (0 for yen, 2 for euros, 3 for dinars) and the matching factor (1, 100, 1000). It replaces the hard-coded `100m`, and it's used here, once.

![Three provider payloads on the left, each in its own vocabulary: a card webhook with type charge.succeeded, an amount of 1999 in minor units and a lowercase currency; a bank status report with TxSts ACSC and InstdAmt 19.99; a wallet event with state PAID and amount_cents 1999. Each passes through its own translator inside a dashed boundary and comes out as a PaymentEvent with PaymentId, Kind, Amount, Currency and OccurredAt, which the webhooks endpoint, reconciliation job and support tool consume](translation-boundary.svg)

Look back at `CardProvider` from the previous section and it's the same idea applied to behaviour rather than data: it wraps the provider's SDK (`CardApiClient`, with its `AuthoriseAndCaptureAsync` and its `Approved` flag) behind an interface shaped the way *our* code wants to call it.

**What it's called.** `CardProvider` is a textbook **Adapter**: "convert the interface of a class into another interface clients expect."[^gof] The translator applies the same intent to incoming data. A boundary made of adapters and translators, whose job is to stop an external model leaking into yours, is what Eric Evans's *Domain-Driven Design* calls an **anti-corruption layer**.[^evans]

There's no Gang of Four pattern called "reader" or "mapper", though you'll hear both words used for this. What matters is which way the types run:

- **If you own the types** and keep converting them to a common base shape, don't convert at all — make them implement a shared interface (the previous section).
- **If you don't own them** — a provider's payload, a library's class — adapt them once at the boundary, as above.
- **If you own a closed set of types** and keep adding *operations* over them, that's the other side of the expression problem from the previous section.

That last case has its own smell. Say the provider call's result is modelled as a small hierarchy, `PaymentOutcome`, with `Captured` and `Declined` subtypes. The ledger code, the notification code and the analytics code each grow their own `if (outcome is Captured c) … else if (outcome is Declined d) …` chain. Then someone adds `PendingReview`, and the analytics chain silently drops it. The GoF answer is **Visitor**: "represent an operation to be performed on the elements of an object structure. Visitor lets you define a new operation without changing the classes of the elements on which it operates."[^gof] Each operation becomes a visitor class with one `Visit` method per subtype, so adding a subtype forces every visitor to handle it.

In modern C#, Visitor's double dispatch is usually replaced by pattern matching over a record hierarchy:

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

The cost is the `_` arm. The compiler can't tell that a record hierarchy is closed, so adding a third outcome compiles fine and fails at runtime. A real Visitor — an interface with one `Visit` method per type — turns that into a compile error in every visitor. Whether that's worth the ceremony depends on how many operations there are and how often the hierarchy changes.

C# 15, arriving with .NET 11 in November 2026, closes most of that gap with **union types**. `Captured` and `Declined` become plain records that don't derive from anything, and `public union PaymentOutcome(Captured, Declined);` replaces the abstract base. The compiler then checks that a `switch` over the union covers every case type, with no discard needed.[^unions] The remaining gap is that a default-initialised union holds `null`, so the documentation recommends a `null` arm, which unlike `_` still lets the compiler flag a newly added case. Where they're available, a union plus a switch expression gives you Visitor's safety without its classes.

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

**Why it hurts.** The parameter count is the symptom, not the problem. The problem shows up when you draw a grid of methods against the dependencies each one uses: `RenderReceiptAsync` touches `db`, `pdf` and `blobs`; `CreateAsync` touches `db`, `providers`, `fraud` and `fx`; the statement touches `db` and `fx`. Nothing touches all eleven. The class is several classes sharing a constructor.

The practical costs follow from that. Testing a refund means constructing nine collaborators the refund never calls. Two people changing unrelated features — finance's statement format and the risk team's fraud rules — edit the same file and collide in review. And because the class is where "payment stuff" goes, every new payment-adjacent feature lands here and makes it worse. Fowler has names for each layer of this. Long Parameter List and Large Class are the symptoms. **Divergent Change** — one class, changed for many different reasons — is the cause.[^refactoring]

**The fix.** Split along the clusters the grid reveals, and name each piece after the job it does:

```csharp
public sealed class CreatePaymentHandler(PaymentDbContext db, PaymentProviders providers, IFraudScreening fraud, IFxRates fx) { ... }
public sealed class RefundPaymentHandler(PaymentDbContext db, PaymentProviders providers) { ... }
public sealed class ProviderEventHandler(PaymentDbContext db, ILedger ledger, IEmailSender email, ISmsSender sms, IAnalytics analytics) { ... }
public sealed class ReceiptRenderer(PaymentDbContext db, IPdfRenderer pdf, IBlobStorage blobs) { ... }
public sealed class MerchantStatements(PaymentDbContext db, IFxRates fx) { ... }
```

`ProviderEventHandler` still has a long list, for now. Completing a payment triggers the ledger, the receipt email, an SMS and analytics inline, and that's a separate smell with its own fix, a few sections down.

The handler shape — a `CreatePayment` object describing the request, and a class whose only job is to carry it out — is a lightweight form of the GoF **Command** pattern ("encapsulate a request as an object"), and it's the shape CQRS-style code and libraries like MediatR are built around.[^gof] Having one class per use case is also what makes the split easy to see.

**What it's called.** The **Single Responsibility Principle**, which is widely misquoted as "a class should do one thing." Martin's own statement is that "each software module should have one and only one reason to change," and he is explicit that reasons are *people*: "it is people who request changes."[^srp] His alternative wording is the more useful one in practice: "Gather together the things that change for the same reasons. Separate those things that change for different reasons." Finance owns the statement, support owns the receipt, risk owns fraud screening. Those are the seams.

**When not to.** SRP read as "small" produces the opposite smell: five one-method classes that always change together, so every feature is a five-file diff. If two pieces of code are always edited in the same commit by the same people for the same reason, they belong together however big that makes the class. The test is who asks for the change, not how many lines there are.

### A subtype that throws `NotSupportedException`

**The smell.** `RefundAsync` moves onto the provider interface, as the first section's refactoring implied it would. Card and wallet refunds go back through the provider's API, but bank transfers can't be reversed by the bank — a refund has to be sent as a new outbound payment. So:

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

And within a sprint, the callers that learned the hard way look like this:

```csharp
if (payment.Method != PaymentMethod.BankTransfer)
    await providers.For(payment.Method).RefundAsync(payment, amount, ct);
else
    await payouts.SendRefundPayoutAsync(payment, amount, ct);
```

**Why it hurts.** That `if` is the switch from the first section, back again. The interface promised that every `IPaymentProvider` can refund; one of them can't, so every caller has to know which concrete type it's holding — which is the thing the interface existed to hide. Add a second non-refundable provider and every one of those checks is now wrong.

**What it's called.** This is a violation of the **Liskov Substitution Principle**. Barbara Liskov and Jeannette Wing's formulation is that if some property is provable about objects of type T, it "should be true for objects y of type S where S is a subtype of T."[^liskov] In plainer terms: code written against the interface must keep working with any implementation of it. Note that this is about *behaviour*, which is why the compiler can't check it. `BankTransferProvider` has the right method signature. What it breaks is the contract: it throws where the others refund. The same goes for an implementation that demands more of its inputs (a provider that rejects amounts over 1,000 when the interface implies any amount works) or promises less about its results.

**The fix.** Make the interface tell the truth. If not every provider can refund, refunding isn't part of being a provider — it's a separate capability:

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

The refund handler depends on a lookup of `IRefundProvider`s, built the same way as `PaymentProviders`. When there's no entry for a method, that's no longer a crash — it's a business decision with its own code path, the outbound refund payout, and it lives in one place. A new non-refundable provider simply doesn't implement `IRefundProvider`, and no caller has to change.

One wiring detail: a class that implements two interfaces needs both registered against the *same* instance. Otherwise you get two card providers.

```csharp
builder.Services.AddSingleton<CardProvider>();
builder.Services.AddSingleton<IPaymentProvider>(sp => sp.GetRequiredService<CardProvider>());
builder.Services.AddSingleton<IRefundProvider>(sp => sp.GetRequiredService<CardProvider>());
```

Splitting an interface by what its clients actually need is the **Interface Segregation Principle**. Martin's statement is that "clients should not be forced to depend upon interfaces that they do not use."[^asd] The split here helps from both directions. The charge path no longer depends on a `RefundAsync` it never calls, and `BankTransferProvider` no longer has to fake one. LSP tells you something's wrong. ISP is usually how you fix it.

**When not to.** `NotSupportedException` isn't always a smell. .NET's own `Stream` throws it from `Seek` on a network stream — but `Stream` also has `CanSeek`, so the interface *advertises* that the capability is optional and callers are expected to check. An optional capability that's declared up front is part of the contract. One that only shows up as an exception isn't.

### `new` in the middle of business logic

**The smell.** A background job expires payments that have sat in `Pending` for more than thirty minutes:

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

**Why it hurts.** The interesting part of this class is one business rule: *a payment pending for more than thirty minutes expires, and someone is told*. To test that rule you need a real SQL Server, a real notification endpoint, and either thirty minutes of patience or rows with doctored timestamps. The rule is welded to its plumbing.

The plumbing has its own problems too. Creating and disposing an `HttpClient` per call is a documented way to exhaust ports under load, because each instance has its own connection pool and closed TCP connections linger in `TIME_WAIT`.[^httpclient] And the connection string is hard-coded in the one place nobody will think to look when the database moves.

**The fix.** Make the job depend on things it owns, described in its own vocabulary, and let something outside the job decide what they really are:

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

The test for the thirty-minute rule now runs in a millisecond, with an in-memory repository and a fake clock. `TimeProvider` is .NET 8's built-in abstraction over the clock; production uses `TimeProvider.System`, and the `Microsoft.Extensions.TimeProvider.Testing` package supplies a `FakeTimeProvider` you can set and advance by hand.[^timeprovider] The repository is implemented with EF Core or Dapper somewhere else. The notifier is implemented with a typed `HttpClient` from `IHttpClientFactory`, which is one of the two lifetimes Microsoft recommends.[^httpclient] (A job like this usually runs inside a `BackgroundService`, which is a singleton, so it should create a DI scope per run and resolve the repository and notifier from it, rather than holding them for the life of the process.)

(The job still saves and *then* notifies, so a notifier failure after the save leaves payments expired but unannounced. That's the same commit-then-react gap the Observer section deals with below.)

**What it's called.** The **Dependency Inversion Principle**, in Martin's original wording: "High level modules should not depend upon low level modules. Both should depend upon abstractions," and "Abstractions should not depend upon details. Details should depend upon abstractions."[^dip]

The word that matters is *inversion*, and it's often lost. Before, the expiry rule depended on SQL Server. After, the SQL code depends on an interface defined *by the expiry rule's side*, in its terms: `FindPendingCreatedBeforeAsync`, not `ExecuteQuery`. Ownership of the abstraction has moved to the high-level code. That's also why **dependency injection is not dependency inversion**: injecting a `SqlConnection` through the constructor is DI, but the rule still depends directly on SQL Server. The inversion comes from *who owns the interface*, not from how the object arrives.

**When not to.** Not every `new` is a smell. `new List<Payment>()`, `new PaymentEvent(...)`, a value object — these are values with no I/O and no environment, and wrapping them behind interfaces buys nothing. The candidates are things that touch the outside world or the environment: the network, the disk, the database, the clock, randomness, configuration.

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

`BankTransferProvider` has it too, except someone forgot the metrics. `WalletProvider` has it, plus a hand-rolled retry loop.

**Why it hurts.** Logging, timing and retrying have nothing to do with how a card is charged, but they're now three-quarters of every provider class. They drift — one provider forgot metrics, one retries and the others don't — and changing the log format is an edit to every provider. Each class has picked up a second and third reason to change.

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

`CardProvider` goes back to containing only card logic, and every provider gets identical instrumentation by being wrapped at registration. The built-in container has no first-class decoration support, so you either wrap in a factory registration or use a library such as Scrutor that adds a `Decorate` extension:

```csharp
builder.Services.AddSingleton<CardProvider>();
builder.Services.AddSingleton<IPaymentProvider>(sp => new InstrumentedProvider(
    sp.GetRequiredService<CardProvider>(),
    sp.GetRequiredService<ILogger<InstrumentedProvider>>(),
    sp.GetRequiredService<PaymentMetrics>()));
```

A decorator wraps one interface. The `IRefundProvider` registration from the Liskov section still points at the bare `CardProvider`, so refunds get no instrumentation until they have a decorator of their own. If that's starting to look like a lot of near-identical wrapper classes, it's a sign the cross-cutting work belongs at a lower level — the HTTP pipeline, below.

![A ChargeAsync call enters the outermost InstrumentedProvider, which logs and starts a timer, then calls the inner CardProvider, which calls the provider's card API. The result returns back out through each layer, and InstrumentedProvider records the duration on the way out. Both providers implement IPaymentProvider](decorator-chain.svg)

**What it's called.** A **Decorator**: "attach additional responsibilities to an object dynamically. Decorators provide a flexible alternative to subclassing for extending functionality."[^gof] The same shape is all over .NET. `DelegatingHandler`s in an `HttpClient` pipeline each wrap the next handler, and so do ASP.NET Core middleware and MediatR pipeline behaviours. All of them can also short-circuit, so mechanically they sit between Decorator and its GoF neighbour **Chain of Responsibility**. The distinction is intent. A decorator adds behaviour around a call that normally goes through. A chain passes a request along until one handler takes responsibility for it.

**When not to — and a warning about retry.** A retrying decorator is the obvious next step, and in a payments system it's the dangerous one. Retrying a charge that timed out is only safe if the provider can recognise the second attempt as the same charge. Otherwise a timeout after the provider actually processed the request turns into a double charge. A retry decorator belongs only around calls that carry an idempotency key, which is a whole topic in the [API post]({{< relref "designing-an-api-that-survives-failure.md" >}}). More generally: decorators are invisible at the call site, which is the point, and also means nobody reading `CardProvider` knows it's being retried. Keep the wrapping in one registration block that's easy to find.

### Status checks scattered everywhere

**The smell.** `Payment` has a public `Status` property, and the rules about it are wherever someone needed them:

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

**Why it hurts.** Those three snippets each encode part of the payment lifecycle, and nobody can see all of it at once. The last one contains a real bug. Providers deliver webhooks late and out of order, so a "completed" event can arrive *after* the payment has already been refunded. This handler flips it back to `Completed`, and a refunded payment now looks refundable a second time. Nothing in the type stops any code anywhere from assigning any status.

**The fix.** First step, which is enough for most systems: take the setter away, and make every transition a method on `Payment` that checks it against one explicit table:

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

Now the whole lifecycle is readable in one table, the controller asks `payment.CanCancel` instead of re-deriving it, and the late webhook can't corrupt anything. (`Authorised → Expired` is there because card authorisations lapse if they aren't captured in time.)

What the event handler does with a rejected transition depends on *why* it was rejected, and there are two cases:

- **Stale or duplicate.** "Completed" arrives for a payment that's already completed, or already refunded. The payment has moved past that event. Acknowledge it and do nothing.
- **Conflict.** "Completed" arrives for a payment our side has already marked expired or cancelled. The provider says money moved, and our record says it didn't. Acknowledge it, so the provider stops redelivering, but record it and alert, because someone (or an automatic refund) has to reconcile it. Silently swallowing this one loses a customer's money. And the expiry job from the DIP section creates exactly this race.

Either way, the handler acknowledges rather than failing, because a webhook that returns an error gets redelivered.

![The transition table drawn as a state diagram. Pending goes to Authorised, Completed, Failed, Expired or Cancelled; Authorised goes to Completed, Cancelled or Expired; Completed goes to PartiallyRefunded or Refunded; PartiallyRefunded loops to itself or goes to Refunded. A dashed red arrow from Refunded back to Completed, labelled as a late completed webhook, is rejected](payment-states.svg)

**What it's called.** The GoF **State** pattern goes one step further: "allow an object to alter its behavior when its internal state changes. The object will appear to change its class."[^gof] Each state becomes an object, and the payment delegates to its current state:

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

That's worth it when behaviour, not just permission, differs a lot by state — when what `Refund` *does* in one state looks nothing like what it does in another. When the difference is mostly "allowed or not", the table is less code, easier to read, and much easier to persist. EF Core can store an enum column directly. It can't store an object graph of states without help.

### One method that does everything that should happen next

**The smell.** With the eleven-parameter constructor split up, completing a payment still looks like this:

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

**Why it hurts.** Every new reaction to a completed payment is an edit to this method — and payments complete from three places (the webhook, the reconciliation job, and a manual action in the support tool). The loyalty change only went into one of them. The failure behaviour is also wrong. If the email service is down, `SendReceiptAsync` throws *after* the payment was committed, so the caller sees a failure for something that succeeded, and the ledger posting for the next payment in the batch never happens.

**The fix.** Have the payment announce what happened, and let the reactions subscribe:

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

Every path that completes a payment goes through `MarkCompleted`, so every path raises the event. Adding loyalty points is a new handler class and no edit to anything that exists.

How the events get dispatched is the decision that matters. Dispatching them in-process right after `SaveChangesAsync` fixes the coupling but not the failure behaviour: a crash between the commit and the dispatch loses them. For anything that must happen — the ledger posting, certainly — write the events to an **outbox** table in the same transaction as the payment, and have a separate process publish them, as described in the [API post]({{< relref "designing-an-api-that-survives-failure.md" >}}). Handlers then run with retries, and must be idempotent, because at-least-once delivery means sometimes twice.

**What it's called.** The intent is **Observer**'s: "define a one-to-many dependency between objects so that when one object changes state, all its dependents are notified and updated automatically."[^gof] The mechanics differ slightly. In GoF Observer the subject keeps a list of its observers. Here the payment knows nothing about who's listening, and a dispatcher routes events to handlers by type, which is **publish–subscribe**. C#'s `event` keyword and `IObservable<T>` are Observer built into the platform, but they're the wrong tools for domain events. They're in-memory, the publisher holds strong references to its subscribers, and nothing survives a restart.

**When not to.** Events trade explicit control flow for decoupling, and the trade is real. "Where does the receipt email come from?" used to be answered by reading one method. Now it's answered by searching for handlers of `PaymentCompleted`. Use events for reactions that are genuinely separate concerns, owned by other people. If something is part of completing a payment — the payment can't be considered complete without it — call it directly.

### Tests that build objects with nine positional arguments

**The smell.**

```csharp
var payment = new Payment(Guid.NewGuid(), Guid.NewGuid(), 100m, "EUR", PaymentMethod.Card, null, null, false, DateTimeOffset.UtcNow);
```

There are two hundred of these across the test suite. Most tests care about one or two of the values, and readers can't tell which. What's the first `null`? What's the `false`? Add a constructor parameter and two hundred tests stop compiling, none of them because of anything they were testing.

Since the State section, there's a second problem: a test that needs a refunded payment can't get one by passing a status to a constructor, because `Status` has no public setter any more.

**The fix.** A test data builder with sensible defaults, so each test states only what it cares about:

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

The builder reaches the refunded state by replaying the real `MarkCompleted` and `Refund` methods in `Build()`, so every test object has been through legal transitions, whatever order the test called the builder methods in. A test can't accidentally construct a payment the domain would never allow.

**What it's called.** **Builder**, or more precisely a descendant of it: this is the *test data builder*, named by Nat Pryce and popularised in Freeman and Pryce's *Growing Object-Oriented Software, Guided by Tests*.[^goos] The GoF book's intent is to "separate the construction of a complex object from its representation so that the same construction process can create different representations,"[^gof] which is about one construction process feeding several output formats. The test data builder borrows the shape (step-by-step construction, then `Build()`) for a different reason: readable defaults.

**When not to.** In production code, C# has taken most of the work away. Object initialisers with `required` properties (C# 11) give you named, checked, any-order construction without a builder class. Reach for a builder when construction has steps or rules that a property list can't express.

### `decimal amount, string currency`, everywhere

**The smell.** Every code sample so far has passed money around as a `decimal` with, sometimes, a `string` currency next to it. That has consequences:

```csharp
public decimal CalculateFee(decimal amount) => amount * 0.029m + 0.30m;   // 0.30 of what?

var total = payment.Amount + fee;   // payment is in GBP; the fee table is in EUR

if (amount <= 0) throw new ArgumentException(...);              // in CreatePaymentHandler
if (amount <= 0 || amount > payment.Amount) throw ...;          // in RefundPaymentHandler
if (currency.Length != 3) throw ...;                            // in two of the five places that take a currency
```

**Why it hurts.** A `decimal` can't know its currency, so adding pounds to euros compiles and runs. The rules about what counts as a valid amount — a real currency code, the currency's own precision, so no fractional yen — are re-checked, slightly differently, wherever an amount enters the system, and missed wherever someone forgot. Fowler's name for this is **Primitive Obsession**: using a language primitive for a concept that has rules of its own.[^refactoring]

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
        return new(minor / CurrencyInfo.MinorUnitFactor(code), code);   // 1, 100 or 1000, as decimal
    }

    public Money MultiplyRounded(decimal factor) =>
        new(decimal.Round(Amount * factor, CurrencyInfo.MinorUnits(Currency), MidpointRounding.AwayFromZero), Currency);

    public static Money operator +(Money a, Money b) =>
        a.Currency == b.Currency
            ? new Money(a.Amount + b.Amount, a.Currency)
            : throw new CurrencyMismatchException(a.Currency, b.Currency);
}
```

The properties are get-only rather than `init`, so a `with` expression can't sneak an invalid value past the constructor. `CurrencyInfo` stays what it was in the adapter section, a lookup of each currency's minor units, but the conversion moves onto the type it produces. The translator's helper becomes `Money.FromMinorUnits`, and `PaymentEvent` carries a `Money` instead of a `decimal` and a `string`. The same goes for every other amount in the system: `Payment.Amount`, `Refund(...)` and `RefundAsync(...)`, `PaymentCompleted.Amount`. From here on, the code samples use `Money`.

`IPaymentProvider.CalculateFee` becomes `Money CalculateFee(Money amount)`, and immediately something useful happens. A 2.9% fee on €19.99 is €0.57971, and that's a value the constructor rejects, so the code has to say how fees round. That's a business rule (round half away from zero? always round up, in the provider's favour?), and it now has exactly one home, in `MultiplyRounded`, instead of whatever each caller did. Meanwhile the GBP-plus-EUR line throws at the first test instead of at month-end reconciliation.

Notice what *isn't* in `Money`: "must be positive." A fee adjustment or a chargeback can be negative. "A charge must be more than zero" and "a refund can't exceed what's left" are rules of particular use cases, and they stay in the handler and in `Payment.Refund`. A value object should enforce what's true of the value everywhere, and nothing more.

**What it's called.** A **value object**, from domain-driven design rather than the Gang of Four:[^evans] an object defined entirely by its values, with no identity, that guarantees its own invariants. Of everything here, it's probably the best return on effort. It's cheap and it's local. Some bugs stop compiling, like passing a bare `decimal` where a `Money` is expected. The rest, like mixed currencies or too many decimal places, fail where the value is created instead of far downstream.

EF Core 8 added **complex types** for exactly this kind of object: they have no key and are stored inline as columns of the owning table.[^complextypes] They aren't discovered by convention, and a get-only property like `Amount` isn't mapped automatically either, so the mapping is configured explicitly (in Infrastructure, as Part 2 describes). Owned types also work, on older versions.

### Two classes that are the same except for two steps

**The smell.** At the end of each day, settled payments are exported to each bank as a file. Bank A's export was written first. Bank B's was copied from it and changed:

```csharp
public sealed class BankASettlementExport { public async Task RunAsync(DateOnly day, CancellationToken ct) { /* load, format as CSV, sign, SFTP */ } }
public sealed class BankBSettlementExport { public async Task RunAsync(DateOnly day, CancellationToken ct) { /* load, format as XML, sign, HTTPS */ } }
```

Three months later, Bank A's export excludes refunded payments, because someone fixed a bug in it. Bank B's doesn't, because nobody remembered the copy existed.

**The fix, first version.** Pull the shared sequence up into a base class and let subclasses fill in the steps that differ:

```csharp
public abstract class SettlementExport(IPaymentRepository payments, IFileSigner signer)
{
    public async Task RunAsync(DateOnly day, CancellationToken ct)
    {
        var settled = await payments.FindSettledOnAsync(day, ct);   // new on IPaymentRepository; excludes refunded, in one place
        var file = Format(settled);
        var signed = signer.Sign(file);
        await UploadAsync(signed, ct);
    }

    protected abstract byte[] Format(IReadOnlyList<Payment> settled);
    protected abstract Task UploadAsync(byte[] file, CancellationToken ct);
}
```

That's **Template Method**: "define the skeleton of an algorithm in an operation, deferring some steps to subclasses."[^gof] It fixes the drift. The shared steps exist once.

**The fix, second version.** Then Bank C arrives. It wants Bank A's CSV format and Bank B's HTTPS upload. With inheritance there's no clean way to combine one subclass's `Format` with another's `UploadAsync`. You end up with a hierarchy of intermediate base classes, or copying again. The steps that vary are really two independent choices, and inheritance forces you to pick them together.

Make each varying step a collaborator instead of an override:

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

Each step is now a Strategy, and the combinations are free. This is the GoF book's own advice — favour composition over inheritance — showing why it's there. Template Method is fine while there's one axis of variation. The moment there are two, composition wins. The book has a pattern for that general problem, too. Two dimensions that need to vary independently, without a subclass for every combination, is what **Bridge** exists for: "decouple an abstraction from its implementation so that the two can vary independently."[^gof]

## Part 2: smells in the architecture

Everything in Part 1 happens inside a class or between a few classes. The same smells exist one level up — between projects and layers, and between the model that changes data and the screens that read it — and the fixes are the same ideas at a larger scale.

### A domain that knows about the database and the web

**The smell.** The `Payment` entity lives in a project called `Payments.Domain`, and looks like this:

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

And `Payments.Domain.csproj` references `Microsoft.EntityFrameworkCore` and the API contracts project.

**Why it hurts.** The most important rules in the system — what a payment is, which transitions are legal, how money behaves — now depend on the persistence framework and on the shape of an HTTP response. Upgrade EF Core and the domain project changes. Rename a field in the public API and the domain project changes. The domain can't be tested, reused or even compiled without dragging the database and web layers along, and nothing stops the next developer from injecting a `DbContext` into an entity method, because the reference is already there.

**The fix.** Make the dependency direction a rule, and arrange the projects so the compiler enforces it:

![Three concentric rings. At the centre, Domain: entities, value objects, domain events. Around it, Application: use cases and the ports they own, such as IPaymentRepository and IPaymentNotifier. The outer ring holds Infrastructure (EF Core DbContext, provider adapters, email) on one side and Presentation (API controllers, webhooks endpoints) on the other. Arrows from every outer ring point inward; none point outward. Program.cs, the composition root, sits outside all of them and references everything](onion-layers.svg)

- **Domain** — entities, value objects, domain events. References nothing but the base class library.
- **Application** — use cases (`RefundPaymentHandler`, `PaymentExpiryJob`) and the interfaces they need from the outside world (`IPaymentRepository`, `IPaymentNotifier`). References Domain.
- **Infrastructure** — EF Core, the provider adapters, the email sender: implementations of Application's interfaces. References Application.
- **Presentation** — controllers and webhook endpoints. References Application.
- **The composition root** — `Program.cs`, which references everything and wires implementations to interfaces. It's the only place that knows the whole picture.

EF mapping moves to `IEntityTypeConfiguration<Payment>` classes in Infrastructure, using the fluent API instead of attributes. `ToResponse` moves to Presentation, where the response type lives.

**What it's called.** Jeffrey Palermo named this the **onion architecture** in 2008. Its central rule is that "all code can depend on layers more central, but code cannot depend on layers further out from the core."[^onion] Robert Martin's **clean architecture** (2012) states the same thing as "the Dependency Rule": "source code dependencies can only point inwards."[^cleanarch] Martin's post is explicit that his diagram is an attempt at "integrating all these architectures into a single actionable idea," and lists hexagonal and onion among them. These aren't competing designs. They're the same rule drawn three ways.

**When not to — or not fully.** Perfect persistence ignorance is a spectrum, and EF Core sits close to the good end without reaching it. It can use private constructors and backing fields, so the domain's public surface stays clean, but you'll still occasionally shape an entity for the mapper's sake. That's usually an acceptable price. Putting `[Table]` attributes back on the entity to save writing a configuration class usually isn't.

### Business logic you can only run through its framework

**The smell.** To test the refund rules you start the web host, seed a database and send HTTP requests. To run the same refund from the nightly batch job, you either call your own API over HTTP or copy the logic. The use cases can only be driven one way, through the controllers, and they can only reach the outside world one way, through the concrete classes they call.

**The fix.** You've seen it already: it's the expiry job from Part 1, applied across the whole application. Each use case declares the ports it needs (`IPaymentRepository`, `IPaymentNotifier`, in Application, in its own vocabulary), and Infrastructure supplies the adapters. The same goes for the driving side. A use case is a plain class with a plain method, so a controller, a background job, a message consumer and a test can all call it directly. Being able to swap the database comes along with this, but it's the least important part. The everyday win is that tests and batch jobs drive the real rules without the web host.

![Before: a use case in Application points directly at SqlPaymentRepository in Infrastructure, so the business rule depends on the database code. After: the use case points at IPaymentRepository, which also lives in Application, and SqlPaymentRepository in Infrastructure points up at the same interface to implement it. The arrow crossing the layer boundary has reversed direction](dependency-flip.svg)

**What it's called.** Alistair Cockburn's **hexagonal architecture**, also called **ports and adapters** (2005), states both halves in its intent: to let an application "equally be driven by users, programs, automated test or batch scripts, and to be developed and tested in isolation from its eventual run-time devices and databases."[^hexagonal] A *port* is an interface the application owns. An *adapter* plugs a real technology into it. It's Dependency Inversion again — the arrow across the boundary flips, so Infrastructure points in toward Application — and the GoF Adapter again, at the scale of a project.

### Business rules in controllers, and repositories that return `IQueryable`

**The smell.** Two versions of the same leak. The first: the rules live in the controller.

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

The support tool's refund button and the batch refund job each re-implement those checks. They already disagree about partial refunds. Fowler would call the controller's interest in the payment's internals **Feature Envy**: a method more interested in another object's data than its own.[^refactoring]

The second: a repository that returns `IQueryable<Payment>`, so callers compose their own queries anywhere. Query logic ends up in every layer, and so do the limits of what EF Core can translate to SQL — a caller that adds a `.Where` with a C# method EF can't translate gets a runtime exception, in code that was supposed to know nothing about the database. The [LINQ post]({{< relref "linq-ef-core-and-the-sql-underneath.md" >}}) covers why the `IQueryable`/`IEnumerable` boundary matters so much.

**The fix.** Rules live in the domain (`payment.Refund(amount)` enforces the status and the remaining balance; the next section shows it). Orchestration lives in an Application use case (`RefundPaymentHandler`: load, call the provider, apply, save). The controller only translates — HTTP request in, command to the handler, result to a status code. The support tool and the batch job call the same handler, so there's one set of rules again. Repositories expose intent-named methods that return materialised results, so the query is written once, where the database is known.

**When not to.** For a small service, a repository wrapped around EF Core can be pure ceremony. EF Core's own documentation describes `DbContext` as "a combination of the Unit Of Work and Repository patterns."[^dbcontext] Using the `DbContext` directly inside Application handlers, while keeping it out of the domain and out of controllers, is a reasonable compromise that gives up the ability to swap the database in exchange for one less layer. What isn't reasonable is rules in controllers. That one costs you as soon as there's a second entry point.

### Refunds written behind the payment's back

**The smell.** The refund rules now live in `Payment.Refund`. But `Refund` is also an entity with its own `DbSet`, and the batch refund job, wanting to be quick, writes rows directly:

```csharp
// BatchRefundJob
foreach (var line in file.Lines)
    db.Refunds.Add(new Refund { PaymentId = line.PaymentId, Amount = line.Amount });
await db.SaveChangesAsync(ct);
```

**Why it hurts.** The rule "total refunds can't exceed the payment" isn't a rule about one refund. It's about a payment and *all* of its refunds together, so anything that can write a `Refund` without going through the `Payment` can break it. This job does: it skips the status check, the remaining-balance check and the status transition. It doesn't even need to be buggy to break things. Two support agents refunding the same payment at the same moment each load it, each see enough balance left, and each save. Every individual check passed, and the payment is over-refunded anyway.

**The fix.** Decide which objects form one consistency boundary, give that cluster a single entry point, and only ever load and save it whole:

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

`Refund` has no public constructor reachable from outside the domain, and there's no `DbSet<Refund>` for application code to write to. `IPaymentRepository` loads a payment together with its refunds and saves them together. The batch job becomes a loop over `RefundPaymentHandler`, the same use case the controller calls.

The `Version` property handles the two-agents race. Configured as a concurrency token, it makes EF Core add `WHERE Version = @original` to the payment's `UPDATE`, and throw `DbUpdateConcurrencyException` if another save got there first.[^concurrency] It's application-managed rather than a database `rowversion` for a reason. Adding a refund *inserts* a `Refund` row, and a second partial refund doesn't even change the payment's status. Without a deliberate change to the payment row itself, the second agent's save would touch nothing that a database-generated token watches.

![A dashed boundary labelled Payment aggregate contains the Payment root, holding Status, Amount and Version, and two Refund entities beneath it. RefundPaymentHandler calls Refund() on the root. BatchRefundJob's direct arrow to a Refund is crossed out, labelled db.Refunds.Add, no longer possible. Outside the boundary, Ledger is its own aggregate, reached by a PaymentCompleted event via the outbox](payment-aggregate.svg)

**What it's called.** An **aggregate**: in Fowler's summary of Evans, "a cluster of domain objects that can be treated as a single unit," with one of them as the **aggregate root**. "Any references from outside the aggregate should only go to the aggregate root," and "transactions should not cross aggregate boundaries."[^aggregate] `Payment` is the root, and `Refund` lives inside it.

That last rule explains a decision from Part 1. The ledger is a different aggregate, so posting to it isn't part of the payment's transaction. It happens in reaction to the `PaymentCompleted` event, through the outbox.

**When not to.** The usual mistake runs the other way: aggregates drawn too big. Make `Merchant` the root of all its payments, and every refund loads and locks a merchant with ten thousand of them. Keep an aggregate as small as its invariants allow, and refer to other aggregates by ID (`Payment.CustomerId`, not a `Customer` navigation property). If two things don't share a rule that must hold at every instant, they belong in separate aggregates.

### One model for every screen

**The smell.** The merchant dashboard shows a page of payments: amount, status, refunded total, method. It's built by loading aggregates and mapping them:

```csharp
var payments = await repository.FindForMerchantAsync(merchantId, page, ct);   // full Payments, with refunds
return payments.Select(p => new PaymentRow(
    p.Id, p.Amount.Amount, p.Amount.Currency, p.Status, p.Method, p.RefundedTotal.Amount, p.CreatedAt)).ToList();
```

Then support wants search by customer email, finance wants statement lines with FX rates, and risk wants a list of payments refunded within an hour. `IPaymentRepository` grows `FindForMerchantAsync`, `FindForSupportSearchAsync`, `FindForStatementAsync` and `FindRecentlyRefundedAsync`. `Payment` grows getters that only exist for screens.

**Why it hurts.** The aggregate is shaped to *protect rules on writes*: small, consistent, loaded whole. Screens want the opposite: flat, joined, filtered, paged, and only the columns on display. Pushing every read through the write model loads far more than the screen needs — the [LINQ post]({{< relref "linq-ef-core-and-the-sql-underneath.md" >}}) has a whole section on what that does to an endpoint — and it bends the domain toward the UI, one property at a time.

**The fix.** Let commands and queries take different paths. Commands — create, refund, cancel — go through the aggregate, as before. Queries skip the domain entirely and project straight from the database into the shape the screen wants:

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

There are no invariants to protect on a read, so there's nothing for the domain model to do. The refunded total is a `SUM` in SQL rather than a loop over loaded refunds, and only the seven columns on the screen leave the database. The query is free to use whatever the database is good at. The repository goes back to the two or three methods the commands need.

**What it's called.** **CQRS**. Fowler's description is "a different model to update information than the model you use to read information."[^cqrs] The command half has been in this post since the SRP section, as `CreatePaymentHandler` and `RefundPaymentHandler`. This is the query half.

CQRS is a ladder, and most systems should stop on the first rung:

1. **Separate query classes over the same database**, as above. Cheap, and usually all you need.
2. **Separate read models**: denormalised tables, kept up to date by handlers of domain events. These are fast to read, but now they're *eventually* consistent with the writes, and the UI has to cope with a refund that doesn't show up for a second.
3. **Separate read stores, or event sourcing**, where the event log is the source of truth and every view is a projection. Powerful, and a large commitment.

**When not to.** Fowler is unusually blunt about this one: "for most systems CQRS adds risky complexity," and it "should only be used on specific portions of a system."[^cqrs] Rung 1 is almost always worth it, since it's barely more than putting queries in their own classes. Rungs 2 and 3 should each be justified by a read workload the rung below has demonstrably failed to serve.

## Part 3: the smell these fixes create

Every fix above adds indirection: an interface, a lookup, a wrapper, a handler, a project. Indirection is a cost — more files, more jumps to follow a call, more names to learn — and it has to buy something to pay for itself. Applied by reflex, the fixes in this post produce their own recognisable smells:

- **An interface for every class**, each with exactly one implementation, existing only so tests can mock it. Interfaces at real boundaries (I/O, the clock, other teams' services) earn their keep. `IPaymentFeeCalculator` with one implementation and no I/O probably doesn't — test the real thing.
- **A factory, a strategy or a plugin system for a variation that doesn't exist yet.** Fowler calls this **Speculative Generality**,[^refactoring] and the XP slogan for avoiding it is **YAGNI** — "you aren't gonna need it." Fowler's write-up of it names four costs of building for a presumed future. There's the cost of building it, and the cost of delaying what was actually needed. There's the cost of repair, when the feature was the right one but built wrong. And there's the *cost of carry*: the added complexity that makes every later change harder, even if the guess was right.[^yagni]
- **Seven projects for a CRUD service.** The onion layers are worth it when there's real domain logic to protect. A service that maps HTTP to SQL and back has nothing at the centre of the onion.
- **A generic `IRepository<T>` over EF Core** that hides nothing and loses features (`Include`, projections, `AsNoTracking`) in the process.
- **Event sourcing and separate read stores for an admin screen.** CQRS's upper rungs bring eventual consistency, projections to rebuild and a second store to operate. They're for workloads that need them, not a default architecture.
- **Every table an aggregate, every aggregate a project.** DDD's tactical patterns pay off where there are rules worth protecting. Evans's book spends at least as much time on the strategic side — the language, and where one model's boundary ends — and that side is useful even in a system with no aggregates at all.

The rule that keeps this in check is old and simple: **refactor toward a pattern when the duplication or the variation is real, not when it's imagined.** Fowler's *Refactoring* passes on Don Roberts's rule of three: the first time you do something, just do it; the second time, wince and duplicate; the third time, refactor.[^refactoring] Most sections in Part 1 started with code that was reasonable when it was written — one switch, one provider, one export — and became a smell when the second and third cases arrived. That's the right time to introduce the pattern, and the smell is what tells you the time has come.

## Cheat sheet

| Smell | What it makes expensive | Fix | Name |
|---|---|---|---|
| Same `switch` on a type code in many places | Adding a case | One implementation per case, lookup at the edge | Strategy, Open/Closed |
| Callers translating the same foreign format | Fixing the translation | Translate once at the boundary into your own type | Adapter, anti-corruption layer |
| Same type-check chain over your own closed hierarchy, in many places | Adding a subtype safely | Visitor, or a C# 15 union with an exhaustive switch | Visitor |
| Huge constructor, methods using disjoint dependencies | Testing, parallel work | Split by who requests changes | Single Responsibility |
| Subtype throws `NotSupportedException`; callers type-check | Adding implementations | Split the capability into its own interface | Liskov, Interface Segregation |
| `new SqlConnection`, `DateTime.UtcNow`, `new HttpClient` in rules | Testing the rule, changing infrastructure | Depend on interfaces the rule owns | Dependency Inversion |
| Same logging/timing/retry around every call | Changing cross-cutting behaviour | Wrap the interface | Decorator |
| Status checks and assignments scattered | Understanding or protecting the lifecycle | Private setter + transition table, or state objects | State |
| One method triggering every downstream reaction | Adding a reaction; failure isolation | Raise a domain event; handlers subscribe (via an outbox) | Observer |
| Long positional constructors in tests | Reading and maintaining tests | Builder with defaults | Builder (test data builder) |
| `decimal` + `string` for money | Correctness | Immutable type owning its rules | Value object |
| Copy-pasted class differing in a few steps | Keeping copies in sync | Template Method; composition once two things vary | Template Method → Strategy, Bridge |
| Domain references EF and web projects | Changing infrastructure | Dependencies point inward, enforced by project references | Onion / clean architecture |
| Use cases only runnable through the web host | Testing, second drivers | Use cases as plain classes; ports owned by Application | Ports and adapters |
| Rules in controllers, `IQueryable` from repositories | Second entry points, query changes | Rules in domain, orchestration in use cases, intent-named repository methods | Application layer, thin controllers |
| Child rows written directly, bypassing their parent's rules | Keeping invariants under concurrency | One root as the only entry point; load and save whole; concurrency token | Aggregate (DDD) |
| Screens served by loading aggregates; repository grows a method per screen | Read performance, domain purity | Queries project straight to DTOs; commands go through the aggregate | CQRS |
| Interfaces, factories and layers with nothing behind them | Every change | Wait for the second or third case | YAGNI, rule of three |

[^codesmell]: Martin Fowler, ["CodeSmell"](https://martinfowler.com/bliki/CodeSmell.html) — the definition, Kent Beck's coining of the term, and the point that "smells don't always indicate a problem."
[^gof]: Erich Gamma, Richard Helm, Ralph Johnson and John Vlissides, *Design Patterns: Elements of Reusable Object-Oriented Software* (Addison-Wesley, 1994) — the 23-pattern catalogue in creational, structural and behavioural groups, the two principles from its introduction, and the intent statements quoted for Strategy, Adapter, Visitor, Command, Decorator, State, Observer, Builder, Template Method and Bridge.
[^dppaper]: Robert C. Martin, *Design Principles and Design Patterns* (2000) — Open/Closed, Liskov Substitution, Dependency Inversion and Interface Segregation as class-design principles, with OCP credited to Bertrand Meyer.
[^asd]: Robert C. Martin, *Agile Software Development: Principles, Patterns, and Practices* (Prentice Hall, 2002) — a chapter on each of the five principles, including the Interface Segregation wording quoted.
[^cleanarchbook]: Robert C. Martin, *Clean Architecture: A Craftsman's Guide to Software Structure and Design* (Prentice Hall, 2017), introduction to Part III — the history of the principles and Michael Feathers's naming of SOLID.
[^oosc]: Bertrand Meyer, *Object-Oriented Software Construction* (Prentice Hall, 1988) — the Open/Closed Principle.
[^exhaustive]: Microsoft Learn, ["Resolve pattern matching errors and warnings"](https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/compiler-messages/pattern-matching-warnings) — CS8509 for a non-exhaustive switch expression, and CS8524 for one that misses unnamed enum values.
[^refactoring]: Martin Fowler, *Refactoring: Improving the Design of Existing Code*, 2nd edition (Addison-Wesley, 2018) — the smells Repeated Switches, Shotgun Surgery, Long Parameter List, Large Class, Divergent Change, Feature Envy, Primitive Obsession and Speculative Generality; the refactoring Replace Conditional with Polymorphism; and Don Roberts's rule of three.
[^keyed]: Microsoft Learn, ["Dependency injection in .NET"](https://learn.microsoft.com/en-us/dotnet/core/extensions/dependency-injection) — keyed service registration with `AddKeyedSingleton` and resolution with `[FromKeyedServices]`.
[^typedclients]: Microsoft Learn, ["Use the IHttpClientFactory"](https://learn.microsoft.com/en-us/dotnet/core/extensions/httpclient-factory) — typed clients registered as transient, "Avoid typed clients in singleton services," and the two alternatives.
[^iso4217]: ISO, ["ISO 4217 — Currency codes"](https://www.iso.org/iso-4217-currency-codes.html) — the three-letter codes and each currency's number of minor-unit digits (0 for JPY, 2 for EUR, 3 for BHD).
[^unions]: Microsoft Learn, ["Union types"](https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/builtin-types/union) — the `union` declaration, its runtime support in .NET 11, and exhaustive `switch` checking over the case types.
[^srp]: Robert C. Martin, ["The Single Responsibility Principle"](https://blog.cleancoder.com/uncle-bob/2014/05/08/SingleReponsibilityPrinciple.html) (2014) — "one and only one reason to change," reasons as people, and "gather together the things that change for the same reasons."
[^liskov]: Barbara H. Liskov and Jeannette M. Wing, ["A Behavioral Notion of Subtyping"](https://doi.org/10.1145/197320.197383), *ACM Transactions on Programming Languages and Systems* 16(6), 1994 — the subtype requirement.
[^dip]: Robert C. Martin, "The Dependency Inversion Principle," *C++ Report*, May 1996 — the two-part statement of the principle.
[^httpclient]: Microsoft Learn, ["HttpClient guidelines for .NET"](https://learn.microsoft.com/en-us/dotnet/fundamentals/networking/http/httpclient-guidelines) — per-instance connection pools, port exhaustion from `TIME_WAIT`, and the two recommended lifetimes (long-lived with `PooledConnectionLifetime`, or short-lived from `IHttpClientFactory`).
[^timeprovider]: Microsoft Learn, ["What is the TimeProvider class"](https://learn.microsoft.com/en-us/dotnet/standard/datetime/timeprovider-overview) — built in from .NET 8, `TimeProvider.System`, `GetUtcNow()`, and `FakeTimeProvider` in `Microsoft.Extensions.TimeProvider.Testing`.
[^goos]: Steve Freeman and Nat Pryce, *Growing Object-Oriented Software, Guided by Tests* (Addison-Wesley, 2009) — test data builders with safe defaults.
[^complextypes]: Microsoft Learn, ["What's New in EF Core 8"](https://learn.microsoft.com/en-us/ef/core/what-is-new/ef-core-8.0/whatsnew) — complex types: no key, stored inline in the owning table's columns, and not discovered by convention.
[^dbcontext]: Microsoft Learn, ["DbContext Class"](https://learn.microsoft.com/en-us/dotnet/api/microsoft.entityframeworkcore.dbcontext) — "DbContext is a combination of the Unit Of Work and Repository patterns."
[^boundedcontext]: Martin Fowler, ["BoundedContext"](https://martinfowler.com/bliki/BoundedContext.html) — bounded contexts as a central pattern of DDD, and a new model where the language changes.
[^cqrs]: Martin Fowler, ["CQRS"](https://martinfowler.com/bliki/CQRS.html) — the definition, Greg Young as its source, and the warnings that it "adds risky complexity" for most systems and belongs only in specific portions of one.
[^cqs]: Martin Fowler, ["CommandQuerySeparation"](https://martinfowler.com/bliki/CommandQuerySeparation.html) — the term coined by Bertrand Meyer in *Object Oriented Software Construction*, and the command/query definitions.
[^aggregate]: Martin Fowler, ["DDD_Aggregate"](https://martinfowler.com/bliki/DDD_Aggregate.html) — the aggregate as a single unit, the root as the only target of outside references, and transactions not crossing aggregate boundaries.
[^concurrency]: Microsoft Learn, ["Handling Concurrency Conflicts"](https://learn.microsoft.com/en-us/ef/core/saving/concurrency) — concurrency tokens, the extra `WHERE` condition, `DbUpdateConcurrencyException`, and application-managed tokens.
[^evans]: Eric Evans, *Domain-Driven Design: Tackling Complexity in the Heart of Software* (Addison-Wesley, 2003) — ubiquitous language, bounded contexts, value objects, aggregates, and the anti-corruption layer as a translating boundary to another model.
[^onion]: Jeffrey Palermo, ["The Onion Architecture: part 1"](https://jeffreypalermo.com/2008/07/the-onion-architecture-part-1/) (2008) — the domain model at the centre, repository interfaces in the application core, and coupling pointing toward the centre.
[^cleanarch]: Robert C. Martin, ["The Clean Architecture"](https://blog.cleancoder.com/uncle-bob/2012/08/13/the-clean-architecture.html) (2012) — the Dependency Rule, the four rings, and the list of architectures the diagram unifies.
[^hexagonal]: Alistair Cockburn, ["Hexagonal Architecture"](https://alistair.cockburn.us/hexagonal-architecture/) (2005) — the intent statement and the ports-and-adapters model.
[^yagni]: Martin Fowler, ["Yagni"](https://martinfowler.com/bliki/Yagni.html) — presumptive features and the costs of build, delay, carry and repair.
