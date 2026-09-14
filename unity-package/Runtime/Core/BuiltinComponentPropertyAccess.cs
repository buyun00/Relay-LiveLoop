#if UNITY_EDITOR || DEVELOPMENT_BUILD
using UnityEngine;

namespace RelayLiveLoop
{
    internal static class BuiltinComponentPropertyAccess
    {
        public static bool TryRead(Component component, string property, out ComponentValue value)
        {
            value = null;
            if (component == null || string.IsNullOrEmpty(property)) return false;
            var rect = component as RectTransform;
            if (rect != null)
            {
                switch (property)
                {
                    case "anchoredPosition": value = FromVector2(rect.anchoredPosition); return true;
                    case "sizeDelta": value = FromVector2(rect.sizeDelta); return true;
                    case "anchorMin": value = FromVector2(rect.anchorMin); return true;
                    case "anchorMax": value = FromVector2(rect.anchorMax); return true;
                    case "pivot": value = FromVector2(rect.pivot); return true;
                }
            }

            var transform = component as Transform;
            if (transform != null)
            {
                switch (property)
                {
                    case "localPosition": value = FromVector3(transform.localPosition); return true;
                    case "localScale": value = FromVector3(transform.localScale); return true;
                    case "localEulerAngles": value = FromVector3(transform.localEulerAngles); return true;
                    case "localRotation": value = FromQuaternion(transform.localRotation); return true;
                }
            }

            return false;
        }

        public static bool TryWrite(Component component, string property, ComponentValue value)
        {
            if (component == null || value == null || string.IsNullOrEmpty(property)) return false;
            var rect = component as RectTransform;
            if (rect != null)
            {
                switch (property)
                {
                    case "anchoredPosition": return AssignVector2(value, assigned => rect.anchoredPosition = assigned);
                    case "sizeDelta": return AssignVector2(value, assigned => rect.sizeDelta = assigned);
                    case "anchorMin": return AssignVector2(value, assigned => rect.anchorMin = assigned);
                    case "anchorMax": return AssignVector2(value, assigned => rect.anchorMax = assigned);
                    case "pivot": return AssignVector2(value, assigned => rect.pivot = assigned);
                }
            }

            var transform = component as Transform;
            if (transform != null)
            {
                switch (property)
                {
                    case "localPosition": return AssignVector3(value, assigned => transform.localPosition = assigned);
                    case "localScale": return AssignVector3(value, assigned => transform.localScale = assigned);
                    case "localEulerAngles": return AssignVector3(value, assigned => transform.localEulerAngles = assigned);
                    case "localRotation": return AssignQuaternion(value, assigned => transform.localRotation = assigned);
                }
            }

            return false;
        }

        private static ComponentValue FromVector2(Vector2 value)
        {
            return ComponentValue.Vector2(value.x, value.y);
        }

        private static ComponentValue FromVector3(Vector3 value)
        {
            return ComponentValue.Vector3(value.x, value.y, value.z);
        }

        private static ComponentValue FromQuaternion(Quaternion value)
        {
            return ComponentValue.Quaternion(value.x, value.y, value.z, value.w);
        }

        private static bool AssignVector2(ComponentValue value, System.Action<Vector2> assign)
        {
            if (value.Kind != ComponentValueKind.Vector2) return false;
            assign(new Vector2((float)value.X, (float)value.Y));
            return true;
        }

        private static bool AssignVector3(ComponentValue value, System.Action<Vector3> assign)
        {
            if (value.Kind != ComponentValueKind.Vector3) return false;
            assign(new Vector3((float)value.X, (float)value.Y, (float)value.Z));
            return true;
        }

        private static bool AssignQuaternion(ComponentValue value, System.Action<Quaternion> assign)
        {
            if (value.Kind != ComponentValueKind.Quaternion) return false;
            assign(new Quaternion((float)value.X, (float)value.Y, (float)value.Z, (float)value.W));
            return true;
        }
    }
}
#endif
