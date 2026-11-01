---
title: "DI Lifetimes and Failure Boundaries"
date: 2026-08-05T10:00:00+02:00
draft: false
tags: ["software", "csharp", "dotnet", "aspnetcore", "dotnet-deep-dives", "ai-generated"]
description: "Captive dependencies, why a singleton can't hold a DbContext, throw vs throw ex, and deciding where in a system a failure should be caught."
---

Two topics that look unrelated and are the same question in different clothes: **which component owns this?**

Dependency injection is about ownership of *lifetime* — how long a thing lives and who decides. Exception handling is about ownership of *failure* — which layer is responsible for deciding what a problem means. Both go wrong the same way, by putting the boundary in the wrong place.

## Exceptions as a design decision

### When to throw

An exception says *this operation cannot complete and I can't decide what to do about it*. It's a transfer of responsibility up the stack to someone with enough context to choose.

The usual guidance is "don't use exceptions for control flow," which is right but under-explained. The reason isn't purely cost. Throwing is genuinely expensive — it walks the stack to build a trace, and the guidance is explicit that you should "consider the performance implications" and avoid designs where exceptions occur routinely[^exception-guidelines] — but the multiplier depends so heavily on stack depth that a single figure would be misleading, and it only matters at volume anyway. The stronger reason is that exceptions are **invisible in the signature**. A method returning `Order` claims to return an `Order`; that it might instead unwind the stack is documented nowhere the compiler can check.

So the useful split:

- **Expected, and the caller must handle it** — return it. A "not found", a validation failure, a parse error. These aren't exceptional; they're outcomes. `TryParse` over `Parse`, a result type, or a nullable return.
- **Unexpected, or the caller can't sensibly continue** — throw. A database that's down, a required configuration missing, an invariant violated.

"User submitted an invalid email" is not exceptional — it's the most predictable thing in the system, and modelling it as an exception means the happy path is written as though it can't happen. "The connection string is missing at startup" absolutely is: there's no sensible continuation.

Result types (`Result<T, TError>`, or a discriminated-union-shaped record hierarchy) make failure visible in the signature and force the caller to handle it. They're a genuine improvement for expected failures, and they get tedious when applied to everything — so use them at the boundaries where failure is part of the contract, and let exceptions handle the genuinely exceptional.

### throw vs throw ex

```csharp
catch (Exception ex)
{
    _logger.LogError(ex, "failed");
    throw ex;      // WRONG — resets the stack trace to this line
}
```

`throw ex;` rethrows the exception object as if it originated *here*.[^rethrow] The original stack trace — the actual location of the bug — is overwritten. You keep the exception type and message, and lose the only information that tells you where to look.

```csharp
throw;             // preserves the original stack trace
```

Bare `throw` continues the original propagation. It's legal only inside a `catch` block, and it's what you want essentially always.

When you need to add context, wrap rather than replace:

```csharp
catch (SqlException ex)
{
    throw new OrderProcessingException($"failed to save order {id}", ex);
}
```

The original goes in as `InnerException` and the full chain survives. The third option, `ExceptionDispatchInfo.Capture(ex).Throw()`, preserves the stack trace while rethrowing from a different location — necessary when you're rethrowing an exception you stored earlier, which is how the async machinery does it internally.

### Exceptions through async code

When an `async Task` method throws, the exception doesn't propagate immediately — it's captured and stored on the returned `Task`, which transitions to Faulted. It's re-thrown when someone awaits.

That has three consequences.

**An unawaited task swallows its exception.** Call an async method, discard the task, and a failure inside it is never observed. It won't crash anything; it'll just silently not have happened. This is the most common way async work disappears.

**`await` unwraps, `.Result` doesn't.** Awaiting a faulted task rethrows the original exception with its stack trace preserved. Calling `.Result` or `.Wait()` throws an `AggregateException` wrapping it, so your `catch (SqlException)` doesn't match and you get a confusing wrapper instead.

**`Task.WhenAll` only surfaces the first.** If five of ten tasks fail, awaiting the `WhenAll` throws one exception. The others are on the task itself:

```csharp
var all = Task.WhenAll(tasks);
try { await all; }
catch { foreach (var ex in all.Exception!.InnerExceptions) _logger.LogError(ex, "one of many"); }
```

And `async void`, as covered in the [async post]({{< relref "async-concurrency-and-a-thread-safe-cache.md" >}}), has nowhere to store a failure at all — it goes straight to the synchronization context and typically kills the process.

### The empty catch

```csharp
catch (Exception)
{
}
```

This says: whatever went wrong, at any layer, for any reason, proceed as though it didn't. It catches `NullReferenceException` from your own bug identically to a transient timeout. The program continues in a state its author never considered, and the eventual failure surfaces somewhere unrelated with no trace of the cause. It also catches the exception you'd need to diagnose the problem.

Logging and swallowing is better but still usually wrong — it produces systems that log continuously and never fail, where nobody notices because nothing is ever *broken*, just wrong.

Catch when you can genuinely do something: retry a transient fault, fall back to a default, translate to a domain error, add context and rethrow. If none of those apply, let it propagate. **Not catching is a valid and frequently correct decision.**

Catch specific types. `catch (Exception)` at a low level is nearly always too broad; at the outermost boundary it's exactly right, which is the next section.

### Where the boundary goes

Most code should not catch anything. Failures should propagate to a small number of deliberate boundaries:

- **The request boundary** — one handler that turns any unhandled exception into a response.
- **The message/job boundary** — one handler deciding retry, dead-letter, or drop.
- **The application boundary** — startup and shutdown.

In ASP.NET Core the request boundary is exception-handler middleware, registered first so it wraps everything after it:

```csharp
app.UseExceptionHandler(...);   // outermost
```

The modern form is `IExceptionHandler`, added in .NET 8, which lets you register several and have each decide whether it handles a given exception:[^iexceptionhandler]

```csharp
public sealed class ValidationExceptionHandler : IExceptionHandler
{
    public async ValueTask<bool> TryHandleAsync(
        HttpContext ctx, Exception ex, CancellationToken ct)
    {
        if (ex is not ValidationException ve) return false;   // pass it on

        await Results.ValidationProblem(ve.Errors)   // IDictionary<string, string[]>
                     .ExecuteAsync(ctx);
        return true;
    }
}
```

Registered with `AddExceptionHandler<T>()` plus `UseExceptionHandler()`, these run in order until one returns true. The pattern to aim for: one handler per *category* of failure, mapping to the right status code and a `ProblemDetails` body, plus a catch-all that returns 500 and logs.

Two rules for the catch-all. **Never return the exception message to the client** — it leaks stack traces, connection strings, and internal structure. Return a correlation ID and log the detail. And **log at the boundary, not at every level**: logging and rethrowing at each layer produces the same failure five times in the log with five different stack depths.

## Dependency injection

### What it's actually for

Not "so you can swap implementations" — that's rare in practice. The real payoff is that a class **declares what it needs and doesn't decide how to get it**. That's what makes it testable, because a test can supply whatever it wants, and it's what lets composition happen in one place instead of being scattered through constructors.

The container is a convenience on top of that. Constructor injection is the pattern; `IServiceCollection` is one way to wire it.

### The three lifetimes

- **Transient** — a new instance per resolution. Safe default. Cheap objects, no shared state.[^di-lifetimes]
- **Scoped** — one instance per scope, which in ASP.NET Core means per HTTP request. The right lifetime for anything carrying request state: `DbContext`, unit of work, the current user.
- **Singleton** — one instance for the application. Expensive-to-create, stateless, or genuinely shared things: caches, `HttpClient` factories, configuration.

The consequence people underestimate: **a singleton must be thread-safe.** It will be called concurrently by every request in flight. A singleton holding a `Dictionary` and mutating it will corrupt it under load, and the symptom is usually an infinite loop or a wrong lookup rather than a clean exception.

### Captive dependencies

A service can only safely depend on something that lives **at least as long** as it does. Violate that and the shorter-lived dependency gets *captured* — held by the longer-lived object well past its intended lifetime.

The canonical case:

> Can a singleton safely depend on `DbContext`?

**No**, and it's a good question precisely because the failure is delayed and confusing rather than immediate.

`AddDbContext` registers as scoped: one per request, disposed at the end. Inject it into a singleton and the singleton captures the very first request's context and holds it forever. Then:

- It's **disposed** after that first request, so every later use throws `ObjectDisposedException`.
- If it somehow isn't, it's now shared across concurrent requests — and `DbContext` is not thread-safe, so you get `A second operation was started on this context instance`.
- Its change tracker accumulates every entity it ever loads, so it also leaks memory.

The correct pattern is to inject the factory and create a scope per unit of work:

```csharp
public sealed class OrderPoller : BackgroundService          // singleton
{
    private readonly IServiceScopeFactory _scopeFactory;

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            using var scope = _scopeFactory.CreateScope();
            var db = scope.ServiceProvider.GetRequiredService<AppDbContext>();
            // ... one unit of work, then the scope and context are disposed
            await Task.Delay(TimeSpan.FromSeconds(30), ct);
        }
    }
}
```

`IDbContextFactory<T>` is the more targeted alternative when the context is all you need.

This bites hardest in `BackgroundService`, which is registered as a singleton — the single most common place people accidentally capture a scoped dependency.

The container will catch some of this for you. Scope validation (`ValidateScopes`) throws when a singleton resolves a scoped service, and `ValidateOnBuild` moves the check to startup; the default host enables both in the Development environment.[^di-validation] Both are worth enabling in every environment: a startup failure beats an `ObjectDisposedException` under production load.

The reverse — a scoped or transient service depending on a singleton — is always fine, since the singleton outlives them.

### Injecting IServiceProvider

```csharp
public class OrderService(IServiceProvider provider)   // smell
{
    public void Process() => provider.GetRequiredService<IEmailSender>().Send();
}
```

This is service location, and it undoes what DI gives you. The dependencies are no longer visible in the constructor, so you can't tell what the class needs without reading every method. Missing registrations become runtime failures rather than startup failures. And tests need a whole container instead of two mocks.

The legitimate uses are narrow: resolving a scope inside a singleton (as above), and genuinely dynamic resolution where the type isn't known until runtime. Otherwise, inject what you need.

### IOptions

Configuration binding, with three flavours that differ by lifetime:

- `IOptions<T>` — singleton, read once at startup. Fine for settings that never change.[^options]
- `IOptionsSnapshot<T>` — scoped, re-read per request. Picks up config changes without a restart.
- `IOptionsMonitor<T>` — singleton with change notifications. The one to use inside another singleton, since `IOptionsSnapshot` can't be injected there.

That last point is a captive dependency in miniature: injecting `IOptionsSnapshot<T>` (scoped) into a singleton is the same mistake as injecting a `DbContext`.

Prefer `AddOptions<T>().Bind(...).ValidateDataAnnotations().ValidateOnStart()` so bad configuration fails at startup with a clear message rather than at first use with a `NullReferenceException`.

### Five dependencies

> How would you test a class with five dependencies?

You mock the ones that matter and supply real instances of the rest — but the more interesting question is the follow-up: is five a problem?

Not inherently. A coordinator that legitimately orchestrates five collaborators is fine. It becomes a signal when the dependencies have **no coherence** — a repository, an email sender, a clock, a feature flag service, and a PDF generator in one class describes something doing several unrelated jobs.

The useful diagnostic isn't the count, it's whether every method uses most of the dependencies. If method A uses two and method B uses the other three, that's two classes wearing one name.

A large constructor is also often a *test* smell before it's a design smell: needing to construct five mocks to test one behaviour is the friction telling you the unit is too big. That's information, not just inconvenience.

## The common thread

Both halves are the same discipline: put the boundary where the knowledge is.

A failure should be handled by the layer that knows what it means — which is usually much further up than where it's tempting to catch it. A lifetime should be owned by the component that knows when the work is done — which is usually a scope, not a singleton.

Most bugs in both categories come from a component taking on responsibility it doesn't have the context to discharge: catching an exception it can't do anything about, or holding a dependency past the point where it's still valid.

Next: [designing an API that survives failure]({{< relref "designing-an-api-that-survives-failure.md" >}}) — where these boundaries meet the network.

[^exception-guidelines]: Microsoft, ["Best practices for exceptions"](https://learn.microsoft.com/en-us/dotnet/standard/exceptions/best-practices-for-exceptions) — performance considerations, and designing so exceptions aren't part of normal flow.
[^rethrow]: Microsoft, ["How to use the try/catch block to catch exceptions"](https://learn.microsoft.com/en-us/dotnet/standard/exceptions/how-to-use-the-try-catch-block-to-catch-exceptions) — rethrowing with `throw;` preserves the original stack trace.
[^iexceptionhandler]: Microsoft, ["Handle errors in ASP.NET Core"](https://learn.microsoft.com/en-us/aspnet/core/fundamentals/error-handling) — `IExceptionHandler`, `AddExceptionHandler`, and handler ordering.
[^di-lifetimes]: Microsoft, ["Dependency injection in ASP.NET Core"](https://learn.microsoft.com/en-us/aspnet/core/fundamentals/dependency-injection) — transient, scoped and singleton lifetimes, and the captive-dependency warning against resolving scoped services from singletons.
[^di-validation]: Microsoft, ["Dependency injection in .NET"](https://learn.microsoft.com/en-us/dotnet/core/extensions/dependency-injection) — `ValidateScopes` and `ValidateOnBuild`, enabled by default in the Development environment.
[^options]: Microsoft, ["Options pattern in ASP.NET Core"](https://learn.microsoft.com/en-us/aspnet/core/fundamentals/configuration/options) — lifetimes of `IOptions`, `IOptionsSnapshot` and `IOptionsMonitor`, and `ValidateOnStart`.
