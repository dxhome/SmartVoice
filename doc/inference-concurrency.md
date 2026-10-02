# SmartVoice inference concurrency

SmartVoice owns runtime admission and lifecycle management. Sherpa's native code,
model constructors, internal safety locks, generation parameters and output
formats are unchanged. The standard factory wraps the existing adapters with
`ElasticRuntimePool` and provider facades; services use provider-neutral ports.

## Defaults

| Setting | Default | Meaning |
|---|---:|---|
| `min_instances` | 1 | Retain this many warm instances per used Sherpa model after demand subsides. Models are loaded lazily, not all at startup. |
| `max_instances` | 2 | Maximum independent adapter runtimes per Sherpa model. |
| `instance_idle_seconds` | 300 | Reclaim idle extra instances after this interval. |
| `num_threads` | 2 | Fixed native constructor thread parameter per instance; never changed on a live session. |
| `max_queued_inference` | 4 | Waiting requests per model in adapter admission mode; excludes leased instances. |
| `max_concurrent_inference` | 1 | Enable adapter-managed parallel inference. `0` forces a single global inference slot. |

Environment equivalents are `SMARTVOICE_MIN_INSTANCES`, `SMARTVOICE_MAX_INSTANCES`,
`SMARTVOICE_INSTANCE_IDLE_SECONDS`, `SMARTVOICE_NUM_THREADS`,
`SMARTVOICE_MAX_QUEUED_INFERENCE` and `SMARTVOICE_MAX_CONCURRENT_INFERENCE`.
Existing configuration files are preserved and are not rewritten. Set
`max_concurrent_inference` to `1` (the default for new configurations) to enable
adapter-managed parallel inference; `0` deliberately forces serial inference.
Set `num_threads` to `2` to use the tested default configuration. This setting is
a binary enable switch despite its legacy name: values other than 0 or 1 are rejected.

The first request creates and warms one instance, and its result is returned to
that request. Subsequent demand can create the second instance while the first
serves another request. Initialization is serialized per model, outside the
pool lock. Waiting requests are FIFO per model. There is no batch accumulation.
The first successfully used instance remains resident; lowering `max_instances`
to 1 trades parallelism for memory. The minimum is a retention floor for instances
already created by demand, not a promise to load unused instances proactively.

Each instance is a separate existing adapter with independent native objects and
locks. Model pools are independent. ASR language-specific native sessions remain
in the existing adapter's cache; different languages can therefore increase
memory within an adapter instance. `max_instances` bounds adapter copies, not the total count of language-specific native ASR sessions cached inside those copies; the reported memory results use one fixed language per model. TTS languages, voices, speed and text do not
create separate pools. Constructor settings are fixed for the application's
lifetime. The optional language detector uses a separate pool. Limits apply per application process; multiple server processes have independent pools. New Sherpa model
types supported by the original adapter inherit the same mechanism without new
scheduler branches.

Additional instances copy only validated file fingerprints under the source
adapter's cache lock. The existing provider rechecks file size, modification time
and manifest hashes. Native model/session memory is not cloned or shared by this
mechanism; operating-system file cache may accelerate later construction. RSS
need not grow exactly in proportion to instance count.

## Admission, timeouts and shutdown

The outer HTTP queue remains bounded. When parallel inference is enabled and the
provider implements `ManagedAdmission`, its transport
capacity is the sum of backend model capacities (including bounded waiting),
rather than a global single inference slot. Its independent AnyIO thread limiter
prevents the default shared thread limit from serializing admitted model calls.
When set to `0`, the outer queue has one execution slot and applies the configured
global waiting limit. Providers without `ManagedAdmission` retain a conservative
single execution slot even when the switch is enabled; enabling this switch does
not add pooling to third-party providers.
Qwen keeps one backend runtime and bounded waiting; this change does not enable
native Qwen batching or multiple processes. Custom providers without the optional
`ManagedAdmission` capability retain the original global queue behavior.

For pooled Sherpa models, `max_queued_inference` is the number of requests waiting
for an instance per model; requests currently using an instance do not count
toward it. A full waiting queue is rejected with HTTP 503. Queue and execution
deadlines remain bounded and are configured separately. Raising the waiting
limit can absorb a short burst, but does not raise sustainable model throughput
or establish a latency target.

A queued request that disconnects or exceeds its HTTP execution deadline receives
a cancellation signal and is removed before acquiring a runtime. Once native
execution has started, cancellation cannot interrupt Sherpa: the instance remains
leased until computation ends. A client can thus receive 504 while computation
continues. Shutdown rejects queued/new work, waits for leased instances and then
closes runtimes. Idle reclamation never closes a busy or warming instance. Retiring instances continue to count against capacity until disposal finishes. Native object release does not guarantee an immediate return to the original single-instance RSS; the lifecycle benchmark records this separately.

Services reuse an adapter-provided model availability snapshot for up to one
second to avoid rescanning the whole catalog per request. This is routing metadata;
execution still validates its assets. Install/removal availability may take up to
one second to appear in inference routing. Public model-list and management
operations read current state without that routing cache.

`/v1/runtime` includes each backend's `pool_limits` and `instance_pools`: instance,
ready, active and waiting counts; peak active count; completed count; and the last
32 initialization request durations per model. Durations include constructor load
and that instance's first complete inference, rather than constructor time alone.
No request text or audio is retained in these metrics. Existing runtime-wait result
fields now include pool waiting time.

## Verification

See the [benchmark overview](../benchmarks/README.md) and [production harness
instructions](../benchmarks/elastic-pool/README.md) for the fixed-arrival HTTP
protocol, response validation, startup/expansion measurements, and CPU/RSS
methodology. The reviewed reports are embedded in each model/platform result:
[SenseVoice](../benchmarks/result/macos-arm64-stt-sensevoice-small-int8-fleurs-standard-2026-09-29.json),
[Matcha](../benchmarks/result/macos-arm64-tts-matcha-zh-baker-fleurs-standard-2026-09-29.json),
and [Supertonic](../benchmarks/result/macos-arm64-tts-supertonic-v3-multilingual-int8-fleurs-standard-2026-09-29.json).
They record the highest tested rate meeting the current success, P90, stability,
and drain checks on one macOS machine; they are not universal rate or latency
guarantees. Supertonic's 1,000-request confirmation is marked as insufficient for
the benchmark's 3,000-request formal reliability sample.
