# HA Artifact Observer

Independent fixed-target Docker artifact observation. Version `0.0.4` is an
artifact-only delivery slice, implemented using the Python standard library.
It can run without Delivery, Registry, Controller or a task executor.

The default Add-on requests no Docker API, Supervisor API, host network,
privileged mode, Ingress or published host port. Its observation switches are
off and its target/principal registrations are empty. The default service only
provides health; installing or building it does not enable observation.

With explicit authorization for an exact instance, the administrator can attach
the Docker socket to this one trusted observer, register exact targets and
independent bearer credentials, and enable both switches. The authenticated
`GET /v1/artifacts/{target_ref}` then returns the actual running container ID,
typed Docker ImageID and associated OCI RepoDigests. Three fixed Docker GETs
fence the container/image identity. Callers cannot choose Docker names, image
IDs, URLs, paths, commands or verbs. There is no Docker write API.

A Docker socket mounted read-only still permits Docker write requests. A
compromised observer could control the host. Each deployment therefore needs
explicit instance/target/socket authorization and a reviewed rollback; software
switches or source publication do not grant that authority. Other execution
components and task programs must not receive this socket.

The standard image contains no preview library, dynamic factory loader or
preview registration reader. Its optional preview qualification route remains
404. The API's injectable hook is reserved for separately reviewed trusted
compositions and cannot be enabled by options or a request.

See [DOCS.md](DOCS.md) for configuration, protocol, verification and revocation,
[DESIGN.md](DESIGN.md) for trust boundaries, and [CHANGELOG.md](CHANGELOG.md).
Local tests do not establish HA deployment, actual runtime identity, or a
complete software-delivery platform. Record those results separately after
instance acceptance.

From the repository root:

```sh
python3 -B -m unittest discover -s tests -p 'test_ha_artifact_observer*.py'
```

Container tests are opt-in through `OBSERVER_TEST_IMAGE`; normal discovery does
not access Docker. Test credentials, HTTP/Unix fixtures and image probes use
synthetic data, never production credentials or user workloads.
