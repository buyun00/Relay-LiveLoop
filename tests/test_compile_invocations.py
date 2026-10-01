import copy, hashlib, tempfile, unittest
from pathlib import Path
from host.compile_invocations import validate_invocations


class InvocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-compiler-call-'); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); (self.root/'actual-compile-dll').mkdir()
        raw = b'synthetic immutable DLL'; self.output = {'relativePath':'Example.dll','sha256':hashlib.sha256(raw).hexdigest(),'size':len(raw)}
        for directory in (self.root,self.root/'actual-compile-dll'):(directory/'Example.dll').write_bytes(raw)
        self.rows=[]
        for operation,api,relative in [('actualCompileDll','HybridCLR.Editor.Commands.CompileDllCommand.CompileDll(string,BuildTarget,bool)','actual-compile-dll'),
                                      ('normalTypeDbCompile','UnityEditor.Build.Player.PlayerBuildInterface.CompilePlayerScripts','.')]:
            self.rows.append({'operation':operation,'api':api,'startedAtUtc':'2032-01-02T03:04:05.1234567Z',
                              'finishedAtUtc':'2032-01-02T03:04:06.1234567Z','elapsedTicks':123456,'frequency':100000,
                              'returned':True,'dllsMatchedReceipt':True,'outputDirectoryRelativePath':relative,'outputs':[copy.deepcopy(self.output)]})
    def verify(self,rows):
        return validate_invocations(rows,self.root,{'example.dll':self.output},lambda p:(hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_size))
    def test_real_per_call_shape_has_distinct_monotonic_clocks_and_bytes(self):
        self.assertEqual(self.verify(self.rows),self.rows)
        # A wall-clock adjustment does not change the observed monotonic duration.
        self.rows[0]['finishedAtUtc']='2031-01-02T03:04:05.1234567Z'
        self.assertEqual(self.verify(self.rows)[0]['elapsedTicks']/self.rows[0]['frequency'],1.23456)
    def test_missing_unknown_started_and_wrong_identity_reject(self):
        for rows in (None,[],[self.rows[0]],self.rows[::-1]):
            with self.assertRaises(ValueError):self.verify(rows)
        for key,value in [('returned',False),('dllsMatchedReceipt',False),('elapsedTicks',True),('elapsedTicks',-1),('frequency',0),
                          ('operation','dispatch'),('api','historical-compile'),('outputDirectoryRelativePath','../outside'),('startedAtUtc','2032-01-02')]:
            rows=copy.deepcopy(self.rows);rows[0][key]=value
            with self.assertRaises(ValueError):self.verify(rows)
    def test_independent_actual_compile_bytes_drift_or_closure_change_reject(self):
        (self.root/'actual-compile-dll/Example.dll').write_bytes(b'changed')
        with self.assertRaises(ValueError):self.verify(self.rows)
        (self.root/'actual-compile-dll/Example.dll').write_bytes(b'synthetic immutable DLL')
        rows=copy.deepcopy(self.rows);rows[0]['outputs']+=rows[0]['outputs']
        with self.assertRaises(ValueError):self.verify(rows)
        rows=copy.deepcopy(self.rows);rows[0]['outputs'][0]['relativePath']='../Example.dll'
        with self.assertRaises(ValueError):self.verify(rows)

if __name__=='__main__':unittest.main()
