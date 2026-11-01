---
title: "C# Language Mechanics: What the Keywords Actually Do"
date: 2026-08-09T10:00:00+02:00
lastmod: 2026-11-01T13:48:00+02:00
draft: false
tags: ["software", "csharp", "dotnet", "dotnet-deep-dives", "ai-generated"]
description: "Value and reference semantics, equality, readonly, ref/in/out, boxing, variance, delegates and events, pattern matching, and records — what each one actually costs and guarantees."
---

Most C# is written without ever needing to know what the compiler does with it. That works right up until it doesn't: a struct that mutates itself back to its old value, an event handler that keeps a page alive for the lifetime of the process, an equality check that quietly compares references when you meant values.

This post is the ground floor of the series: the language semantics everything else sits on. It's a survey; the async, EF and failure-design posts are where things get long.

## Value types and reference types

The distinction people recite is "structs go on the stack, classes go on the heap." That's approximately true and unhelpfully so. The real distinction is **what a variable holds**: a value type variable holds the value; a reference type variable holds a reference to a value.

Where that memory lives is an implementation detail. A `struct` field inside a `class`, a struct captured by a lambda, and a struct in an `async` method that survives an `await` all live on the heap. The observable consequence is copy semantics:

![Assignment copies what the variable holds. Value type: s1 and s2 each hold their own PointStruct, so setting s2.X to 99 leaves s1.X at 1. Reference type: c1 and c2 each hold a reference to the same PointClass object on the heap, so setting c2.X to 99 is visible through c1](copy-semantics.svg)

```csharp
struct PointStruct { public int X; }
class PointClass { public int X; }

var s1 = new PointStruct { X = 1 };
var s2 = s1;          // copies the value
s2.X = 99;
// s1.X is still 1

var c1 = new PointClass { X = 1 };
var c2 = c1;          // copies the reference
c2.X = 99;
// c1.X is now 99
```

### Passing a reference type to a method

The answer is "by value", which sounds wrong. C# passes everything by value by default, and for a reference type the *thing being copied* is the reference, so the method gets its own copy of a reference to the same object:

```csharp
void Mutate(List<int> list) => list.Add(1);   // caller sees this
void Reassign(List<int> list) => list = new List<int>();   // caller does not
```

`Mutate` follows the reference and changes the shared object. `Reassign` overwrites the method's own copy and the caller never notices. Add `ref` and `Reassign` would work, because the parameter is then an alias for the caller's variable.

## class vs struct vs record

These are three axes, not three points, which is why the comparison is usually muddled:

- `class` — reference type, reference equality by default.
- `struct` — value type, value equality by default (though the default implementation is slow; see below).
- `record` — a *modifier* on either. `record` (or `record class`) is a reference type with compiler-generated value equality. `record struct` is a value type with a better-generated equality implementation than a plain struct gets.

What `record` actually generates for you: a value-based `Equals`/`GetHashCode`, a `ToString` that prints members, a `Deconstruct` for positional records, and a copy constructor backing the `with` expression.[^records]

```csharp
public record Money(decimal Amount, string Currency);

var a = new Money(10m, "ZAR");
var b = new Money(10m, "ZAR");
a == b;                          // true — value equality
var c = a with { Amount = 20m }; // non-destructive mutation
```

Reach for a record when the type's identity *is* its data — DTOs, domain events, value objects, cache keys, anything you'd want to compare by content or use as a dictionary key. Reach for a class when the object has identity independent of its field values, which is most entities and every service.

The `with` expression makes immutability affordable: "the same thing, but with one field different" without a constructor call listing every unchanged member.

One caveat on structs: the default `ValueType.Equals` takes an `object` (so the argument is boxed) and compares field by field, and Microsoft recommends overriding it, "particularly if your value type contains fields that are reference types".[^valuetypeequals] If a struct is used as a dictionary key on a hot path, implement `IEquatable<T>` — or make it a `record struct` and let the compiler do it.

## Equality: ==, Equals, and ReferenceEquals

Three mechanisms that answer three different questions.

`ReferenceEquals(a, b)` asks whether two references point at the same object. It can't be overridden. On value types it's always false, because both arguments get boxed into distinct objects.

`Equals` is virtual — it dispatches on the runtime type of the object. This is the one that respects a type's own definition of equality.

`==` is an operator, which means it's resolved by the compiler **at compile time based on the static type**. It is not virtual. That's the whole source of the confusion:

```csharp
object x = "hello";
object y = "hel" + Console.ReadLine();  // "hello" at runtime, not interned

x == y;          // false — operator resolved for object, i.e. reference comparison
x.Equals(y);     // true  — virtual dispatch reaches string.Equals
```

`string` overloads `==` to do value comparison, but only when the compiler knows both sides are `string`.[^equality] Widen either side to `object` and you silently get reference comparison back.

The practical rule: in generic code, never use `==` on `T` unless you've constrained it. Use `EqualityComparer<T>.Default.Equals(a, b)`, which routes to `IEquatable<T>` when available and does the right thing for value and reference types alike.

## const, readonly, and what readonly guarantees

`const` is compile-time: the value is baked into the call site's IL.[^const] That has a surprising deployment property: if library A exposes `public const int Version = 1` and assembly B compiles against it, B contains the literal `1`. Ship a new A with `Version = 2` and B still says `1` until B is recompiled. For anything that might change independently, `static readonly` is the safer choice — it's a field read at runtime.

`readonly` on a field means the reference can't be reassigned after construction. It says nothing at all about the object being referenced:

```csharp
private readonly List<string> _items = new();

_items = new List<string>();  // compile error
_items.Add("still fine");     // completely legal
```

`readonly` gives you immutability of the *binding*, not the *value*. For actual immutability you need a type that is itself immutable — `ImmutableList<T>`, or exposing `IReadOnlyList<T>` (which is a weaker promise: the caller can't mutate through that interface, but a cast can defeat it).

`readonly struct` is a stronger and more useful thing: it declares the entire struct immutable, which lets the compiler stop making defensive copies every time you call a member on it through a `readonly` field or `in` parameter. On a struct used in a hot loop, that's a measurable difference.

## ref, in, and out

All three make the parameter an **alias** for the caller's variable rather than a copy. They differ in the definite-assignment contract:

| | Caller must initialise | Callee must assign | Callee may read | Callee may write |
|---|---|---|---|---|
| `ref` | yes | no | yes | yes |
| `out` | no | yes | no (until assigned) | yes |
| `in` | yes | no | yes | no |

`out` is the familiar one, from `TryParse`. `ref` is for genuine two-way mutation of a caller's variable. `in` is the interesting one: it's a readonly alias, and its purpose is performance, not semantics — it avoids copying a large struct on every call.

`in` has a trap. If the struct isn't declared `readonly`, the compiler can't prove that calling a method or property on it won't mutate it, so it defensively copies the struct before *every such call*, turning the optimisation into a pessimisation.[^inparam] `in` and `readonly struct` (or at least `readonly` members) belong together.

`ref` also applies to returns and locals (`ref return`, `ref readonly`), which is how `Span<T>` indexers hand you a direct reference into memory instead of a copy.

### ref struct

A `ref struct` is a type that is guaranteed to live on the stack. `Span<T>` is the canonical one. The constraints follow from that guarantee: it can't be boxed to `ValueType` or `object`, can't be the type of a field in a class or non-`ref struct`, can't be captured by a lambda or local function, and can't be the element type of an array.[^refstruct]

Two of these were relaxed in C# 13, though not as far as it sounds. Before C# 13 a `ref struct` couldn't appear in an `async` method at all; now it can, but "a `ref struct` variable can't be used in the same block as the `await` expression".[^refstruct] Iterators changed the same way — allowed, "provided they aren't in code segments with the `yield return` statement". So the underlying rule never moved: the value still must not be alive across a suspension point, because that's exactly where it would have to be stored on the heap. C# 13 also let a `ref struct` implement interfaces and be used as a type argument under an `allows ref struct` constraint.

None of those are arbitrary: each is a way the value could outlive the stack frame it points into. Where `Span<T>` genuinely pays off is covered in the memory post.

## Boxing

Boxing converts a value type to a reference type by allocating a heap object and copying the value into it.[^boxing] Unboxing copies it back out. Each box is an allocation, which means GC pressure.

Where it happens, in rough order of how often it surprises people:

```csharp
object o = 42;                        // explicit-ish
IComparable c = 42;                   // value type to interface — boxes
ArrayList list = new(); list.Add(42);  // non-generic collection — boxes
string.Format("{0}", 42);              // boxed into object[] args
```

Generics were introduced largely to kill the third case: `List<int>` stores ints inline, `ArrayList` boxes every one. The difference is easy to measure with `GC.GetAllocatedBytesForCurrentThread()` — adding 1,000 ints to a pre-sized `ArrayList` allocates about 32,000 bytes against roughly 4,000 for a `List<int>`, because each boxed `int` is a 24-byte heap object plus a reference to store it. A single box on 64-bit is those 24 bytes: an object header, a method table pointer, and 4 bytes of payload rounded up to alignment.

Two subtler sources. First, a struct that overrides `Equals(object)` but doesn't implement `IEquatable<T>` will be boxed on every comparison inside a generic collection. Second, `enum` values box when passed as `object` — including in logging calls, which is why structured logging on a hot path can allocate more than expected.

Boxing is only a problem at volume: one box is nothing, a box per item per request is a Gen 0 collection you didn't need.

## Nullable reference types

NRTs are a **compile-time** feature with no runtime representation.[^nrt] `string?` and `string` are the same type at runtime — the difference lives in attributes and the compiler's flow analysis.

So nullability is advisory: a null can still arrive at a non-nullable parameter from unannotated code, from reflection, from deserialisation, or from any assembly compiled without the feature on. And warnings are only as good as your build settings — `<Nullable>enable</Nullable>` plus warnings-as-errors, or the analysis is decoration.

NRTs earn their keep at boundaries: annotating a domain model honestly forces you to decide, once, whether a field is genuinely optional, and the flow analysis holds you to it downstream. Each `!` (null-forgiving) is a small admission that should be justified.

## Variance

Covariance and contravariance are about which substitutions are safe when a generic type is used through a different type argument.

- **Covariance** (`out T`) — a producer of a derived type can stand in for a producer of a base type. `IEnumerable<string>` is an `IEnumerable<object>`, because everything it hands you is a `string`, and a `string` is an `object`.
- **Contravariance** (`in T`) — a consumer of a base type can stand in for a consumer of a derived type. `Action<object>` is an `Action<string>`, because anything that can handle any `object` can certainly handle a `string`.

The `in`/`out` keywords in a generic declaration are the compiler enforcing that `T` only appears in a safe position[^variance] — `out T` may only appear in return position, `in T` only in parameter position. That's why `IList<T>` is invariant: it both accepts and returns `T`, so neither substitution is safe. If `IList<string>` were an `IList<object>`, you could insert an `int` into a list of strings.

Arrays are covariant and shouldn't be — it's a pre-generics wart. `string[]` is assignable to `object[]`, and writing the wrong type into it throws `ArrayTypeMismatchException` at runtime rather than failing to compile.

## Interface vs abstract class

The mechanical differences are well known: multiple interfaces, single base class; abstract classes carry state and constructors; interfaces (since C# 8) can carry default implementations.

The design distinction is more useful. An interface describes a **capability** — what a thing can do, decoupled from what it is. An abstract class describes a **partial identity** — a shared implementation with holes in it. `IDisposable` is a capability. `HttpMessageHandler` is a partial identity.

Default interface implementations muddy this, and their real purpose is narrow: adding a member to a published interface without breaking every existing implementor. They aren't a general substitute for a base class — they can't hold state, and they're only visible through the interface, not the implementing type.

## Delegates, Func/Action, and events

A delegate is a type-safe function pointer, and specifically a **multicast** one: every delegate derives from `MulticastDelegate` and carries an invocation list.[^multicast]

```csharp
Action log = () => Console.Write("a");
log += () => Console.Write("b");
log();   // prints "ab"
```

Two consequences. Delegates are immutable, so `+=` allocates a new delegate with a longer invocation list and reassigns. And invoking a multicast delegate with a return value returns **only the last handler's result**, silently discarding the rest, which is a good reason to keep multicast `Func`s out of your design.

`Func<...>` and `Action<...>` are the BCL's pre-declared generic delegate types. Declare a named delegate when the name carries meaning the signature doesn't, or when you need `ref`/`out` parameters, which `Func` and `Action` can't express.

### Events

An `event` is a delegate field with access restricted by the compiler. From outside the declaring type, only `+=` and `-=` are legal. Without the keyword, any caller could assign over the whole invocation list — `Handler = myHandler` — and silently unsubscribe everyone else, or invoke it themselves.

The classic bug:

```csharp
if (Changed != null)
    Changed(this, EventArgs.Empty);   // race: last handler can unsubscribe here
```

Between the check and the invocation, another thread can unsubscribe the final handler and null the field. The fix reads as a formality but isn't:

```csharp
Changed?.Invoke(this, EventArgs.Empty);
```

`?.` reads the field into a temporary once and invokes that. Because delegates are immutable, the temporary stays valid: a handler that just unsubscribed may still get called, but nothing throws.

Events are also the most reliable way to leak memory in .NET: the publisher holds a reference to every subscriber, so a short-lived subscriber attached to a long-lived publisher never becomes collectable. The memory post covers it in full.

## Pattern matching

Pattern matching started as sugar for `is` plus a cast and has grown into branching that reads as a description of the data's shape.

```csharp
decimal Price(Shipment s) => s switch
{
    { Weight: > 100 } => 50m,                          // property pattern
    { Destination.Country: "ZA", Express: true } => 20m, // nested property
    Shipment(_, 0) => 0m,                              // positional (needs Deconstruct)
    null => throw new ArgumentNullException(nameof(s)),
    _ => 10m
};
```

Switch **expressions** are exhaustive-checked: if the compiler can't prove every input is handled it warns, and an unmatched value throws `SwitchExpressionException` at runtime rather than falling through.[^switch-expr] Arms are evaluated top to bottom, so a broader pattern above a narrower one makes the narrower one unreachable (a compile error when the compiler can prove it).

The caveat: heavy type-based pattern matching over a class hierarchy is often polymorphism written inside-out. If you're switching on the runtime type of a thing you own, a virtual method is usually the better answer. Pattern matching shines on data you *don't* own, or where the behaviour genuinely belongs to the caller rather than the type.

## Where this goes next

Everything above is single-threaded and mostly free. The interesting failures start with concurrency, a database or a network. The [next post]({{< relref "async-concurrency-and-a-thread-safe-cache.md" >}}) takes `async`/`await` apart, then builds a thread-safe cache three ways to show what "only one caller should populate this" actually costs.

[^equality]: Microsoft, ["Equality operators"](https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/operators/equality-operators) and ["Object.Equals"](https://learn.microsoft.com/en-us/dotnet/api/system.object.equals) — `==` is resolved on the static type; `Equals` is virtual.
[^const]: Microsoft, ["const (C# Reference)"](https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/keywords/const) — constants are evaluated at compile time and substituted into consuming assemblies.
[^boxing]: Microsoft, ["Boxing and Unboxing"](https://learn.microsoft.com/en-us/dotnet/csharp/programming-guide/types/boxing-and-unboxing) — boxing allocates on the heap and copies the value.
[^nrt]: Microsoft, ["Nullable reference types"](https://learn.microsoft.com/en-us/dotnet/csharp/nullable-references) — a compile-time static analysis feature; annotations don't change runtime behaviour.
[^variance]: Microsoft, ["Covariance and Contravariance in Generics"](https://learn.microsoft.com/en-us/dotnet/standard/generics/covariance-and-contravariance) — `out`/`in` type parameters and why `IList<T>` is invariant.
[^refstruct]: Microsoft, ["ref struct types"](https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/builtin-types/ref-struct) — the full restriction list, and the C# 13 changes to `async`, iterators, interfaces, and `allows ref struct`.
[^multicast]: Microsoft, ["MulticastDelegate Class"](https://learn.microsoft.com/en-us/dotnet/api/system.multicastdelegate) — delegates hold an invocation list; invoking returns the last method's result.
[^records]: Microsoft, ["Records"](https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/builtin-types/record) — synthesised value equality, `ToString`, `Deconstruct`, and the copy constructor behind `with`.
[^valuetypeequals]: Microsoft, ["ValueType.Equals(Object)"](https://learn.microsoft.com/en-us/dotnet/api/system.valuetype.equals) — the default calls `Object.Equals` on each field; override it for performance, particularly with reference-type fields.
[^inparam]: Microsoft, ["Structure types: readonly instance members"](https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/builtin-types/struct#readonly-instance-members) — when a non-`readonly` member is called from a read-only context, "the compiler creates a copy of the structure instance and calls the non-`readonly` member on that copy".
[^switch-expr]: Microsoft, ["switch expression"](https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/operators/switch-expression) — non-exhaustive switch expressions warn, and throw `SwitchExpressionException` at runtime.
