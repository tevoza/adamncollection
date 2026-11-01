---
title: "LINQ, EF Core, and the SQL Underneath"
date: 2026-08-07T10:00:00+02:00
lastmod: 2026-11-01T13:56:00+02:00
draft: false
tags: ["software", "csharp", "dotnet", "linq", "ef-core", "dotnet-deep-dives", "ai-generated"]
description: "Deferred execution, IQueryable vs IEnumerable, expression trees, N+1, tracking — and a full walkthrough of why an endpoint returning 50,000 rows takes fifteen seconds."
---

LINQ is the feature that makes C# pleasant, and EF Core is the reason a lot of C# is slow. Those two facts are related: LINQ is so uniform that the same syntax means "iterate this list" in one place and "generate SQL and round-trip to a database" in another, with nothing in the code to mark the difference.

What matters is being able to say what a given query turns into: how many round trips, how much data, and when.

## Deferred execution

The most important thing about LINQ: most operators don't do anything when you call them.

```csharp
var result = users
    .Where(x => x.IsActive)
    .Select(x => x.Name);
```

Nothing has happened yet. `Where` returned an object that *knows how* to filter, and `Select` wrapped that in an object that knows how to project: a description of a computation.

The work happens when something enumerates it: a `foreach`, or an operator that executes immediately. `ToList`, `ToArray`, `ToDictionary`, `Count` and `Sum` read everything; `Any`, `First` and `Single` read only as far as they need. Add `.ToList()` to the chain above and the query runs.

The good consequence is composability. `Where(a).Where(b)` builds a pipeline traversed once, item at a time, and `.Where(x => x.IsActive).First()` pulls items until one matches, then stops.

The dangerous consequence is that a variable holding a query is not a variable holding results.

### Multiple enumeration

```csharp
var expensive = users.Where(u => IsEligible(u));   // a query, not results

if (expensive.Any())                  // enumeration 1
    foreach (var u in expensive)      // enumeration 2 — runs the whole thing again
        Console.WriteLine(u.Name);
```

`IsEligible` runs over the collection twice. Against an in-memory list that's wasted CPU. Against a database it's two round trips. If the source can only be traversed once (a stream, a reader), the second enumeration returns nothing or throws; if the data changes in between, the two passes silently disagree.

The fix is to materialise once with `.ToList()`, which commits you to holding every result in memory: right for a hundred rows, wrong for ten million. Materialise where you're done composing and about to use the results more than once.

This cuts against a common habit. Returning `IEnumerable<T>` from a repository looks like good encapsulation, but it hands the caller a query whose timing and cost are invisible, and an exception if the `DbContext` is disposed by then. Return `IReadOnlyList<T>` when you mean "these are the results."

## IEnumerable vs IQueryable

They look almost identical and behave completely differently; this is the distinction between LINQ that works and LINQ that quietly downloads your database.

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

The second version loads the entire table across the network and filters it in your process. On ten million rows that's the difference between a working endpoint and an outage, and nothing in the code looks wrong.

![The same Where lambda on two static types. On IQueryable it becomes an expression tree that EF translates, so the database runs WHERE IsActive = 1 and only matching rows cross the network. On IEnumerable it becomes a compiled delegate, so the database sends every row and the filter runs in the application process](queryable-vs-enumerable.svg)

The switch is thrown by anything that leaves you holding something other than an `IQueryable<T>`: an explicit `.AsEnumerable()`, a `.ToList()` followed by more operators, or (most insidiously) a repository method typed to return `IEnumerable<T>`. From there every operator runs in memory, with no warning.

The other way to trip it is calling something EF can't translate:

```csharp
_db.Users.Where(u => MyHelper.IsEligible(u))    // no SQL for this
```

EF Core 3.0 and later throw `InvalidOperationException` rather than silently falling back to client evaluation.[^ef-clienteval] That breaking change was the right call: the old behaviour turned a translation failure into a silent full-table download. The one place client evaluation is still allowed is the top-level projection, where you can call arbitrary C# in a final `Select` because the rows have already been fetched.

## Expression trees, briefly

An `Expression<Func<T, bool>>` is a tree of nodes. `u => u.Age > 18` becomes a `LambdaExpression` whose body is a `BinaryExpression` of type `GreaterThan`, with a `MemberExpression` (`u.Age`) on the left and a `ConstantExpression` (`18`) on the right.

EF walks the tree and emits `[Age] > 18`. It's also why the set of translatable operations is closed: a provider can only translate methods it has a SQL mapping for, so your own helpers have nowhere to go. Composing `Where` calls is how dynamic filtering works without string concatenation:

```csharp
// Compose predicates conditionally — still fully translatable
IQueryable<User> q = _db.Users;
if (activeOnly)        q = q.Where(u => u.IsActive);
if (minAge is int age) q = q.Where(u => u.Age >= age);
var results = await q.ToListAsync();
```

Because nothing executes until `ToListAsync`, this produces one SQL statement with exactly the predicates that applied, which beats a string-built `WHERE` clause on safety and readability.

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

The choice documents intent as much as behaviour. `Single` says "this is unique and I want to know loudly if that's ever false." `First` says "there may be several and I only care about one" — which is why `First` without an `OrderBy` is a smell: without ordering, "first" is whatever the database felt like returning.

There's a performance asymmetry too: `Single` must look for a second match to prove uniqueness, so on an unindexed column, proving there's no second row can mean scanning the whole table.

## The cost of ToList

`.ToList()` costs the enumeration plus the allocation, with three consequences:

**It ends streaming.** Everything is in memory at once. Fine for a page of results, ruinous for a full table.

**It ends SQL translation.** `.ToList().Where(...)` filters in memory over everything you just loaded. `.Where(...).ToList()` filters in the database.

**It forces the round trip early**, which is sometimes exactly what you want: to release a `DbContext`, make the timing explicit, or stop a lazy query executing in a `finally` block or a serializer. `ToList` isn't a performance problem; `ToList` in the wrong place is.

## Tracking

By default EF Core **tracks** every entity a query returns: it keeps a snapshot in the `DbContext`'s change tracker so `SaveChanges` can work out what changed. That snapshot costs memory and CPU proportional to the number of entities and their properties.

For a read-only query, it's pure waste:

```csharp
var users = await _db.Users.AsNoTracking().ToListAsync();
```

The documentation puts it plainly: no-tracking queries "are generally quicker to execute because there's no need to set up the change tracking information".[^ef-tracking] How much quicker depends on the entity count and shape, so measure it on your own query; the point is that for a read-only endpoint the saving is free.

Tracking also has a correctness dimension people meet by accident: within one `DbContext`, "EF Core checks if the entity is already in the context. If EF Core finds an existing entity, then the same instance is returned",[^ef-tracking] and it does not overwrite that instance's values with the ones just read from the database. This is identity resolution: a second query won't show you changes another process made if you already have that row loaded.

`AsNoTracking` never consults the tracker, which has its own consequence: a no-tracking query returns "a new instance of the entity even when the same entity is contained in the result multiple times".[^ef-tracking] If you need deduplicated instances without tracking, `AsNoTrackingWithIdentityResolution()` deduplicates them without tracking them in the context.

Use tracking when you intend to modify and save. Use `AsNoTracking` for everything else, which in a typical API is most queries.

## N+1

The classic ORM failure, and it's classic because the code looks completely reasonable:

```csharp
var orders = await _db.Orders.ToListAsync();      // 1 query

foreach (var order in orders)
    Console.WriteLine(order.Customer.Name);        // N queries, one per order
```

With lazy loading on, each `order.Customer` access is a separate round trip: a thousand orders, up to a thousand-and-one queries. Each is fast, which is why it hides; the total is dominated by network latency rather than database work.

EF Core disables lazy loading by default, which turns most of these into a `NullReferenceException` in development instead of a performance disaster in production. But lazy loading is easy to switch on (`UseLazyLoadingProxies`), and the same shape reappears whenever a loop issues a query per item.

The fixes:

```csharp
// Eager loading — one query with a JOIN
await _db.Orders.Include(o => o.Customer).ToListAsync();

// Projection — best: only the columns you need
await _db.Orders
    .Select(o => new { o.Id, CustomerName = o.Customer.Name })
    .ToListAsync();
```

Detecting it is largely a matter of *looking at the SQL*.

### Include has its own trap

`Include` on a collection navigation produces a JOIN, which repeats the parent row once per child: 100 orders with 50 items each come back as 5,000 rows. Add a second collection `Include`, say 20 payments per order, and you get a cartesian product: 100 × 50 × 20 = 100,000 rows for data three queries could fetch as 7,100.

![Two collection Includes on 100 orders. As one query, the JOIN returns 100 × 50 × 20 = 100,000 rows, each repeating the order's columns. As a split query, EF sends three queries returning 100 orders, 5,000 items and 2,000 payments, 7,100 rows in all, and stitches them together in memory](include-explosion.svg)

EF Core 5.0 added `AsSplitQuery()` for this: one query per collection navigation, stitched together in memory.[^ef-splitquery] Three round trips usually beat an order of magnitude more rows over the wire. The tradeoff: split queries aren't executed in a single transaction by default, so the data can shift between them.

Projection avoids the whole issue: select the shape you actually need and EF generates SQL that returns exactly that.

## Fifty thousand rows in fifteen seconds

An endpoint returns 50,000 records and takes 15 seconds. The instinct, "add an index", is usually wrong, and the answer is almost always "return less data".

**Start by finding where the time goes.** Fifteen seconds can sit in query execution, transferring rows, materialising entities, serialising JSON, or something unrelated like a synchronous call mid-pipeline. Log the timings and split the budget before touching anything.

**Look at the generated SQL.** Not the LINQ — the SQL. EF Core logs the commands it executes at `Information` level under the `Database.Command` category, so raising that in development is usually enough;[^ef-logging] add `EnableSensitiveDataLogging` if you also need the parameter *values* (and note the name is a warning — it will put user data in your logs). Or attach a profiler. This is where you discover the query that ran isn't the one you thought you wrote: an accidental client evaluation, a cartesian explosion, or a thousand small queries where you expected one. Many "slow EF query" investigations end here.

**Get the execution plan** for that SQL from the database. It tells you whether the database is the problem: a scan where you expected a seek, a missing index on a foreign key or filter column, a bad join order, stale statistics. If the plan is clean and the query runs in 200 ms in a SQL client, no index will help.

**Then ask the real question: why 50,000 rows?**

- **Does the caller need them all?** Almost certainly not; nobody renders 50,000 rows. If it's a UI, paginate — `Skip`/`Take`, or better, keyset pagination (`WHERE Id > @last ORDER BY Id`), which doesn't degrade as the offset grows the way `OFFSET` does. If it's an export, stream it with `IAsyncEnumerable` (no-tracking, or the change tracker holds every row anyway) and write to the response as you go.
- **Do you need every column?** `SELECT *` over a table with a couple of `nvarchar(max)` columns moves a lot of data you're not using. Projecting to a DTO with the six fields the client renders can cut transfer by an order of magnitude.
- **Are you tracking them?** 50,000 tracked entities means 50,000 snapshots.
- **Are you loading a graph?** Check for `Include`s pulling collections and multiplying the row count.

**Then serialisation.** If the payload is genuinely required, `System.Text.Json` source generation avoids the reflection cost and streaming to the response body avoids buffering the whole document. But optimising the serialisation of data you shouldn't be sending is solving the wrong problem.

**And allocations.** 50,000 entities plus 50,000 DTOs plus a JSON buffer is enough to push large arrays onto the Large Object Heap and trigger Gen 2 collections mid-request, which shows up as latency that gets *worse* under concurrent load.

So: measure, read the SQL, get the plan, then attack the row and column counts. Usually the fix is pagination plus projection plus `AsNoTracking`, and the index everyone reached for first was never the bottleneck.

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

This loads every order, line item and customer into memory, with the order columns repeated per item by the JOIN, to produce one decimal per customer.

```csharp
var totals = await _db.Orders
    .SelectMany(o => o.Items, (o, i) => new { o.CustomerId, Amount = i.Price * i.Quantity })
    .GroupBy(x => x.CustomerId)
    .Select(g => new { CustomerId = g.Key, Total = g.Sum(x => x.Amount) })
    .ToDictionaryAsync(x => x.CustomerId, x => x.Total);
```

Flattening to order lines first keeps the shape simple enough for EF to translate: one SQL statement with a JOIN, a `GROUP BY` and a `SUM`, returning one row per customer (customers with no orders are absent, so read it with `TryGetValue`).

The general principle: **push aggregation down to the database, and pull back the smallest shape that answers the question.** Most EF performance work applies that one sentence.

## Transactions and SaveChanges

`SaveChanges` walks the change tracker, works out the INSERT/UPDATE/DELETE statements needed, orders them to respect foreign keys, and executes them **inside an implicit transaction**. One `SaveChanges` is atomic — all of it commits or none of it does.[^ef-transactions]

That decides where your transaction boundaries go. If your unit of work is a single `SaveChanges`, you already have atomicity. You need `BeginTransaction` to span multiple `SaveChanges` calls, or to combine them with raw SQL or the set-based `ExecuteUpdateAsync`/`ExecuteDeleteAsync`, which run immediately as their own statements, outside the change tracker and any `SaveChanges` transaction, so a later `SaveChanges` of tracked entities can overwrite what they wrote.[^ef-executeupdate]

### Optimistic vs pessimistic concurrency

**Pessimistic** locks the row up front so nobody else can touch it. Correct, and it doesn't scale: it holds database locks across your application's think-time.

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

EF includes the token in the `WHERE` clause of the UPDATE: `WHERE Id = 5 AND RowVersion = 0x...`. If another transaction changed the row, the version no longer matches, zero rows are affected, and EF throws `DbUpdateConcurrencyException`.[^ef-concurrency] Without a token, that write is a silent last-writer-wins — the classic lost update.

Handling the exception means choosing a policy (reload and retry, surface the conflict, or merge), which is why EF makes you write it.

### DbContext lifetime

`DbContext` is **not thread-safe**. EF Core detects the overlap and throws an `InvalidOperationException` reporting that "a second operation was started on this context instance before a previous operation completed" — the message goes on to name the cause as different threads sharing one instance.[^ef-dbcontext] Where it isn't detected, you get change-tracker corruption instead, which is worse because it's silent.

It's designed to be short-lived: one per unit of work, which in a web app means one per request. `AddDbContext` registers it as **scoped** by default, which gives you exactly that.[^ef-dbcontext-lifetime]

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

LINQ's uniformity is the source of most of its hazards: the same expression means "filter a list" or "generate SQL" depending on a static type you can't see at the call site.

Two habits cover most of it. Know whether you're holding an `IQueryable` or an `IEnumerable` at every point in a chain. And when something's slow, read the SQL before you theorise: the gap between the query you wrote and the query that ran is usually where the time is.

Next: [memory, GC, and allocation]({{< relref "memory-gc-and-allocation.md" >}}) — including why a garbage-collected service still runs out of memory.

[^ef-tracking]: Microsoft, ["Tracking vs. No-Tracking Queries"](https://learn.microsoft.com/en-us/ef/core/querying/tracking) — no-tracking queries are "generally quicker to execute"; identity resolution returns the tracked instance; no-tracking returns a new instance per occurrence; `AsNoTrackingWithIdentityResolution`.
[^ef-clienteval]: Microsoft, ["Client vs. Server Evaluation"](https://learn.microsoft.com/en-us/ef/core/querying/client-eval) — EF Core 3.0 stopped silently evaluating arbitrary expressions on the client; top-level projections remain the exception.
[^ef-splitquery]: Microsoft, ["Single vs. Split Queries"](https://learn.microsoft.com/en-us/ef/core/querying/single-split-queries) — cartesian explosion from multiple collection `Include`s, `AsSplitQuery`, and the consistency tradeoff.
[^ef-logging]: Microsoft, ["Simple Logging"](https://learn.microsoft.com/en-us/ef/core/logging-events-diagnostics/simple-logging) — EF Core logs executed commands; `EnableSensitiveDataLogging` additionally logs parameter values.
[^ef-dbcontext]: Microsoft, ["DbContext Lifetime, Configuration, and Initialization"](https://learn.microsoft.com/en-us/ef/core/dbcontext-configuration/) — `DbContext` is not thread-safe, and the exception raised when operations overlap.
[^ef-dbcontext-lifetime]: Microsoft, ["DbContext Lifetime, Configuration, and Initialization"](https://learn.microsoft.com/en-us/ef/core/dbcontext-configuration/) — `AddDbContext` registers the context as a scoped service by default; `IDbContextFactory` for other lifetimes.
[^ef-concurrency]: Microsoft, ["Handling Concurrency Conflicts"](https://learn.microsoft.com/en-us/ef/core/saving/concurrency) — concurrency tokens in the `WHERE` clause, and `DbUpdateConcurrencyException` when no rows match.
[^ef-executeupdate]: Microsoft, ["ExecuteUpdate and ExecuteDelete"](https://learn.microsoft.com/en-us/ef/core/saving/execute-insert-update-delete) — they "take effect immediately", are "completely unaware of EF's change tracker", and don't implicitly start a transaction.
[^ef-transactions]: Microsoft, ["Using Transactions"](https://learn.microsoft.com/en-us/ef/core/saving/transactions) — a single `SaveChanges` is applied in a transaction.
