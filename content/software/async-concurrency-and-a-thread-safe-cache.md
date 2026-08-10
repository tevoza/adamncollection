---
title: "Async, Concurrency, and a Thread-Safe Cache"
date: 2026-08-08T10:00:00+02:00
draft: false
tags: ["software", "csharp", "dotnet", "async", "concurrency", "dotnet-deep-dives", "ai-generated"]
description: "What async/await compiles to, why sync-over-async starves the thread pool, and three ways to build a cache where only one caller populates a missing value."
---

`async` and `await` are two of the best-designed features in C#, and they hide so much that it's entirely possible to ship async code for years with a mental model that's subtly wrong. Usually that's fine. It stops being fine under load, which is exactly when the failure is hardest to reproduce.

This post has two halves. The first takes the machinery apart. The second builds one thing properly — a cache where a hundred simultaneous callers cause exactly one database hit — because that single problem exercises almost everything in the first half.

Every number quoted below came out of a scratch console project on .NET 10, not from memory.

## async does not create a thread

This is the load-bearing misconception, so it goes first.

`async` is a compiler instruction. It says: rewrite this method as a state machine so it can be suspended and resumed. It does not start a thread, queue work, or introduce parallelism — the documentation is unusually blunt about this: "The `async` and `await` keywords don't cause extra threads to be created. Async methods don't require multithreading because an async method doesn't run on its own thread."[^async-threads] A method marked `async` containing no `await` at all runs entirely synchronously on the calling thread, and the compiler warns you about it.[^async-nowait]

What actually happens is closer to a callback transformation. The compiler splits your method at each `await` into a state machine implementing `IAsyncStateMachine`, with a `MoveNext()` method containing your code as a switch over states.[^async-statemachine] Roughly:

```csharp
public async Task<User> GetUserAsync(int id)
{
    var row = await _db.QueryAsync(id);   // suspension point
    return Map(row);
}
```

becomes, conceptually: run until `QueryAsync` returns a `Task`. If that task is already complete, keep going synchronously. If it isn't, hook a continuation onto it, **return an incomplete `Task` to the caller, and release the current thread entirely**. When the I/O completes, the continuation runs `MoveNext()` again, re-entering the method at state 1.

The thread isn't blocked while the database works. It isn't parked or sleeping. It's gone — back to the pool, serving other requests. That is the entire point of async, and it's why async improves *throughput* (requests served per thread) rather than *latency* (how long one request takes). An async request is often marginally slower than the sync equivalent; a server full of them handles vastly more load.

The corollary: `async` does nothing useful for CPU-bound work. Awaiting a computation doesn't make it faster, it just adds state machine overhead. For CPU-bound parallelism you want `Parallel.For`/`ForEachAsync` or explicit `Task.Run`, which genuinely do use multiple threads.

- **I/O-bound** — waiting on a network, disk, or database. There is no thread doing the work; the OS notifies on completion. Async is a pure win.
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

Microsoft's own guidance is that `Task` remains the default: "the default choice for any asynchronous method should be to return a `Task` or `Task<TResult>`. Only if performance analysis proves it worthwhile should a `ValueTask<TResult>` be used".[^valuetask-rules]

Use `ValueTask<T>` for hot-path methods that usually complete synchronously. Use `Task` everywhere else. It is the correct default, and "we changed everything to ValueTask for performance" is usually a change that made the code harder to reason about and no faster.

## async void

`async void` exists for exactly one reason: event handlers, whose signature the framework fixed before async existed. Everywhere else it is a bug waiting to happen.

The problem is that the caller gets no `Task`, so there is nothing to await and nothing to observe. An exception thrown inside an `async void` method cannot propagate to the caller — the caller has already moved on. Instead it's raised on the captured synchronization context, which in ASP.NET Core or a console app means **it crashes the process**. There's no try/catch you can put around the call site that will help, because by the time it throws, the call site has returned.

`async Task` returns a task that captures the exception, so it surfaces at the `await`. Prefer it always. If you're writing a fire-and-forget background operation, that's not a reason for `async void` — it's a reason to explicitly capture and log:

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

**The deadlock**, in any environment with a single-threaded synchronization context — classic ASP.NET, WinForms, WPF. The sequence: your thread calls `.Result` and blocks, holding the context. The async operation completes and tries to schedule its continuation *onto that same context*. The context is occupied by the thread waiting for the continuation. Neither can proceed. `ConfigureAwait(false)` inside the library fixes it by telling the continuation not to require the original context — which is why library code should use it and application code generally needn't.

Modern ASP.NET Core has **no** synchronization context, so this specific deadlock doesn't occur. That's led to a widespread belief that `.Result` is now safe. It isn't — the second failure is worse, because it scales.

**Thread-pool starvation.** Every blocked thread is a pool thread doing nothing but waiting. Beyond its minimum, the pool doesn't simply add a thread per queued work item — it "creates and destroys worker threads in order to optimize throughput", tuning the count with a hill-climbing algorithm rather than reacting instantly.[^threadpool-injection] That's the right behaviour when blocking is rare and brief, and exactly the wrong shape when every request is blocking: new threads arrive far slower than blocked ones accumulate. Meanwhile the queue grows. Latency climbs from milliseconds to seconds, then requests time out, and CPU usage sits near zero the whole time because nothing is actually *doing* anything.

Raising `ThreadPool.SetMinThreads` is the usual panicked response, and the docs warn against it directly: "unnecessarily increasing these values can cause performance problems… In most cases the thread pool will perform better with its own algorithm for allocating threads."[^threadpool-injection] It buys time; it doesn't fix a blocking call.

The signature is unmistakable once you've seen it: high latency, growing queue, flat low CPU. The fix is never "add more threads"; it's to stop blocking. Async all the way down, or synchronous all the way down — the mixture is what kills you.

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

Running that over 100,000 items on a multi-core machine, it threw an `IndexOutOfRangeException` from inside `List.Add` and the list ended up with **8,194 items**. Not 100,000. The important part isn't the exception — it's that on a different run, with different timing, it might not throw at all and simply lose 90% of your data silently.

The fixes, in preference order:

```csharp
// Best: don't share mutable state at all
var results = items.AsParallel().Select(Process).ToList();

// Or for async work
var results = await Task.WhenAll(items.Select(ProcessAsync));

// If you must accumulate into a shared collection
var results = new ConcurrentBag<Result>();
Parallel.ForEach(items, item => results.Add(Process(item)));
```

### Task.WhenAll vs Parallel.ForEach

They solve different problems and are not interchangeable.

`Task.WhenAll` is for **I/O-bound** work. It starts N asynchronous operations and waits for all of them; no threads are consumed while they're in flight. Its weakness is that it has no built-in concurrency limit — `Task.WhenAll` over 10,000 HTTP calls tries to start 10,000 HTTP calls.

`Parallel.ForEach` is for **CPU-bound** work. It partitions across pool threads and blocks until done. Handing it an async lambda is a common mistake — `Parallel.ForEach(items, async i => await ...)` takes an `Action`, so the lambda becomes `async void`, and the loop returns immediately having awaited nothing. `Parallel.ForEachAsync`, added in .NET 6, is the async-aware version, and it accepts a `ParallelOptions` carrying `MaxDegreeOfParallelism`.[^foreachasync]

### Choosing a lock

- `lock` — syntax sugar over `Monitor.Enter`/`Exit` with a `try`/`finally`. Fast, reentrant, and **cannot be held across an `await`**: the compiler forbids it (CS1996), because `Monitor` locks are thread-affine — "a synchronized block of code is owned by a thread" — and the continuation may resume on a different one.[^monitor-affinity]
- `Monitor` — the same primitive, unwrapped, when you need `TryEnter` with a timeout.
- `SemaphoreSlim` — a counter, not a lock. Not thread-affine, so `WaitAsync` *can* be awaited. This is the standard choice for async mutual exclusion, with a count of 1. Not reentrant: the same thread awaiting twice deadlocks itself.
- `Interlocked` — single atomic operations (`Increment`, `Exchange`, `CompareExchange`) implemented as CPU instructions, no kernel involvement. Enormously cheaper than a lock when a single counter or reference swap is all you need.

### Thread-safe is not atomic

`ConcurrentDictionary` is thread-safe: no operation corrupts it. That does not make *sequences* of operations atomic:

```csharp
if (!dict.ContainsKey(key))     // thread-safe
    dict[key] = Compute();      // thread-safe
                                // ...but the pair is a race
```

Between the check and the write, another thread can do the same thing. Both compute; one wins. This is precisely the cache stampede, and it's why `ConcurrentDictionary` provides `GetOrAdd` and `AddOrUpdate` as single composite operations.

Even `GetOrAdd` has a caveat worth knowing, and it's documented explicitly: **the factory delegate is not run under a lock.** The dictionary "uses fine-grained locking to ensure thread safety… However, the `valueFactory` delegate is called outside the locks to avoid the problems that can arise from executing unknown code under a lock. Therefore, `GetOrAdd` is not atomic."[^getoradd-factory] The consequence follows directly: "If you call `GetOrAdd` simultaneously on different threads, `valueFactory` may be called multiple times, but only one key/value pair will be added to the dictionary."[^getoradd-factory]

For a cheap factory that's a harmless waste. For a factory that queries a database, it's the entire problem this post is about.

### Race conditions and deadlocks

A **race condition** is correctness depending on timing. A **deadlock** is two or more parties each holding what the other needs. The standard defence against deadlock is lock ordering: if every code path acquires locks in the same global order, a cycle is impossible.

Both are hostile to testing, because passing tests prove nothing about the interleaving you didn't happen to hit. Prefer designs that make them structurally impossible — immutable data, message passing, single-writer ownership — over designs that need careful locking to be correct.

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

All 10,000 tasks are created immediately, but only 20 get past the gate at once; the rest are suspended at `WaitAsync` costing nothing but their state machine. Measured over 10,000 items, peak observed concurrency was exactly 20.

The `try`/`finally` is not optional. An exception between `WaitAsync` and `Release` permanently reduces the semaphore's capacity, and enough of those deadlock the system.

`Parallel.ForEachAsync` with `MaxDegreeOfParallelism = 20` does the same thing with less ceremony, and is the better default when you don't need the results as a materialised array.

## Building GetOrAddAsync

Now the real thing. The signature:

```csharp
Task<T> GetOrAddAsync<T>(string key, Func<Task<T>> factory);
```

The requirement that makes it interesting: **when N callers ask for the same missing key simultaneously, the factory must run exactly once.**

This is cache stampede — also called thundering herd or dogpiling. A popular key expires; every in-flight request misses; every one of them queries the database. The database, which was comfortably serving cached traffic, receives a hundred identical expensive queries at once. Latency spikes, connections exhaust, and the resulting slowness means even more requests pile in behind. It's a positive feedback loop, and it usually happens at peak traffic, because that's when the most requests are in flight when the key expires.

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

This is correct in the sense that it always returns the right value. It also has no stampede protection whatsoever: the gap between `TryGetValue` returning false and `Set` completing is wide open, and every caller arriving in that window runs the factory.

With 100 concurrent callers against a factory taking 50 ms, **the factory ran 100 times.** Not occasionally — every time, deterministically, because 50 ms is an eternity next to the microseconds it takes 100 tasks to reach the same line.

### Attempt 2: a semaphore per key

The instinct is to lock. A single global lock would work and would also serialise every cache miss in the application, including unrelated keys. So: one lock per key.

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

100 callers, factory ran **once**. The 99 losers wait at `WaitAsync`, then hit the double-check and return the cached value without touching the factory. The double-check inside the lock is essential — without it, all 100 would run the factory one after another, which is worse than the naive version.

The problem is `_locks`. Nothing removes entries, so it grows forever — a slow memory leak keyed by every cache key the process has ever seen. Unbounded on user-supplied keys, that's a denial-of-service vector.

The obvious fix is to remove the semaphore in the `finally`. **This is subtly broken**, and it's worth spelling out because it looks so reasonable:

```csharp
finally
{
    gate.Release();
    _locks.TryRemove(key, out _);   // race
}
```

Consider three callers. A takes the gate. B calls `GetOrAdd` and receives *the same* semaphore instance, then waits. A finishes, releases, and removes the entry from the dictionary. C now calls `GetOrAdd`, finds nothing, and creates a **new** semaphore — which is uncontended, so C proceeds straight into the critical section. B, meanwhile, has just been released and is also in the critical section. Two callers, one key, mutual exclusion gone.

In my test this never fired, because by then the cache was populated and the double-check caught everyone. That's the character of the bug: it's invisible until the timing is exactly wrong, which is to say, in production.

Doing removal correctly means reference counting the waiters and only removing at zero, under a lock — enough bookkeeping that it's worth asking whether the whole approach is right. The pragmatic alternatives are to accept the growth with a bounded eviction policy, or to keep a fixed-size array of semaphores indexed by `key.GetHashCode()` and accept that unrelated keys occasionally share a gate.

### Attempt 3: cache the task, not the value

The reframing that makes the problem dissolve: instead of caching `T`, cache `Task<T>`.

A `Task<T>` is a first-class value. It can be stored, handed to many callers, and awaited by all of them. If the task is still running, everyone awaiting it is waiting on the *same in-flight operation*. There's nothing to synchronise, because there's only ever one operation.

```csharp
private readonly ConcurrentDictionary<string, Lazy<Task<T>>> _cache = new();

public Task<T> GetOrAddAsync(string key, Func<Task<T>> factory) =>
    _cache.GetOrAdd(key, _ => new Lazy<Task<T>>(
        factory, LazyThreadSafetyMode.ExecutionAndPublication)).Value;
```

100 callers, factory ran **once**. No locks, no double-check, three lines.

The `Lazy<T>` wrapper is doing specific work. Recall that `ConcurrentDictionary.GetOrAdd` may invoke its factory on multiple threads concurrently. Without `Lazy`, several threads could each call `factory()` — starting several database queries — even though only one resulting task gets stored. `LazyThreadSafetyMode.ExecutionAndPublication` means "only one thread at a time can initialize the `Lazy<T>` instance",[^lazy-mode] so the *creation of the task* is what's synchronised: `.Value` is computed exactly once, and `factory()` is invoked exactly once. Extra `Lazy` instances may be constructed and discarded, but constructing a `Lazy` is nearly free — it's `.Value` that matters.

Now the catch, and it's a real one. **A faulted task stays cached.** Called three times against a factory that always throws, the factory ran **once**; the second and third callers received the cached faulted task and re-threw the original exception without retrying. The cache has memoised the failure. If the database was down for one second, the key serves that error until the entry expires — potentially forever, if the entry has no TTL.

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

Cancellation is genuinely awkward here, and it's where most hand-rolled caches quietly get it wrong.

If the factory takes a `CancellationToken` from the *first* caller, then that caller cancelling kills the shared operation for everyone waiting on it. That's rarely what you want — caller A hitting refresh shouldn't fail callers B through Z.

The correct shape is to decouple them: the shared factory runs with its own token (or a linked one that only cancels when *all* callers have gone away), while each caller awaits with their own:

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
| Cleanup | Genuinely hard to get right | Falls out of cache eviction |
| Failure behaviour | Retries naturally | Caches the failure — must evict |
| Late callers | Wait, then read the cached value | Await the same in-flight task |
| Global bottleneck | No, per key | No |

The `Lazy` approach is better, and it's the one the pre-`HybridCache` libraries converged on — LazyCache is built directly on `Lazy<Task<T>>` over `IMemoryCache`, which is where the name comes from.

The semaphore approach is still worth understanding, because the pattern generalises to things that aren't caches — anywhere you need "only one of these at a time, per key".

### And now you don't have to

`HybridCache`, in the `Microsoft.Extensions.Caching.Hybrid` package, does all of this for you, and stampede protection is one of its headline features:[^hybridcache]

```csharp
var user = await hybridCache.GetOrCreateAsync(
    $"user:{id}",
    async ct => await _db.GetUserAsync(id, ct),
    cancellationToken: ct);
```

The guarantee, stated precisely: "A `HybridCache` instance ensures that only one concurrent caller for a given key calls the factory method, and all other callers using the same instance wait for the result of that call."[^hybridcache] It also handles cancellation the way the hand-rolled version had to be taught to — "the cancellation token passed to the factory is canceled when all callers awaiting the shared operation have canceled", so one impatient caller doesn't kill the shared work. On top of that you get a two-level cache (in-memory L1 in front of a distributed L2 like Redis), configurable serialization, and tag-based invalidation.

Read that guarantee carefully, though, because it contains a limit that's easy to miss: **the coordination is per instance.** The docs spell it out — "This coordination doesn't extend to other `HybridCache` instances, even if they use the same secondary distributed cache."[^hybridcache] So `HybridCache` solves the stampede *within* a process, exactly as `Lazy<Task<T>>` does, and no better across processes. Ten instances behind a load balancer will still send ten queries to your database when a hot key expires. An L2 narrows the window — whichever instance finishes first populates Redis, and the others may hit it on their next attempt — but nothing in the library serialises the factory across machines. If you need that, you need a distributed lock, and you need to decide whether the added failure mode is worth it.

So the manual implementations above are not what you should write today. They're what's happening underneath the API you should be calling — and knowing that is the difference between using `HybridCache` and being able to reason about what it does when something goes wrong.

## What to take from this

The through-line is that async is about *not occupying a thread while waiting*, and almost every async bug is a violation of that: blocking a thread with `.Result`, spawning unbounded concurrent operations, or serialising work behind a lock that didn't need to exist.

The cache is a good exercise because getting it right requires holding several of these at once — atomicity versus thread-safety, where a lock helps and where reframing removes the need for one, and what happens when the thing you cached is a failure.

Next in the series: [LINQ, EF Core, and the SQL underneath]({{< relref "linq-ef-core-and-the-sql-underneath.md" >}}) — deferred execution, expression trees, and why an endpoint returning 50,000 rows takes fifteen seconds.

[^async-threads]: Microsoft, ["The Task asynchronous programming model"](https://learn.microsoft.com/en-us/dotnet/csharp/asynchronous-programming/task-asynchronous-programming-model) — the "Threads" section states that `async`/`await` create no extra threads and that an async method doesn't run on its own thread.
[^async-nowait]: Microsoft, ["The Task asynchronous programming model"](https://learn.microsoft.com/en-us/dotnet/csharp/asynchronous-programming/task-asynchronous-programming-model) — "If an async method doesn't use an `await` operator to mark a suspension point, the method executes as a synchronous method does… The compiler issues a warning for such methods."
[^async-statemachine]: Microsoft, ["IAsyncStateMachine Interface"](https://learn.microsoft.com/en-us/dotnet/api/system.runtime.compilerservices.iasyncstatemachine) — the compiler-generated type representing a suspended async method, and its `MoveNext` method.
[^valuetask-rules]: Microsoft, ["ValueTask&lt;TResult&gt; Struct"](https://learn.microsoft.com/en-us/dotnet/api/system.threading.tasks.valuetask-1) — the list of operations that must never be performed, the "results are undefined" warning, and the guidance that `Task` should remain the default.
[^threadpool-injection]: Microsoft, ["ThreadPool Class"](https://learn.microsoft.com/en-us/dotnet/api/system.threading.threadpool) — thread creation and destruction to optimise throughput, and the caution against raising `SetMinThreads`.
[^getoradd-factory]: Microsoft, ["ConcurrentDictionary&lt;TKey,TValue&gt;.GetOrAdd Method"](https://learn.microsoft.com/en-us/dotnet/api/system.collections.concurrent.concurrentdictionary-2.getoradd) — the `valueFactory` runs outside the locks, `GetOrAdd` is not atomic, and the factory may be called multiple times.
[^monitor-affinity]: Microsoft, ["Monitor Class"](https://learn.microsoft.com/en-us/dotnet/api/system.threading.monitor) — thread ownership of a synchronised block.
[^foreachasync]: Microsoft, ["Parallel.ForEachAsync Method"](https://learn.microsoft.com/en-us/dotnet/api/system.threading.tasks.parallel.foreachasync) — introduced in .NET 6; overloads accepting `ParallelOptions`.
[^lazy-mode]: Microsoft, ["LazyThreadSafetyMode Enum"](https://learn.microsoft.com/en-us/dotnet/api/system.threading.lazythreadsafetymode) — `ExecutionAndPublication` allows only one thread to initialise the instance.
[^tryremove-kvp]: Microsoft, ["ConcurrentDictionary&lt;TKey,TValue&gt;.TryRemove Method"](https://learn.microsoft.com/en-us/dotnet/api/system.collections.concurrent.concurrentdictionary-2.tryremove) — the `KeyValuePair` overload removes only when the stored value matches.
[^waitasync]: Microsoft, ["Task.WaitAsync Method"](https://learn.microsoft.com/en-us/dotnet/api/system.threading.tasks.task.waitasync) — added in .NET 6; completes when the task completes or the token is cancelled.
[^hybridcache]: Microsoft, ["HybridCache library in ASP.NET Core"](https://learn.microsoft.com/en-us/aspnet/core/performance/caching/hybrid) — single-caller factory guarantee, factory cancellation semantics, and the explicit statement that coordination does not extend across `HybridCache` instances.
