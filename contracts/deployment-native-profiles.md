# Native compile profiles in local deployments

`bootstrap.ps1` accepts `-NativeCompileProfilesFile`, `-EditorJobRoot`, and
`-EditorArtifactRoot` together. These server-owned paths are stored in the private
machine configuration as `nativeCompile`. The profile registry must match that
machine's game project and installed Unity, including its exact baseline hashes.
Profile version 3 keeps the explicit external source roots and file lists; doctor
reports context version 4 and receipt version 2 separately from runtime evidence.

An existing protected `-RuntimeSessionFile` can also be configured. When starting
a Player-bound Host, start passes all four required Native preparation arguments
and admits the explicitly configured Editor artifact root. Without a session,
Native configuration remains available for inspection and portability; start
does not invent or authenticate a Player session. Attached processes remain
unowned and stop preserves them.

Portable export uses manifest version 2 when Native profiles are configured.
It replaces machine roots with root-relative references, hashes the exact source
and reference files, and carries only the explicitly trusted external source
files. Import rebuilds a new server profile from the discovered game checkout,
the configured installed `Editor/Unity.exe`, and `compiler-sources/<rootId>` under
the new data root. The rebuilt profile has a new digest. Import checks every
local project source, reference and baseline before publishing the profile.

Library, credentials and runtime sessions remain excluded. A destination needs
its own matching baseline and dependencies; missing or changed files fail the
import instead of substituting cached assemblies. Existing profiles are never
overwritten. Legacy exports without Native configuration retain manifest version
1. Profile reconstruction does not mark second-machine, Native, input or rendering
acceptance as passed.
