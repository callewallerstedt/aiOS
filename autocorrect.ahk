#Requires AutoHotkey v2.0
#SingleInstance Force

^!+F12::Suspend

global InsertDownAt := 0
global InsertHoldActive := false
global InsertLongFired := false
global DiscordMutedByUs := false
global WsaInited := false
global VoiceHoldMs := 280
global VoiceSettingsCheckedAt := 0
global DiscordMuteEnabled := false
global DiscordMuteExplicit := ""
global DiscordMuteSendDelayMs := 35
global VoiceHotkey := "Insert"          ; current hotkey name (AHK key syntax)
global VoiceHotkeyRegistered := ""      ; what we actually registered last
global VoiceHotkeyDown := ""            ; the "$<key>" string we registered
global VoiceHotkeyUp := ""              ; the "$<key> up" string we registered

AiosHeartbeat() {
    path := A_ScriptDir "\.aios-ahk-heartbeat"
    try FileDelete(path)
    try FileAppend(A_NowUTC, path, "UTF-8")
}

VoiceHotkeyDownHandler(*) {
    global InsertDownAt, InsertHoldActive, InsertLongFired
    if (InsertHoldActive) {
        return
    }
    LoadVoiceConfig()
    InsertHoldActive := true
    InsertLongFired := false
    InsertDownAt := A_TickCount
    SetTimer(InsertHoldThreshold, -VoiceHoldMs)
}

VoiceHotkeyUpHandler(*) {
    global InsertHoldActive, InsertLongFired
    SetTimer(InsertHoldThreshold, 0)
    if (!InsertHoldActive) {
        return
    }
    InsertHoldActive := false
    if (InsertLongFired) {
        VoiceStopFast()
        DiscordMuteStop()
    } else {
        AiosToggleNow()
    }
}

InsertHoldThreshold() {
    global InsertHoldActive, InsertLongFired
    if (!InsertHoldActive || InsertLongFired) {
        return
    }
    InsertLongFired := true
    DiscordMuteStart()
    VoiceStartFast()
}

RegisterVoiceHotkey(keyName) {
    global VoiceHotkeyRegistered, VoiceHotkeyDown, VoiceHotkeyUp
    if (keyName = "") {
        keyName := "Insert"
    }
    if (VoiceHotkeyRegistered = keyName) {
        return
    }
    ; Tear down any previous registration cleanly.
    if (VoiceHotkeyDown != "") {
        try Hotkey VoiceHotkeyDown, "Off"
    }
    if (VoiceHotkeyUp != "") {
        try Hotkey VoiceHotkeyUp, "Off"
    }
    newDown := "$" . keyName
    newUp := "$" . keyName . " up"
    try {
        Hotkey newDown, VoiceHotkeyDownHandler
        Hotkey newUp, VoiceHotkeyUpHandler
        VoiceHotkeyRegistered := keyName
        VoiceHotkeyDown := newDown
        VoiceHotkeyUp := newUp
    } catch as err {
        ; If the new key is invalid, fall back to Insert so the user can
        ; always recover.
        if (keyName != "Insert") {
            RegisterVoiceHotkey("Insert")
        }
    }
}

LoadVoiceConfig() {
    global VoiceHoldMs, VoiceSettingsCheckedAt, DiscordMuteEnabled, DiscordMuteExplicit, VoiceHotkey
    now := A_TickCount
    if (now - VoiceSettingsCheckedAt < 1500) {
        return
    }
    VoiceSettingsCheckedAt := now
    path := A_ScriptDir "\helper_config.json"
    holdMs := 280
    discordEnabled := false
    discordHotkey := ""
    voiceHotkey := "Insert"
    if FileExist(path) {
        try {
            txt := FileRead(path, "UTF-8")
            if RegExMatch(txt, '"hold_ms"\s*:\s*(\d+)', &m) {
                holdMs := Integer(m[1])
            } else if RegExMatch(txt, '"double_press_ms"\s*:\s*(\d+)', &m) {
                holdMs := Integer(m[1])
            }
            if RegExMatch(txt, '"discord_mute_enabled"\s*:\s*(true|false)', &m) {
                discordEnabled := (m[1] = "true")
            }
            if RegExMatch(txt, '"discord_mute_hotkey"\s*:\s*"([^"]*)"', &m) {
                discordHotkey := m[1]
            }
            if RegExMatch(txt, '"voice_hotkey"\s*:\s*"([^"]*)"', &m) {
                voiceHotkey := m[1]
            }
        } catch {
        }
    }
    if (holdMs < 150) {
        holdMs := 150
    }
    if (holdMs > 800) {
        holdMs := 800
    }
    VoiceHoldMs := holdMs
    DiscordMuteEnabled := discordEnabled
    DiscordMuteExplicit := HotkeyToExplicitSendSequence(discordHotkey)
    VoiceHotkey := voiceHotkey = "" ? "Insert" : voiceHotkey
    RegisterVoiceHotkey(VoiceHotkey)
}

LoadDiscordMuteSettings() {
    global DiscordMuteEnabled, DiscordMuteExplicit
    path := A_ScriptDir "\helper_config.json"
    discordEnabled := false
    discordHotkey := ""
    if FileExist(path) {
        try {
            txt := FileRead(path, "UTF-8")
            if RegExMatch(txt, '"discord_mute_enabled"\s*:\s*(true|false)', &m) {
                discordEnabled := (m[1] = "true")
            }
            if RegExMatch(txt, '"discord_mute_hotkey"\s*:\s*"([^"]*)"', &m) {
                discordHotkey := m[1]
            }
        } catch {
        }
    }
    DiscordMuteEnabled := discordEnabled
    DiscordMuteExplicit := HotkeyToExplicitSendSequence(discordHotkey)
}

LoadVoiceHoldMs() {
    LoadVoiceConfig()
    return VoiceHoldMs
}

HotkeyToExplicitSendSequence(hotkey) {
    hotkey := NormalizeHotkeyInput(hotkey)
    if (hotkey = "") {
        return ""
    }
    parts := StrSplit(hotkey, "+")
    if (parts.Length = 0) {
        return ""
    }
    key := Trim(parts[parts.Length])
    down := ""
    up := ""
    if (parts.Length > 1) {
        Loop parts.Length - 1 {
            part := StrLower(Trim(parts[A_Index]))
            if (part = "ctrl" || part = "control") {
                down .= "{Ctrl down}"
                up := "{Ctrl up}" . up
            } else if (part = "shift") {
                down .= "{Shift down}"
                up := "{Shift up}" . up
            } else if (part = "alt" || part = "menu" || part = "altgr") {
                down .= "{Alt down}"
                up := "{Alt up}" . up
            } else if (part = "win" || part = "lwin" || part = "rwin" || part = "windows") {
                down .= "{LWin down}"
                up := "{LWin up}" . up
            }
        }
    }
    keyLower := StrLower(key)
    special := Map(
        "enter", "Enter", "return", "Enter", "space", "Space", "tab", "Tab",
        "esc", "Escape", "escape", "Escape", "backspace", "Backspace",
        "delete", "Delete", "del", "Delete", "insert", "Insert",
        "home", "Home", "end", "End", "pgup", "PgUp", "pgdn", "PgDn",
        "pageup", "PgUp", "pagedown", "PgDn",
    )
    keyName := ""
    if special.Has(keyLower) {
        keyName := special[keyLower]
    } else if RegExMatch(key, "i)^F(\d{1,2})$") {
        keyName := StrUpper(key)
    } else if (StrLen(key) = 1) {
        keyName := StrLower(key)
    } else {
        keyName := key
    }
    keySend := "{" . keyName . " down}{" . keyName . " up}"
    return down . keySend . up
}

SendDiscordHotkey(clearVoiceKey := true) {
    global DiscordMuteExplicit, DiscordMuteSendDelayMs, VoiceHotkey
    if (DiscordMuteExplicit = "") {
        return
    }
    if (clearVoiceKey) {
        ; The voice hotkey is still physically held during hold-to-dictate —
        ; release it virtually so Discord sees the mute chord cleanly.
        SendEvent("{" . VoiceHotkey . " up}")
        Sleep(DiscordMuteSendDelayMs)
    }
    SetKeyDelay(DiscordMuteSendDelayMs, DiscordMuteSendDelayMs)
    SendEvent(DiscordMuteExplicit)
    Sleep(DiscordMuteSendDelayMs)
}

NormalizeHotkeyInput(hotkey) {
    hotkey := Trim(hotkey)
    if (hotkey = "") {
        return ""
    }
    if InStr(hotkey, "+") {
        return RegExReplace(hotkey, "\s*\+\s*", "+")
    }
    return RegExReplace(hotkey, "\s+", "+")
}

DiscordMuteStart() {
    global DiscordMutedByUs, DiscordMuteEnabled, DiscordMuteExplicit
    LoadDiscordMuteSettings()
    if (!DiscordMuteEnabled || DiscordMuteExplicit = "") {
        return
    }
    SendDiscordHotkey(true)
    DiscordMutedByUs := true
}

DiscordMuteStop() {
    global DiscordMutedByUs, DiscordMuteEnabled, DiscordMuteExplicit
    if (!DiscordMutedByUs) {
        return
    }
    LoadDiscordMuteSettings()
    if (DiscordMuteEnabled && DiscordMuteExplicit != "") {
        SendDiscordHotkey(false)
    }
    DiscordMutedByUs := false
}

AiosToggleNow() {
    pythonw := A_ScriptDir "\.venv\Scripts\pythonw.exe"
    if !FileExist(pythonw) {
        pythonw := "C:\Python313\pythonw.exe"
    }
    if !FileExist(pythonw) {
        pythonw := "pythonw.exe"
    }
    Run('"' pythonw '" "' A_ScriptDir '\helper_overlay.py" --toggle', A_ScriptDir)
}

; Dedicated aiOS launcher requested for this desktop. Insert keeps its
; existing tap-to-open / hold-to-dictate behavior.
^Space::AiosToggleNow()

; --- fast TCP send to the voice server (no Python spawn) ---

EnsureWSA() {
    global WsaInited
    if (WsaInited) {
        return true
    }
    buf := Buffer(400, 0)
    if (DllCall("ws2_32\WSAStartup", "UShort", 0x202, "Ptr", buf.Ptr, "Int") != 0) {
        return false
    }
    WsaInited := true
    return true
}

SendToVoice(msg) {
    if !EnsureWSA() {
        return false
    }
    sock := DllCall("ws2_32\socket", "Int", 2, "Int", 1, "Int", 6, "UInt")
    if (sock = -1 || sock = 0xFFFFFFFF) {
        return false
    }
    addr := Buffer(16, 0)
    NumPut("UShort", 2, addr, 0)
    NumPut("UShort", DllCall("ws2_32\htons", "UShort", 48737, "UShort"), addr, 2)
    NumPut("UInt", 0x0100007F, addr, 4)   ; 127.0.0.1 in network byte order
    res := DllCall("ws2_32\connect", "Ptr", sock, "Ptr", addr.Ptr, "Int", 16, "Int")
    if (res != 0) {
        DllCall("ws2_32\closesocket", "Ptr", sock)
        return false
    }
    size := StrPut(msg, "UTF-8")
    payload := Buffer(size, 0)
    StrPut(msg, payload, "UTF-8")
    DllCall("ws2_32\send", "Ptr", sock, "Ptr", payload.Ptr, "Int", size - 1, "Int", 0, "Int")
    DllCall("ws2_32\closesocket", "Ptr", sock)
    return true
}

VoiceStartFast() {
    if (SendToVoice("start")) {
        return
    }
    pythonw := A_ScriptDir "\.venv\Scripts\pythonw.exe"
    if !FileExist(pythonw) {
        pythonw := "C:\Python313\pythonw.exe"
    }
    if !FileExist(pythonw) {
        pythonw := "pythonw.exe"
    }
    Run('"' pythonw '" "' A_ScriptDir '\voice_dictation.py" --start', A_ScriptDir, "Hide")
}

VoiceStopFast() {
    SendToVoice("stop")
}

; Initial config load registers the hotkey before the user does anything.
LoadVoiceConfig()
; Poll the config so Settings → Voice hotkey changes take effect within 2s
; even when the user hasn't pressed the current hotkey.
SetTimer(LoadVoiceConfig, 1700)
AiosHeartbeat()
SetTimer(AiosHeartbeat, 5000)

::constantyl::constantly
::missspell::misspell
::missspelled::misspelled
::missspelling::misspelling
::autocorect::autocorrect
::autocorret::autocorrect
::teh::the
::adn::and
::dont::don't
::cant::can't
::wont::won't
::im::I'm
::ive::I've
::ill::I'll
::intuaive::intuitive
::intuative::intuitive
::visulas::visuals
::becuase::because
::seperate::separate
::definately::definitely
::recieve::receive
::pelase::please
::wallersetdt::wallerstedt
::taht::that
::yuo::you
::@gm::calle.wallerstedt@gmail.com
::@cm::callew@chalmers.se
