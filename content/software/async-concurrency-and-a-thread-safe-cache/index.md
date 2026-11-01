---
title: "Async, Concurrency, and a Thread-Safe Cache"
date: 2026-08-08T10:00:00+02:00
lastmod: 2026-11-01T13:54:00+02:00
draft: false
tags: ["software", "csharp", "dotnet", "async", "concurrency", "dotnet-deep-dives", "ai-generated"]
description: "What async/await compiles to, why sync-over-async starves the thread pool, and three ways to build a cache where only one caller populates a missing value."
---

`async` and `await` hide so much that it's possible to ship async code for years with a mental model that's subtly wrong. That stops being fine under load, which is exactly when the failure is hardest to reproduce.

This post has two halves. The first takes the machinery apart. The second builds one thing properly, a cache where a hundred simultaneous callers cause exactly one database hit, because that problem exercises almost everything in the first half. Every number quoted came out of a scratch console project on .NET 10.

## async does not create a thread

`async` is a compiler instruction: rewrite this method as a state machine so it can be suspended and resumed. It doesn't start a thread, queue work, or introduce parallelism, and the documentation is blunt about it: "The `async` and `await` keywords don't cause extra threads to be created. Async methods don't require multithreading because an async method doesn't run on its own thread."[^async-threads] A method marked `async` containing no `await` at all runs entirely synchronously on the calling thread, and the compiler warns you about it.[^async-nowait]

What actually happens is closer to a callback transformation. The compiler splits your method at each `await` into a state machine implementing `IAsyncStateMachine`, with a `MoveNext()` method containing your code as a switch over states.[^async-statemachine] Roughly:

```csharp
public async Task<User> GetUserAsync(int id)
{
    var row = await _db.QueryAsync(id);   // suspension point
    return Map(row);
}
```

becomes, conceptually: run until `QueryAsync` returns a `Task`. If that task is already complete, keep going synchronously. If it isn't, hook a continuation onto it, **return an incomplete `Task` to the caller, and release the current thread entirely**. When the I/O completes, the continuation runs `MoveNext()` again, re-entering the method at the state after that `await`.

![One request on a timeline. Thread 7 runs GetUserAsync until await QueryAsync, then returns to the thread pool and serves other requests while the database works; no thread waits on the query. When the I/O completes, the continuation runs MoveNext on whichever pool thread is free, here thread 12, which runs Map and returns](await-thread.svg)

The thread isn't blocked while the database works. It's back in the pool, serving other requests. That's why async improves *throughput* (requests served per thread) rather than *latency*: an async request is often marginally slower than the sync equivalent, but a server full of them handles vastly more load.

The corollary: `async` does nothing useful for CPU-bound work; awaiting a computation just adds state machine overhead. For CPU-bound parallelism you want `Parallel.For`/`ForEachAsync` or explicit `Task.Run`, which genuinely do use multiple threads.

- **I/O-bound** — waiting on a network, disk, or database. No thread is doing the work; the OS notifies on completion. Async is a pure win.
- **CPU-bound** — the work *is* a thread burning cycles. Async only helps if you deliberately move it off the current thread.

## Task, ValueTask, and allocation

A `Task` is a promise object: a heap allocation carrying a state (pending/succeeded/faulted/cancelled), a result slot, an exception slot, and a continuation list.

`ValueTask<T>` exists because that allocation is wasteful when the method usually completes synchronously. A cache lookup that hits 95% of the time allocates a `Task<T>` on every one of those hits for nothing. `ValueTask<T>` is a struct that either wraps a result directly (no allocation) or wraps a `Task` when it genuinely has to suspend.

The cost is a much more restrictive contract. The documented list of things you must never do to a `ValueTask` is:[^valuetask-rules]

- await it more than once;
- call `.AsTask()` more than once;
- use `.Result` or `.GetAwaiter().GetResult()` before it has completed, or more than once;
- use more than one of these techniques to consume it.

`Task` permits all of them. Violating the `ValueTask` rules doesn't reliably throw — the docs simply say "the results are undefined", because the underlying `IValueTaskSource` may have been recycled for a different operation by the time you look again. If you need to consume it twice, call `.AsTask()` once and use that.

Microsoft's own guidance is that `Task` remains the default: "the default choice for any asynchronous method should be to return a `Task` or `Task<TResult>`. Only if performance analysis proves it worthwhile should a `ValueTask<TResult>` be used".[^valuetask-rules] "We changed everything to ValueTask for performance" usually made the code harder to reason about and no faster.

## async void

`async void` exists for exactly one reason: event handlers, whose signature the framework fixed before async existed. Everywhere else it is a bug waiting to happen.

The caller gets no `Task`, so there's nothing to await and nothing to observe. "The caller of a void-returning async method can't catch exceptions thrown from the method",[^async-void] because there's no `Task` to carry them. Instead they're "raised directly on the SynchronizationContext that was active when the async void method started";[^cleary] ASP.NET Core and console apps have none, so the exception lands on a thread-pool thread as an unhandled exception and **crashes the process**.

`async Task` stores the exception in the task, so it surfaces at the `await`. A fire-and-forget background operation isn't a reason for `async void`; it's a reason to capture and log explicitly:

```csharp
_ = Task.Run(async () =>
{
    try { await DoWorkAsync(); }
    catch (Exception ex) { _logger.LogError(ex, "background work failed"); }
});
```

Better still, use a hosted service, so shutdown can wait for it.

## Sync over async, and thread-pool starvation

Calling `.Result` or `.Wait()` on an incomplete task blocks the current thread until it completes. Two distinct failures follow.

**The deadlock**, in any environment with a single-threaded synchronization context (classic ASP.NET, WinForms, WPF). Your thread calls `.Result` and blocks, holding the context. The async operation completes and tries to schedule its continuation *onto that same context*, which is occupied by the thread waiting for the continuation. `ConfigureAwait(false)` on every `await` inside the library fixes it by telling the continuation not to require the original context, which is why library code should use it and application code generally needn't.

ASP.NET Core has **no** synchronization context, so this deadlock doesn't occur there. That's led to a widespread belief that `.Result` is now safe. It isn't, and the second failure is worse, because it scales.

**Thread-pool starvation.** Every blocked thread is a pool thread doing nothing but waiting. Beyond its minimum, the pool doesn't simply add a thread per queued work item — it "creates and destroys worker threads in order to optimize throughput".[^threadpool-injection] Gradual injection is the right behaviour when blocking is rare and brief, and the wrong shape when every request blocks: new threads arrive slower than blocked ones accumulate. Meanwhile the queue grows, latency climbs from milliseconds to seconds, requests time out, and CPU sits near zero because nothing is actually *doing* anything.

Raising `ThreadPool.SetMinThreads` is the usual panicked response, and the docs warn against it: "unnecessarily increasing these values can cause performance problems… In most cases the thread pool will perform better with its own algorithm for allocating threads."[^threadpool-injection] It buys time; it doesn't fix a blocking call.

The signature is high latency, a growing queue and flat low CPU. The fix is to stop blocking: async all the way down, or synchronous all the way down. The mixture is what kills you.

## Primitives, and code that looks fine

Here's a snippet that appears in a lot of codebases:

```csharp
var results = new List<Result>();

Parallel.ForEach(items, item =>
{
    results.Add(Process(item));
});
```

`List<T>` is not thread-safe. `Add` reads a count, possibly resizes the backing array, writes an element, and increments the count — none of it atomic. Concurrent callers interleave, and you get torn state.

Over 100,000 items on a multi-core machine, it threw an `IndexOutOfRangeException` from inside `List.Add` and the list ended up with **8,194 items**. The worse outcome is the run where, with different timing, it doesn't throw at all and silently loses most of your data.

The fixes, in preference order:

```csharp
// Best: don't share mutable state at all
var results = items.AsParallel().Select(Process).ToList();   // add AsOrdered() if order matters

// Or for async work
var results = await Task.WhenAll(items.Select(ProcessAsync));

// If you must accumulate into a shared collection
var results = new ConcurrentBag<Result>();
Parallel.ForEach(items, item => results.Add(Process(item)));
```

### Task.WhenAll vs Parallel.ForEach

They solve different problems. `Task.WhenAll` is for **I/O-bound** work. It starts N asynchronous operations and waits for all of them; no threads are consumed while they're in flight. Its weakness is that it has no built-in concurrency limit — `Task.WhenAll` over 10,000 HTTP calls tries to start 10,000 HTTP calls.

`Parallel.ForEach` is for **CPU-bound** work. It partitions across pool threads and blocks until done. Handing it an async lambda is a common mistake — `Parallel.ForEach(items, async i => await ...)` takes an `Action`, so the lambda becomes `async void`, and the loop returns immediately having awaited nothing. `Parallel.ForEachAsync`, added in .NET 6, is the async-aware version, and it accepts a `ParallelOptions` carrying `MaxDegreeOfParallelism`.[^foreachasync]

### Choosing a lock

- `lock` — syntax sugar over `Monitor.Enter`/`Exit` with a `try`/`finally`, or, from C# 13 and .NET 9, over `Lock.EnterScope()` when you lock a dedicated `System.Threading.Lock`, which the docs recommend "for best performance".[^lock] Fast, reentrant, and **can't be held across an `await`**: the compiler forbids it (CS1996), because these locks are thread-affine ("the thread that acquired a lock must release the lock") and the continuation may resume on a different one.[^monitor-affinity]
- `Monitor` — the same primitive, unwrapped, when you need `TryEnter` with a timeout.
- `SemaphoreSlim` — a counter, not a lock. Not thread-affine, so `WaitAsync` *can* be awaited. This is the standard choice for async mutual exclusion, with a count of 1. Not reentrant: the same async flow waiting twice deadlocks itself.
- `Interlocked` — single atomic operations (`Increment`, `Exchange`, `CompareExchange`) implemented as CPU instructions, no kernel involvement. Much cheaper than a lock when a single counter or reference swap is all you need.

### Thread-safe is not atomic

`ConcurrentDictionary` is thread-safe: no operation corrupts it. That does not make *sequences* of operations atomic:

```csharp
if (!dict.ContainsKey(key))     // thread-safe
    dict[key] = Compute();      // thread-safe
                                // ...but the pair is a race
```

Between the check and the write, another thread can do the same thing. Both compute; one wins. This is precisely the cache stampede, and it's why `ConcurrentDictionary` provides `GetOrAdd` and `AddOrUpdate` as single composite operations.

Even `GetOrAdd` has a documented caveat: **the factory delegate is not run under a lock.** The dictionary "uses fine-grained locking to ensure thread safety… However, the `valueFactory` delegate is called outside the locks to avoid the problems that can arise from executing unknown code under a lock. Therefore, `GetOrAdd` is not atomic."[^getoradd-factory] The consequence follows directly: "If you call `GetOrAdd` simultaneously on different threads, `valueFactory` may be called multiple times, but only one key/value pair will be added to the dictionary."[^getoradd-factory]

For a cheap factory that's a harmless waste. For a factory that queries a database, it's the entire problem this post is about.

### Race conditions and deadlocks

A **race condition** is correctness depending on timing. A **deadlock** is two or more parties each holding what the other needs. The standard defence against deadlock is lock ordering: if every code path acquires locks in the same global order, a cycle is impossible.

Both are hostile to testing, because passing tests prove nothing about the interleaving you didn't hit. Prefer designs that make them structurally impossible (immutable data, message passing, single-writer ownership) over designs that need careful locking.

### Limiting concurrency

The concrete version of "10,000 items, at most 20 at a time":

```csharp
var gate = new SemaphoreSlim(20);

var tasks = items.Select(async item =>
{
    await gate.WaitAsync(cancellationToken);
    try { return await ProcessAsync(item); }
    finally { gate.Release(); }
});

var results = await Task.WhenAll(tasks);
```

All 10,000 tasks are created immediately, but only 20 get past the gate at once; the rest are suspended at `WaitAsync`, costing only their state machine. Measured, peak concurrency was exactly 20. The `try`/`finally` isn't optional: an exception between `WaitAsync` and `Release` permanently reduces the semaphore's capacity.

`Parallel.ForEachAsync` with `MaxDegreeOfParallelism = 20` does the same thing with less ceremony, and is the better default when you don't need the results as a materialised array.

## Building GetOrAddAsync

Now the real thing. The signature:

```csharp
Task<T> GetOrAddAsync<T>(string key, Func<Task<T>> factory);
```

The requirement that makes it interesting: **when N callers ask for the same missing key simultaneously, the factory must run exactly once.**

This is cache stampede (also called thundering herd or dogpiling). A popular key expires, every in-flight request misses, and every one queries the database: a hundred identical expensive queries at once. Latency spikes, connections run out, and the slowness lets even more requests pile in behind. It's a feedback loop, and it happens at peak traffic, because that's when the most requests are in flight.

### Attempt 1: the obvious one

```csharp
public async Task<T> GetOrAddAsync<T>(string key, Func<Task<T>> factory)
{
    if (_cache.TryGetValue(key, out T? hit)) return hit!;

    var value = await factory();
    _cache.Set(key, value, _ttl);
    return value;
}
```

It always returns the right value, and has no stampede protection: every caller arriving between `TryGetValue` returning false and `Set` completing runs the factory. With 100 concurrent callers against a factory taking 50 ms, **the factory ran 100 times**, every time, because 50 ms is an eternity next to the microseconds it takes 100 tasks to reach the same line.

### Attempt 2: a semaphore per key

The instinct is to lock. A single global lock would serialise every cache miss in the application, including unrelated keys, so: one lock per key.

```csharp
private readonly ConcurrentDictionary<string, SemaphoreSlim> _locks = new();

public async Task<T> GetOrAddAsync<T>(string key, Func<Task<T>> factory)
{
    if (_cache.TryGetValue(key, out T? hit)) return hit!;

    var gate = _locks.GetOrAdd(key, _ => new SemaphoreSlim(1, 1));
    await gate.WaitAsync();
    try
    {
        // Double-check: someone may have populated it while we waited.
        if (_cache.TryGetValue(key, out hit)) return hit!;

        var value = await factory();
        _cache.Set(key, value, _ttl);
        return value;
    }
    finally
    {
        gate.Release();
    }
}
```

100 callers, factory ran **once**. The 99 losers wait at `WaitAsync`, then hit the double-check and return the cached value without touching the factory. The double-check inside the lock is essential: without it, all 100 would run the factory one after another, which is worse than the naive version.

The problem is `_locks`. Nothing removes entries, so it grows with every key the process has ever seen, and on user-supplied keys that's a denial-of-service vector. The obvious fix is to remove the semaphore in the `finally`. **This is subtly broken:**

```csharp
finally
{
    gate.Release();
    _locks.TryRemove(key, out _);   // race
}
```

Consider three callers. A takes the gate. B calls `GetOrAdd`, receives *the same* semaphore, and waits. A finishes, releases, and removes the entry. C calls `GetOrAdd`, finds nothing, and creates a **new**, uncontended semaphore, so C walks straight into the critical section, where B, just released, already is. Two callers, one key, mutual exclusion gone.

![Three callers against one key over time. A acquires semaphore 1 and B waits on the same semaphore 1. A releases and removes the dictionary entry. C then finds no entry, creates semaphore 2 and enters at once, while B is released from semaphore 1 and also enters: two callers are inside the critical section together](semaphore-race.svg)

In the test this never fired, because by then the cache was populated and the double-check caught everyone. It bites when the double-check misses: A's factory threw, or the entry expired or was evicted in between, so it shows up with failure retries and short TTLs, under load, in production.

Doing removal correctly means reference-counting the waiters and only removing at zero, under a lock, which is enough bookkeeping to ask whether the whole approach is right. The pragmatic alternatives are to accept the growth with a bounded eviction policy, or to keep a fixed-size array of semaphores indexed by `key.GetHashCode()` and accept that unrelated keys occasionally share a gate.

### Attempt 3: cache the task, not the value

The reframing that makes the problem dissolve: instead of caching `T`, cache `Task<T>`.

A `Task<T>` is a value: it can be stored, handed to many callers, and awaited by all of them. While it's running, everyone awaiting it is waiting on the *same in-flight operation*, so there's nothing to synchronise.

```csharp
private readonly ConcurrentDictionary<string, Lazy<Task<T>>> _cache = new();

public Task<T> GetOrAddAsync(string key, Func<Task<T>> factory) =>
    _cache.GetOrAdd(key, _ => new Lazy<Task<T>>(
        factory, LazyThreadSafetyMode.ExecutionAndPublication)).Value;
```

100 callers, factory ran **once**. No locks, no double-check, three lines. (This sketch sits in a `TaskCache<T>` class, and a bare `ConcurrentDictionary` never expires anything, so in a real cache you'd store the `Lazy<Task<T>>` in an `IMemoryCache` with a TTL, as the earlier attempts do; the points below assume that.)

The `Lazy<T>` wrapper is doing specific work. `ConcurrentDictionary.GetOrAdd` may invoke its factory on several threads at once, so without `Lazy` several threads could each call `factory()` and start a database query, even though only one task gets stored. `LazyThreadSafetyMode.ExecutionAndPublication` means "only one thread at a time can initialize the `Lazy<T>` instance",[^lazy-mode] so the *creation of the task* is what's synchronised: `.Value` is computed exactly once, and `factory()` is invoked exactly once. Extra `Lazy` instances may be constructed and discarded, but constructing a `Lazy` is nearly free — it's `.Value` that matters.

The catch: **a faulted task stays cached.** Called three times against a factory that always throws, the factory ran **once**; the second and third callers got the cached faulted task and re-threw the original exception. If the database was down for one second, the key serves that error until the entry expires, or forever if it has no TTL.

So the `Lazy` approach needs failure eviction:

```csharp
public async Task<T> GetOrAddAsync(string key, Func<Task<T>> factory)
{
    var lazy = _cache.GetOrAdd(key, _ => new Lazy<Task<T>>(
        factory, LazyThreadSafetyMode.ExecutionAndPublication));
    try
    {
        return await lazy.Value;
    }
    catch
    {
        _cache.TryRemove(new KeyValuePair<string, Lazy<Task<T>>>(key, lazy));
        throw;
    }
}
```

The `TryRemove` overload taking a `KeyValuePair` matters: it "removes a key and value from the dictionary" only if the pair matches what's currently stored,[^tryremove-kvp] so a failing caller can't evict a healthy replacement that another thread installed in the meantime.

### Cancellation

If the factory takes a `CancellationToken` from the *first* caller, that caller cancelling kills the shared operation for everyone waiting on it: caller A hitting refresh fails callers B through Z. The correct shape is to decouple them: the shared factory runs with its own token (or one you cancel yourself once every waiter has gone, by counting them), while each caller awaits with their own:

```csharp
var shared = GetOrAddAsync(key, factory);
return await shared.WaitAsync(callerToken);
```

`Task.WaitAsync(CancellationToken)`, added in .NET 6, lets an individual caller stop waiting without disturbing the underlying operation.[^waitasync] The work continues, populates the cache, and the next caller benefits.

### Comparing the two

| | Semaphore-per-key | `Lazy<Task<T>>` |
|---|---|---|
| Factory runs once | Yes | Yes |
| Lines of code | ~20 plus lifecycle management | ~5 |
| Extra allocation | A `SemaphoreSlim` per key | A `Lazy` per key (cheap) |
| Cleanup | Genuinely hard to get right | The cache's own TTL, once stored in `IMemoryCache` |
| Failure behaviour | Retries naturally | Caches the failure — must evict |
| Late callers | Wait, then read the cached value | Await the same in-flight task |
| Global bottleneck | No, per key | No |

The `Lazy` approach is better. The semaphore approach is still worth understanding, because it generalises to anything that needs "only one of these at a time, per key".

### And now you don't have to

`HybridCache`, in the `Microsoft.Extensions.Caching.Hybrid` package, does all of this for you, and stampede protection is one of its headline features:[^hybridcache]

```csharp
var user = await hybridCache.GetOrCreateAsync(
    $"user:{id}",
    async ct => await _db.GetUserAsync(id, ct),
    cancellationToken: ct);
```

The guarantee, stated precisely: "A `HybridCache` instance ensures that only one concurrent caller for a given key calls the factory method, and all other callers using the same instance wait for the result of that call."[^hybridcache] It also handles cancellation the way the hand-rolled version had to be taught to — "the cancellation token passed to the factory is canceled when all callers awaiting the shared operation have canceled", so one impatient caller doesn't kill the shared work. You also get a two-level cache (in-memory L1 in front of a distributed L2 like Redis), configurable serialization, and tag-based invalidation.

That guarantee has a limit that's easy to miss: **the coordination is per instance.** "This coordination doesn't extend to other `HybridCache` instances, even if they use the same secondary distributed cache."[^hybridcache] So `HybridCache` solves the stampede *within* a process, exactly as `Lazy<Task<T>>` does. Ten instances behind a load balancer still send ten queries when a hot key expires; an L2 narrows the window, but nothing serialises the factory across machines. That takes a distributed lock, and a decision about whether its failure mode is worth it.

So the manual implementations above aren't what you should write today. They're what the API does for you, and knowing them lets you debug it.

## What to take from this

The through-line is that async is about *not occupying a thread while waiting*, and almost every async bug is a violation of that: blocking a thread with `.Result`, spawning unbounded concurrent operations, or serialising work behind a lock that didn't need to exist.


Next in the series: [LINQ, EF Core, and the SQL underneath]({{< relref "linq-ef-core-and-the-sql-underneath.md" >}}) — deferred execution, expression trees, and why an endpoint returning 50,000 rows takes fifteen seconds.

[^async-threads]: Microsoft, ["The Task asynchronous programming model"](https://learn.microsoft.com/en-us/dotnet/csharp/asynchronous-programming/task-asynchronous-programming-model) — the "Threads" section states that `async`/`await` create no extra threads and that an async method doesn't run on its own thread.
[^async-nowait]: Microsoft, ["The Task asynchronous programming model"](https://learn.microsoft.com/en-us/dotnet/csharp/asynchronous-programming/task-asynchronous-programming-model) — "If an async method doesn't use an `await` operator to mark a suspension point, the method executes as a synchronous method does… The compiler issues a warning for such methods."
[^async-statemachine]: Microsoft, ["IAsyncStateMachine Interface"](https://learn.microsoft.com/en-us/dotnet/api/system.runtime.compilerservices.iasyncstatemachine) — the compiler-generated type representing a suspended async method, and its `MoveNext` method.
[^valuetask-rules]: Microsoft, ["ValueTask&lt;TResult&gt; Struct"](https://learn.microsoft.com/en-us/dotnet/api/system.threading.tasks.valuetask-1) — the list of operations that must never be performed, the "results are undefined" warning, and the guidance that `Task` should remain the default.
[^threadpool-injection]: Microsoft, ["ThreadPool Class"](https://learn.microsoft.com/en-us/dotnet/api/system.threading.threadpool) — thread creation and destruction to optimise throughput, and the caution against raising `SetMinThreads`.
[^getoradd-factory]: Microsoft, ["ConcurrentDictionary&lt;TKey,TValue&gt;.GetOrAdd Method"](https://learn.microsoft.com/en-us/dotnet/api/system.collections.concurrent.concurrentdictionary-2.getoradd) — the `valueFactory` runs outside the locks, `GetOrAdd` is not atomic, and the factory may be called multiple times.
[^async-void]: Microsoft, ["Async return types: Void return type"](https://learn.microsoft.com/en-us/dotnet/csharp/asynchronous-programming/async-return-types#void-return-type) — the caller can't catch exceptions from an async void method, and "such unhandled exceptions are likely to cause your application to fail".
[^lock]: Microsoft, ["The lock statement"](https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/statements/lock) — `lock` on a `System.Threading.Lock` compiles to `EnterScope()`, otherwise to `Monitor.Enter`/`Exit`; no `await` in the body; from .NET 9 and C# 13, lock a dedicated `Lock` instance for best performance.
[^cleary]: Stephen Cleary, ["Async/Await - Best Practices in Asynchronous Programming"](https://learn.microsoft.com/en-us/archive/msdn-magazine/2013/march/async-await-best-practices-in-asynchronous-programming), MSDN Magazine, March 2013 — exceptions from async void methods are raised on the SynchronizationContext active when the method started.
[^monitor-affinity]: Microsoft, ["Monitor Class"](https://learn.microsoft.com/en-us/dotnet/api/system.threading.monitor) — "Because the Monitor class has thread affinity, the thread that acquired a lock must release the lock by calling the Monitor.Exit method."
[^foreachasync]: Microsoft, ["Parallel.ForEachAsync Method"](https://learn.microsoft.com/en-us/dotnet/api/system.threading.tasks.parallel.foreachasync) — introduced in .NET 6; overloads accepting `ParallelOptions`.
[^lazy-mode]: Microsoft, ["LazyThreadSafetyMode Enum"](https://learn.microsoft.com/en-us/dotnet/api/system.threading.lazythreadsafetymode) — `ExecutionAndPublication` allows only one thread to initialise the instance.
[^tryremove-kvp]: Microsoft, ["ConcurrentDictionary&lt;TKey,TValue&gt;.TryRemove Method"](https://learn.microsoft.com/en-us/dotnet/api/system.collections.concurrent.concurrentdictionary-2.tryremove) — the `KeyValuePair` overload removes only when the stored value matches.
[^waitasync]: Microsoft, ["Task.WaitAsync Method"](https://learn.microsoft.com/en-us/dotnet/api/system.threading.tasks.task.waitasync) — added in .NET 6; completes when the task completes or the token is cancelled.
[^hybridcache]: Microsoft, ["HybridCache library in ASP.NET Core"](https://learn.microsoft.com/en-us/aspnet/core/performance/caching/hybrid) — single-caller factory guarantee, factory cancellation semantics, and the explicit statement that coordination does not extend across `HybridCache` instances.
