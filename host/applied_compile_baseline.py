"""Use verified completed module loads as subsequent normal compiler baselines."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import uuid

from .errors import CommandError
from .composite_update import COMPOSITE_ROUTE
from .native_compile_profile import _reject_reparse_path, native_compile_profile_digest, verify_profile_baselines


SCOPE = 'loaded_compile_baseline'


def require(value, message):
    if not value:
        raise CommandError('STATE_UNKNOWN', message, stage='loaded_compile_baseline', runtime_changed=None, recoverable=False)


def validate_loaded_rows(plan, manifest, candidate, input_sha256, rows):
    """Validate authenticated terminal loader evidence against the exact sent bytes."""
    expected = {row['name']: row for row in candidate['payloads']}
    require(isinstance(rows, list) and rows and len(rows) == len(expected) == len(candidate['payloads']),
            'Terminal loader evidence does not exactly cover the payload closure.')
    names = set()
    for row in rows:
        require(isinstance(row, dict) and row.get('assemblyName') in expected and row['assemblyName'] not in names,
                'Terminal loader evidence has an unknown or duplicate assembly.')
        name = row['assemblyName']; payload = expected[name]; names.add(name)
        require(row.get('taskId') == plan['taskId'] and row.get('inputSha256') == input_sha256
                and row.get('loadedAssemblyName') == name and row.get('generation') == payload['generationAfter']
                and type(row.get('generation')) is int
                and row.get('loadedDllSha256') == payload['dllSha256']
                and row.get('loadedPdbSha256', '') == (payload['pdbSha256'] if payload.get('pdbBase64') else '')
                and isinstance(row.get('loadedAssemblyFullName'), str) and row['loadedAssemblyFullName']
                and isinstance(row.get('loaderApi'), str) and row['loaderApi'],
                'Terminal loader identity/generation/hash differs from the authenticated candidate.')
        try:
            mvid = uuid.UUID(row.get('moduleVersionId', ''))
        except (ValueError, TypeError, AttributeError) as exc:
            raise CommandError('STATE_UNKNOWN', 'Terminal loaded assembly MVID is unavailable.', stage='loaded_compile_baseline', runtime_changed=None) from exc
        require(mvid.int != 0, 'Terminal loaded assembly MVID is empty.')
    return json.loads(json.dumps(rows))


def retain_verified_reload(runtime, plan, manifest, job_id, after, loaded):
    """Retain evidence; not usable until the normal coordinator completes this job."""
    require(runtime._ledger is not None and isinstance(loaded, dict), 'No durable verified reload evidence exists.')
    task = runtime._ledger.get_task(plan['taskId'])
    profile = runtime._profile_resolver.for_task(task)
    evidence = plan['details'].get('preparationEvidence') or {}
    if plan['route'] == COMPOSITE_ROUTE:
        evidence = evidence.get('code') or {}
    require(evidence.get('providerId') == 'source-snapshot-native-compile-preparation'
            and evidence.get('runtimeManifestArtifactId') in {row['artifactId'] for row in plan['details']['artifacts']}
            and evidence.get('compileInputReceiptArtifactId') and evidence.get('compileInputReceiptSha256'),
            'Reload was not produced by the normal verified source compiler.')
    manifest_metadata = next(row for row in plan['details']['artifacts'] if row['artifactId'] == evidence['runtimeManifestArtifactId'])
    record = {'schema': 'relay.liveloop.loaded-compile-baseline', 'version': 1, 'taskId': task['taskId'],
              'sessionId': runtime._session_id, 'launchId': runtime._launch_id, 'moduleId': profile.module_id,
              'originalProfileDigest': native_compile_profile_digest(profile), 'planId': plan['planId'], 'jobId': job_id,
              'inputSnapshot': plan['inputSnapshot'], 'runtimeRevisionAfter': after['runtimeRevision'],
              'moduleGeneration': after['moduleGeneration'], 'viewGeneration': after['viewGeneration'],
              'compileInputReceiptArtifactId': evidence['compileInputReceiptArtifactId'],
              'compileInputReceiptSha256': evidence['compileInputReceiptSha256'],
              'editorJobId': evidence['editorJobId'],
              'runtimeManifestArtifactId': evidence['runtimeManifestArtifactId'],
              'runtimeManifestSha256': manifest_metadata['sha256'],
              'compilerProfileDigest': evidence['profileDigest'], 'loaderInputSha256': loaded['inputSha256'],
              'loadedAssemblies': loaded['rows'], 'payloads': manifest['payloads']}
    old = runtime._ledger.latest_version(runtime._session_id, SCOPE, profile.module_id)
    if old and old['generation'] == after['moduleGeneration']:
        require(old['metadata'] == record, 'Another verified reload baseline already occupies this generation.')
        return
    runtime._ledger.record_version({'sessionId': runtime._session_id, 'scope': SCOPE, 'subject': profile.module_id,
                                   'generation': after['moduleGeneration'], 'revision': after['runtimeRevision'],
                                   'state': 'verified_ack_requires_completed_job', 'appliedPlanId': plan['planId'], 'metadata': record})


def _registered_json(artifacts, artifact_id, expected_hash, task_id):
    meta, stream = artifacts.open_verified(artifact_id)
    with stream:
        owner = artifacts.ledger.get_artifact(artifact_id)
        require(owner.get('taskId') == task_id and meta.get('sha256') == expected_hash and meta.get('size', meta.get('sizeBytes', 0)) <= 16 * 1024 * 1024,
                'Registered compiler evidence changed or belongs to another task.')
        return json.loads(stream.read().decode('utf-8'))


def current_profile(ledger, artifacts, runtime, task, original):
    """Derive on every Prepare; never mutate configured profiles or advance on acceptance."""
    version = ledger.latest_version(task['sessionId'], SCOPE, original.module_id)
    if version is None:
        state = runtime._last_states.get(task['taskId'])
        require(state is None or state['moduleGeneration'] == original.initial_module_generation,
                'Observed generation has no completed compiler/load baseline; refuse the old baseline.')
        return original
    proof = version['metadata']; job = ledger.get_job(proof['jobId']); result = job.get('result') or {}
    require(job.get('operation') == 'iterate' and job.get('state') == 'completed' and job.get('runtimeChanged') is True
            and job.get('error') is None and result.get('facts', {}).get('runtimeMatched') is True
            and job.get('planId') == proof['planId'] and job.get('taskId') == proof['taskId']
            and result.get('runtimeRevisionAfter') == proof['runtimeRevisionAfter'],
            'Only a completed, matched normal Reload may advance the compilation baseline.')
    require(runtime.is_verified and runtime._session_id == task['sessionId'] == proof['sessionId']
            and runtime._launch_id == proof['launchId'] and proof['moduleId'] == original.module_id
            and proof['originalProfileDigest'] == native_compile_profile_digest(original),
            'Loaded baseline targets another launch/module or server-owned compile profile.')
    state = runtime._last_states.get(task['taskId'])
    require(state is not None and state['moduleGeneration'] == proof['moduleGeneration'] == version['generation'],
            'Current authenticated module generation differs from the completed Reload baseline.')
    plan = ledger.get_plan(proof['planId'])
    require(plan.get('taskId') == proof['taskId'] and plan.get('sessionId') == proof['sessionId']
            and plan.get('route') in {'MODULE_RELOAD', COMPOSITE_ROUTE} and plan.get('inputSnapshot') == proof['inputSnapshot'],
            'Durable plan no longer matches the completed Reload baseline.')
    manifest = _registered_json(artifacts, proof['runtimeManifestArtifactId'], proof['runtimeManifestSha256'], proof['taskId'])
    require(manifest.get('schema') == 'relay.liveloop.runtime-update-manifest' and manifest.get('route') == 'MODULE_RELOAD'
            and manifest.get('taskId') == proof['taskId'] and manifest.get('sessionId') == proof['sessionId']
            and manifest.get('launchId') == proof['launchId'] and manifest.get('moduleId') == original.module_id
            and manifest.get('dependencyClosure') == list(original.dependency_closure)
            and manifest.get('inputSnapshot') == proof['inputSnapshot'] and manifest.get('payloads') == proof['payloads']
            and manifest.get('nextGeneration') == proof['moduleGeneration'],
            'Loaded baseline proof differs from the exact registered normal runtime manifest.')
    receipt = _registered_json(artifacts, proof['compileInputReceiptArtifactId'], proof['compileInputReceiptSha256'], proof['taskId'])
    receipt_owner = ledger.get_artifact(proof['compileInputReceiptArtifactId'])
    compiler_job = ledger.get_job(proof['editorJobId'])
    require(receipt_owner['kind'] == 'native_compile_input_receipt' and receipt_owner['jobId'] == proof['editorJobId']
            and compiler_job['operation'] == 'prepare' and compiler_job['state'] == 'completed' and compiler_job['taskId'] == proof['taskId'],
            'Reload compiler receipt is not owned by its completed normal Prepare job.')
    require(receipt.get('schema') == 'relay.liveloop.native-compile-input-receipt'
            and receipt.get('inputSnapshot') == proof['inputSnapshot'] and receipt.get('profileDigest') == proof['compilerProfileDigest']
            and receipt.get('compileResultStatus') == 'SUCCESS' and receipt.get('typeDbPresent') is True
            and receipt.get('inputSetMatches') is True and receipt.get('graphMatch') is True,
            'Completed Reload has no matching immutable successful compiler receipt.')
    payloads = {row['name']: row for row in proof['payloads']}
    loaded = {row['assemblyName']: row for row in proof['loadedAssemblies']}
    require(len(payloads) == len(proof['payloads']) == len(loaded) == len(proof['loadedAssemblies'])
            and set(payloads) == set(loaded) == {row.name for row in original.assemblies},
            'Completed Reload does not cover the exact server-owned assembly closure.')
    root = original.project_root
    suffix = hashlib.sha256((proof['sessionId'] + '\n' + proof['launchId'] + '\n' + proof['planId']).encode()).hexdigest()
    directory = root / 'Library' / 'RelayLiveLoopAppliedBaselines' / suffix
    # Only task-owned Library copies. Never overwrite configured baseline or source assets.
    current = root
    for part in directory.relative_to(root).parts:
        current = current / part
        current.mkdir(exist_ok=True)
        _reject_reparse_path(root, current)
    assemblies = []
    outputs = {row['relativePath']: row for row in receipt['outputs']}
    for assembly in original.assemblies:
        payload = payloads[assembly.name]; observed = loaded[assembly.name]
        meta, stream = artifacts.open_verified(payload['dllArtifactId'])
        with stream:
            owner = artifacts.ledger.get_artifact(payload['dllArtifactId'])
            require(owner.get('taskId') == proof['taskId'] and meta.get('kind') == 'runtime_reload_dll'
                    and observed['loadedDllSha256'] == 'sha256:' + meta['sha256']
                    and observed['generation'] == proof['moduleGeneration'] and observed['inputSha256'] == proof['loaderInputSha256'],
                    'Actually loaded DLL evidence differs from the registered compiler payload.')
            output = outputs.get(assembly.name + '.dll')
            require(output is not None and output['sha256'] == meta['sha256'] and output['size'] == meta.get('size', meta.get('sizeBytes')),
                    'Runtime DLL bytes are not the sealed compiler output.')
            raw = stream.read()
        destination = directory / (assembly.name + '.dll')
        if destination.exists():
            _reject_reparse_path(root, destination)
            require(destination.read_bytes() == raw, 'Frozen loaded-baseline bytes drifted; never overwrite them.')
        else:
            with destination.open('xb') as file:
                file.write(raw); file.flush(); os.fsync(file.fileno())
        assemblies.append(replace(assembly, baseline_path=destination.relative_to(root).as_posix(), baseline_sha256=meta['sha256']))
    derived = replace(original, assemblies=tuple(assemblies), initial_module_generation=proof['moduleGeneration'])
    verify_profile_baselines(derived)
    return derived
