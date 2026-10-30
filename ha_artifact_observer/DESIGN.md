# Fixed-target artifact observation

Version `0.0.4` is a standalone artifact-only delivery slice. It has no Delivery,
Registry, Controller or task-execution dependency. Source completion, local
verification, image builds and actual HA acceptance are separate evidence.
The slice must not be described as deployed until instance acceptance completes.

The administrator binds one Docker instance and exact target/container names to
independent bearer principals. A caller provides only a logical target. Three
fixed AF_UNIX GETs fence container inspect, the actual associated image inspect,
and the original container inspect again. Changes, unavailable facts, stopped
containers and malformed/over-budget data are rejected. No list, Docker write,
exec, pull, build, shell, arbitrary path or arbitrary URL interface exists.

Facts preserve the actual complete container ID, Docker ImageID and associated
OCI RepoDigests as separate types. Empty RepoDigests remain empty. Expected
release metadata and target self-reports never fill missing engine facts.
Facts do not prove registry signatures, writable-layer contents, mounted data,
application health or successful deployment/restore.

Only this independently trusted service may receive the socket after explicit
authorization for each instance and target scope. A read-only socket mount does
not limit Docker verbs; a compromised process can affect the host. Public
metadata therefore keeps Docker API, Supervisor API and observation disabled,
with manual boot and no host network, host-port publication or privileged mode.
Runtime switches do not grant authority or create a socket attachment.

The standard image includes seven fixed Python modules and a fixed run script.
It includes no Controller/Delivery code, preview library, registration loader,
runtime assets, user state or credentials. An API-only injectable preview hook
is retained for separately reviewed compositions; standard main never installs
it, so its route returns 404. No Python factory can be chosen by an option,
environment variable or request.

Connection handlers are bounded and expire absolute slow request-line/headers
after five seconds. GET framing rejects duplicate lengths and all transfer or
content encodings. Docker read budgets cannot be extended by a slow status
line, headers or chunk stream. The optional injected hook has a smaller total
budget and cannot return a late MAC. Errors expose stable codes only.

Configuration, acceptance and revocation are specified in [DOCS.md](DOCS.md).
Revocation removes caller credentials, disables/stops the observer and removes
its socket permission; no target service needs to be modified. The observer
stores no observation database and never mutates the engine.

Protocol references:

- [Moby v28.0.1 image inspect fields](https://github.com/moby/moby/blob/v28.0.1/api/types/image/image_inspect.go#L18-L43)
- [OCI Image Spec v1.1.1 ImageID](https://github.com/opencontainers/image-spec/blob/v1.1.1/config.md#imageid)

OCI config identity and an engine's actual runtime identity are not assumed
interchangeable across engine storage implementations.
