#if UNITY_EDITOR
using System.Web.Script.Serialization;
using RelayLiveLoop;

namespace UnityEngine
{
    public static class JsonUtility
    {
        public static string ToJson(object value, bool prettyPrint = false)
        {
            var json = new JavaScriptSerializer().Serialize(value);
            var result = value as EditorJobResult;
            if (result != null && result.resultJson == null)
            {
                // Unity JsonUtility serializes a null string field as "".
                json = json.Replace("\"resultJson\":null", "\"resultJson\":\"\"");
            }

            return json;
        }

        public static T FromJson<T>(string json)
        {
            return new JavaScriptSerializer().Deserialize<T>(json);
        }
    }
}
#endif

