"""Read-only validation of per-call timings and immutable compiler output bindings."""
from datetime import datetime
from pathlib import Path
import re

from .native_compile_profile import _reject_reparse_path


def validate_invocations(rows, output_root, output_by_name, hash_file):
    if not isinstance(rows, list) or len(rows) != 2:
        raise ValueError('Timing receipt must contain the two distinct actual compiler calls')
    operations = ('actualCompileDll', 'normalTypeDbCompile')
    apis = ('HybridCLR.Editor.Commands.CompileDllCommand.CompileDll(string,BuildTarget,bool)',
            'UnityEditor.Build.Player.PlayerBuildInterface.CompilePlayerScripts')
    expected_dlls = {key: value for key, value in output_by_name.items() if key.endswith('.dll')}
    if not expected_dlls:
        raise ValueError('No sealed DLL outputs exist for the actual compiler calls')
    keys = {'operation', 'api', 'startedAtUtc', 'finishedAtUtc', 'outputDirectoryRelativePath',
            'elapsedTicks', 'frequency', 'returned', 'dllsMatchedReceipt', 'outputs'}
    for index, row in enumerate(rows):
        relative = 'actual-compile-dll' if index == 0 else '.'
        if (not isinstance(row, dict) or set(row) != keys or row['operation'] != operations[index]
                or row['api'] != apis[index] or row['outputDirectoryRelativePath'] != relative
                or row['returned'] is not True or row['dllsMatchedReceipt'] is not True
                or type(row['elapsedTicks']) is not int or not 0 <= row['elapsedTicks'] <= 2**63 - 1
                or type(row['frequency']) is not int or not 1 <= row['frequency'] <= 2**63 - 1):
            raise ValueError('Actual compiler call identity, clock, or terminal evidence is invalid')
        for field in ('startedAtUtc', 'finishedAtUtc'):
            value = row[field]
            if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}Z', value):
                raise ValueError('Compiler call UTC representation is invalid')
            datetime.fromisoformat(value.replace('Z', '+00:00'))
        # UTC identifies the invocation. Elapsed duration uses the monotonic ticks,
        # independently of any wall-clock adjustment during the synchronous call.
        directory = output_root / relative
        _reject_reparse_path(output_root, directory)
        outputs = row['outputs']
        if not isinstance(outputs, list) or not 1 <= len(outputs) <= 4096:
            raise ValueError('Compiler call output inventory is empty or exceeds its bound')
        actual_dlls = {}; seen = set()
        for item in outputs:
            if (not isinstance(item, dict) or set(item) != {'relativePath', 'sha256', 'size'}
                    or not isinstance(item['relativePath'], str) or item['relativePath'] in ('', '.', '..')
                    or any(c in item['relativePath'] for c in '/\\:\r\n\0')
                    or not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])
                    or type(item['size']) is not int or item['size'] < 0):
                raise ValueError('Actual compiler call output inventory is malformed')
            name = item['relativePath'].casefold()
            if name in seen:
                raise ValueError('Compiler call repeats an output name')
            seen.add(name); path = directory / item['relativePath']
            _reject_reparse_path(output_root, path)
            if not path.is_file() or hash_file(path) != (item['sha256'], item['size']):
                raise ValueError('Actual compiler call output bytes changed')
            if name.endswith('.dll'):
                actual_dlls[name] = (item['sha256'], item['size'])
        if actual_dlls != {key: (value['sha256'], value['size']) for key, value in expected_dlls.items()}:
            raise ValueError('Actual compiler DLL bytes differ from the normal TypeDB/payload receipt')
    return rows
