#if RELAYLIVELOOP_RELEASE_INSPECTOR
using System;
using System.Reflection;

internal static class ReleaseSurfaceInspector
{
    public static int Main(string[] arguments)
    {
        if (arguments.Length != 1) return 2;
        var assembly = Assembly.LoadFile(arguments[0]);
        if (assembly.GetExportedTypes().Length != 0) return 1;
        Console.WriteLine("RELEASE ISOLATION PASS: zero exported runtime control types without UNITY_EDITOR or DEVELOPMENT_BUILD.");
        return 0;
    }
}
#endif
