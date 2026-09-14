#if UNITY_EDITOR
using System;
using System.Collections.Generic;

namespace RelayLiveLoop
{
    public sealed class EditorJobProviderRegistry
    {
        private readonly Dictionary<string, IEditorJobProvider> _providers =
            new Dictionary<string, IEditorJobProvider>(StringComparer.Ordinal);

        public IDisposable Register(IEditorJobProvider provider)
        {
            if (provider == null) throw new ArgumentNullException(nameof(provider));
            if (string.IsNullOrWhiteSpace(provider.ProviderId))
            {
                throw new ArgumentException("Provider id is required.", nameof(provider));
            }

            if (_providers.ContainsKey(provider.ProviderId))
            {
                throw new InvalidOperationException("Editor job provider is already registered.");
            }

            _providers.Add(provider.ProviderId, provider);
            return new Registration(this, provider.ProviderId, provider);
        }

        public bool TryResolve(string providerId, string kind, out IEditorJobProvider provider)
        {
            if (_providers.TryGetValue(providerId ?? string.Empty, out provider) && provider.Supports(kind))
            {
                return true;
            }

            provider = null;
            return false;
        }

        public void Clear()
        {
            _providers.Clear();
        }

        private sealed class Registration : IDisposable
        {
            private EditorJobProviderRegistry _registry;
            private readonly string _providerId;
            private readonly IEditorJobProvider _provider;

            public Registration(EditorJobProviderRegistry registry, string providerId, IEditorJobProvider provider)
            {
                _registry = registry;
                _providerId = providerId;
                _provider = provider;
            }

            public void Dispose()
            {
                var registry = _registry;
                if (registry == null) return;
                IEditorJobProvider current;
                if (registry._providers.TryGetValue(_providerId, out current) && ReferenceEquals(current, _provider))
                {
                    registry._providers.Remove(_providerId);
                }

                _registry = null;
            }
        }
    }
}
#endif
