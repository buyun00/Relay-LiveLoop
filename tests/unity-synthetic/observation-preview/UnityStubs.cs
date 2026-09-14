#if RELAYLIVELOOP_SYNTHETIC
using System;
using System.Collections;
using System.Collections.Generic;
using System.Threading;

namespace UnityEngine
{
    public class Object
    {
        private static int _nextId;
        private readonly int _id = Interlocked.Increment(ref _nextId);
        public string name { get; set; }
        public int GetInstanceID() { return _id; }
        public static void Destroy(Object value) { }
    }

    public class Component : Object
    {
        public GameObject gameObject { get; internal set; }
    }

    public class Behaviour : Component
    {
        public bool enabled { get; set; } = true;
    }

    public class MonoBehaviour : Behaviour
    {
        protected Coroutine StartCoroutine(IEnumerator routine)
        {
            while (routine.MoveNext())
            {
                if (routine.Current == null) Time.AdvanceFrame();
                Thread.Sleep(1);
            }
            return new Coroutine();
        }
    }

    public sealed class Coroutine { }
    public sealed class WaitForEndOfFrame { }

    public struct Vector2
    {
        public Vector2(float xValue, float yValue) { x = xValue; y = yValue; }
        public float x;
        public float y;
    }

    public struct Vector3
    {
        public Vector3(float xValue, float yValue, float zValue) { x = xValue; y = yValue; z = zValue; }
        public float x;
        public float y;
        public float z;
    }

    public struct Quaternion
    {
        public Quaternion(float xValue, float yValue, float zValue, float wValue)
        {
            x = xValue; y = yValue; z = zValue; w = wValue;
        }
        public float x;
        public float y;
        public float z;
        public float w;
    }

    public struct Color32
    {
        public Color32(byte red, byte green, byte blue, byte alpha)
        {
            r = red; g = green; b = blue; a = alpha;
        }
        public byte r;
        public byte g;
        public byte b;
        public byte a;
    }

    public class Transform : Component
    {
        public Vector3 localPosition { get; set; }
        public Vector3 localScale { get; set; }
        public Vector3 localEulerAngles { get; set; }
        public Quaternion localRotation { get; set; }
        public Transform parent { get; set; }
    }

    public sealed class RectTransform : Transform
    {
        public Vector2 anchoredPosition { get; set; }
        public Vector2 sizeDelta { get; set; }
        public Vector2 anchorMin { get; set; }
        public Vector2 anchorMax { get; set; }
        public Vector2 pivot { get; set; }
    }

    public sealed class GameObject : Object
    {
        private readonly List<Component> _components = new List<Component>();

        public GameObject(string objectName, params Type[] componentTypes)
        {
            name = objectName;
            var transformType = typeof(Transform);
            for (var index = 0; index < componentTypes.Length; index++)
            {
                if (typeof(Transform).IsAssignableFrom(componentTypes[index]))
                {
                    transformType = componentTypes[index];
                    break;
                }
            }
            AddComponent(transformType);
            for (var index = 0; index < componentTypes.Length; index++)
            {
                if (!typeof(Transform).IsAssignableFrom(componentTypes[index])) AddComponent(componentTypes[index]);
            }
        }

        public bool activeSelf { get; set; } = true;
        public bool activeInHierarchy { get; set; } = true;
        public Transform transform { get { return (Transform)_components[0]; } }

        public Component AddComponent(Type type)
        {
            var component = (Component)Activator.CreateInstance(type);
            component.gameObject = this;
            component.name = type.Name;
            _components.Add(component);
            return component;
        }

        public T[] GetComponents<T>() where T : Component
        {
            var result = new List<T>();
            for (var index = 0; index < _components.Count; index++)
            {
                var typed = _components[index] as T;
                if (typed != null) result.Add(typed);
            }
            return result.ToArray();
        }
    }

    public sealed class Texture2D : Object
    {
        private readonly Color32[] _pixels;
        public Texture2D(int textureWidth, int textureHeight, Color32[] pixels)
        {
            width = textureWidth;
            height = textureHeight;
            _pixels = pixels;
        }
        public int width { get; private set; }
        public int height { get; private set; }
        public Color32[] GetPixels32() { return (Color32[])_pixels.Clone(); }
    }

    public static class ScreenCapture
    {
        public static int Width = 2;
        public static int Height = 2;
        public static Color32[] Pixels =
        {
            new Color32(255, 0, 0, 255), new Color32(0, 255, 0, 255),
            new Color32(0, 0, 255, 255), new Color32(255, 255, 255, 255)
        };

        public static Texture2D CaptureScreenshotAsTexture()
        {
            return new Texture2D(Width, Height, (Color32[])Pixels.Clone());
        }
    }

    public static class Time
    {
        public static int frameCount { get; private set; }
        public static double realtimeSinceStartupAsDouble { get; private set; }
        public static void Reset(int frame)
        {
            frameCount = frame;
            realtimeSinceStartupAsDouble = 0;
        }
        public static void AdvanceFrame()
        {
            frameCount++;
            realtimeSinceStartupAsDouble += 0.016;
        }
    }
}
#endif
