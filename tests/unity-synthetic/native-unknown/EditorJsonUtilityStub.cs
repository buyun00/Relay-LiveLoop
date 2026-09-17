#if RELAYLIVELOOP_EDITOR_STORE_SYNTHETIC
using System.IO;
using System.Runtime.Serialization.Json;
using System.Text;

namespace UnityEngine
{
    public static class JsonUtility
    {
        public static string ToJson(object value, bool prettyPrint = false)
        {
            using (var stream = new MemoryStream())
            {
                new DataContractJsonSerializer(value.GetType()).WriteObject(stream, value);
                var json = Encoding.UTF8.GetString(stream.ToArray());
                // Unity JsonUtility emits null string fields as empty strings. The
                // production store normalizer then restores its explicit JSON null.
                return json.Replace("\"resultJson\":null", "\"resultJson\":\"\"")
                    .Replace("\"resultJson\": null", "\"resultJson\": \"\"");
            }
        }

        public static T FromJson<T>(string json)
        {
            var bytes = Encoding.UTF8.GetBytes(json);
            using (var stream = new MemoryStream(bytes))
            {
                return (T)new DataContractJsonSerializer(typeof(T)).ReadObject(stream);
            }
        }
    }
}
#endif
