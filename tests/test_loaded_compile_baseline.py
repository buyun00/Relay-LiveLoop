from __future__ import annotations
import copy, hashlib, json, tempfile, unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from host.applied_compile_baseline import current_profile, retain_verified_reload, validate_loaded_rows
from host.artifacts import ArtifactStore
from host.errors import CommandError
from host.ledger import Ledger
from host.native_compile_profile import NativeCompileAssemblyProfile, NativeCompileProfile, NativeCompileProfileRegistry, native_compile_profile_digest
from host.native_compile_provider import NativeSourcePreparationProvider


class CompletedReloadBaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-loaded-baseline-')
        root = Path(self.temp.name); self.root = root / 'project'; self.root.mkdir(); unity = root / 'unity'; unity.mkdir()
        (self.root / 'Library').mkdir(); (self.root / 'Library/original.dll').write_bytes(b'original-metadata')
        self.ledger = Ledger(root / 'ledger.sqlite3'); self.artifacts = ArtifactStore(self.ledger, [root])
        self.addCleanup(self.temp.cleanup); self.addCleanup(self.ledger.close)
        self.task = self.ledger.create_task({'sessionId': 'session-synthetic', 'goal': 'Modify the example module', 'target': 'example-page',
                                           'allowedImpact': {'reloadModules': ['Example.Module']}, 'acceptance': {}})
        self.profile = NativeCompileProfile('example-profile', 'example-page', self.root, unity, '2022.3', 'StandaloneWindows64', 'Standalone',
                                           'Debug', True, 0, (), (), (), (), 'Example.Module', ('Example.Module',), 0, 'Example.Module',
                                           (NativeCompileAssemblyProfile('Example.Module', 'Example.Module', (), 'Library/original.dll', hashlib.sha256(b'original-metadata').hexdigest()),), 10)
        self.runtime = SimpleNamespace(_ledger=self.ledger, _artifacts=self.artifacts, _session_id='session-synthetic', _launch_id='launch-synthetic',
            _profile_resolver=NativeCompileProfileRegistry([self.profile]), _last_states={self.task['taskId']: {'moduleGeneration': 1}}, is_verified=True)
        self.provider = object.__new__(NativeSourcePreparationProvider)
        self.provider._profiles=self.runtime._profile_resolver; self.provider._runtime=self.runtime; self.provider._ledger=self.ledger; self.provider._artifacts=self.artifacts
        self.ledger.create_job({'jobId':'prepare-synthetic','requestId':'request-prepare','operation':'prepare','taskId':self.task['taskId'],'state':'completed','stage':'prepared','runtimeChanged':False,'result':{},'error':None})
        digest = hashlib.sha256(b'reloaded-metadata').hexdigest(); self.snapshot='sha256:'+'a'*64
        self.dll = self.register('dll.bin', b'reloaded-metadata', 'runtime_reload_dll', 'prepare-synthetic')
        receipt = {'schema':'relay.liveloop.native-compile-input-receipt','version':1,'inputSnapshot':self.snapshot,
                   'profileDigest':native_compile_profile_digest(self.profile),'compileResultStatus':'SUCCESS','typeDbPresent':True,'inputSetMatches':True,'graphMatch':True,
                   'outputs':[{'relativePath':'Example.Module.dll','sha256':digest,'size':len(b'reloaded-metadata')}]}
        self.receipt=self.register('receipt.json',json.dumps(receipt).encode(),'native_compile_input_receipt','prepare-synthetic')
        self.payload={'name':'Example.Module','dllArtifactId':self.dll['artifactId']}
        manifest={'schema':'relay.liveloop.runtime-update-manifest','route':'MODULE_RELOAD','taskId':self.task['taskId'],'sessionId':self.task['sessionId'],
                  'launchId':'launch-synthetic','moduleId':'Example.Module','dependencyClosure':['Example.Module'],'inputSnapshot':self.snapshot,'payloads':[self.payload],'nextGeneration':1}
        self.manifest=self.register('manifest.json',json.dumps(manifest).encode(),'runtime_update_manifest','prepare-synthetic')
        self.plan=self.ledger.create_plan({'taskId':self.task['taskId'],'sessionId':self.task['sessionId'],'inputSnapshot':self.snapshot,'expectedRuntimeRevision':'revision-before',
                  'route':'MODULE_RELOAD','state':'prepared','prepareComplete':True,'approvalRequired':False,'details':{
                  'artifacts':[self.manifest], 'preparationEvidence':{
                  'providerId':'source-snapshot-native-compile-preparation','runtimeManifestArtifactId':self.manifest['artifactId'],'compileInputReceiptArtifactId':self.receipt['artifactId'],
                  'compileInputReceiptSha256':self.receipt['sha256'],'profileDigest':receipt['profileDigest'],'editorJobId':'prepare-synthetic'}}})
        self.ledger.create_job({'jobId':'iterate-synthetic','requestId':'request-iterate','operation':'iterate','taskId':self.task['taskId'],'planId':self.plan['planId'],
                               'state':'running','stage':'runtime_apply','runtimeChanged':None,'result':{},'error':None})
        self.loaded=[{'assemblyName':'Example.Module','loadedAssemblyName':'Example.Module','loadedAssemblyFullName':'Example.Module, Version=1.0.0.0',
                      'taskId':self.task['taskId'],'inputSha256':'sha256:'+'b'*64,'generation':1,'loadedDllSha256':'sha256:'+digest,'loadedPdbSha256':'',
                      'moduleVersionId':'10000000-0000-0000-0000-000000000001','loaderApi':'example-native-unloadable-loader'}]
        self.after={'runtimeRevision':'revision-after','moduleGeneration':1,'viewGeneration':1}
        self.retain()

    def register(self,name,raw,kind,job):
        path=Path(self.temp.name)/name;path.write_bytes(raw)
        return self.artifacts.register(path,kind=kind,artifact_id='artifact-'+name.replace('.','-'),task_id=self.task['taskId'],job_id=job)
    def retain(self):
        retain_verified_reload(self.runtime,self.plan,{'payloads':[self.payload]},'iterate-synthetic',self.after,{'inputSha256':'sha256:'+'b'*64,'rows':self.loaded})
    def complete(self):
        self.ledger.update_job('iterate-synthetic',state='completed',stage='runtime_reconciled',runtime_changed=True,
                               result={'facts':{'runtimeMatched':True},'runtimeRevisionAfter':'revision-after'},error=None)
    def test_normal_profile_lookup_only_advances_after_real_terminal_and_preserves_original(self):
        with self.assertRaises(CommandError):self.provider.profile_for_task(self.task)
        self.complete();derived=self.provider.profile_for_task(self.task)
        self.assertEqual(derived.initial_module_generation,1)
        self.assertEqual((self.root/derived.assemblies[0].baseline_path).read_bytes(),b'reloaded-metadata')
        self.assertEqual((self.root/self.profile.assemblies[0].baseline_path).read_bytes(),b'original-metadata')
        self.assertEqual(self.provider.profile_for_task(self.task),derived)
        self.assertNotEqual(native_compile_profile_digest(derived),native_compile_profile_digest(self.profile))
    def test_pending_failed_unknown_no_advance_and_no_copy(self):
        for state in ('queued','running','failed','state_unknown'):
            self.ledger.update_job('iterate-synthetic',state=state,stage='synthetic',runtime_changed=None,result={},error=None)
            with self.assertRaises(CommandError):self.provider.profile_for_task(self.task)
        self.assertFalse((self.root/'Library/RelayLiveLoopAppliedBaselines').exists())
    def test_wrong_launch_generation_or_profile_rejects(self):
        self.complete()
        self.runtime._launch_id='other-launch'
        with self.assertRaises(CommandError):self.provider.profile_for_task(self.task)
        self.runtime._launch_id='launch-synthetic';self.runtime._last_states[self.task['taskId']]['moduleGeneration']=2
        with self.assertRaises(CommandError):self.provider.profile_for_task(self.task)
        self.runtime._last_states[self.task['taskId']]['moduleGeneration']=1
        with self.assertRaises(CommandError):current_profile(self.ledger,self.artifacts,self.runtime,self.task,replace(self.profile,configuration='Release'))
    def test_registered_payload_or_frozen_file_drift_rejects(self):
        self.complete();derived=self.provider.profile_for_task(self.task)
        path=self.root/derived.assemblies[0].baseline_path;path.write_bytes(b'changed')
        with self.assertRaises(CommandError):self.provider.profile_for_task(self.task)
    def test_receipt_failure_or_output_mismatch_rejects(self):
        self.complete();owner=self.ledger.get_artifact(self.receipt['artifactId'])
        Path(owner['absolutePath']).write_bytes(b'changed-registered-receipt')
        with self.assertRaises(CommandError):self.provider.profile_for_task(self.task)
    def test_loader_ack_exact_closure_binding_has_negative_cases(self):
        candidate={'payloads':[{'name':'Example.Module','generationAfter':1,'dllSha256':self.loaded[0]['loadedDllSha256'],'pdbBase64':'','pdbSha256':'sha256:'+'0'*64}]}
        self.assertEqual(validate_loaded_rows(self.plan,{},candidate,'sha256:'+'b'*64,self.loaded),self.loaded)
        for key,value in [('taskId','wrong-task'),('inputSha256','wrong-input'),('generation',True),('generation',2),('loadedDllSha256','sha256:'+'0'*64),('loadedAssemblyName','other'),('moduleVersionId','00000000-0000-0000-0000-000000000000')]:
            rows=copy.deepcopy(self.loaded);rows[0][key]=value
            with self.assertRaises(CommandError):validate_loaded_rows(self.plan,{},candidate,'sha256:'+'b'*64,rows)
        for rows in (None,[],self.loaded+self.loaded):
            with self.assertRaises(CommandError):validate_loaded_rows(self.plan,{},candidate,'sha256:'+'b'*64,rows)

if __name__=='__main__':unittest.main()
