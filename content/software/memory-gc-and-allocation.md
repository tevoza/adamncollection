---
title: "Memory, GC, and Allocation"
date: 2026-08-06T10:00:00+02:00
draft: true
tags: ["software", "csharp", "dotnet", "performance", "dotnet-deep-dives"]
description: "Generational GC, the Large Object Heap, why using doesn't free anything, how events leak memory, and where Span<T> genuinely helps."
---

Garbage collection is the feature that lets most .NET developers never think about memory, which works well enough that when it stops working the vocabulary to describe the problem is often missing.

The question this post is organised around: **if the GC cleans everything up, why does a .NET service still run out of memory?**

## The generational heap

The GC's central bet is the *generational hypothesis*: most objects die young. A request allocates DTOs, strings, and temporaries that are garbage microseconds later. A few objects — caches, singletons, configuration — live for the process lifetime. Very little sits in between.

So the heap is split by age:

- **Gen 0** — where almost everything is allocated. Small, collected constantly, and very cheap.
- **Gen 1** — survivors of a Gen 0 collection. A buffer between short-lived and long-lived.
- **Gen 2** — survivors of Gen 1. Long-lived objects. Collected rarely and expensively, because it means walking the whole heap.

A Gen 0 collection is fast for a reason worth understanding: cost is proportional to the number of **surviving** objects, not the number of dead ones. Dead objects cost nothing to collect — the collector traces from the roots, copies out what's alive, and treats everything else as free space. Allocating a million short-lived objects that all die is genuinely cheap.

What's expensive is objects that survive long enough to be **promoted**. Every promotion moves work into a more expensive generation. This inverts the intuitive advice: the problem isn't allocating a lot, it's *keeping* a lot.

### The Large Object Heap

Objects of 85,000 bytes or more go on the **Large Object Heap** instead — in practice, arrays of any real size. A `byte[100_000]`, a `List<T>` that has grown past the threshold, a large string.

Two properties make the LOH a common source of trouble. It's collected only with Gen 2, so large objects hang around far longer than their lifetime suggests. And it is **not compacted by default** — freed blocks leave holes, and the allocator reuses them only when a new object fits.

That produces the failure mode people find surprising: a process with plenty of free memory that still throws `OutOfMemoryException`, because the free memory is scattered in fragments and nothing is contiguous enough for a 2MB array. You can force compaction with `GCSettings.LargeObjectHeapCompactionMode`, but it's a full blocking collection and a stopgap, not a fix. The real fix is not to churn large arrays — which is what `ArrayPool<T>` is for.

## Why using doesn't free anything

`IDisposable` and the GC solve **different problems**, and conflating them is the source of most confusion here.

The GC manages **managed memory**. It runs when it decides to, and you can't tell it precisely when.

`IDisposable` manages **everything else**: file handles, sockets, database connections, unmanaged buffers, OS locks. These are scarce in a way memory isn't — a process can hold a few thousand file handles, and the GC has no idea they're scarce because to it they're just a small object with an `IntPtr` field.

So:

```csharp
using var stream = File.OpenRead(path);
```

At the closing brace, `Dispose` runs and **the file handle is released**. The `FileStream` object itself is still on the heap, still occupying memory, until the GC gets to it — which may be seconds later, or never, if the process exits first.

`Dispose` releases resources. The GC releases memory. `using` does the first and says nothing about the second.

Finalizers are the safety net, and a bad one. An object with a finalizer can't be collected on the first pass — it goes on the finalizer queue, gets processed by a dedicated thread, and is only collectable on the *next* collection. That guarantees promotion, which is why the guidance is to implement a finalizer only when directly holding unmanaged resources, and to call `GC.SuppressFinalize(this)` in `Dispose` so the correct path skips the queue entirely.

`IAsyncDisposable` exists because `Dispose` is synchronous and some cleanup is genuinely I/O — flushing a buffer to disk, sending a close frame on a WebSocket, committing a transaction. Doing that in a synchronous `Dispose` means blocking, which in an async application means the thread-pool starvation described in the [async post]({{< relref "async-concurrency-and-a-thread-safe-cache.md" >}}). `await using` gives cleanup somewhere to await.

## Leaks in a garbage-collected language

A memory leak in .NET isn't memory the GC failed to free. It's memory that is still **reachable** and therefore still, by definition, live. The GC is working perfectly; something is holding a reference it shouldn't.

Common culprits:

**Static collections.** A `static Dictionary<string, T>` used as a cache with no eviction is a leak with a friendly name. Roots are permanent, so everything reachable from one is permanent.

**Event handlers.** The big one, below.

**Captured closures.** A lambda captures the variables it uses — and if it's an instance method's lambda referencing a field, it captures `this`, keeping the entire object alive. Store that lambda in a long-lived list and you've retained whatever it closed over.

**Timers and background tasks.** A `System.Threading.Timer` holds its callback, which holds its target. An unforgotten timer keeps its object graph alive forever.

**Unbounded queues and buffers.** Any producer/consumer where the producer outpaces the consumer. Not strictly a leak — the memory is in use — but it fails the same way.

### Events, specifically

This is the canonical .NET leak because the direction of the reference is the opposite of what the code reads like.

```csharp
public class Publisher            // long-lived — a singleton service
{
    public event EventHandler? DataChanged;
}

public class Subscriber           // short-lived — per request, per view
{
    public Subscriber(Publisher p) => p.DataChanged += OnDataChanged;
    private void OnDataChanged(object? s, EventArgs e) { }
}
```

The subscriber subscribes to the publisher, so it reads as though the subscriber depends on the publisher. But `+=` adds a delegate to the *publisher's* invocation list, and that delegate holds a reference to its target — the subscriber. **The publisher holds the subscriber alive.**

Create a subscriber per request against a singleton publisher, never unsubscribe, and every subscriber that ever existed stays reachable for the process lifetime, along with everything it references. Memory climbs steadily and only under real traffic, which makes it a production-only bug.

The fixes, in order of preference: unsubscribe in `Dispose` and make the subscriber's lifetime explicit; prefer a short-lived subscription object over a long-lived one; or, if the relationship genuinely must be loose, a weak event pattern — though needing one is usually a sign the design is wrong.

The general form of the rule: **a longer-lived object holding a reference to a shorter-lived one is where leaks come from.** That covers events, static caches, and captured closures in one sentence.

## Reducing allocation

Everything above is about objects living too long. This is the other half: not creating them in the first place.

### `Span<T>` and `Memory<T>`

`Span<T>` is a window over a contiguous block of memory — an array, a stack buffer, a slice of a string, unmanaged memory — that lets you read and write it without copying.

The classic win is string processing. Every `Substring` allocates a new string and copies characters:

```csharp
// Allocates: one string per part, plus the array
static (string Key, string Value) ParseNaive(string line)
{
    var i = line.IndexOf('=');
    return (line.Substring(0, i), line.Substring(i + 1));
}

// Allocates nothing — the spans point into the original string
static (ReadOnlySpan<char> Key, ReadOnlySpan<char> Value) ParseSpan(ReadOnlySpan<char> line)
{
    var i = line.IndexOf('=');
    return (line[..i], line[(i + 1)..]);
}
```

Slicing a span is pointer arithmetic: a new struct holding a reference and a length. No copy, no allocation, no GC pressure.

`Span<T>` is a `ref struct`, so it's stack-only and can't be stored in a field, boxed, or captured — [the language mechanics post]({{< relref "csharp-language-mechanics.md" >}}) covers why those constraints exist. `Memory<T>` is the heap-friendly counterpart for when you need to hold a window across an `await`; you call `.Span` on it at the point of use.

The honest caveat: `Span<T>` helps when you are **parsing or slicing on a hot path**. Rewriting incidental string handling in span-based style buys nothing and costs readability. The wins are in serializers, parsers, protocol handling, and tight loops — and the way to know is to measure allocations before and after, not to assume.

### `ArrayPool<T>`

When you need a large temporary buffer repeatedly, renting beats allocating:

```csharp
var buffer = ArrayPool<byte>.Shared.Rent(size);
try
{
    // buffer.Length may be LARGER than size — the pool rounds up
    var used = buffer.AsSpan(0, size);
    // ...
}
finally
{
    ArrayPool<byte>.Shared.Return(buffer, clearArray: true);
}
```

This keeps large arrays off the LOH and out of Gen 2. Three things to get right: the returned array is usually bigger than you asked for, so never use `buffer.Length` as the logical size; `Return` belongs in a `finally`; and pass `clearArray: true` if the buffer held anything sensitive, because the next renter sees whatever you left behind. Using a buffer after returning it is a genuinely nasty bug — another component may already have it.

### Strings

String concatenation in a loop is the textbook allocation mistake: strings are immutable, so each `+=` allocates a new one and copies everything so far. N concatenations, O(N²) copying.

`StringBuilder` maintains a growable buffer and copies once at the end. But it's not free — it allocates the builder and its chunks — so for a small fixed number of pieces it's slower than the alternatives.

Roughly: a fixed, small number of parts → interpolation (`$"{a}-{b}"`), which the compiler lowers to an efficient `DefaultInterpolatedStringHandler` that often avoids intermediate strings entirely. A loop or an unknown count → `StringBuilder`. Joining a collection → `string.Join`, which sizes the result correctly in one pass.

Note that interpolation in **logging** deserves separate care. `_logger.LogDebug($"user {id} did {thing}")` builds the string *before* the call, so it costs even when debug logging is disabled. The template form, `_logger.LogDebug("user {Id} did {Thing}", id, thing)`, defers formatting and gives you structured fields — though the arguments still box if they're value types.

## Finding it

Reasoning about allocation is useful; measuring it is what actually resolves questions.

For a quick answer in a test or a scratch app, `GC.GetAllocatedBytesForCurrentThread()` around a block gives an exact byte count, which is how the boxing numbers in the [language mechanics post]({{< relref "csharp-language-mechanics.md" >}}) were produced. For anything serious, BenchmarkDotNet with `[MemoryDiagnoser]` reports allocations per operation alongside timings, and it handles the warmup and statistics you'd otherwise get wrong.

For a running process, `dotnet-counters` gives you live Gen 0/1/2 collection counts, heap size, and allocation rate with no setup. `dotnet-gcdump` captures a heap snapshot you can open in Visual Studio to see what's retaining what — which is the tool for a leak, because the question with a leak is never "what's allocated" but "what's still holding a reference to it."

The signature to learn: **memory that grows and never returns to baseline after load stops** is a leak. Memory that spikes under load and recovers is allocation pressure — a throughput problem, not a correctness one. They look similar on a dashboard and need completely different fixes.

## So why does it still run out of memory?

Collecting the answers:

- Something **reachable** is accumulating — a static cache, an event subscription, a growing queue. The GC can't help; the memory is live by definition.
- The **LOH is fragmented**, so allocation fails despite free memory being available.
- **Allocation rate outruns collection**, so the heap grows faster than Gen 0 can clear it and objects get promoted to Gen 2 purely by surviving the pressure they collectively created.
- **Unmanaged memory** — native handles, unmanaged buffers, memory-mapped files — isn't GC-visible at all. A leaked handle shows in process RSS and not in the managed heap.
- The GC is **deliberately not collecting**, because Server GC on a machine with plenty of RAM sees no reason to. High memory usage is not the same as a memory problem.

That last one is worth ending on. Server GC trades memory for throughput on purpose, and a container showing steadily high memory may be a perfectly healthy process behaving as designed — right up until it meets a container memory limit and gets killed. Configuring the GC's heap limit to match the container limit is not optional in Kubernetes, and it's the one memory setting most services get wrong.

Next: [DI, lifetimes, and failure boundaries]({{< relref "di-lifetimes-and-failure-boundaries.md" >}}).
