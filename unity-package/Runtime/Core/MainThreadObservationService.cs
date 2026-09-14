#if UNITY_EDITOR || DEVELOPMENT_BUILD
using System;
using System.Collections.Generic;
using System.Text;
using UnityEngine;

namespace RelayLiveLoop
{
    public sealed class MainThreadObservationService : IRuntimeObservationProvider
    {
        private readonly RuntimeSessionIdentity _identity;
        private readonly IRelayLiveLoopMainThreadGuard _mainThread;
        private readonly ObjectHandleRegistry _handles;
        private readonly Queue<RuntimeObjectHandle> _issuedComponentHandles = new Queue<RuntimeObjectHandle>();
        private readonly int _maximumIssuedComponentHandles;

        public MainThreadObservationService(
            RuntimeSessionIdentity identity,
            IRelayLiveLoopMainThreadGuard mainThread,
            ObjectHandleRegistry handles,
            int maximumIssuedComponentHandles = 512)
        {
            _identity = identity ?? throw new ArgumentNullException(nameof(identity));
            _mainThread = mainThread ?? throw new ArgumentNullException(nameof(mainThread));
            _handles = handles ?? throw new ArgumentNullException(nameof(handles));
            if (maximumIssuedComponentHandles < 16) throw new ArgumentOutOfRangeException(nameof(maximumIssuedComponentHandles));
            _maximumIssuedComponentHandles = maximumIssuedComponentHandles;
        }

        public RelayLiveLoopResult<ObjectObservation> ObserveObject(RuntimeObjectHandle target, int maximumComponents)
        {
            _mainThread.AssertMainThread();
            if (maximumComponents <= 0 || maximumComponents > 64)
            {
                return Failure<ObjectObservation>(
                    RelayLiveLoopErrorCode.InvalidMessage,
                    "observe",
                    "Maximum component count must be between 1 and 64.",
                    false);
            }

            var resolved = _handles.Resolve<UnityEngine.Object>(target);
            if (!resolved.Succeeded) return RelayLiveLoopResult<ObjectObservation>.Failure(resolved.Error);
            var gameObject = resolved.Value as GameObject;
            var targetComponent = resolved.Value as Component;
            if (gameObject == null && targetComponent != null) gameObject = targetComponent.gameObject;
            if (gameObject == null)
            {
                return Failure<ObjectObservation>(
                    RelayLiveLoopErrorCode.CapabilityUnavailable,
                    "observe",
                    "This object type has no generic GameObject observation provider.",
                    true);
            }

            var observedComponents = new List<ObservedComponent>();
            var components = gameObject.GetComponents<Component>();
            for (var index = 0; index < components.Length && observedComponents.Count < maximumComponents; index++)
            {
                var component = components[index];
                if (component == null) continue;
                var componentHandle = _handles.Register(target.ScopeId, component);
                TrackIssuedHandle(componentHandle);
                var behaviour = component as Behaviour;
                observedComponents.Add(new ObservedComponent
                {
                    TypeName = component.GetType().FullName ?? component.GetType().Name,
                    Handle = componentHandle,
                    Enabled = behaviour == null ? (bool?)null : behaviour.enabled
                });
            }

            return RelayLiveLoopResult<ObjectObservation>.Success(new ObjectObservation
            {
                SessionId = _identity.SessionId,
                RuntimeRevision = _identity.RuntimeRevision,
                Frame = Time.frameCount,
                RealtimeSeconds = Time.realtimeSinceStartupAsDouble,
                Target = target,
                Name = gameObject.name ?? string.Empty,
                HierarchyPath = BuildHierarchyPath(gameObject.transform),
                InstanceId = gameObject.GetInstanceID(),
                ActiveSelf = gameObject.activeSelf,
                ActiveInHierarchy = gameObject.activeInHierarchy,
                Components = observedComponents
            });
        }

        public RelayLiveLoopResult<ComponentPropertyObservation> ObserveProperty(
            RuntimeObjectHandle component,
            string property)
        {
            _mainThread.AssertMainThread();
            var resolved = _handles.Resolve<Component>(component);
            if (!resolved.Succeeded) return RelayLiveLoopResult<ComponentPropertyObservation>.Failure(resolved.Error);
            ComponentValue value;
            if (!BuiltinComponentPropertyAccess.TryRead(resolved.Value, property, out value))
            {
                return Failure<ComponentPropertyObservation>(
                    RelayLiveLoopErrorCode.CapabilityUnavailable,
                    "observe_component",
                    "No registered generic property provider supports this component/property pair.",
                    true);
            }

            return RelayLiveLoopResult<ComponentPropertyObservation>.Success(new ComponentPropertyObservation
            {
                Component = component,
                Property = property,
                Value = value,
                Frame = Time.frameCount
            });
        }

        private void TrackIssuedHandle(RuntimeObjectHandle handle)
        {
            _issuedComponentHandles.Enqueue(handle);
            while (_issuedComponentHandles.Count > _maximumIssuedComponentHandles)
            {
                _handles.Remove(_issuedComponentHandles.Dequeue());
            }
        }

        private static string BuildHierarchyPath(Transform transform)
        {
            var names = new List<string>();
            var current = transform;
            while (current != null && names.Count < 64)
            {
                names.Add(current.gameObject.name ?? string.Empty);
                current = current.parent;
            }

            var builder = new StringBuilder();
            for (var index = names.Count - 1; index >= 0; index--)
            {
                if (builder.Length > 0) builder.Append('/');
                builder.Append(names[index]);
                if (builder.Length >= 4096) break;
            }

            return builder.ToString();
        }

        private static RelayLiveLoopResult<T> Failure<T>(
            RelayLiveLoopErrorCode code,
            string stage,
            string message,
            bool recoverable)
        {
            return RelayLiveLoopResult<T>.Failure(RelayLiveLoopErrors.Create(code, stage, message, recoverable));
        }
    }
}
#endif
