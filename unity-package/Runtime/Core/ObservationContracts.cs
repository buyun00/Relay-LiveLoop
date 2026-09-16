#if UNITY_EDITOR || DEVELOPMENT_BUILD
using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;

namespace RelayLiveLoop
{
    public enum ComponentValueKind
    {
        Boolean,
        Integer,
        Float,
        String,
        Vector2,
        Vector3,
        Quaternion
    }

    public sealed class ComponentValue
    {
        public ComponentValueKind Kind { get; private set; }
        public bool BooleanValue { get; private set; }
        public long IntegerValue { get; private set; }
        public double FloatValue { get; private set; }
        public string StringValue { get; private set; }
        public double X { get; private set; }
        public double Y { get; private set; }
        public double Z { get; private set; }
        public double W { get; private set; }

        public static ComponentValue Boolean(bool value)
        {
            return new ComponentValue { Kind = ComponentValueKind.Boolean, BooleanValue = value };
        }

        public static ComponentValue Integer(long value)
        {
            return new ComponentValue { Kind = ComponentValueKind.Integer, IntegerValue = value };
        }

        public static ComponentValue Float(double value)
        {
            return new ComponentValue { Kind = ComponentValueKind.Float, FloatValue = value };
        }

        public static ComponentValue String(string value)
        {
            return new ComponentValue { Kind = ComponentValueKind.String, StringValue = value ?? string.Empty };
        }

        public static ComponentValue Vector2(double x, double y)
        {
            return new ComponentValue { Kind = ComponentValueKind.Vector2, X = x, Y = y };
        }

        public static ComponentValue Vector3(double x, double y, double z)
        {
            return new ComponentValue { Kind = ComponentValueKind.Vector3, X = x, Y = y, Z = z };
        }

        public static ComponentValue Quaternion(double x, double y, double z, double w)
        {
            return new ComponentValue { Kind = ComponentValueKind.Quaternion, X = x, Y = y, Z = z, W = w };
        }

        public bool EquivalentTo(ComponentValue other, double tolerance = 0.00001)
        {
            if (other == null || Kind != other.Kind) return false;
            switch (Kind)
            {
                case ComponentValueKind.Boolean: return BooleanValue == other.BooleanValue;
                case ComponentValueKind.Integer: return IntegerValue == other.IntegerValue;
                case ComponentValueKind.String: return string.Equals(StringValue, other.StringValue, StringComparison.Ordinal);
                case ComponentValueKind.Float: return NearlyEqual(FloatValue, other.FloatValue, tolerance);
                case ComponentValueKind.Vector2:
                    return NearlyEqual(X, other.X, tolerance) && NearlyEqual(Y, other.Y, tolerance);
                case ComponentValueKind.Vector3:
                    return NearlyEqual(X, other.X, tolerance) && NearlyEqual(Y, other.Y, tolerance) && NearlyEqual(Z, other.Z, tolerance);
                case ComponentValueKind.Quaternion:
                    return NearlyEqual(X, other.X, tolerance) && NearlyEqual(Y, other.Y, tolerance) &&
                        NearlyEqual(Z, other.Z, tolerance) && NearlyEqual(W, other.W, tolerance);
                default: return false;
            }
        }

        private static bool NearlyEqual(double left, double right, double tolerance)
        {
            return Math.Abs(left - right) <= tolerance;
        }
    }

    public sealed class ObservedComponent
    {
        public string TypeName { get; set; }
        public RuntimeObjectHandle Handle { get; set; }
        public bool? Enabled { get; set; }
    }

    public sealed class ObjectObservation
    {
        public string SessionId { get; set; }
        public string RuntimeRevision { get; set; }
        public long Frame { get; set; }
        public double RealtimeSeconds { get; set; }
        public RuntimeObjectHandle Target { get; set; }
        public string Name { get; set; }
        public string HierarchyPath { get; set; }
        public int InstanceId { get; set; }
        public bool ActiveSelf { get; set; }
        public bool ActiveInHierarchy { get; set; }
        public List<ObservedComponent> Components { get; set; }
    }

    public sealed class ComponentPropertyObservation
    {
        public string SessionId { get; set; }
        public string RuntimeRevision { get; set; }
        public RuntimeObjectHandle Component { get; set; }
        public string Property { get; set; }
        public ComponentValue Value { get; set; }
        public long Frame { get; set; }
    }

    public sealed class ComponentPreviewRequest
    {
        public string TaskId { get; set; }
        public RuntimeObjectHandle Component { get; set; }
        public string Property { get; set; }
        public ComponentValue Expected { get; set; }
        public ComponentValue Replacement { get; set; }
    }

    public sealed class ComponentPreviewResult
    {
        public string OverlayId { get; set; }
        public string TaskId { get; set; }
        public RuntimeObjectHandle Component { get; set; }
        public string Property { get; set; }
        public ComponentValue Original { get; set; }
        public ComponentValue Applied { get; set; }
        public ComponentValue Readback { get; set; }
        public long Frame { get; set; }
        public bool FormalSourceChanged { get; set; }
    }

    public sealed class ComponentRevertResult
    {
        public string OverlayId { get; set; }
        public ComponentValue Restored { get; set; }
        public ComponentValue Readback { get; set; }
        public long Frame { get; set; }
    }

    public sealed class FreshFrameCaptureRequest
    {
        public string ArtifactId { get; set; }
        public string ExpectedSessionId { get; set; }
        public string ExpectedRuntimeRevision { get; set; }
        public long ExpectedViewportGeneration { get; set; }
        public long MinimumFrameExclusive { get; set; }
        public int MaximumWidth { get; set; }
        public int MaximumHeight { get; set; }
        public TimeSpan Timeout { get; set; }
    }

    public sealed class FreshFrameArtifact
    {
        public string ArtifactId { get; set; }
        public string Kind { get; set; }
        public string MediaType { get; set; }
        public string Path { get; set; }
        public string Sha256 { get; set; }
        public long Size { get; set; }
        public long Frame { get; set; }
        public int Width { get; set; }
        public int Height { get; set; }
        public bool Fresh { get; set; }
        public string RuntimeRevision { get; set; }
        public long ViewportGeneration { get; set; }
        public long PublishedAtUnixMilliseconds { get; set; }
    }

    public interface IRuntimeObservationProvider
    {
        RelayLiveLoopResult<ObjectObservation> ObserveObject(RuntimeObjectHandle target, int maximumComponents);
        RelayLiveLoopResult<ComponentPropertyObservation> ObserveProperty(RuntimeObjectHandle component, string property);
    }

    public interface IComponentPreviewProvider
    {
        RelayLiveLoopResult<ComponentPreviewResult> Preview(ComponentPreviewRequest request);
        RelayLiveLoopResult<ComponentRevertResult> Revert(string taskId, string overlayId);
        int PurgeScopeGeneration(string scopeId, long generation);
        void Clear();
    }

    public interface IFreshFrameCaptureProvider
    {
        Task<RelayLiveLoopResult<FreshFrameArtifact>> Capture(
            FreshFrameCaptureRequest request,
            CancellationToken cancellationToken);
    }
}
#endif
