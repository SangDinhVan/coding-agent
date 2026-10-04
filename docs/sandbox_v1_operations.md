# Safety sandbox operations

The supported profile is rootless Linux Docker with cgroups v2, immutable local
image/helper identities and independent SHADOW source admission. LIVE and RW
opaque shell are unsupported. Read/write/edit use exact one-use descriptor-based
helper grants against a private workspace. Arbitrary shell runs in a separate
RO container for each action. Neither container sees original source, immutable
recovery inputs, state, provider credentials or the controller daemon socket.
New CLI sessions publish each permitted write/edit through the trusted controller
to the selected source folder. Private-only internal/legacy sessions retain their
scope. Publication rechecks the before-image and root/parent identities under a
controller writer lock; file containers never gain a host mount. A failed or
unknown publication requires reconciliation of both private and source state.

Control state is private (directories 0700, records 0600) outside the source.
The before-generation includes admitted dirty and untracked bytes; source Git
hooks/filters are not used to read it. Generations use bounded independent
copies, reject links/special files/unsafe modes, and verify file/directory
identity around copying. File, aggregate and scan ceilings are 20 MiB, 100 MiB,
and 10000 entries; harvest has a 30-second deadline. The old free-space watchdog
is supplementary and is not a hard workspace quota.

Action containers require network none, non-root, rootfs RO, cap-drop ALL,
no-new-privileges, cgroup CPU/memory/PID limits and tmpfs size/nr_inodes. The
actual mount, helper and runtime probes must match. Removing the whole container
and verifying an empty daemon listing revokes detached descendants. Exit zero
still reports enforcement observation unknown. An unverified cleanup or
transport result quarantines the session and blocks mutation/harvest/export.

Trusted startup only accepts SANDBOX_IMAGE or CLI --image; it never builds an
image from admitted project code. Rebuild a trusted image only from reviewed
controller helper sources as an explicit setup task. Pin the resulting identity
and use the matching controller version. Docker Desktop is not physically
certified for this safe adapter. There is no host execution fallback.

Use /changes to stop the adapter and seal before/after bytes. /export requires
--export-root plus --allow-patch-export and an exact human-approved change-set
digest. Destination traversal is descriptor-relative and no-follow, creation is
exclusive. New CLI file writes are already published per action; there is no
whole-task automatic apply. Binary/non-UTF8 and unrepresentable
empty-directory changes deny export. Export publication audit failure is unknown
and quarantines the session. Never delete private backups to resolve a conflict.

Disclosure gates run before provider requests, local text sinks and patch writes.
Rejected raw data is discarded from bounded controller memory; only safe reason
metadata and hashes are retained. Traces remain private. Security provenance
and flags are controller annotations, never labels accepted from stdout, and
remain monotonic across compaction/restart. Declassification is unavailable.
Unknown/encoded secret detection and RW/external capability expansion remain
future work, not claims of the supported profile.

For commands and setup see [run guide](run_guide.md). For exact physical and
unit evidence see [safety acceptance](superpowers/safety-acceptance.md).
