---
title: ".NET Deep Dives"
date: 2026-08-10T10:00:00+02:00
draft: false
tags: ["software", "csharp", "dotnet", "dotnet-deep-dives", "ai-generated"]
description: "A six-part series on the parts of C# and .NET where correct-looking code behaves badly — async, EF Core, memory, DI, and failure design."
---

C# is a language you can be productive in long before you understand it. That's mostly a compliment. The cost is a category of bug that doesn't look like a bug: code that reads correctly, passes review, works in development, and behaves badly under concurrency, load, or partial failure.

This series is about that category. Six posts, each on an area where the gap between what the code says and what the runtime does is wide enough to fall into.

The organising question throughout is **what does this actually turn into** — how many round trips, how many allocations, which thread, and what happens when it fails halfway through. Every measured claim came out of a scratch project rather than memory, and where something surprised me I've said so.

Because these posts are AI-written, the specific claims in them are footnoted against Microsoft's .NET documentation — thresholds, defaults, version-introduced behaviour, and API guarantees all cite the page they came from. That pass was worth doing for its own sake: it caught several things the first drafts got wrong, including a confident and completely incorrect claim about how far `HybridCache`'s stampede protection reaches. Where a claim couldn't be sourced, it has been softened or removed rather than left to sound authoritative. Treat an uncited number as an opinion.

## The posts

**[C# Language Mechanics]({{< relref "csharp-language-mechanics.md" >}})** — the ground floor. Value and reference semantics, why `==` and `Equals` disagree, what `readonly` does and doesn't promise, `ref`/`in`/`out`, boxing, variance, delegates and events, pattern matching and records.

**[Async, Concurrency, and a Thread-Safe Cache]({{< relref "async-concurrency-and-a-thread-safe-cache.md" >}})** — what `async` compiles to, why it doesn't create threads, and how `.Result` starves a thread pool. Then the centerpiece: building a cache where a hundred simultaneous callers cause exactly one database hit, three different ways, and what each gets wrong.

**[LINQ, EF Core, and the SQL Underneath]({{< relref "linq-ef-core-and-the-sql-underneath.md" >}})** — deferred execution, the `IQueryable`/`IEnumerable` distinction that decides whether filtering happens in SQL or in your process, N+1, tracking, and a full walkthrough of why an endpoint returning 50,000 rows takes fifteen seconds.

**[Memory, GC, and Allocation]({{< relref "memory-gc-and-allocation.md" >}})** — generational collection, the Large Object Heap, why `using` doesn't free memory, how event handlers leak, where `Span<T>` genuinely helps, and why a garbage-collected service still runs out of memory.

**[DI Lifetimes and Failure Boundaries]({{< relref "di-lifetimes-and-failure-boundaries.md" >}})** — captive dependencies and why a singleton can't hold a `DbContext`, `throw` versus `throw ex`, how exceptions move through async code, and deciding which layer should catch a failure.

**[Designing an API That Survives Failure]({{< relref "designing-an-api-that-survives-failure.md" >}})** — a payment API where requests arrive twice and downstream services fail: idempotency keys, unique constraints, retries, circuit breakers, outboxes. Plus the ASP.NET Core machinery underneath, a testing strategy, and an argument for when *not* to adopt Clean Architecture, CQRS, or microservices.

## A recurring theme

Reading them back, the same idea shows up in three separate places under three names.

The cache stampede is solved by pushing atomicity down to `Lazy<Task<T>>` rather than coordinating callers with locks. Idempotency is solved by pushing atomicity down to a unique constraint rather than checking for duplicates in application code. Both are the same move: **a check followed by an act is a race, and the fix is to find the layer that can do both at once.**

The third is EF's `GroupBy` — push the aggregation down to the database rather than pulling rows up to aggregate them. Different problem, same instinct: do the work at the layer that's built for it, rather than reimplementing it one level up with less information.

If there's a single thing worth taking from the series, it's probably that.
