from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import relay_liveloop


class ServeProviderFactoryTests(TestCase):
    def run_serve(self, root: Path, **factories):
        args = Namespace(database=str(root / 'ledger.sqlite3'), artifact_root=[str(root / 'artifacts')],
                         machine_config=None, runtime_session_file=None, native_compile_profiles=None,
                         editor_job_root=None, editor_artifact_root=None, host='127.0.0.1', port=0, **factories)
        ledger = MagicMock()
        artifacts = object()
        service = SimpleNamespace(providers=MagicMock(), coordinator=None, close=MagicMock())
        server = MagicMock()
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch.object(relay_liveloop, '_token_from_args', return_value='synthetic-token'), \
             patch.object(relay_liveloop, 'Ledger', return_value=ledger), \
             patch.object(relay_liveloop, 'ArtifactStore', return_value=artifacts), \
             patch.object(relay_liveloop, 'build_command_service', return_value=service) as build, \
             patch.object(relay_liveloop, 'create_http_server', return_value=server):
            try:
                result = relay_liveloop._serve(args)
            except BaseException:
                ledger.close.assert_called_once()
                build.assert_not_called()
                raise
        ledger.close.assert_called_once()
        server.server_close.assert_called_once()
        return result, build.call_args

    def test_preparation_receives_the_same_source_and_wrapped_runtime(self):
        order = []
        source = object()
        runtime = object()
        preparation = object()

        def make_source(context):
            order.append('source')
            self.assertIsNone(context.runtime_transport)
            return source

        def make_runtime(context):
            order.append('runtime')
            self.assertIs(context.source_provider, source)
            self.assertIsNone(context.runtime_provider)
            return runtime

        def make_preparation(context):
            order.append('preparation')
            self.assertIs(context.source_provider, source)
            self.assertIs(context.runtime_provider, runtime)
            return preparation

        with TemporaryDirectory(prefix='synthetic-serve-factories-') as temporary:
            result, call = self.run_serve(Path(temporary), source_provider_factory=make_source,
                                        runtime_provider_factory=make_runtime, preparation_provider_factory=make_preparation)
        self.assertEqual(result, 0)
        self.assertEqual(order, ['source', 'runtime', 'preparation'])
        self.assertIs(call.kwargs['source_provider'], source)
        self.assertIs(call.kwargs['runtime_provider'], runtime)
        self.assertIs(call.kwargs['preparation_provider'], preparation)

    def test_optional_factories_leave_default_composition_available(self):
        with TemporaryDirectory(prefix='synthetic-serve-default-') as temporary:
            result, call = self.run_serve(Path(temporary))
        self.assertEqual(result, 0)
        for key in ('source_provider', 'runtime_provider', 'preparation_provider'):
            self.assertIsNone(call.kwargs[key])

    def test_runtime_factory_failure_closes_ledger_before_service_publication(self):
        def fail(_context):
            raise ValueError('synthetic runtime composition failed')

        with TemporaryDirectory(prefix='synthetic-serve-failure-') as temporary:
            with self.assertRaisesRegex(ValueError, 'synthetic runtime composition failed'):
                self.run_serve(Path(temporary), runtime_provider_factory=fail)
