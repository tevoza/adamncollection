---
title: "Memory, GC, and Allocation"
date: 2026-08-06T10:00:00+02:00
lastmod: 2026-11-01T15:12:00+02:00
draft: false
tags: ["software", "csharp", "dotnet", "performance", "dotnet-deep-dives", "ai-generated"]
description: "Generational GC, the Large Object Heap, why using doesn't free anything, how events leak memory, and where Span<T> genuinely helps."
---

Garbage collection lets most .NET developers never think about memory, which works well enough that when it stops working the vocabulary to describe the problem is often missing. This post is organised around one question: **if the GC cleans everything up, why does a .NET service still run out of memory?**

## The generational heap

The GC's central bet is the *generational hypothesis*: most objects die young. A request allocates DTOs, strings, and temporaries that are garbage microseconds later. A few objects — caches, singletons, configuration — live for the process lifetime. Very little sits in between.

So the heap is split by age:

- **Gen 0** — where almost everything is allocated. Small, collected constantly, and very cheap.
- **Gen 1** — survivors of a Gen 0 collection. A buffer between short-lived and long-lived.
- **Gen 2** — survivors of Gen 1. Long-lived objects. Collected rarely and expensively, because it means walking the whole heap.

![The managed heap by generation. New objects are allocated in Gen 0, which is collected often and cheaply; most die there. Survivors are promoted to Gen 1, then to Gen 2, which is collected rarely and walks the whole heap. Objects of 85,000 bytes or more go straight to the Large Object Heap, which is collected only with Gen 2 and swept rather than compacted](generations.svg)

A Gen 0 collection is fast because its cost is proportional to the **surviving** objects, not the dead ones: the collector traces from the roots, keeps what's alive, and treats everything else as free space. Allocating a million short-lived objects that all die is cheap.[^gc-fundamentals]

What's expensive is objects that survive long enough to be **promoted**, moving work into a more expensive generation. The problem isn't allocating a lot, it's *keeping* a lot.

### The Large Object Heap

Objects of 85,000 bytes or more go on the **Large Object Heap** instead: in practice, arrays of any real size, such as a `byte[100_000]`, the backing array of a `List<T>` that has grown past the threshold, or a large string. "If an object is greater than or equal to 85,000 bytes in size, it's considered a large object",[^loh] and the figure "was determined by performance tuning"[^loh] (it can be raised via `System.GC.LOHThreshold`).[^gc-config]

Two properties make the LOH a common source of trouble. "Large objects belong to generation 2 because they are collected only during a generation 2 collection",[^loh] so they hang around far longer than their lifetime suggests. And it is **not compacted by default** — "because compaction is expensive, the GC *sweeps* the LOH; it makes a free list out of dead objects that can be reused later",[^loh] so freed blocks leave holes that get reused only when a new object fits.

The result is fragmentation: holes too small for the next array, so the GC commits more memory instead. On a 64-bit machine that mostly shows up as a heap far bigger than its live data; under a container's hard limit it can end in `OutOfMemoryException` with plenty of free space inside the heap, though the runtime "might also compact the LOH automatically".[^loh] You can force a compaction with `GCSettings.LargeObjectHeapCompactionMode`, or have the GC compact a fragmented LOH automatically with a non-zero `System.GC.ConserveMemory`,[^gc-config] but the real fix is not to churn large arrays, which is what `ArrayPool<T>` is for.

## Why using doesn't free anything

`IDisposable` and the GC solve **different problems**. The GC manages **managed memory**, when it decides to. `IDisposable` manages **everything else**: file handles, sockets, database connections, unmanaged buffers, OS locks. These are scarce in a way memory isn't, and the GC can't tell, because to it a handle is just a small object with an `IntPtr` field.

```csharp
using var stream = File.OpenRead(path);
```

At the closing brace, `Dispose` runs and **the file handle is released**. The `FileStream` object is still on the heap until the GC gets to it. `Dispose` releases resources, the GC releases memory, and `using` does only the first.

Finalizers are the safety net, and a bad one. An object with a finalizer survives the collection that finds it unreachable: it's kept alive until a dedicated thread runs its finalizer, and only collectable on a later collection of the generation it was promoted to. That guarantees promotion, which is why the guidance is to wrap unmanaged handles in a `SafeHandle` rather than write finalizers, and to call `GC.SuppressFinalize(this)` in `Dispose` so the correct path skips finalization entirely.[^finalizers]

`IAsyncDisposable` exists because some cleanup is I/O (flushing a buffer, sending a WebSocket close frame, committing a transaction), and doing that in a synchronous `Dispose` means blocking, with the thread-pool starvation described in the [async post]({{< relref "async-concurrency-and-a-thread-safe-cache.md" >}}). `await using` gives cleanup somewhere to await.

## Leaks in a garbage-collected language

A memory leak in .NET isn't memory the GC failed to free. It's memory that is still **reachable** and therefore, by definition, live: something is holding a reference it shouldn't. Common culprits:

**Static collections.** A `static Dictionary<string, T>` used as a cache with no eviction is a leak with a friendly name. Roots are permanent, so everything reachable from one is permanent.

**Event handlers.** The big one, below.

**Captured closures.** A lambda that references a field captures `this`, keeping the entire object alive. Store that lambda in a long-lived list and you've retained whatever it closed over.

**Timers and background tasks.** A running `System.Threading.Timer` holds its callback, and so its target, until the timer is disposed; the object usually keeps the timer reachable through its own field.

**Unbounded queues and buffers.** A producer outpacing its consumer. Not strictly a leak, but it fails the same way.

### Events, specifically

This is the canonical .NET leak because the reference points the opposite way from how the code reads.

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

It reads as though the subscriber depends on the publisher. But `+=` adds a delegate to the *publisher's* invocation list, and that delegate holds a reference to its target, the subscriber. **The publisher holds the subscriber alive.**

![Reference direction in an event subscription. The code reads as Subscriber depending on Publisher. In memory, the long-lived Publisher's DataChanged invocation list holds a delegate whose target is each Subscriber, so every subscriber created per request stays reachable as long as the publisher does](event-leak.svg)

Create a subscriber per request against a singleton publisher, never unsubscribe, and every subscriber that ever existed stays reachable for the process lifetime. Memory climbs steadily and only under real traffic, which makes it a production-only bug.

The fixes, in order of preference: unsubscribe in `Dispose` and make the subscriber's lifetime explicit; prefer a short-lived subscription object; or, if the relationship must be loose, a weak event pattern, though needing one usually means the design is wrong.

The general rule: **a longer-lived object holding a reference to a shorter-lived one is where leaks come from.** That covers events, static caches and captured closures.

## Reducing allocation

Everything above is about objects living too long. The other half is not creating them in the first place.

### `Span<T>` and `Memory<T>`

`Span<T>` is a window over contiguous memory (an array, a stack buffer, part of a string, unmanaged memory) that lets you read and write it without copying. The classic win is string processing, where every `Substring` allocates and copies:

```csharp
// Allocates two new strings
static bool TryParse(string line, out string key, out string value)
{
    var i = line.IndexOf('=');
    if (i < 0) { key = value = ""; return false; }
    key = line.Substring(0, i);
    value = line.Substring(i + 1);
    return true;
}

// Allocates nothing — the spans point into the original string
static bool TryParse(ReadOnlySpan<char> line, out ReadOnlySpan<char> key, out ReadOnlySpan<char> value)
{
    var i = line.IndexOf('=');
    if (i < 0) { key = value = default; return false; }
    key = line[..i];
    value = line[(i + 1)..];
    return true;
}
```

Slicing a span is pointer arithmetic: a new struct holding a reference and a length. No copy, no allocation, no GC pressure.[^span]

`Span<T>` is a `ref struct`, so it's stack-only and can't be stored in a field, boxed, or captured — [the language mechanics post]({{< relref "csharp-language-mechanics.md" >}}) covers why those constraints exist. `Memory<T>` is the heap-friendly counterpart for when you need to hold a window across an `await`; you call `.Span` on it at the point of use.

The caveat: `Span<T>` helps when you're **parsing or slicing on a hot path** (serializers, parsers, protocol handling, tight loops). Rewriting incidental string handling in span style buys nothing and costs readability; measure allocations before and after.

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

This keeps large arrays from churning the LOH. Three things to get right: the array is usually bigger than you asked for, so never use `buffer.Length` as the logical size; `Return` belongs in a `finally`; and pass `clearArray: true` (as here) if the buffer held anything sensitive, because the next renter sees what you left. Using a buffer after returning it is a nasty bug, since another component may already have it.[^arraypool]

### Strings

String concatenation in a loop is the textbook allocation mistake: strings are immutable, so each `+=` copies everything so far, O(N²) in total. `StringBuilder` keeps a growable buffer and copies once at the end, but it allocates the builder and its chunks, so for a few fixed pieces it's slower than the alternatives. Roughly: a fixed, small number of parts → interpolation (`$"{a}-{b}"`), which from C# 10 the compiler lowers using `DefaultInterpolatedStringHandler`, often avoiding intermediate strings entirely[^interpolation]. A loop or an unknown count → `StringBuilder`. Joining a collection → `string.Join`, which sizes the result correctly in one pass.

Interpolation in **logging** deserves separate care. `_logger.LogDebug($"user {id} did {thing}")` builds the string *before* the call, so it costs even when debug logging is off. The template form, `_logger.LogDebug("user {Id} did {Thing}", id, thing)`, defers formatting and gives you structured fields, though value-type arguments still box.

## Finding it

Reasoning about allocation is useful; measuring it settles questions. For a quick answer in a test or a scratch app, `GC.GetAllocatedBytesForCurrentThread()` around a block gives a byte count[^allocated-bytes], which is how the boxing numbers in the [language mechanics post]({{< relref "csharp-language-mechanics.md" >}}) were produced. For anything serious, BenchmarkDotNet with `[MemoryDiagnoser]` reports allocations per operation alongside timings, with the warmup and statistics you'd otherwise get wrong.

For a running process, `dotnet-counters` shows live Gen 0/1/2 collection counts, heap size and allocation rate. `dotnet-gcdump` captures a heap snapshot you can open in Visual Studio to see what's retaining what, which is the tool for a leak: the question is never "what's allocated" but "what's still holding a reference to it".

The signature to learn: **memory that grows and never returns to baseline after load stops** is a leak. Memory that spikes under load and recovers is allocation pressure, a throughput problem. They look similar on a dashboard and need different fixes.

## So why does it still run out of memory?

- Something **reachable** is accumulating — a static cache, an event subscription, a growing queue. The GC can't help; the memory is live by definition.
- The **LOH is fragmented**, so the heap grows past its live data, and past a container limit.
- **Allocation rate outruns collection**, so the heap grows faster than Gen 0 can clear it and objects get promoted to Gen 2 purely by surviving the pressure they collectively created.
- **Unmanaged memory** — native handles, unmanaged buffers, memory-mapped files — isn't GC-visible at all. A leaked handle shows in process RSS and not in the managed heap.
- The GC is **deliberately not collecting**, because Server GC on a machine with plenty of RAM sees no reason to (less so with DATAS, below). High memory usage is not the same as a memory problem.

Server GC has traditionally traded memory for throughput on purpose, so a container showing steadily high memory may be a healthy process behaving as designed.

It's often claimed you must configure the GC's heap limit by hand in Kubernetes. That's out of date. The runtime treats the container limit as total physical memory and defaults the heap hard limit to **75%** of it.[^gc-config] Two more defaults point the same way: with Server GC, DATAS (which sizes the heap to roughly the long-lived data rather than the machine) is on by default from .NET 9, and the GC gets more aggressive about full compacting collections once memory load reaches about 90%.[^gc-config]

So the setting you're most likely to need is `System.GC.HeapHardLimitPercent`, and only to make the heap *smaller* than 75% because something else in the container needs memory.

Next: [DI, lifetimes, and failure boundaries]({{< relref "di-lifetimes-and-failure-boundaries.md" >}}).

[^loh]: Microsoft, ["Large object heap (LOH)"](https://learn.microsoft.com/en-us/dotnet/standard/garbage-collection/large-object-heap) — the 85,000-byte threshold, LOH collection with gen 2, sweeping rather than compaction, and the allocation/collection cost model.
[^gc-config]: Microsoft, ["Garbage collector config settings"](https://learn.microsoft.com/en-us/dotnet/core/runtime-config/garbage-collector) — `System.GC.LOHThreshold`; the 75% default heap hard limit percent in a memory-constrained environment; the ~90% high-memory threshold; `ConserveMemory` compacting a fragmented LOH; DATAS (a Server GC setting) enabled by default from .NET 9.
[^gc-fundamentals]: Microsoft, ["Fundamentals of garbage collection"](https://learn.microsoft.com/en-us/dotnet/standard/garbage-collection/fundamentals) — generations, promotion of survivors, and what triggers a collection.
[^finalizers]: Microsoft, ["Cleaning up unmanaged resources"](https://learn.microsoft.com/en-us/dotnet/standard/garbage-collection/unmanaged) and ["Object.Finalize"](https://learn.microsoft.com/en-us/dotnet/api/system.object.finalize) — finalization requires at least two collections, `GC.SuppressFinalize` skips it, and "we recommend that you use a safe handle instead of providing your own finalizer".
[^span]: Microsoft, ["System.Span&lt;T&gt;"](https://learn.microsoft.com/en-us/dotnet/api/system.span-1) and ["Memory&lt;T&gt; and Span&lt;T&gt; usage guidelines"](https://learn.microsoft.com/en-us/dotnet/standard/memory-and-spans/memory-t-usage-guidelines) — stack-only semantics, slicing without copying, and when to prefer `Memory<T>`.
[^arraypool]: Microsoft, ["ArrayPool&lt;T&gt; Class"](https://learn.microsoft.com/en-us/dotnet/api/system.buffers.arraypool-1) — `Rent` may return an array longer than requested, and `Return`'s `clearArray` parameter.
[^interpolation]: Microsoft, ["String interpolation in C#"](https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/tokens/interpolated) — from C# 10 the compiler lowers interpolated strings using `DefaultInterpolatedStringHandler`.
[^allocated-bytes]: Microsoft, ["GC.GetAllocatedBytesForCurrentThread"](https://learn.microsoft.com/en-us/dotnet/api/system.gc.getallocatedbytesforcurrentthread) — total bytes allocated on the current thread, used for the allocation figures quoted in this series.
