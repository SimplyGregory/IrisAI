' Starts Iris from source, silently - no console window.
'
' On the first run this opens the setup wizard; after that it starts the panel
' and waits for the hotkey. The built IrisAI.exe does the same thing without
' needing Python, so this is only for running from the source folder.
'
' To start Iris with Windows, press Win+R, type   shell:startup   and put a
' shortcut to this file in the folder that opens.
'
' pythonw.exe is Python without a console window. Resolved from PATH so this
' keeps working if Python is upgraded or moved.

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

here = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = here

' 0 = hidden window, False = do not wait for it to exit
shell.Run "pythonw.exe """ & here & "\IrisAI.py""", 0, False
