# Optional qualified execution assets

Default source builds contain no execution policy, runtime or credential.
`runtime-build/stage_execution.py` prepares a new private context from explicit
policy and fully hashed runtime trees. It copies only the public Python source,
fixed launcher, pinned assets and optional SHA-pinned official native fnOS CLI.
The stage receipt records hashes; it never grants host or deployment authority.

Live keys, OAuth sessions, options, logs, databases, user files and tests must
never be staged here or copied into an image. Instance policies and generated
binary trees are ignored by Git. Provision secrets separately in the documented
private `/data` locations only after authorization and live qualification.

Failed staging leaves a private partial context without a success receipt.
Do not reuse or build that directory. No preparation command installs an Add-on,
registers a Runner, enables execution, changes accounts or publishes source.


An optional `--release-authority-policy` input is validated as closed nonsecret
data and copied only to `release-authority-policy.json`; source manifest and
staging receipt pin its exact SHA256. Private role/read tokens and registration
are never accepted as staging inputs. Without this optional policy the authority
stays unavailable. See [RELEASE_AUTHORITY_BOOT.md](../RELEASE_AUTHORITY_BOOT.md).
