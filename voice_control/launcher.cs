// Optional Windows GUI launcher. Build from the repository root with:
// C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /target:winexe /out:JevVoiceLauncher.exe voice_control\launcher.cs
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
        string python = Path.Combine(root, ".venv-voice", "Scripts", "pythonw.exe");
        if (!File.Exists(python))
        {
            MessageBox.Show("The voice environment is missing. Run the install commands in voice_control/README.md first.", "Jev Voice Control");
            return;
        }
        Process.Start(new ProcessStartInfo {
            FileName = python,
            Arguments = "-m voice_control.app",
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = true
        });
    }
}
