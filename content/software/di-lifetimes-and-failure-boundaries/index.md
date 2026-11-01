---
title: "DI Lifetimes and Failure Boundaries"
date: 2026-08-05T10:00:00+02:00
lastmod: 2026-11-01T13:59:00+02:00
draft: false
tags: ["software", "csharp", "dotnet", "aspnetcore", "dotnet-deep-dives", "ai-generated"]
description: "Captive dependencies, why a singleton can't hold a DbContext, throw vs throw ex, and deciding where in a system a failure should be caught."
---

Two topics that look unrelated and are the same question: **which component owns this?** Dependency injection is about ownership of *lifetime*, how long a thing lives and who decides. Exception handling is about ownership of *failure*, which layer decides what a problem means. Both go wrong by putting the boundary in the wrong place.

## Exceptions as a design decision

### When to throw

An exception says *this operation can't complete and I can't decide what to do about it*: a transfer of responsibility up the stack to someone with enough context to choose.

The usual guidance is "don't use exceptions for control flow", which is right but under-explained. Throwing is expensive (it walks the stack to build a trace, and the guidance says to avoid designs where exceptions occur routinely[^exception-guidelines]), but that only matters at volume. The stronger reason is that exceptions are **invisible in the signature**. A method returning `Order` claims to return an `Order`; that it might instead unwind the stack is documented nowhere the compiler can check.

So the useful split:

- **Expected, and the caller must handle it** — return it. A "not found", a validation failure, a parse error. These aren't exceptional; they're outcomes. `TryParse` over `Parse`, a result type, or a nullable return.
- **Unexpected, or the caller can't sensibly continue** — throw. A database that's down, a required configuration missing, an invariant violated.

"User submitted an invalid email" is the most predictable thing in the system; "the connection string is missing at startup" has no sensible continuation.

Result types (`Result<T, TError>`, or a record hierarchy shaped like a discriminated union) make failure visible in the signature and force the caller to handle it. They get tedious applied to everything, so use them where failure is part of the contract and let exceptions handle the genuinely exceptional.

### throw vs throw ex

```csharp
catch (Exception ex)
{
    _logger.LogError(ex, "failed");
    throw ex;      // WRONG — resets the stack trace to this line
}
```

`throw ex;` rethrows the exception object as if it originated *here*,[^rethrow] overwriting the stack trace: you keep the type and message, and lose where the bug actually is.

```csharp
throw;             // preserves the original stack trace
```

Bare `throw` (legal only inside a `catch`) continues the original propagation, and is what you want essentially always. To add context, wrap rather than replace:

```csharp
catch (SqlException ex)
{
    throw new OrderProcessingException($"failed to save order {id}", ex);
}
```

The original goes in as `InnerException` and the full chain survives. The third option, `ExceptionDispatchInfo.Capture(ex).Throw()`, keeps the original trace while rethrowing from outside the `catch`, for an exception you stored earlier;[^rethrow] it's how `await` rethrows a task's stored exception.

### Exceptions through async code

When an `async Task` method throws, the exception is stored on the returned `Task`, which becomes Faulted, and rethrown when someone awaits. Three consequences:

**An unawaited task swallows its exception.** Discard the task and a failure inside it is never observed. Nothing crashes; the work just silently didn't happen.

**`await` unwraps, `.Result` doesn't.** Awaiting a faulted task rethrows the original exception with its stack trace. `.Result` or `.Wait()` throws an `AggregateException` wrapping it, so your `catch (SqlException)` doesn't match.

**`Task.WhenAll` only surfaces the first.** If five of ten tasks fail, awaiting the `WhenAll` throws one exception. The others are on the task itself:

```csharp
var all = Task.WhenAll(tasks);
try { await all; }
catch when (all.Exception is not null)
{
    foreach (var ex in all.Exception.InnerExceptions) _logger.LogError(ex, "one of many");
}
```

And `async void`, as covered in the [async post]({{< relref "async-concurrency-and-a-thread-safe-cache.md" >}}), has nowhere to store a failure at all, which typically kills the process.

### The empty catch

```csharp
catch (Exception)
{
}
```

This says: whatever went wrong, proceed as though it didn't. It treats a `NullReferenceException` from your own bug like a transient timeout, and the program continues in a state its author never considered, failing later somewhere unrelated with no trace of the cause.

Logging and swallowing is better but still usually wrong: it produces systems that log continuously and never fail, where nothing is ever *broken*, just wrong.

Catch when you can genuinely do something: retry a transient fault, fall back to a default, translate to a domain error, add context and rethrow. If none of those apply, let it propagate. **Not catching is a valid and frequently correct decision.**

Catch specific types. `catch (Exception)` at a low level is nearly always too broad; at the outermost boundary it's exactly right, which is the next section.

### Where the boundary goes

Most code shouldn't catch anything. Failures should propagate to a few deliberate boundaries:

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
        // RequestValidationException: your own type carrying IDictionary<string, string[]> Errors
        if (ex is not RequestValidationException ve) return false;   // pass it on

        await Results.ValidationProblem(ve.Errors)
                     .ExecuteAsync(ctx);
        return true;
    }
}
```

Registered with `AddExceptionHandler<T>()` plus `UseExceptionHandler()`, these run in order until one returns true. The pattern to aim for: one handler per *category* of failure, mapping to the right status code and a `ProblemDetails` body, plus a catch-all that returns 500 and logs.

Two rules for the catch-all. **Never return the exception message to the client**: it leaks stack traces, connection strings and internal structure. Return a correlation ID and log the detail. And **log at the boundary, not at every level**, or the same failure appears five times in the log at five stack depths.

## Dependency injection

### What it's actually for

Not "so you can swap implementations", which is rare in practice. The payoff is that a class **declares what it needs and doesn't decide how to get it**, so a test can supply whatever it wants and composition happens in one place. Constructor injection is the pattern; the container is one way to wire it.

### The three lifetimes

- **Transient** — a new instance per resolution. For cheap objects with no shared state.[^di-lifetimes]
- **Scoped** — one instance per scope, which in ASP.NET Core means per HTTP request. The right lifetime for anything carrying request state: `DbContext`, unit of work, the current user.
- **Singleton** — one instance for the application. Expensive-to-create, stateless, or genuinely shared things: caches, `HttpClient` factories, configuration.

The consequence people underestimate: **a singleton must be thread-safe**, because every request in flight calls it concurrently. A singleton mutating a `Dictionary` will corrupt it under load, and the symptom is a hang, a wrong lookup or an intermittent `InvalidOperationException` rather than a clean, reproducible failure.

### Captive dependencies

A service can only safely depend on something that lives **at least as long** as it does. Otherwise the shorter-lived dependency gets *captured*, held by the longer-lived object past its intended lifetime.

![Lifetimes on a timeline. The singleton and the root container live for the whole application. Each HTTP request gets its own scope with its own DbContext, disposed when the request ends. A DbContext injected into the singleton is resolved from the root instead, so one context outlives every request and is shared by all of them](captive-dependency.svg)

> Can a singleton safely depend on `DbContext`?

**No**, and the failure is delayed and confusing rather than immediate. `AddDbContext` registers the context as scoped: one per request, disposed at the end. A singleton's dependencies are resolved from the root container, though. In Development the default host refuses to build ("Cannot consume scoped service … from singleton …"), but validation is off by default elsewhere, and there the "scoped" context injected into it is created in the root scope and lives as long as the application:[^di-lifetimes]

- It's shared by every concurrent request, and `DbContext` isn't thread-safe, so you get `A second operation was started on this context instance`.
- Its change tracker accumulates every entity it ever loads, which leaks memory and, through identity resolution, serves stale data.
- Nothing disposes it until shutdown: a scoped service created in the root is "effectively promoted to singleton".[^di-validation]

The correct pattern is to inject the factory and create a scope per unit of work:

```csharp
public sealed class OrderPoller(IServiceScopeFactory scopeFactory) : BackgroundService   // singleton
{
    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            await using (var scope = scopeFactory.CreateAsyncScope())
            {
                var db = scope.ServiceProvider.GetRequiredService<AppDbContext>();
                // ... one unit of work
            }   // scope and context disposed here, before the wait
            await Task.Delay(TimeSpan.FromSeconds(30), ct);
        }
    }
}
```

`IDbContextFactory<T>` is the more targeted alternative when the context is all you need. This bites hardest in `BackgroundService`, which is registered as a singleton and is the most common place to capture a scoped dependency by accident.

The container catches some of this. Scope validation (`ValidateScopes`) checks that scoped services aren't resolved from the root or injected into singletons, and `ValidateOnBuild` checks every registration at startup; the default host enables both only in the Development environment.[^di-validation][^host-validation] Enable them everywhere: a startup failure beats a corrupted context under production load. The reverse, a scoped or transient service depending on a singleton, is always fine.

### Injecting IServiceProvider

```csharp
public class OrderService(IServiceProvider provider)   // smell
{
    public void Process() => provider.GetRequiredService<IEmailSender>().Send();
}
```

This is service location, and it undoes what DI gives you: dependencies are invisible in the constructor, missing registrations fail at runtime rather than startup, and tests need a whole container instead of two mocks. The legitimate uses are narrow: creating a scope inside a singleton (though injecting `IServiceScopeFactory`, as above, is narrower), and genuinely dynamic resolution where the type isn't known until runtime.

### IOptions

Configuration binding, with three flavours that differ by lifetime:

- `IOptions<T>` — singleton, bound once on first use. Fine for settings that never change.[^options]
- `IOptionsSnapshot<T>` — scoped, re-read per request. Picks up config changes without a restart.
- `IOptionsMonitor<T>` — singleton with change notifications. The one to use inside another singleton, since `IOptionsSnapshot` can't be injected there.

Injecting `IOptionsSnapshot<T>` (scoped) into a singleton is the `DbContext` mistake in miniature. Prefer `AddOptions<T>().Bind(...).ValidateDataAnnotations().ValidateOnStart()` so bad configuration fails at startup with a clear message rather than at first use with a `NullReferenceException`.

### Five dependencies

> How would you test a class with five dependencies?

Mock the ones that matter and supply real instances of the rest. The more interesting question is whether five is a problem.

Not inherently: a coordinator that orchestrates five collaborators is fine. It's a signal when the dependencies have **no coherence**: a repository, an email sender, a clock, a feature-flag service and a PDF generator in one class describe several unrelated jobs. The useful diagnostic is whether every method uses most of the dependencies. If method A uses two and method B the other three, that's two classes wearing one name, and needing five mocks to test one behaviour is that friction showing up in the tests first.

## The common thread

Both halves are the same discipline: put the boundary where the knowledge is.

A failure should be handled by the layer that knows what it means, usually much further up than where it's tempting to catch it. A lifetime should be owned by the component that knows when the work is done, usually a scope, not a singleton. Most bugs in both come from a component taking on responsibility it lacks the context for: catching an exception it can't do anything about, or holding a dependency past the point where it's valid.

Next: [designing an API that survives failure]({{< relref "designing-an-api-that-survives-failure.md" >}}) — where these boundaries meet the network.

[^exception-guidelines]: Microsoft, ["Best practices for exceptions"](https://learn.microsoft.com/en-us/dotnet/standard/exceptions/best-practices-for-exceptions) — performance considerations, and designing so exceptions aren't part of normal flow.
[^rethrow]: Microsoft, ["Best practices for exceptions: Capture and rethrow exceptions properly"](https://learn.microsoft.com/en-us/dotnet/standard/exceptions/best-practices-for-exceptions#capture-and-rethrow-exceptions-properly) — `throw e` restarts the stack trace; use bare `throw` inside the handler and `ExceptionDispatchInfo` outside it.
[^iexceptionhandler]: Microsoft, ["Handle errors in ASP.NET Core"](https://learn.microsoft.com/en-us/aspnet/core/fundamentals/error-handling) — `IExceptionHandler`, `AddExceptionHandler`, and handler ordering.
[^di-lifetimes]: Microsoft, ["Service lifetimes"](https://learn.microsoft.com/en-us/dotnet/core/extensions/dependency-injection/service-lifetimes) — transient, scoped and singleton lifetimes; resolving a scoped service from a singleton "causes the scoped service to behave like a singleton, which can lead to incorrect state when processing subsequent requests".
[^di-validation]: Microsoft, ["Dependency injection in .NET: Scope validation"](https://learn.microsoft.com/en-us/dotnet/core/extensions/dependency-injection/overview#scope-validation) — a scoped service created in the root container "is effectively promoted to singleton"; in Development the default provider checks that scoped services aren't resolved from the root or injected into singletons.
[^host-validation]: Microsoft, [".NET Generic Host: Host builder settings"](https://learn.microsoft.com/en-us/dotnet/core/extensions/generic-host#host-builder-settings) — the default builders enable scope validation and dependency validation (`ValidateOnBuild`) when the environment is Development.
[^options]: Microsoft, ["Options pattern in ASP.NET Core"](https://learn.microsoft.com/en-us/aspnet/core/fundamentals/configuration/options) — lifetimes of `IOptions`, `IOptionsSnapshot` and `IOptionsMonitor`, and `ValidateOnStart`.
