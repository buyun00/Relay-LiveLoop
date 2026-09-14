# Generic provider boundaries

Relay LiveLoop keeps stable orchestration code separate from application modules and optional SDK adapters.

| Provider | Input | Output and required behavior |
| --- | --- | --- |
| Command service | operation, request ID, task ID, arguments, caller identity | One idempotent result or job ID; transport adapters do not duplicate orchestration |
| Host lifecycle | authenticated graceful request, wait policy, preserve-Player assertion | Explicit active and non-interruptible jobs; drains new work and exits only when the durable ledger is safe |
| Editor job provider | job ID, kind, immutable input snapshot, provider ID, artifact root | Persisted stage, diagnostics, and content-addressed artifacts |
| Compile provider | build target, defines, references, immutable source input | Candidate assemblies, diagnostics, elapsed time, and exact configuration; never substitutes an older artifact after failure |
| Asset build provider | immutable inputs and affected asset identifiers | Complete candidate release and dependency set; preparation does not activate it |
| Source provider | stable source address and expected old value | Unique source resolution and before/after result; ambiguous sources are rejected |
| Runtime transport | authenticated command, session, expected runtime revision | Main-thread execution stage and result; transport threads do not access Unity objects |
| Hotfix provider | verified assemblies, exact method set, module generation | Actual apply result tied to the current generation |
| Resource provider | prepared release and approved scope | Active release and consumer generation; shared resources are not forcibly unloaded |
| Module provider | module ID, dependency closure, context payload | Quiesce, dispose, unload, load, and restore evidence without retaining old module objects |
| View provider | stable view ID and current generation | Observe, refresh, context capture/restore, rebuild, and stable-state evidence |
| Evidence provider | task, plan, target generation, assertions | Fresh frame, state, input, and check artifacts; absent evidence remains absent |

Stable contracts must not keep runtime `Type`, delegate, task, socket, token, or engine object references from a replaceable module. Serialized context is versioned plain data.
