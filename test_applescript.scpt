tell application "System Events"
    tell process "Pro Tools"
        try
            set editWin to first window whose name contains "Edit:"
            set transGroup to first group of editWin whose name is "Transport View Cluster"
            set preBtn to first button of (entire contents of transGroup) whose (name contains "Pre" or name contains "pre")
            return "direct:" & (name of preBtn) & "=" & (value of preBtn as string)
        on error errMsg
            return "not_found: " & errMsg
        end try
    end tell
end tell
