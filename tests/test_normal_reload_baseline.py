import json, unittest
from dataclasses import replace
from host.native_compile_profile import native_compile_profile_digest
from tests.test_native_compile_preparation import NativeCompilePreparationTests as Fixture


class NormalReloadBaseline(unittest.TestCase):
    setUp=Fixture.setUp
    tearDown=Fixture.tearDown
    _write_profile=Fixture._write_profile
    _command=Fixture._command
    _open_task=Fixture._open_task
    _wait_job=Fixture._wait_job
    _prepare=Fixture._prepare

    def test_normal_command_service_reload_ack_to_next_prepare_baseline(self):
        original_invoke=self.player.invoke
        def invoke(**kwargs):
            reply=original_invoke(**kwargs)
            if kwargs['operation']=='module.load':
                command=json.loads(kwargs['payload']);candidate=command['arguments']['candidate'];body=json.loads(reply.payload)
                body['result']['loadedAssemblies']=[{
                    'taskId':command['taskId'],'inputSha256':candidate['inputSha256'],'assemblyName':row['name'],
                    'loadedAssemblyName':row['name'],'loadedAssemblyFullName':row['name']+', Version=1.0.0.0',
                    'moduleVersionId':'10000000-0000-0000-0000-000000000001','generation':row['generationAfter'],
                    'loadedDllSha256':row['dllSha256'],'loadedPdbSha256':row['pdbSha256'] if row['pdbBase64'] else '',
                    'loaderApi':'synthetic-verified-loader'} for row in candidate['payloads']]
                reply=replace(reply,payload=json.dumps(body).encode())
            return reply
        self.player.invoke=invoke;self.editor.mode='module';task_id=self._open_task('MODULE_RELOAD')
        first=self._prepare(task_id);self.assertEqual(first['state'],'completed',first)
        plan=first['result']['plan'];accepted=self._command('iterate',task_id,{'planId':plan['planId']})
        self.assertEqual(accepted['status'],'accepted',accepted)
        result=self._wait_job(accepted['jobId']);self.assertEqual(result['state'],'completed',result)
        derived=self.preparation.profile_for_task(self.ledger.get_task(task_id))
        self.assertEqual(derived.initial_module_generation,3)
        self.assertEqual((self.project_root/derived.assemblies[0].baseline_path).read_bytes(),b'compiled:module:Synthetic.Assembly')
        self.assertEqual(self.baseline_path.read_bytes(),self.baseline_bytes)
        # The same normal Prepare path consumes the new baseline/profile digest.
        # Only the synthetic compiler edge changes its next classification to body-only.
        self.editor.mode='hotfix'
        second=self._prepare(task_id);self.assertEqual(second['state'],'completed',second)
        self.assertEqual(second['result']['plan']['route'],'HOTFIX')
        self.assertEqual(second['result']['prepareBinding']['profileDigest'],native_compile_profile_digest(derived))
        self.assertEqual(self.editor.enqueue_bindings[-1]['prepareBinding']['profileDigest'],native_compile_profile_digest(derived))

del Fixture
if __name__=='__main__':unittest.main()
