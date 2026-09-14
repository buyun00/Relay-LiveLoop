#if RELAYLIVELOOP_SYNTHETIC
using System;
using System.Collections.Generic;
using System.Web.Script.Serialization;

namespace UnityEngine
{
    public class Object
    {
    }

    public class MonoBehaviour : Object
    {
    }

    public static class JsonUtility
    {
        public static string ToJson(object value, bool prettyPrint = false)
        {
            return new JavaScriptSerializer().Serialize(value);
        }

        public static T FromJson<T>(string json)
        {
            return new JavaScriptSerializer().Deserialize<T>(json);
        }
    }
}

namespace UnityEditor
{
    [AttributeUsage(AttributeTargets.Class)]
    public sealed class InitializeOnLoadAttribute : Attribute
    {
    }

    public static class EditorApplication
    {
        public static event Action update;

        public static void RaiseUpdate()
        {
            var handler = update;
            if (handler != null) handler();
        }
    }

    public static class AssemblyReloadEvents
    {
        public static event Action beforeAssemblyReload;

        public static void RaiseBeforeAssemblyReload()
        {
            var handler = beforeAssemblyReload;
            if (handler != null) handler();
        }
    }

    public static class SessionState
    {
        private static readonly Dictionary<string, string> Values =
            new Dictionary<string, string>(StringComparer.Ordinal);

        public static void SetString(string key, string value)
        {
            Values[key] = value;
        }

        public static string GetString(string key, string defaultValue)
        {
            string value;
            return Values.TryGetValue(key, out value) ? value : defaultValue;
        }

        public static void EraseString(string key)
        {
            Values.Remove(key);
        }
    }
}
#endif
