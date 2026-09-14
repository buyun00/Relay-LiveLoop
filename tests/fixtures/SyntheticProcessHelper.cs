using System;
using System.IO;
using System.Threading;

internal static class SyntheticProcessHelper
{
    private static int Main(string[] args)
    {
        for (var index = 0; index + 1 < args.Length; index++)
        {
            if (string.Equals(args[index], "-projectPath", StringComparison.OrdinalIgnoreCase))
            {
                var projectPath = Path.GetFullPath(args[index + 1]);
                File.WriteAllLines(Path.Combine(projectPath, "synthetic-editor-arguments.txt"), args);
                break;
            }
        }

        Thread.Sleep(TimeSpan.FromMinutes(5));
        return 0;
    }
}
