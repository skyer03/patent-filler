using System;
using System.Diagnostics;
using System.IO;
using System.Threading.Tasks;

internal static class PatentAutofillNativeHostLauncher
{
    private static void CopyWithFlush(Stream source, Stream destination)
    {
        var buffer = new byte[8192];
        int count;
        while ((count = source.Read(buffer, 0, buffer.Length)) > 0)
        {
            destination.Write(buffer, 0, count);
            destination.Flush();
        }
    }

    private static string Quote(string value)
    {
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }

    public static int Main()
    {
        string launcherDirectory = AppDomain.CurrentDomain.BaseDirectory;
        string rootFile = Path.Combine(launcherDirectory, "project-root.txt");
        if (!File.Exists(rootFile)) return 21;
        string projectRoot = File.ReadAllText(rootFile).Trim();
        if (!Directory.Exists(projectRoot)) return 22;

        string bundledPython = Path.Combine(projectRoot, ".runtime", "python312-full", "python.exe");
        string venvPython = Path.Combine(projectRoot, ".venv", "Scripts", "python.exe");
        string python = File.Exists(bundledPython) ? bundledPython : venvPython;
        if (!File.Exists(python)) return 23;

        string store = Path.Combine(projectRoot, ".m6", "dom-bridge");
        var startInfo = new ProcessStartInfo
        {
            FileName = python,
            Arguments = "-m app --native-host --native-store " + Quote(store),
            WorkingDirectory = projectRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true
        };
        using (var child = Process.Start(startInfo))
        {
            if (child == null) return 24;
            var copyInput = Task.Run(() =>
            {
                try { CopyWithFlush(Console.OpenStandardInput(), child.StandardInput.BaseStream); }
                finally { try { child.StandardInput.Close(); } catch { } }
            });
            var copyOutput = Task.Run(() =>
            {
                CopyWithFlush(child.StandardOutput.BaseStream, Console.OpenStandardOutput());
            });
            Task.WaitAll(copyInput, copyOutput);
            child.WaitForExit();
            return child.ExitCode;
        }
    }
}
