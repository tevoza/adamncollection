---
title: "Containers, Kubernetes and the Linux Underneath"
date: 2026-10-31T21:00:00+02:00
lastmod: 2026-11-01T12:00:00+02:00
draft: false
tags: ["software", "docker", "containers", "kubernetes", "helm", "aws", "eks", "ecs", "fargate", "linux", "security", "ai-generated"]
description: "What a container really is, how images are built, the Linux users, groups, permissions and capabilities that decide whether it runs, and how Kubernetes, Helm, EKS and ECS run it in production. Built around one .NET service and three real failures."
---

Most backend developers can write a Dockerfile that works on their laptop. Fewer can say why the same image fails with `Permission denied` the day someone adds `USER` to it, why a Kubernetes pod sits in `CreateContainerConfigError` without ever starting, or why `docker stop` takes exactly ten seconds. All three come down to a small amount of Linux that container tooling hides until it doesn't: processes, user IDs, file modes and capabilities.

This post builds that model from the bottom up, around one job:

> **The payments API ships.** Your ASP.NET Core payments service (the one that calls the bank over mutual TLS) has to move from a VM to containers on AWS. The platform team's rules are short: images must not run as root, the bank's client key comes from a Kubernetes Secret, and the production namespace enforces the Kubernetes `restricted` security profile. Three things go wrong on the way:
>
> 1. The first `docker run` after adding `USER $APP_UID` dies with `System.UnauthorizedAccessException: Access to the path '/app/data' is denied.`
> 2. In the cluster, the service can't read its client key: `Access to the path '/etc/bank/tls.key' is denied.` It's the same image that worked in the dev namespace.
> 3. When the production namespace switches to `restricted`, a pod for an older build never starts. Its status is `CreateContainerConfigError: container has runAsNonRoot and image has non-numeric user (app), cannot verify user is non-root`.

Each one gets explained where its mechanism comes up, and the fixes are collected at the end.

The Docker and Linux commands below were run for real (Docker Engine 24.0.7 on WSL 2, `alpine:3.22`, `aspnet:10.0` and a small test version of the service), with output trimmed where marked `...`. The Kubernetes, Helm and AWS configuration was **not** run against a cluster, so its behaviour is footnoted to the documentation or source code instead.

## A container is a process

A container isn't a small virtual machine. It's an ordinary Linux process with a restricted view of the machine: no guest kernel, no boot, and system calls into the same kernel as every other process on the host.

![A virtual machine stack compared with a container stack. On the left, a hypervisor runs two VMs, each with its own guest kernel, OS libraries and app. On the right, one Linux kernel runs two containers directly; each is a process tree with its own namespaces, cgroup limits and image filesystem, sharing the host kernel](vm-vs-container.svg)

Here Docker runs under WSL 2, whose VM provides the only Linux kernel, and every container reports it:

```console
$ docker run --rm alpine:3.22 uname -r
6.18.54.1-microsoft-standard-WSL2
```

The alpine image ships no kernel; `uname` asked the only one there is. Two kernel features turn that process into a "container".

**Namespaces** decide what the process can *see*. Each namespace type wraps one global resource so that processes inside it get their own isolated instance. The main ones:[^namespaces]

| Namespace | What it isolates | What it means in practice |
| --- | --- | --- |
| `pid` | Process IDs | Your app sees itself as PID 1 and can't see host processes |
| `mnt` | Mount points | The container gets its own root filesystem (the image) |
| `net` | Network devices, IPs, ports | Its own `eth0`, its own port 8080, its own routing table |
| `uts` | Hostname | `hostname` returns the container ID or pod name |
| `ipc` | System V IPC, POSIX message queues | No shared memory with other containers by accident |
| `user` | User and group IDs | UID 0 inside can map to an unprivileged UID outside (often **not** used, see below) |
| `cgroup` | The view of the cgroup tree | The container sees its own cgroup as the root |

**Control groups (cgroups)** decide how much the process can *use*. A cgroup limits and accounts for CPU, memory, I/O and process counts for a group of processes.[^cgroups] A Kubernetes memory limit of 512Mi is a number written into a cgroup file, and when the process exceeds it, the kernel's OOM killer ends it, not Kubernetes. Modern distributions use cgroup v2, a single unified hierarchy:

```console
$ docker info --format '{{.CgroupVersion}}'
2
```

What matters most for this post: **a container's processes run as ordinary Linux users, with ordinary Linux permissions, on a shared kernel.** Namespaces change what a process can see, not who it is. Unless user namespaces are switched on (most Docker and Kubernetes setups don't do it by default), UID 0 inside the container *is* UID 0 on the host kernel. The kernel only knows numbers, and 0 is 0.[^userns]

That's why "don't run as root" is a real rule, not a style preference. Namespaces, capabilities, seccomp and read-only mounts all make container root weaker than host root, but root inside is still the starting point an attacker wants.

## Images: layers, digests and the Dockerfile

An image is the filesystem the container sees, plus some metadata: the default command, environment variables, working directory, the user to run as, and the exposed ports. The OCI image specification defines the format.[^ocispec] An image is a list of **layers**, each a tarball of filesystem changes, and a config describing how to run it.

### Layers and the overlay filesystem

When the container starts, the runtime stacks the image's read-only layers and adds one writable layer on top, using an overlay filesystem.[^overlayfs] Reads fall through the stack until they find the file. Writes go to the top layer: modifying a file from a lower layer copies it up first ("copy-up"), and deleting one writes a "whiteout" marker that hides it.

![Image layers stacked under a container. Bottom to top: base OS layer (Ubuntu), .NET runtime layer, an app layer with the published DLLs, and a thin writable container layer. A read of /app/Payments.Api.dll falls through to the app layer; a write to /app/data/started.txt lands in the writable layer, which is discarded when the container is removed](image-layers.svg)

Three consequences follow:

- **The writable layer dies with the container.** Anything written there is gone when the container is replaced. State belongs in a volume or an external store.
- **Deleting in a later layer doesn't shrink the image.** `RUN rm` of a file added by an earlier `COPY` only hides it; the bytes stay in the earlier layer. That's why build tools go in a separate build stage rather than being "cleaned up" later.
- **Layers are shared and cached.** Ten services built on the same base image share those base layers on disk and in the registry. The build cache works layer by layer, so instruction order decides how much gets rebuilt.

### Tags move, digests don't

A tag like `aspnet:10.0` is a mutable pointer. Microsoft moves it every time it publishes a patched image. A **digest** (`sha256:...`) is a content hash of the image manifest, so it can't change without the content changing.[^ocispec] Production manifests should reference images by digest, or by an immutable tag that your pipeline never reuses, so that "redeploy the same version" means the same bytes.

The image ID Docker prints after a build is a hash too. With the classic image store used here it's the config's digest; with the containerd image store, the default on fresh installs from Docker Engine 29, it's the manifest's (or index's).[^containerdstore] Either way it's a local ID. The digest to pin in a deployment is the one `docker push` prints, because that's what the registry resolves.

```console
$ docker build -q -t payments:fixed .
sha256:3ca767a9b9abb84e74648de37ff7d1ca96abd39f1a9390e3194ad971b7dc1360
```

### A multi-stage Dockerfile for the payments API

Here's the first version of the Dockerfile. It builds with the full SDK and copies only the published output into the much smaller runtime image:[^multistage]

```dockerfile
# Build stage: has the SDK, compilers and NuGet cache. Never shipped.
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src
COPY Payments.Api.csproj .
RUN dotnet restore
COPY . .
RUN dotnet publish -c Release -o /app/publish --no-restore

# Runtime stage: only the ASP.NET Core runtime and the app.
FROM mcr.microsoft.com/dotnet/aspnet:10.0
WORKDIR /app
COPY --from=build /app/publish .
USER $APP_UID
ENTRYPOINT ["dotnet", "Payments.Api.dll"]
```

A few details are doing real work:

- **The project file is copied and restored before the source.** Restoring packages is slow, and the restore layer only changes when the `.csproj` does. Editing a `.cs` file invalidates only the layers from `COPY . .` onwards.
- **A `.dockerignore` keeps `bin/`, `obj/` and `.git` out of the build context.** Without one, a local `bin/` can end up copied over the published output, and the build context gets sent to the builder on every build.
- **`ENTRYPOINT` uses the exec form** (a JSON array). That matters for shutdown, as the Linux section below shows.
- **`USER $APP_UID`** switches to a non-root user. The .NET images have defined an `app` user since .NET 8,[^dotnetappuser] and they set an `APP_UID` environment variable holding its numeric ID. Since .NET 8, ASP.NET Core in these images also listens on port 8080 rather than 80, so a non-root user can bind it without help.[^dotnetport]

The base image doesn't switch user for you. The standard `aspnet` image still runs as root unless you opt in:

```console
$ docker image inspect mcr.microsoft.com/dotnet/aspnet:10.0 --format '{{json .Config.Env}}'
["PATH=...","APP_UID=1654","ASPNETCORE_HTTP_PORTS=8080","DOTNET_RUNNING_IN_CONTAINER=true",...]

$ docker run --rm --entrypoint sh mcr.microsoft.com/dotnet/aspnet:10.0 -c 'id; grep "^app" /etc/passwd'
uid=0(root) gid=0(root) groups=0(root)
app:x:1654:1654::/home/app:/bin/sh
```

So `USER $APP_UID` expands at build time to `USER 1654`, and the image's config records the number, which matters in Kubernetes:

```console
$ docker image inspect payments:fixed --format '{{.Config.User}}'
1654
```

Then the first run fails:

```console
$ docker run --rm payments:naive
Unhandled exception. System.UnauthorizedAccessException: Access to the path '/app/data' is denied.
...
```

The app creates `/app/data` at startup to write a small state file. Why it can't takes the next section.

## The Linux you actually need

### Users and groups are numbers

The kernel doesn't know user names. Every process has a set of numeric IDs, mainly a real and effective **user ID (UID)** and **group ID (GID)**, plus a list of **supplementary groups**.[^credentials] Every file has an owning UID, an owning GID and a set of permission bits. When a process touches a file, the kernel compares numbers.

Names live in `/etc/passwd` and `/etc/group`, text files that tools like `ls` and `id` use to translate numbers for humans. Inside a container, those files come from the **image**, not the host. Run Alpine as a UID it has never heard of and everything still works; it just can't print a name:

```console
$ docker run --rm --user 1654 alpine:3.22 sh -c 'id; whoami'
uid=1654 gid=0(root) groups=0(root)
whoami: unknown uid 1654
```

An unknown UID is fine; nothing requires a matching `/etc/passwd` entry. But notice **the GID is 0**. When the UID has no `/etc/passwd` entry, and so no primary group, Docker and containerd run it with the `root` group,[^dockerfile][^containerduid] and the same happens in Kubernetes when `runAsUser` is set without `runAsGroup`. Group 0 isn't root *privilege* (that comes from UID 0 and capabilities), but files group-writable for GID 0 are writable by this process, which some platforms rely on deliberately.

Files work the same way. A file chowned to a UID with no `/etc/passwd` entry shows the bare number:

```console
# chown 1654:1654 owned && ls -l owned
-rw-r--r-- 1 1654 1654 0 Oct 31 22:00 owned
# getent passwd 1654 || echo "no passwd entry for 1654"
no passwd entry for 1654
```

Names are resolved against whichever `/etc/passwd` the *reading* process sees, so the same file can show as `app` in one image and as an unrelated user in another. `ls -ln` is the habit to build.

![The same numeric UID seen from three places. The container process runs as UID 1654, which the image's /etc/passwd calls "app". A file it writes into a volume is stored on disk with owner 1654. On the host, /etc/passwd has no 1654, so ls shows the number; another image might map 1654 to a different name entirely. Only the number is real](uid-mapping.svg)

### Permission bits, and how the kernel checks them

Each file has three sets of three bits: **owner**, **group** and **other**, each with **r**ead, **w**rite and e**x**ecute. In octal, r=4, w=2, x=1, so `640` means owner `rw-`, group `r--`, other `---`.

On directories the bits mean something different, which is behind a lot of confusing errors:

- **r** on a directory lets you list its names.
- **w** lets you create, delete and rename entries in it. Creating `/app/data` needs write permission on `/app`, not on anything inside it.
- **x** lets you *traverse* it: use it in a path to reach something inside.

You can have one without the other. With a directory at `744` (other gets `r--`), an unprivileged user can't reach a world-readable file inside, because they can't traverse the directory:

```console
# ls -ldn locked
drwxr--r-- 2 0 0 4096 Oct 31 22:01 locked
$ cat locked/file
cat: locked/file: Permission denied
```

Change the directory to `711` (other gets `--x`) and the same user can read the file by name but can't list the directory:

```console
$ ls locked
ls: cannot open directory 'locked': Permission denied
$ cat locked/file
hi
```

The check itself is simple: the kernel picks exactly **one** of the three bit sets and uses only that one.[^pathres]

![How the kernel decides whether a process may open a file. If the process has CAP_DAC_OVERRIDE (normally root), the bit check is bypassed. Otherwise: if the process UID equals the file's owner UID, only the owner bits are used. Else if the file's GID is the process's GID or one of its supplementary groups, only the group bits are used. Else the other bits are used. The chosen bits either allow the operation or the result is EACCES, Permission denied. Worked example: UID 1654 trying to create /app/data where /app is owned by 0:0 with mode 755 falls through to the other bits, r-x, which have no w](permission-check.svg)

Because the selection is first-match, an owner whose owner bits are `---` is denied even when the other bits say `rw-`.

Now the payments failure makes sense. Every file `COPY` puts into an image is owned by UID 0 and GID 0 unless you say otherwise.[^dockerfile] So `/app` is owned by root:

```console
$ docker run --rm --entrypoint sh payments:naive -c 'id; ls -ldn /app; ls -ln /app | grep "Payments.Api\(.dll\)\?$"'
uid=1654(app) gid=1654(app) groups=1654(app)
drwxr-xr-x 1 0 0 4096 Oct 31 19:59 /app
-rwxr-xr-x 1 0 0 78192 Oct 31 19:59 Payments.Api
-rw-r--r-- 1 0 0  5632 Oct 31 19:59 Payments.Api.dll
```

UID 1654 isn't the owner and isn't in group 0, so the kernel uses the *other* bits of `/app`, `r-x`, and creating `/app/data` fails. Root-owned app files are exactly what you want; only the data directory needs a different owner.

### umask and the setgid directory

When a process creates a file, it asks for a mode (usually `666` for files, `777` for directories) and the kernel removes whatever bits are set in the process's **umask**.[^umask] The common default of `022` removes write for group and other:

```console
$ umask
0022
$ touch f; mkdir d; ls -ld f d | cut -c1-10
drwxr-xr-x
-rw-r--r--
$ umask 0002; touch g; ls -l g | cut -c1-10
-rw-rw-r--
```

That's why a file your app writes into a shared volume often isn't writable by the next container with a different UID: it was created `644`.

The **setgid bit on a directory** (shown as `s` in the group execute position) makes new entries inherit the directory's group instead of the creating process's primary group.[^inode] Combined with group-write permission, it's the standard way to share a directory between several UIDs that share a group:

```console
# mkdir shared; chgrp 4000 shared; chmod 2775 shared; ls -ldn shared
drwxrwsr-x 2 0 4000 4096 Oct 31 22:01 shared

# As UID 1654, not in group 4000: the "other" bits apply, r-x
$ touch shared/a
touch: cannot touch 'shared/a': Permission denied

# As UID 1654 with supplementary group 4000: group bits rwx apply
$ touch shared/b; ls -ln shared
-rw-r--r-- 1 1654 4000 0 Oct 31 22:01 b
```

The new file's group is 4000, taken from the directory; without the setgid bit it would be 1654. Kubernetes `fsGroup` uses this mechanism on volumes.

### Root is a bundle of capabilities

Historically, Linux had two kinds of process: UID 0, which bypassed permission checks, and everyone else. **Capabilities** split root's powers into separate flags that can be granted or removed one at a time.[^capabilities] A few that matter for containers:

| Capability | What it allows |
| --- | --- |
| `CAP_DAC_OVERRIDE` | Bypass file read, write and execute permission checks |
| `CAP_CHOWN` | Change file ownership to anyone |
| `CAP_FOWNER` | Act as the owner of any file, for example to `chmod` it |
| `CAP_SETUID` / `CAP_SETGID` | Change the process's UID and GID |
| `CAP_NET_BIND_SERVICE` | Bind ports below the unprivileged-port threshold (1024 by default) |
| `CAP_NET_RAW` | Raw sockets (`ping`, packet crafting) |
| `CAP_SYS_ADMIN` | A large grab-bag, including mounting filesystems. Treat as "root". |

Docker doesn't give a container all of root's capabilities. It starts root processes with a default set of 14,[^mobycaps] which you can see in `/proc/1/status` and decode with `capsh`:

```console
$ docker run --rm alpine:3.22 grep Cap /proc/1/status
CapInh: 0000000000000000
CapPrm: 00000000a80425fb
CapEff: 00000000a80425fb
CapBnd: 00000000a80425fb
CapAmb: 0000000000000000

$ capsh --decode=00000000a80425fb
0x00000000a80425fb=cap_chown,cap_dac_override,cap_fowner,cap_fsetid,cap_kill,cap_setgid,cap_setuid,cap_setpcap,cap_net_bind_service,cap_net_raw,cap_sys_chroot,cap_mknod,cap_audit_write,cap_setfcap
```

`CapEff` is the effective set, what the process can use right now. `CapBnd` is the bounding set, the ceiling it can ever regain. Switch to a non-root UID and the effective set drops to nothing, although the bounding set is unchanged:

```console
$ docker run --rm --user 1654 alpine:3.22 sh -c 'grep CapEff /proc/1/status; touch /etc/x'
CapEff: 0000000000000000
touch: /etc/x: Permission denied
```

A non-root process with an empty effective set, plus `allowPrivilegeEscalation: false` (covered below) so it can't regain capabilities through a setuid binary, is the shape you want.

Dropping capabilities from a *root* process is weaker than it sounds, because root still owns most of the filesystem:

```console
$ docker run --rm --cap-drop ALL alpine:3.22 sh -c 'id -u; grep CapEff /proc/1/status; touch /etc/x && echo wrote /etc/x'
0
CapEff: 0000000000000000
wrote /etc/x
```

With no capabilities at all, UID 0 still wrote to `/etc`, because it owns `/etc` and the owner bits allow it. Dropping capabilities and running as non-root solve different problems, and you want both.

One port detail catches people out. Without `CAP_NET_BIND_SERVICE`, a process can't bind ports below `net.ipv4.ip_unprivileged_port_start`, which defaults to 1024. Docker sets that sysctl to `0` inside containers that don't use host networking,[^mobyport] and containerd 2.0 does the same for Kubernetes pods by default (earlier versions didn't).[^containerdports]

```console
$ docker run --rm --user 1654 --cap-drop ALL alpine:3.22 cat /proc/sys/net/ipv4/ip_unprivileged_port_start
0
```

So "my non-root container binds port 80 fine" can be true on one runtime and false on another. The .NET images sidestep it by defaulting to 8080, and so should yours.

### PID 1 and signals

The first process in a PID namespace is PID 1, and PID 1 is special in two ways. The kernel doesn't apply default signal handlers to it, so a `SIGTERM` that would kill a normal process is ignored unless PID 1 has installed a handler. And PID 1 is expected to reap orphaned child processes, or they pile up as zombies.[^pidns]

`docker stop` and Kubernetes both stop a container by sending `SIGTERM`, waiting for a grace period, then sending `SIGKILL`. Docker waits 10 seconds by default; Kubernetes waits 30.[^podlifecycle] An app that gets the `SIGTERM` can finish in-flight requests and exit cleanly; one that doesn't is killed mid-request.

Which process is PID 1 depends on the form of `ENTRYPOINT`. The exec form runs your binary directly as PID 1. The shell form (`ENTRYPOINT dotnet Payments.Api.dll`) runs `/bin/sh -c "..."`, so the shell is PID 1 and your app is its child. Docker's reference says what happens next: the executable "will not receive Unix signals".[^dockerfile] Here's the difference, timed:

```console
# Exec form: dotnet is PID 1
$ docker exec pay ps -o pid,comm
    PID COMMAND
      1 dotnet
     33 ps
$ time docker stop pay
real    0m0.393s
$ docker inspect pay --format '{{.State.ExitCode}}'
0

# Shell form: sh is PID 1, dotnet never sees SIGTERM
$ docker exec p-shell ps -o pid,comm
    PID COMMAND
      1 sh
      7 dotnet
     34 ps
$ time docker stop p-shell
real    0m10.635s
$ docker inspect p-shell --format '{{.State.ExitCode}}'
137
```

Exit code 137 is 128 + 9: killed by `SIGKILL` after the ten-second wait. In Kubernetes that's 30 seconds of a pod taking no new traffic, and then its in-flight requests are cut off.

A wrapper script has the same problem, and `docker run --init` (which puts a tiny init, `docker-init`, backed by `tini`, at PID 1)[^dockerrun] doesn't fully fix it. With a `start.sh` that runs `dotnet Payments.Api.dll` as its last line and the container started with `--init`, `docker stop` returned quickly with exit code 143, but the .NET host never logged "Application is shutting down...". Reasoning from that: the init forwarded `SIGTERM` to the script, the script (with no handler) died, the init exited with it, and the kernel killed dotnet along with everything else in the PID namespace.[^pidns] The fix is one word in the script's last line:

```sh
#!/bin/sh
echo starting
exec dotnet Payments.Api.dll
```

`exec` replaces the shell with dotnet, so dotnet becomes PID 1, or the init's direct child under `--init`, and receives the signal itself. The container then stopped in 0.36 seconds with exit code 0 and a graceful shutdown in the log.

![Where docker stop's SIGTERM goes in four setups. Exec form: dotnet is PID 1, handles it, exit 0. Shell form: sh is PID 1 and doesn't forward it, so dotnet is killed by SIGKILL after 10 seconds, exit 137. Wrapper script with --init: tini forwards SIGTERM to the script, the script dies and the kernel kills dotnet, exit 143 with no graceful stop. Script ending in exec: dotnet has replaced the shell, so it is PID 1 or tini's direct child, and handles the signal, exit 0](signals.svg)

An init process is still worth having when your app spawns child processes it doesn't reap. Kubernetes has no `--init` flag, so put `tini` in the image's `ENTRYPOINT`. `shareProcessNamespace: true` also works, because it makes the pod's `pause` container PID 1 and `pause` reaps zombies.[^sharepid][^pause] The cost is that every container in the pod can then see the others' processes and files. ECS has `initProcessEnabled`.[^ecstaskdef]

## Root and non-root in practice

**Run as a numeric UID.** Use `USER 1654` (or `USER $APP_UID`, which expands to it), not `USER app`. The kernel needs a number either way, and as failure 3 will show, Kubernetes can only verify "non-root" from a number.

**Keep app files root-owned and not writable.** Files copied in as `0:0` with mode `644`/`755` can be read and executed by the app user but not modified, so an attacker with code execution in your app can't overwrite your DLLs. `chown -R app /app` "to fix permissions" throws that away.

**Make specific directories writable, and only those.** For the payments service that's `/app/data`:

```dockerfile
RUN mkdir -p /app/data && chown $APP_UID:0 /app/data && chmod 770 /app/data
USER $APP_UID
```

The app owns the directory, so it gets `rwx`. Group 0 gets `rwx` too, for platforms such as OpenShift that run images under a random UID in group 0.[^openshift] For copied files that need specific modes, `COPY --chown` and `COPY --chmod` set them in the same layer rather than adding another layer of `RUN chmod`.[^dockerfile]

With that change:

```console
$ docker run -d --name pay -p 18080:8080 payments:fixed
$ curl -s localhost:18080/whoami
uid=1654 user=app
$ docker exec pay sh -c 'ls -ldn /app/data; ls -ln /app/data'
drwxrwx--- 1 1654 0 4096 Oct 31 19:59 /app/data
-rw-r--r-- 1 1654 1654 28 Oct 31 19:59 started.txt
```

**Prefer a read-only root filesystem, with explicit writable mounts.** Better than a writable directory in the image is no writable image at all. `--read-only` mounts the container's root filesystem read-only, and anything that needs writing gets a volume or a `tmpfs`:[^dockerrun]

```console
$ docker run --rm --read-only payments:fixed
Unhandled exception. System.IO.IOException: Read-only file system : '/app/data/started.txt'

$ docker run -d --read-only \
    --tmpfs /app/data:uid=1654,gid=0,mode=770 \
    --tmpfs /tmp \
    payments:fixed
...
      Now listening on: http://[::]:8080
```

The read-only error is useful: it names each path your app writes to, so you find them in development rather than in production. `/tmp` is a common one.

**Know what the hardened base images remove.** "Distroless" and "chiseled" images, Microsoft's Ubuntu Chiseled variants for .NET among them, contain no shell or package manager, and they run as the non-root user by default.[^chiseled] That shrinks the attack surface, and it changes how you debug: `docker exec ... sh` has nothing to run. In Kubernetes, `kubectl debug --target` attaches an ephemeral container with its own tools to the app container's process namespace;[^kubectldebug] in a `restricted` namespace, add `--profile=restricted` or admission rejects it.

## Kubernetes

Kubernetes runs containers across a fleet of machines and keeps them in a state you declare. You don't say "start three containers"; you write an object that says "there should be three replicas of this pod", and controllers keep correcting the difference between that and what's running. That loop is why a deleted pod comes back, and why editing a Deployment rolls out new pods.

### The objects that matter for an API

![How the main Kubernetes objects fit together for one API. Traffic arrives at an Ingress (or Gateway), which routes to a Service. The Service selects pods by label (app: payments-api) and load-balances across their IPs. A Deployment owns a ReplicaSet, which owns three Pods. Each pod mounts a ConfigMap and a Secret as volumes and runs under a ServiceAccount, which is what EKS Pod Identity or IRSA maps to an IAM role](k8s-objects.svg)

- A **Pod** is one or more containers that share a network namespace (and so an IP address) and can share volumes. It's the unit Kubernetes schedules. You rarely create one directly.
- A **Deployment** declares a pod template and a replica count. It manages **ReplicaSets**, one per template version, which manage the pods. A rolling update creates a new ReplicaSet and shifts pods across gradually.
- A **Service** gives a stable virtual IP and DNS name to whichever pods currently match its label selector. Pods come and go; the Service name stays.
- An **Ingress** (or the newer **Gateway API**) routes external HTTP traffic to Services.
- **ConfigMaps** and **Secrets** hold configuration and credentials, mounted as files or exposed as environment variables. Secrets are base64-*encoded*, not encrypted, and by default they're stored unencrypted in etcd,[^secrets] so turn on encryption at rest and restrict who can read them.
- A **ServiceAccount** is the pod's identity to the Kubernetes API, and on EKS, the thing an IAM role is attached to.

### Probes, resources and shutdown

Three settings decide whether a Deployment behaves well in production.

**Probes.** A *readiness* probe decides whether the pod receives traffic from Services; failing it takes the pod out of rotation without restarting it. A *liveness* probe decides whether the container is restarted. A *startup* probe holds off the other two until slow initialisation finishes.[^probes] The usual mistake is a liveness check on a dependency such as the database: when the database blips, every pod restarts together and the outage gets worse. Liveness should answer "is this process wedged?", readiness "can I serve right now?".

**Requests and limits.** A *request* is what the scheduler reserves for the container on a node. A *limit* is the cgroup ceiling. A CPU limit is enforced by throttling, so the container slows down; a memory limit by the OOM killer, so it dies with `Reason: OOMKilled`.[^resources] The .NET garbage collector treats the memory limit as the machine's physical memory and by default caps the heap at 75% of it, so the limit also shapes your app's memory behaviour.[^dotnetgc]

**Graceful shutdown.** When a pod is deleted, Kubernetes removes it from Service endpoints and sends `SIGTERM`, then `SIGKILL` after `terminationGracePeriodSeconds`, which defaults to 30.[^podlifecycle] Those happen concurrently, so a pod can receive a few requests *after* `SIGTERM` while load balancers catch up, which is why many teams add a short `preStop` sleep.

### The security context

The pod and container `securityContext` is where the Linux model becomes YAML; every field maps to something above:

```yaml
spec:
  securityContext:                  # pod level: applies to all containers
    runAsNonRoot: true              # refuse to start a container that would run as UID 0
    runAsUser: 1654                 # override the image's USER
    runAsGroup: 1654                # primary GID; if omitted, the image's passwd entry, else 0
    fsGroup: 1654                   # supplementary group, applied to volumes
    seccompProfile:
      type: RuntimeDefault          # the runtime's syscall filter
  containers:
    - name: api
      securityContext:              # container level
        allowPrivilegeEscalation: false   # sets no_new_privs: setuid binaries can't raise privileges
        readOnlyRootFilesystem: true
        capabilities:
          drop: ["ALL"]
```

**`runAsNonRoot`** is a check, not a setting. The kubelet looks at the UID the container will run as (from `runAsUser`, or from the image's `USER`) and refuses to start it if it's 0. If the image's `USER` is a **name**, the kubelet can't resolve it without the image's `/etc/passwd`, so it refuses that too: "image has non-numeric user ... cannot verify user is non-root".[^kubeletnonroot] That's failure 3.

**`fsGroup`** is the setgid directory from earlier, applied automatically. For volume types that support it, the kubelet sets each file's group to the `fsGroup` GID and adds group `rw` (or `r` on read-only volumes such as Secrets), sets setgid on directories, and gives the process `fsGroup` as a supplementary group.[^k8ssc][^volumeownership] On large volumes the recursive change slows pod startup; `fsGroupChangePolicy: OnRootMismatch` skips it when the volume's root already looks right.[^k8ssc]

**`allowPrivilegeEscalation`** defaults to `true` and controls the kernel's `no_new_privs` flag.[^k8ssc] With it set to `false`, a setuid-root binary in the image can't give your process root.

**`seccompProfile: RuntimeDefault`** filters system calls the container almost certainly doesn't need. Docker applies its default profile automatically, but Kubernetes runs pods as `Unconfined` unless you ask for `RuntimeDefault` or the node turns on the kubelet's `seccompDefault` setting.[^seccomp] "It was fine under Docker" doesn't carry over here.

**`readOnlyRootFilesystem`** is `--read-only`. Writable paths become `emptyDir` volumes (a directory that lives as long as the pod, optionally in memory):

```yaml
      volumeMounts:
        - { name: data, mountPath: /app/data }
        - { name: tmp,  mountPath: /tmp }
  volumes:
    - { name: data, emptyDir: {} }
    - { name: tmp,  emptyDir: { medium: Memory, sizeLimit: 64Mi } }
```

### Secrets as files, and their modes

When a Secret is mounted as a volume, each key becomes a file. If you don't set a mode, each file is created `0644`.[^secretfiles] You can set `defaultMode` for the whole volume and `mode` per key. Secret volume files are owned by root.

That's the setup for failure 2. The dev namespace mounted the bank client key with the default `0644`, readable by everyone, including UID 1654. For production, someone rightly tightened the key to `0400`. Owner read-only is the conventional mode for a private key, but the owner is root:

```yaml
  volumes:
    - name: bank-client
      secret:
        secretName: payments-bank-client
        defaultMode: 0400
```

UID 1654 isn't the owner, and both the group bits and the other bits are `---`, so the read is denied whichever set applies. The fix is `fsGroup: 1654` on the pod: the kubelet sets each file's group to 1654 and adds group read, giving `0440`.[^volumeownership] The app can read the key, other users still can't, and the mount is read-only.

![The bank key read by a process running as UID 1654 in group 1654. With defaultMode 0400 alone, the file is 0:0 with mode r-- --- ---; the process is neither owner nor in the group, the other bits are empty, and the read fails with EACCES. With fsGroup 1654 added, the kubelet sets the group to 1654 and ORs in 0440, giving r-- r-- ---, so the group bits apply and the read is allowed](secret-fsgroup.svg)

One YAML caution from the same documentation page: `0400` is octal in YAML but would be read as the decimal number 400 in JSON, so JSON manifests need `256`.[^secretfiles]

### Pod Security Standards

The **Pod Security Standards** define three profiles: `privileged` (anything goes), `baseline` (blocks known privilege escalations such as host namespaces and privileged containers), and `restricted` (current hardening best practice).[^pss] The built-in **Pod Security Admission** controller enforces them per namespace, using labels:[^psa]

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: payments-prod
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/warn: restricted
```

To pass `restricted`, a pod must set `runAsNonRoot: true`, must not set `runAsUser: 0`, must set `allowPrivilegeEscalation: false`, must drop `ALL` capabilities (adding back only `NET_BIND_SERVICE`), must use the `RuntimeDefault` or `Localhost` seccomp profile, and can only use a fixed list of volume types (no `hostPath`).[^pss] The `securityContext` above passes. Applying `warn` and `audit` before `enforce` shows you which workloads would fail before anything gets rejected.

## Helm

Kubernetes manifests are static YAML, and a real service needs several of them that differ slightly per environment: different replica counts, image tags, hostnames and resource sizes. **Helm** is the most common answer. A *chart* is a directory of templated manifests plus a `values.yaml` of defaults. Installing a chart with a particular set of values creates a *release*, and Helm keeps a history of each release's revisions so you can roll back.[^helm]

```text
payments-api/
  Chart.yaml            # name, version, appVersion
  values.yaml           # defaults
  values-prod.yaml      # production overrides
  templates/
    deployment.yaml
    service.yaml
    ingress.yaml
    _helpers.tpl        # shared template snippets
```

Templates are Go templates over YAML. The security context belongs in the chart, with the safe settings as defaults, so every environment gets them unless it explicitly opts out:

```yaml
# values.yaml
image:
  repository: 123456789012.dkr.ecr.eu-west-1.amazonaws.com/payments-api
  digest: ""            # required; the pipeline sets the pushed digest
podSecurityContext:
  runAsNonRoot: true
  runAsUser: 1654
  runAsGroup: 1654
  fsGroup: 1654
  seccompProfile: { type: RuntimeDefault }
containerSecurityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities: { drop: ["ALL"] }
```

```yaml
# templates/deployment.yaml (excerpt)
    spec:
      serviceAccountName: {{ include "payments-api.fullname" . }}
      securityContext:
        {{- toYaml .Values.podSecurityContext | nindent 8 }}
      containers:
        - name: api
          image: "{{ .Values.image.repository }}@{{ required "image.digest is required" .Values.image.digest }}"
          securityContext:
            {{- toYaml .Values.containerSecurityContext | nindent 12 }}
```

The commands you'll use most:

```sh
helm lint ./payments-api                                  # catch template and chart errors
helm template ./payments-api -f values-prod.yaml          # render locally, apply nothing
helm upgrade --install payments-api ./payments-api \
  -n payments-prod -f values-prod.yaml \
  --set image.digest=sha256:5e1d07b4...                   # install or upgrade the release
helm history payments-api -n payments-prod                # list revisions
helm rollback payments-api 41 -n payments-prod            # go back to revision 41
```

`helm template` is the important habit. It shows exactly the YAML that will reach the cluster, which is all Kubernetes ever sees, and it's a good thing to diff in code review.

Helm 4, released in November 2025, kept these commands and the chart format; the changes are mostly in plugins, the SDK and server-side apply.[^helm4] If your needs are only "the same manifests with a few per-environment patches", **Kustomize** (built into `kubectl` as `kubectl apply -k`) does that without a template language and is worth considering first. Helm earns its place when you need real parameterisation, packaging for other teams, or release history and rollback.

## Where it runs on AWS: EKS or ECS

AWS offers two container orchestrators, and both can run on either EC2 instances you manage or **Fargate**, where AWS runs each pod or task in its own small VM and you never see a node.

![Who manages what on EKS and ECS, each on EC2 or Fargate. EKS: AWS runs the Kubernetes control plane; on EC2 you manage worker nodes (AMIs, patching, scaling), on Fargate AWS runs each pod in its own VM. ECS: AWS runs the orchestrator, which has no control plane for you to see; on EC2 you manage container instances, on Fargate AWS runs each task in its own VM. In every case you own the image, its USER and file permissions, the task or pod security settings, and the IAM role](eks-vs-ecs.svg)

**EKS** is managed Kubernetes. AWS runs the control plane (API server, etcd, scheduler) and you get the full Kubernetes API, so everything above applies unchanged. You also take on Kubernetes' operational surface: cluster version upgrades, add-ons (VPC CNI, CoreDNS, the load balancer controller), and node management unless you use Fargate or EKS Auto Mode, where AWS also runs the nodes, autoscaling, load balancer integration and storage drivers.[^eksauto]

**ECS** is AWS's own orchestrator. It has no control plane you can see; the main concepts are a *task definition* (roughly a pod spec), a *task* (a running instance of one) and a *service* (keep N tasks running behind a load balancer). It's less to learn and operate, but tied to AWS, with no Helm and no Kubernetes ecosystem.

The Linux settings carry over directly. ECS task definitions have the same knobs under different names:[^ecstaskdef]

```json
{
  "family": "payments-api",
  "requiresCompatibilities": ["FARGATE"],
  "networkMode": "awsvpc",
  "taskRoleArn": "arn:aws:iam::123456789012:role/payments-api-task",
  "executionRoleArn": "arn:aws:iam::123456789012:role/payments-api-execution",
  "containerDefinitions": [{
    "name": "api",
    "image": "123456789012.dkr.ecr.eu-west-1.amazonaws.com/payments-api@sha256:5e1d07b4...",
    "user": "1654",
    "readonlyRootFilesystem": true,
    "linuxParameters": {
      "initProcessEnabled": true,
      "capabilities": { "drop": ["ALL"] }
    },
    "stopTimeout": 30,
    "portMappings": [{ "containerPort": 8080 }]
  }]
}
```

`user` is `docker run --user`, `readonlyRootFilesystem` is `--read-only` and `initProcessEnabled` is `--init`. On Fargate, `SYS_PTRACE` is the only capability you can *add*. `stopTimeout` is the grace period between `SIGTERM` and `SIGKILL` (default 30 seconds, maximum 120), and Fargate tasks must use the `awsvpc` network mode, which gives each task its own network interface and VPC IP address.[^ecstaskdef]

**IAM for workloads** is where the two differ most, and it matters for the payments service, which also reads its database password from AWS Secrets Manager:

- On **ECS**, the **task role** (`taskRoleArn`) is the identity your code's AWS SDK calls use. The **execution role** is different: it's what the ECS agent uses to pull the image and fetch secrets *before* your code starts. Mixing these up is a common source of `AccessDenied`.[^ecstaskdef]
- On **EKS**, an IAM role is attached to a Kubernetes **ServiceAccount**. **IRSA** (IAM roles for service accounts) federates through the cluster's OIDC provider. **EKS Pod Identity** is newer and simpler: an agent DaemonSet on each node hands out credentials, there's no OIDC provider to manage, and the role's trust policy names one service principal, `pods.eks.amazonaws.com`, for every cluster.[^ekspodid] Pod Identity isn't supported for pods on Fargate, which use IRSA instead.[^ekspodid][^eksfargate]

**Fargate on EKS** also has Kubernetes-specific limits: no DaemonSets, no privileged containers, no `hostNetwork` or `hostPort`, and no EBS volumes. Each pod gets its own VM and kernel,[^eksfargate] which is a real security benefit: a container escape lands in a VM with nothing else in it.

A rough way to choose, as opinion rather than rule: if you already run Kubernetes, need its ecosystem (operators, service mesh, Helm charts from vendors) or need to stay portable across clouds, use **EKS**. If you're an AWS shop running a handful of stateless HTTP services and workers and don't want to operate Kubernetes, **ECS on Fargate** gets you to production with far less to learn. Either way, the image, its `USER`, its file permissions and its shutdown behaviour are the same work.

## Resolving the payments API

| Failure | Cause | Fix |
| --- | --- | --- |
| `Access to the path '/app/data' is denied` | `COPY` leaves `/app` owned by `0:0`, mode `755`; UID 1654 gets the other bits, `r-x` | Keep app files root-owned; add the one `RUN mkdir … chown $APP_UID:0` line from [Root and non-root in practice](#root-and-non-root-in-practice), and an `emptyDir` in the cluster |
| `Access to the path '/etc/bank/tls.key' is denied` | Secret files are root-owned; `defaultMode: 0400` leaves group and other `---` | `fsGroup: 1654`, giving `0440` with group 1654. Not `defaultMode: 0444`, which makes the key readable by every UID |
| `image has non-numeric user (app), cannot verify user is non-root` | `USER app` gives the kubelet a name it can't check without the image's `/etc/passwd` | A numeric `USER` (`$APP_UID` expands to `1654`), or `runAsUser: 1654` in the pod; the service now does both |

To confirm fix 2, `kubectl exec deploy/payments-api -- stat -L -c '%a %g' /etc/bank/tls.key` should print `440 1654` (`-L` follows the symlinks Secret volumes use).

The full production pod spec that comes out of the Helm chart:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: payments-api
spec:
  replicas: 3
  selector:
    matchLabels: { app: payments-api }
  template:
    metadata:
      labels: { app: payments-api }
    spec:
      serviceAccountName: payments-api            # mapped to an IAM role via Pod Identity
      terminationGracePeriodSeconds: 30
      securityContext:
        runAsNonRoot: true
        runAsUser: 1654
        runAsGroup: 1654
        fsGroup: 1654
        seccompProfile: { type: RuntimeDefault }
      containers:
        - name: api
          image: 123456789012.dkr.ecr.eu-west-1.amazonaws.com/payments-api@sha256:5e1d07b4...
          ports: [{ containerPort: 8080 }]
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: { drop: ["ALL"] }
          resources:
            requests: { cpu: 250m, memory: 256Mi }
            limits:   { memory: 512Mi }
          readinessProbe:
            httpGet: { path: /healthz, port: 8080 }
          livenessProbe:
            httpGet: { path: /healthz, port: 8080 }
            initialDelaySeconds: 10
          lifecycle:
            preStop:
              sleep: { seconds: 5 }                # let load balancers stop sending traffic
          volumeMounts:
            - { name: data, mountPath: /app/data }
            - { name: tmp, mountPath: /tmp }
            - { name: bank-client, mountPath: /etc/bank, readOnly: true }
      volumes:
        - { name: data, emptyDir: {} }
        - { name: tmp, emptyDir: { medium: Memory, sizeLimit: 64Mi } }
        - name: bank-client
          secret:
            secretName: payments-bank-client
            defaultMode: 0400
```

It passes the `restricted` profile. The process can write only to `/app/data` and `/tmp`, can read the key through its group, holds no capabilities, and shuts down on `SIGTERM` within its grace period. The `sleep` `preStop` action is on by default from Kubernetes 1.30 and stable in 1.34; older clusters need an `exec` hook and a `sleep` binary in the image.[^lifecycle][^sleepgate] The bare `/healthz` serves both probes because it checks only the process; a readiness check on the bank connection would be a reasonable next step.

## Cheat sheet

**Who am I, and what can I do?**

```sh
id                                        # UID, GID, supplementary groups (numbers and names)
grep -E 'Uid|Gid|Groups|Cap' /proc/1/status   # the same for PID 1, plus capability sets
capsh --decode=00000000a80425fb           # turn a capability mask into names
cat /proc/sys/net/ipv4/ip_unprivileged_port_start
```

**Why can't I read or write this?**

```sh
ls -ln path                               # numeric owner/group and mode; never trust names across images
ls -ldn dir                               # the directory's own mode (needs w to create, x to traverse)
stat -c '%a %u:%g %n' path                # octal mode and numeric owner in one line
namei -l /etc/bank/tls.key                # every directory on the path, with modes (util-linux)
```

**Docker**

```sh
docker run --rm --user 1654 image id      # run as an arbitrary UID
docker run --rm --read-only --tmpfs /tmp image   # find every path the app writes to
docker run --rm --cap-drop ALL image      # run with no capabilities
docker image inspect image --format '{{.Config.User}} {{json .Config.Entrypoint}}'
docker inspect c --format '{{.State.ExitCode}}'  # 137 = SIGKILL, 143 = SIGTERM
docker exec c ps -o pid,comm              # who is PID 1?
```

**Kubernetes**

```sh
kubectl describe pod p                    # events: CreateContainerConfigError, OOMKilled, probe failures
kubectl get pod p -o jsonpath='{.status.containerStatuses[0].lastState}'
kubectl exec deploy/payments-api -- stat -L -c '%a %u:%g' /etc/bank/tls.key
kubectl debug -it pod/p --image=busybox --target=api --profile=restricted   # debug shell-less images
kubectl label ns payments-prod pod-security.kubernetes.io/warn=restricted   # see violations without blocking
```

**Helm**

```sh
helm template ./chart -f values-prod.yaml # render without applying
helm upgrade --install rel ./chart -f values-prod.yaml
helm history rel; helm rollback rel N
```

**Rules of thumb**

- A container is a process on a shared kernel. UID 0 inside is UID 0 unless user namespaces are on.
- `USER` with a number. App files root-owned and read-only. One writable directory, owned by the app UID with group 0, or better, a volume.
- Read-only root filesystem, `drop: ["ALL"]`, `allowPrivilegeEscalation: false`, `seccompProfile: RuntimeDefault`.
- Exec-form `ENTRYPOINT`, and `exec` at the end of any wrapper script.
- Use `fsGroup` for volume access, not `chmod 777` and not `defaultMode: 0444`.
- Reference images by digest.
- EKS if you want Kubernetes; ECS on Fargate if you just want containers running on AWS.

[^namespaces]: Linux man-pages, ["namespaces(7)"](https://man7.org/linux/man-pages/man7/namespaces.7.html) — the namespace types (cgroup, IPC, network, mount, PID, time, user, UTS) and what each isolates.
[^cgroups]: Linux man-pages, ["cgroups(7)"](https://man7.org/linux/man-pages/man7/cgroups.7.html) — control groups for limiting and accounting resource usage, and the cgroup v1 and v2 hierarchies.
[^userns]: Kubernetes, ["User Namespaces"](https://kubernetes.io/docs/concepts/workloads/pods/user-namespaces/) — opting a pod in with `hostUsers: false`, so that UIDs inside the pod map to unprivileged UIDs on the node; requires idmap mount support, in practice Linux 6.3 or later. Linux man-pages, ["user_namespaces(7)"](https://man7.org/linux/man-pages/man7/user_namespaces.7.html), for the kernel mechanism.
[^ocispec]: Open Container Initiative, ["Image Format Specification"](https://github.com/opencontainers/image-spec/blob/main/spec.md) — manifests, layers, image config and content-addressable descriptors identified by digest.
[^overlayfs]: Linux kernel documentation, ["Overlay Filesystem"](https://docs.kernel.org/filesystems/overlayfs.html) — upper and lower layers, copy-up on write, and whiteouts for deleted files.
[^multistage]: Docker, ["Multi-stage builds"](https://docs.docker.com/build/building/multi-stage/) — using multiple `FROM` stages and copying only the artifacts needed into the final image.
[^dockerfile]: Docker, ["Dockerfile reference"](https://docs.docker.com/reference/dockerfile/) — `USER` ("When the user doesn't have a primary group then the image (or the next instructions) will be run with the `root` group"), `COPY --chown` ("Without this flag, files are created with UID and GID of 0") and `--chmod`, and `ENTRYPOINT` shell form, where the executable "will not be the container's `PID 1`, and will not receive Unix signals".
[^dotnetappuser]: Microsoft, ["Breaking change: New non-root 'app' user in Linux images"](https://learn.microsoft.com/en-us/dotnet/core/compatibility/containers/8.0/app-user) — the `app` user, opt-in, introduced in .NET 8. The UID 1654 and `APP_UID` variable shown in this post were read from the `aspnet:10.0` image itself.
[^dotnetport]: Microsoft, ["Breaking change: Default ASP.NET Core port changed from 80 to 8080"](https://learn.microsoft.com/en-us/dotnet/core/compatibility/containers/8.0/aspnet-port) — the new default port and the `ASPNETCORE_HTTP_PORTS` variable, from .NET 8.
[^chiseled]: Microsoft, ["Ubuntu Chiseled + .NET"](https://github.com/dotnet/dotnet-docker/blob/main/documentation/ubuntu-chiseled.md) — chiseled images with no shell or package manager and a non-root user by default.
[^openshift]: Red Hat Developer, ["Adapting Docker and Kubernetes containers to run on Red Hat OpenShift Container Platform"](https://developers.redhat.com/blog/2020/10/26/adapting-docker-and-kubernetes-containers-to-run-on-red-hat-openshift-container-platform) — containers run under an arbitrary UID in the root group (GID 0), so writable paths need group 0 ownership and group permissions.
[^credentials]: Linux man-pages, ["credentials(7)"](https://man7.org/linux/man-pages/man7/credentials.7.html) — real, effective, saved and filesystem user and group IDs, and supplementary group IDs.
[^pathres]: Linux man-pages, ["path_resolution(7)"](https://man7.org/linux/man-pages/man7/path_resolution.7.html) — the permission check: owner bits if the filesystem UID matches, else group bits if the file's group matches the process's GID or a supplementary group, else other bits; search (execute) permission needed on each directory in the path; and the capability overrides.
[^umask]: Linux man-pages, ["umask(2)"](https://man7.org/linux/man-pages/man2/umask.2.html) — the file mode creation mask and how it's applied to permissions requested by `open(2)` and `mkdir(2)`.
[^inode]: Linux man-pages, ["inode(7)"](https://man7.org/linux/man-pages/man7/inode.7.html) — the set-group-ID bit on a directory causing new files to inherit the directory's group ID.
[^capabilities]: Linux man-pages, ["capabilities(7)"](https://man7.org/linux/man-pages/man7/capabilities.7.html) — the list of capabilities, permitted/effective/bounding/ambient sets, and what happens to them across a UID change.
[^mobycaps]: Moby (Docker Engine) source, [`daemon/pkg/oci/caps/defaults.go`](https://github.com/moby/moby/blob/master/daemon/pkg/oci/caps/defaults.go) — `DefaultCapabilities()`, the 14 capabilities granted by default.
[^mobyport]: Moby (Docker Engine) source, [`daemon/oci_linux.go`](https://github.com/moby/moby/blob/master/daemon/oci_linux.go) — sets `net.ipv4.ip_unprivileged_port_start` to `0` for containers not using host networking, with the comment "allow opening any port less than 1024 without CAP_NET_BIND_SERVICE".
[^containerdports]: containerd, ["CRI Plugin Config Guide"](https://github.com/containerd/containerd/blob/main/docs/cri/config.md) — `enable_unprivileged_ports` sets `ip_unprivileged_port_start=0` for containers not using host network; "before containerd v2.0, this value defaulted to false".
[^pidns]: Linux man-pages, ["pid_namespaces(7)"](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html) — the namespace's init process becoming the parent of orphans, only receiving signals for which it has installed a handler, and the kernel terminating all other processes in the namespace with `SIGKILL` when the init process terminates.
[^podlifecycle]: Kubernetes, ["Pod Lifecycle: Termination of Pods"](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination) — `SIGTERM`, then `SIGKILL` after the grace period; "The default `terminationGracePeriodSeconds` setting is 30 seconds"; `preStop` hooks and endpoint removal. Docker's 10-second default is in ["docker container stop"](https://docs.docker.com/reference/cli/docker/container/stop/).
[^dockerrun]: Docker, ["docker container run"](https://docs.docker.com/reference/cli/docker/container/run/) — `--init` (an init as PID 1, by default `docker-init`, backed by tini), `--read-only`, `--tmpfs`, `--user`, `--cap-add` and `--cap-drop`.
[^ecstaskdef]: AWS, ["Amazon ECS task definition parameters for Fargate"](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_definition_parameters.html) — `user`, `readonlyRootFilesystem`, `linuxParameters.capabilities` (with `SYS_PTRACE` the only valid `add` value), `initProcessEnabled` (maps to `--init`), `stopTimeout` (default 30 seconds, maximum 120), `taskRoleArn` versus `executionRoleArn`, and `awsvpc` being required on Fargate.
[^secrets]: Kubernetes, ["Secrets"](https://kubernetes.io/docs/concepts/configuration/secret/) — "Kubernetes Secrets are, by default, stored unencrypted in the API server's underlying data store (etcd)."
[^dotnetgc]: Microsoft, ["Runtime configuration options for garbage collection"](https://learn.microsoft.com/en-us/dotnet/core/runtime-config/garbage-collector) — inside a container with a memory limit, "that limit is treated as the total physical memory", and the default heap hard limit is 75% of it.
[^probes]: Kubernetes, ["Configure Liveness, Readiness and Startup Probes"](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/) — liveness failures restart the container, readiness failures remove it from Service endpoints, and startup probes disable the others until they succeed.
[^resources]: Kubernetes, ["Resource Management for Pods and Containers"](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/) — "`cpu` limits are enforced by CPU throttling", "`memory` limits are enforced by the kernel with out of memory (OOM) kills", and the `OOMKilled` reason.
[^k8ssc]: Kubernetes, ["Configure a Security Context for a Pod or Container"](https://kubernetes.io/docs/tasks/configure-pod-container/security-context/) — `runAsUser`, `runAsGroup`, `fsGroup` ownership of volumes and the setgid example, `fsGroupChangePolicy`, and `allowPrivilegeEscalation` defaulting to true and controlling `no_new_privs`.
[^kubeletnonroot]: Kubernetes source, [`pkg/kubelet/kuberuntime/security_context_others.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/kuberuntime/security_context_others.go) — the `runAsNonRoot` checks and their messages, including "container has runAsNonRoot and image has non-numeric user (%s), cannot verify user is non-root" and "image will run as root"; these surface as `CreateContainerConfigError`.
[^volumeownership]: Kubernetes source, [`pkg/volume/volume_linux.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/volume/volume_linux.go) — `changeFilePermission` sets each file's group to `fsGroup` and ORs in `0660` (read-write volumes) or `0440` (read-only volumes), plus setgid and `0110` on directories. Secret volumes are read-only and apply `fsGroup` ownership ([`pkg/volume/secret/secret.go`](https://github.com/kubernetes/kubernetes/blob/master/pkg/volume/secret/secret.go)).
[^seccomp]: Kubernetes, ["Restrict a Container's Syscalls with seccomp"](https://kubernetes.io/docs/tutorials/security/seccomp/) — without the kubelet's `seccompDefault` setting, "the default is `Unconfined`".
[^secretfiles]: Kubernetes, ["Distribute Credentials Securely Using Secrets"](https://kubernetes.io/docs/tasks/inject-data-application/distribute-credentials-secure/) — "If you don't specify any permissions, `0644` is used by default", `defaultMode` and per-key modes, and the JSON-versus-octal note.
[^pss]: Kubernetes, ["Pod Security Standards"](https://kubernetes.io/docs/concepts/security/pod-security-standards/) — the privileged, baseline and restricted profiles and the controls in each.
[^psa]: Kubernetes, ["Pod Security Admission"](https://kubernetes.io/docs/concepts/security/pod-security-admission/) — the built-in admission controller (stable since v1.25) and its `enforce`, `audit` and `warn` namespace labels.
[^lifecycle]: Kubernetes, ["Container Lifecycle Hooks"](https://kubernetes.io/docs/concepts/containers/container-lifecycle-hooks/) — `exec`, `httpGet` and `sleep` hook handlers.
[^helm]: Helm, ["Using Helm"](https://helm.sh/docs/intro/using_helm/) — charts, releases, values files, `helm upgrade --install`, `helm history` and `helm rollback`; and ["Chart Template Guide"](https://helm.sh/docs/chart_template_guide/) for templates and `helm template`.
[^helm4]: Helm, ["Helm 4 Released"](https://helm.sh/blog/helm-4-released/) — released 12 November 2025; redesigned plugin system, updated SDK, server-side apply support.
[^containerdstore]: Docker, ["containerd image store with Docker Engine"](https://docs.docker.com/engine/storage/containerd/) — the containerd image store is the default for fresh installations of Docker Engine 29.0 and later.
[^containerduid]: containerd source, [`pkg/oci/spec_opts.go`](https://github.com/containerd/containerd/blob/main/pkg/oci/spec_opts.go) — `WithUserID`: "If /etc/passwd does not exist, or uid is not found in /etc/passwd, it sets the requested uid, additionally sets the gid to 0".
[^sharepid]: Kubernetes, ["Share Process Namespace between Containers in a Pod"](https://kubernetes.io/docs/tasks/configure-pod-container/share-process-namespace/) — with `shareProcessNamespace: true`, the container process no longer has PID 1; the pod sandbox (`/pause`) does.
[^pause]: Kubernetes source, [`build/pause/linux/pause.c`](https://github.com/kubernetes/kubernetes/blob/master/build/pause/linux/pause.c) — the `SIGCHLD` handler `sigreap` calls `waitpid(-1, NULL, WNOHANG)` in a loop to reap exited children.
[^kubectldebug]: Kubernetes, ["Debug Running Pods"](https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/) — ephemeral containers for images that don't include debugging utilities, "such as with distroless images", and `--target` joining another container's process namespace.
[^sleepgate]: Kubernetes, ["Feature Gates"](https://kubernetes.io/docs/reference/command-line-tools-reference/feature-gates/) — `PodLifecycleSleepAction`: alpha in 1.29, beta and on by default from 1.30, stable from 1.34.
[^eksauto]: AWS, ["Automate cluster infrastructure with EKS Auto Mode"](https://docs.aws.amazon.com/eks/latest/userguide/automode.html) — AWS manages the EC2 nodes, compute autoscaling, pod networking, Elastic Load Balancing integration and block storage drivers.
[^ekspodid]: AWS, ["Learn how EKS Pod Identity grants pods access to AWS services"](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html) — associating IAM roles with service accounts without OIDC providers, the `pods.eks.amazonaws.com` principal, the Pod Identity Agent DaemonSet, and "Linux and Windows pods that run on AWS Fargate (Fargate) aren't supported".
[^eksfargate]: AWS, ["Simplify compute management with AWS Fargate"](https://docs.aws.amazon.com/eks/latest/userguide/fargate.html) — EKS on Fargate considerations: no DaemonSets, no privileged containers, no `HostPort` or `HostNetwork`, no EBS volumes, a dedicated kernel per pod, and IRSA for IAM credentials.
