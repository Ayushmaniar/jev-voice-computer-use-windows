// Windows GUI launcher that carries the app icon. installer\build.ps1 builds it into the installed app,
// where it sits next to the bundled Python in runtime\; install_start_menu.ps1 builds it for a source
// checkout, where it is given the repository root and uses .venv-voice. By hand, from the repository root:
// C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /target:winexe /win32icon:voice_control\assets\jev-voice-logo.ico /out:JevVoiceLauncher.exe voice_control\launcher.cs
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("Jev Voice Control")]
[assembly: AssemblyDescription("Push-to-talk control for Windows apps")]
[assembly: AssemblyProduct("Jev Voice Control")]
[assembly: AssemblyCompany("Jev")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

internal static class JevVoiceLauncher
{
    [STAThread]
    private static void Main(string[] args)
    {
        string root = args.Length > 0 ? Path.GetFullPath(args[0]) : AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(root, "runtime", "pythonw.exe");
        // The bundled runtime ignores PYTHON* variables, the user's site-packages and the current folder (-E -s -P), so
        // Python packages installed elsewhere on the PC can't shadow the app's own.
        string flags = "-E -s -P ";
        if (!File.Exists(python))
        {
            python = Path.Combine(root, ".venv-voice", "Scripts", "pythonw.exe");
            flags = "";
        }
        if (!File.Exists(python))
        {
            MessageBox.Show("Jev Voice's Python runtime is missing. Reinstall Jev Voice Control, or for a source " +
                            "checkout run the install commands in voice_control/README.md.", "Jev Voice Control");
            return;
        }
        Process.Start(new ProcessStartInfo {
            FileName = python,
            Arguments = flags + "-m voice_control.app",
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = true
        });
    }
}
