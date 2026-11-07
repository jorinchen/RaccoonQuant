' 小浣熊量化选股 - 静默启动器（不显示任何命令行窗口）
Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")
WshShell.CurrentDirectory = FSO.GetParentFolderName(WScript.ScriptFullName)
WshShell.Run "cmd /c run.bat", 0, True
