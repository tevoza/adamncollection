---
title: "LINQ, EF Core, and the SQL Underneath"
date: 2026-08-07T10:00:00+02:00
draft: false
tags: ["software", "csharp", "dotnet", "linq", "ef-core", "dotnet-deep-dives"]
description: "Deferred execution, IQueryable vs IEnumerable, expression trees, N+1, tracking — and a full walkthrough of why an endpoint returning 50,000 rows takes fifteen seconds."
---

LINQ is the feature that makes C# pleasant, and EF Core is the reason a lot of C# is slow. Those two facts are related: LINQ is so uniform that the same syntax means "iterate this list" in one place and "generate SQL and round-trip to a database" in another, with nothing in the code to mark the difference.

Fluency with LINQ is easy. What matters is being able to say what a given query turns into — how many round trips, how much data, and when.

## Deferred execution

The single most important thing about LINQ: most operators don't do anything when you call them.

```csharp
var result = users
    .Where(x => x.IsActive)
    .Select(x => x.Name);
```

Nothing has happened yet. No filtering, no projection, no iteration. `Where` returned an object that *knows how* to filter, and `Select` wrapped that in an object that knows how to project. You've built a description of a computation.

The work happens when something enumerates it — a `foreach`, or a **greedy** operator that must consume the sequence to produce its answer: `ToList`, `ToArray`, `ToDictionary`, `Count`, `Sum`, `Any`, `First`, `Single`. Add `.ToList()` to the chain above and the query runs.

The elegant consequence is composability. `Where(a).Where(b)` doesn't iterate twice; it builds a pipeline traversed once, item at a time. `.Where(x => x.IsActive).First()` doesn't filter a million records and take one — it pulls items until one matches, then stops.

The dangerous consequence is that a variable holding a query is not a variable holding results.

### Multiple enumeration

```csharp
var expensive = users.Where(u => IsEligible(u));   // a query, not results

if (expensive.Any())                  // enumeration 1
    foreach (var u in expensive)      // enumeration 2 — runs the whole thing again
        Console.WriteLine(u.Name);
```

`IsEligible` runs over the collection twice. Against an in-memory list that's wasted CPU. Against a database it's two round trips. If the source is a stream, a reader, or anything else that can only be traversed once, the second enumeration returns nothing at all — or throws.

Worse is when the query has side effects, or when the underlying data changes between enumerations and the two passes silently disagree.

The fix is to materialise once with `.ToList()`. The tradeoff is that you've now committed to holding every result in memory and giving up streaming — which is the right call for a hundred rows and the wrong one for ten million. The rule of thumb: materialise at the point where you're done composing and about to use the results more than once.

Note that this cuts against a common habit. Returning `IEnumerable<T>` from a repository method looks like good encapsulation, but it hands the caller a query whose execution timing and cost are invisible — and if the `DbContext` is disposed by then, an exception. Return `IReadOnlyList<T>` when you mean "these are the results."

## IEnumerable vs IQueryable

They look almost identical and behave completely differently. This is the distinction that separates LINQ that works from LINQ that quietly downloads your database.

`IEnumerable<T>` takes `Func<T, bool>` — **compiled code**, a delegate. To filter, it must run that delegate against each item, in memory, in your process.

`IQueryable<T>` takes `Expression<Func<T, bool>>` — an **expression tree**, which is the lambda as *data*: a walkable object graph describing the comparison. EF Core traverses that tree and translates it into SQL.

The lambda you write is identical. What the compiler does with it depends entirely on the static type of what you're calling it on.

```csharp
// IQueryable — translated to SQL, filtered in the database
var a = _db.Users.Where(u => u.IsActive).ToList();
// SELECT ... FROM Users WHERE IsActive = 1

// IEnumerable — AsEnumerable() ends translation
var b = _db.Users.AsEnumerable().Where(u => u.IsActive).ToList();
// SELECT ... FROM Users     <- every row, then filtered in memory
```

The second version loads the entire table across the network and filters it in your process. On a table of ten million rows this is the difference between a working endpoint and an outage. And nothing in the code looks wrong.

The switch is thrown by anything that changes the static type to `IEnumerable<T>`: an explicit `.AsEnumerable()` or `.ToList()`, a `foreach`, or — most insidiously — a repository method typed to return `IEnumerable<T>`. Once that happens, every operator downstream runs in memory, and there is no warning.

The other way to trip it is calling something EF can't translate:

```csharp
_db.Users.Where(u => MyHelper.IsEligible(u))    // no SQL for this
```

EF Core 3.0 and later throw `InvalidOperationException` rather than silently falling back to client evaluation. This was a breaking change that caused a lot of grumbling and was absolutely the right decision — the old behaviour turned a translation failure into a silent full-table download.

## Expression trees, briefly

An `Expression<Func<T, bool>>` is a tree of nodes. `u => u.Age > 18` becomes a `LambdaExpression` whose body is a `BinaryExpression` of type `GreaterThan`, with a `MemberExpression` (`u.Age`) on the left and a `ConstantExpression` (`18`) on the right.

That's what makes provider translation possible: EF walks the tree and emits `[Age] > 18`. It's also what makes the closed set of translatable operations unavoidable — a provider can only translate nodes it recognises, so arbitrary method calls have nowhere to go.

You can build them yourself, which is how dynamic filtering and sorting get implemented without string concatenation:

```csharp
// Compose predicates conditionally — still fully translatable
IQueryable<User> q = _db.Users;
if (activeOnly)        q = q.Where(u => u.IsActive);
if (minAge is int age) q = q.Where(u => u.Age >= age);
var results = await q.ToListAsync();
```

Because nothing executes until `ToListAsync`, this produces one SQL statement with exactly the predicates that applied. This is the correct pattern for dynamic queries, and it beats a string-built `WHERE` clause on both safety and readability.

## Select vs SelectMany

`Select` is a one-to-one mapping: N in, N out. `SelectMany` is one-to-many followed by a flatten: N in, however-many out.

```csharp
orders.Select(o => o.Items)      // IEnumerable<List<OrderItem>>  — nested
orders.SelectMany(o => o.Items)  // IEnumerable<OrderItem>        — flat
```

In query syntax, `SelectMany` is what a second `from` clause compiles to — it's the join-like operation.

## First, FirstOrDefault, Single, SingleOrDefault

Four methods encoding two independent questions: *may it be missing?* and *may there be more than one?*

| | 0 matches | 1 match | 2+ matches |
|---|---|---|---|
| `First` | throws | value | **first one** |
| `FirstOrDefault` | `default` | value | **first one** |
| `Single` | throws | value | throws |
| `SingleOrDefault` | `default` | value | throws |

The choice is a documentation decision as much as a behavioural one. `Single` says "this is unique and I want to know loudly if that's ever false." `First` says "there may be several and I only care about one" — which is why `First` without an `OrderBy` is a smell: without ordering, "first" is whatever the database felt like returning.

There's a performance asymmetry too. `First` can stop at the first match; `Single` must scan for a second one to prove uniqueness. Against SQL, `First` emits `TOP 1` while `Single` emits `TOP 2` so it can detect the duplicate. Don't use `Single` on a large unindexed column as a way of being careful — it costs more than you think.

## The cost of ToList

`.ToList()` allocates a `List<T>` and fills it, which means it always costs the enumeration plus the allocation. Three specific consequences:

**It ends streaming.** Everything is in memory at once. Fine for a page of results, ruinous for a full table.

**It ends SQL translation.** `.ToList().Where(...)` filters in memory over everything you just loaded. `.Where(...).ToList()` filters in the database. The order of these two calls is the whole ballgame.

**It forces the round trip early**, which is sometimes exactly what you want — to release a `DbContext`, to make the timing explicit, or to stop a lazy query from executing in a `finally` block or a serializer.

`ToList` isn't a performance problem. `ToList` in the wrong place is.

## Tracking

By default EF Core **tracks** every entity a query returns: it keeps a snapshot in the `DbContext`'s change tracker so `SaveChanges` can work out what changed. That snapshot costs memory and CPU proportional to the number of entities and their properties.

For a read-only query, it's pure waste:

```csharp
var users = await _db.Users.AsNoTracking().ToListAsync();
```

On large read-only result sets this is one of the easiest wins available — often 20–50% off the query's total cost, entirely from not building change-tracking state.

Tracking also has a correctness dimension people meet by accident: within one `DbContext`, a query returning an entity that's already tracked gives you back **the tracked instance**, not fresh data from the database. This is identity resolution, and it means a second query won't show you changes another process made if you already have that row loaded. `AsNoTracking` sidesteps it by never consulting the tracker.

Use tracking when you intend to modify and save. Use `AsNoTracking` for everything else, which in a typical API is most queries.

## N+1

The classic ORM failure, and it's classic because the code looks completely reasonable:

```csharp
var orders = await _db.Orders.ToListAsync();      // 1 query

foreach (var order in orders)
    Console.WriteLine(order.Customer.Name);        // N queries, one per order
```

Each `order.Customer` access triggers a separate database round trip. A thousand orders means a thousand-and-one queries. Each is individually fast — a few milliseconds — which is exactly why it hides: nothing is slow, there's just an enormous number of them, and the total is dominated by network latency rather than database work.

EF Core disables lazy loading by default, which turns most of these into a `NullReferenceException` at development time instead of a performance disaster in production — a good trade. But lazy loading is easy to switch on (`UseLazyLoadingProxies`), and the pattern reappears whenever a loop touches a navigation property.

The fixes:

```csharp
// Eager loading — one query with a JOIN
await _db.Orders.Include(o => o.Customer).ToListAsync();

// Projection — best: only the columns you need
await _db.Orders
    .Select(o => new { o.Id, CustomerName = o.Customer.Name })
    .ToListAsync();
```

Detecting it is largely a matter of *looking at the SQL*, which brings us to the main event.

### Include has its own trap

`Include` on a collection navigation produces a JOIN, and a JOIN duplicates the parent row once per child. Load 100 orders each with 50 items and one JOIN returns 5,000 rows, with all the order columns repeated 50 times each. Stack two collection `Include`s and you get a cartesian product — 100 × 50 × 20 rows for data you could have fetched in three queries.

This is bad enough that EF Core added `AsSplitQuery()`, which issues one query per collection navigation and stitches the results together in memory. Trading one round trip for three is usually the right call when the alternative is an order of magnitude more rows over the wire.

Projection avoids the whole issue: select the shape you actually need and EF generates SQL that returns exactly that.

## Fifty thousand rows in fifteen seconds

Here's the scenario worth working through properly, because the instinct — "add an index" — is usually wrong, and the actual answer is almost always "return less data."

An endpoint returns 50,000 records and takes 15 seconds. How do you investigate?

**Start by finding where the time goes.** Fifteen seconds is a budget to be accounted for, and it can sit in any of: query execution in the database, transferring rows over the network, materialising entities in EF, serialising to JSON, or something entirely unrelated like a synchronous call in the middle of the pipeline. Guessing wastes the most time. Log the timings and split the budget before touching anything.

**Look at the generated SQL.** Not the LINQ — the SQL. Turn on `EnableSensitiveDataLogging` in development, or attach a profiler. This is where you discover the query you thought you wrote isn't the query being run: an accidental client evaluation, a cartesian explosion from stacked `Include`s, or a thousand small queries where you expected one. A large fraction of "slow EF query" investigations end here.

**Get the execution plan** for that SQL, from the database, not from EF. This tells you whether the database itself is the problem: a table scan where you expected a seek, a missing index on a foreign key or a filter column, a bad join order, or stale statistics. If the plan is clean and the query runs in 200ms in a SQL client, the database isn't your problem and no index will help.

**Then ask the real question: why 50,000 rows?** This is almost always where the answer lives, and everything above is just proving it.

- **Does the caller need them all?** Almost certainly not. Nobody renders 50,000 rows. If it's a UI, paginate — `Skip`/`Take`, or better, keyset pagination (`WHERE Id > @last ORDER BY Id`), which doesn't degrade as the offset grows the way `OFFSET` does. If it's an export, stream it with `IAsyncEnumerable` and write to the response as you go instead of buffering the lot.
- **Do you need every column?** `SELECT *` over a table with a couple of `nvarchar(max)` columns moves an enormous amount of data you're not using. Projecting to a DTO with the six fields the client renders can cut transfer by an order of magnitude on its own.
- **Are you tracking them?** 50,000 tracked entities means 50,000 snapshots. `AsNoTracking` on a read-only endpoint is free.
- **Are you loading a graph?** Check for `Include`s pulling collections and multiplying the row count.

**Then serialisation.** 50,000 objects to JSON is real work and real allocation. If the payload is genuinely required, `System.Text.Json` with source generation avoids the reflection cost, and streaming directly to the response body avoids buffering the whole document in memory. But this only matters once the row count is justified — optimising the serialisation of data you shouldn't be sending is solving the wrong problem.

**And allocations.** 50,000 entities plus 50,000 DTOs plus a JSON buffer is enough to push large arrays onto the Large Object Heap and trigger Gen 2 collections mid-request. That shows up as latency that gets *worse* under concurrent load, which is a useful signature.

The order matters: measure, read the SQL, get the plan, then attack the row count and the column count. Nine times out of ten the fix is pagination plus projection plus `AsNoTracking`, and the index everyone reached for first was never the bottleneck.

## A worked projection

Total spend per customer, from `Order` → `Customer` and `Order` → `List<OrderItem>`.

The version that looks fine and isn't:

```csharp
var orders = await _db.Orders
    .Include(o => o.Items)
    .Include(o => o.Customer)
    .ToListAsync();

var totals = orders
    .GroupBy(o => o.CustomerId)
    .ToDictionary(g => g.Key, g => g.Sum(o => o.Items.Sum(i => i.Price * i.Quantity)));
```

This loads every order, every line item, and every customer into memory — with the order columns duplicated once per item by the JOIN — to produce one decimal per customer. The aggregation happens in C# over data that never needed to leave the database.

```csharp
var totals = await _db.Orders
    .GroupBy(o => o.CustomerId)
    .Select(g => new
    {
        CustomerId = g.Key,
        Total = g.Sum(o => o.Items.Sum(i => i.Price * i.Quantity))
    })
    .ToDictionaryAsync(x => x.CustomerId, x => x.Total);
```

One SQL statement with a `GROUP BY` and a `SUM`, returning one row per customer. The database is very good at this; it's what it's for.

The general principle: **push aggregation down to the database, and pull back the smallest shape that answers the question.** Most EF performance work is applications of that one sentence.

## Transactions and SaveChanges

`SaveChanges` walks the change tracker, works out the INSERT/UPDATE/DELETE statements needed, orders them to respect foreign keys, and executes them **inside an implicit transaction**. One `SaveChanges` is atomic — all of it commits or none of it does.

That's the detail worth internalising, because it determines where your transaction boundaries should go. If your unit of work is a single `SaveChanges`, you already have atomicity and don't need an explicit transaction. You need `BeginTransaction` when you must span multiple `SaveChanges` calls, or combine EF work with raw SQL.

### Optimistic vs pessimistic concurrency

**Pessimistic** locks the row up front so nobody else can touch it. Correct, and it doesn't scale — it holds database locks across your application's think-time.

**Optimistic** doesn't lock. It detects, at write time, that someone else changed the row since you read it, and fails. The mechanism is a **concurrency token** — typically a `rowversion`/`timestamp` column:

```csharp
public class Product
{
    public int Id { get; set; }
    public decimal Price { get; set; }

    [Timestamp]
    public byte[] RowVersion { get; set; } = null!;
}
```

EF includes the token in the `WHERE` clause of the UPDATE: `WHERE Id = 5 AND RowVersion = 0x...`. If another transaction changed the row, the version no longer matches, zero rows are affected, and EF throws `DbUpdateConcurrencyException`. Without a token, that write is a silent last-writer-wins — the classic lost update.

Handling the exception means deciding a policy: reload and retry, surface the conflict to the user, or merge. There is no generic right answer, which is why EF makes you write it.

### DbContext lifetime

`DbContext` is **not thread-safe**, and this is not a soft warning — concurrent use corrupts the change tracker and throws `InvalidOperationException: A second operation was started on this context instance before a previous operation completed`.

It's designed to be short-lived: one per unit of work, which in a web app means one per request. `AddDbContext` registers it as **scoped**, which gives you exactly that.

The most common way to break it is to fire off parallel queries on one context:

```csharp
// Broken — one context, two concurrent operations
var usersTask  = _db.Users.ToListAsync();
var ordersTask = _db.Orders.ToListAsync();
await Task.WhenAll(usersTask, ordersTask);
```

If you genuinely need query parallelism, use `IDbContextFactory<T>` and give each operation its own context. Usually, though, two sequential awaits are fine and the parallelism wasn't worth it.

The other way to break it is to inject a `DbContext` into a singleton — which is the captive dependency problem, and it gets its own section in the [DI and failure boundaries post]({{< relref "di-lifetimes-and-failure-boundaries.md" >}}).

## What to take from this

LINQ's uniformity is a genuine achievement and the source of most of its hazards: the same expression means "filter a list" or "generate SQL" depending on a static type you can't see at the call site.

Two habits cover most of it. Know whether you're holding an `IQueryable` or an `IEnumerable` at every point in a chain. And when something's slow, read the SQL before you theorise — the gap between the query you wrote and the query that ran is where the time usually is.

Next: [memory, GC, and allocation]({{< relref "memory-gc-and-allocation.md" >}}) — including why a garbage-collected service still runs out of memory.
