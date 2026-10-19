' FactoryTalk View SE display code-behind (original sample for Rungstone tests)
' Resets the batch counter and stamps the operator action.
Private Sub pbResetCount_Released()
    Dim tg As Tag
    Set tg = gTagGroup.Item("[FILL_PLC]HMI_Reset_Count")
    tg.Value = 1
    LogDiagnosticsMessage "Batch count reset by " & CurrentUserName()
End Sub

Private Sub Display_AnimationStart()
    Dim acc
    acc = gTagGroup.Item("[FILL_PLC]Fill_Timer.ACC").Value
    If acc > 60000 Then
        MsgBox "Fill time longer than one minute"
    End If
End Sub
