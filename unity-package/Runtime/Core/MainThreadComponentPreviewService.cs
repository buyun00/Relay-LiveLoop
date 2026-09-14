#if UNITY_EDITOR || DEVELOPMENT_BUILD
using System;
using System.Collections.Generic;
using UnityEngine;

namespace RelayLiveLoop
{
    public sealed class MainThreadComponentPreviewService : IComponentPreviewProvider
    {
        private sealed class Overlay
        {
            public string OverlayId;
            public string TaskId;
            public RuntimeObjectHandle Component;
            public string Property;
            public ComponentValue Original;
            public ComponentValue Applied;
        }

        private readonly IRelayLiveLoopMainThreadGuard _mainThread;
        private readonly ObjectHandleRegistry _handles;
        private readonly Dictionary<string, Overlay> _overlays =
            new Dictionary<string, Overlay>(StringComparer.Ordinal);
        private readonly int _maximumOverlays;

        public MainThreadComponentPreviewService(
            IRelayLiveLoopMainThreadGuard mainThread,
            ObjectHandleRegistry handles,
            int maximumOverlays = 256)
        {
            _mainThread = mainThread ?? throw new ArgumentNullException(nameof(mainThread));
            _handles = handles ?? throw new ArgumentNullException(nameof(handles));
            if (maximumOverlays <= 0) throw new ArgumentOutOfRangeException(nameof(maximumOverlays));
            _maximumOverlays = maximumOverlays;
        }

        public RelayLiveLoopResult<ComponentPreviewResult> Preview(ComponentPreviewRequest request)
        {
            _mainThread.AssertMainThread();
            if (request == null || request.Component == null || request.Expected == null ||
                request.Replacement == null || string.IsNullOrWhiteSpace(request.Property) ||
                string.IsNullOrWhiteSpace(request.TaskId))
            {
                return Failure<ComponentPreviewResult>(
                    RelayLiveLoopErrorCode.InvalidMessage,
                    "component_preview",
                    "Preview request is incomplete.",
                    false,
                    false);
            }

            if (_overlays.Count >= _maximumOverlays)
            {
                return Failure<ComponentPreviewResult>(
                    RelayLiveLoopErrorCode.Busy,
                    "component_preview",
                    "Preview overlay capacity is full.",
                    true,
                    false);
            }

            var resolved = _handles.Resolve<Component>(request.Component);
            if (!resolved.Succeeded) return RelayLiveLoopResult<ComponentPreviewResult>.Failure(resolved.Error);
            ComponentValue current;
            if (!BuiltinComponentPropertyAccess.TryRead(resolved.Value, request.Property, out current))
            {
                return Failure<ComponentPreviewResult>(
                    RelayLiveLoopErrorCode.CapabilityUnavailable,
                    "component_preview",
                    "No registered generic preview provider supports this component/property pair.",
                    true,
                    false);
            }

            if (!current.EquivalentTo(request.Expected))
            {
                return Failure<ComponentPreviewResult>(
                    RelayLiveLoopErrorCode.InputChanged,
                    "component_preview",
                    "Current component value differs from the expected original value.",
                    true,
                    false);
            }

            if (!BuiltinComponentPropertyAccess.TryWrite(resolved.Value, request.Property, request.Replacement))
            {
                return Failure<ComponentPreviewResult>(
                    RelayLiveLoopErrorCode.CapabilityUnavailable,
                    "component_preview",
                    "Replacement value kind is not supported for this property.",
                    true,
                    false);
            }

            ComponentValue readback;
            if (!BuiltinComponentPropertyAccess.TryRead(resolved.Value, request.Property, out readback) ||
                !readback.EquivalentTo(request.Replacement))
            {
                var restored = BuiltinComponentPropertyAccess.TryWrite(resolved.Value, request.Property, current);
                return Failure<ComponentPreviewResult>(
                    RelayLiveLoopErrorCode.RestoreFailed,
                    "component_preview_readback",
                    restored
                        ? "Preview readback differed; original value was restored."
                        : "Preview readback differed and automatic restoration failed.",
                    false,
                    !restored);
            }

            var overlayId = Guid.NewGuid().ToString("N");
            _overlays.Add(overlayId, new Overlay
            {
                OverlayId = overlayId,
                TaskId = request.TaskId,
                Component = request.Component,
                Property = request.Property,
                Original = current,
                Applied = request.Replacement
            });
            return RelayLiveLoopResult<ComponentPreviewResult>.Success(new ComponentPreviewResult
            {
                OverlayId = overlayId,
                TaskId = request.TaskId,
                Component = request.Component,
                Property = request.Property,
                Original = current,
                Applied = request.Replacement,
                Readback = readback,
                Frame = Time.frameCount,
                FormalSourceChanged = false
            });
        }

        public RelayLiveLoopResult<ComponentRevertResult> Revert(string taskId, string overlayId)
        {
            _mainThread.AssertMainThread();
            Overlay overlay;
            if (string.IsNullOrWhiteSpace(taskId) || string.IsNullOrWhiteSpace(overlayId) ||
                !_overlays.TryGetValue(overlayId, out overlay))
            {
                return Failure<ComponentRevertResult>(
                    RelayLiveLoopErrorCode.StaleTarget,
                    "component_revert",
                    "Preview overlay is missing or expired.",
                    true,
                    false);
            }

            if (!string.Equals(taskId, overlay.TaskId, StringComparison.Ordinal))
            {
                return Failure<ComponentRevertResult>(
                    RelayLiveLoopErrorCode.ContractMismatch,
                    "component_revert",
                    "Preview overlay belongs to another task.",
                    false,
                    false);
            }

            var resolved = _handles.Resolve<Component>(overlay.Component);
            if (!resolved.Succeeded)
            {
                _overlays.Remove(overlayId);
                return RelayLiveLoopResult<ComponentRevertResult>.Failure(resolved.Error);
            }

            ComponentValue current;
            if (!BuiltinComponentPropertyAccess.TryRead(resolved.Value, overlay.Property, out current))
            {
                _overlays.Remove(overlayId);
                return Failure<ComponentRevertResult>(
                    RelayLiveLoopErrorCode.CapabilityUnavailable,
                    "component_revert",
                    "The property is no longer supported.",
                    false,
                    null);
            }

            if (!current.EquivalentTo(overlay.Applied))
            {
                return Failure<ComponentRevertResult>(
                    RelayLiveLoopErrorCode.InputChanged,
                    "component_revert",
                    "Current value changed after preview; revert will not overwrite it.",
                    true,
                    true);
            }

            if (!BuiltinComponentPropertyAccess.TryWrite(resolved.Value, overlay.Property, overlay.Original))
            {
                return Failure<ComponentRevertResult>(
                    RelayLiveLoopErrorCode.RestoreFailed,
                    "component_revert",
                    "Original value could not be restored.",
                    false,
                    true);
            }

            ComponentValue readback;
            if (!BuiltinComponentPropertyAccess.TryRead(resolved.Value, overlay.Property, out readback) ||
                !readback.EquivalentTo(overlay.Original))
            {
                return Failure<ComponentRevertResult>(
                    RelayLiveLoopErrorCode.RestoreFailed,
                    "component_revert_readback",
                    "Revert did not read back the original value.",
                    false,
                    true);
            }

            _overlays.Remove(overlayId);
            return RelayLiveLoopResult<ComponentRevertResult>.Success(new ComponentRevertResult
            {
                OverlayId = overlayId,
                Restored = overlay.Original,
                Readback = readback,
                Frame = Time.frameCount
            });
        }

        public int PurgeScopeGeneration(string scopeId, long generation)
        {
            _mainThread.AssertMainThread();
            var remove = new List<string>();
            foreach (var pair in _overlays)
            {
                if (pair.Value.Component.Generation == generation &&
                    string.Equals(pair.Value.Component.ScopeId, scopeId, StringComparison.Ordinal))
                {
                    remove.Add(pair.Key);
                }
            }

            foreach (var id in remove) _overlays.Remove(id);
            return remove.Count;
        }

        public void Clear()
        {
            _mainThread.AssertMainThread();
            _overlays.Clear();
        }

        private static RelayLiveLoopResult<T> Failure<T>(
            RelayLiveLoopErrorCode code,
            string stage,
            string message,
            bool recoverable,
            bool? runtimeChanged)
        {
            return RelayLiveLoopResult<T>.Failure(new RelayLiveLoopError(
                code,
                stage,
                message,
                runtimeChanged,
                recoverable,
                Array.Empty<string>()));
        }
    }
}
#endif
